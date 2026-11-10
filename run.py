#!/usr/bin/env python3
"""Reproducible upstream package builds with the C compiler executed by QEMU."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import resource
import shlex
import shutil
import statistics
import struct
import subprocess
import sys
import tarfile
import time

from toolchain import download, prepare as prepare_toolchain, sha256, write_json
import configuration

HERE = Path(__file__).resolve().parent
REGISTRY = configuration.package_registry()
DEFAULT_DATA = HERE / 'data'


def command_output(argv: list[str]) -> str:
    return subprocess.check_output(argv, text=True, stderr=subprocess.STDOUT).strip()


def print_summary(path: Path) -> None:
    summary = path.read_text()
    print(summary, flush=True)
    combined = os.environ.get('QEMU_BENCH_COMBINED_SUMMARY')
    if combined:
        with Path(combined).open('a') as stream:
            stream.write(summary + '\n')


def executable(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f'Missing executable: {name}')
    return str(Path(path).resolve())


def check_riscv(path: Path) -> None:
    with path.open('rb') as stream:
        header = stream.read(20)
    if len(header) < 20 or header[:5] != b'\x7fELF\x02' or header[5] != 1 or struct.unpack('<H', header[18:20])[0] != 243:
        raise RuntimeError(f'Expected a little-endian ELF64 RISC-V executable: {path}')


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        if ('.git' in path.relative_to(root).parts or '__pycache__' in path.parts
                or path.name == '.benchmark-source.json'):
            continue
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            digest.update(f'L\0{relative}\0{os.readlink(path)}\n'.encode())
        elif path.is_file():
            digest.update(f'F\0{relative}\0{sha256(path)}\n'.encode())
    return digest.hexdigest()


def source_tree(package: str, args, cache: Path) -> tuple[Path, dict]:
    spec = REGISTRY[package]
    if args.source_dir:
        root = args.source_dir.resolve()
        if not root.is_dir():
            raise RuntimeError(f'Local source directory does not exist: {root}')
        return root, {'kind': 'local', 'path': str(root), 'tree_sha256': tree_digest(root)}
    archive = cache / 'downloads' / f"{package}-{spec['version']}.tar.gz"
    fetched_url = None
    if archive.is_file() or args.offline:
        # A corrupt existing cache is an error, even if mirrors are available.
        download(spec['url'], archive, spec['sha256'], args.offline)
    else:
        failures = []
        for url in [spec['url'], *spec.get('mirrors', [])]:
            try:
                download(url, archive, spec['sha256'], False)
                fetched_url = url
                break
            except (OSError, RuntimeError) as error:
                failures.append(str(error))
                print(f'Source download failed: {error}', flush=True)
        else:
            raise RuntimeError('All checked source URLs failed: ' + '; '.join(failures))
    root = cache / 'sources' / f"{package}-{spec['version']}"
    marker = root / '.benchmark-source.json'
    if not marker.exists() or 'tree_sha256' not in json.loads(marker.read_text()):
        temporary = root.with_name(root.name + '.partial')
        if temporary.exists():
            shutil.rmtree(temporary)
        temporary.mkdir(parents=True)
        try:
            with tarfile.open(archive) as stream:
                # Python's data filter rejects escaping paths, special files,
                # and links outside the extraction directory.
                stream.extractall(temporary, filter='data')
            entries = list(temporary.iterdir())
            if len(entries) != 1 or not entries[0].is_dir() or entries[0].is_symlink():
                raise RuntimeError('Source archive must contain exactly one top-level directory')
            if root.exists():
                shutil.rmtree(root)
            entries[0].rename(root)
            write_json(marker, {'url': spec['url'], 'download_url': fetched_url,
                                'sha256': spec['sha256'], 'tree_sha256': tree_digest(root)})
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
    if json.loads(marker.read_text()).get('sha256') != spec['sha256']:
        raise RuntimeError(f'Source cache metadata mismatch: {root}')
    if json.loads(marker.read_text())['tree_sha256'] != tree_digest(root):
        raise RuntimeError(f'Cached source tree was modified: {root}; remove it to re-extract the verified archive')
    return root, {'kind': 'upstream', 'version': spec['version'], 'url': spec['url'],
                  'download_url': json.loads(marker.read_text()).get('download_url'),
                  'archive_sha256': spec['sha256'], 'tree_sha256': tree_digest(root)}


def qemu_info(label: str, path: Path) -> dict:
    path = path.expanduser().resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError(f'QEMU is not executable: {path}')
    digest = sha256(path)
    info = {'label': label, 'path': str(path), 'sha256': digest,
            'version': command_output([str(path), '--version']), 'commit': None}
    metadata = path.parent / 'metadata.json'
    if metadata.exists():
        saved = json.loads(metadata.read_text())
        if saved.get('sha256') != digest:
            raise RuntimeError(f'QEMU binary does not match its saved metadata: {path}')
        info['commit'] = saved.get('commit')
        info['build_metadata'] = saved
    return info


def variants(args) -> list[dict]:
    values = args.qemu
    if not values:
        raise RuntimeError('Pass --qemu LABEL=/absolute/path/to/qemu-riscv64 (repeat for A/B)')
    found = []
    for value in values:
        label, sep, filename = value.partition('=')
        if not sep:
            label, filename = ('qemu' if len(values) == 1 else f'qemu{len(found)+1}'), value
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', label) or any(x['label'] == label for x in found):
            raise RuntimeError('QEMU labels must be unique and contain only letters, digits, _ or -')
        found.append(qemu_info(label, Path(filename)))
    commits = dict(value.split('=', 1) for value in args.qemu_commit)
    unknown = set(commits) - {x['label'] for x in found}
    if unknown:
        raise RuntimeError(f'--qemu-commit refers to unknown labels: {sorted(unknown)}')
    for item in found:
        if item['label'] in commits:
            commit = commits[item['label']]
            if not re.fullmatch(r'[0-9a-fA-F]{40}', commit):
                raise RuntimeError('--qemu-commit requires a full 40-character Git commit')
            if item['commit'] and item['commit'] != commit:
                raise RuntimeError(f"Commit conflicts with saved metadata: {item['label']}")
            item['commit'] = commit
    reference = getattr(args, 'reference', None)
    if reference:
        if reference not in {item['label'] for item in found}:
            raise ValueError(f'Unknown reference QEMU label: {reference}')
        found.sort(key=lambda item: item['label'] != reference)
    return found


def clean_environment() -> dict[str, str]:
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith(('QEMU_', 'CCACHE_', 'DISTCC_')) or key in {
            'CC', 'CXX', 'CPP', 'CFLAGS', 'CXXFLAGS', 'CPPFLAGS', 'LDFLAGS', 'LIBRARY_PATH',
            'CPATH', 'C_INCLUDE_PATH', 'CPLUS_INCLUDE_PATH', 'COMPILER_PATH', 'GCC_EXEC_PREFIX',
            'MAKEFLAGS', 'MFLAGS', 'LD_PRELOAD', 'LD_LIBRARY_PATH', 'CONFIG_SITE'}:
            del env[key]
    env.update({'LC_ALL': 'C', 'TZ': 'UTC', 'SOURCE_DATE_EPOCH': '1704067200', 'CONFIG_SITE': '/dev/null'})
    return env


def host_environment() -> dict:
    """Record relevant read-only host settings without changing system policy."""
    def read(path):
        try:
            return Path(path).read_text().strip()
        except OSError:
            return None
    cpus = {}
    for cpu in sorted(os.sched_getaffinity(0)):
        root = Path(f'/sys/devices/system/cpu/cpu{cpu}')
        cpus[str(cpu)] = {name: read(root / name) for name in (
            'topology/core_id', 'topology/thread_siblings_list', 'cpufreq/scaling_driver',
            'cpufreq/scaling_governor', 'cpufreq/scaling_min_freq', 'cpufreq/scaling_max_freq',
            'cpufreq/scaling_cur_freq', 'cpufreq/energy_performance_preference')}
    return {'utc': datetime.now(timezone.utc).isoformat(), 'cpus': cpus,
            'boost': read('/sys/devices/system/cpu/cpufreq/boost'),
            'smt_active': read('/sys/devices/system/cpu/smt/active'),
            'loadavg': read('/proc/loadavg'), 'memory_pressure': read('/proc/pressure/memory'),
            'ac_online': {str(p): read(p) for p in Path('/sys/class/power_supply').glob('*/online')}}


def invoke(argv: list[str], cwd: Path, env: dict, log: Path, timeout: float, *,
           stdout: Path | None = None) -> dict:
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started_utc = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    with log.open('ab') as stream:
        stream.write(('$ ' + shlex.join(argv) + '\n').encode())
        stream.flush()
        process = subprocess.Popen(argv, cwd=cwd, env=env,
                                   stdout=subprocess.PIPE if stdout else stream,
                                   stderr=stream if stdout else subprocess.STDOUT,
                                   start_new_session=True)
        try:
            if stdout is None:
                code = process.wait(timeout=timeout)
            else:
                # Waiting on the output pipe avoids polling delays in short guest runs.
                captured, _ = process.communicate(timeout=timeout)
                code = process.returncode
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            import signal
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    finished = time.perf_counter()
    if stdout is not None:
        stdout.write_bytes(captured)
    result = {'wall_seconds': finished - started,
              'started_utc': started_utc, 'started_monotonic_seconds': started,
              'finished_monotonic_seconds': finished,
              'user_seconds': after.ru_utime - before.ru_utime,
              'sys_seconds': after.ru_stime - before.ru_stime, 'returncode': code}
    if code:
        tail = log.read_text(errors='replace')[-6000:]
        raise RuntimeError(f'Command failed ({code}); log: {log}\n{tail}')
    return result


def make_launcher(config: dict, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    config_path = directory / 'compiler.json'
    write_json(config_path, config)
    launcher = directory / 'cc'
    # /bin/sh is used only to quote paths and exec host Python. No guest exec chain.
    argv = [sys.executable, config.get('dispatcher', str(HERE / 'compiler.py')), 'cc', str(config_path)]
    launcher.write_text('#!/bin/sh\nexec ' + shlex.join(argv) + ' "$@"\n')
    launcher.chmod(0o755)
    if ',' in str(config_path) or ',' in str(HERE) or ',' in sys.executable:
        raise RuntimeError('GCC -wrapper does not support commas in launcher paths')
    return launcher


def build_commands(package: str, args, cc: Path, tools: dict) -> tuple[list[list[str]], list[str]]:
    jobs = f'-j{args.jobs}'
    if REGISTRY[package]['recipe'] == 'lz4':
        return [], ['make', jobs, 'lib', 'lz4', 'BUILD_SHARED=no', 'BUILD_STATIC=yes']
    if REGISTRY[package]['recipe'] == 'zlib':
        return [['./configure', '--static']], ['make', jobs]
    if REGISTRY[package]['recipe'] == 'sqlite':
        return [], ['make', '-f', str(HERE / 'sqlite-benchmark.mk'), jobs]
    if REGISTRY[package]['recipe'] != 'zlib-ng':
        raise RuntimeError(f"Unsupported build recipe: {REGISTRY[package]['recipe']}")
    return [['cmake', '-S', '.', '-B', 'build', '-DCMAKE_SYSTEM_NAME=Linux',
             '-DCMAKE_SYSTEM_PROCESSOR=riscv64', f'-DCMAKE_C_COMPILER={cc}',
             f"-DCMAKE_AR={tools['AR']}", f"-DCMAKE_RANLIB={tools['RANLIB']}",
             '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_C_FLAGS_RELEASE=-DNDEBUG',
             '-DBUILD_SHARED_LIBS=OFF', '-DZLIB_COMPAT=OFF',
             '-DZLIB_ENABLE_TESTS=OFF']], ['cmake', '--build', 'build', '--parallel', str(args.jobs)]


def load_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def verify_qemu(variant: dict) -> None:
    if sha256(Path(variant['path'])) != variant['sha256']:
        raise RuntimeError(f"QEMU binary changed during this experiment: {variant['path']}")


def copy_source(source: Path, work: Path, cmake_build: bool) -> None:
    if source == work or source.is_relative_to(work) or work.is_relative_to(source):
        raise RuntimeError(f'Source and temporary build directory overlap: {source}, {work}')
    if work.exists():
        marker = work / '.benchmark-work.json'
        if not marker.is_file() or marker.read_text() != 'standalone-qemu-build-benchmark-v1\n':
            raise RuntimeError(f'Refusing to remove an unmarked working directory: {work}')
        shutil.rmtree(work)
    shutil.copytree(source, work, symlinks=True, ignore=shutil.ignore_patterns('.git', '.benchmark-source.json'))
    (work / '.benchmark-work.json').write_text('standalone-qemu-build-benchmark-v1\n')
    if cmake_build:
        old_build = work / 'build'
        if old_build.is_symlink():
            old_build.unlink()
        elif old_build.is_dir():
            shutil.rmtree(old_build)


def prepare_build(package, args, cc, tools, work, env, log):
    configure, build = build_commands(package, args, cc, tools)
    for command in configure:
        invoke(command, work, env, log, args.timeout)
    if args.source_dir:
        recipe = REGISTRY[package]['recipe']
        # zlib's release Makefile is a placeholder without a clean target.
        # Configure first, then clean the copy with its actual build recipe.
        if recipe in ('zlib', 'lz4'):
            invoke(['make', 'clean'], work, env, log, args.timeout)
        elif recipe == 'sqlite':
            invoke(['make', '-f', str(HERE / 'sqlite-benchmark.mk'), 'clean'],
                   work, env, log, args.timeout)
        # CMake builds use a newly created build/ directory in copy_source().
    return build


def build_once(package: str, source: Path, variant: dict, tc: dict, cross: dict,
               args, session: Path, iteration: int, warmup: bool, plugin: Path | None = None) -> dict:
    label = variant['label']
    name = f"{package}-{'coverage' if plugin else 'warmup' if warmup else 'run'}-{iteration:02d}-{label}"
    out = session / name
    out.mkdir()
    work = args.data_dir.resolve() / 'work' / package
    copy_source(source, work, REGISTRY[package]['recipe'] == 'zlib-ng')
    verify_qemu(variant)
    events = out / 'compiler-events.jsonl'
    coverage_dir = out / 'coverage'
    coverage_dir.mkdir()
    config = {'cross_cc': cross['path'], 'guest_cc1': tc['cc1'], 'guest_sysroot': tc['sysroot'],
              'dispatcher': str(session / 'compiler.py'),
              'builtin_includes': cross['builtin_includes'],
              'default_pic_flags': cross['default_pic_flags'],
              'qemu': variant['path'], 'cpu': args.cpu, 'events': str(events),
              'plugin': None, 'coverage_dir': str(coverage_dir)}
    cc = make_launcher(config, out / 'launcher')
    env = clean_environment()
    flags = f'-O2 -g0 -march={args.march} -mabi=lp64d -frandom-seed=qemu-build-benchmark'
    flags += f' -ffile-prefix-map={shlex.quote(str(work))}=/benchmark/source'
    flags += ' ' + args.cflags
    env.update(cross['tools'])
    env.update({'CC': shlex.quote(str(cc)), 'CPP': shlex.quote(str(cc)) + ' -E', 'CFLAGS': flags})
    log = out / 'build.log'
    print(f'{name}: preparing fresh build; log: {log}', flush=True)
    build = prepare_build(package, args, cc, cross['tools'], work, env, log)
    configured = load_events(events)
    if plugin:
        config['plugin'] = str(plugin)
        write_json(cc.parent / 'compiler.json', config)
    build_argv = build
    if args.perf and not plugin:
        build_argv = ['perf', 'stat', '-x,', '-e', 'cycles:u,instructions:u', '-o', str(out / 'perf.csv'), '--', *build]
    result = invoke(build_argv, work, env, log, args.timeout)
    verify_qemu(variant)
    if args.perf and not plugin:
        counts = {}
        for line in (out / 'perf.csv').read_text().splitlines():
            columns = line.split(',')
            if len(columns) > 2 and columns[2].strip() in {'cycles:u', 'instructions:u'}:
                counts[columns[2].strip()] = float(columns[0])
        if set(counts) != {'cycles:u', 'instructions:u'}:
            raise RuntimeError(f'perf did not produce both requested counters; see {out / "perf.csv"}')
        result['cycles'] = counts['cycles:u']
        result['instructions'] = counts['instructions:u']
    compiled = load_events(events)[len(configured):]
    if plugin:
        config['plugin'] = None
        write_json(cc.parent / 'compiler.json', config)
    guest = [e for e in compiled if e.get('emulated')]
    if not guest or any(e['returncode'] for e in guest):
        raise RuntimeError(f'Build did not complete successful emulated cc1 invocations; see {events}')
    artifacts = {}
    artifacts_dir = out / 'artifacts'
    for relative in REGISTRY[package]['artifacts']:
        path = work / relative
        if not path.is_file():
            raise RuntimeError(f'Expected build artifact missing: {path}')
        artifacts[relative] = {'sha256': sha256(path), 'bytes': path.stat().st_size}
        target = artifacts_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    # Execute one freshly built guest program explicitly, outside the timed region.
    if REGISTRY[package]['validation'] == 'lz4-roundtrip':
        invoke([variant['path'], '-cpu', args.cpu, '-L', cross['runtime'],
                str(work / 'programs/lz4'), '--version'], work, clean_environment(), out / 'check.log', args.timeout)
        sample = out / 'sample.txt'
        sample.write_bytes(b'QEMU upstream package build validation\n' * 1024)
        compressed, restored = out / 'sample.lz4', out / 'restored.txt'
        prefix = [variant['path'], '-cpu', args.cpu, '-L', cross['runtime'], str(work / 'programs/lz4')]
        invoke([*prefix, '-q', '-f', str(sample), str(compressed)], work, clean_environment(), out / 'check.log', args.timeout)
        invoke([*prefix, '-q', '-d', '-f', str(compressed), str(restored)], work, clean_environment(), out / 'check.log', args.timeout)
        if sha256(sample) != sha256(restored):
            raise RuntimeError('Built LZ4 failed a compression/decompression round trip')
    elif REGISTRY[package]['validation'] == 'zlib-example':
        invoke([variant['path'], '-cpu', args.cpu, '-L', cross['runtime'],
                str(work / 'example')], work, clean_environment(), out / 'check.log', args.timeout)
    elif REGISTRY[package]['validation'] == 'sqlite-integrity':
        database = out / 'check.db'
        sql = ("CREATE TABLE t(id INTEGER PRIMARY KEY, v INTEGER);"
               "BEGIN; WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL "
               "SELECT x+1 FROM n WHERE x<10000) "
               "INSERT INTO t SELECT x,x*x FROM n; COMMIT;"
               "CREATE INDEX t_v ON t(v);"
               "SELECT 'BENCH:rows='||count(*)||';sum='||sum(id)||"
               "';squares='||sum(v) FROM t;"
               "SELECT 'BENCH:range='||count(*) FROM t WHERE v BETWEEN 100 AND 10000;"
               "SELECT 'BENCH:json='||json_extract('{\"x\":[2,3,5]}','$.x[2]');"
               "PRAGMA integrity_check;")
        (out / 'check.sql').write_text(sql + '\n')
        prefix = [variant['path'], '-cpu', args.cpu, '-L', cross['runtime'],
                  str(work / 'sqlite3'), '-batch', '-noheader', str(database)]
        invoke([*prefix, sql], work, clean_environment(), out / 'check.log', args.timeout)
        invoke([*prefix, "SELECT 'BENCH:reopen='||count(*) FROM t; PRAGMA integrity_check;"],
               work, clean_environment(), out / 'check.log', args.timeout)
        output = (out / 'check.log').read_text().splitlines()
        expected = ['BENCH:rows=10000;sum=50005000;squares=333383335000',
                    'BENCH:range=91', 'BENCH:json=5', 'BENCH:reopen=10000']
        if [line for line in output if line.startswith('BENCH:')] != expected or output.count('ok') != 2:
            raise RuntimeError('Built SQLite failed query, persistence or integrity checks')
    elif REGISTRY[package]['validation'] == 'zlib-ng-roundtrip':
        check = out / 'check.c'
        check.write_text('#include "zlib-ng.h"\n#include <string.h>\n'
                         'int main(void) { const unsigned char s[]="qemu-benchmark"; '
                         'unsigned char c[128], d[128]; size_t n=sizeof(c), m=sizeof(d); '
                         'return zng_compress(c,&n,s,sizeof(s)) || zng_uncompress(d,&m,c,n) '
                         '|| m!=sizeof(s) || memcmp(s,d,m); }\n')
        target = out / 'check.elf'
        invoke([str(cc), '-I', str(work), '-I', str(work / 'build'), str(check),
                str(work / 'build/libz-ng.a'), '-o', str(target)], work, env, out / 'check.log', args.timeout)
        invoke([variant['path'], '-cpu', args.cpu, '-L', cross['runtime'], str(target)],
               work, clean_environment(), out / 'check.log', args.timeout)
    result.update({'package': package, 'variant': label, 'iteration': iteration, 'warmup': warmup,
                   'coverage_run': bool(plugin), 'directory': str(out), 'cc1_invocations': len(guest),
                   'cc1_wall_sum': sum(e['wall_seconds'] for e in guest), 'artifacts': artifacts,
                   'configure_timed': False, 'build_argv': build_argv, 'cflags': flags})
    if plugin:
        counts = {'vsetivli': 0, 'vsetvli': 0, 'vsetvl': 0}
        files = list(coverage_dir.glob('*.json'))
        if not files:
            raise RuntimeError('Coverage plugin produced no counts')
        for path in files:
            for key, value in json.loads(path.read_text()).items():
                counts[key] += value
        result['vset_counts'] = counts
    write_json(out / 'result.json', result)
    print(f"{name}: {result['wall_seconds']:.3f}s, {len(guest)} emulated cc1 calls", flush=True)
    return result


def report(rows: list[dict], session: Path, status: str) -> None:
    measured = [r for r in rows if not r['warmup'] and not r['coverage_run']]
    fields = ['package', 'variant', 'iteration', 'wall_seconds', 'user_seconds', 'sys_seconds', 'cycles', 'instructions',
              'cc1_invocations', 'cc1_wall_sum']
    with (session / 'results.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(measured)
    lines = ['# Package build benchmark', '', f'Status: {status}', '',
             '## Build time', '',
             '| Package | QEMU | Runs | Median wall (s) | Min–max (s) | Stddev (s) | Wall change | Wall / baseline |',
             '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    stats = []
    consistent = True
    for package in dict.fromkeys(r['package'] for r in measured):
        group = [r for r in measured if r['package'] == package]
        reference = None
        for label in dict.fromkeys(r['variant'] for r in group):
            selected = [r for r in group if r['variant'] == label]
            values = [r['wall_seconds'] for r in selected]
            median = statistics.median(values)
            if reference is None:
                reference = median
            ratio = median / reference
            stat = {'package': package, 'variant': label, 'runs': len(values), 'median_seconds': median,
                    'min_seconds': min(values), 'max_seconds': max(values),
                    'stddev_seconds': statistics.stdev(values) if len(values) > 1 else None,
                    'wall_change_percent': 100 * (ratio - 1), 'wall_ratio': ratio}
            stats.append(stat)
            sd = f"{stat['stddev_seconds']:.3f}" if len(values) > 1 else 'n/a'
            lines.append(f"| {package} | {label} | {len(values)} | {median:.3f} | "
                         f"{min(values):.3f}–{max(values):.3f} | {sd} | "
                         f"{stat['wall_change_percent']:.2f}% | {ratio:.3f}x |")
        hashes = {json.dumps(r['artifacts'], sort_keys=True) for r in group}
        consistent &= len(hashes) == 1
    for row in rows:
        if row.get('coverage_run'):
            lines += ['', f"Untimed {row['package']} / {row['variant']} dynamic vector configuration counts: "
                      + json.dumps(row['vset_counts']) + '.']
            if row['vset_counts']['vsetivli'] == 0:
                lines += ['', 'This workload did not execute vsetivli: its timings cannot demonstrate the vsetivli optimization.']
    (session / 'summary.md').write_text('\n'.join(lines) + '\n')
    write_json(session / 'results.json', {'status': status, 'statistics': stats,
                                        'artifacts_identical': consistent, 'runs': rows})


def arguments(argv=None, *, defaults=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('packages', nargs='*', metavar='PACKAGE', help='one or more of: ' + ', '.join(REGISTRY))
    parser.add_argument('--list', action='store_true', help='list supported upstream packages')
    parser.add_argument('--qemu', action='append', metavar='LABEL=PATH', help='repeat for A/B; first variant is baseline')
    parser.add_argument('--qemu-commit', action='append', default=[], metavar='LABEL=SHA40')
    parser.add_argument('--reference', help='QEMU label used as the statistical reference; default: first')
    parser.add_argument('--cross-cc', default='riscv64-linux-gnu-gcc')
    parser.add_argument('--guest-cc1', type=Path, help='custom native RISC-V cc1 (must match the driver version)')
    parser.add_argument('--guest-sysroot', type=Path, help='runtime directory for custom cc1; also set for a static cc1')
    parser.add_argument('--compiler', choices=('rvv', 'debian'), default='rvv',
                        help='RVV upstream compiler for this optimization, or the smaller distribution compiler')
    parser.add_argument('--rvv-compiler', dest='compiler', action='store_const', const='rvv',
                        default=argparse.SUPPRESS, help='alias for --compiler rvv')
    parser.add_argument('--bootstrap-jobs', type=int, default=4, help='parallelism for the optional RVV compiler build')
    parser.add_argument('--toolchain-lock', type=Path, help='reuse an exact Debian package lock')
    parser.add_argument('--bootstrap-lock', type=Path, help='reuse an exact RVV compiler bootstrap dependency lock')
    parser.add_argument('--data-dir', type=Path, default=DEFAULT_DATA)
    parser.add_argument('--source-dir', type=Path, help='use a local source tree, copied before each build; one package only')
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--prepare-only', action='store_true', help='populate source and guest compiler caches without building')
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--warmups', type=int, default=1)
    parser.add_argument('--jobs', type=int, default=1)
    parser.add_argument('--cpu', default='rv64,v=true,vlen=128,elen=64')
    parser.add_argument('--march', default='rv64gcv')
    parser.add_argument('--cflags', default='', help='extra target flags; these do not change how cc1 itself was built')
    parser.add_argument('--timeout', type=float, default=1800, help='per-command timeout in seconds')
    parser.add_argument('--affinity', help='host CPU list, e.g. 2 or 2,3; inherited by all children')
    parser.add_argument('--perf', action='store_true', help='collect host cycles:u/instructions:u for the timed build tree')
    parser.add_argument('--profile-vset', action='store_true', help='extra untimed build counting dynamic vset instructions')
    parser.add_argument('--profile-qemu', type=Path, help='separate plugin-enabled QEMU for the untimed coverage build')
    parser.add_argument('--plugin-include', type=Path, help='directory containing qemu-plugin.h from the profiling QEMU')
    parser.add_argument('--microbench', action='store_true', help='also run the vsetivli mechanism and scalar control after package builds')
    parser.add_argument('--micro-min-seconds', type=float, default=2.0, help='calibrate both microbench variants to at least this duration')
    parser.add_argument('--micro-iterations', type=int, help='fixed microbench loop count, skipping calibration')
    if defaults:
        parser.set_defaults(**defaults)
    args = parser.parse_args(argv)
    unknown = set(args.packages) - set(REGISTRY)
    if unknown:
        parser.error(f'unknown package workloads: {sorted(unknown)}')
    if not args.list and not args.packages:
        parser.error('choose one or more supported packages, or --list')
    if args.source_dir and len(args.packages) != 1:
        parser.error('--source-dir requires exactly one package')
    if args.source_dir and not args.source_dir.is_dir():
        parser.error(f'local source directory does not exist: {args.source_dir}')
    if bool(args.guest_cc1) != bool(args.guest_sysroot):
        parser.error('--guest-cc1 and --guest-sysroot must be provided together')
    if args.bootstrap_lock and (args.compiler != 'rvv' or args.guest_cc1):
        parser.error('--bootstrap-lock is for --compiler rvv without --guest-cc1')
    if min(args.repetitions, args.jobs, args.bootstrap_jobs) < 1 or args.warmups < 0 or args.timeout <= 0:
        parser.error('repetitions/jobs/timeout must be positive and warmups nonnegative')
    if any(c in args.march for c in ' \t\n'):
        parser.error('--march must be a single ISA string')
    if args.micro_min_seconds <= 0 or (args.micro_iterations is not None and not 1 <= args.micro_iterations < (1 << 63)):
        parser.error('positive --micro-min-seconds and --micro-iterations in 1..2^63-1 required')
    return args


def main(argv=None, *, prepared_args=None) -> int:
    args = prepared_args if prepared_args is not None else arguments(argv)
    if args.list:
        for name, spec in REGISTRY.items():
            print(f"{name:10} {spec['version']:10} {spec['url']}")
        return 0
    if not hasattr(tarfile, 'data_filter'):
        raise RuntimeError('Python with tarfile.data_filter is required (Python 3.12+, or a patched 3.11)')
    # Fail on unusable QEMU paths before an expensive first-time GCC bootstrap.
    qemus = [] if args.prepare_only else variants(args)
    profile_qemu = None
    if not args.prepare_only:
        profile_qemu = qemu_info('coverage', args.profile_qemu) if args.profile_qemu else qemus[0]
        if args.profile_vset and '-plugin' not in command_output([profile_qemu['path'], '-help']):
            raise RuntimeError('--profile-vset needs a plugin-enabled first QEMU; use --profile-qemu for a separate one')
        if args.profile_vset and (not args.plugin_include or not (args.plugin_include / 'qemu-plugin.h').is_file()):
            raise RuntimeError('--profile-vset requires --plugin-include DIR containing the matching qemu-plugin.h')
    data = args.data_dir.resolve()
    data.mkdir(parents=True, exist_ok=True)
    lock = (data / '.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError(f'Another benchmark is using {data}')
    cache = data / 'cache'
    cache.mkdir(exist_ok=True)
    cross_cc = executable(args.cross_cc)
    machine = command_output([cross_cc, '-dumpmachine'])
    version = command_output([cross_cc, '-dumpfullversion'])
    if not machine.startswith('riscv64'):
        raise RuntimeError(f'--cross-cc must target riscv64, got {machine}')
    tools = {name: executable(machine + '-' + name.lower()) for name in ('AR', 'RANLIB', 'NM', 'STRIP')}
    runtime = Path('/usr') / machine
    if not runtime.is_dir():
        raise RuntimeError(f'Target runtime not found: {runtime}; install libc6-dev-riscv64-cross')
    cross = {'path': cross_cc, 'sha256': sha256(Path(cross_cc)), 'version': version, 'machine': machine,
             'tools': tools, 'runtime': str(runtime),
             'native_cc1': command_output([cross_cc, '-print-prog-name=cc1'])}
    cross['builtin_includes'] = [str(path.resolve()) for path in (
        Path(command_output([cross_cc, '-print-file-name=include'])),
        Path(command_output([cross_cc, '-print-file-name=include-fixed']))) if path.is_dir()]
    # An upstream native cc1 has /usr/include as its compiled-in default.
    # Supply the cross driver's target libc headers explicitly as well.
    cross['builtin_includes'].append(str(runtime / 'include'))
    cross['builtin_include_sha256'] = {p: tree_digest(Path(p)) for p in cross['builtin_includes']}
    cross['native_cc1_sha256'] = sha256(Path(cross['native_cc1']))
    macros = subprocess.run([cross_cc, '-dM', '-E', '-x', 'c', '-'], input='', text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=clean_environment(), check=True).stdout
    pie = re.search(r'^#define __PIE__ (\d+)$', macros, re.MULTILINE)
    pic = re.search(r'^#define __PIC__ (\d+)$', macros, re.MULTILINE)
    cross['default_pic_flags'] = (['-fPIE' if pie[1] == '2' else '-fpie'] if pie else
                                  ['-fPIC' if pic[1] == '2' else '-fpic'] if pic else ['-fno-pie', '-fno-pic'])
    cross['tools_sha256'] = {name: sha256(Path(path)) for name, path in tools.items()}
    cross['subtools'] = {}
    for name in ('as', 'ld', 'collect2'):
        path = Path(executable(command_output([cross_cc, '-print-prog-name=' + name])))
        cross['subtools'][name] = {'path': str(path), 'sha256': sha256(path)}
    if args.toolchain_lock and not args.toolchain_lock.is_file():
        raise RuntimeError(f'Toolchain lock does not exist: {args.toolchain_lock}')
    if args.toolchain_lock and (args.compiler != 'debian' or args.guest_cc1):
        raise RuntimeError('--toolchain-lock is for --compiler debian; the RVV compiler saves a separate bootstrap.lock.json')
    if args.guest_cc1:
        tc = {'cc1': str(args.guest_cc1.resolve()), 'sysroot': str(args.guest_sysroot.resolve()),
              'sha256': sha256(args.guest_cc1), 'origin': 'custom; validated by compile probe'}
    elif args.compiler == 'rvv':
        from prepare_rvv_compiler import prepare as prepare_rvv
        tc = prepare_rvv(data, cross_cc, args.bootstrap_jobs, args.offline, args.bootstrap_lock)
    else:
        tc = prepare_toolchain(cache, version.split('.')[0], args.offline, args.toolchain_lock)
        if tc['version'] != version:
            raise RuntimeError(f"Host driver {version} and guest cc1 {tc['version']} differ; choose a matching --cross-cc")
    check_riscv(Path(tc['cc1']))
    tc['runtime_tree_sha256'] = tree_digest(Path(tc['sysroot']))
    sources = {p: source_tree(p, args, cache) for p in dict.fromkeys(args.packages)}
    if args.prepare_only:
        print('Source and toolchain caches ready:', data)
        return 0
    executable('make')
    if 'zlib-ng' in sources:
        executable('cmake')
    affinity = configuration.apply_affinity(args.affinity)
    session = data / 'runs' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    session.mkdir(parents=True)
    # Keep compiler subprocesses on the same harness version throughout a run,
    # even if the reusable scripts are edited during the experiment.
    shutil.copy2(HERE / 'compiler.py', session / 'compiler.py')
    shutil.copy2(HERE / 'vset_count.c', session / 'vset_count.c')
    microbench = None
    if args.microbench:
        # Load and snapshot the microbenchmark before the long package run.
        # Later edits must not change the code or input used by this session.
        import importlib.util
        for filename in ('microbench.py', 'microbench.S'):
            shutil.copy2(HERE / filename, session / filename)
        spec = importlib.util.spec_from_file_location('frozen_microbench', session / 'microbench.py')
        microbench = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(microbench)
    print('Results:', session, flush=True)
    metadata = {'schema': 1, 'started_utc': datetime.now(timezone.utc).isoformat(),
                'scope': 'RISC-V cc1 emulation; host GCC driver, make and cross-binutils',
                'host': platform.uname()._asdict(), 'affinity': sorted(os.sched_getaffinity(0)),
                'qemu': qemus, 'coverage_qemu': profile_qemu if args.profile_vset else None,
                'guest_compiler': tc, 'host_cross_toolchain': cross,
                'sources': {p: info for p, (_, info) in sources.items()},
                'arguments': {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                'scripts_sha256': {p.name: sha256(p) for p in HERE.iterdir() if p.suffix in {'.py', '.c', '.json', '.S', '.mk'}},
                'target_runtime_sha256': tree_digest(runtime), 'status': 'running'}
    metadata['host_environment_before'] = host_environment()
    configuration.write_manifest(session, args, qemus,
        [*sources, *(['vsetivli'] if args.microbench else [])], affinity, runner='packages',
        sources=metadata['sources'], guest_compiler=tc, host_cross_toolchain=cross,
        host_environment_before=metadata['host_environment_before'])
    cpu_info = Path('/proc/cpuinfo')
    if cpu_info.exists():
        metadata['cpu_model'] = next((line.partition(':')[2].strip() for line in cpu_info.read_text().splitlines()
                                      if line.startswith(('model name', 'uarch'))), platform.machine())
    write_json(session / 'metadata.json', metadata)
    if tc.get('lock'):
        shutil.copy2(tc['lock'], session / 'toolchain.lock.json')
    if tc.get('bootstrap_lock'):
        shutil.copy2(tc['bootstrap_lock'], session / 'bootstrap.lock.json')
    rows = []
    try:
        # Probe catches ISA, ABI, header search, matching cc1 and runtime problems
        # before spending time on a complete package build.
        probe_dir = session / 'probe'
        probe_dir.mkdir()
        probe_source = probe_dir / 'probe.c'
        probe_source.write_text('#include <stdio.h>\nint main(void) { puts("qemu-benchmark-ok"); return 0; }\n')
        banner = subprocess.run([qemus[0]['path'], '-cpu', args.cpu, '-L', tc['sysroot'], tc['cc1'],
                                 '-quiet', '-version', '-fsyntax-only', '-o', os.devnull], input='',
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=clean_environment(), timeout=args.timeout, check=True).stdout
        (probe_dir / 'cc1-version.log').write_text(banner)
        found = re.search(r'version (\d+\.\d+\.\d+)', banner)
        if found is None or found[1] != version:
            raise RuntimeError(f'Guest cc1 version does not match host driver {version}; see cc1-version.log')
        metadata['guest_compiler']['version_banner'] = banner
        for variant in qemus:
            cfg = {'cross_cc': cross_cc, 'guest_cc1': tc['cc1'], 'guest_sysroot': tc['sysroot'],
                   'dispatcher': str(session / 'compiler.py'),
                   'builtin_includes': cross['builtin_includes'],
                   'default_pic_flags': cross['default_pic_flags'],
                   'qemu': variant['path'], 'cpu': args.cpu, 'events': str(probe_dir / f"{variant['label']}.jsonl")}
            cc = make_launcher(cfg, probe_dir / variant['label'])
            output = probe_dir / (variant['label'] + '.elf')
            invoke([str(cc), f'-march={args.march}', '-mabi=lp64d', str(probe_source), '-o', str(output)],
                   probe_dir, clean_environment(), probe_dir / 'probe.log', args.timeout)
            check_riscv(output)
            invoke([variant['path'], '-cpu', args.cpu, '-L', str(runtime), str(output)],
                   probe_dir, clean_environment(), probe_dir / 'probe.log', args.timeout)
        if args.perf:
            executable('perf')
            invoke(['perf', 'stat', '-e', 'cycles:u,instructions:u', '--', 'true'], probe_dir,
                   clean_environment(), probe_dir / 'perf.log', args.timeout)
        plugin = None
        if args.profile_vset:
            plugin = session / 'vset-count.so'
            glib_flags = shlex.split(command_output(['pkg-config', '--cflags', '--libs', 'glib-2.0']))
            invoke([executable('cc'), '-shared', '-fPIC', '-O2', '-std=c11', '-I', str(args.plugin_include.resolve()),
                    str(session / 'vset_count.c'), '-o', str(plugin), *glib_flags], probe_dir, clean_environment(),
                   probe_dir / 'plugin.log', args.timeout)
            plugin_check = probe_dir / 'plugin-check'
            plugin_check.mkdir()
            invoke([profile_qemu['path'], '-cpu', args.cpu, '-L', str(runtime), '-plugin',
                    f'{plugin},out={plugin_check}', str(output)], probe_dir, clean_environment(),
                   probe_dir / 'plugin.log', args.timeout)
            if not list(plugin_check.glob('*.json')):
                raise RuntimeError('Coverage plugin failed its initial output check')
        for package, (source, _) in sources.items():
            for iteration in range(args.warmups):
                for variant in qemus:
                    rows.append(build_once(package, source, variant, tc, cross, args, session, iteration+1, True))
            for iteration in range(args.repetitions):
                # AB/BA for two variants; rotate and reverse for larger sets.
                order = qemus[iteration % len(qemus):] + qemus[:iteration % len(qemus)]
                if len(qemus) > 2 and iteration % 2:
                    order = list(reversed(order))
                for variant in order:
                    rows.append(build_once(package, source, variant, tc, cross, args, session, iteration+1, False))
                    report(rows, session, 'running')
            if args.profile_vset:
                rows.append(build_once(package, source, profile_qemu, tc, cross, args, session, 1, False, plugin))
        report(rows, session, 'complete')
        if not json.loads((session / 'results.json').read_text())['artifacts_identical']:
            raise RuntimeError('Build artifacts differ across measured runs; inspect artifacts and logs before using timings')
        if microbench:
            micro_output = microbench.run_suite(qemus, session / 'microbench', cross_cc, args.cpu,
                args.warmups, args.repetitions, args.timeout, args.micro_min_seconds,
                args.micro_iterations, profile_qemu if args.profile_vset else None,
                args.plugin_include, source_dir=session, manifest_args=args, affinity_record=affinity)
            metadata['microbench'] = str(micro_output)
            print((micro_output / 'summary.md').read_text(), flush=True)
        metadata['status'] = 'complete'
        print_summary(session / 'summary.md')
        return 0
    except BaseException as error:
        metadata.update({'status': 'failed', 'error': str(error), 'exception': type(error).__name__})
        report(rows, session, 'failed')
        raise
    finally:
        metadata['finished_utc'] = datetime.now(timezone.utc).isoformat()
        metadata['host_environment_after'] = host_environment()
        write_json(session / 'metadata.json', metadata)
        print('Saved:', session, flush=True)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as error:
        print(f'benchmark: {error}', file=sys.stderr)
        sys.exit(1)
