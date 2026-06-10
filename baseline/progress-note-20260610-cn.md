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

## 待办
SF100 重活（budget sweep + edge-type-only + full-semantic + 阶段4/5/6/7）受 e11 占用磁盘/内存阻塞，
需先决定 e11 去留（见与用户确认）。其余非阻塞项继续：外部系统构建(阶段7)、analysis 汇总管线、文档。
