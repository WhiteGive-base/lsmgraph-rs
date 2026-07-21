# P02B SF10 sentinel runs

本目录只保存 P02B clean-window 放行结果。实现、配置、正式调用方式和 fail-closed
契约见 `../../../runners/p02b/README-CN.md`。

所有真实 attempt 写入 `raw/<run-id>/`。只有 `sentinel-result.json` 与 `PASS` 的
SHA-256 相互绑定、且
`validate_sentinel_result.py --consumer P10|P20 --require-formal` 返回 0 时，才能启动
下游正式实验。`FIXTURE-PASS` 永远不能放行正式实验。
