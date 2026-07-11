# Priority Evaluation Supplement 20260709

This supplement closes the three highest-priority evaluation gaps with measured Linux-side artifacts.

## Inputs

- SF10 SemL0 matrix: `/data/WorkSpace/lsmgraph-rs-cidr-paper/baseline/w6-sf10-priority-20260709-summary.md`
- SF100 SemL0 matrix: `/data/WorkSpace/lsmgraph-rs-cidr-paper/baseline/sf100-matrix-20260613-cn.md`
- External baseline metrics: `/data/WorkSpace/lsmgraph-rs-cidr-paper/baseline/external-baselines-20260626/3plus3-baselines/metrics.tsv`

## 1. SF10 SemL0 Rows With Digest-Gated External Baselines

SemL0 rows are internal storage-layout rows from the W6 core typed-neighbor workload. External rows are scoped to their original digest-gated typed-neighbor workload. This is not a full DBMS benchmark.

| system | scale | scope | ops | avg_us | p50_us | p90_us | p99_us | load_s | peak_rss_kb | disk_bytes | correctness | claim_scope |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SemL0-schema | SF10 | W6 core typed-neighbor; 9 core edge types; 1000 samples/type; 3 repeats | 9,000 | 749.8 | 1,048 | 1,248 | 1,407 | import measured separately | see resource table | 13.3 GiB | PASS (9,000) | internal storage-layout row, not external DBMS |
| SemL0-budg-b64 | SF10 | W6 core typed-neighbor; 9 core edge types; 1000 samples/type; 3 repeats | 9,000 | 525.4 | 706 | 989 | 1,170 | import measured separately | see resource table | 13.3 GiB | PASS (9,000) | internal storage-layout row, not external DBMS |
| SemL0-semantic | SF10 | W6 core typed-neighbor; 9 core edge types; 1000 samples/type; 3 repeats | 9,000 | 509.1 | 702 | 744 | 1,170 | import measured separately | see resource table | 14.6 GiB | PASS (9,000) | internal storage-layout row, not external DBMS |
| SemL0-naive | SF10 | W6 core typed-neighbor; 9 core edge types; 1000 samples/type; 3 repeats | 9,000 | 4,177.3 | 5,713 | 7,426 | 7,833 | import measured separately | see resource table | 13.4 GiB | anchor | internal storage-layout row, not external DBMS |
| SemL0-kv-lsm | SF10 | W6 core typed-neighbor; 9 core edge types; 1000 samples/type; 3 repeats | 9,000 | 4,111.1 | 5,676 | 6,296 | 7,833 | import measured separately | see resource table | 13.4 GiB | PASS (9,000) | internal storage-layout row, not external DBMS |
| LiveGraph | SF10 | all-types-summary | 32140 | 391.062912 |  |  |  | 1457.36 | 47908544 | 38654705664 | PASS | scope-limited measured external baseline |
| LiveGraph | SF10 | positive-edge-types-summary | 16070 | 2.089441 |  |  |  | 1457.36 | 47908544 | 38654705664 | PASS | scope-limited measured external baseline |
| Aster RocksGraph | SF10 | all-types-summary | 1700 | 1408.27 | 580.529 | 1061.24 | 15809.6 | 794.261865 | 15621088 | 10401409041 | PASS | measured Aster/RocksGraph typed-neighbor bridge baseline |
| Aster RocksGraph | SF10 | positive-edge-types-summary | 850 | 564.505 | 558.747 | 917.97 | 1123.39 | 794.261865 | 15621088 | 10401409041 | PASS | measured Aster/RocksGraph typed-neighbor bridge baseline |
| Neo4j Community | SF10 | all-types-summary | 1700 | 1974806.0859911756 | 3442.775 | 70662.558 | 34522340.128 | 216.84547472000122 |  | 16664449041 | PASS | measured graph DB baseline; SF10 main |
| Neo4j Community | SF10 | positive-edge-types-summary | 850 | 6444.488203529413 | 1459.891 | 15364.28 | 70333.282 | 216.84547472000122 |  | 16664449041 | PASS | measured graph DB baseline; SF10 main |
| TuGraph | SF10 | all-types-summary | 1700 | 3775.45 | 12.63 | 633.49 | 158870 | 3939.95 | 14697508 | 14960539904 | PASS | measured graph DB baseline; SF10 full |
| NebulaGraph | SF10 | all-types-summary | 1700 | 744146.6177941178 | 741.524 | 20218.339 | 14228169.276 | 2493.4713282585144 | 3665396 | 46185803519 | PASS | measured graph DB baseline; NebulaGraph nGQL edge-type model |
| NebulaGraph | SF10 | positive-edge-types-summary | 850 | 1753.3543270588223 | 480.324 | 3450.354 | 20629.986 | 2493.4713282585144 | 3665396 | 46185803519 | PASS | measured graph DB baseline; NebulaGraph nGQL edge-type model |

## 2. SF10 Import Resource Overhead

Resource numbers are parsed from `/usr/bin/time -v` logs emitted by the W6 runner during import.

| variant | store_gib | l0_files | import_wall_s | user_cpu_s | sys_cpu_s | cpu_pct | max_rss_kb | fs_inputs | fs_outputs | exit_status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| schema | 13.3 | 376 | 199.49 | 331.82 | 51.04 | 191% | 2226668 | 12618512 | 27947592 | 0 |
| naive | 13.4 | 170 | 193.52 | 319.67 | 49.91 | 190% | 2330236 | 64 | 28125696 | 0 |
| kv-lsm | 13.4 | 170 | 198.85 | 326.96 | 59.66 | 194% | 2439460 | 96 | 28125688 | 0 |
| edge-type-only | 13.3 | 380 | 198.46 | 330.76 | 48.97 | 191% | 1877168 | 64 | 27949632 | 0 |
| semantic | 14.6 | 737 | 204.20 | 366.23 | 58.76 | 208% | 8783744 | 64 | 30705208 | 0 |
| budg-b64 | 13.3 | 444 | 195.51 | 330.64 | 47.16 | 193% | 2366196 | 16 | 27960736 | 0 |
| budg-b256 | 13.7 | 569 | 199.95 | 346.73 | 50.21 | 198% | 5435800 | 8 | 28790272 | 0 |
| budg-b1024 | 14.5 | 605 | 207.49 | 371.95 | 52.68 | 204% | 8610404 | 8 | 30383816 | 0 |
| oracle | 13.4 | 170 | 197.46 | 322.78 | 56.93 | 192% | 2375464 | 40 | 28125688 | 0 |

## 3. SF10 to SF100 Scale Trend

The trend uses the same W6 core typed-neighbor protocol. SF10 uses 1,000 sampled sources per core edge type; SF100 uses the frozen 5,000 sampled sources per core edge type.

| variant | scale | ops_per_repeat | candidate_l0 | read_bytes | avg_us | p99_us | store_gib | l0_files | candidate_per_op |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| schema | SF10 | 9,000 | 131,321 | 81,477,611 | 749.8 | 1,407 | 13.3 | 376 | 14.59 |
| budg-b64 | SF10 | 9,000 | 149,019 | 78,331,325 | 525.4 | 1,170 | 13.3 | 444 | 16.56 |
| semantic | SF10 | 9,000 | 158,358 | 78,098,749 | 509.1 | 1,170 | 14.6 | 737 | 17.60 |
| naive | SF10 | 9,000 | 1,001,798 | 338,940,880 | 4,177.3 | 7,833 | 13.4 | 170 | 111.31 |
| kv-lsm | SF10 | 9,000 | 1,001,798 | 338,940,880 | 4,111.1 | 7,833 | 13.4 | 170 | 111.31 |
| schema | SF100 | 45,000 | 5,929,197 | 859,764,709 | 9,816.8 | 37,333 | 133.9 | 3,444 | 131.76 |
| budg-b64 | SF100 | 45,000 | 6,022,507 | 859,490,557 | 8,722.1 | 30,981 | 133.9 | 3,483 | 133.83 |
| semantic | SF100 | 45,000 | 7,782,877 | 673,877,573 | 8,250.9 | 30,415 | 146.4 | 6,615 | 172.95 |
| naive | SF100 | 45,000 | 49,257,601 | 3,629,768,504 | 53,513.8 | 179,259 | 134.6 | 1,703 | 1094.61 |
| kv-lsm | SF100 | 45,000 | 49,257,601 | 3,629,768,504 | 53,892.0 | 178,889 | 134.6 | 1,703 | 1094.61 |

## Claim Boundary

- Use the external table as scope-limited typed-neighbor positioning, not a full graph database head-to-head.
- CPU/resource overhead is import-side `/usr/bin/time -v`; it is not a perf-counter CPU-cycle study.
- The scale table is a two-point SF10/SF100 trend for the W6 protocol. SF30 remains covered by separate dynamic/property workloads.
