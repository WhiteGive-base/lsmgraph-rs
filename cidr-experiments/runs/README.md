# New Runs

需要补跑的实验按 `<experiment-id>/<campaign-id>/` 归档。每个 campaign 至少保存：配置、Git SHA、binary SHA-256、数据/query truth SHA-256、命令、重复编号、correctness 结果、资源采集和远端 raw 路径。

当前服务器尚未开始新的正式 benchmark。

共享负载下允许执行的 correctness-only 命令与验收 gate：[`P02-SF1-CORRECTNESS-GATE/README-CN.md`](P02-SF1-CORRECTNESS-GATE/README-CN.md)。这些 run 必须标记 `performance_eligible=false`。
