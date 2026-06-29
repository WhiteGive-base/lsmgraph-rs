# Table C2 中文版：Lifecycle Retention

本表应按三层理解：

## Layer A：controlled read-amplification

Synthetic rows 固定 partition structure，只改变 merge policy，完整测 read workload 与 correctness。

| scale | policy | retention | read after vs before | output segs | write_amp | mismatches |
|---|---|---:|---|---:|---:|---:|
| SF1 | naive | 0.0 | 4x blow-up | 1 | 1.22 | 0 |
| SF1 | semantic | 1.0 | flat | 4 | 1.87 | 0 |
| SF10c | naive | 0.0 | 6x blow-up | 1 | 1.14 | 0 |
| SF10c | semantic | 1.0 | flat | 6 | 1.85 | 0 |

## Layer B：real LDBC retention + write cost

真实数据测 surface + write cost。

| scale | policy | retention | exact_surface after | output segs | write_amp |
|---|---|---:|---:|---:|---:|
| SF1 real | naive | 0.0 | 0.0 | 17 from 44 | 1.08 |
| SF1 real | semantic | 1.0 | 1.0 | 44 from 44 | 1.27 |
| SF30 real | naive | 0.0 | 0.0 | 503 from 528 | 1.07 |
| SF30 real | semantic | 1.0 | 1.0 | 528 from 528 | 1.24 |

结论：semantic merge 在真实 1.09B-edge SF30 上保留 surface；naive merge 摧毁 surface。Cost bounded：write_amp 1.24 vs 1.07。

## Layer C：real SF30 metadata read-amp proxy

这不是完整 end-to-end read workload；它在真实 SF30 post-merge segment metadata 上 replay 40 个 typed-neighbor `(src_label, edge_type)` partitions，统计 candidate segments/bytes。

| policy | query partitions | avg candidate segs/query | candidate bytes total | exact bytes before | weighted read-amp proxy |
|---|---:|---:|---:|---:|---:|
| naive | 40 | 93.6 | 282.16 GB | 43.25 GB | 6.52x |
| semantic | 40 | 13.2 | 43.25 GB | 43.25 GB | 1.00x |

结论：真实 SF30 上，naive mixed outputs 会让 typed partitions 保守读取大量无关 segments；semantic 把 candidate bytes 保持在 exact partition bytes。
