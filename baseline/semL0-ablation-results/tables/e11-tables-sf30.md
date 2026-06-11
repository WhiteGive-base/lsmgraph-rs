# E11 SemL0 Ablation Tables (sf30, date_tag=20260608)

## P1 — Read-amplification comparison (core query workload, mean of 3 repeats)

| Variant | L0 signal | cand. L0 segs | read bytes | vs baseline | header reads | body reads | avg us | p99 us | mismatches |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Schema-only | src_label | 75,649 | 13,142,544 | 99.8% | 7.00 | 12,439 | 2,184 | 28,689 | 0 |
| Naive L0 scan | unstructured L0 scan | 602,946 | 8,038,962,160 | 0.0% | 519 | 68,891 | 13,640 | 18,222 | N/A |
| LSMGraph-style LSM-CSR | key/range only | 602,946 | 8,038,962,160 | 0.0% | 519 | 68,891 | 13,329 | 17,056 | N/A |
| Label-only | src_label only | 429,753 | 8,003,710,184 | 0.4% | 809 | 68,891 | 12,732 | 34,722 | N/A |
| Edge-type-only | edge_type only | 72,413 | 4,696,320 | 99.9% | 0.00 | 11,332 | 3,574 | 11,989 | N/A |
| Degree-only / degree-aware | degree_class only | 708,853 | 8,034,605,224 | 0.1% | 903 | 68,429 | 13,295 | 18,167 | N/A |
| Full semantic (upper bound) | label+edge_type+degree | 94,183 | 4,696,320 | 99.9% | 0.00 | 11,332 | 5,704 | 28,672 | 0 |
| Budgeted semantic / SemL0 | benefit-scored subset | 98,027 | 13,142,544 | 99.8% | 7.00 | 12,439 | 6,702 | 30,883 | 0 |
| Full L0->L1 compact | L0 eliminated | 1,196 | 38,568,432 | 99.5% | 1.00 | 1,805 | 93.78 | 278 | N/A |

_'vs baseline' = read-byte reduction relative to the naive/schema baseline._

## P2 — Write / maintenance cost

| Variant | import s | throughput e/s | store bytes | L0 files | L0 bytes | manifest bytes | max RSS kb |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Schema-only | 1441.73 | 754,544 | 42,994,664,727 | 1076 | 42,993,928,408 | 736157 | 54858952 |
| Naive L0 scan | 1194.17 | 910,966 | 42,818,497,255 | 519 | 42,818,141,232 | 355861 | 2436484 |
| LSMGraph-style LSM-CSR | 1170.55 | 929,348 | 42,818,497,255 | 519 | 42,818,141,232 | 355861 | 2435524 |
| Label-only | 1164.2 | 934,417 | 42,818,902,803 | 1019 | 42,818,205,232 | 697409 | 2467344 |
| Edge-type-only | 1404.04 | 774,799 | 42,994,700,749 | 1078 | 42,993,962,120 | 738467 | 54168148 |
| Degree-only / degree-aware | 1192.76 | 912,043 | 42,819,137,053 | 1312 | 42,818,242,736 | 894155 | 2526776 |
| Full semantic (upper bound) | 1373.47 | 792,044 | 42,995,492,789 | 2062 | 42,994,088,072 | 1404555 | 56118188 |
| Budgeted semantic / SemL0 | 1390.73 | 782,214 | 42,995,280,813 | 1732 | 42,994,012,376 | 1181944 | 54158872 |
| Full L0->L1 compact | 1961.04 | 554,730 | 79,693,512,242 | 519 | 42,818,141,232 | 374056 | 167250336 |
