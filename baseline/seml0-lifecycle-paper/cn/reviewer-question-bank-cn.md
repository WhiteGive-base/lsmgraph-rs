# Reviewer Question Bank 中文版

## Q1. Read bytes 降了，但 latency 不总是提升，有什么意义？

SemL0 的主目标是安全地降低 read amplification 并保留 semantic pruning surface，而不是声明 uniform latency speedup。Latency 受 metadata lookup、cache state、async scheduling、body decode 等影响。因此论文把 latency 作为 supporting evidence。真正稳健的收益是：candidate segments 更少、read bytes 更低、compaction 后 surface 能保留，并且 write cost 有界。

## Q2. 和 compaction tuning / learned LSM 有什么不同？

LSM tuning 通常调 size ratios、triggers、bloom bits，信号是 size/frequency，记录是 opaque KV。SemL0 的控制信号是 property-graph query signature，维护对象是 segment-level semantic pruning surface，并要求 exact-proof safety。两者正交，可组合。

## Q3. Semantic merge 会不会导致 write amplification 或 file count 爆炸？

Cost 与 benefit 同表报告。`output_segments` 等于 active semantic partitions 数，受 schema 约束，不是无界增长。Synthetic 中 write_amp 约 1.85 vs 1.14；真实 SF30 中 write_amp 1.24 vs 1.07。新增 real SF30 metadata replay 中，semantic weighted candidate-byte proxy = 1.00x，naive = 6.52x，所以额外写成本对应真实 segment distribution 上的 read-amp 保留收益。代价存在，但 bounded and explainable。

## Q4. Schema 变化后旧 storage 会失效或漏读吗？

不会。Additive changes 只影响未来写入；旧 segments 保留自己的 `schema_epoch`。当 exactness 不可证明时，SemL0 conservative read。No-false-negative theorem 说明 pruning 只能导致 extra reads，不能漏掉 visible edge。

## Q5. External SOTA baseline 怎么回答？

已有 LiveGraph SF10 真实外部系统对比：load 1,502s，peak RSS 约 45.7GiB，positive core edge types scan weighted avg 2.742us。LiveGraph SF100 load 不可行（17-21 days），所以 external comparison 诚实限定在 SF10。LSMGraph/BACH 属于 layout-level work，SemL0 是 semantic pruning/retention layer，正交可组合。

## Q6. C2 是 synthetic 还是真实？

三层证据：

- Controlled synthetic：测 read amplification 与 correctness，naive 4-6x blow-up，semantic flat，0 mismatch。
- Real LDBC SF30：测 retention + write cost，1.09B edges，40 partitions，528 exact L1；semantic retention 1.0 vs naive 0.0，write_amp 1.24 vs 1.07。
- Real SF30 metadata replay：测 candidate segments/bytes proxy，40 typed-neighbor partitions；naive avg candidate segments/query = 93.6、weighted read-amp proxy = 6.52x；semantic = 13.2、1.00x。

所以 C2 不是纯 synthetic，但论文必须诚实说明 metadata replay 不是完整 body-read workload。

## Q7. full semantic RSS 118GiB 是不是说明系统不可用？

不是。118.03GiB 是 unbudgeted/full-materialization stress point，不是推荐配置。SemL0 需要 benefit scoring 和 budget 的原因正是 full semantic 可能触发 fanout/RSS cliff。主 claim 应写成 budgeted lifecycle control：budgeted/schema variants 的 RSS 与 naive 接近，同时保留有用 pruning；C2 证明 surface 能以显式 bounded write cost 跨 compaction 保留。
