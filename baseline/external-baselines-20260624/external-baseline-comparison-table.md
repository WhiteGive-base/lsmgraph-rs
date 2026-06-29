# External Baseline Comparison Table（2026-06-26）

| System | Workload fit | Completed evidence | SF10 load/RSS/footprint | SF10 scan summary | Correctness | Claim level |
|---|---|---|---|---|---|---|
| LiveGraph | typed-neighbor adjacency scan can be expressed | SF1 smoke + SF10 scan on same LDBC edge set; external drivers clean-rebuilt; digest verifier added | load 1,457.36 s; peak RSS 47,908,544 KB; footprint 38,654,705,664 bytes | positive edge types ops-weighted avg 2.089 us over 16,070 sampled scans; all-types avg 391.063 us | edge-count gate PASS; digest PASS: SF10 checked 32,140 / mismatches 0 | measured SF10 external baseline; scope-limited |
| Teseo | dynamic graph container, not yet mapped to this property-graph typed workload | none in this run | - | - | - | qualitative only |
| GraphOne | dynamic graph store/analytics, workload mapping uncertain | none in this run | - | - | - | qualitative only |
| LLAMA | analytics-oriented snapshot graph, weak fit for typed-neighbor property graph reads | none in this run | - | - | - | qualitative only |

Use this as a scope-limited external baseline table. Do not turn it into a broad claim that SemL0 beats LiveGraph as a complete graph database.
