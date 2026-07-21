# P10 TuGraph 真实适配器

## 已冻结的执行口径

本目录实现 TuGraph 4.5.2 的真实 embedded P10 入口。正式运行不连接、复用、启动或停止任何 `lgraph_server`：P10 adapter 只派生一个原生 worker，worker 在同一进程中依次完成一次完整 warmup trace 和一次完整 measured trace。

每条查询的 `CLOCK_MONOTONIC` 区间包含：创建只读事务、按 dense VID 定位源点、按 edge label ID seek 出边、完整物化所有目标 dense VID、计算 `mix64-dense-dst-count-sum-xor-v1`、中止只读事务。Galaxy/GraphDB 打开和 34 个 edge label ID 的解析属于 setup，不计入逐查询区间。

正式 truth 固定为 1700 条有序查询，SHA-256 为：

```text
876ec4be45bb7c220ce595b8d8efd5c79d13db285569cc0d6ae3e8bc19a1c788
```

edge type 必须由 `sf10-edge-type-labels.tsv` 精确映射为 34 个 TuGraph label。store 还必须保证 `internal VID == dense ID`；适配器不做运行时重映射。

## 为什么使用本地镜像编译、但不在容器内运行

服务器上的 TuGraph 4.5.2 C++ ABI 与宿主 GCC 9 编译结果不兼容：GCC 9 产物进入 `Galaxy` 构造函数即 SIGSEGV。已验证的路径是使用服务器已有、digest-pinned 的 Ubuntu 18.04 镜像内 `/usr/local/bin/g++`（GCC 8.4）离线编译，然后在宿主原生执行 worker。

构建过程强制 `--pull=never`、`--network none`、只读输入挂载、唯一临时容器名和 `--rm`。镜像只参与构建，不参与 P10 运行，因此 formal system 的 `containers` 和 `image_digests` 均为空；P31 统计 adapter 根进程及其 worker 子进程。runtime manifest 另外记录构建镜像 repo digest、image ID、容器内编译器 SHA、TuGraph/Boost/date/compat headers、`liblgraph.so`、Docker client、源码和完整编译参数。

```bash
python3 -B cidr-experiments/runners/p10/adapters/tugraph/build_tugraph_worker.py \
  --output /ABSOLUTE/ARTIFACTS/tugraph-p10-worker \
  --runtime-manifest /ABSOLUTE/ARTIFACTS/tugraph-runtime-manifest.json
```

默认构建镜像固定为：

```text
tugraph/tugraph-runtime-ubuntu18.04@sha256:b492ccc83f866170b1fb43da5fd89e0ea43199a3144e9faee5b0c02b167bcee5
```

构建器只接受本地已有的 repo-digest 引用，不会拉取镜像。

## store manifest 与登录凭据

先由 P02B 的 `build_lineage_manifest.py --kind store` 对不可变 TuGraph store 生成树哈希，再生成本适配器的 store manifest：

```bash
python3 -B cidr-experiments/runners/p10/adapters/tugraph/make_store_manifest.py \
  --store /ABSOLUTE/TUGRAPH_SF10_STORE \
  --store-lineage /ABSOLUTE/p02b-store-lineage.json \
  --dataset /ABSOLUTE/dense-edges.txt \
  --dataset-sha256 DATASET_SHA256 \
  --truth /ABSOLUTE/truth-s50-seed42.tsv \
  --truth-sha256 876ec4be45bb7c220ce595b8d8efd5c79d13db285569cc0d6ae3e8bc19a1c788 \
  --edge-labels cidr-experiments/runners/p10/adapters/tugraph/sf10-edge-type-labels.tsv \
  --importer /ABSOLUTE/FROZEN_IMPORTER \
  --import-config /ABSOLUTE/FROZEN_IMPORT_CONFIG \
  --source-revision REVISION \
  --vertex-count VERTEX_COUNT \
  --password-sha256 SHA256_OF_PASSWORD \
  --output /ABSOLUTE/ARTIFACTS/tugraph-store-manifest.json
```

密码不进入 argv、JSON 或日志。运行前由环境变量 `CIDR_TUGRAPH_PASSWORD` 提供；adapter 先与 store manifest 中的密码 SHA-256 比对，再让 worker 继承。用户名默认是 `admin`，也被 store manifest 冻结。

## fail-closed 边界

- formal request 只能使用权威 1700-row truth SHA；fixture truth 不能冒充 formal。
- fixture 必须声明 `fixture-process-lifetime-v1`；formal 必须声明
  `prebuilt-store-query-process-lifetime-v1`，并在同一个 native worker
  进程内依次完成 store open、warmup 和 measured trace。
- request 的 `runtime_libraries` 必须包含 runtime manifest 中同路径、同
  SHA-256 的 `liblgraph.so`；adapter provenance 会再次回写并交叉校验。
- formal store 必须 `formal_eligible=true`，且 dataset/store/binary SHA 必须与 P10 request 完全一致。
- formal 必须携带 P02B result、validator 和 validator SHA，并通过 `--consumer P10 --require-formal`；fixture 禁止携带 P02B 声明。
- 每条查询超过 deadline 会记录 `timeout`；formal 的 `max_timeouts=0`，因此任何 timeout 都拒绝发布。无法从库调用内部安全抢占的永久阻塞由 P10 adapter 整体 timeout 与 P31 root-process 作用域终止并拒绝。
- worker 只以 `Galaxy(... durable=false, create_if_not_exist=false)` 和 `OpenGraph(... read_only=true)` 打开 store。
- P31 使用 `service_lifecycle=in-process`、`containers=[]`、`extra_pids=[]`；worker 是 adapter 的直接子进程，不能绕过 P31。

## 隔离正确性测试

以下测试只在临时目录构造 4 点、102 边的小图，不进行 SF10 性能计时，也不接触已有 TuGraph 服务或数据库：

```bash
python3 -B cidr-experiments/runners/p10/tests/tugraph/test_tugraph_adapter.py -v
bash cidr-experiments/runners/p10/tests/run_tests.sh
```

第一条验证真实 TuGraph API 的 1700 warmup + 1700 measured、逐查询 truth/digest/latency、runtime/store lineage 和 P02B fail-closed；第二条验证共享 P10/P31 orchestrator 契约。
