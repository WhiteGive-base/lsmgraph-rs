# Normalized plot data

- `provisional/`：从历史证据确定性重建、保留 caveat 的诊断 TSV；可画 provisional 图，但不满足正式论文门槛。
- `candidates/`：逐 metric 的旧证据候选，用于字段映射和补跑设计；不可直接绘制正式图。

未来通过冻结契约的数据再进入单独的 formal campaign 目录。数值使用基础单位或在列名中显式写单位，且必须能回溯到 run manifest 和原始字段。
