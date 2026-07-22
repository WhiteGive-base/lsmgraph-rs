# P10 Neo4j Community 正式实验契约

`neo4j_adapter.py` 是 P10/P11 的 Bolt/Cypher 查询入口；`run_suite.py` 负责每个
repeat 的启动、P31 监测、优雅停止和停止后审计。正式流程不再依赖人工预启动容器。

## 一个 repeat 的固定顺序

1. suite manifest 预注册独立的 `repeat_root`、runtime store、logs、容器名、Bolt
   端口、`clone_id`、受控 import/store manifest，以及同一份 launcher 路径和 SHA-256。
2. P31 启动前，launcher 对 pristine source 和 runtime clone 各做完整内容树哈希与 stat 闭合；
   路径、大小、文件 SHA-256、树 SHA-256、文件数和总字节数必须完全相同，并拒绝任何
   shared inode/hard link。仅有路径和大小相同不能作为 clone 证明。三个 repeat 必须绑定
   同一个 source 路径和同一棵 source 内容树。
3. 容器尚未启动时，对 runtime store 做完整 pre-launch 树审计。正式 offline proof 不读取
   其他用户不可见的 `/proc`：store root 必须由当前 EUID/EGID 所有且精确为 `0700`，
   `databases/store_lock` 必须同 owner 且精确为 `0600`。审计在整个全树哈希期间持续持有
   `fcntl` 非阻塞独占锁，并在哈希前后复查 root/lock 身份及所有 running container 的
   overlapping bind mount；哈希读取后执行 `POSIX_FADV_DONTNEED`。pre 树必须与冻结
   manifest 完全一致，此阶段不允许 `mutable_deltas`。
4. `launch_neo4j_runtime.py launch` 只用预注册配置创建容器；`create` 必须返回完整 64-hex
   container ID，此后的 inspect/start/stop/rm 一律只使用该完整 ID，绝不按容器名执行
   生命周期操作。`create` 还必须显式传入与 `/data`、`/logs` owner 相同的 numeric
   `--user UID:GID`，并把 Docker `Config.User` 写入回执。它输出
   `p10-neo4j-runtime-launch-receipt-v2`，P10、adapter 和 P31 都
   绑定该回执中的实际 container ID、PID、`StartedAt` 和 `RestartCount`。
5. launch PASS 后才启动 P31；P31 READY 必须先观察到同一容器身份，再启动 adapter。
   adapter 完成 readiness、只读数据库、内存、JVM/GC、索引和 EXPLAIN 计划检查后，执行
   warmup 和 measured 查询。
6. P31 测量窗口结束后，无论 adapter/P31 是否 PASS，orchestrator 都调用
   `launch_neo4j_runtime.py stop`。正式 PASS 要求优雅停止、Neo4j exit code 0、
   `Running=false`、PID 0、`RestartCount=0`，再按完整 ID `rm` 并用精确名称过滤证明
   容器不存在；`p10-neo4j-runtime-stop-receipt-v2` 同时绑定原 launch SHA 和上述证据。
7. 只有 stop receipt 验证通过后，才执行严格 post-stop 全树审计。该审计再次在 owner-only
   root、独占 `store_lock` 和 Docker mount rescan 的同一保护区内完成，并绑定 stop
   receipt；不可变树 SHA、文件数和字节数必须与 pre 完全一致。

最终 `validated-result.json` 同时发布 pre audit、launch、P31/adapter、stop 和
post-stop audit 的路径与 SHA-256。缺少任何一环都不能发布正式 PASS。

## 容器、内存和查询的固定条件

- Neo4j Community：`neo4j:5.26.24`；RepoDigest 固定为
  `sha256:f66304b9511c60d33555a2c451f88e03d82d1ebc893f32d84c98a6b326096435`。
- Docker mount 只能有两个：runtime store 到 `/data`、独立 logs 到 `/logs`；二者均为
  当前 EUID/EGID 所有且 mode 精确为 `0700` 的 bind root。容器必须以相同 numeric
  `UID:GID` 运行。禁止额外/nested mount 和所有 `server.directories.*`、
  `dbms.directories.*` 覆盖。
- Bolt 只发布到 `127.0.0.1`；`restart=no`；Docker log driver 为 `none`；
  `Memory=0`、`MemorySwap=0`。这里的 0 表示没有额外 Docker cgroup 内存上限。
- Neo4j 预注册内存配置固定为 heap initial `8G`、heap max `8G`、page cache `16G`。
  adapter 用 `SHOW SETTINGS` 核对 live 值，并记录主机 `MemTotal`。这不是跨系统统一的
  “24G 总内存上限”，论文和图表中不能这样表述。
- 数据库 `neo4j` 必须 online、requested online 且 live access 为 read-only；
  `v_id` 必须是覆盖 `:V(id)` 的 ONLINE RANGE node index。
- 代表性 typed-neighbor 查询在 measured 前执行 `EXPLAIN`，计划必须包含绑定
  `v_id`/`:V(id)` 的 `NodeIndexSeek`。
- 容器内必须恰好观察到一个 JVM 后代，且 argv 显式包含 `-XX:+UseG1GC`；查询前后
  JVM PID 和完整 argv SHA 不得变化。
- Python driver 固定为 `neo4j==5.28.3`，正式绑定整个安装包树 SHA-256
  `77c6f87a8f944f08431f943bda42e6df5d99f6f898ae10f9a4cdd7f33899eb50`，不能只记录版本号。

查询形状固定为：

```cypher
MATCH (s:V {id: $src})-[:E_{P|N}<type>]->(d:V)
RETURN d.id AS dst
```

同一 adapter/driver/session 先完整 warmup，再执行 measured；并发度为 1。计时覆盖
`session.run`、结果完整 materialization 和 `count/sum_hash/xor_hash`，正式结果允许的
timeout 和 correctness mismatch 均为 0。

## 正式 store 的准备

历史 SF10 store 只能用于 `performance_eligible=false` 的诊断，不能通过补一个新 receipt
升级为正式数据。正式 store 必须从空目录经受控 importer 产生：

```bash
/usr/bin/python3.8 -B import_neo4j_store.py \
  --store-root /abs/p10/repeat-01/neo4j-runtime-copy \
  --import-root /abs/p02b/neo4j-import \
  --logs-root /abs/p10/import-logs-r01 \
  --dataset-manifest /abs/p02b/sf10-dataset-manifest.json \
  --image-ref neo4j:5.26.24 \
  --image-digest sha256:f66304b9511c60d33555a2c451f88e03d82d1ebc893f32d84c98a6b326096435 \
  --container-prefix cidr-p10-neo4j-import-r01 \
  --output /abs/p10/repeat-01/controlled-import-receipt.json
```

import 完成且所有相关容器停止后，冻结完整 v3 store manifest。

受控 importer 输出 `p10-neo4j-controlled-import-receipt-v3`。两个 import 阶段都必须从
`create` 捕获完整 64-hex ID，后续 start/stop/rm 仅按该 ID 操作，并保存正常删除与精确
名称不存在的证据。两个阶段都显式使用 `--user UID:GID`，receipt 冻结并复验实际
`Config.User`。冻结前必须确认运行用户拥有 store、logs root 与 `store_lock`；importer 会将
root/lock 规范化为 `0700`/`0600`，任何 owner、mode 或 container user 不满足都会 fail closed：

```bash
/usr/bin/python3.8 -B freeze_neo4j_store.py \
  --store-root /abs/p10/repeat-01/neo4j-runtime-copy \
  --dataset-manifest /abs/p02b/sf10-dataset-manifest.json \
  --truth /abs/p01/truth-s50-seed42.tsv \
  --neo4j-version 5.26.24 \
  --runtime-image-ref neo4j:5.26.24 \
  --runtime-image-digest sha256:f66304b9511c60d33555a2c451f88e03d82d1ebc893f32d84c98a6b326096435 \
  --import-receipt /abs/p10/repeat-01/controlled-import-receipt.json \
  --import-receipt-sha256 REPLACE_IMPORT_RECEIPT_SHA256 \
  --assert-container-stopped cidr-p10-neo4j-sf10-r01 \
  --output /abs/p10/repeat-01/store-manifest-v3.json
```

当前 v3 lineage 要求 controlled import receipt 的 `final_store.root` 就是该 repeat 的
runtime store，因此模板按三个 repeat 分别登记三个受控 import receipt。若要改成“一次
import 后复制三份”，必须先实现并审计独立 clone producer/receipt，再扩展 store provenance；
仅凭 copy 命令或 launcher 的 no-hardlink/stat 检查不能冒充 import lineage。

`formal-system.template.json` 中所有 `/ABS/...`、`REPLACE_*` 都必须替换。三个 repeat 的
store、logs、repeat root、clone ID、容器名、Bolt 端口、store manifest 和 import receipt
必须独立；`source_store_root` 可指向同一只读 pristine 对照树，但它必须与每个 runtime
store 的完整相对路径集合、文件大小和逐文件 SHA-256 完全一致，且不能共享 hard link
inode。source/runtime 的所有大文件哈希都在 P31 启动前完成；P31 与 adapter 只消费已封存
receipt，不会再次读取或哈希 nodes/relationships CSV。

## 测试边界

纯 contract/mock 测试不会启动 Docker，也不会读取 SF10 大文件：

```bash
cd cidr-experiments/runners/p10
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3.8 -m unittest -v \
  tests.test_neo4j_hardening \
  tests.test_neo4j_runtime_lifecycle \
  tests.test_neo4j_sealed_receipts \
  tests.test_p31_consumer_contract \
  tests.test_p10_orchestrator
```

旧 `test_neo4j_adapter.py` 仍是 v2 freezer + 人工 `docker run` 的真实 tiny fixture，已经用
`unittest.skip` 隔离，不能作为 v3 lifecycle 证据，也不会再被误执行。若以后恢复真实 tiny
集成测试，必须改成受控 import + launch/stop receipt 的完整路径。此前误触发记录见
`neo4j/NONFORMAL-TEST-NOTE-CN.md`。

在合入 main 后、读取或运行 SF10 前，必须先用官方 `neo4j:5.26.24` RepoDigest 跑一次真实
tiny 受控 import → launch → readiness → stop → remove → post-stop audit gate，确认 numeric
`--user UID:GID` 与官方 entrypoint、`/data`、`/logs`、store lock 及优雅停止完全兼容。该 gate
没有真实 PASS receipt 时不得启动 SF10；本次纯 contract 修复不启动 Docker。
