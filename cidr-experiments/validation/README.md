# Validation

- `VISUAL-QA-CN.md`：六组 provisional 图的 layout、PDF 回渲染和逐图目视验收。
- `FORMAL-PLOT-GATES-CN.md`：六个正式绘图入口的编译、导入和拒绝旧 candidate 数据测试。

验收分三层：

1. **Data gate**：来源、版本、workload、重复和 correctness 是否满足复用条件。
2. **Semantic gate**：图的比较是否 apples-to-apples，caption 是否越过原型边界。
3. **Visual gate**：PNG 与 PDF 回渲染版本均无文字裁切、图例/标签遮挡、panel 重叠、字体缺失或两栏尺寸不可读。

当前 provisional 验收汇总写入 [`VISUAL-QA-CN.md`](VISUAL-QA-CN.md)；正式数据补齐后另生成 final QA，不覆盖 provisional 记录。
