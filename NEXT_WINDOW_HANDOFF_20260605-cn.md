# SemL0 新窗口交接

日期：2026-06-05

## 当前决策

我们目前不继续打磨论文。

当前的首要目标是：

```text
完成支撑论文论点的代码和实验闭环。
```

论文可以作为参考包存在，但下一个工作窗口应优先考虑实现就绪状态和实验证据。

## 代码方面的实际改进

仓库不仅仅是文档。代码在多个方面有所改动：

- C1 语义 L0 剪枝和度感知候选选择：
  - `src/graph.rs`
  - 重要修复记录：`docs/archive/stage-md-20260605/seml0-stage-p7-05-edge-type-mismatch-diagnosis-and-fix-20260604.md`
- 加性模式目录和模式时代处理：
  - `src/schema.rs`
  - `src/graph.rs`
- CSR 属性元数据、属性值分段和编码时代钩子：
  - `src/csr/format.rs`
  - `src/csr/writer.rs`
  - `src/csr/reader.rs`
  - `src/property_encoding.rs`
- 针对性正确性测试：
  - `tests/engine_tests.rs`
- 反馈压缩基准测试二进制：
  - `src/bin/p3_feedback_bench.rs`

当前代码的最佳描述是：

```text
具有针对性正确性覆盖和部分实验证据的范围限定研究原型
```

不应将其描述为：

```text
完整的生产就绪模式演进 LSM 图系统
```

## 当前支持的论点边界

已通过当前代码和现有证据支持：

- 加性模式变更不会自动使旧存储失效；
- 添加边标签或属性可通过目录时代加保守读取处理；
- 未知、旧、不完整或墓碑敏感的元数据必须保守读取；
- C1 语义剪枝有最新代码 SF1 证据路径；
- 真实的类型读取剪枝误报已诊断并修复；
- 存在针对性的快照/墓碑/模式/属性正确性测试；
- 反馈压缩有针对性测试和微基准脚手架。

尚未完全支持：

- 最新代码 SF30/SF100 最终性能声明；
- 长期工作负载切换下反馈的持续稳定性；
- 生产写停顿或重写背压声明；
- 重命名/删除/类型变更物理重写的完整模式迁移；
- 所有表和日志的一键可重现性；
- 最终提交就绪状态。

## 从这里开始的五步主线

1. 代码就绪门槛

   确认当前代码中哪些论点可执行。先运行针对性测试。

2. 证据和保留映射

   确定哪些存储/日志是论文证据，哪些是可丢弃的构建或临时输出。

3. 完成缺失的实验

   优先进行最新代码 C1 刷新、持续反馈和模式/快照压力证据。

4. 冻结实验证据

   创建稳定的日志目录、命令记录、二进制/配置标记和表输入。

5. 在证据冻结后才返回论文

   论文写作应总结冻结的证据，而非驱动工作。

## 新窗口中首先阅读的文件

按此顺序优先阅读：

1. `NEXT_WINDOW_HANDOFF_20260605.md`
2. `experiment-completion-plan.md`
3. `workspace-cleanup-retention-20260605.md`
4. `CURRENT_MATERIALS_INDEX_20260605.md`
5. `docs/archive/stage-md-20260605/seml0-stage-p7-05-edge-type-mismatch-diagnosis-and-fix-20260604.md`
6. `docs/archive/stage-md-20260605/seml0-stage-p6-61-final-implementation-and-submission-roadmap-20260604.md`
7. `docs/archive/stage-md-20260605/seml0-current-summary-final-target-after-p5-c1-ablation-command-bundle-audit-20260605.md`
8. `docs/archive/stage-md-20260605/seml0-current-summary-final-target-after-p5-sustained-feedback-compaction-plan-20260605.md`
9. `docs/archive/stage-md-20260605/seml0-current-summary-final-target-after-p5-mixed-schema-delta-stress-inventory-20260605.md`

## 下一个窗口中不要做的事

- 不要首先继续论文正文润色。
- 不要生成 TeX 或 PDF。
- 不要在未做出保留决策的情况下删除存储或日志。
- 不要覆盖旧实验存储。
- 在存在新证据之前，不要声称 SF30/SF100 或持续反馈已完成。

## 新窗口中的推荐首个操作

从这里开始：

```text
阅读 NEXT_WINDOW_HANDOFF_20260605.md 和 experiment-completion-plan.md。
然后仅运行代码就绪门槛。
暂不撰写论文。
```
