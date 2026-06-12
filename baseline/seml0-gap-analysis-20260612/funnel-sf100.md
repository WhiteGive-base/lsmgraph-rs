# SF100 L0 pruning funnel (per-stage segment counts, summed over 9 edge types)

Stage order matches src/graph.rs:2081-2145: semantic-index candidates -> src-range filter -> SourceBloom -> filter_passed (body read issued) -> matched (non-empty). candidate = after semantic L0 index; range/bloom_filtered = pruned by that stage. kv-style is simulated.

| variant | source | ops | candidate | range_filtered | bloom_filtered | filter_passed | body_reads | matched | read_bytes | body_bytes |
|---|---|---|---|---|---|---|---|---|---|---|
| naive | measured | 45,000 | 49,257,601 | 0 | 46,070,376 | 3,188,799 | 3,122,166 | 3,122,166 | 27,318,428,688 | 1,000,730,528 |
| schema | measured | 45,000 | 5,929,197 | 0 | 5,380,648 | 548,552 | 532,227 | 532,227 | 156,981,696 | 153,835,168 |
| edge-type-only | measured | 45,000 | 5,899,015 | 0 | 5,350,611 | 548,404 | 532,193 | 532,193 | 153,829,408 | 153,829,408 |
| semantic | measured | 45,000 | 7,782,877 | 0 | 7,230,778 | 553,162 | 532,193 | 532,193 | 10,479,735,312 | 153,829,408 |
| budg-b64 | measured | 45,000 | 6,022,519 | 0 | 5,474,005 | 548,514 | 532,193 | 532,193 | 153,829,408 | 153,829,408 |
| budg-b256 | measured | 45,000 | 6,486,337 | 0 | 5,937,089 | 549,248 | 532,193 | 532,193 | 153,829,408 | 153,829,408 |
| budg-b1024 | measured | 45,000 | 7,676,875 | 0 | 7,125,616 | 551,437 | 532,193 | 532,193 | 734,072,656 | 153,829,408 |
| kv-style | simulated(model) | 45,000 | 45,000 | n/a | n/a | 45,000 | 4,807,169 | 45,000 | 175,938,084 | n/a |
