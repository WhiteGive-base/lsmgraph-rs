# SemL0 SF30/SF100 消融与代价实验 — 交付物（date_tag=20260608）

这是 `需要补的论文实验.md` 要求的 SemL0 消融/代价实验的全部产物副本。
实验在 git worktree `/data/WorkSpace/lsmgraph-ablation`（分支 `codex/query-semantic-lsm-research` @ `c059d63`）上运行，
走纯 LSM import 路径（`import --relation snb-full --l0-layout <variant>`），与本仓库的 BaseGraph SF100 读路径相互独立。

## 先看这个
- **`semL0-ablation-summary-20260608-cn.md`** —— 中文留痕主文档（P1 读放大 / P2 写代价 / P4 feedback + “关键发现与注意事项”）。

## 目录结构
- `semL0-ablation-summary-20260608-cn.md` — 主报告。
- `tables/` — 归一化结果：`e11-tables-sf30.md`、`e11-tables-sf100.md`（P1/P2 markdown 表）、`e11-variants-{sf30,sf100}.tsv`（全字段机读）、`e11-mechanism-isolation.csv`。
- `traces/` — 原始痕迹，每变体一个目录 `e11-{sf30,sf100}-<variant>-20260608/`：
  - `import.stdout/stderr`（导入结果/进度）、`time.log`（墙钟+RSS）、`file-summary.tsv`（store/L0/manifest 字节）、
    `fair-core-s200-r1.json` / `fair-alltypes-s50-r1.json`（storage-bench 读测量，含 neighbor_summary）、
    `neighbor-compare-core.json`（正确性，关键变体）。
  - `p3-feedback-vs-no-feedback-20260608.json` — feedback compaction 对照。
  - `e11-{sf30,sf100}-sample-plans-20260608/` — 共享采样计划。
- `scripts/` — 复现脚本：`run-e11-baseline-matrix.sh`（核心，规模参数化）+ `-sf30/-sf100` 封装；
  `extract-e11-tables.py`、`assemble-semL0-summary.py`（出表/出文档）；`reproduce-semL0-ablation.sh`（一键）。

## 一句话结论
语义布局把每查询候选 L0 段压低约 **8×**（SF30 60万→7-10万；SF100 197万→24-31万），读放大随规模变大；
**edge_type 是关键信号**，单独 label/degree 几乎无效；**预算式 SemL0 比无预算 full_semantic 更稳健**；正确性 **mismatch=0**。

## 两个必读注意点（详见主文档“关键发现”节）
1. **SF100 上 full_semantic/benefit_scored 的 read_bytes 偏高（GB 级）**：匹配段数与 schema 相同，但每段读字节 ~1000×——degree 分区段在大规模触发整段 body 读取。**SF100 读放大请以候选段数为准**，read_bytes 这两格需带此注解。
2. **full_compact@SF100 缺失（OOM）**：该写优化极端基线的一次性 L0→L1 合并在 35.7 亿边下峰值 ~472GB 被 OOM kill（合并实现的规模限制，非 SemL0 问题）；SF30 已有完整数据。

## 复现（在 worktree 内运行）
```bash
cd /data/WorkSpace/lsmgraph-ablation
bash reproduce-semL0-ablation.sh sf30        # 或 sf100 / all
```
注：`scripts/` 下脚本依赖 worktree 的 release 二进制与数据路径；此处副本用于查阅与归档。
原始可运行版本在 `/data/WorkSpace/lsmgraph-ablation/`（含已打补丁的内核：`SNB_SKIP_ADJ_CACHE`、`SNB_SKIP_SEM_INDEX`）。
