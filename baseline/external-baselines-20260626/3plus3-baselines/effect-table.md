# 3+3 Baseline Effect Table

Generated at: 2026-06-30T02:49:33+08:00

## Numeric external rows that passed the gate

| System | Scale | Scope | Ops | Avg us | P50 us | P90 us | P99 us | Load s | Peak RSS KB | Disk bytes | Claim level |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| LiveGraph | SF1 | all-types-summary | 16,140 | 41.293 | - | - | - | 21.155 | 4,850,512 | 5,368,709,120 | scope-limited measured external baseline |
| LiveGraph | SF1 | positive-edge-types-summary | 8,070 | 1.364 | - | - | - | 21.155 | 4,850,512 | 5,368,709,120 | scope-limited measured external baseline |
| LiveGraph | SF1 | negative-edge-types-summary | 8,070 | 81.223 | - | - | - | 21.155 | 4,850,512 | 5,368,709,120 | scope-limited measured external baseline |
| LiveGraph | SF10 | all-types-summary | 32,140 | 391.063 | - | - | - | 1,457.36 | 47,908,544 | 38,654,705,664 | scope-limited measured external baseline |
| LiveGraph | SF10 | positive-edge-types-summary | 16,070 | 2.089 | - | - | - | 1,457.36 | 47,908,544 | 38,654,705,664 | scope-limited measured external baseline |
| LiveGraph | SF10 | negative-edge-types-summary | 16,070 | 780.036 | - | - | - | 1,457.36 | 47,908,544 | 38,654,705,664 | scope-limited measured external baseline |
| Aster RocksGraph | SF1 | all-types-summary | 1,700 | 150.985 | 10.680 | 381.904 | 3,411.36 | 74.399 | 1,851,684 | 1,054,936,788 | measured Aster/RocksGraph typed-neighbor bridge baseline |
| Aster RocksGraph | SF1 | positive-edge-types-summary | 850 | 8.648 | 8.890 | 12.321 | 22.621 | 74.399 | 1,851,684 | 1,054,936,788 | measured Aster/RocksGraph typed-neighbor bridge baseline |
| Aster RocksGraph | SF1 | negative-edge-types-summary | 850 | 293.322 | 15.533 | 602.615 | 3,618.67 | 74.399 | 1,851,684 | 1,054,936,788 | measured Aster/RocksGraph typed-neighbor bridge baseline |
| Aster RocksGraph | SF10 | all-types-summary | 1,700 | 1,408.27 | 580.529 | 1,061.24 | 15,809.60 | 794.262 | 15,621,088 | 10,401,409,041 | measured Aster/RocksGraph typed-neighbor bridge baseline |
| Aster RocksGraph | SF10 | positive-edge-types-summary | 850 | 564.505 | 558.747 | 917.970 | 1,123.39 | 794.262 | 15,621,088 | 10,401,409,041 | measured Aster/RocksGraph typed-neighbor bridge baseline |
| Aster RocksGraph | SF10 | negative-edge-types-summary | 850 | 2,252.04 | 710.325 | 1,782.86 | 65,524.10 | 794.262 | 15,621,088 | 10,401,409,041 | measured Aster/RocksGraph typed-neighbor bridge baseline |
| Neo4j Community | SF10 | all-types-summary | 1,700 | 1,974,806.09 | 3,442.78 | 70,662.56 | 34,522,340.13 | 216.845 | - | 16,664,449,041 | measured graph DB baseline; SF10 main |
| Neo4j Community | SF10 | positive-edge-types-summary | 850 | 6,444.49 | 1,459.89 | 15,364.28 | 70,333.28 | 216.845 | - | 16,664,449,041 | measured graph DB baseline; SF10 main |
| Neo4j Community | SF10 | negative-edge-types-summary | 850 | 3,943,167.68 | 8,292.58 | 307,835.19 | 166,537,341.71 | 216.845 | - | 16,664,449,041 | measured graph DB baseline; SF10 main |
| Neo4j Community | SF1 | all-types-summary | 1,700 | 180,922.68 | 2,899.28 | 43,831.28 | 3,458,485.92 | 34.628 | - | 2,174,091,281 | measured graph DB baseline; SF1 smoke |
| Neo4j Community | SF1 | positive-edge-types-summary | 850 | 4,296.64 | 1,464.00 | 7,972.26 | 49,406.34 | 34.628 | - | 2,174,091,281 | measured graph DB baseline; SF1 smoke |
| Neo4j Community | SF1 | negative-edge-types-summary | 850 | 357,548.72 | 6,208.12 | 132,812.86 | 16,584,154.52 | 34.628 | - | 2,174,091,281 | measured graph DB baseline; SF1 smoke |
| TuGraph | SF1 | all-types-summary | 1,700 | 420.925 | 6.358 | 151.398 | 17,602.60 | 269.804 | 1,618,668 | 1,595,001,088 | measured graph DB baseline; SF1 full |
| TuGraph | SF10 | all-types-summary | 1,700 | 3,775.45 | 12.630 | 633.490 | 158,870 | 3,939.95 | 14,697,508 | 14,960,539,904 | measured graph DB baseline; SF10 full |
| NebulaGraph | SF1 | all-types-summary | 1,700 | 61,553.97 | 581.010 | 10,736.32 | 1,189,334.15 | 194.644 | 1,673,516 | 4,552,617,537 | measured graph DB baseline; NebulaGraph nGQL edge-type model |
| NebulaGraph | SF1 | positive-edge-types-summary | 850 | 1,012.96 | 434.137 | 1,955.45 | 11,884.35 | 194.644 | 1,673,516 | 4,552,617,537 | measured graph DB baseline; NebulaGraph nGQL edge-type model |
| NebulaGraph | SF1 | negative-edge-types-summary | 850 | 122,094.98 | 1,063.56 | 37,996.81 | 5,773,921.39 | 194.644 | 1,673,516 | 4,552,617,537 | measured graph DB baseline; NebulaGraph nGQL edge-type model |
| NebulaGraph | SF10 | all-types-summary | 1,700 | 744,146.62 | 741.524 | 20,218.34 | 14,228,169.28 | 2,493.47 | 3,665,396 | 46,185,803,519 | measured graph DB baseline; NebulaGraph nGQL edge-type model |
| NebulaGraph | SF10 | positive-edge-types-summary | 850 | 1,753.35 | 480.324 | 3,450.35 | 20,629.99 | 2,493.47 | 3,665,396 | 46,185,803,519 | measured graph DB baseline; NebulaGraph nGQL edge-type model |
| NebulaGraph | SF10 | negative-edge-types-summary | 850 | 1,486,539.88 | 2,035.73 | 98,061.03 | 63,139,284.45 | 2,493.47 | 3,665,396 | 46,185,803,519 | measured graph DB baseline; NebulaGraph nGQL edge-type model |

## Internal/layout-style rows

| System | Scale | Scope | Ops | Avg us | P50 us | P90 us | P99 us | Disk bytes | Notes |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| LSMGraph-style | SF30 | core-s200 | 70,129 | 335.225 | 100 | 100 | 100 | 42,818,497,255 | body_reads=68891; candidate_l0_segments=602946; l0_files=519 |
| LSMGraph-style | SF30 | alltypes-s50 | 77,929 | 328.675 | 100 | 100 | 1,000 | 42,818,497,255 | body_reads=77016; candidate_l0_segments=426066; l0_files=519 |
| LSMGraph-style | SF100 | core-s200 | 129,485 | 556.741 | 100 | 100 | 10,000 | 140,589,867,058 | body_reads=125407; candidate_l0_segments=1967530; l0_files=1703 |

Rows not listed here have not passed the numeric-table gate yet. Keep them in qualitative comparison or artifact-attempt text.
