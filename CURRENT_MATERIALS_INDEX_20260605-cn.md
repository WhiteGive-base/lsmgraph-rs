# SemL0 当前材料索引

日期：2026-06-05

## 当前工作规则

Linux 是主工作空间：

```text
/data/WorkSpace/lsmgraph-rs
```

本地 Windows 工作空间是工作镜像：

```text
E:\文档\DGS项目复现\lsmgraph-rs
```

下一阶段不要从论文润色开始。应从代码就绪和实验闭环开始。

## 已检查的 Linux 状态

检查时间：2026-06-05 22:52 CST。

重要的 Linux 目录：

| 路径 | 状态 | 决策 |
|---|---|---|
| `src/` | 实现代码 | 保留 |
| `tests/` | 正确性测试 | 保留 |
| `examples/` | 可运行示例 | 保留 |
| `store/` | 约 130 GB，基础图和实验存储 | 保留，不要盲删 |
| `remote-logs/` | 约 297 MB，证据日志和汇总 | 保留 |
| `paper/` | 现有论文包 | 保留，当前不是活跃重点 |
| `figures/` | 论文/证据资源 | 保留 |
| `target-codex-*` | 115 个生成的构建缓存 | 已在 Linux 上删除 |
| `target/` | 标准 Cargo 构建缓存，清理前约 3.9 GB | 已在 Linux 上删除 |

清理 target 后的 Linux 磁盘状态：

```text
/data 总大小：2.0T
已用：约 1.4T
可用：约 517G
```

## 根目录策略

仓库根目录应只包含：

- 源码/构建文件，如 `Cargo.toml`、`Cargo.lock`、`README.md`；
- 入口交接文档；
- 当前实验计划；
- 清理/材料索引文档。

分阶段的 md 文件不应留在根目录。它们是有用的历史资料，但会让下一个工作窗口难以操作。

## 需要保留在根目录的入口文档

| 文件 | 用途 |
|---|---|
| `NEXT_WINDOW_HANDOFF_20260605.md` | 新窗口从这里开始 |
| `experiment-completion-plan.md` | 下一步主执行计划 |
| `CURRENT_MATERIALS_INDEX_20260605.md` | 本材料清单 |
| `workspace-cleanup-retention-20260605.md` | 已清理内容及必须保留内容 |
| `README.md` | 项目概览 |

## 归档材料布局

实验运行脚本：

```text
scripts/experiments/
```

分析/表格脚本：

```text
scripts/analysis/
```

维护辅助脚本：

```text
scripts/maintenance/
```

阶段文档：

```text
docs/archive/stage-md-20260605/
```

历史实验汇总和研究笔记：

```text
docs/evidence-summaries-20260605/
```

远程日志保留在原位：

```text
remote-logs/
```

远程日志保留细节：

```text
docs/REMOTE_LOGS_RETENTION_20260605.md
```

当前决策：

```text
不要删除 remote-logs/
Linux 上 remote-logs 大约 297 MB
许多 p5-* 条目是文档检查日志，但 p1/qslsm/p7 条目属于证据
```

存储保留在原位：

```text
store/
```

2026-06-05 整合后的清理统计：

| 工作空间 | 根目录 md 文件 | stage 归档 md | 证据汇总 md | target* 目录 |
|---|---:|---:|---:|---:|
| Linux 主工作区 | 5 | 401 | 19 | 0 |
| 本地镜像 | 5 | 400 | 24 | 0 |

下一阶段以 Linux 统计为准。

第二次整合后根目录脚本/候选源码清理结果：

| 类别 | 新位置 | 决策 |
|---|---|---|
| `run-*.sh` | `scripts/experiments/` | 保留为实验命令包 |
| `render-*.py`, `summarize-*.py` | `scripts/analysis/` | 保留为表格/日志汇总脚本 |
| `cleanup-unused-store-targets-*.sh`, `manifest_summary.py`, `store_size_summary.py` | `scripts/maintenance/` | 保留，但不是根目录入口 |
| 根目录 `.bib` 和 `.tex` 候选文件 | `paper/archive/root-candidates-20260605/` | 作为论文源历史保留，当前不是活跃重点 |
| `tmp-sync-files`, `tmp-sync-files-p15` | 已在 Linux 上删除 | 旧同步临时文件 |
| 根目录临时文件，如 `output.json`、空的 `graph.rs` 和 shell 输出产物 | `docs/archive/root-scratch-20260605/` | 已归档而非删除 |

## 重要归档文件

根目录清理后，请通过归档路径阅读以下文件：

| 主题 | 文件 |
|---|---|
| P7.5 C1 类型读取 bug 修复 | `docs/archive/stage-md-20260605/seml0-stage-p7-05-edge-type-mismatch-diagnosis-and-fix-20260604.md` |
| 实现/投稿边界 | `docs/archive/stage-md-20260605/seml0-stage-p6-61-final-implementation-and-submission-roadmap-20260604.md` |
| C1 消融命令审计 | `docs/archive/stage-md-20260605/seml0-current-summary-final-target-after-p5-c1-ablation-command-bundle-audit-20260605.md` |
| 持续反馈计划 | `docs/archive/stage-md-20260605/seml0-current-summary-final-target-after-p5-sustained-feedback-compaction-plan-20260605.md` |
| 模式增量压力清单 | `docs/archive/stage-md-20260605/seml0-current-summary-final-target-after-p5-mixed-schema-delta-stress-inventory-20260605.md` |

## 当前技术定位

代码确实有真实的实现工作，但实验闭环尚未完成。

当前代码可以支撑范围限定的原型性论点：

- 带有已修复类型读取正确性问题的语义 L0 剪枝；
- 加性模式时代处理；
- 属性元数据和定宽属性值原型路径；
- 针对性的快照/墓碑/模式正确性测试；
- 反馈压缩针对性测试和基准脚手架。

当前代码尚不应被用于更宽泛的论点：

- 完整的模式迁移引擎；
- 最新代码下最终的 SF30/SF100 性能；
- 生产级写停顿安全性；
- 长期持续反馈稳定性；
- 最终论文投稿就绪状态。

## 下一步操作目标

按以下顺序运行实验闭环：

1. E0 代码就绪门槛。
2. E1 针对性正确性套件。
3. E2/E3 最新代码 SF1 C1 刷新和消融。
4. 如果 P3 仍是主要贡献，则运行 E4 持续反馈工作负载切换。
5. 运行 E5 模式增量压力。
6. 决定目标投稿渠道是否需要 SF30/SF100。
