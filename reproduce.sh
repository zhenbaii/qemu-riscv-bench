#!/usr/bin/env bash
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

usage() {
    cat <<'EOF'
Usage: ./reproduce.sh BASELINE_COMMIT PATCHED_COMMIT --recipe RECIPE [--affinity CPU] [--dry-run]

Recipes:
  vsetvli-v1    Focused vsetvli program with perf, then LZ4, zlib and SQLite.

Use a QEMU checkout with both commits available. The default checkout is
../qemu beside this bench repository. Set QEMU_SRC for a different location.
--dry-run prints both benchmark plans without building or running them.
EOF
}

if [[ ${1-} == --help || ${1-} == -h ]]; then
    usage
    exit 0
fi
if [[ $# -lt 2 || $1 == -* || $2 == -* ]]; then
    usage >&2
    exit 2
fi

baseline=$1
candidate=$2
shift 2

recipe=
options=()
dry_run=false
while (($#)); do
    case "$1" in
        --recipe)
            [[ $# -ge 2 && -n $2 && $2 != -* && -z $recipe ]] || { usage >&2; exit 2; }
            recipe=$2; shift 2 ;;
        --dry-run) dry_run=true; options+=("$1"); shift ;;
        --affinity)
            [[ $# -ge 2 && $2 != -* ]] || { usage >&2; exit 2; }
            options+=("$1" "$2"); shift 2 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

case "$recipe" in
    vsetvli-v1)
        configs=(vsetvli-v1-program.yml vsetvli-v1-mail.yml)
        headings=('Targeted workloads' 'General workloads')
        ;;
    '') echo 'Missing required option --recipe.' >&2; usage >&2; exit 2 ;;
    *) echo "Unknown recipe: $recipe" >&2; usage >&2; exit 2 ;;
esac

workspace=$(cd -- "$here/../.." && pwd -P)
if [[ -x $workspace/build-qemu-experiment.sh && -f $workspace/qemu/configure ]]; then
    repo="$workspace/qemu"
    workspace_builder="$workspace/build-qemu-experiment.sh"
else
    repo=${QEMU_SRC:-"$here/../qemu"}
    workspace_builder=
fi
if [[ ! -f $repo/configure ]]; then
    echo "QEMU checkout not found at $repo. Set QEMU_SRC to your checkout." >&2
    exit 2
fi
repo=$(cd -- "$repo" && pwd -P)
resolve() {
    git -C "$repo" rev-parse --verify --quiet --end-of-options "$1^{commit}" ||
        { echo "Commit not available in $repo: $1" >&2; return 2; }
}
baseline=$(resolve "$baseline")
candidate=$(resolve "$candidate")
if [[ $baseline == "$candidate" ]]; then
    echo 'Baseline and patched commits must differ.' >&2
    exit 2
fi

if ! $dry_run; then
    for commit in "$baseline" "$candidate"; do
        if [[ -n $workspace_builder ]]; then
            "$workspace_builder" --commit "$commit"
        else
            "$here/scripts/build-qemu.sh" "$repo" "$commit" "$commit" user
        fi
    done
fi

binaries="$repo/experiment-binaries"
for index in "${!configs[@]}"; do
    printf '\n=== %s ===\n' "${headings[index]}"
    "$here/run.sh" --config "$here/experiments/${configs[index]}" \
        --qemu "baseline=$binaries/$baseline/qemu-riscv64" \
        --qemu "candidate=$binaries/$candidate/qemu-riscv64" \
        --qemu-commit "baseline=$baseline" --qemu-commit "candidate=$candidate" "${options[@]}"
done
