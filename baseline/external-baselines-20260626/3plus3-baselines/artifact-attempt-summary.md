# Artifact Attempt Summary

Generated at: 2026-06-27T02:40:37+08:00

Current paper-safe interpretation:

- LiveGraph: SF1/SF10 DONE; scope-limited measured external baseline. SF10 typed-neighbor scan and query-count digest completed; scope-limited external baseline.
- LSMGraph-style: EXTRACTED; internal layout baseline, not official external artifact. Existing SF30/SF100 layout-style results extracted; not external official system.
- Aster: ARTIFACT NOT FOUND LOCALLY; artifact attempt / qualitative until workload bridge passes. No local Aster source checkout or Docker image found; 2026-06-29 public search found the paper but no official runnable GitHub/source/artifact entry. Keep as qualitative/artifact-needed row until source/artifact is available.
- Neo4j Community: SF10 DONE; candidate numeric baseline after typed-neighbor digest PASS. Neo4j neo4j:5.26.24; samples_per_edge_type=50; import model=all dense edge types as distinct outgoing relationship types.
- TuGraph: SF10 FULL DONE; candidate numeric baseline after typed-neighbor digest PASS. Full SF10 loaded 29987835 vertices and 355185382 edges; checked=1700, mismatches=0, avg_us=3775.45, p99_us=158870.
- NebulaGraph: SERVER IMAGES READY; candidate numeric baseline after typed-neighbor digest PASS. Official GitHub repos are reachable from remote. Docker Hub/direct access failed, but docker.1ms.run mirror worked for `vesoft/nebula-{graphd,metad,storaged}:v3.8.0`; images were pulled and retagged to official names on 2026-06-29. Next blocker is service/schema/loader/query/digest smoke.

Paper wording rule: LiveGraph can be cited as a measured scope-limited SF10 typed-neighbor external baseline. LSMGraph-style is an internal layout baseline. Neo4j, TuGraph, NebulaGraph, and Aster must stay as attempts/qualitative rows until their per-query digest passes.
