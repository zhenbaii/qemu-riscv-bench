#!/usr/bin/env python3
"""Local vsetivli mechanism benchmark and a libc-free scalar noise control."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import resource
import shutil
import statistics
import struct
import subprocess
import sys
import time

import run as build
import configuration
from toolchain import sha256, write_json

DEFINITION = configuration.micro_definition('vsetivli')
CASES = tuple(DEFINITION['cases'])
MAX_ITERATIONS = (1 << 63) - 1


def expected_output(case: str, iterations: int) -> bytes:
    value = iterations & 0xffffffff
    if value & 0x80000000:
        value |= 0xffffffff00000000
    return struct.pack('<QQQ', value, 8 if case == CASES[0] else 0,
                       0xc8 if case == CASES[0] else 0)


def execute(case, binary, variant, cpu, iterations, directory, timeout, extra=()):
    build.verify_qemu(variant)
    command = [variant['path'], '-cpu', cpu, *extra, str(binary), str(iterations)]
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started_utc = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    with (directory / 'stderr.log').open('ab') as log:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log,
            env=build.clean_environment(), start_new_session=True)
        try:
            output, _ = process.communicate(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            import signal
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
    finished = time.perf_counter()
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    build.verify_qemu(variant)
    if process.returncode or output != expected_output(case, iterations):
        raise RuntimeError(f'{case}/{variant["label"]}: incorrect output or exit '
                           f'{process.returncode}; see {directory / "stderr.log"}')
    return {'case': case, 'variant': variant['label'], 'iterations': iterations,
            'wall_seconds': finished - started,
            'user_seconds': after.ru_utime - before.ru_utime,
            'sys_seconds': after.ru_stime - before.ru_stime,
            'started_utc': started_utc, 'started_monotonic_seconds': started,
            'finished_monotonic_seconds': finished, 'command': command,
            'host_affinity': sorted(os.sched_getaffinity(0)), 'output_hex': output.hex()}


def parse_chain_edges(text: str, symbols: dict[str, int]) -> list[dict]:
    # Resolve host TB pointers only after collecting the entire short trace:
    # the destination's Trace line can follow the corresponding Linking line.
    traces = dict((host.lower(), int(pc, 16)) for host, pc in re.findall(
        r'Trace \d+: (0x[0-9a-fA-F]+) \[[^/\]\n]+/([0-9a-fA-F]+)/', text))
    wanted = {(symbols['benchmark_loop'], symbols['vector_successor_first']),
              (symbols['vector_successor_first'], symbols['vector_successor_second'])}
    found = []
    for source, slot, destination in re.findall(
            r'Linking TBs (0x[0-9a-fA-F]+) index (\d+) -> (0x[0-9a-fA-F]+)', text):
        edge = (traces.get(source.lower()), traces.get(destination.lower()))
        if edge in wanted:
            found.append({'source_pc': edge[0], 'destination_pc': edge[1], 'slot': int(slot)})
    return found


def validate_counts(case, iterations, counts):
    expected = {'vsetivli': 2 * iterations if case == CASES[0] else 0,
                'vsetvli': 0, 'vsetvl': 0}
    if counts != expected:
        raise RuntimeError(f'{case}: vector configuration coverage {counts} != {expected}')


def summarize(rows, qemus):
    result = []
    for case in CASES:
        reference = None
        for variant in qemus:
            values = [r['wall_seconds'] for r in rows if r['case'] == case
                      and r['variant'] == variant['label'] and r['phase'] == 'measured']
            if not values:
                continue
            median = statistics.median(values)
            if reference is None:
                reference = median
            result.append({'case': case, 'variant': variant['label'], 'runs': len(values),
                'median_seconds': median, 'min_seconds': min(values), 'max_seconds': max(values),
                'stddev_seconds': statistics.stdev(values) if len(values) > 1 else None,
                'wall_ratio': median / reference})
    return result


def run_suite(qemus, output: Path, cross_cc: str, cpu: str, warmups=1, repetitions=3,
              timeout=1800, min_seconds=2.0, iterations=None, profile_qemu=None,
              plugin_include=None, source_dir=None, manifest_args=None, affinity_record=None):
    if iterations is not None and not 1 <= iterations <= MAX_ITERATIONS:
        raise ValueError('iterations must be in 1..2^63-1')
    if min_seconds <= 0 or repetitions < 1 or warmups < 0:
        raise ValueError('positive min_seconds/repetitions and nonnegative warmups required')
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest_args = manifest_args if manifest_args is not None else argparse.Namespace(
        cpu=cpu, warmups=warmups, repetitions=repetitions, timeout=timeout,
        min_seconds=min_seconds, iterations=iterations, cross_cc=cross_cc)
    mask = sorted(os.sched_getaffinity(0))
    affinity_record = affinity_record or dict(requested=None, inherited=mask, effective=mask)
    configuration.write_manifest(output, manifest_args, qemus, ['vsetivli'], affinity_record,
        runner='vsetivli', host_environment_before=build.host_environment())
    source_dir = Path(source_dir) if source_dir else Path(__file__).resolve().parent
    if sha256(source_dir / DEFINITION['source']['path']) != DEFINITION['source']['sha256']:
        raise RuntimeError('vsetivli source checksum differs from its YAML definition')
    shutil.copy2(source_dir / 'microbench.S', output / 'microbench.S')
    shutil.copy2(source_dir / 'microbench.py', output / 'microbench.py')
    cross_cc = build.executable(cross_cc)
    machine = build.command_output([cross_cc, '-dumpmachine'])
    if machine != 'riscv64-linux-gnu':
        raise RuntimeError(f'This Linux syscall benchmark needs riscv64-linux-gnu, got {machine}')
    objdump, nm = build.executable(machine + '-objdump'), build.executable(machine + '-nm')
    metadata = {'scope': 'whole libc-free guest executable under QEMU; not a package build',
        'started_utc': datetime.now(timezone.utc).isoformat(), 'status': 'running',
        'qemu': qemus, 'cpu': cpu, 'host_affinity': sorted(os.sched_getaffinity(0)),
        'warmups': warmups, 'repetitions': repetitions, 'order': 'AB/BA alternating',
        'min_seconds': min_seconds, 'fixed_iterations': iterations,
        'source_sha256': sha256(output / 'microbench.S'),
        'driver_sha256': sha256(output / 'microbench.py'),
        'host_environment_before': build.host_environment(),
        'cross_cc': cross_cc, 'cross_cc_sha256': sha256(Path(cross_cc)),
        'calibration': 'same frozen iteration count for all QEMUs within each case; calibrations excluded',
        'case_iterations': {}, 'binaries': {}, 'trace_diagnostics': {}, 'coverage': {}}
    rows = []
    write_json(output / 'metadata.json', metadata)
    try:
        for case in CASES:
            vector = case == CASES[0]
            binary = output / (case + '.elf')
            command = [cross_cc, '-nostdlib', '-nostartfiles', '-static', '-fno-pie', '-no-pie',
                '-Wl,--build-id=none', '-Wl,-e,_start', '-mabi=lp64d',
                '-march=rv64gcv' if vector else '-march=rv64gc',
                f'-DVECTOR_CASE={int(vector)}', str(output / 'microbench.S'), '-o', str(binary)]
            build.invoke(command, output, build.clean_environment(), output / 'build.log', timeout)
            disassembly = build.command_output([objdump, '-d', str(binary)])
            (output / (case + '.disassembly')).write_text(disassembly + '\n')
            # No libc or CRT is linked. The control's entire executable must
            # contain no vector instruction, including setup and teardown.
            vector_mnemonics = re.findall(r'^\s*[0-9a-f]+:\s+[0-9a-f]+\s+(v\S+)', disassembly, re.M)
            if (not vector and vector_mnemonics) or (vector and vector_mnemonics.count('vsetivli') != 2):
                raise RuntimeError(f'Unexpected instruction coverage in {case}: {vector_mnemonics}')
            metadata['binaries'][case] = {'sha256': sha256(binary), 'build_command': command,
                                          'vector_mnemonics': vector_mnemonics}
            if vector:
                symbols = {parts[2]: int(parts[0], 16) for line in build.command_output([nm, str(binary)]).splitlines()
                           if len(parts := line.split()) == 3}
                pcs = [symbols[k] for k in ('benchmark_loop', 'vector_successor_first', 'vector_successor_second')]
                if len({pc >> 12 for pc in pcs}) != 1:
                    raise RuntimeError('Hot-loop successors are not within one guest page')
                for variant in qemus:
                    log = output / (variant['label'] + '-chain.log')
                    execute(case, binary, variant, cpu, 37, output, timeout,
                            ['-d', 'exec', '-D', str(log)])
                    metadata['trace_diagnostics'][variant['label']] = {
                        'untimed_iterations': 37, 'vector_successor_chain_edges': parse_chain_edges(log.read_text(), symbols)}
            count = iterations or 10000
            if iterations is None:
                for attempt in range(12):
                    calibration = [execute(case, binary, q, cpu, count, output, timeout) for q in qemus]
                    for row in calibration:
                        row.update(phase='calibration', iteration=attempt + 1)
                        rows.append(row)
                    fastest = min(r['wall_seconds'] for r in calibration)
                    if fastest >= min_seconds:
                        break
                    factor = min(100, max(2, math.ceil(min_seconds / fastest * 1.1)))
                    if count > MAX_ITERATIONS // factor:
                        raise RuntimeError('Cannot reach the minimum duration within the iteration limit')
                    count *= factor
                else:
                    raise RuntimeError('Calibration did not reach the minimum duration')
            metadata['case_iterations'][case] = count
            for phase, rounds in [('warmup', warmups), ('measured', repetitions)]:
                for iteration in range(rounds):
                    order = qemus[iteration % len(qemus):] + qemus[:iteration % len(qemus)]
                    for variant in order:
                        row = execute(case, binary, variant, cpu, count, output, timeout)
                        row.update(phase=phase, iteration=iteration + 1)
                        rows.append(row)
                        write_json(output / 'results.json', {'status': 'running', 'runs': rows})
                        print(f"{case}/{phase}-{iteration+1}/{variant['label']}: {row['wall_seconds']:.3f}s", flush=True)
        if profile_qemu:
            if not plugin_include or not (Path(plugin_include) / 'qemu-plugin.h').is_file():
                raise RuntimeError('--profile-qemu requires --plugin-include DIR containing the matching qemu-plugin.h')
            include = plugin_include
            plugin_source = source_dir / 'vset_count.c'
            shutil.copy2(plugin_source, output / 'vset_count.c')
            plugin = output / 'vset-count.so'
            import shlex
            glib = shlex.split(build.command_output(['pkg-config', '--cflags', '--libs', 'glib-2.0']))
            build.invoke([build.executable('cc'), '-shared', '-fPIC', '-O2', '-std=c11',
                '-I', str(include), str(output / 'vset_count.c'), '-o', str(plugin), *glib],
                output, build.clean_environment(), output / 'build.log', timeout)
            for case in CASES:
                destination = output / (case + '-coverage')
                destination.mkdir()
                execute(case, output / (case + '.elf'), profile_qemu, cpu, 37, output, timeout,
                        ['-plugin', f'{plugin},out={destination}'])
                files = list(destination.glob('*.json'))
                if len(files) != 1:
                    raise RuntimeError(f'Expected one coverage result for {case}')
                counts = json.loads(files[0].read_text())
                validate_counts(case, 37, counts)
                metadata['coverage'][case] = counts
            metadata['coverage_qemu'] = profile_qemu
            metadata['coverage_plugin_source_sha256'] = sha256(output / 'vset_count.c')
        metadata['status'] = 'complete'
    except BaseException as error:
        metadata.update(status='failed', error=str(error))
        raise
    finally:
        metadata['finished_utc'] = datetime.now(timezone.utc).isoformat()
        metadata['host_environment_after'] = build.host_environment()
        measured = [r for r in rows if r['phase'] == 'measured']
        metadata['all_measured_samples_reach_duration_floor'] = bool(measured) and all(
            r['wall_seconds'] >= min_seconds for r in measured)
        write_json(output / 'metadata.json', metadata)
        statistics_rows = summarize(rows, qemus)
        write_json(output / 'results.json', {'status': metadata['status'], 'statistics': statistics_rows, 'runs': rows})
        with (output / 'results.csv').open('w', newline='') as stream:
            writer = csv.DictWriter(stream, ['case', 'variant', 'iteration', 'iterations', 'wall_seconds', 'user_seconds', 'sys_seconds'], extrasaction='ignore')
            writer.writeheader()
            writer.writerows(r for r in rows if r['phase'] == 'measured')
        lines = ['# Mechanism and scalar control', '', f"Status: {metadata['status']}", '',
            'Scope: libc-free guest loop, including QEMU startup; distinct from package build timings.',
            'Calibration, warmups, chain traces and optional instruction counting are excluded.',
            'The synthetic workload measures the selected guest instruction path.', '',
            '| Case | QEMU | Runs | Median wall (s) | Min–max (s) | Wall / baseline |',
            '| --- | --- | ---: | ---: | ---: | ---: |']
        if not metadata['all_measured_samples_reach_duration_floor']:
            lines[4:4] = ['Some samples are below the requested duration floor; short fixed-count runs are functional checks, not reliable performance evidence.', '']
        for s in statistics_rows:
            ratio = (f"{s['wall_ratio']:.3f}x" if metadata['all_measured_samples_reach_duration_floor']
                     else 'n/a (short run)')
            lines.append(f"| {s['case']} | {s['variant']} | {s['runs']} | {s['median_seconds']:.3f} | "
                         f"{s['min_seconds']:.3f}–{s['max_seconds']:.3f} | {ratio} |")
        lines += ['', 'Untimed observed vsetivli successor chain edges:', '',
                  json.dumps(metadata['trace_diagnostics'], indent=2), '']
        (output / 'summary.md').write_text('\n'.join(lines))
    return output


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', action='append', metavar='LABEL=PATH')
    parser.add_argument('--qemu-commit', action='append', default=[])
    parser.add_argument('--reference', help='QEMU label used as statistical reference; default: first')
    parser.add_argument('--cross-cc', default='riscv64-linux-gnu-gcc')
    parser.add_argument('--cpu', default=DEFINITION['cpu'])
    parser.add_argument('--affinity', help='host CPU list, inherited by every QEMU')
    parser.add_argument('--warmups', type=int, default=1)
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--min-seconds', type=float, default=2.0)
    parser.add_argument('--iterations', type=int, help='fixed count for both cases; skips calibration')
    parser.add_argument('--timeout', type=float, default=1800)
    parser.add_argument('--data-dir', type=Path, default=build.DEFAULT_DATA)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--profile-qemu', type=Path, help='optional separate untimed instruction-count verification')
    parser.add_argument('--plugin-include', type=Path, help='directory containing matching qemu-plugin.h')
    args = parser.parse_args(argv)
    if args.warmups < 0 or args.repetitions < 1 or args.min_seconds <= 0 or args.timeout <= 0:
        parser.error('positive repetitions/min-seconds/timeout and nonnegative warmups required')
    if args.iterations is not None and not 1 <= args.iterations <= MAX_ITERATIONS:
        parser.error('--iterations must be in 1..2^63-1')
    return args


def main(argv=None, *, prepared_args=None):
    args = prepared_args if prepared_args is not None else arguments(argv)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    with (args.data_dir / '.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f'Another benchmark is using {args.data_dir}')
        qemus = build.variants(args)
        profile = build.qemu_info('coverage', args.profile_qemu) if args.profile_qemu else None
        if profile and '-plugin' not in build.command_output([profile['path'], '-help']):
            raise RuntimeError('--profile-qemu must support -plugin')
        affinity = configuration.apply_affinity(args.affinity)
        output = args.output or args.data_dir / 'micro-runs' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        run_suite(qemus, output, args.cross_cc, args.cpu, args.warmups, args.repetitions,
                  args.timeout, args.min_seconds, args.iterations, profile, args.plugin_include,
                  manifest_args=args, affinity_record=affinity)
        print((output / 'summary.md').read_text())
        print('Saved:', output)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'microbench: {error}', file=sys.stderr)
        sys.exit(1)
