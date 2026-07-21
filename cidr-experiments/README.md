# SemL0 CIDR 实验工作包

本目录统一管理文章实验大纲、既有证据、补跑记录、规范化数据、Python 绘图脚本、图表输出和视觉验收结果。旧 W6/W8/W9/C2 等大体积证据不搬动，由 registry 保存来源路径和校验信息。

当前总览：[`CURRENT-STATUS-CN.md`](CURRENT-STATUS-CN.md)。Linux 补跑实时状态与阶段 ETA：[`PROGRESS-CN.md`](PROGRESS-CN.md)。

## 当前任务

1. 审计现有实验数据，判定 `REUSE / FIX / RERUN`。
2. 对缺失或口径不合格的数据补跑实验。
3. 用 Python 生成六组主图，输出 PNG、SVG、PDF。
4. 将 PDF 重新渲染为 PNG，逐图检查裁切、文字/图例遮挡和两栏可读性。

## 目录

| 路径 | 内容 |
|---|---|
| `plan/` | 文章实验大纲和最终执行矩阵 |
| `data/` | 数据来源 registry、规范化 TSV、provenance |
| `runs/` | 新实验 run manifest、远端路径与状态；大日志不进 Git |
| `figures/scripts/` | Python/Matplotlib 绘图代码 |
| `figures/output/` | 生成的 PNG、SVG、PDF |
| `validation/` | 数据门槛、PDF 渲染、视觉 QA 和问题清单 |
| `work/` | 多 agent 的中间审计；验证后再迁入正式目录 |

文章实验大纲：[`plan/ARTICLE-EXPERIMENT-OUTLINE-CN.md`](plan/ARTICLE-EXPERIMENT-OUTLINE-CN.md)

## 六组主图

1. `fig1-end-to-end`：相同接口的系统对比。
2. `fig2-component-ablation`：核心设计逐组件消融。
3. `fig3-resource-tradeoff`：CPU、内存、磁盘和文件数权衡。
4. `fig4-dynamic-compaction`：动态读写与 compaction 时间线。
5. `fig5-workload-coverage`：property、degree、2-hop 和 workload shift。
6. `fig6-scalability`：数据规模和并发扩展。

正确性/fallback 以表格为主，不硬塞成柱状图。

## 当前可复现命令

```powershell
python cidr-experiments/data/normalize_existing.py
python cidr-experiments/figures/scripts/plot_provisional_evidence.py
python cidr-experiments/figures/scripts/validate_rendered.py `
  --input-dir cidr-experiments/figures/output/provisional `
  --render-dir cidr-experiments/validation/rendered-provisional
```

这三条命令只重建 provisional 诊断产物。正式图使用冻结数据契约，并会在 provenance、digest、independent repeats 或 matched protocol 缺失时拒绝绘图。

正式绘图入口与负向门槛测试说明：[`figures/scripts/formal/README.md`](figures/scripts/formal/README.md)、[`validation/FORMAL-PLOT-GATES-CN.md`](validation/FORMAL-PLOT-GATES-CN.md)。

用户 2026-07-21 最新论文 ZIP 的实验需求对照见 [`work/latest-paper-audit/LATEST-PAPER-AUDIT-CN.md`](work/latest-paper-audit/LATEST-PAPER-AUDIT-CN.md)。
