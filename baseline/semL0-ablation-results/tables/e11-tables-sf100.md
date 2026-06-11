# E11 SemL0 Ablation Tables (sf100, date_tag=20260608)

## P1 — Read-amplification comparison (core query workload; counters are deterministic, single pass)

| Variant | L0 signal | cand. L0 segs | read bytes | vs baseline | header reads | body reads | avg us | p99 us | mismatches |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Schema-only | src_label | 236,667 | 9,374,272 | 100.0% | 4.00 | 20,915 | 6,355 | 38,428 | 0 |
| Naive L0 scan | unstructured L0 scan | 1,967,530 | 26,359,973,584 | 0.0% | 1,703 | 125,407 | 40,773 | 573,111 | N/A |
| LSMGraph-style LSM-CSR | key/range only | 1,967,530 | 26,359,973,584 | 0.0% | 1,703 | 125,407 | 40,933 | 573,111 | N/A |
| Label-only | src_label only | 1,402,094 | 26,248,259,392 | 0.4% | 2,639 | 125,407 | 38,845 | 573,889 | N/A |
| Edge-type-only | edge_type only | 235,457 | 6,223,136 | 100.0% | 0.00 | 20,909 | 12,947 | 50,289 | N/A |
| Degree-only / degree-aware | degree_class only | 2,340,919 | 26,347,781,976 | 0.0% | 2,960 | 125,005 | 41,006 | 578,667 | N/A |
| Full semantic (upper bound) | label+edge_type+degree | 308,679 | 10,124,569,608 | 61.6% | 1,178 | 20,909 | 23,808 | 67,917 | N/A |
| Budgeted semantic / SemL0 | benefit-scored subset | 312,069 | 4,530,279,696 | 82.8% | 848 | 20,915 | 11,848 | 51,583 | N/A |
| Full L0->L1 compact | L0 eliminated | N/A | N/A | 100.0% | N/A | N/A | N/A | N/A | N/A |

_'vs baseline' = read-byte reduction relative to the naive/schema baseline._

## P2 — Write / maintenance cost

| Variant | import s | throughput e/s | store bytes | L0 files | L0 bytes | manifest bytes | max RSS kb |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Schema-only | 4821.0 | 740,711 | 141,242,442,584 | 3444 | 141,240,078,392 | 2364030 | 219415968 |
| Naive L0 scan | 3789.0 | 942,457 | 140,589,867,058 | 1703 | 140,588,695,920 | 1170976 | 2396580 |
| LSMGraph-style LSM-CSR | 3785.0 | 943,453 | 140,589,867,058 | 1703 | 140,588,695,920 | 1170976 | 2401248 |
| Label-only | 3857.0 | 925,841 | 140,591,160,804 | 3293 | 140,588,899,440 | 2261202 | 2410248 |
| Edge-type-only | 4683.0 | 762,539 | 141,242,480,585 | 3446 | 141,240,112,104 | 2368319 | 217094128 |
| Degree-only / degree-aware | 3830.0 | 932,368 | 140,591,943,160 | 4269 | 140,589,024,368 | 2918630 | 2542852 |
| Full semantic (upper bound) | 4817.0 | 741,326 | 141,245,036,056 | 6615 | 141,240,517,736 | 4518158 | 215394188 |
| Budgeted semantic / SemL0 | 4678.0 | 763,354 | 141,244,439,671 | 5570 | 141,240,350,520 | 3811482 | 216896780 |
| Full L0->L1 compact | 5701.0 | 0 | N/A | N/A | N/A | N/A | 472694124 |
