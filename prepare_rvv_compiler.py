#!/usr/bin/env python3
"""Build an upstream GCC cc1 that itself uses RVV; no guest execution or root."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import tarfile
import sys

from toolchain import download, parse_packages, sha256, write_json

HERE = Path(__file__).resolve().parent
DEFAULT_DATA = HERE / 'data'
GCC_SHA512 = ('932bdef0cda94bacedf452ab17f103c0cb511ff2cec55e9112fc0328cbf1d803b425'
              '95728ea7b200e0a057c03e85626f937012e49a7515bc5dd256b2bf4bc396')


def output(command):
    return subprocess.check_output(command, text=True).strip()


def run(command, cwd, env, log):
    print(shlex.join(command), flush=True)
    with log.open('ab') as stream:
        stream.write(('$ ' + shlex.join(command) + '\n').encode())
        stream.flush()
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=stream,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait()
        except KeyboardInterrupt:
            import signal
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
        if code:
            raise RuntimeError(f'Compiler bootstrap command failed ({code}); see {log}\n'
                               + log.read_text(errors='replace')[-4000:])


def prepare(data: Path, cross_cc: str, jobs: int, offline: bool, bootstrap_lock: Path | None = None) -> dict:
    if not hasattr(tarfile, 'data_filter'):
        raise RuntimeError('RVV bootstrap requires Python with tarfile.data_filter (Python 3.12+)')
    cross_cc = str(Path(shutil.which(cross_cc) or cross_cc).resolve())
    version = output([cross_cc, '-dumpfullversion'])
    machine = output([cross_cc, '-dumpmachine'])
    if version != '14.2.0' or machine != 'riscv64-linux-gnu':
        raise RuntimeError('RVV bootstrap currently requires a riscv64-linux-gnu GCC 14.2.0 cross driver')
    host_arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
    if host_arch is None:
        raise RuntimeError('RVV bootstrap supports x86_64 and aarch64 Linux build hosts')
    for name in ('gcc', 'g++', 'make', 'dpkg-deb'):
        if not shutil.which(name):
            raise RuntimeError(f'RVV bootstrap requires {name}')
    cache = data.resolve() / 'cache'
    base = cache / 'rvv-gcc-14.2.0'
    base.mkdir(parents=True, exist_ok=True)
    lock_path = base / 'bootstrap.lock.json'
    if bootstrap_lock:
        supplied = json.loads(bootstrap_lock.read_text())
        if supplied.get('host_architecture') != host_arch or supplied.get('gcc_version') != version:
            raise RuntimeError('Bootstrap lock does not match the host architecture and GCC version')
        if lock_path.exists() and sha256(lock_path) != sha256(bootstrap_lock):
            raise RuntimeError('Existing bootstrap cache uses a different lock; choose a new --data-dir')
        if not lock_path.exists():
            shutil.copy2(bootstrap_lock, lock_path)
    stamp = base / 'compiler.json'
    if stamp.exists():
        metadata = json.loads(stamp.read_text())
        if sha256(Path(metadata['cc1'])) != metadata['sha256']:
            raise RuntimeError('RVV compiler cache checksum mismatch')
        if sha256(base / 'bootstrap.lock.json') != metadata['bootstrap_lock_sha256']:
            raise RuntimeError('RVV compiler bootstrap lock changed after the compiler was built')
        metadata['bootstrap_lock'] = str(base / 'bootstrap.lock.json')
        print(f"Using cached RVV compiler: {metadata['cc1']}", flush=True)
        return metadata
    print(f'Preparing the RVV compiler once; build log: {base / "build.log"}', flush=True)
    mirror = 'https://deb.debian.org/debian'
    if lock_path.exists():
        lock = json.loads(lock_path.read_text())
        if lock['host_architecture'] != host_arch:
            raise RuntimeError('RVV bootstrap cache was created for another host architecture')
    else:
        native_index = cache / 'debian-trixie-riscv64-Packages.xz'
        download(f'{mirror}/dists/trixie/main/binary-riscv64/Packages.xz', native_index, None, offline)
        host_index = cache / f'debian-trixie-{host_arch}-Packages.xz'
        download(f'{mirror}/dists/trixie/main/binary-{host_arch}/Packages.xz', host_index, None, offline)
        native = parse_packages(native_index)
        host = parse_packages(host_index)
        packages = [dict(native[name], destination='guest') for name in ('libgmp-dev', 'libmpfr-dev', 'libmpc-dev')]
        packages += [dict(host[name], destination='host') for name in
                     ('g++-14-riscv64-linux-gnu', 'libstdc++-14-dev-riscv64-cross')]
        lock = {'mirror': mirror, 'host_architecture': host_arch, 'gcc_version': version,
                'native_index_sha256': sha256(native_index), 'host_index_sha256': sha256(host_index),
                'packages': packages}
        write_json(lock_path, lock)
    for package in lock['packages']:
        archive = cache / 'debs' / Path(package['Filename']).name
        download(lock['mirror'] + '/' + package['Filename'], archive, package['SHA256'], offline)
        destination = base / package['destination']
        destination.mkdir(exist_ok=True)
        subprocess.run(['dpkg-deb', '-x', str(archive), str(destination)], check=True)
    source_archive = cache / 'downloads/gcc-14.2.0.tar.xz'
    # Pinned from the upstream release's sha512.sum, rather than trusting a
    # freshly fetched checksum on each run.
    expected = GCC_SHA512
    download('https://gcc.gnu.org/pub/gcc/releases/gcc-14.2.0/gcc-14.2.0.tar.xz', source_archive, None, offline)
    with source_archive.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha512').hexdigest() != expected:
            raise RuntimeError('GCC source SHA-512 mismatch against the upstream release checksum')
    source = base / 'gcc-14.2.0'
    if not (source / 'configure').exists():
        if source.exists():
            shutil.rmtree(source)
        with tarfile.open(source_archive) as stream:
            stream.extractall(base, filter='data')
    host = base / 'host'
    guest = base / 'guest'
    include = host / 'usr/riscv64-linux-gnu/include/c++/14'
    cxx = base / 'cross-cxx'
    command = [str(host / 'usr/bin/riscv64-linux-gnu-g++-14'),
               '-B' + str(host / 'usr/libexec/gcc-cross/riscv64-linux-gnu/14') + '/',
               '-B/usr/lib/gcc-cross/riscv64-linux-gnu/14/',
               '-B/usr/riscv64-linux-gnu/bin/',
               '-B/usr/riscv64-linux-gnu/lib/',
               '-isystem', str(include), '-isystem', str(include / machine),
               '-isystem', output([cross_cc, '-print-file-name=include']),
               '-isystem', '/usr/riscv64-linux-gnu/include',
               '-L' + str(host / 'usr/lib/gcc-cross/riscv64-linux-gnu/14')]
    cxx.write_text('#!/bin/sh\nexec ' + shlex.join(command) + ' "$@"\n')
    cxx.chmod(0o755)
    build = base / 'build'
    build.mkdir(exist_ok=True)
    env = os.environ.copy()
    for name in ('CFLAGS', 'CXXFLAGS', 'CPPFLAGS', 'LDFLAGS', 'LIBS', 'GCC_EXEC_PREFIX',
                 'COMPILER_PATH', 'CPATH', 'C_INCLUDE_PATH', 'CPLUS_INCLUDE_PATH', 'MAKEFLAGS'):
        env.pop(name, None)
    flags = '-O2 -g0 -march=rv64gcv -mabi=lp64d -I' + str(guest / 'usr/include/riscv64-linux-gnu')
    env.update({'CC': cross_cc, 'CXX': str(cxx), 'CC_FOR_BUILD': shutil.which('gcc'),
                'CXX_FOR_BUILD': shutil.which('g++'), 'CFLAGS': flags, 'CXXFLAGS': flags,
                'CFLAGS_FOR_BUILD': '-O2 -g0', 'CXXFLAGS_FOR_BUILD': '-O2 -g0',
                'LDFLAGS_FOR_BUILD': '',
                'LDFLAGS': '-static', 'LC_ALL': 'C', 'SOURCE_DATE_EPOCH': '1704067200'})
    native = output(['gcc', '-dumpmachine'])
    configure = [str(source / 'configure'), '--build=' + native, '--host=' + machine,
                 '--target=' + machine, '--prefix=' + str(base / 'install'),
                 '--disable-bootstrap', '--disable-multilib', '--disable-nls', '--disable-werror',
                 '--enable-languages=c', '--without-isl', '--disable-libsanitizer',
                 '--disable-libquadmath', '--disable-libgomp', '--disable-libatomic']
    for name in ('gmp', 'mpfr', 'mpc'):
        configure += [f'--with-{name}-include=' + str(guest / ('usr/include/riscv64-linux-gnu' if name == 'gmp' else 'usr/include')),
                      f'--with-{name}-lib=' + str(guest / 'usr/lib/riscv64-linux-gnu')]
    log = base / 'build.log'
    if not (build / 'Makefile').exists():
        run(configure, build, env, log)
    # GCC 14's EXTRA_BUILD_FLAGS overrides CFLAGS/LDFLAGS for build-machine
    # generators but omits CXXFLAGS. Supply all three for a Canadian cross.
    build_command = ['make', f'-j{jobs}',
                     'EXTRA_BUILD_FLAGS=CFLAGS=-O2 CXXFLAGS=-O2 LDFLAGS=', 'all-gcc']
    run(build_command, build, env, log)
    cc1 = build / 'gcc/cc1'
    if not cc1.exists():
        raise RuntimeError(f'GCC build produced no cc1; see {log}')
    runtime = base / 'empty-runtime'
    runtime.mkdir(exist_ok=True)
    metadata = {'cc1': str(cc1), 'sysroot': str(runtime), 'sha256': sha256(cc1),
                'version': version, 'origin': 'upstream GCC built for an RVV host',
                'source_url': 'https://gcc.gnu.org/pub/gcc/releases/gcc-14.2.0/gcc-14.2.0.tar.xz',
                'source_sha256': sha256(source_archive), 'source_sha512': expected,
                'bootstrap_lock_sha256': sha256(lock_path), 'configure': configure,
                'bootstrap_lock': str(lock_path),
                'build_command': build_command,
                'cflags': flags, 'ldflags': '-static', 'log': str(log),
                'cross_cc_sha256': sha256(Path(cross_cc)), 'script_sha256': sha256(Path(__file__))}
    write_json(stamp, metadata)
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=DEFAULT_DATA)
    parser.add_argument('--cross-cc', default='riscv64-linux-gnu-gcc')
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--bootstrap-lock', type=Path, help='reuse an exact RVV bootstrap dependency lock')
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    args.data_dir.mkdir(parents=True, exist_ok=True)
    with (args.data_dir / '.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(json.dumps(prepare(args.data_dir, args.cross_cc, args.jobs, args.offline, args.bootstrap_lock), indent=2))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as error:
        print(f'RVV compiler: {error}', file=sys.stderr)
        sys.exit(1)
