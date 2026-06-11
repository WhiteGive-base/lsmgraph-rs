# Strong baseline (sf100) — run qslsm-sf100-strong-baseline-20260610

- input: `/data/WorkSpace/ldbc-sf100/social_network`  scale: **sf100**  samples: 5000
- directed edges: **3,570,968,680**  edge_types: 1,2,3,7,8,9,10,11,12
- budget sweep: 64 256 1024  (byte gate disabled; file budget is the cost axis)

## P1 — Read amplification (shared sample plan)

| Variant | read MiB | candidate L0 | body reads | header reads | avg us | L0 files | source |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| edge-type-only | 146.70 | 5,899,015 | 532,193 | 0 | 4288.8 | 3,446 | measured |
| schema | 149.71 | 5,929,197 | 532,227 | 4 | 4906.2 | 3,444 | measured |
| semantic | 9994.25 | 7,782,877 | 532,193 | 1,222 | 4731.1 | 6,615 | measured |
| budg-b64 | 146.70 | 6,022,519 | 532,193 | 0 | 3658.3 | 3,483 | measured |
| budg-b256 | 146.70 | 6,486,337 | 532,193 | 0 | 3681.1 | 3,675 | measured |
| budg-b1024 | 700.07 | 7,676,875 | 532,193 | 258 | 4060.6 | 4,451 | measured |
| kv-style | 167.79 | 45,000 | 4,807,169 | 45,000 | 4906.2 | N/A | measured |

## P2 — Maintenance cost

| Variant | store GiB | L0 files | import s | source |
| --- | ---: | ---: | ---: | --- |
| edge-type-only | N/A | 3,446 | 4079.7 | measured |
| schema | 131.5 | 3,444 | 4173.4 | measured |
| semantic | 131.5 | 6,615 | 4137.3 | measured |
| budg-b64 | 131.5 | 3,483 | 4106.1 | measured |
| budg-b256 | 131.5 | 3,675 | 4100.2 | measured |
| budg-b1024 | 131.5 | 4,451 | 4103.4 | measured |
| kv-style | N/A | N/A | N/A | measured |

## Budget sweep (SemL0 controllable cost)

schema = 149.71 MiB read; full-semantic = 9994.25 MiB read. Budgeted should interpolate monotonically and stay <= schema.

| budget (extra L0 files) | read MiB | L0 files | vs schema |
| --- | ---: | ---: | ---: |
| 64 | 146.70 | 3,483 | 2.0% less |
| 256 | 146.70 | 3,675 | 2.0% less |
| 1024 | 700.07 | 4,451 | -367.6% less |
