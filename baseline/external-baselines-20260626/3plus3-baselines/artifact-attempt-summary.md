# Artifact Attempt Summary

Generated at: 2026-06-30T02:49:33+08:00

Current paper-safe interpretation:

- LiveGraph: SF1/SF10 DONE; scope-limited measured external baseline. SF10 typed-neighbor scan and query-count digest completed; scope-limited external baseline.
- LSMGraph-style: EXTRACTED; internal layout baseline, not official external artifact. Existing SF30/SF100 layout-style results extracted; not external official system.
- Aster: SF10 DONE; candidate numeric baseline after typed-neighbor digest PASS. Aster/RocksGraph source available; typed-neighbor bridge uses compact logical vertex per (edge_type,src) to avoid sparse-id Morris counter blow-up.
- Neo4j Community: SF10 DONE; candidate numeric baseline after typed-neighbor digest PASS. Neo4j neo4j:5.26.24; samples_per_edge_type=50; import model=all dense edge types as distinct outgoing relationship types.
- TuGraph: SF10 FULL DONE; candidate numeric baseline after typed-neighbor digest PASS. Full SF10 loaded 29987835 vertices and 355185382 edges; checked=1700, mismatches=0, avg_us=3775.45, p99_us=158870.
- NebulaGraph: SF10 DONE; candidate numeric baseline after typed-neighbor digest PASS. NebulaGraph server images available; nGQL edge-type model uses one edge type per dense edge type.

Paper wording rule: only systems whose rows pass the same-workload count/hash digest gate can be cited as measured numeric baselines. LSMGraph-style remains an internal layout baseline. Failed, skipped, or pending rows stay in artifact-attempt/qualitative text.
