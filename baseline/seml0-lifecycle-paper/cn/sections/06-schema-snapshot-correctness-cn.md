# 06 C3：Schema / Snapshot Correctness 中文版

C3 证明 pruning 不会在 schema evolution、snapshot、tombstone 下漏读。

## 三个 invariant

1. **Segment Schema**：segment 记录自己的 `schema_epoch` 和 `property_encoding_epoch`。
2. **Epoch-Aware Resolution**：读路径按 segment 自己的 epoch 解释 metadata。
3. **Conservative Pruning**：只有 exact/conservative proof 才剪枝；mixed/unknown/tombstone/older epoch 都保留。

## No-false-negative theorem

如果 segment 包含 query 下可见的 matching edge，则 `signature_pruning_decision` 不会剪掉它。Schema/snapshot uncertainty 只能降低 precision，不能制造 false negative。

## Evidence

W13 十个 schema-evolution tests 通过，覆盖 epoch advance、old-segment readability、mixed schema + snapshot + tombstone deltas across compaction/reopen、alias/drop、encoding epoch、new edge label exact-vs-mixed pruning。

## 边界

不覆盖 arbitrary migration、online full-store rewrite、range/string/compound predicates、完整 SQL null semantics。

