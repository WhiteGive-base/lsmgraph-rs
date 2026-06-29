# 相关系统设计矩阵

本表用于 CIDR 版 qualitative comparison。除非某系统通过 reproducibility gate，否则不进入主数值表。

| 系统 | 主要目标 | 存储布局 | 更新模型 | 是否使用 query semantics 控制存储 | 是否关注 physical rewrite/compaction 后语义保持 | schema/snapshot 相关性 | SemL0 对位 |
|---|---|---|---|---|---|---|---|
| SemL0 | LSM 动态属性图的 query-semantic control plane | LSM-style CSR segments + semantic metadata | flush + compaction + snapshot delta | 是，query signature 下沉为 storage control signal | 是，C2 证明 naive merge 会摧毁 surface，semantic merge 保留/重建 | 是，epoch-aware + conservative fallback | 本文系统 |
| LiveGraph | Transactional graph storage, fast sequential adjacency scans | Transactional Edge Log + adjacency scans | transactional updates | 否，重点是 scan layout 和事务 | 不以 semantic surface retention 为目标 | MVCC/transaction 相关 | 强设计对照，typed-neighbor scan candidate |
| Teseo | Dynamic structural graph analysis | sparse arrays + fat tree | transactional dynamic updates | 否 | 不以 LSM compaction semantic retention 为目标 | transaction support | qualitative baseline |
| GraphOne | Real-time analytics on evolving graphs | edge log + adjacency store, dual versioning | ingestion + graph views | 否 | 不以 query-semantic pruning surface 为目标 | historical/stream/static views | qualitative baseline |
| LLAMA | Multi-versioned graph analytics | large multiversioned CSR arrays | snapshot/delta merge | 否 | merge/versioning 相关，但非 query-semantic | multi-version arrays | weak analytics sanity |
| Aspen | Low-latency graph streaming | compressed purely-functional trees | streaming updates | 否 | 非 LSM compaction target | versioned functional structure | design-space comparison |
| LSMGraph | Dynamic graph storage with multi-level CSR | LSM + multi-level CSR | dynamic graph updates | 未知/非主目标 | 关注 LSM graph layout，但非 SemL0 semantic surface | storage-level | 最接近 LSM 图存储背景 |
| Aster | Scalable graph database over LSM structures | enhanced LSM structures | graph DB updates | 未知/非主目标 | 可能关注 LSM storage organization，但非 query signature control plane | graph DB related | 近邻 qualitative comparison |
| BACH | HGTAP layout transformation | adjacency list <-> CSR using LSM-trees | HTAP-oriented | 否/非主目标 | 关注 layout transformation，不是 semantic pruning retention | 不明确 | 近邻 qualitative comparison |
| LSM-Community | 利用社区结构的图存储 | community-aware graph storage | social graph updates | 使用 graph structure，不是 query signature | 非 SemL0 surface retention | 不明确 | 结构感知存储对比 |
| ByteGraph | 分布式工业图数据库 | distributed graph DB | production graph workload | 查询语言/系统层优化 | 非 LSM semantic control plane | production DB | 写法参考，不直接数值对比 |
| BG3 | ByteDance cost-effective I/O-efficient graph DB | graph DB storage | industry workload | 系统级优化 | 非 SemL0 control plane | production DB | 写法参考 |
| Galaxybase | native distributed graph database for HTAP | distributed native graph DB | HTAP | 系统级 query support | 非 SemL0 control plane | production DB | 写法参考 |
| GES | Huawei graph processing engine/service | graph engine/service | service workload | 系统级 query/analytics | 非 SemL0 control plane | service system | 写法参考 |
| Nebula Graph | open-source distributed graph database | distributed graph DB | graph DB workload | 查询语言层 | 非 SemL0 control plane | graph DB | 背景系统 |

## CIDR 版比较结论

可以写：

- 现有系统分别优化 transaction、dynamic analytics、multi-version CSR、LSM graph layout、distributed graph DB service。
- SemL0 的差异是把 property-graph query signatures 作为 storage-level control plane，并明确处理 compaction 后 semantic surface retention 和 schema/snapshot fallback。
- 许多系统可作为 design baselines，但并不天然支持同一 LDBC SNB typed-neighbor property-graph workload。

不要写：

- SemL0 全面优于这些系统。
- 这些系统都已经跑过。
- Teseo/GraphOne/LLAMA/Aster/NebulaGraph 是完成的数值 baseline。
