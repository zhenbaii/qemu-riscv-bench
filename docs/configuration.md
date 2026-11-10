# YAML configuration

## Workload definitions

Each package has one versioned definition in `workloads/packages/`:

```yaml
schema: 1
id: zlib
kind: package-build
source:
  version: 1.3.1
  url: https://github.com/madler/zlib/releases/download/v1.3.1/zlib-1.3.1.tar.gz
  mirrors:
    - https://zlib.net/fossils/zlib-1.3.1.tar.gz
  sha256: 9a93b2b7dfdac77ceba5a558a580e74667dd6fede4585b91eefb60f03b72df23
recipe: zlib
artifacts: [libz.a, example, minigzip]
validation: zlib-example
```

The definition contains input identity, a build recipe, expected output paths,
and a validation recipe. Build and validation code lives in the runner. Supported
recipes are `lz4`, `zlib`, `sqlite`, and `zlib-ng`; their validation recipes are
`lz4-roundtrip`, `zlib-example`, `sqlite-integrity`, and `zlib-ng-roundtrip`.
Adding a new build system requires a recipe implementation as well as a YAML
definition. YAML is data, not executable shell code.

`workloads/micro/vsetvli.yml` describes the eight fixtures and their assembly
hashes. `workloads/micro/vsetivli.yml` identifies the shared assembly source and
two cases. The guest CPU requirement belongs to these fixed instruction
fixtures; the vsetvli oracle uses VLEN 128. Assembly is stored in `vsetvli/` and
`microbench.S`. Oracle, calibration, and diagnostic logic remains in Python.

Workload documents are loaded once per process and copied into each session's
manifest with their original SHA-256. Changing a workload version or definition
creates a new measurement identity. Archived JSON results retain their original
historical schema and are not rewritten by this migration.

### Custom guest programs

Add a YAML definition under `workloads/programs/` for a local guest workload:

```yaml
schema: 1
id: rvv-add
kind: guest-program
source:
  path: rvv-add.c
args: ["20000"]
expected_stdout: |
  rvv-add: ok
```

`source.path` is relative to the workload YAML unless absolute. It points to
one self-contained `.c`, `.S` or `.s` file. The host cross compiler builds a
static RISC-V ELF with `-O3 -g0 -march=rv64gcv -mabi=lp64d` by default. Use
`build.march` and `build.cflags` to select other flags. For assembly providing
its own `_start`, pass `-nostdlib` in `build.cflags`.

Set `runner: programs` and list the desired IDs in `workloads` in an experiment
file. `args` are literal strings passed to the guest program. A successful
sample exits zero and its stdout matches `expected_stdout` byte for byte,
including the final newline if present. Use `expected_stdout: ""` for a
self-checking workload that exits silently.

The runner times fresh QEMU processes, including startup. Choose arguments
that make the guest do enough work per process for the comparison. Source
compilation, warmups and output checks are outside the measured samples.
Session directories under `data/programs/` preserve the source snapshot, ELF,
their SHA-256 values, per-run output and raw timing samples.

Enable host sampling for a `programs` experiment with:

```yaml
diagnostics:
  perf_profile: true
  profile_symbol: helper_lookup_tb_ptr
```

After timing completes, the runner repeats each selected program once per QEMU
under `perf record -e cycles:u` at a requested 999 Hz. It saves `perf.data`
and the native text report under `profile-<label>/`. The optional
`profile_symbol` adds a row for the symbol's self overhead. The denominator
includes all sampled user-space cycles, including translated guest code.
This requires Linux perf and permission to sample user-space hardware events.
If profiling is unavailable, the summary records the error and keeps the timing results.

## Recipe files

`reproduce.sh --recipe NAME` reads `recipes/NAME.yml`. Use `include` to
reuse another recipe and `stages` to add experiment files from `experiments/`.
Included recipes run in the listed order before the recipe's own stages.
Each included recipe runs once. The same baseline and candidate commits are
used for all stages.

The supplied `vsetvli-v2` recipe only includes `vsetvli-v1`. The following
example adds the optional lookup diagnostic in a custom recipe.

```yaml
schema: 1
include: [vsetvli-v1]
stages:
  - config: vsetvli-v2-lookup.yml
    heading: Cold TB-link lookup
```

## Experiment files

Start with a file in `experiments/`, copy it to `*.local.yml`, and fill in local
paths. The tracked examples contain no QEMU paths or machine-specific core IDs.

| Field | Meaning |
| --- | --- |
| `schema` | Required integer `1` |
| `name` | Experiment name stored with the results |
| `runner` | `packages`, `programs`, `vsetvli`, or `vsetivli` |
| `workloads` | Package IDs, guest-program IDs, or the matching micro suite ID |
| `reference` | QEMU label used for comparison, independent of mapping order |
| `qemu` | Mapping of labels to `path` and optional full `commit` SHA40 |
| `host.affinity` | Logical CPU ID list, `auto` for the first allowed CPU, or `null` to inherit the launching mask |
| `build` | Cross compiler, guest compiler selection, jobs and target flags |
| `guest.cpu` | QEMU guest CPU string, for runners exposing a CPU option |
| `measurement` | Warmups, repetitions, timeout, and micro calibration/count. The vsetvli runner also accepts a single `case` ID. |
| `storage` | Data/output paths, offline mode, optional local package source |
| `diagnostics` | Optional perf, coverage and host-GDB settings |

All paths inside YAML resolve from the experiment file's directory. `~` is
expanded; environment-variable interpolation is not performed. For example,
`storage.data_dir: ../data` in a file under `experiments/` selects the repository's
`data/` directory. Command-line paths resolve from the working directory.

Command-line scalar values override the experiment, which overrides runner
defaults. Positional package or guest-program IDs override the corresponding workload list.
Repeated `--qemu LABEL=PATH` and `--qemu-commit LABEL=SHA40` update the corresponding
label. Use `--reference LABEL` to override the configured reference. A real run
requires a path for every configured QEMU label; a preview can retain `null`.

Examples of runner-specific options:

```yaml
# Package compiler and target program flags
build:
  compiler: rvv
  cross_cc: riscv64-linux-gnu-gcc
  jobs: 1
  march: rv64gcv
  cflags: ""

# Fixed-count micro run instead of automatic calibration
measurement:
  warmups: 1
  repetitions: 10
  iterations: 100000000
  timeout: 1800

# Separate diagnostic executions supported by the vsetvli runner
diagnostics:
  perf: true
  lookup_counts: true
```

`build` also accepts `bootstrap_jobs`, `guest_cc1`, `guest_sysroot`,
`bootstrap_lock`, and `toolchain_lock`. `storage` accepts `data_dir`, `output`,
`offline`, and `source_dir`. Diagnostics can include `profile_vset`,
`profile_qemu`, `plugin_include`, and `diagnose_only` where supported by the
selected runner. Unsupported runner options are rejected by its argument parser.
Unknown configuration keys and duplicate YAML mapping keys are rejected.

Use `python3 run.py --help`, `python3 vsetvli.py --help`, or
`python3 microbench.py --help` for the corresponding runner's options.

## Host affinity

`--list-cpus` reads host topology and the inherited allowed CPU set. At runtime,
`host.affinity: [2, 3, 6]` or `--affinity 2-3,6` selects exactly that logical CPU
set. A requested CPU outside the inherited set is rejected. The harness applies
the mask and reads it back; a partial application is an error. It records
requested, inherited, and effective values. With `null`, it records the
inherited mask without changing it.

`host.affinity: auto` or `--affinity auto` selects the lowest logical CPU ID
in the launching process's allowed set. The preview shows that choice as
the requested mask. A run applies and records it through the same affinity
path as an explicit CPU list.

Package preparation/bootstrap happens before package measurement affinity is
applied. Configure, probes, warmups, measured package builds, and their child
processes then inherit the selected mask. Micro runners apply it before guest
compilation and execution. The harness does not reserve cores, disable SMT,
change frequency policy, or disable boost; it records these host settings.

## Preview and results

```sh
./run.sh --config experiments/packages.example.yml --dry-run
./run.sh --config experiments/vsetvli.example.yml --affinity 2 --dry-run
```

Preview performs configuration parsing and read-only host inspection. It does
not invoke QEMU, build tools, downloads, affinity setters, or output-directory
creation. It does not check that declared QEMU binaries exist or establish
their hashes/versions. The preview's `effective` affinity is `null`.

At execution, `manifest.json` records experiment/workload documents, their
SHA-256 values, merged arguments, the selected reference, verified QEMU binary
identities, and the effective affinity. The package manifest also includes
source/compiler/toolchain fingerprints. Existing per-run `metadata.json`,
`results.json`, CSV, input snapshots, and logs provide runtime details.

The SHA of a supplied commit is an identity declaration, not proof of how an
external binary was built. A sibling QEMU `metadata.json` is checked for its
binary SHA-256 and for conflicts with an explicitly supplied commit.
