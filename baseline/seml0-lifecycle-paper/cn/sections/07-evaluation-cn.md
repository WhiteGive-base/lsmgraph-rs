# 07 Evaluation 中文版

六个 RQ：

1. **RQ1**：semantic surface 是否降低 read amplification？W6 SF100 支持。
2. **RQ2**：benefit-scored materialization 是否避免 full-semantic cost？W6 budget sweep 支持。
3. **RQ3**：feedback 是否适应 workload shift，dynamic churn 下 surface 是否有帮助？W7/W9 支持。
4. **RQ4**：schema evolution/deltas 是否保持 correctness？W13 支持。
5. **RQ5**：write/storage/maintenance cost 多大？W6 maintenance/RSS 支持。
6. **RQ6**：semantic-aware compaction 是否以 bounded cost 保留 surface？C2 支持。

## 最重要数字

### W6 SF100

`naive` candidate L0 = 49,257,601；`schema` = 5,929,197；`edge-type-only` = 5,899,015；`budg-b64` = 6,022,507；`semantic` read bytes 最低。全部 `mismatches=0`。

注意：`semantic` import RSS = 118.03 GiB，是 unbudgeted/full-materialization outlier。必须把它写成 SemL0 需要 budgeted lifecycle control 的证据，而不是推荐配置。

### W9 SF30 dynamic

Schema p99 从 1,270.6us 到 8,740.2us；semantic p99 从 414.6us 到 1,274.0us。Semantic p99 vs schema 平均 -82.5%，但代价是 candidate L0 +86.3%，L0 files +93.4%。

### C2

Synthetic rows 证明完整 read workload 下的 read blow-up；real SF30 证明 retention 和 write cost；新增 metadata proxy 进一步说明真实 SF30 post-merge metadata 上 naive = 6.52x weighted candidate-byte read-amp，semantic = 1.00x。
