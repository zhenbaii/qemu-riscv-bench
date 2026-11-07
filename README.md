# qemu-riscv-bench

Reproduce QEMU RISC-V user-mode performance measurements with explicit emulator
binaries, checked guest outputs, and retained per-run samples.

Workloads are defined in [`workloads/`](workloads/). QEMU identities, host
affinity, compiler choices, and measurement parameters belong in separate
[`experiments/`](experiments/) YAML files. `run.sh` is the common entry point.
Configuration reference: [`docs/configuration.md`](docs/configuration.md).

Three workloads are available:

| Entry point | Workload | Preparation |
| --- | --- | --- |
| `vsetvli.py` | Eight libc-free loops covering immediate/register vtype, zero-register forms, 4/8/16-byte vector capacities, and a scalar control | RISC-V cross assembler/linker; no downloads |
| `microbench.py` | A libc-free `vsetivli` state-switching loop and scalar control | RISC-V cross assembler/linker; no downloads |
| `run.sh` | Clean builds of LZ4 1.10.0, zlib 1.3.1, SQLite 3.45.1, and zlib-ng 2.2.3, with RISC-V GCC `cc1` executed by QEMU | Pinned package sources and a guest compiler |

Start with a dry run to inspect the configuration. The package benchmark's
default RVV compiler needs a substantial one-time GCC build. Python 3.12+ and
PyYAML are required; dependencies are listed in `requirements.txt`.

## Inspect and configure an experiment

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt

./run.sh --list
./run.sh --list-cpus
./run.sh --config experiments/packages.example.yml --dry-run

cp experiments/packages.example.yml experiments/packages.local.yml
```

Edit `packages.local.yml` to fill in the before/after QEMU paths and, when
available, their full commit IDs. Set `host.affinity: [2]` to select logical CPU
2 on your machine, or leave it `null` to inherit the current mask. The CPU ID is
an example, not a built-in default. The `.local.yml` files are ignored by Git.

Preview the final settings, optionally overriding values on the command line:

```sh
./run.sh --config experiments/packages.local.yml --affinity 2 --jobs 1 --dry-run
```

`--dry-run` reads YAML and host affinity, then prints resolved JSON. It does not
execute QEMU (even `--version`), run build tools, download inputs, apply affinity,
or create output directories. QEMU paths may remain `null` while previewing an
example. A real run rejects unfilled paths.

To execute that experiment when ready, use the same command without `--dry-run`.
For instruction microbenchmarks, choose `experiments/vsetvli.example.yml` or
`experiments/vsetivli.example.yml`. The direct Python entry points below remain
available for compatibility.

Every executed session writes `manifest.json` containing the experiment and
workload definitions with SHA-256 values, resolved arguments, QEMU identities,
and requested/inherited/effective affinity. The existing raw timing and metadata
files remain available alongside it.

## Requirements

Use a Linux host, Python 3.12+, a RISC-V Linux cross toolchain, and one or more
`qemu-riscv64` binaries with the RISC-V Vector extension enabled. Supply absolute
QEMU paths explicitly. The experiment's `reference` or `--reference LABEL`
selects the statistical reference; otherwise the first label is used.
The package runner currently expects the Debian/Ubuntu cross-toolchain layout;
the libc-free loops need only the cross compiler and binutils.

For a Debian/Ubuntu host, the package names are:

```sh
sudo apt install python3 python3-venv gcc g++ make dpkg gcc-riscv64-linux-gnu \
  binutils-riscv64-linux-gnu libc6-dev-riscv64-cross cmake
```

The optional RVV GCC bootstrap currently requires a **14.2.0** RISC-V cross
driver. Check `riscv64-linux-gnu-gcc -dumpfullversion`; pass `--cross-cc` to
select a matching driver. `--compiler debian` and custom guest compilers are
other options, described below. CMake is needed only for zlib-ng. `perf`, GDB,
and QEMU plugin headers are optional diagnostic dependencies.

## Quick start: vsetvli

Run from this repository's root, replacing the two QEMU paths and CPU affinity
with your own. Omit `--affinity` to inherit the current affinity.

```sh
python3 vsetvli.py --list

python3 vsetvli.py \
  --qemu baseline=/absolute/path/before/qemu-riscv64 \
  --qemu candidate=/absolute/path/after/qemu-riscv64 \
  --affinity 2 --repetitions 9 --warmups 1
```

The script builds each guest, checks iterations 1, 2, 37, and 1000 against a
scalar/CSR/vector oracle, calibrates a common iteration count for all QEMUs
within each case, and measures alternating AB/BA pairs. The default minimum
calibration duration is one second. Every guest uses
`rv64,v=true,vlen=128,elen=64`; the oracle and 4/8/16-byte cases use this fixed
VLEN. Outputs include the accumulated scalar result, `vl`, `vtype`, `vstart`,
and the vector bytes. The scalar guest is compiled with `-march=rv64gc` and its
disassembly is checked for vector instructions.

For a quick functional smoke run, use `--iterations 1000 --warmups 0
--repetitions 1`. Such a short run is dominated by startup and is unsuitable
for a performance claim. A fixed count such as `--iterations 100000000`
skips calibration. Select cases with repeated `--case NAME` arguments.

Results appear in a new `data/vsetvli/<UTC timestamp>/` directory:

- `summary.md`: medians, ranges, reduction relative to the first QEMU, and paired wins.
- `samples.csv`: every calibration, warmup, and measured sample, with its phase.
- `results.json`: arguments, QEMU hashes/commits, raw samples, checks, and host settings.
- `inputs/`: assembly snapshots, rebuilt ELF files, disassemblies, symbols, and build commands.
- `samples/`: stdout and stderr for each execution.

Use `--output NEW_DIRECTORY` or `--data-dir DIRECTORY` to select another
destination. Existing output directories are never overwritten. Failed runs
return nonzero and retain a result marked `failed`.

## Lookup counts and hardware counters

These are separate executions after the uninstrumented measurements:

```sh
python3 vsetvli.py \
  --qemu baseline=/absolute/path/before/qemu-riscv64 \
  --qemu candidate=/absolute/path/after/qemu-riscv64 \
  --affinity 2 --perf --lookup-counts
```

`--perf` records host user-mode cycles, instructions, branches, and branch misses.
`--lookup-counts` requires an x86-64 host and unstripped, non-LTO QEMUs with
usable debug information for `CPURISCVState`, `RISCVCPU`, `helper_lookup_tb_ptr`,
`riscv_get_tb_cpu_state`, and `cpu_tb_exec`. It runs each vector fixture for 37
iterations under **host GDB**, without the guest gdbstub. Counts at the tested
instruction's successor PC are reported separately for helper entry, TB-state
extraction, and dispatcher entry. TB-state extraction is a proxy for lookup-key
requests in these exception-free fixtures, not an independent count of hash
table misses. The implementation assumes the x86-64 SysV ABI and QEMU's
24-byte `get_tb_cpu_state` return type; recheck that assumption when adapting it
to other QEMU versions. An unsupported setup fails rather than reporting zero.

Use `--diagnose-only` to run correctness checks and GDB diagnostics without
performance measurements. These counts are useful for distinguishing repeated
lookups from one-time direct-link establishment. They are not timing samples.

## Package builds

```sh
./run.sh --list

./run.sh zlib \
  --qemu baseline=/absolute/path/before/qemu-riscv64 \
  --qemu candidate=/absolute/path/after/qemu-riscv64 \
  --affinity 2 --jobs 1 --warmups 1 --repetitions 5
```

The default `--compiler rvv` downloads GCC 14.2.0 and cross-builds a static native
RISC-V `cc1` whose own code uses `-march=rv64gcv`. GCC's release archive has a
pinned SHA-512; the package releases have pinned SHA-256 values in
[`workloads/packages/`](workloads/packages/). The x86-64 dependency lock used for the
historical compiler build is in [`locks/`](locks/):

```sh
./run.sh zlib --prepare-only \
  --bootstrap-lock locks/rvv-gcc-14.2.0-amd64.json
```

The bootstrap supports x86-64 and AArch64 Linux hosts. Its first build requires
several GB of disk and additional build time, all outside the measurements.
On AArch64, omit the amd64 lock and retain the generated architecture-specific
`bootstrap.lock.json`. Cache downloads and locks are kept in `data/cache/`.
Old Debian package URLs may eventually disappear; preserve the checked archives
and locks for long-term reproduction.

zlib's release asset and fossil mirror must match the same fixed SHA-256.
Alternative download URLs do not change the source version or accepted bytes.

The smaller `--compiler debian` mode extracts a distribution RISC-V compiler
and runtime without installing them. It requires a matching host cross-driver
version. It is a different workload and may execute few of the vector
instructions relevant to a patch. Its generated dependency lock can be reused
with `--toolchain-lock FILE`.
Debian package hashes come from the HTTPS Packages index; this lightweight
extractor does not implement APT Release-signature verification.

To supply an existing native RISC-V compiler:

```sh
./run.sh zlib \
  --qemu baseline=/absolute/path/before/qemu-riscv64 \
  --qemu candidate=/absolute/path/after/qemu-riscv64 \
  --cross-cc /absolute/path/to/riscv64-linux-gnu-gcc \
  --guest-cc1 /absolute/path/to/riscv64/cc1 \
  --guest-sysroot /absolute/path/to/guest-runtime \
  --affinity 2 --jobs 1 --warmups 1 --repetitions 5
```

The guest compiler and host cross driver must match. For a static `cc1`, supply
an existing empty directory as `--guest-sysroot`. Target program headers and
libraries come from the host cross toolchain's `/usr/<target>/` directory;
`--guest-sysroot` supplies the runtime of the guest compiler itself.

Once the cache is populated, add `--offline` to prohibit downloads. Use
`--source-dir DIRECTORY` with one selected package for local sources; builds
operate on copies. A shared data directory is locked against concurrent runs.

### What is timed

```text
host make / CMake
  -> host RISC-V GCC driver
     -> selected qemu-riscv64 -> native RISC-V cc1
     -> host RISC-V assembler / linker
```

The elapsed time covers a clean package build with **emulated compiler stages**.
Preparation, configure, warmups, and validation are excluded. Every round starts
with a fresh source copy. Defaults are `-O2 -g0 -march=rv64gcv -mabi=lp64d`, no
LTO, and one build job. The driver, make, assembler, and linker run on the host;
this is not a whole guest userspace build. The compiler's own build flags and
the flags it uses to compile a package are recorded separately.

Results in `data/runs/<UTC timestamp>/` include per-round timing, compiler
invocations, build/check logs, artifact hashes, source/toolchain fingerprints,
and metadata. Built artifacts must be byte-identical across measured rounds.
LZ4 and zlib checks run their produced programs; SQLite checks queries,
persistence, and database integrity; zlib-ng checks compression round trips.

For untimed dynamic `vsetivli`/`vsetvli`/`vsetvl` instruction counts, add
`--profile-vset --plugin-include /path/to/qemu/include/plugins`. Supply
`--profile-qemu PATH` if the measured QEMUs lack plugin support. Building the
plugin requires a host C compiler, pkg-config, and GLib headers. Unlike
`vsetvli.py --perf`, the package runner's `--perf` wraps the measured build;
compare only runs made with the same instrumentation setting.

## vsetivli loop

```sh
python3 microbench.py \
  --qemu baseline=/absolute/path/before/qemu-riscv64 \
  --qemu candidate=/absolute/path/after/qemu-riscv64 \
  --affinity 2 --repetitions 9
```

This older fixture switches between `e32/AVL=4` and `e16/AVL=8`, executes two
`vsetivli` instructions per loop, and records direct-link edges in a separate
short `-d exec` run. Its scalar control has no libc/CRT dependency. It tests
`vsetivli`; use `vsetvli.py` for the reviewer-followup immediate-vtype cases.

## Revisions, historical data, and checks

Build the before/after QEMUs with the same host compiler and configuration.
Pass full commits with repeated `--qemu-commit LABEL=SHA40`. The harness also
recognizes a binary's sibling `metadata.json` containing `sha256` and `commit`,
and rejects a checksum mismatch. It never builds or switches QEMU source trees.

The [historical v1/v2 data](results/20261106-vsetvli-v1-v2/) records its original
QEMU identities and all 144 measured microbenchmark samples. See
[`docs/historical-vsetvli.md`](docs/historical-vsetvli.md) for the scope and
limitations. These are archived measurements made before this repository was
assembled; they are not results of running this reorganized harness.

The [measurement protocol](docs/protocol.md) explains order, timing boundaries,
and controls. Run the harness checks with:

```sh
python3 -m unittest discover -s tests -v
```

The harness and fixtures were developed with AI assistance; see
[`PROVENANCE.md`](PROVENANCE.md). Downloaded projects retain their upstream
licenses. [中文说明](README.zh-CN.md).
