# QEMU RISC-V benchmark

[中文](README.zh-CN.md)

## Reproduce a patch

The supplied recipes use x86-64 Linux, Python 3.12+, the QEMU build
dependencies, `riscv64-linux-gnu-gcc`, and `riscv64-linux-gnu-gcc-14` (14.2.0).
Install Linux `perf` to collect profiles.

Put a clean QEMU checkout containing both commits at `../qemu` relative to
this repository, or set `QEMU_SRC` to the checkout path. Run the commands from
the benchmark repository.

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt

./reproduce.sh BASELINE_COMMIT PATCHED_COMMIT --recipe vsetvli-v2
```

The launcher builds both QEMUs and pins runs to the first allowed logical CPU.
`vsetvli-v1` and `vsetvli-v2` run the same focused program and LZ4, zlib, and
SQLite builds. The first package run builds and caches the guest `cc1`.
Add `--dry-run` to preview the plan or `--affinity CPU` to select a logical CPU.

## Use prebuilt QEMUs

```sh
./run.sh --list

./run.sh --config experiments/vsetvli-v1-mail.yml zlib \
  --qemu baseline=/path/to/baseline/qemu-riscv64 \
  --qemu candidate=/path/to/patched/qemu-riscv64
```

Use `./run.sh --config experiments/vsetvli-v1-program.yml` with the same
`--qemu` options to run just the focused program. For custom workloads, see
the [YAML reference](docs/configuration.md).

The terminal prints the combined results for a recipe. Summaries and raw
samples are saved under `data/programs/` and `data/runs/`.

[Measurement protocol](docs/protocol.md) · [Provenance](PROVENANCE.md)
