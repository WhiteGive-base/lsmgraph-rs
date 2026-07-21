# P10 SemL0 adapter

`seml0_adapter.py` 是 P10/P11 的 SemL0 正式入口。它只启动一次
`lsmgraph storage-bench`，由同一个 `Engine` 依次完成整条共享 trace 的
warmup 和 measured 阶段；不会把不同 edge type 拆成互相交错的预热/计时段。

支持的配置为：

- `naive`：`--l0-layout naive`；
- `schema`：`--l0-layout schema`；
- `b64` / `budg-b64`：`--l0-layout semantic-budgeted`，并提供 degree hint；
- `semantic`：`--l0-layout semantic`，并提供 degree hint。

正式模式在启动二进制前会 fail closed 地验证：P02B 对 P10 的 admission、
P02B result/validator SHA、当前 Git HEAD 与 clean tree、release binary、truth、
共享 sample plan、双向 ID map、P02B store manifest 与当前 store tree、P31
wrapper。二进制、truth、plan、store 任一 SHA 与 P02B provenance 不一致均拒绝
运行。运行结束后，orchestrator 还会把 `adapter-provenance.json` 与外层 P31
manifest 的 wrapper、binary、truth、Git SHA 和 store root 再交叉校验一次。

原始输出保存在每个 repeat 的 `adapter-output/seml0-raw/`；论文统计只消费经过
`p10_contract.py` 复核的 `adapter-result.json`、`query-observations.tsv`、
`phase-events.jsonl` 和 `adapter-provenance.json`。

`fixture` 模式仅用于契约/SF1 correctness smoke，不能进入正式图表。正式运行
必须使用 `--mode formal`，并且 P02B formal sentinel 必须由同一个 release binary、
truth、sample plan 和 store 生成。
