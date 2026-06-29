# SemL0 Lifecycle Paper 阅读包说明

本目录已整理成两个阅读入口：

- `en/`：英文原件拷贝，保留原始结构，适合对照最终投稿稿。
- `cn/`：中文阅读版，保留英文技术术语、代码符号、证据路径和关键数字，适合快速审稿式阅读。
- `cidr/`：CIDR 专用材料包，包含 CIDR 写法标准、SemL0 新定位、外部 baseline 调研、比较矩阵和远端原型部署证据。

## 建议阅读顺序

### 第一遍：抓故事

1. `cn/seml0-lifecycle-paper-draft-cn.md`
   只读 Abstract、Contributions、§1、§10。
2. `cn/sections/00-abstract-cn.md`
3. `cn/sections/01-introduction-cn.md`
4. `cn/sections/10-conclusion-cn.md`

读完这一遍，你应该只记住一句话：

> SemL0 把 property-graph query signature 变成 LSM 图存储的 lifecycle control signal：flush 生成 semantic pruning surface，read 用它剪枝，feedback/compaction 保留或重建它，schema/snapshot 规则保证不漏读。

### 第二遍：看三大贡献和数据

1. `cn/sections/04-query-semantic-pruning-surface-cn.md`  
   C1：剪枝面如何生成、怎么用、为什么安全。
2. `cn/sections/05-lifecycle-retention-cn.md`  
   C2：naive merge 如何毁掉 surface，semantic merge 如何保留/重建；重点看真实 SF30。
3. `cn/sections/06-schema-snapshot-correctness-cn.md`  
   C3：schema/snapshot/tombstone 下为什么不漏读。
4. `cn/sections/07-evaluation-cn.md`  
   看 Table 1、Table 2、C2 表。
5. `cn/tables/table-c2-retention-cn.md`
6. `cn/tables/plot-data-md-cn.md`

### 第三遍：按 SIGMOD 审稿视角挑刺

1. `cn/sections/09-limitations-cn.md`
2. `cn/sections/08-related-work-cn.md`
3. `cn/reviewer-question-bank-cn.md`
4. `cn/tables/external-baseline-md-cn.md`

## 我建议你重点盯的判断点

- C2 现在已经不只是 synthetic：真实 LDBC SF30 retention 已完成，1.09B edges，semantic retention 1.0 vs naive 0.0，write_amp 1.24 vs 1.07。
- 新增 real SF30 metadata read-amp proxy：40 个 typed-neighbor partitions 下，naive weighted candidate-byte read-amp = 6.52x，semantic = 1.00x；avg candidate segments/query = 93.6 vs 13.2。
- 但这个 proxy 不是完整 body-read workload。论文必须区分三层：synthetic full read workload、real SF30 retention/write cost、real SF30 metadata replay。
- External baseline 目前不能写成已完成数值对比。LiveGraph/Teseo/GraphOne/LLAMA 需要先通过 reproducibility gate；CIDR 版先写 qualitative comparison + artifact attempts。
- `semantic` full variant 的 RSS outlier 是危险点，主 claim 应放在 budgeted / retention / bounded cost。
- 投稿前还需要清理 citations、figure placeholders、cross references、路径和最终 TeX/PDF。

## 文件地图

### 中文入口

- `cn/seml0-lifecycle-paper-advisor-draft-cn.md`：导师审阅版中文 draft，重点写研究主线、证据、风险和希望导师判断的问题
- `cn/seml0-lifecycle-paper-draft-cn.md`：单文件中文阅读版
- `cn/reviewer-question-bank-cn.md`：审稿问题预案
- `cn/final-paper-outline-cn.md`：中文 outline
- `cn/sections/`：逐节中文阅读版
- `cn/tables/`：表格与画图数据中文说明

### CIDR 入口

- `cidr/README-CN.md`：CIDR 材料包入口
- `cidr/cidr-standards-and-exemplars-cn.md`：CIDR 标准与参考论文写法
- `cidr/seml0-cidr-positioning-cn.md`：SemL0 CIDR 新定位
- `cidr/external-baseline-survey-cn.md`：外部 baseline 调研
- `cidr/comparison-matrix-cn.md`：相关系统设计矩阵
- `cidr/deployment-evidence-map-cn.md`：远端原型部署证据地图

### 英文原件

- `en/seml0-lifecycle-paper-draft-md.md`
- `en/reviewer-question-bank.md`
- `en/final-paper-outline-md.md`
- `en/sections/`
- `en/tables/`
