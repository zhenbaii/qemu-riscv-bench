# Provenance

The harness is assembled from the independently developed benchmark scripts in
the QEMU experiment workspace, with AI-assisted development and adaptation.
The package runner and `vsetivli` loop originated in the experiments of
2026-11-03/04. The eight `vsetvli` fixtures and host-GDB counter originated in
the local reviewer-followup experiment of 2026-11-06. The standalone entry
points, portability changes, documentation, and additional checks were prepared
on 2026-11-07.

`results/20261106-vsetvli-v1-v2/` contains a documented export of historical
measurements. Machine-local paths were omitted from exported sample rows;
times, iteration counts, sample order, hashes, and revision identities were
preserved. Original source-file SHA-256 values are recorded in the export.
Reorganizing the harness does not turn these into fresh measurements.

The benchmark infrastructure and original fixtures are offered under the MIT
license in `LICENSE`. Downloaded GCC, Debian packages, LZ4, zlib, SQLite, and
zlib-ng remain separate upstream works under their respective licenses. Their
source URLs, fixed release versions, and checksums are recorded by the harness.
The optional counter plugin uses the public `qemu-plugin.h` API supplied by the
user's QEMU build.

The recorded QEMU v2 revision is an AI-assisted local research implementation,
not an upstream contribution or an upstream commit. This repository's results
do not attest to upstream acceptance or a developer certificate of origin for
that implementation.
