#!/usr/bin/env python3
"""Host GCC driver launcher and -wrapper dispatcher. Never uses binfmt_misc."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time


def emit(path: str, row: dict) -> None:
    data = (json.dumps(row) + '\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def main() -> int:
    mode, config_path, *args = sys.argv[1:]
    config = json.loads(Path(config_path).read_text())
    if mode == 'cc':
        if any(x.startswith(('-flto', '-fplugin=', '-wrapper')) for x in args):
            raise RuntimeError('LTO, GCC plugins and replacement wrappers are not supported by this C benchmark')
        wrapper = ','.join([sys.executable, str(Path(__file__).resolve()), 'dispatch', config_path])
        command = [config['cross_cc'], '-wrapper', wrapper, '-fno-lto', '-fno-use-linker-plugin', *args]
        os.execv(command[0], command)
    if mode != 'dispatch' or not args:
        raise RuntimeError('Invalid compiler dispatcher invocation')
    tool, *tool_args = args
    name = Path(tool).name
    if name == 'cc1':
        if any(x.startswith(('-flto', '-fplugin=')) for x in tool_args):
            raise RuntimeError('LTO and compiler plugins are unsupported, including flags expanded from response files')
        # The host driver contributes host paths for its target headers; cc1's
        # architecture matches the target. -L supplies only cc1's guest runtime.
        env = os.environ.copy()
        for key in tuple(env):
            if key.startswith('QEMU_'):
                del env[key]
        command = [config['qemu'], '-cpu', config['cpu'], '-L', config['guest_sysroot']]
        if config.get('plugin'):
            command += ['-plugin', f"{config['plugin']},out={config['coverage_dir']}"]
        command += [config['guest_cc1']]
        pic_options = {'-fPIC', '-fpic', '-fPIE', '-fpie', '-fno-PIC', '-fno-pic', '-fno-PIE', '-fno-pie'}
        if not any(x in pic_options for x in tool_args):
            command += config.get('default_pic_flags', [])
        command += tool_args
        if '-nostdinc' not in tool_args:
            for directory in config.get('builtin_includes', []):
                command += ['-isystem', directory]
        started = time.perf_counter()
        result = subprocess.run(command, env=env)
        emit(config['events'], {'tool': 'cc1', 'emulated': True, 'qemu': config['qemu'],
                               'cwd': os.getcwd(),
                               'host_affinity': sorted(os.sched_getaffinity(0)),
                               'pid': os.getpid(), 'argv': command, 'returncode': result.returncode,
                               'wall_seconds': time.perf_counter() - started})
        return result.returncode if result.returncode >= 0 else 128 - result.returncode
    if name in {'cc1plus', 'lto1', 'lto-wrapper', 'f951'}:
        raise RuntimeError(f'Unsupported compiler subcommand {name}; this runner handles C without LTO')
    emit(config['events'], {'tool': name, 'emulated': False, 'argv': args, 'cwd': os.getcwd()})
    os.execv(tool, args)
    return 127


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, OSError) as error:
        print(f'benchmark compiler: {error}', file=sys.stderr)
        sys.exit(2)
