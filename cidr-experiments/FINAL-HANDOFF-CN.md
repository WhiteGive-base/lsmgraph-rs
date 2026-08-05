# SemL0 CIDR 实验代码封板说明

封板日期：2026-08-05

封板分支：`codex/cidr-experiment-final-20260805`

本文件是本轮实验工程的最终代码索引。仓库保留实验 runner、数据合同、验证器、正式绘图入口和小型 provenance/normalized 证据；大型数据库、raw timing、容器层、临时渲染目录和运行日志不作为 Git 源码交付。

## 1. 代码入口

| 路径 | 用途 |
|---|---|
| `f1c-control/e01-v2/` | Figure 1 七系统、三重复的 manifest、admission、执行、postprocess 和 receipt 合同 |
| `runners/p02b/` | SF10 sentinel、稳定性计算、lineage manifest 和 clean-ready 验证 |
| `runners/p10/` | 跨系统 adapter/orchestrator 与 P31 资源采集绑定 |
| `runners/p20/` | A0--A6 profile 执行、正确性门、规范化与聚合 |
| `runners/p20-prep/` | property 候选、workload truth、pristine inventory 和 correctness 准备 |
| `runners/p31/` | 资源 collector、manifest、schema 和 run validator |
| `runners/p40/` | fixed-trace 动态实验入口（若该目录存在于所选提交） |
| `figures/scripts/formal/` | Figure 1--6 的 fail-closed 正式绘图入口 |
| `plan/` | 冻结的实验矩阵、字段要求和图表合同 |
| `data/normalized/`、`data/provenance/` | 可入库的小型规范化数据与来源绑定 |
| `runs/` | 小型 run manifest、DONE/receipt；raw 和大日志按 `.gitignore` 排除 |

各模块的细节以就近的 `README.md` / `README-CN.md` 为准。早期的 `CURRENT-STATUS-CN.md` 与 `PROGRESS-CN.md` 仅作为历史快照保留。

## 2. Linux 快速验证

以下命令从仓库根目录执行，不启动 Docker、不触碰冻结 store，也不运行正式 timing：

```bash
python3 -m unittest discover \
  -s cidr-experiments/f1c-control/e01-v2/tests \
  -p 'test_*.py' -v

python3 -m unittest discover \
  -s cidr-experiments/runners/tests \
  -p 'test_*.py' -v

python3 -m unittest discover \
  -s cidr-experiments/runners/p20 \
  -p 'test_*.py' -v

python3 -m unittest discover \
  -s cidr-experiments/runners/p20-prep \
  -p 'test_*.py' -v

python3 -m compileall -q \
  cidr-experiments/f1c-control/e01-v2 \
  cidr-experiments/runners \
  cidr-experiments/figures/scripts/formal
```

正式绘图的输入合同与示例命令见 `figures/scripts/formal/README.md`。绘图入口在 provenance、digest、matched protocol 或独立重复不满足时应拒绝输出，这是预期行为。

## 3. 正式运行边界

1. 正式性能运行只能在 Linux clean window 内串行执行；不得把 smoke、fixture、synthetic 或 conditional evidence 提升为正式 timing。
2. `formal_eligible`、`performance_eligible`、`paper_claim_eligible` 必须以对应 receipt 为准。代码封板不自动改变任何历史证据的资格。
3. 大型 store、raw、容器和外部系统数据继续使用 manifest 中登记的绝对路径及 SHA；仓库只保存能够验证其 lineage 的小文件。
4. 任一 `digest_pass=false`、`mismatch_count>0`、receipt 缺失或 SHA 漂移都必须 fail closed。
5. 重新运行前先检查磁盘水位、后台服务、P03/P02B admission 和 P31 collector，不得直接复用旧的 clean-window 结论。

## 4. Git 与产物策略

- 最终分支从 `codex/f1-target-p02b-20260728` 的完整线性证据链封板。
- 旧出图 worktree 中未提交的预览文件不属于本次封板，避免把候选图和最终 runner 混在同一提交。
- Python `__pycache__`、`*.pyc`、raw、日志、数据库目录和大归档不得提交。
- PNG/PDF/SVG 只有在它们是论文交付物且能由已入库脚本与冻结输入重建时才保留；中间渲染页和临时 QA 目录不入库。
- 恢复或复现实验时先记录 `git rev-parse HEAD`，再按对应 manifest/receipt 的 SHA 检查外部资产。

## 5. 暂停后的恢复顺序

1. `git status --short --branch` 确认工作区干净，并记录当前 commit。
2. 执行第 2 节的非 timing 回归。
3. 重新审计 CPU、内存、磁盘、容器和其他用户进程；旧资源快照不可直接复用。
4. 需要正式运行时，按 P03 clean-window → P02B sentinel → 对应 runner → P31/validator → normalize/plot/QA 的顺序恢复。
5. 新证据使用新的 run ID 和不可覆盖目录，不修改历史 receipt。
