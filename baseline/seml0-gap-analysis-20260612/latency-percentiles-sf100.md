# SF100 neighbor latency percentiles (merged across edge types)

Source: per-edge-type histogram buckets in *-bench.json (neighbor_metrics.storage.get_neighbors_latency). Buckets are coarse (16 bounds, src/metrics.rs); `*_ub` = conservative bucket upper bound, `*_interp` = linear interpolation inside the bucket. Internal analysis only — final paper numbers need finer buckets (C9). kv-style is a simulation; no real histogram exists.

| variant | source | ops | hist_count | avg_us | p50_ub_us | p50_interp_us | p90_ub_us | p90_interp_us | p99_ub_us | p99_interp_us | merge_mode |
|---|---|---|---|---|---|---|---|---|---|---|---|
| naive | measured | 45,000 | 45,000 | 5382.8 | 2000.0 | 1529.9 | 20000.0 | 13577.3 | 50000.0 | 42698.6 | sum-per-type |
| schema | measured | 45,000 | 45,000 | 4906.2 | 500.0 | 447.1 | 20000.0 | 13716.6 | 100000.0 | 77457.6 | sum-per-type |
| edge-type-only | measured | 45,000 | 45,000 | 4288.8 | 500.0 | 413.8 | 20000.0 | 13937.1 | 50000.0 | 48960.2 | sum-per-type |
| semantic | measured | 45,000 | 45,000 | 4731.1 | 500.0 | 365.4 | 20000.0 | 14313.5 | 100000.0 | 77202.1 | sum-per-type |
| budg-b64 | measured | 45,000 | 45,000 | 3658.3 | 250.0 | 245.2 | 20000.0 | 11990.9 | 50000.0 | 47506.5 | sum-per-type |
| budg-b256 | measured | 45,000 | 45,000 | 3681.1 | 500.0 | 268.2 | 20000.0 | 11968.0 | 50000.0 | 47313.0 | sum-per-type |
| budg-b1024 | measured | 45,000 | 45,000 | 4060.6 | 500.0 | 269.4 | 20000.0 | 12416.7 | 100000.0 | 50641.0 | sum-per-type |
| kv-style | simulated(model) | 45,000 | n/a | n/a | n/a | n/a | n/a | n/a | 250,000 | n/a | per-type-summary-only |
