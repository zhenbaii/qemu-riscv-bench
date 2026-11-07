"""Fetch a native RISC-V cc1 and runtime from public Debian packages, without root."""
from __future__ import annotations

import hashlib
import json
import lzma
from pathlib import Path
import re
import shutil
import subprocess
import urllib.request


def sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def download(url: str, dest: Path, digest: str | None, offline: bool) -> str:
    if dest.is_file():
        actual = sha256(dest)
        if digest is None or actual == digest:
            return actual
        raise RuntimeError(f'Checksum mismatch in cached file: {dest}; remove it and retry')
    if offline:
        raise RuntimeError(f'Offline cache miss: {dest}')
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_name(dest.name + '.part')
    print(f'Downloading {url}', flush=True)
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'qemu-build-benchmark/1'})
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open('wb') as out:
            shutil.copyfileobj(response, out)
        actual = sha256(temporary)
        if digest is not None and actual != digest:
            raise RuntimeError(f'Checksum mismatch for {url}: expected {digest}, got {actual}')
        temporary.replace(dest)
        return actual
    finally:
        temporary.unlink(missing_ok=True)


def parse_packages(path: Path) -> dict[str, dict[str, str]]:
    keep = {'Package', 'Version', 'Architecture', 'Depends', 'Pre-Depends', 'Filename', 'SHA256'}
    packages = {}
    record: dict[str, str] = {}
    with lzma.open(path, 'rt', encoding='utf-8') as stream:
        for line in stream:
            if line == '\n':
                if 'Package' in record:
                    packages[record['Package']] = record
                record = {}
            elif not line[0].isspace():
                key, _, value = line.rstrip('\n').partition(': ')
                if key in keep:
                    record[key] = value
    if 'Package' in record:
        packages[record['Package']] = record
    return packages


def closure(packages: dict, root: str) -> list[dict]:
    selected = {}
    pending = [root]
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        if name not in packages:
            raise RuntimeError(f'Debian index has no package {name}; use a matching --cross-cc or custom --guest-cc1')
        package = packages[name]
        selected[name] = package
        deps = ','.join(package.get(field, '') for field in ('Depends', 'Pre-Depends'))
        for group in deps.split(','):
            if not group.strip():
                continue
            alternatives = [re.match(r'\s*([a-z0-9+.-]+)', x).group(1) for x in group.split('|')]
            match = next((x for x in alternatives if x in packages), None)
            if match is None:
                raise RuntimeError(f'Unresolved Debian dependency: {group}')
            pending.append(match)
    return [selected[x] for x in sorted(selected)]


def prepare(cache: Path, major: str, offline: bool, lock_path: Path | None = None) -> dict:
    if not shutil.which('dpkg-deb'):
        raise RuntimeError('dpkg-deb is required to unpack the guest toolchain (Debian/Ubuntu package: dpkg)')
    mirror = 'https://deb.debian.org/debian'
    lock_path = lock_path or cache / f'debian-trixie-gcc{major}.lock.json'
    if lock_path.exists():
        lock = json.loads(lock_path.read_text())
    else:
        index = cache / 'debian-trixie-riscv64-Packages.xz'
        url = f'{mirror}/dists/trixie/main/binary-riscv64/Packages.xz'
        digest = download(url, index, None, offline)
        packages = closure(parse_packages(index), f'cpp-{major}-riscv64-linux-gnu')
        lock = {'schema': 1, 'mirror': mirror, 'suite': 'trixie', 'architecture': 'riscv64',
                'gcc_major': major, 'index_url': url, 'index_sha256': digest, 'packages': packages}
        write_json(lock_path, lock)
    if (not isinstance(lock, dict) or lock.get('schema') != 1 or lock.get('gcc_major') != major
            or lock.get('architecture') != 'riscv64' or not lock.get('packages')):
        raise RuntimeError(f'Toolchain lock does not match GCC {major}: {lock_path}')
    root = cache / ('toolchain-' + sha256(lock_path)[:16])
    stamp = root / '.complete.json'
    for package in lock['packages']:
        filename = Path(package['Filename']).name
        download(lock['mirror'].rstrip('/') + '/' + package['Filename'], cache / 'debs' / filename,
                 package['SHA256'], offline)
    if not stamp.exists():
        temporary = root.with_name(root.name + '.partial')
        if temporary.exists():
            shutil.rmtree(temporary)
        temporary.mkdir(parents=True)
        for package in lock['packages']:
            subprocess.run(['dpkg-deb', '-x', str(cache / 'debs' / Path(package['Filename']).name),
                            str(temporary)], check=True)
        for name in ('lib', 'bin', 'sbin'):
            if not (temporary / name).exists():
                (temporary / name).symlink_to('usr/' + name)
        write_json(temporary / '.complete.json', {'lock_sha256': sha256(lock_path)})
        if root.exists():
            shutil.rmtree(root)
        temporary.rename(root)
    # Debian packages assume a merged-/usr filesystem. dpkg-deb alone does not
    # create these directory aliases, including the ELF interpreter's /lib.
    for name in ('lib', 'bin', 'sbin'):
        if not (root / name).exists() and not (root / name).is_symlink():
            (root / name).symlink_to('usr/' + name)
    matches = list(root.glob(f'usr/libexec/gcc/riscv64-linux-gnu/{major}/cc1'))
    matches += list(root.glob(f'usr/lib/gcc/riscv64-linux-gnu/{major}/cc1'))
    if len(matches) != 1:
        raise RuntimeError(f'Expected one native RISC-V cc1 under {root}, found {matches}')
    compiler = next(p for p in lock['packages'] if p['Package'] == f'cpp-{major}-riscv64-linux-gnu')
    return {'cc1': str(matches[0].resolve()), 'sysroot': str(root.resolve()),
            'sha256': sha256(matches[0]), 'version': compiler['Version'].split('-')[0],
            'lock': str(lock_path.resolve()), 'lock_sha256': sha256(lock_path), 'origin': lock['mirror']}
