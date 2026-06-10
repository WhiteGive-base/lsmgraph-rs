# SF100 强 baseline 工程进度（开始于 2026-06-10）

主项目：`/data/WorkSpace/lsmgraph-rs`，分支 `codex/sf100-basegraph-bench`。
计划：`~/.claude/plans/data-workspace-lsmgraph-rs-baseline-bas-jiggly-puddle.md`。

## 关键事实（核实过）
- `baseline/` 旧「SF100 强 baseline」其实是 ~SF1（`social_network_tugraph`，34.7M 有向边，store ~3.6G）。
- 真实 SF100 输入：`/data/WorkSpace/ldbc-sf100/social_network`（90G，dynamic/+static/），单 store ~132G。
- 「budgeted≈schema」根因：`--semantic-budget-min-edge-type-score` 默认 `1.0e308`（不需要改代码，传有限值即可）。
- 外部系统源码本地已构建：`deps/{LiveGraph(.so),teseo(.a),GraphOne}`。
- `lsmgraph-ablation` 是本仓 worktree（分支 `codex/query-semantic-lsm-research` c059d63），与主分支在 ea45ef1 后分叉。

## 阶段 0（完成）— 抢救 worktree 产物 + 清磁盘
- 全部已迁入 `migrated-from-ablation-20260610/`（82M）：
  - `patches/ablation-uncommitted-src-vs-c059d63.patch`（worktree 未提交补丁：graph.rs/full_loader.rs/props.rs）
  - `patches/MERGE-REFS.txt`（ablation/main/merge-base 提交号）
  - `scripts/`（run-e11-baseline-matrix*.sh、extract/assemble*.py、kv_style_baseline.py、reproduce-semL0-ablation.sh 等）
  - `results/remote-logs/`（SF30 全量含 full_compact、SF100 已完成变体、sample-plans、p3-feedback json、normalized 表）
  - 注：SF100 的 e11 结果（benefit_scored 等）仍在写，释放 worktree 前会再 re-sync 一次。
- 删除错规模残留 store 回收 ~60G：`/data` 297G→357G free。
- e11 SF100 跑仍在进行（orchestrator PID 1687257 + benefit_scored import PID 1802729）。
  该 benefit_scored 用默认 budget（1e308）→ 会复现「budgeted==schema」，作为问题数据点保留；阶段 2 另跑 tuned sweep。

## 阶段 1（完成）— 把 SemL0 能力合并进主分支
- 还原点 tag：`pre-semL0-merge-20260610`（50a3d15）。
- 合并 `codex/query-semantic-lsm-research`(c059d63) → `codex/sf100-basegraph-bench`，提交 `c08ca3f`。
  仅 2 处冲突：`.gitignore`(取并集)、`src/snb/props.rs`(保留两侧方法)。
- 修一处语义不兼容：SemL0 的 `reset_storage_metrics`/`storage_metrics_snapshot_json` 引用了主线已删除的
  `fallback_engine` 字段 → 改为只用 `view.delta().engine().metrics()`（主线用 `fallback_hits` 计数器替代）。
- 叠加 SF100 可行性补丁，提交 `9983d6f`（SNB_SKIP_SEM_INDEX / SNB_SKIP_ADJ_CACHE / AdjacencyBuilder disabled）。
- 验证闸全过：`cargo build --release` ✓、`cargo test --no-run` ✓、semantic 单测 ✓；
  二进制 `target/release/{lsmgraph,p3-feedback-bench}` 带 `--l0-layout`/`--semantic-budget-*`/storage-bench 全旗标。
- **SF1 端到端冒烟**（`remote-logs/smoke-merge-sf1-20260610/`，已删 store）：
  schema read_bytes=3,314,048；edge-type-only=600,224；semantic=600,224；
  neighbor-compare schema-vs-{edge-type-only,semantic} 均 450/450、mismatches=0。→ 合并行为与 ablation 一致。

## 阶段 2（代码完成，待 SF100 数据）— 让 semantic-budgeted 真正生效
- 读懂 budgeted 算法：`evaluate_budgeted_semantic_edge_type` 用 byte gate(默认4MB) + score gate(默认1e308=禁用)
  选 edge type；`apply_..._file_budget` 按 score 贪心保留至 `--semantic-budget-max-extra-l0-files` 耗尽。
  默认 → 只有≥4MB的组进 exact，其余塌缩成 MIXED → 退化≈schema（bug 确认）。
- **SF1 验证（旧算法）**：schema=3.31MB读；budg-default=3.31MB(=schema, 复现bug)；
  budg-b256=0.60MB(=full-semantic 读放大) 但 L0 文件 111 < semantic 138（Pareto 赢）；
  但中间预算(b64=24.5MB)反而**比 schema 差**（未选中 edge type 塌缩 MIXED 丢了 edge_type 剪枝）= cliff。
- **算法修正（提交 0f044c5，用户批准）**：未选中 edge type 改为保留真实 edge_type（degree 混合=schema 式），
  不再塌缩 MIXED。budgeted 变成 schema→semantic 的**单调可控曲线**、任何预算下都 ≤schema 读放大。
  推荐主表用 `--semantic-budget-min-edge-type-bytes 0 --semantic-budget-max-extra-l0-files {0,32,64,128,256,512,1024}`。
- harness 已重写：`baseline/codex_qslsm_sf100_strong_baseline.sh`（真实SF100 / 64MB memgraph / SF100 env /
  import→bench→删 / budget sweep / 样本分档 / kv-style 附录 / feedback / 可选 full-compact / RUN_COMPARE）。
  bash -n 通过，已镜像到 `scripts/`。

## 重要发现：e11 SF100 结果是**部分**的，不能全复用
`migrated-from-ablation-20260610/results/.../e11-tables-sf100.md`：只有 schema(9.37MB读/141G/3444 L0)、
naive/lsmgraph-style(26.4GB读/0%)、label-only(0.4%) 有完整数；**edge-type-only=N/A，full-semantic/degree-only/
benefit_scored 未进表**（生成表时还没跑完）。→ SF100 的 SemL0 关键变体（edge-type-only / full-semantic /
budget sweep）必须用**修正后的二进制**重跑；schema/naive/label-only 可复用。
e11 当前还在跑 benefit_scored import（默认参数=会复现 bug），之后还要 full_compact（有磁盘风险）—— 均属冗余/我会重做。

## e11 自然结束（无需停）+ 关键发现
2026-06-10 08:4x，e11 SF100 跑**自行结束**：benefit_scored import 完成→删；`import full_compact`
在 L0→L1 compaction 阶段 **OOM**（dmesg 04:38 anon-rss 472GB）→ 进程全灭，worktree store 清空。
→ **full_compact @ SF100 受内存限制不可行**（不只是磁盘）；harness 默认 RUN_FULL_COMPACT=0；上界引 SF30。
机器现空闲（disk 541G→526G free、RAM 445G free），SF100 主跑解锁。
真实 SF100 规模确认：**directed_edges=3,570,968,680（35.7亿）**。

## 阶段 3/6（进行中）— SF100 主跑已启动
`remote-logs/qslsm-sf100-strong-baseline-20260610/`（PID 1928457，s5000）：
schema(参考+plan) + edge-type-only + semantic + budg-{64,256,1024}，用**修正二进制**；
naive/lsmgraph-style/label-only/degree-only 复用 e11 staged 数。预计 ~10h。
监控：persistent monitor（里程碑+失败）。汇总脚本 `baseline/summarize_strong_baseline.py`（合成数据已验证）就绪。

## 阶段 7（已就绪+方案固定，待主跑释放内存后跑）
4 个外部系统源码/库均就绪（LiveGraph .so / Teseo .a / GraphOne 可执行 / LLAMA 模板；TBB+g++9.4+cmake 齐）。
实现方案+公平性方法学固定在 `baseline/external-driver-implementation-plan-cn.md`
（全局 id 重映射、edge-type 映射、共享转换器、各系统薄 driver、先 SF10 验证后 SF100 串行跑避免 OOM）。

## 主跑 bug 修复（已提交 3106832）
首跑在 schema 导入完成（132G，70min，**无损保留**）后崩在 harness：`bench_variant`/`compare_variant`
一行内 `local name=$1 ... store=...${name}` 在 set -u 下 `${name}` 作为 local 实参提前展开=unbound；
且 main() 在 plan 生成前就 bench schema。已修：拆成两条语句、重排 import→plan-gen→bench、import 加
「store 已存在则跳过」断点续跑。**重启后 schema 导入跳过，正常进入 plan-gen**（PID 2043648）。

## 外部对照边集来源（复查发现，已记入 external-driver-implementation-plan-cn.md §3b）
LDBC SF1/SF100 是复合实体文件格式（hasCreator/replyOf/containerOf 内嵌列），且 lsmgraph 无批量边导出。
干净 enabler = 给 lsmgraph 加 `scan --dump-edges`（纯增量）。**但不要在主跑期间 rebuild 二进制**
（主跑会为后续变体 spawn 新进程）→ 外部实现整体安排在主跑结束后。

## 会话末状态（2026-06-10）
- 已提交（本会话）：merge(c08ca3f)→SF100补丁(9983d6f)→budgeted修法(0f044c5)→harness+summary工具(39e3a4e)
  →外部方案+trace(c92a791)→harness修bug(3106832) + 本次文档更新。
- 正在跑：SF100 强 baseline（PID 2043648，s5000），plan-gen 中，预计 ~7h；persistent monitor 看守。
- 主跑产出后立即可做：`python3 baseline/summarize_strong_baseline.py --out-dir
  remote-logs/qslsm-sf100-strong-baseline-20260610 --scale sf100 --reuse-tsv
  migrated-from-ablation-20260610/results/remote-logs/e11-normalized-20260608/e11-variants-sf100.tsv`
  → P1/P2/budget-sweep 表（合并复用 e11 的 naive/lsmgraph/label/degree 行）。

## 主跑第二个 bug：read bench 漏了 --semantic-degree-hint（已修 6beda2b）
首批 SF100 数发现异常：semantic 读 **9994 MiB（10GB）**，比 schema(149.7MiB) **差 66×**，与 SF1/SF30 相反。
根因：`bench_variant` 的 storage-bench 读**没传 `--semantic-degree-hint`** → 查询只按 edge_type，
degree 分区的 layout（semantic/budgeted）把每个 degree-class 段都当 candidate（爆炸），而非按 degree 剪枝。
e11 参考 bench 一直带这个旗标（确认 5 处），我漏了。schema/edge-type-only/naive 无 degree 分区 → hint 无关
（不受影响，数有效）。已给 bench_variant 加上，重启：schema 跳过导入+plan-gen（缓存），重新 bench schema
（作 hint-invariance 校验）、重跑 semantic + budg-{64,256,1024}（带 hint）；edge-type-only 保留旧结果（hint 无关）。
**有效 SF100 数（hint 无关，已确认）：** schema 149.7MiB / cand 5.93M / 3444 L0；
edge-type-only 146.7MiB / cand 5.90M / 3446 L0（两者对 naive 26GB 均剪枝 ~99.4%；彼此差 2%，SF100 大样本下层级差被 body 读主导压扁）。
**重启 PID 2307023（01:02），预计 ~9h → 明早 ~10:00；带 hint 的 semantic 应大幅低于 schema。**

## 重大发现：full semantic 在 SF100 反而比 schema 差（hint 无关）
带 hint 重跑后 semantic 仍 **10GB 读**（与不带 hint 字节相同）→ 不是 hint 问题。逐 et 拆解（et=2 HasCreator）：
schema 与 semantic 的 filter_passed(~9.3K)、body(160KB)、read_syscalls(~5.2K) 几乎相同，但
**read_bytes 3GB vs 3MB** → semantic 每次 body pread **~590KB 但只用 ~32B**（add_read 计 pread 全长，
record_body_read 只计用到的边）→ **~950× body 过读**。但**延迟只差 ~3×**（avg 1290us vs 409us，OS cache 吸收）。
另外 candidate_l0 semantic 7.78M > schema 5.93M、L0 文件 6615 > 3444 = degree 分区**多产生段**（真实属性）。
SF1/SF30 semantic 是最好的；SF100 反转 → 要么 scale 阈值、要么 merge 合并 reader.rs 引入的 body-read range 退化。
**两面性：** 若 budgeted（受 file budget 限制分段）避开了这个爆炸 → "可控成本反而胜过 full upper bound" 是个强故事；
若 budgeted 也爆 → reader body-read range 是真 bug 需修。**等 budg-b64（~2h）判定。**
注：latency + candidate_l0 这两个指标有效；read_bytes 受过读影响需谨慎/或修 reader。

## 待办（主跑结束后）
阶段8 出 SF100 表 + 重写 sf100-strong-baseline-summary.md（去掉夸大表述、带 scale 列）；
阶段4 edge-type-不够用 workload（storage-bench 加 property/signature 模式）；阶段5 feedback 专项；
阶段7 外部：加 `scan --dump-edges` → 转换器 → LiveGraph/Teseo/GraphOne driver（先 SF10 后 SF100 串行）；
阶段6 full_compact 上界引 SF30（SF100 OOM）；阶段9 迁移确认后 `git worktree remove` 释放 ~204G+。
