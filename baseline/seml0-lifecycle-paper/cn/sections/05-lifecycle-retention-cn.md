# 05 C2：Lifecycle Retention 中文版

C2 是承重贡献：semantic pruning surface 不只是 L0 artifact，而是必须跨 LSM lifecycle 维护的状态。

## 为什么 naive compaction 会毁掉 surface

Pruning power 依赖 `src_label` 和 `edge_type_partition` 是否 concrete。Naive merge 跨多个 label/edge type 合并时，输出 segment 会记录 `UNKNOWN_SOURCE_LABEL` / `MIXED_EDGE_TYPE`，读路径无法再按 label/edge type 证明 disjoint，于是 selective query 变成 full-segment read。

## Semantic merge

`LevelMergePolicy.semantic_partition_outputs=true` 时，merge 按 `(src_label, edge_type)` 分组输出 segments。Writer 为每个 output 重新推导 metadata，因此能保留或重建 exact surface。

## Controlled synthetic 结果

| scale | policy | retention | read after vs before | write_amp |
|---|---|---:|---|---:|
| SF1 | naive | 0.0 | 4x blow-up | 1.22 |
| SF1 | semantic | 1.0 | flat | 1.87 |
| SF10c | naive | 0.0 | 6x blow-up | 1.14 |
| SF10c | semantic | 1.0 | flat | 1.85 |

## 真实 SF30 结果

真实 LDBC SF30：1.09B directed edges，40 `(src_label, edge_type)` partitions，528 exact L1 segments。

| policy | retention | mixed_after | output_segs | write_amp |
|---|---:|---:|---:|---:|
| naive | 0.0 | 1.0 | 503 | 1.07 |
| semantic | 1.0 | 0.0 | 528 | 1.24 |

## 真实 SF30 metadata read-amp proxy

新增 proxy 不是完整 end-to-end read workload，而是在真实 SF30 post-merge segment metadata 上 replay 40 个 typed-neighbor partitions：

| policy | avg candidate segs/query | candidate bytes total | weighted read-amp proxy |
|---|---:|---:|---:|
| naive | 93.6 | 282.16 GB | 6.52x |
| semantic | 13.2 | 43.25 GB | 1.00x |

这说明 naive mixed outputs 在真实 SF30 segment distribution 上确实带来 read-amp 后果；semantic merge 把 candidate bytes 保持在 exact partition bytes。

## 边界

Controlled rows 测完整 read workload + correctness；真实 SF30 测 surface + write cost + metadata-level candidate replay。不要把 proxy 写成完整 SF30 body-read workload，也不要写 negligible overhead、optimal scheduler、production scheduler。
