"""Build local C or assembly workloads and time their execution under QEMU."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import fcntl
import math
from pathlib import Path
import shlex
import shutil
import subprocess

import configuration
from measurement import summarize
import run as build
from toolchain import sha256, write_json


PERF_EVENT = 'cycles:u'
PERF_FREQUENCY = '999'


def arguments(argv, *, defaults):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('programs', nargs='*', metavar='WORKLOAD')
    parser.add_argument('--cross-cc', default='riscv64-linux-gnu-gcc')
    parser.add_argument('--march', default='rv64gcv')
    parser.add_argument('--cflags', default='', help='extra build flags, after the default -O3')
    parser.add_argument('--cpu', default='rv64,v=true,vlen=128,elen=64')
    parser.add_argument('--data-dir', type=Path, default=build.DEFAULT_DATA)
    parser.add_argument('--output', type=Path, help='new result directory')
    parser.add_argument('--warmups', type=int, default=1)
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--timeout', type=float, default=1800, help='per-command timeout in seconds')
    parser.add_argument('--perf-profile', action='store_true', help='record one separate host cycles profile per variant')
    parser.add_argument('--profile-symbol', help='include this QEMU function in the profile summary')
    parser.set_defaults(**defaults)
    args = parser.parse_args(argv)
    catalog = configuration.documents()
    if not args.programs or any(name not in catalog or catalog[name]['definition']['kind'] != 'guest-program'
                               for name in args.programs):
        parser.error('choose one or more guest-program workload IDs')
    args.programs = list(dict.fromkeys(args.programs))
    if args.repetitions < 1 or args.warmups < 0 or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('positive repetitions/timeout and nonnegative warmups required')
    if args.profile_symbol is not None and (not args.perf_profile or not args.profile_symbol.strip()):
        parser.error('--profile-symbol needs a nonempty symbol name and --perf-profile')
    shlex.split(args.cflags)
    return args


def build_input(workload, directory, cross_cc, args, env):
    filename = Path(workload['definition']['source']['path']).expanduser()
    source = Path(workload['path']).parent / filename
    copy = directory / source.name
    shutil.copyfile(source, copy)
    guest = directory / 'guest.elf'
    command = [cross_cc, '-O3', '-g0', f'-march={args.march}', '-mabi=lp64d',
               copy.name, *shlex.split(args.cflags), '-static', '-o', guest.name]
    build.invoke(command, directory, env, directory / 'build.log', args.timeout)
    build.check_riscv(guest)
    return dict(path=str(guest), sha256=sha256(guest), source=str(copy), source_sha256=sha256(copy),
                compile_command=command)


def execute(workload, guest, variant, directory, args, env, prefix=()):
    executable = Path(guest['path'])
    if sha256(executable) != guest['sha256']:
        raise RuntimeError(f'Guest executable changed: {executable}')
    build.verify_qemu(variant)
    directory.mkdir()
    stdout = directory / 'stdout'
    command = [*prefix, variant['path'], '-cpu', args.cpu, str(executable), *workload.get('args', [])]
    row = build.invoke(command, directory, env, directory / 'run.log', args.timeout, stdout=stdout)
    if stdout.read_bytes() != workload['expected_stdout'].encode('utf-8'):
        raise RuntimeError(f'{workload["id"]}/{variant["label"]}: guest stdout differs from expected_stdout; see {stdout}')
    build.verify_qemu(variant)
    if sha256(executable) != guest['sha256']:
        raise RuntimeError(f'Guest executable changed: {executable}')
    return dict(row, command=command, directory=str(directory), output_sha256=sha256(stdout))


def profile(workload, guest, variant, directory, args, env):
    perf = build.executable('perf')
    env = dict(env, PERF_CONFIG='/dev/null', DEBUGINFOD_URLS='')
    data = directory / 'perf.data'
    report_path = directory / 'perf-report.txt'
    prefix = [perf, 'record', '--no-buildid-cache', '-e', PERF_EVENT, '-F', PERF_FREQUENCY, '-o', str(data), '--']
    run = execute(workload, guest, variant, directory, args, env, prefix)
    report_command = [perf, 'report', '--stdio', '--stdio-color', 'never', '--no-children',
                      '--sort', 'dso,symbol', '--percent-limit', '0', '--show-nr-samples',
                      '--show-total-period', '--field-separator', '|', '-i', str(data)]
    build.invoke(report_command, directory, env, directory / 'run.log', args.timeout, stdout=report_path)

    samples = total_period = symbol_samples = symbol_period = 0
    qemu_dso = Path(variant['path']).name
    for line in report_path.read_text().splitlines():
        if line.startswith('#') or '|' not in line:
            continue
        _, count, period, dso, symbol, *_ = (field.strip() for field in line.split('|'))
        samples += int(count)
        total_period += int(period)
        if dso == qemu_dso and symbol.removeprefix('[.] ') == args.profile_symbol:
            symbol_samples += int(count)
            symbol_period += int(period)
    if not samples or not total_period:
        raise RuntimeError(f'No {PERF_EVENT} samples in {report_path}')
    return dict(status='complete', event=PERF_EVENT, frequency_hz=int(PERF_FREQUENCY),
                perf_version=build.command_output([perf, '--version']),
                command=run['command'], report_command=report_command, data=str(data), report=str(report_path),
                output_sha256=run['output_sha256'], total_samples=samples, total_period=total_period,
                symbol=args.profile_symbol, symbol_samples=symbol_samples, symbol_period=symbol_period,
                symbol_overhead_percent=100 * symbol_period / total_period if args.profile_symbol else None)


def report(result, output):
    result['summary'] = summarize(result['runs'], result['cases'], [q['label'] for q in result['qemu']])
    write_json(output / 'results.json', result)
    with (output / 'samples.csv').open('w', newline='') as stream:
        fields = ['case', 'phase', 'pair', 'position', 'variant', 'wall_seconds',
                  'user_seconds', 'sys_seconds', 'output_sha256']
        writer = csv.DictWriter(stream, fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(result['runs'])
    lines = ['# Guest program benchmark', '', f"Status: {result['status']}", '',
             '## Execution time', '',
             '| Workload | QEMU | Runs | Median (s) | Min–max (s) | Wall change (s) | Wall change | Wall / baseline |',
             '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for row in result['summary']:
        lines.append(f"| {row['case']} | {row['variant']} | {row['runs']} | {row['median_seconds']:.6f} | "
                     f"{row['min_seconds']:.6f}–{row['max_seconds']:.6f} | "
                     f"{row['wall_change_seconds']:.6f} | {row['wall_change_percent']:.2f}% | {row['wall_ratio']:.3f}x |")
    if result['profiles']:
        lines += ['', '## Separate perf profiles', '',
                  '| Workload | QEMU | Event | Samples | Symbol | Symbol samples | Overhead | Report |',
                  '| --- | --- | --- | ---: | --- | ---: | ---: | --- |']
        failures = []
        for row in result['profiles']:
            if row['status'] != 'complete':
                failures.append(f"- {row['case']}/{row['variant']} profile unavailable. {row['error']}")
                continue
            symbol = f"`{row['symbol']}`" if row['symbol'] else '—'
            count = row['symbol_samples'] if row['symbol'] else '—'
            share = f"{row['symbol_overhead_percent']:.2f}%" if row['symbol'] else '—'
            link = Path(row['report']).relative_to(output).as_posix()
            lines.append(f"| {row['case']} | {row['variant']} | {row['event']} | {row['total_samples']} | "
                         f"{symbol} | {count} | {share} | [perf report]({link}) |")
        if failures:
            lines += ['', *failures]
    (output / 'summary.md').write_text('\n'.join(lines) + '\n')


def main(*, prepared_args):
    args = prepared_args
    qemus = build.variants(args)
    cross_cc = build.executable(args.cross_cc)
    toolchain = dict(path=cross_cc, sha256=sha256(Path(cross_cc)),
                     machine=build.command_output([cross_cc, '-dumpmachine']),
                     version=build.command_output([cross_cc, '-dumpfullversion']))
    if toolchain['machine'] != 'riscv64-linux-gnu':
        raise RuntimeError(f'Expected a riscv64-linux-gnu cross compiler, got {toolchain["machine"]}')
    data = args.data_dir.resolve()
    data.mkdir(parents=True, exist_ok=True)
    with (data / '.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(f'Another benchmark is using {data}; choose another --data-dir') from error
        now = datetime.now(timezone.utc)
        output = (args.output or data / 'programs' / now.strftime('%Y%m%dT%H%M%S.%fZ')).resolve()
        output.mkdir(parents=True, exist_ok=False)
        print('Results:', output, flush=True)
        result = dict(schema=1, status='running', cases=args.programs, qemu=qemus, cpu=args.cpu,
                      reference=qemus[0]['label'], inputs={}, runs=[], profiles=[], started_utc=now.isoformat())
        try:
            affinity = configuration.apply_affinity(args.affinity)
            configuration.write_manifest(output, args, qemus, args.programs, affinity,
                                         runner='programs', cross_toolchain=toolchain,
                                         host_environment_before=build.host_environment())
            catalog = configuration.documents()
            env = build.clean_environment()
            for name in args.programs:
                directory = output / name
                directory.mkdir()
                result['inputs'][name] = build_input(catalog[name], directory, cross_cc, args, env)
            for name in args.programs:
                for phase, rounds in [('warmup', args.warmups), ('measured', args.repetitions)]:
                    for iteration in range(rounds):
                        offset = iteration % len(qemus) if phase == 'measured' else 0
                        order = qemus[offset:] + qemus[:offset]
                        for position, variant in enumerate(order, 1):
                            tag = f'{phase}-{iteration+1:02d}-{variant["label"]}'
                            row = execute(catalog[name]['definition'], result['inputs'][name], variant,
                                          output / name / tag, args, env)
                            row.update(case=name, phase=phase, pair=iteration+1, position=position,
                                       variant=variant['label'])
                            result['runs'].append(row)
                            report(result, output)
                            print(f"{name}/{tag}: {row['wall_seconds']:.6f}s", flush=True)
            if args.perf_profile:
                for name in args.programs:
                    for variant in qemus:
                        directory = output / name / f'profile-{variant["label"]}'
                        try:
                            row = profile(catalog[name]['definition'], result['inputs'][name], variant,
                                          directory, args, env)
                        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
                            row = dict(status='unavailable', error=str(error).splitlines()[0])
                        result['profiles'].append(dict(case=name, variant=variant['label'], **row))
                        print(f"{name}/{directory.name}: {row['status']}", flush=True)
            result['status'] = 'complete'
        except BaseException as error:
            result.update(status='failed', error=str(error))
            raise
        finally:
            result['finished_utc'] = datetime.now(timezone.utc).isoformat()
            result['host_environment_after'] = build.host_environment()
            report(result, output)
    print((output / 'summary.md').read_text(), flush=True)
    return 0
