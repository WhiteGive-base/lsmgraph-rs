# P31 正式资源采集器

本目录提供一个可复用的外层 runner，用于在不修改 benchmark 程序的前提下，同时采集整个进程树、主机、NVMe 设备和存储目录的资源数据。输出是固定 schema 的 TSV 和带 SHA-256 的 run manifest，可直接作为后续统计/画图的原始输入。

## 最小调用契约

P20 或其他实验 runner 只调用 `run_with_resources.sh`，不要单独启动 collector 或手工创建 `DONE`：

```bash
cidr-experiments/runners/p31/run_with_resources.sh \
  --run-dir /data/WorkSpace/results/P31/example-r1 \
  --task-id P31-example-r1 \
  --performance-eligible false \
  --repo-root /data/WorkSpace/lsmgraph-rs \
  --device nvme1n1 \
  --data-mount /data \
  --interval 1 \
  --disk-interval 15 \
  --min-samples 2 \
  --store lsm=/data/WorkSpace/results/stores/example-r1 \
  --temp spill=/data/WorkSpace/results/tmp/example-r1 \
  -- ./target/release/benchmark --config config.toml
```

`--run-dir` 必须是绝对路径，并且不存在或为空目录。`--store LABEL=PATH` 至少一个，标签在同一角色内不能重复。多个 store/temp 根目录必须互不重叠；manifest 会直接拒绝相同或嵌套根目录，避免聚合峰值重复计数。

如果 workload 还会启动脱离主进程组的服务，使用可重复的 `--extra-pid PID`；如果服务在 Docker 容器中，使用可重复的 `--container NAME`。collector 会定期解析容器 init PID，并将它们的后代进程纳入聚合。

对在本次 run 之前已启动的 extra/container 服务，CPU 和 `/proc/io` 以首次观测值为零基线，不会将服务的历史累计量算入本次实验。未能解析的容器或从未观测到的 extra PID 会使 validator 失败。

## 正式运行

论文数据必须使用 `--performance-eligible true`，且不能使用 `--allow-missing-aux-tools`。正式运行需要：

- Git worktree 在启动时必须干净，并记录 HEAD SHA 和主机指纹。
- `--binary`、`--dataset`、`--config` 都必须是存在的路径并有 SHA-256。文件会自动哈希；大型目录应传入预先固化的 `--*-sha256` 数据版本哈希。
- 若使用 truth 或 query/trace，通过 `--truth` 或 `--query-or-trace` 声明；目录同样需要显式 SHA-256。
- 建议在释放其他重负载之后运行，固定 CPU/NUMA 和缓存策略，并且每个正式任务独占目标数据盘。主机 load/memory 会被记录供审计，但不会“校正”受干扰的性能数据。

示例：

```bash
cidr-experiments/runners/p31/run_with_resources.sh \
  --run-dir /data/WorkSpace/results/P31/formal-r1 \
  --task-id P31-formal-r1 \
  --performance-eligible true \
  --repo-root /data/WorkSpace/lsmgraph-rs \
  --store lsm=/data/WorkSpace/results/stores/formal-r1 \
  --binary ./target/release/benchmark \
  --dataset /data/datasets/ldbc-sf10 --dataset-sha256 HEX64 \
  --query-or-trace /data/traces/sf10.tsv \
  --config configs/formal.toml \
  -- ./target/release/benchmark --config configs/formal.toml
```

## 输出与固定 schema

- `resource-samples.tsv`：进程树 PID 集合及数量、user/sys CPU 累计量与区间 CPU%、RSS/PSS、`/proc/<pid>/io` 累计量与带宽、host load/memory/swap、`/proc/diskstats` 的 IOPS/带宽/await/util，以及数据挂载点剩余空间。
- `disk-samples.tsv`：每个扫描轮次下的 store/temp 根目录总大小、文件数、扫描完整性，以及互斥的 payload/metadata/catalog/sidecar/manifest/WAL/temp/other 分项。
- `iostat-samples.tsv`：从原始 iostat 流规范化的 NVMe IOPS、MiB/s、read/write/combined await、queue 和 util。
- `pidstat.raw` 与 `iostat.raw`：固定 C locale 的独立审计流；pidstat 保留 root task 及其已回收子进程统计，权威的实时进程树聚合值来自 `/proc`。
- `run-manifest.json`、`execution.json`、`collector-status.json`、`validation.json`：运行身份、命令/采集器状态、哈希和汇总值。

列名和版本只在 `resource_schema.py` 定义；任何列缺失、顺序改变、根目录扫描不完整、正式 run 中的 RSS/PSS/进程 I/O 不可读、缺少设备 delta 或 iostat/pidstat 会使 validator 失败。manifest 还会记录 sysstat 版本、NVMe 型号/队列参数和数据挂载信息。

## 安全与失败语义

wrapper 用新 process group 启动命令，中断时只终止该命令组；collector 通过 run 目录中的停止标记正常收尾。只有 `validate_resource_run.py` 有权创建 `DONE`。命令非零退出、collector 异常、schema/哈希/来源门控失败都会产生 `FAILED`，删除旧 `DONE`，并让 wrapper 非零退出，不会伪造 PASS。

资源采集的主要开销是每个进程的 `smaps_rollup` 读取和目录全量扫描。默认进程采样 1 s、目录扫描 15 s；大型多文件 store 可在预试后将 `--disk-interval` 调高到 30–60 s，但同一图中的各系统必须使用相同间隔。

## 自测

```bash
cd cidr-experiments/runners/p31
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py' -v
bash -n run_with_resources.sh tests/smoke_resource_collector.sh
tests/smoke_resource_collector.sh
```

### 外部容器身份与启动门控

指定 `--container NAME` 时，collector 会在第一个资源样本中为容器进程树建立零基线，并将
`container_id`、正整数 `pid`、`started_at` 和 `restart_count` 原子写入
`collector-ready.json`。wrapper 只有看到该文件后才会 `exec` 正式命令；默认等待上限为
60 秒，可用 `--collector-ready-timeout` 调整。首样本未覆盖容器 PID、容器无法解析或 collector
提前退出时，正式命令不会被释放。

`collector-status.json` 保留每次身份解析历史和按首次出现排序的唯一身份集合。validator 要求
每个请求容器在全程只有一个身份，且 `containers_seen` 中的 PID 为正整数并真实出现在
`resource-samples.tsv`。最终 `run-manifest.json` 的 `collector_result` 会复制 ready 信息、身份历史、
唯一集合及 ready/status artifact 的 SHA-256，供 P10 做端到端交叉绑定。

smoke 只运行数秒的小型 Python fixture，不启动任何正式 benchmark。

### Short clean-window v2 integrity guard

传入 `--batch-lease`、`--batch-gate-tool`、`--batch-consumer` 和
`--batch-anchor-binary` 时四项必须同时存在。wrapper 并行启动 resource collector 与
batch integrity guard；只有两者都写 READY，benchmark 命令才被释放。释放边界写入
`command-release.json`，命令结束后先记录 `command_ended_at_utc`，再让 guard 采集并
封存最后状态。

validator 要求 collector/guard READY ≤ command release ≤ command end ≤ guard end，
并调用 canonical gate tool 离线重验 guard samples。lease、gate、anchor binary、READY、
status、samples 与 release 的 SHA-256 全部进入 `validation.json`、manifest summary 与
`DONE` hash 链。任一 gap、污染、HEAD/dirty/binary/expiry 漂移，或缺少首尾覆盖，都会
删除 `DONE` 并 fail closed。没有四项 batch 参数时保持 legacy P31 行为，且拒绝出现未声明
的 guard/release evidence。
