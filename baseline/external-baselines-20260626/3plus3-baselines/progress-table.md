# 3+3 Baseline Progress Table

Generated at: 2026-06-30T02:49:33+08:00

| Group | System | Status | SF1 | SF10 | Correctness | Raw artifact | Notes |
|---|---|---|---|---|---|---|---|
| problem-adjacent | LiveGraph | SF1/SF10 DONE | PASS | PASS | PASS | `baseline/external-baselines-20260624/livegraph` | SF10 typed-neighbor scan and query-count digest completed; scope-limited external baseline. |
| problem-adjacent | LSMGraph-style | EXTRACTED | N/A | N/A | PASS-INTERNAL | `baseline/semL0-ablation-results/traces/e11-*-lsmgraph_style-20260608` | Existing SF30/SF100 layout-style results extracted; not external official system. |
| problem-adjacent | Aster | SF10 DONE | PASS | PASS | PASS | `baseline/external-baselines-20260626/3plus3-baselines/systems/aster` | Aster/RocksGraph source available; typed-neighbor bridge uses compact logical vertex per (edge_type,src) to avoid sparse-id Morris counter blow-up. |
| open-source-graph-db | Neo4j Community | SF10 DONE | PASS | PASS | PASS | `baseline/external-baselines-20260626/3plus3-baselines/systems/neo4j/sf10-main` | Neo4j neo4j:5.26.24; samples_per_edge_type=50; import model=all dense edge types as distinct outgoing relationship types. |
| open-source-graph-db | TuGraph | SF10 FULL DONE | PASS | PASS | PASS | `baseline/external-baselines-20260626/3plus3-baselines/systems/tugraph/sf10-main-r1` | Full SF10 loaded 29987835 vertices and 355185382 edges; checked=1700, mismatches=0, avg_us=3775.45, p99_us=158870. |
| open-source-graph-db | NebulaGraph | SF10 DONE | PASS | PASS | PASS | `baseline/external-baselines-20260626/3plus3-baselines/systems/nebulagraph` | NebulaGraph server images available; nGQL edge-type model uses one edge type per dense edge type. |

Numeric-table gate: clean build/version, same LDBC edge set, same sampled typed-neighbor workload, per-query count/hash digest, load/RSS/footprint/latency logs, raw config, and DONE marker.
