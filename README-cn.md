# lsmgraph-rs

LSMGraph 原型，用于在 LDBC SNB SF1 数据集
（位于 `/data/WorkSpace/dgs/data/social_network_tugraph`）
上复现存储引擎核心。

本项目有意不读取或依赖现有 DGS 的 `dgs_db`。DGS 仅用作架构、查询参考实现和 LDBC 验证框架。

## 已实现功能

- 基于 Tokio 的引擎外壳。
- `IoBackend` 抽象层，支持有界阻塞 `pread`/`pwrite`。
- 可选的 `direct-io` 和功能性 `uring` I/O 后端。
- MemGraph，支持活跃/冻结轮换、低出度内联段和高出度 `BTreeMap` 溢出。
- CSR 写入器/读取器，采用稀疏按源偏移量和固定二进制边体。
- Manifest 重放，用于重新打开持久化存储。
- L0 版本链和可见邻居合并。
- L0 到 L1 的压缩。
- L1+ 的简单多级索引。
- SNB 完整拓扑/属性导入，用于 IC1-IC14 验证。
- 持久化 SNB 邻接缓存。
- 兼容 DGS 的 HTTP 适配器，对接复制的 LDBC Java driver。
- 针对 `validation_params_tugraph.csv` 的混合 IC/IS/IU 验证。

## Vendored LDBC 资产

上游实现树原样复制在：

```text
/data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls
```

LSMGraph 专用封装脚本位于：

```text
/data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/lsmgraph
```

当前适配器范围为 IC1-IC14。复制的 IS1-IS7 和 IU1-IU8 验证文件原样保留。IS1-IS7 和 IU1-IU8 已启用，用于混合 Tugraph 验证流程和 HTTP 适配器。

## 服务器配置

在 Linux 服务器上执行：

```bash
cd /data/WorkSpace/lsmgraph-rs
export PATH="$HOME/.cargo/bin:$PATH"
```

构建和测试：

```bash
cargo test
cargo build --release --features direct-io,uring
```

启动兼容 DGS 的 LSMGraph HTTP 适配器。封装脚本默认使用 `127.0.0.1:9090`，与复制的 Java driver 验证配置一致。

```bash
deps/ldbc_snb_interactive_impls/lsmgraph/start_server.sh
```

等价的显式命令：

```bash
target/release/lsmgraph snb-server \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full \
  --host 127.0.0.1 \
  --port 9090
```

## 导入命令

将 SF1 `person_knows` 导入全新的存储：

```bash
target/release/lsmgraph import \
  --input /data/WorkSpace/dgs/data/social_network_tugraph \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1 \
  --relation person_knows
```

导入当前支持的所有 SNB 拓扑关系：

```bash
target/release/lsmgraph import \
  --input /data/WorkSpace/dgs/data/social_network_tugraph \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-topology \
  --relation all-topology
```

导入标签编码的 SNB 拓扑及顶点/边属性，用于 IC 验证：

```bash
target/release/lsmgraph import \
  --input /data/WorkSpace/dgs/data/social_network_tugraph \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full \
  --relation snb-full
```

为现有 `snb-full` 存储重建持久化 SNB 邻接缓存：

```bash
target/release/lsmgraph snb-cache \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full
```

## Java Driver 验证

最终的 LDBC 验证路径是复制在 `deps/ldbc_snb_interactive_impls/dgs` 下的 Java driver。LSMGraph 只启动 HTTP 适配器并暴露端口，不使用 DGS 内核。

准备一个干净的初始存储用于混合 IC/IS/IU 验证：

```bash
FORCE=1 deps/ldbc_snb_interactive_impls/lsmgraph/prepare_validation_store.sh
```

在标准 Java driver 端口上启动 LSMGraph：

```bash
DATA_DIR=/data/WorkSpace/lsmgraph-rs/store/sf1-full-validation \
  deps/ldbc_snb_interactive_impls/lsmgraph/start_server.sh
```

运行复制的 Java LDBC driver 对 `validation_params_tugraph.csv` 进行验证：

```bash
MAX_LINES=100 deps/ldbc_snb_interactive_impls/lsmgraph/run_driver_validate_mixed.sh
```

需要时运行复制的 Java driver 单查询验证：

```bash
QUERIES=ic2 deps/ldbc_snb_interactive_impls/lsmgraph/run_driver_validate_ic.sh
QUERIES=is1 deps/ldbc_snb_interactive_impls/lsmgraph/run_driver_validate_ic.sh
QUERIES=iu5 deps/ldbc_snb_interactive_impls/lsmgraph/run_driver_validate_ic.sh
```

## 内部调试命令

存储烟雾测试：

```bash
target/release/lsmgraph scan \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full

target/release/lsmgraph stats \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full
```

验证所有 `person_knows` 邻接表与原始 CSV：

```bash
target/release/lsmgraph validate-knows \
  --input /data/WorkSpace/dgs/data/social_network_tugraph \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1 \
  --max-vertices 100000
```

对照遗留的合并 `validation_params.csv` 验证：

```bash
target/release/lsmgraph snb-validate \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full \
  --validation-params /data/WorkSpace/dgs/deps/ldbc_snb_interactive_impls/dgs/validation_params.csv \
  --max-lines 500
```

直接对照复制的官方 split 文件验证：

```bash
deps/ldbc_snb_interactive_impls/lsmgraph/validate_splits.sh
```

常用的 split 验证参数覆盖：

```bash
QUERIES=ic1,ic2,ic14 MAX_LINES_PER_QUERY=100 \
  deps/ldbc_snb_interactive_impls/lsmgraph/validate_splits.sh

MAX_LINES_PER_QUERY=0 \
  deps/ldbc_snb_interactive_impls/lsmgraph/validate_splits.sh
```

`MAX_LINES_PER_QUERY=0` 表示处理每个选中的 split 文件中的全部行。

以下 Rust 验证器是内部调试辅助工具，不是最终的 LDBC 验证路径。

直接验证复制的 Tugraph 混合 IC/IS/IU 文件：

```bash
target/release/lsmgraph snb-validate-mixed \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full-validation \
  --validation-params /data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/dgs/validation_params_tugraph.csv \
  --max-lines 500
```

## 性能命令

查询验证 wall time 和内存：

```bash
/usr/bin/time -v target/release/lsmgraph snb-validate \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full \
  --validation-params /data/WorkSpace/dgs/deps/ldbc_snb_interactive_impls/dgs/validation_params.csv \
  --max-lines 500
```

存储扫描吞吐量烟雾测试：

```bash
/usr/bin/time -v target/release/lsmgraph scan \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full
```

Blocking/direct/io_uring 后端烟雾对比：

```bash
/usr/bin/time -v target/release/lsmgraph --io-backend blocking scan \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full

/usr/bin/time -v target/release/lsmgraph --io-backend direct scan \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full

/usr/bin/time -v target/release/lsmgraph --io-backend uring scan \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full
```

## SF1 实测结果

预期 SF1 `person_knows` 结果：

- CSV 数据行数：`180623`
- 双向导入后的有向记录数：`361246`
- 在该文件中至少有 1 条 knows 边的顶点数：`9163`

SF1 `all-topology` 烟雾测试结果：

- 遍历的输入行数（跨所有支持的 CSV 源）：`19308214`
- 有向拓扑边记录数：`17436661`
- 默认 64 MiB MemGraph 压缩前产生了 `9` 个 L0 CSR 文件。

SF1 `snb-full` 结果：

- 包括反向查找边在内的有向拓扑边记录数：`34692699`
- 默认 64 MiB MemGraph 压缩前产生了 `17` 个 L0 CSR 文件。
- 持久化 SNB 邻接缓存：`snb_adjacency.bin`，`11330230` 个组，约 `611M`。
- `snb-validate --max-lines 500` 在 IC1-IC14 上全部通过 `500/500`。
- Direct split 验证每个查询前 `100` 行，共计 `1400/1400` 全部通过。
- 混合 Tugraph 验证处理 `validation_params_tugraph.csv` 前 `500` 行：`476` 次读取通过，`24` 次更新应用，`0` 次失败。
- 复制的 Java LDBC driver 混合验证处理 `validation_params_tugraph.csv` 前 `100` 行：执行了 `23` 种操作类型，`0` 次崩溃，`0` 次错误，`BUILD SUCCESS`。
- Java LDBC driver 对 `snb-server` 的 IC2 完整 split 验证：处理了 `1428` 次操作，`0` 次错误。

I/O 后端烟雾测试结果：

- `--io-backend direct` 导入并验证了 SF1 `person_knows`：`361246/361246`。
- `--io-backend uring` 导入了 SF1 `person_knows`；scan 返回了 `361246`。
- `uring` 目前每次操作创建一个 ring 用于正确性测试。在添加持久化 ring worker 之前，完全随机邻居验证比 blocking 后端慢得多。

## 下一步工作

- 将大型 SNB 查询模块拆分为 `ic`、`is`、`iu`、`validation` 和 `http_adapter` 模块。
- 用混合 IC/IS/IU 验证配置运行 Java LDBC driver。
- mmap SNB 邻接缓存，以减少验证器启动时的内存拷贝开销。
- 将功能性的 `uring` 后端替换为持久化 ring worker 和批量提交。
