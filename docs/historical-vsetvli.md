# Historical vsetvli v1/v2 experiment, 2026-11-06

This archive compares two local QEMU revisions with the same official base:

| Identity | Commit |
| --- | --- |
| Official base | `a3565937b39779d4e3704298095f566e7559ad53` |
| v1 | `77a8f2db05602891f59e623995639326bba51f60` |
| v2, local reviewer-followup implementation | `d6cb1c3fb712338bf769ac8cd26b471358db2d15` |

The benchmark repository does not contain these QEMU objects. They identify
the archived experiment; supply your own before/after binaries to run the
workloads. The v2 implementation was AI-assisted local research, not an upstream
contribution.

The review discussion is available in
[Chao Liu's reply](https://lore.kernel.org/qemu-devel/20261105074115.99275-1-chao.liu@processmission.com/).
The v1/v2 comparison isolates the single-exit specialization for `rs1=x0`
with nonzero `rd`, and immediate vtype configurations with `maxsz < 8`.
It does not compare the complete direct-chaining change against upstream.

## Focused loops

Host: x86-64 Linux, affinity to logical CPU 7; sibling 17 remained online.
Governor was `powersave`, EPP `balance_performance`, boost enabled. These
settings were recorded, not changed. Guest CPU was
`rv64,v=true,vlen=128,elen=64`. Each variant had one warmup followed by nine
alternating AB/BA pairs; every sample was retained. Instrumented executions
were separate. The historical calibrator chose a shared count from a short
calibration and capped it at 500 million iterations; it did not guarantee a
minimum duration for every final sample. The reorganized harness instead
rechecks the duration during calibration.

| Case | v1 median (s) | v2 median (s) | Wall reduction | v2 faster pairs |
| --- | ---: | ---: | ---: | ---: |
| case5-max | 0.605564 | 0.606986 | -0.235% | 3/9 |
| case3-small | 1.030812 | 1.003532 | +2.646% | 9/9 |
| case4-small | 1.019240 | 0.986673 | +3.195% | 9/9 |
| case3-boundary8 | 0.917288 | 0.916174 | +0.122% | 7/9 |
| case3-large | 0.918636 | 0.918010 | +0.068% | 5/9 |
| case1-register | 1.095911 | 1.095953 | -0.004% | 3/9 |
| case2-x0-x0 | 1.107342 | 1.106529 | +0.073% | 6/9 |
| scalar-control | 0.294151 | 0.294580 | -0.146% | 4/9 |

The 4-byte cases had a consistent reduction in this session. The max-AVL case
had smaller generated code without a repeatable wall-time improvement. The
unaffected cases and scalar control help characterize the comparison.

The native host-GDB diagnostic used 37 iterations and counted only entries at
the selected vset instruction's successor PC:

| Case | helper entries v1→v2 | TB-state extractions v1→v2 | dispatcher entries v1→v2 |
| --- | ---: | ---: | ---: |
| case5-max | 0→0 | 1→1 | 1→1 |
| case3-small / case4-small, each | 0→0 | 2→1 | 2→1 |
| case3-boundary8 / case3-large, each | 0→0 | 2→2 | 2→2 |
| case1-register / case2-x0-x0, each | 37→37 | 38→38 | 1→1 |

The small-capacity specialization saved a cold direct-link establishment. Both
versions already had zero repeated helper entries on the optimized paths.

## zlib build

zlib 1.3.1 was built with a static RVV GCC 14.2.0 `cc1` under each QEMU, using
host make/driver/binutils, `-O2 -g0 -march=rv64gcv -mabi=lp64d`, `-j1`, and CPU 7.
There was one excluded warmup for each QEMU and three measured AB/BA/AB pairs.
All measured artifacts were byte-identical.

| Pair | v1 wall (s) | v2 wall (s) |
| --- | ---: | ---: |
| 1 | 70.768021 | 69.966888 |
| 2 | 70.012825 | 69.665906 |
| 3 | 69.917415 | 69.862395 |

Medians were 70.012825 and 69.862395 seconds, a 0.215% reduction. Three pairs
give limited resolution for such a small difference; this does not establish
a stable application-level gain.

## Archived files

In [`results/20261106-vsetvli-v1-v2/`](../results/20261106-vsetvli-v1-v2/):

- `micro.json` preserves all 144 measured loop samples, calibration/warmup
  records, separate perf observations, and original QEMU hashes.
- `zlib.json` preserves all eight builds, including the two excluded warmups.
- `package-metadata.json` records the compiler identity, source/toolchain
  fingerprints, arguments, and host settings of the package run.
- `inputs.json`, `host-code.json`, and `mechanism.json` record original input
  identities, generated-code sizes, and diagnostic counts.
- `protocol.json`, `revisions.json`, and `source-hashes.json` identify the
  protocol, QEMU revisions, and original local JSON files used for this export.

Machine-local paths are represented as `<workspace>/...`; the hostname is
omitted. Numeric samples and hashes are unchanged. The original full build
artifacts, compiler-call logs, and debug traces remain in the experiment
workspace and are not part of this compact JSON export. The exported source
hashes refer to those original files, before path normalization.
