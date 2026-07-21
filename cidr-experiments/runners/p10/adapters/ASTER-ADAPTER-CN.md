# P10 Aster/RocksGraph 正式 adapter

本目录的 `aster_adapter.py` 与 `aster_p10_worker.cc` 直接调用冻结 Aster
源码中的 `rocksdb::RocksGraph`：建库使用 `AddVertexWithEdges`，查询使用
`GetAllEdges`。它不是结果回放器，也不调用旧的汇总 JSON。

## typed-neighbor bridge

Aster 的公开邻接接口没有 edge-type 参数。worker 按 dense edge-list 中首次出现的
顺序，为每个 `(edge_type, src)` 分配一个连续 RocksGraph vertex ID，并将真实 dense
`dst` 存入该逻辑顶点的出邻接表。映射固化为：

```text
p10-aster-key-map.tsv
```

因此正负 edge type 都保留，查询输出仍使用 P01 的
`mix64-dense-dst-count-sum-xor-v1`。计时边界从 key-map lookup 开始，覆盖
`GetAllEdges`、完整结果物化和 digest；每个 query 单独记录 `CLOCK_MONOTONIC`
延迟。一次 native worker 进程严格先跑完整 warmup，再跑完整 measured，查询顺序
与 1,700 行 truth 完全一致。

## 生命周期

- `fresh`：只允许 `fixture`。目标 store 必须已存在且为空；worker 导入 dense 数据，
  关闭 RocksGraph，随后在同一进程中重新打开 store，再执行 warmup → measured。
- `reopen`：正式模式唯一允许的生命周期。adapter 在计时前验证已发布 store
  manifest、key map、store metadata 和逻辑 store SHA，然后只重新打开已有 DB。

对应 P10 的显式进程生命周期为：tiny fixture 使用
`fixture-process-lifetime-v1`；正式 `reopen` 必须使用
`prebuilt-store-query-process-lifetime-v1`。Aster worker 静态包含 RocksDB，因此当前
正式配置的 `runtime_libraries` 应为空；若以后改为动态链接，request 中每个共享库的
绝对路径与 SHA-256 都会被 adapter 校验并写入 provenance。

RocksDB 以读写方式 reopen 时会滚动 `CURRENT`、MANIFEST、OPTIONS、WAL 和 LOG，
所以这些物理文件的逐字节 tree hash 不能作为跨 repeat 的不变量。正式
`store_sha256` 使用下列查询语义不变量：

```text
sha256-aster-query-store-v1(
  key-map,
  store-metadata,
  GraphMeta,
  default-column-family 中按 RocksDB comparator 顺序排列的全部 key/value
)
```

该 digest 由同一个冻结 worker 通过 `DB::OpenForReadOnly` 流式产生；adapter 在
RocksGraph 查询前后各验证一次。manifest 仍保存完整物理 snapshot tree 和逐文件
SHA，供首次发布审计和磁盘占用核对，但不把正常 reopen 的日志滚动误判为数据变化。

## 构建与 tiny correctness

只链接服务器上已存在的 Aster `librocksdb.a`：

```bash
cidr-experiments/runners/p10/adapters/build_aster_p10_worker.sh \
  --aster-root /data/WorkSpace/lsmgraph-rs/deps/Aster \
  --output /ABS/BIN/aster_p10_worker
```

构建脚本会输出 worker capability、worker 源码 SHA-256 和 binary SHA-256。真实
RocksGraph tiny fixture（不属于论文性能数据）：

```bash
ASTER_SOURCE_ROOT=/data/WorkSpace/lsmgraph-rs/deps/Aster \
ASTER_P10_BINARY=/ABS/BIN/aster_p10_worker \
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest -v \
  cidr-experiments/runners/p10/tests/test_aster_adapter.py
```

测试覆盖 fresh import、冻结 manifest、reopen、两轮 warmup/两轮 measured、逐查询
truth/digest、binary SHA 篡改拒绝以及 formal 缺少 P02B/P31 时在 worker 启动前拒绝。

## 发布 store manifest

store 完成一次 correctness 建库并含有 `p10-aster-key-map.tsv` 与
`p10-aster-store.json` 后，在 store 外生成 manifest：

```bash
python3 -B cidr-experiments/runners/p10/adapters/build_aster_store_manifest.py \
  --store /ABS/ASTER-STORE \
  --output /ABS/MANIFEST/aster-store-manifest.json \
  --source-root /data/WorkSpace/lsmgraph-rs/deps/Aster \
  --source-commit FULL_40_HEX_COMMIT \
  --binary /ABS/BIN/aster_p10_worker \
  --dataset /ABS/edges-dense.txt
```

manifest 输出的 `store_sha256` 必须原样写入 P10 suite
`systems[aster].store_roots[].sha256`。adapter、binary、dataset、truth、store
manifest、P02B result/validator 与 P31 wrapper 的 SHA-256 均必须在 suite args 中
冻结，不能在运行时留空。

## formal fail-closed 条件

正式运行还要求：

1. P10 harness worktree 与 Aster source tree 都 clean；Aster HEAD 必须等于
   `--source-commit`，`system_version` 必须精确为
   `aster-rocksgraph@FULL_40_HEX_COMMIT`。
2. truth 必须恰好 1,700 行；binary、dataset 和 store SHA 必须同时与 request、
   adapter argv 和 store manifest 一致。
3. `validate_sentinel_result.py --consumer P10 --require-formal` 返回 host-global
   PASS，且 hostname/P31 host fingerprint 与当前机器一致；默认 sentinel 最大年龄为
   3,600 秒。
4. 外层 orchestrator 必须由真实 P31 包住 adapter。结束后会把 P31 的 Git SHA、
   binary/dataset/truth/wrapper SHA 和唯一 store root 与 Aster provenance 再次交叉绑定。
5. 任一 timeout、truth mismatch、顺序变化、缺/多 observation、phase digest
   不一致、逻辑 store 前后变化、P31 非 DONE 或 provenance 漂移均不产生正式结果。

P31 统计覆盖整个 adapter 生命周期；论文 QPS/P50/P95/P99 只取 measured phase。
二者不能混用。`fresh` 结果始终是 correctness fixture，不能进入 Figure 1。

## 当前正式运行前仍需准备

- 已建立 detached clean source view
  `/data/WorkSpace/lsmgraph-cidr-deps/Aster-clean`（commit
  `6abb258e577c479325092a8ac0e7691fdfd154c2`）。原 checkout 的未跟踪
  `graph_test/graph_example` 保留不动；它不能作为 formal 的 `--source-root`。
- 仍需从该 clean source view 构建并发布最终 worker binary。当前已有 binary 只用于
  tiny 联合验证；adapter 会分别冻结 clean source HEAD/clean 状态、worker 源码 SHA
  与最终可执行文件 SHA，不把“同一路径”当作构建来源证明。
- 用最终 binary 为 SF10 建立/迁移一个带 P10 key-map 与 metadata 的 reopen store，
  先跑 1,700-query correctness，再发布 store manifest。
- 获得同机、未过期的正式 P02B PASS，并把真实 P31 wrapper SHA 写入 formal suite。

这些准备完成前只能运行 tiny/SF1 correctness，不得启动 SF10 timing。
