#!/usr/bin/env python3
"""Compare QEMU's vsetvli paths with checked, libc-free RISC-V guests."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import platform
import resource
import shutil
import struct
import subprocess
import sys
import time

import run as build
import configuration
from measurement import summarize
from toolchain import sha256, write_json

HERE = Path(__file__).resolve().parent
DEFINITION = configuration.micro_definition('vsetvli')
CPU = DEFINITION['cpu']
CASES = DEFINITION['cases']
MAX_ITERATIONS = (1 << 60) - 1


def expected_output(name: str, n: int) -> bytes:
    case = CASES[name]
    maximum = case['maximum']
    active = n if case['mode'] in ('max', 'keep') else n // 2
    vl = maximum if case['mode'] in ('max', 'keep') or n % 2 == 0 else 0
    vtype = {'m1': 0, 'mf2': 7, 'mf4': 6, None: 0}[case['lmul']]
    total = maximum * active if case['summed'] else 0
    vector = bytes([active % 256]) * maximum if case['lmul'] else bytes(maximum)
    if case['mode'] == 'scalar':
        total, vl = maximum * ((n + 1) // 2), 0
    return struct.pack('<QQQQ', total, vl, vtype, 0) + vector


def build_inputs(names: list[str], output: Path, cross_cc: str) -> dict:
    directory = output / 'inputs'
    directory.mkdir()
    cc = build.executable(cross_cc)
    machine = build.command_output([cc, '-dumpmachine'])
    if machine != 'riscv64-linux-gnu':
        raise RuntimeError('Use a riscv64-linux-gnu C cross compiler')
    nm = build.executable(machine + '-nm')
    objdump = build.executable(machine + '-objdump')
    inputs = {}
    for name in names:
        source = HERE / 'vsetvli' / (name + '.S')
        if sha256(source) != CASES[name]['source_sha256']:
            raise RuntimeError(f'Input source checksum mismatch: {source}')
        copy = directory / source.name
        shutil.copy2(source, copy)
        guest = directory / (name + '.elf')
        march = 'rv64gc' if name == 'scalar-control' else 'rv64gcv'
        command = [cc, '-static', '-nostdlib', '-march=' + march,
                   '-mabi=lp64d', '-o', str(guest), str(copy)]
        subprocess.run(command, env=build.clean_environment(), check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        symbols = {parts[2]: int(parts[0], 16)
                   for line in build.command_output([nm, '-n', str(guest)]).splitlines()
                   if len(parts := line.split()) == 3}
        if symbols['vector_config'] >> 12 != symbols['vector_successor'] >> 12:
            raise RuntimeError(f'{name}: vset and successor must share a 4 KiB guest page')
        disassembly = build.command_output([objdump, '-d', str(guest)])
        (directory / (name + '.dis')).write_text(disassembly + '\n')
        if name == 'scalar-control':
            # Match instruction mnemonics, excluding labels such as vector_config.
            import re
            if re.search(r'^\s*[0-9a-f]+:\s+[0-9a-f]+\s+v\w+', disassembly, re.M):
                raise RuntimeError('Scalar control unexpectedly contains vector instructions')
        inputs[name] = dict(CASES[name], path=str(guest), sha256=sha256(guest),
                           compile_command=command,
                           symbols={key: symbols[key] for key in
                                    ('benchmark_loop', 'vector_config', 'vector_successor')})
    write_json(directory / 'manifest.json', inputs)
    return inputs


def execute(name, variant, inputs, n, directory, tag, timeout, perf=False):
    build.verify_qemu(variant)
    guest = inputs[name]
    if sha256(Path(guest['path'])) != guest['sha256']:
        raise RuntimeError(f'Guest executable changed: {name}')
    command = [variant['path'], '-cpu', CPU, guest['path'], str(n)]
    stat = directory / (tag + '.perf.csv')
    if perf:
        command = ['perf', 'stat', '-x,', '--no-big-num', '-o', str(stat),
                   '-e', 'cycles:u,instructions:u,branches:u,branch-misses:u', '--', *command]
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started_utc = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    # Captured pipes use communicate(), avoiding timeout polling in the timed wait.
    result = subprocess.run(command, capture_output=True, env=build.clean_environment(), timeout=timeout)
    wall = time.perf_counter() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    (directory / (tag + '.stdout')).write_bytes(result.stdout)
    (directory / (tag + '.stderr')).write_bytes(result.stderr)
    if result.returncode or result.stdout != expected_output(name, n):
        raise RuntimeError(f'{name}/{variant["label"]}: failed output/exit check; see {directory / tag}')
    build.verify_qemu(variant)
    row = dict(case=name, variant=variant['label'], iterations=n, wall_seconds=wall,
               user_seconds=after.ru_utime - before.ru_utime,
               sys_seconds=after.ru_stime - before.ru_stime,
               started_utc=started_utc, command=command, returncode=result.returncode,
               output_sha256=sha256(directory / (tag + '.stdout')))
    if perf:
        counters = {}
        for fields in csv.reader(stat.read_text().splitlines()):
            if len(fields) > 2 and fields[2].strip() in (
                    'cycles:u', 'instructions:u', 'branches:u', 'branch-misses:u'):
                try:
                    counters[fields[2].strip()] = float(fields[0])
                except ValueError as error:
                    raise RuntimeError(f'Unavailable perf counter; see {stat}') from error
        if len(counters) != 4:
            raise RuntimeError(f'perf did not return all four counters; see {stat}')
        row['counters'] = counters
        row['timing_usable'] = False
    return row


def report(result, output):
    result['summary'] = summarize(result['runs'], result['cases'], [q['label'] for q in result['qemu']])
    write_json(output / 'results.json', result)
    with (output / 'samples.csv').open('w', newline='') as stream:
        fields = ['case', 'phase', 'pair', 'position', 'variant', 'iterations',
                  'wall_seconds', 'user_seconds', 'sys_seconds', 'output_sha256']
        writer = csv.DictWriter(stream, fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(result['runs'])
    lines = ['# vsetvli benchmark', '', 'Status: ' + result['status'], '',
             'Wall time includes process startup. Diagnostic/perf runs are excluded.', '',
             '| Case | QEMU | Runs | Median (s) | Min–max (s) | Wall change | Wall / baseline | Faster pairs |',
             '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for row in result['summary']:
        lines.append(f"| {row['case']} | {row['variant']} | {row['runs']} | {row['median_seconds']:.6f} | "
                     f"{row['min_seconds']:.6f}–{row['max_seconds']:.6f} | "
                     f"{row['wall_change_percent']:.2f}% | {row['wall_ratio']:.3f}x | "
                     f"{row['faster_pairs']}/{len(row['paired_wall_ratios'])} |")
    lines += ['', 'Wall / baseline is the ratio of median elapsed times. Lower is faster.', '']
    if result['diagnostics']:
        lines += ['## Untimed lookup diagnostics', '',
                  'Entries at the tested successor PC; 37 guest iterations per fresh process.', '',
                  '| Case | QEMU | helper | TB-state extraction | dispatcher |',
                  '| --- | --- | ---: | ---: | ---: |']
        for row in result['diagnostics']:
            counts = row['target_successor']
            lines.append(f"| {row['case']} | {row['variant']} | {counts['helper']} | "
                         f"{counts['state_lookup']} | {counts['dispatcher']} |")
        lines += ['']
    (output / 'summary.md').write_text('\n'.join(lines))


def lookup_counts(variant, name, inputs, directory, timeout):
    if platform.machine() != 'x86_64':
        raise RuntimeError('--lookup-counts currently requires an x86-64 Linux host')
    gdb = build.executable('gdb')
    config_path = directory / f'{name}-{variant["label"]}.config.json'
    stdout = directory / f'{name}-{variant["label"]}.stdout'
    output = directory / f'{name}-{variant["label"]}.json'
    cfg = dict(qemu=variant['path'], cpu=CPU, guest=inputs[name]['path'], iterations=37,
               stdout=str(stdout), output=str(output),
               successor_pc=inputs[name]['symbols']['vector_successor'])
    write_json(config_path, cfg)
    env = build.clean_environment()
    env['QEMU_BENCH_GDB_CONFIG'] = str(config_path)
    script = directory / 'gdb_lookup.py'
    command = [gdb, '-q', '-nx', '-batch', '-ex', 'set auto-load off', '-x', str(script)]
    build.verify_qemu(variant)
    with (directory / f'{name}-{variant["label"]}.log').open('w') as stream:
        subprocess.run(command, env=env, stdout=stream, stderr=subprocess.STDOUT,
                       timeout=timeout, check=True)
    build.verify_qemu(variant)
    if stdout.read_bytes() != expected_output(name, 37):
        raise RuntimeError('GDB diagnostic guest output check failed')
    result = json.loads(output.read_text())
    if result['errors'] or result['exit_status'] != [0]:
        raise RuntimeError(f'GDB diagnostic failed: {output}')
    return dict(case=name, variant=variant['label'], **result)


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--case', action='append', choices=list(CASES), help='repeat; default: all eight')
    parser.add_argument('--qemu', action='append', metavar='LABEL=PATH')
    parser.add_argument('--qemu-commit', action='append', default=[], metavar='LABEL=SHA40')
    parser.add_argument('--reference', help='QEMU label used as statistical reference; default: first')
    parser.add_argument('--cross-cc', default='riscv64-linux-gnu-gcc')
    parser.add_argument('--data-dir', type=Path, default=build.DEFAULT_DATA)
    parser.add_argument('--output', type=Path, help='new result directory; never overwritten')
    parser.add_argument('--affinity', help='host CPU list, e.g. 2 or 2,3')
    parser.add_argument('--warmups', type=int, default=1)
    parser.add_argument('--repetitions', type=int, default=9)
    parser.add_argument('--min-seconds', type=float, default=1.0)
    parser.add_argument('--iterations', type=int, help='fixed count for every case and QEMU; skips calibration')
    parser.add_argument('--timeout', type=float, default=1800)
    parser.add_argument('--perf', action='store_true', help='separate untimed hardware-counter runs')
    parser.add_argument('--lookup-counts', action='store_true', help='separate host-GDB counts; debug QEMU, x86-64 host')
    parser.add_argument('--diagnose-only', action='store_true', help='only correctness and host-GDB diagnostics')
    args = parser.parse_args(argv)
    if args.repetitions < 1 or args.warmups < 0 or args.min_seconds <= 0 or args.timeout <= 0:
        parser.error('positive repetitions/min-seconds/timeout and nonnegative warmups required')
    if args.iterations is not None and not 1 <= args.iterations <= MAX_ITERATIONS:
        parser.error('--iterations must be in 1..2^60-1')
    return args


def main(argv=None, *, prepared_args=None):
    args = prepared_args if prepared_args is not None else arguments(argv)
    if args.list:
        for name, case in CASES.items():
            print(f"{name:20} {case['insn']}")
        return 0
    qemus = build.variants(args)
    names = list(dict.fromkeys(args.case or CASES))
    data = args.data_dir.resolve()
    data.mkdir(parents=True, exist_ok=True)
    with (data / '.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(f'Another benchmark is using {data}; choose another --data-dir') from error
        affinity = configuration.apply_affinity(args.affinity)
        output = (args.output or data / 'vsetvli' /
                  datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')).resolve()
        output.mkdir(parents=True, exist_ok=False)
        print('Results:', output, flush=True)
        shutil.copy2(HERE / 'vsetvli.py', output / 'vsetvli.py')
        shutil.copy2(HERE / 'measurement.py', output / 'measurement.py')
        result = dict(schema=1, status='running', cpu=CPU, qemu=qemus, cases=names,
                      arguments={k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                      started_utc=datetime.now(timezone.utc).isoformat(),
                      host_environment_before=build.host_environment(),
                      host=dict(system=platform.system(), release=platform.release(), machine=platform.machine()),
                      scripts_sha256={name: sha256(HERE / name) for name in
                                      ('vsetvli.py', 'run.py', 'toolchain.py', 'measurement.py')},
                      outliers_removed=0, order='AB/BA alternating; rotation for more than two QEMUs',
                      timed_instrumentation='none', runs=[], validation=[], diagnostics=[], perf_runs=[])
        report(result, output)
        configuration.write_manifest(output, args, qemus, ['vsetvli'], affinity,
            runner='vsetvli', host_environment_before=result['host_environment_before'])
        try:
            inputs = build_inputs(names, output, args.cross_cc)
            result['inputs'] = inputs
            logs = output / 'samples'
            logs.mkdir()
            for name in names:
                for variant in qemus:
                    for n in (1, 2, 37, 1000):
                        tag = f'check-{name}-{variant["label"]}-{n}'
                        result['validation'].append(execute(name, variant, inputs, n, logs, tag, args.timeout))
            if not args.diagnose_only:
                for name in names:
                    n = args.iterations or 100000
                    if args.iterations is None:
                        for attempt in range(8):
                            calibration = []
                            for variant in qemus:
                                row = execute(name, variant, inputs, n, logs,
                                              f'calibration-{name}-{attempt}-{variant["label"]}', args.timeout)
                                row.update(phase='calibration', pair=attempt + 1)
                                result['runs'].append(row)
                                calibration.append(row['wall_seconds'])
                            fastest = min(calibration)
                            if fastest >= args.min_seconds:
                                break
                            n *= min(100, max(2, math.ceil(args.min_seconds / fastest * 1.1)))
                            if n > MAX_ITERATIONS:
                                raise RuntimeError('Calibration exceeded iteration limit')
                        else:
                            raise RuntimeError('Calibration did not reach --min-seconds')
                    for warmup in range(args.warmups):
                        for variant in qemus:
                            row = execute(name, variant, inputs, n, logs,
                                          f'warmup-{name}-{warmup}-{variant["label"]}', args.timeout)
                            row.update(phase='warmup', pair=warmup + 1)
                            result['runs'].append(row)
                    for pair in range(args.repetitions):
                        order = qemus[pair % len(qemus):] + qemus[:pair % len(qemus)]
                        for position, variant in enumerate(order, 1):
                            row = execute(name, variant, inputs, n, logs,
                                          f'measured-{name}-{pair}-{variant["label"]}', args.timeout)
                            row.update(phase='measured', pair=pair + 1, position=position)
                            result['runs'].append(row)
                            report(result, output)
                        print(f'{name}: pair {pair + 1}/{args.repetitions}', flush=True)
            # Instrumentation starts only after all uninstrumented timing has finished.
            if args.perf and not args.diagnose_only:
                build.executable('perf')
                for name in names:
                    n = next(r['iterations'] for r in result['runs'] if r['case'] == name and r['phase'] == 'measured')
                    for variant in qemus:
                        result['perf_runs'].append(execute(name, variant, inputs, n, logs,
                            f'perf-{name}-{variant["label"]}', args.timeout, perf=True))
                        report(result, output)
            if args.lookup_counts or args.diagnose_only:
                directory = output / 'diagnostics'
                directory.mkdir()
                shutil.copy2(HERE / 'diagnostics/gdb_lookup.py', directory / 'gdb_lookup.py')
                for name in names:
                    if name == 'scalar-control':
                        continue
                    for variant in qemus:
                        result['diagnostics'].append(lookup_counts(variant, name, inputs, directory, args.timeout))
                        report(result, output)
            result['status'] = 'complete'
        except BaseException as error:
            result.update(status='failed', error=str(error), exception=type(error).__name__)
            raise
        finally:
            result['host_environment_after'] = build.host_environment()
            result['finished_utc'] = datetime.now(timezone.utc).isoformat()
            report(result, output)
        print((output / 'summary.md').read_text(), flush=True)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'vsetvli benchmark: {error}', file=sys.stderr)
        sys.exit(1)
