# baseline/ 目录索引（给导师看）

本目录汇总 SemL0 论文相关的**结果、设计文档与论文初稿**。内容来自两条并行的工作线：

## A. SemL0 查询语义消融（本次主线，论文核心证据）
- **`semL0-paper-draft-cn.md`** —— **论文初稿（导师讨论版）**，含 SF1/SF30/SF100 实测数据表与诚实的注意点。先看这个。
- **`semL0-ablation-results/`** —— 跑出来的结果文档:
  - `semL0-ablation-summary-20260608-cn.md` 中文留痕主报告（P1 读放大 / P2 写代价 / P4 feedback + 关键发现）
  - `tables/` 归一化表（e11-tables-sf30.md、e11-tables-sf100.md、e11-variants-*.tsv）
  - `traces/` 每变体原始痕迹（import/time/fair-*.json/neighbor-compare）+ feedback JSON
  - `scripts/` 矩阵/分析/一键复现脚本
- **`semL0-design-docs/`** —— 设计文档（论文各章草稿 + 实验计划）:
  叙事主线、章节大纲、引言、背景与问题、系统总览、查询语义物理设计、反馈式 compaction、评测、相关工作、局限、结论，以及 `需要补的论文实验.md`（本轮实验的出发点）。

### 一句话结论
语义布局把每查询候选 L0 段降低约 8×（SF30 60万→7-10万；SF100 197万→24-31万），(label+edge_type) 读字节降 99.8–100%，exact-proof（mismatch=0）；**(label,edge_type) 是跨规模稳健主因**，带预算的 SemL0 以更小写代价逼近全量上界。

### 当前待补
- full_compact@SF100：流式合并已实现并在 SF1 验证 mismatch=0；正等并发实验释放内存后**自动补跑**（完成后会刷新 `semL0-ablation-results/tables/e11-tables-sf100.md`）。
- SF100 上 full_semantic/benefit_scored 的 read_bytes 偏高（degree 段整段 body 读取）——读放大以候选段数为准，详见草稿 §6.4 注。

## B. qslsm SF100 强基线（另一并行会话产出，主仓库）
以下文件来自并行运行的 strong-baseline 会话，非本消融主线：
- **报告**：`sf100-strong-baseline-summary.md`（**真实 SF100 权威总结，先看**）、`reader-overread-rootcause-eval-cn.md`（read_bytes 过读根因+对论文收益评估，对应上面 §当前待补 第 2 条）、`progress-note-20260610-cn.md`（全程工程 trace + 两个 bug 修复）、`external-baseline-comparison.md`、`external-driver-implementation-plan-cn.md`。
- **结果数据**：`sf100-results-s5000.tsv`（每变体聚合）、`sf100-strong-baseline-traces/`（每变体 stats + `sf100-per-edge-type-s5000.tsv` 每-edge-type 指标 + 机器汇总 + run-config + progress；完整 18MB 原始 bench JSON 留在 `remote-logs/qslsm-sf100-strong-baseline-20260610/` 未入 git）。
- **脚本/工具**：`codex_qslsm_sf100_strong_baseline.sh`、`summarize_strong_baseline.py`、`run-status.sh`、`kv_style_baseline.py`、`external-drivers/`（LiveGraph driver）、`summary-enhanced.tsv`（旧 ~SF1 数据，保留参考）。
