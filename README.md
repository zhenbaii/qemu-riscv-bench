# QEMU RISC-V benchmark

[中文](README.zh-CN.md)

## Reproduce a patch

Use Linux, Python 3.12+, the QEMU build dependencies, and a RISC-V Linux cross
toolchain. Profiling requires Linux `perf`.

For package builds with `build.compiler: rvv`, the bench downloads GCC and
builds and caches the guest `cc1` before the first measurement. This step uses
a RISC-V GCC 14.2.0 cross driver, `gcc`, `g++`, `make`, and `dpkg-deb`.

Put a clean QEMU checkout containing both commits at `../qemu` relative to
this repository, or set `QEMU_SRC` to the checkout path. Run the commands from
the benchmark repository.

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt

./reproduce.sh BASELINE_COMMIT PATCHED_COMMIT --recipe recipe-name
```

Replace `recipe-name` with a filename from [`recipes/`](recipes/) without
the `.yml` extension. The launcher builds both QEMUs and runs the selected recipe.
Add `--dry-run` to preview the plan or `--affinity CPU` to override its CPU affinity.

## Use prebuilt QEMUs

```sh
./run.sh --list

./run.sh --config experiments/packages.example.yml zlib \
  --qemu baseline=/path/to/baseline/qemu-riscv64 \
  --qemu candidate=/path/to/patched/qemu-riscv64
```

For a guest program, use `./run.sh --config experiments/programs.example.yml`
with the same `--qemu` options. See the [YAML reference](docs/configuration.md)
for custom workloads and recipes.

The terminal prints the combined results for a recipe. Summaries and raw
samples are saved under `data/`.

[Measurement protocol](docs/protocol.md) · [Provenance](PROVENANCE.md)
