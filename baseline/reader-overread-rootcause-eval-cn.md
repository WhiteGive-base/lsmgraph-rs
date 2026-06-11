# SF100 read_bytes 过读：根因定位 + 修复对论文收益评估（2026-06-11）

针对用户问题「定位 read_bytes 过读的具体原因 + 评估修复后对论文的收益」。

## 1. 根因（已确认 + 量化）
**现象**：SF100 s5000 下 `semantic` 读 10 GB、`budg-b1024` 读 700 MiB，而 schema/edge-type/budg-b64/256 ~147 MiB。
逐 et 拆（et=2 HasCreator）：semantic 与 schema 的 body(160KB)、body_reads(5000)、filter_passed(~9.3K) 几乎相同，
但 **read_bytes 3 GB vs 3 MB**，差异全在 **offset 数组读**：semantic `offset_reads=123`、schema `offset_reads=3`。

**两个叠加因素：**
1. **读整段 offset 数组**：`reader` 服务一次 get_neighbors 时，`load_metadata` 把该段**整个 offset 数组**读进来
   （`read_at(offsets_offset, offsets_len)`），再取目标 source 的那 24B 项。64MB 段的 degree-1 高边类型
   （HasCreator/ReplyOf）offset 数组可达 ~48MB（2M sources × `EDGE_OFFSET_LEN=24`）。读 48MB 只为 1 个 source。
2. **元数据 cache 抖动**：`CsrMetadataCache` 容量**硬编码 4096**（`graph.rs:577`）。
   schema 3444 个 L0 文件 < 4096 → 全缓存、offset 数组只读一次（3 misses）；
   semantic **6615** 个 L0 文件 > 4096 → LRU 抖动 → 每次查询重读大 offset 数组（123 misses）→ read_bytes 爆。

**为什么不是真慢**：重读的 48MB 命中 OS page cache，所以**延迟只差 ~3×**（avg 1290µs vs 409µs）。
body(154MB) 正确。→ **read_bytes 是 cache 抖动 + 读整段 offset 的实现伪影，不是布局的真实读放大。**

## 2. 修复选项与代价
| 选项 | 做法 | 代价 | 风险 |
| --- | --- | --- | --- |
| A 调大 cache | `max_entries` ≥ 文件数（或可配） | 代码 1 行 | **内存高**：6615 × 最大 48MB offset ≈ 上百 GB，不可行 |
| B 精确读 offset | 不读整段数组，二分/稀疏索引只读目标 source 的 24B 项 | 中（reader 读路径，可能加段内稀疏索引） | 低；read_bytes → ~body |
| C 不修 | 主表用 latency+candidate+L0files，read_bytes 带"cache 抖动伪影"注脚/附录 | 零 | 零 |

## 3. 对论文的收益评估
- **核心结论不依赖修复**：budgeted 受控（L0 3483≈schema）、**延迟最优**（比 schema 快 ~25%）、对 naive 剪枝 ~99%、
  full-semantic 过度分段（6615 L0 文件本身就是真实维护成本）——这些用 **latency / candidate_l0 / L0 files / vs-naive** 都成立，**不需要 read_bytes**。
- **修复的收益**：让 `read_bytes` 成为可信指标（semantic 会从 10GB 降到 ≈schema/body ~150-200MB）。不修则
  semantic/budg-b1024 的 read_bytes 是实现伪影，审稿人会质疑"10GB 是 cache 实现问题还是布局问题"。
- **净评估**：
  - 对**论文结论**：修复收益 **低**（结论已由其它指标支撑）。
  - 对 **read_bytes 指标可信度**：修复收益 **中**（避免一个伪影数被攻击）。
  - 代价：选项 B **中**（reader 改动 + 重跑 semantic+budg-b1024 ~5h）；选项 A 不可行（内存）。

## 4. 建议
- **主表/主张用 latency + candidate_l0 + L0 files + vs-naive**（全部干净、结论完整）。read_bytes 对
  重分段层级（semantic/budg-b1024）**带注脚**说明是 cache-thrashing 伪影、真实代价看 latency。→ **零成本、结论可用**。
- **可选后续（若要 read_bytes 也干净）**：做选项 B（精确 offset 读），它同时是个正当的系统优化
  （读 24B 而非 48MB），修完重跑这两个变体即可；预期 semantic read_bytes ≈ schema，叙事更稳但**不改变结论**。
- 不建议选项 A（cache 调大）——治标且内存不可行。

**一句话**：这个 bug 该修（选项 B，正当优化），但**它不阻塞论文**——核心强 baseline 结论用 latency/candidate/L0-files 已经成立；read_bytes 修复属于"锦上添花 + 防审稿人挑刺"，中等优先级。
