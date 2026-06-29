# 02 背景与问题 中文版

LSM graph layout：写入进入内存，然后 flush 成 immutable CSR-like L0 segments；后台 compaction 向下合并。L0 segments 重叠导致读路径探测多个 segment。Blind compaction 可能减少 segment 数，但生成更 coarse 的 mixed segments。

Graph query signature 包含 source label、edge type、direction、degree class、destination label、property presence/value、snapshot、schema epoch。

安全契约：

> 只有 metadata 证明 disjointness 或 absence 时才剪枝；否则 conservative read。

这条契约贯穿 C1、C2、C3。

