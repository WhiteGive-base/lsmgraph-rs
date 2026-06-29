# 08 Related Work 中文版

SemL0 相关工作分四类：

1. LSM / KV compaction tuning。
2. Query-driven physical design / adaptive indexing。
3. Graph storage and LSM-based graph stores。
4. Schema evolution / MVCC。

核心定位：

> SemL0 的控制信号是 property-graph query signature，维护对象是 segment-level semantic pruning surface，并且该 surface 跨 LSM lifecycle conservative-correct。

## External baseline

LiveGraph SF10 是真实外部系统证据：

- edges：355,185,382。
- vertices：29,987,835。
- load time：1,502.59s。
- peak RSS：約 45.7 GiB。
- on-disk：37.58 GB block + 1 GB WAL。
- positive core edge types scan weighted avg：2.742us。

边界：LiveGraph SF100 load 不可行（17-21 days），所以 external comparison 是 SF10-only。

RocksDB-style KV-LSM 是 internal style baseline，不是 official RocksDB。KV-style encoding 是 simulation，不能写成 external measured system。

TODO：TeX 阶段补精确 bibkeys，尤其 LSMGraph VLDB'24、BACH PVLDB'25、LSM tuning、adaptive indexing。

