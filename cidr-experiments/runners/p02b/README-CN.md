# P02B：SF10 clean-window sentinel

本目录把 P03 的主机静默窗口、P01 的 shared truth/ID bridge、三次独立进程
SF10 试跑和 P31 资源采集绑定成一个 fail-closed 放行门。它本身始终
`performance_eligible=false`，只决定 P10/P20 能否开始；sentinel 数值不能进入论文性能图。

## 放行条件

只有以下条件全部成立，runner 才写 `PASS` 和 `sentinel-result.json`：

1. P03 `READY`、`COMPLETE`、`STATE`、`samples.tsv`、`latest.tsv`、
   `classification.env` 和 monitor 脚本相互一致，monitor 脚本 SHA-256 未变，最后至少
   15 个样本连续通过，且 `READY` 不超过 300 秒；
2. Git worktree clean；binary、truth、query plan、config、dataset/store manifest、
   P31 wrapper 和 ID-map manifest 的 SHA-256 在开始与结束时一致；
3. `shared-truth-verify` 检查 1,700 个查询、0 mismatch，并重新生成 query plan；
   新 plan 必须与预先冻结的 plan 字节级 SHA-256 相同；
4. 三个独立 `storage-bench` 进程顺序执行。每个进程使用同一个 cpuset、线程数、
   warmup、cache policy、store 和 query plan，并由自己的 P31 sidecar 采集；
5. run-level QPS 使用总 operations / query elapsed time；P99 从所有 entry/round 的
   storage latency histogram 合并后计算。CV 定义为 sample standard deviation / mean；
   QPS CV `<=3%` 且 P99 CV `<=5%`。

任一命令、truth、P31、hash、schema、运行时长或 CV gate 失败，只写 `FAILED`，不会留下
`PASS`。runner 只会在超时时终止自己创建的进程组，不检查、停止或修改其他用户进程。

## 正式准备

正式运行必须使用已经 commit 的 clean `codex/cidr-sentinel` worktree，并在该 commit 上
构建 release binary。当前配置在 `configs/sf10-seml0.json`，冻结：

- housekeeping CPU：`0-15,64-79`；formal CPU：跨两个 NUMA 节点的 48 个物理核；
- `RAYON_NUM_THREADS=48`，blocking I/O，schema sentinel store；
- 不 drop OS page cache；每个 repeat 新建进程，在进程内做 1 次 warmup；
- 3 个独立 repeat、每个 query plan 10 次 measured repeat，且每个 run 的累计 query
  elapsed time不得少于 30 秒；
- P31 1 秒采样、15 秒目录采样，必须有 pidstat/iostat。

先生成不可变 dataset/store 的 canonical tree manifest。该操作读取完整目录，属于
G1-I/O，不得与正式 timing 并发：

```bash
python3 cidr-experiments/runners/p02b/build_lineage_manifest.py \
  --kind dataset --root /data/WorkSpace/ldbc-sf10/social_network \
  --output /data/WorkSpace/results/P02B/sf10-dataset-manifest.json

python3 cidr-experiments/runners/p02b/build_lineage_manifest.py \
  --kind store --root /data/WorkSpace/lsmgraph-rs/store/w6-sf10-priority-20260709/schema \
  --output /data/WorkSpace/results/P02B/sf10-schema-store-manifest.json
```

使用 P01 consumer 在非正式准备目录生成 shared query plan；P02B 会再次生成并比较 hash：

```bash
target/release/lsmgraph --io-backend blocking shared-truth-verify \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/w6-sf10-priority-20260709/schema \
  --truth-tsv /data/WorkSpace/lsmgraph-rs/baseline/external-baselines-20260626/3plus3-baselines/systems/neo4j/sf10-main/workload/truth-s50-seed42.tsv \
  --id-map-dir /data/WorkSpace/lsmgraph-rs/cidr-experiments/artifacts/id-maps/P01-IDMAP-20260721T171325Z-187e851 \
  --expected-queries 1700 --l0-layout schema --semantic-degree-hint \
  --sample-plan-out /data/WorkSpace/results/P02B/sf10-shared-truth-plan.json \
  --output /data/WorkSpace/results/P02B/sf10-plan-preflight.json
```

## P03 READY 后运行

显式调用下面命令；runner 不会在后台自行启动 benchmark：

```bash
run_id="P02B-SF10-SENTINEL-$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short=12 HEAD)"
python3 cidr-experiments/runners/p02b/run_sf10_sentinel.py \
  --run-dir "$PWD/cidr-experiments/runs/P02B-SF10-SENTINEL/raw/$run_id" \
  --clean-ready /ABS/P03-RUN/READY \
  --repo-root "$PWD" \
  --binary "$PWD/target/release/lsmgraph" \
  --dataset-manifest /data/WorkSpace/results/P02B/sf10-dataset-manifest.json \
  --store /data/WorkSpace/lsmgraph-rs/store/w6-sf10-priority-20260709/schema \
  --store-manifest /data/WorkSpace/results/P02B/sf10-schema-store-manifest.json \
  --truth /data/WorkSpace/lsmgraph-rs/baseline/external-baselines-20260626/3plus3-baselines/systems/neo4j/sf10-main/workload/truth-s50-seed42.tsv \
  --query-plan /data/WorkSpace/results/P02B/sf10-shared-truth-plan.json \
  --id-map-dir /data/WorkSpace/lsmgraph-rs/cidr-experiments/artifacts/id-maps/P01-IDMAP-20260721T171325Z-187e851 \
  --config "$PWD/cidr-experiments/runners/p02b/configs/sf10-seml0.json"
```

P10/P20 在启动前必须显式消费并校验结果：

```bash
python3 cidr-experiments/runners/p02b/validate_sentinel_result.py \
  --result /ABS/P02B-RUN/sentinel-result.json --consumer P20 --require-formal
```

fixture 会生成 `FIXTURE-PASS`，永远不能通过 `--require-formal`。

## 预计时间

- P03 从第一个干净样本到 `READY`：约 15 分钟（15 个、60 秒间隔）；
- P02B SemL0 sentinel：协议下限为 3×30 秒 measured query time，加 shared-truth、warmup、
  P31 收尾和目录扫描，预计约 8–20 分钟；
- 因此清场后的最短放行链约 23–35 分钟。首次真实 run 若 10 repeats 未达到每 run 30 秒，
  会 fail closed；应在非正式 pilot 后冻结更高 repeat 数并重新 commit，而不是运行时放宽。

## 测试

```bash
cd cidr-experiments/runners/p02b
tests/run_tests.sh
```

测试只运行一个两秒级 fake binary，完成三次独立假 repeat、真实 P31 sidecar、CV、marker
tamper 和 fixture-as-formal 拒绝测试；不构建或运行 lsmgraph benchmark。
