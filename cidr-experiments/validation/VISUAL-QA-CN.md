# 六组 provisional 图视觉验收

验收日期：2026-07-21

## 自动验收

运行：

```powershell
python cidr-experiments/figures/scripts/validate_rendered.py `
  --input-dir cidr-experiments/figures/output/provisional `
  --render-dir cidr-experiments/validation/rendered-provisional
```

结果：六个 PDF 均为单页、非空，重新以 180 DPI 渲染成功；四边最小 ink margin 为 6 px，所有图的 `layout.json` 均为 `PASS`，没有 `<6 pt` 字体、文本越界或文本碰撞。机器报告：[`rendered-provisional/render-validation.json`](rendered-provisional/render-validation.json)。

## 逐图目视验收

| 图 | 裁切 | 标签/图例遮挡 | 颜色/打印语义 | 结论 |
|---|---|---|---|---|
| Fig.1 context | 无 | 系统名、P99 N/A、scatter direct labels 已错位排布 | SemL0 与历史外部点可区分 | PASS |
| Fig.2 layout | 无 | panel label 已与最高点留出 headroom | scale 同时用颜色与 marker | PASS |
| Fig.3 import resource | 无 | L0 file 数竖排在 bar 顶部，无越界 | variant 颜色固定 | PASS |
| Fig.4 compaction | 无 | 单一 legend，不覆盖轨迹；四 panel 对齐 | policy 同时用颜色/线型/marker | PASS |
| Fig.5 coverage | 无 | cell 数值、row/column label、colorbar 无重叠 | improvement=紫、regression=橙；two-hop regression 清楚 | PASS |
| Fig.6 two-point | 无 | shared legend 与数据分离 | variant 同时用颜色/线型/marker | PASS |

目视过程中实际修正过的问题：Fig.1 系统 tick/散点标签碰撞，Fig.2 panel label 与高值点过近，Fig.5 diverging colormap 方向，以及所有图底部 caveat 与页面边缘的安全间距。

## 边界

这里验收的是图形排版和信息编码，不把 provisional 数据提升为正式论文证据。正式数据补齐后必须重新执行同一套 layout + PDF render + 目视验收。

