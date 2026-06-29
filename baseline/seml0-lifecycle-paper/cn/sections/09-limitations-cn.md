# 09 Limitations 中文版

必须保留的边界：

- **Latency 是 supporting signal。** 不声明 uniform speedup。
- **Composite vs single-axis semantics。** 不声明 composite partitioning 稳定优于 edge-type-only。
- **C2 retention 的三层证据要分清。** Synthetic rows 测完整 read workload + correctness；real SF30 测 retention + write cost；real SF30 metadata replay 测 candidate segments/bytes proxy。不要把 proxy 写成完整 body-read workload。
- **External comparison 是 SF10-only。** LiveGraph SF100 不可行；RocksDB-style/KV-style 不是外部系统。
- **Feedback 是 controlled evidence。** 不是 production trace。
- **Schema scope 是 additive + fixed-width。** 不覆盖 arbitrary migration、range/string/compound predicates。
- **无 production write-stall characterization。**
- **投稿工程未完成。** Venue、template、TeX/PDF、page budget、figures、bib citations 仍要做。
