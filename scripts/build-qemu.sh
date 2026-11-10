#!/usr/bin/env bash
# Build a revision in an existing QEMU checkout.
set -euo pipefail
repo=$(cd -- "$1" && pwd -P)
ref=$2
variant=$3
mode=$4
case ${mode} in
    user) target=riscv64-linux-user; binary_name=qemu-riscv64; metadata_name=metadata.json ;;
    system) target=riscv64-softmmu; binary_name=qemu-system-riscv64; metadata_name=metadata-system.json ;;
    *) echo 'Build mode must be user or system.' >&2; exit 2 ;;
esac

git_dir=$(git -C "${repo}" rev-parse --absolute-git-dir)
exec 9>"${git_dir}/qemu-experiment-build.lock"
flock -n 9 || { echo 'Another shared-tree build is running.' >&2; exit 2; }
if [[ -n $(git -C "${repo}" status --porcelain --untracked-files=no) ]]; then
    echo 'Commit or stash tracked QEMU edits before building a recorded variant.' >&2
    exit 2
fi
original_branch=$(git -C "${repo}" symbolic-ref -q --short HEAD || true)
original_commit=$(git -C "${repo}" rev-parse HEAD)
commit=$(git -C "${repo}" rev-parse --verify --end-of-options "${ref}^{commit}")
if [[ ${ref} != HEAD ]]; then
    restore() {
        if [[ -n ${original_branch} ]]; then
            git -C "${repo}" switch --quiet "${original_branch}"
        else
            git -C "${repo}" switch --quiet --detach "${original_commit}"
        fi
    }
    trap restore EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    git -C "${repo}" switch --quiet --detach "${commit}"
fi

build=${repo}/build-experiments
configure_args=(--target-list="${target}" --disable-werror --disable-docs)
if [[ ${mode} == user ]]; then
    configure_args+=(--static)
else
    configure_args+=(--disable-gtk --disable-sdl --disable-vnc --disable-opengl --disable-tools --disable-guest-agent)
fi
printf -v configure_signature "'%s' " "${configure_args[@]}"
mkdir -p "${build}"
if [[ ! -f ${build}/build.ninja || ! -f ${build}/config.status ]] ||
        ! grep -Fq -- "${configure_signature}\"\$@\"" "${build}/config.status"; then
    (cd "${build}" && ../configure "${configure_args[@]}")
fi
ninja -C "${build}" -j "${QEMU_BUILD_JOBS:-8}" "${binary_name}"
python3 - "${repo}" "${variant}" "${commit}" "${ref}" "${target}" "${binary_name}" "${metadata_name}" "${configure_args[@]}" <<'PY'
import hashlib
import json
import pathlib
import shutil
import sys

repo, variant, commit, ref, target, binary_name, metadata_name, *configure_args = sys.argv[1:]
repo = pathlib.Path(repo)
output = repo / 'experiment-binaries' / variant
# Old workspace aliases point into the immutable history store.
if output.is_symlink():
    output.unlink()
output.mkdir(parents=True, exist_ok=True)
binary = output / binary_name
temporary = output / (binary_name + '.tmp')
shutil.copy2(repo / 'build-experiments' / binary_name, temporary)
temporary.replace(binary)
digest = hashlib.sha256(binary.read_bytes()).hexdigest()
metadata = output / metadata_name
temporary = metadata.with_suffix('.tmp')
temporary.write_text(json.dumps({
    'commit': commit, 'ref': ref, 'sha256': digest, 'source': str(repo),
    'target': target, 'configure_args': configure_args,
}, indent=2) + '\n')
temporary.replace(metadata)
print(f'Binary: {binary}\nCommit: {commit}\nSHA256: {digest}')
PY
