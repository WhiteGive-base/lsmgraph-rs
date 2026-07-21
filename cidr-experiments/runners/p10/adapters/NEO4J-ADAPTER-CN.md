# P10 Neo4j Community adapter

`neo4j_adapter.py` 是 P10/P11 的真实 Bolt/Cypher client-server 入口。adapter
本身**不会**启动、停止、重启、导入或复制 Neo4j；正式 repeat 使用
`external-prestarted-query-process-lifetime-v1`，由 P31 覆盖 suite manifest 中声明的
同一个容器。

## 冻结查询与计时边界

- 关系类型映射固定为正类型 `E_P<type>`、负类型 `E_N<abs(type)>`；查询固定为
  `MATCH (s:V {id: $src})-[:TYPE]->(d:V) RETURN d.id AS dst`。
- 同一 adapter/driver/session 依次执行完整 truth warmup，再执行 measured；并发度固定为 1。
- 每个 query 用 Linux `CLOCK_MONOTONIC` 包住 `session.run`、完整 Bolt result
  materialization 和 `count/sum_hash/xor_hash` digest。Cypher transaction timeout 与契约中的
  `per_query_timeout_ms` 相同；正式结果允许的 timeout 仍为 0。
- 输出仍是统一的 `query-observations.tsv`、`phase-events.jsonl`、
  `adapter-result.json`；`adapter-provenance.json` 额外记录容器 ID、image ID/RepoDigest、
  Neo4j server agent、Python driver 版本及包树 SHA、三类 lineage manifest 和 P02B admission。

## 正式 fail-closed 绑定

正式查询发出前必须全部通过：

1. request 的 client-server `external_service` 只有一个 Neo4j 容器和一个 image digest；
   这两个字段直接来自 suite manifest，也正是 P31 收到的容器名。
2. 容器必须正在运行、`restart=no`、Bolt 只发布到 `127.0.0.1`、`/data` bind source
   等于 request 的 `neo4j-runtime` store，且默认数据库配置为 read-only。
3. image tag 固定为 `neo4j:5.26.24`，RepoDigest 固定为
   `sha256:f66304b9511c60d33555a2c451f88e03d82d1ebc893f32d84c98a6b326096435`。
   本机当前 image ID 为
   `sha256:ab41749b9bf44722570afadae444ffb0fc1e1f4bf04fb364326b6598bba8b87e`；正式身份以
   RepoDigest 为准。
4. Python Bolt driver 版本固定为 `5.28.3`；adapter 同时重算安装包文件树 SHA，不能只写版本字符串。
5. P02B dataset manifest、P01 truth、Neo4j runtime-store manifest 与 request 的 SHA/路径必须一致。
   store manifest 还会复验选定的不可变 sentinel files。
6. canonical P02B admission 必须是同一 Git HEAD、同一 lsmgraph release binary、同一主机，
   结果年龄不超过 21,600 秒，并且显式 `--require-formal` 放行 P10。

## SF10 store 的安全准备

历史 store
`baseline/external-baselines-20260626/3plus3-baselines/systems/neo4j/sf10-main/neo4j-data`
只作为冻结源，不能直接 mount 到容器。应先确认全新目标不存在，再把源 store reflink/copy
到 run-root 之外的独立 runtime 路径。源目录不支持 reflink 时，完整 copy 约需额外 16 GiB；
copy 前后都应独立校验文件数量和 SHA-256。

容器启动前，对**runtime copy**执行只读冻结工具：

```bash
python3 -B cidr-experiments/runners/p10/adapters/freeze_neo4j_store.py \
  --store-root /abs/new/neo4j-runtime \
  --dataset-manifest /abs/sf10-dataset-manifest.json \
  --truth /abs/truth-s50-seed42.tsv \
  --neo4j-version 5.26.24 \
  --image-digest sha256:f66304b9511c60d33555a2c451f88e03d82d1ebc893f32d84c98a6b326096435 \
  --sentinel databases/neo4j/neostore.nodestore.db \
  --sentinel databases/neo4j/neostore.relationshipstore.db \
  --assert-container-stopped cidr-p10-neo4j-sf10-r01 \
  --output /abs/manifests/neo4j-r01-store.json
```

该工具不会复制、删除或修改 store，也不会管理服务；输出必须位于 runtime store 之外。
随后预启动唯一容器，使用 `--restart no`、`-p 127.0.0.1:PORT:7687`、精确 tag、
`NEO4J_AUTH=none` 和
`NEO4J_server_databases_default__to__read__only=true`。容器名、RepoDigest、runtime store
lineage SHA 必须原样写入 suite manifest。

## Tiny 真实 fixture

下面的测试会用唯一容器、localhost 随机端口和 tiny offline import，验证真实 Bolt 查询、
统一输出契约、orchestrator/P31 bridge、image/driver tamper 以及缺少正式 P02B admission 时的
fail-closed 行为。测试数据明确 `performance_eligible=false`；结束时强制清理容器和临时 store。

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest -v \
  cidr-experiments/runners/p10/tests/test_neo4j_adapter.py
```

当前没有运行 SF10 正式 timing。正式前仍需：在最终 clean commit 上补跑同主机 P02B、为每个
repeat 创建并冻结独立 runtime copy、预启动对应容器，并让 P31/clean-window gate 通过。
