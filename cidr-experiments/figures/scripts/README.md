# Python plot scripts

- `plot_provisional_evidence.py`：从 `data/normalized/provisional/` 重建六组历史诊断图。
- `formal/plot_figure1_*.py` 至 `formal/plot_figure6_*.py`：六个正式绘图入口，命令和门槛见 `formal/README.md`。
- `figure_common.py`：统一配色、字体、输出格式和 layout 碰撞/越界检测。
- `validate_rendered.py`：将 PDF 回渲染为 PNG，检查页数、空白页和 ink margin。

正式入口对必需字段、单位、correctness、matched protocol、独立重复数和缺失/不支持值执行显式校验；旧 candidate 数据会被拒绝。
