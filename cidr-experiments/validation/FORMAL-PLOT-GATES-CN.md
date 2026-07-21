# 正式绘图入口门槛验收

验收日期：2026-07-21

- `python -m compileall -q cidr-experiments/data cidr-experiments/figures/scripts`：PASS。
- Fig.1–Fig.6 六个正式入口执行 `--help`：全部 PASS，依赖和模块导入正常。
- 分别将六份历史 candidate TSV 送入对应正式入口：六个入口均以退出码 `2` 拒绝，且未创建 formal 输出目录。
- 冻结契约：`plan/FIGURE-DATA-REQUIREMENTS.tsv`，128 个字段行、11 列。
- matched provenance 正负 fixture：PASS。Fig.1 全 campaign 锁 host，并按底层 engine 锁 Git/binary/system version；Fig.2/3/5 全 campaign 锁 host/Git/binary；Fig.6 在拆分 E07/E08 前锁整图 system/host/Git/binary。
- Fig.2 staircase fixture：严格 JSON 解析以及逐步 `+1 switch` 正例 PASS；移除、替换、一次增加多个开关和非法 JSON 均按预期拒绝。
- 通用 validator fixture：F4 合法多窗口和 F5 三种条件 grain 通过；重复/重叠窗口、双 grain、缺 denominator、跨 cell 欠 repeats 均按预期拒绝。

该负向测试证明旧 evidence/candidate 不会因为“有数值”而被误画成正式论文图。正式数据必须补齐契约要求的 provenance、matched protocol、correctness digest、零 mismatch 和独立 repeats 后才会通过。
