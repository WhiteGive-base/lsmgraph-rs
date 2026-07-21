# Figures

所有图由 `scripts/` 中的 Python 文件从 `../data/normalized/` 生成，同时输出：

- PNG：快速审阅和对话展示；
- SVG：可编辑矢量版本；
- PDF：论文插图版本。

每张图必须通过 `validation/` 中的数据检查和视觉 QA；不得在脚本中硬编码实验数据。

- `scripts/plot_provisional_evidence.py`：从旧证据生成六组明确标记的诊断图。
- `scripts/formal/`：正式 Fig.1–Fig.6 绘图入口；数据不满足 digest、matched protocol 或 repeats 时拒绝绘图。
- `output/provisional/`：当前已生成并验收的 PNG/SVG/PDF/layout JSON，不应直接替换论文正式结果图。
