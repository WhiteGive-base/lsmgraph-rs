# Short clean-window / batch lease v2 接入说明

本接入把一次正式批次的放行链固定为：P03 `5 × 60 s` clean window → P02B
稳定性 sentinel → 24 小时 batch lease → P10/P20 每个 repeat 的 P31 连续 integrity
guard。lease 只是 admission，不是性能结果，也不替代 repeat 期间的监控。

## 协议选择

- v2 P02B 使用 `p02b/configs/sf10-seml0-short-gate-v2.json`，并显式传入
  `--batch-gate-tool` 与绝对路径 `--batch-lease-output`。
- P10 正式 v2 使用 `--batch-lease` 与 `--batch-gate-tool`；旧路径必须显式使用
  `--legacy-v1-clean-ready`（`--clean-ready-file` 仅保留兼容别名）。
- P20 正式 v2 使用 `--batch-lease` 与 `--batch-gate-tool`；旧 6 小时 P02B freshness
  路径必须显式使用 `--legacy-v1-admission`，且同时传入旧 result 与 SHA-256。
- P31 的四个 batch 参数必须全有或全无：lease、gate tool、consumer、anchor binary。

所有 consumer 都只接受仓库内 canonical `batch_gate_v2.py`。混合 v1/v2 参数、缺失
marker、过期 lease、host/HEAD/dirty/binary 变化、P02B PASS marker/provenance 变化均
fail closed。一次 Figure 2 聚合批次也禁止混合两种 admission protocol。

## 双 READY 与首尾覆盖

v2 P31 同时等待 resource collector 与 integrity guard 各自产生 READY 后才释放 benchmark
命令。命令释放时间写入 `command-release.json`。validator 要求：

1. collector READY 与 guard READY 都不晚于 command release；
2. command end 不晚于 guard end，保证最后一个 guard sample 覆盖命令尾部；
3. guard 每秒复验 lease、repo HEAD/clean、anchor binary 与已知污染进程；
4. sample gap 不超过 3 秒；
5. READY/status/samples/release 的 SHA-256 被 P31 validation、DONE 及 P10/P20
   provenance 再次封存。

## P02B PASS 与 lease 发行失败

P02B 本体成功后先写 `PASS`。若后续 lease 发行失败，runner 保留该 `PASS`，写
`BATCH-LEASE-FAILED.json` 并返回退出码 `3`。这表示“P02B 事实成立、批次未放行”；
P10/P20 没有有效 lease 与 lease marker 时必须拒绝启动。不得把这个状态改写成 P02B
`FAILED`，也不得仅凭保留的 P02B `PASS` 绕过 v2 admission。

## 离线验证

以下测试不运行 benchmark：

```bash
(cd cidr-experiments/runners && python3 -B -m unittest -v tests.test_batch_gate_v2)
(cd cidr-experiments/runners/p02b && python3 -B -m unittest discover -s tests -p 'test_*.py' -v)
(cd cidr-experiments/runners/p10 && python3 -B -m unittest -v tests.test_batch_lease_v2)
(cd cidr-experiments/runners/p20 && python3 -B -m unittest -v test_aggregate_figure2)
(cd cidr-experiments/runners/p20 && python3 -B -m unittest -v test_run_single_profile.AdmissionProtocolUnitTest)
(cd cidr-experiments/runners/p31 && python3 -B -m unittest -v tests.test_batch_integrity_v2)
bash -n cidr-experiments/runners/p31/run_with_resources.sh
bash -n cidr-experiments/runners/p10/run_adapter_with_p31.sh
```
