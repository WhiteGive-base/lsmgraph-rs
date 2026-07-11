# Aster-Morris8 SF30 b64 experiment

This experiment compares the unmodified `3d09ecb` budget-64 import with an
experimental Aster-style Morris8 admission policy. Morris8 never becomes
correctness-critical metadata: an admitted source is written with its exact
run-length degree class, and a rejected source falls back to `Mixed`.

The report deliberately separates four quantities:

1. Morris counter update skips (`attempted - applied`).
2. Exact/Morris degree-class confusion near 16 and 1024.
3. Degree materialization decisions skipped by Morris.
4. Counterfactual query false skips if the estimated class were persisted
   directly. The implemented safe path is checked independently with result
   digests and must remain zero-mismatch.

The import uses the CIDR/W6 `MEMGRAPH_BYTES=67108864` setting. The SF30 query
workload is the fixed `sf30-core-s200.plan.json` plan over edge
types `1,2,3,7,8,9,10,11,12`, with one warmup and three measured rounds. Import
and query runs are serialized to avoid NVMe contention. Raw large stores and
benchmark JSON stay under `/data/WorkSpace/try-aster-artifacts`; this directory
contains the scripts and compact summaries needed to reproduce and audit them.

`raw_false_skip_edge_rate` counts L0 edge records from required exact segments.
For this insertion-only SNB import it is also the missed-result-edge proxy. It
must not be generalized to update/tombstone workloads without visible-version
reconciliation.

The concise Chinese result and run-length explanation are in
[`conclusion-cn.md`](conclusion-cn.md).
