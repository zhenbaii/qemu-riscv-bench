# Measurement protocol

1. Provide explicit QEMU binaries, commits, build configurations, and a fixed
   guest compiler. Use the same guest inputs and iteration count within each
   before/after comparison. Record changes of compiler, CPU model, VLEN, source
   version, or QEMU base as a new experiment.
2. Select a host CPU affinity if practical. The harness records available
   governor, frequency range, EPP, SMT siblings, boost, load, and power-supply
   information before and after the run. It does not alter frequency policy or
   reserve cores. Binding to a logical CPU does not exclude its SMT sibling.
3. Run correctness probes before measurements. Every vset loop execution is
   checked against a scalar/CSR/vector oracle. Package builds require emulated
   `cc1` invocations and validate their produced artifacts after timing.
4. For `vsetvli.py`, calibrate all QEMUs in a case to a common iteration count
   whose fastest calibration execution exceeds the requested minimum. Every
   measurement is a fresh process; startup and initial translation are included.
   A sufficiently long loop reduces their relative contribution.
5. Warm up each QEMU, then alternate AB/BA order for two variants. With more
   variants, rotate their order. Retain every sample. Odd pair counts are not
   perfectly balanced; choose an even count when exact order balance matters.
6. Report sample count, median, range, paired changes, and the scalar control.
   Wall reduction is `100 * (1 - candidate / reference)`; positive values mean
   less elapsed time. Avoid conclusions about small differences within observed
   variation or about application speedup from a focused loop alone.
7. Keep host-GDB, instruction tracing, and coverage runs out of performance
   statistics. `vsetvli.py --perf` also executes separately after all timings.
   The package runner's optional `--perf` measures an instrumented build; record
   that setting and compare like with like.
8. A helper entry, a TB-state extraction call, a dispatcher entry, and a hash
   table miss are different events. The GDB diagnostic reports the first three
   independently at the selected successor PC. One fewer cold-link lookup does
   not imply one fewer helper call on every later iteration.
9. Results are written to fresh directories. Runtime failures retain a failed
   status and return nonzero. Keep the full result directory and exact dependency
   archives/locks when sharing a measurement. Never relabel historical data as a
   measurement of a newer patch revision.
