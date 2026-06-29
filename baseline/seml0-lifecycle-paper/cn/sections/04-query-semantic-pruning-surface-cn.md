# 04 C1：Query-Semantic Pruning Surface 中文版

C1 讲如何生成和利用剪枝面。

## 关键机制

- `GraphAccessSignature`：storage-facing query signature，不是 logical plan。
- `CsrSegmentMeta`：记录 source/destination label、edge-type partition、degree class、property bitmap、tombstone flag、schema epoch 等。
- `SemanticSummaryCompleteness`：`Exact` / `Conservative` / `Unknown`。
- `signature_pruning_decision`：返回 `{ pruned, reason }`，用于审计和计量。

## Prune reasons

`time`、`src_label`、`edge_type`、`direction`、`degree`、`dst_label`、`property_absence`。

## Keep reasons

`mixed_unknown_fallback`、`budgeted_not_materialized`、`schema_tombstone_fallback`、`kept_candidate`。

## Evidence

- W6 SF100：`naive` candidate L0 mean 49,257,601；pruned variants 约低一个数量级；全部 `mismatches=0`。
- W8 SF30：property predicates 改善 candidate/body/read/elapsed；2-hop 改善 body/read/elapsed，但不写 universal candidate reduction。
- W14：correctness parity 成立，但 composite semantics 稳定优于 edge-type-only 未证明。

## Claim 边界

可写：query-semantic metadata 可安全降低 candidate/read amplification。  
不可写：always improves latency、composite semantics always beats edge-type-only。

