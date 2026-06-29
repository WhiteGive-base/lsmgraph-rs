# 01 引言 中文版

引言回答：为什么 LSM graph store 需要 query-semantic physical lifecycle management？

动态图需要高吞吐写入，LSM 适合写路径。但 L0 overlap 让单个 source vertex 的查询探测很多 segments。传统叙事把问题看作 adjacency locality，SemL0 认为它也是 semantics 问题：query 在读 body 前已经暴露 label、edge type、direction、degree、property、schema epoch。

SemL0 的 thesis：

> Property-graph query signature 应当作为 LSM graph store 的 lifecycle control signal。

Flush 创建 semantic pruning surface；read 用 exact proof 剪枝；feedback/compaction 保留或重建 surface；schema/snapshot 规则保证 conservative correctness。

边界：不声称完整图数据库、完整 schema migration engine、production write-stall study 或 end-to-end production report。

