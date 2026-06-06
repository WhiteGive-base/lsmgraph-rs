# lsmgraph-rs

基于 LSMGraph 原型，在 `/data/WorkSpace/dgs/data/social_network_tugraph` 下复现 LDBC SNB SF1 数据的存储引擎核心。

本项目有意不读取或依赖现有 DGS 的 `dgs_db`。DGS 仅用作模式、查询参考实现和 LDBC 验证工具链。

## 已实现功能

- 基于 Tokio 的引擎外壳。
- `IoBackend` 抽象，支持有界阻塞 `pread`/`pwrite`。
- 可选的 `direct-io` 和功能性 `uring` I/O 后端。
- MemGraph，支持 active/frozen 轮换、低度内联分段和高度过溢 BTreeMap。
- CSR 写入器/读取器，支持稀疏按源偏移和固定二进制边体。
- 清单重放，用于重新打开持久化存储。
- L0 版本链和可见邻接合并。
- L0 到 L1 的压缩。
- L1+ 查找的简单多级索引。
- SNB 完整拓扑/属性导入，用于 IC1-IC14 验证。
- 持久化的 SNB 邻接缓存。
- DGS 兼容的 HTTP 适配器，用于复制的 LDBC Java 驱动。
- 针对 `validation_params_tugraph.csv` 的混合 IC/IS/IU 验证。

## 打包的 LDBC 资产

上游实现树原样复制到：

```text
/data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls
```

LSMGraph 专用包装器位于：

```text
/data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/lsmgraph
```

当前适配器范围为 IC1-IC14。复制的 IS1-IS7 和 IU1-IU8 验证文件原样存在。IS1-IS7 和 IU1-IU8 在混合 Tugraph 验证流程和 HTTP 适配器中已启用。

## 服务器设置

在 Linux 服务器上运行：

```bash
cd /data/WorkSpace/lsmgraph-rs
export PATH="$HOME/.cargo/bin:$PATH"
```

构建和测试：

```bash
cargo test
cargo build --release --features direct-io,uring
```

启动 DGS 兼容的 LSMGraph HTTP 适配器。包装器默认使用 `127.0.0.1:9090`，与复制的 Java 驱动验证属性匹配。

```bash
deps/ldbc_snb_interactive_impls/lsmgraph/start_server.sh
```

等效的显式命令：

```bash
target/release/lsmgraph snb-server \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full \
  --host 127.0.0.1 \
  --port 9090
```

## 导入命令

将 SF1 `person_knows` 导入新的存储：

```bash
target/release/lsmgraph import \
  --input /data/WorkSpace/dgs/data/social_network_tugraph \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1 \
  --relation person_knows
```

导入所有当前支持的 SNB 拓扑关系：

```bash
target/release/lsmgraph import \
  --input /data/WorkSpace/dgs/data/social_network_tugraph \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-topology \
  --relation all-topology
```

导入标签编码的 SNB 拓扑及顶点/边属性用于 IC 验证：

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

## Java 驱动验证

最终 LDBC 验证路径是 `deps/ldbc_snb_interactive_impls/dgs` 下复制的 Java 驱动。LSMGraph 仅启动 HTTP 适配器并暴露端口；不使用 DGS 内核。

准备混合 IC/IS/IU 验证的干净初始存储：

```bash
FORCE=1 deps/ldbc_snb_interactive_impls/lsmgraph/prepare_validation_store.sh
```

在标准 Java 驱动端口上启动 LSMGraph：

```bash
DATA_DIR=/data/WorkSpace/lsmgraph-rs/store/sf1-full-validation \
  deps/ldbc_snb_interactive_impls/lsmgraph/start_server.sh
```

使用 `validation_params_tugraph.csv` 运行复制的 Java LDBC 驱动：

```bash
MAX_LINES=100 deps/ldbc_snb_interactive_impls/lsmgraph/run_driver_validate_mixed.sh
```

需要时运行复制的 Java 驱动单查询验证：

```bash
QUERIES=ic2 deps/ldbc_snb_interactive_impls/lsmgraph/run_driver_validate_ic.sh
QUERIES=is1 deps/ldbc_snb_interactive_impls/lsmgraph/run_driver_validate_ic.sh
QUERIES=iu5 deps/ldbc_snb_interactive_impls/lsmgraph/run_driver_validate_ic.sh
```

## 内部调试命令

存储冒烟检查：

```bash
target/release/lsmgraph scan \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full

target/release/lsmgraph stats \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full
```

根据原始 CSV 验证所有 `person_knows` 邻接表：

```bash
target/release/lsmgraph validate-knows \
  --input /data/WorkSpace/dgs/data/social_network_tugraph \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1 \
  --max-vertices 100000
```

根据遗留的组合 `validation_params.csv` 验证：

```bash
target/release/lsmgraph snb-validate \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full \
  --validation-params /data/WorkSpace/dgs/deps/ldbc_snb_interactive_impls/dgs/validation_params.csv \
  --max-lines 500
```

直接根据复制的官方分割文件验证：

```bash
deps/ldbc_snb_interactive_impls/lsmgraph/validate_splits.sh
```

有用的分割验证覆盖参数：

```bash
QUERIES=ic1,ic2,ic14 MAX_LINES_PER_QUERY=100 \
  deps/ldbc_snb_interactive_impls/lsmgraph/validate_splits.sh

MAX_LINES_PER_QUERY=0 \
  deps/ldbc_snb_interactive_impls/lsmgraph/validate_splits.sh
```

`MAX_LINES_PER_QUERY=0` 表示每个选定分割文件的所有行。

以下 Rust 验证器是内部调试辅助工具，不是最终 LDBC 验证路径。

直接验证复制的 Tugraph 混合 IC/IS/IU 文件：

```bash
target/release/lsmgraph snb-validate-mixed \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full-validation \
  --validation-params /data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/dgs/validation_params_tugraph.csv \
  --max-lines 500
```

## 性能命令

查询验证墙上时间和内存：

```bash
/usr/bin/time -v target/release/lsmgraph snb-validate \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full \
  --validation-params /data/WorkSpace/dgs/deps/ldbc_snb_interactive_impls/dgs/validation_params.csv \
  --max-lines 500
```

存储扫描吞吐量冒烟测试：

```bash
/usr/bin/time -v target/release/lsmgraph scan \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full
```

阻塞/direct/io_uring 后端冒烟对比：

```bash
/usr/bin/time -v target/release/lsmgraph --io-backend blocking scan \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full

/usr/bin/time -v target/release/lsmgraph --io-backend direct scan \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full

/usr/bin/time -v target/release/lsmgraph --io-backend uring scan \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full
```

BaseGraph/DynamicGraphView Linux 构建和基准测试入口点：

```bash
target/release/lsmgraph base-build \
  --input /data/WorkSpace/ldbc-sf10/social_network \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf10-base-dynamic

target/release/lsmgraph dynamic-stats \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf10-base-dynamic

bash deps/ldbc_snb_interactive_impls/lsmgraph/run_linux_benchmarks.sh
```

`run_linux_benchmarks.sh` 始终运行 SF10 BaseGraph 构建、存储 I/O 基准测试和复制的 Java 驱动基准测试。SF30 是条件性的：仅在 SF10 完成且达到 `MIN_SF10_QPS` 时才运行，除非设置了 `RUN_SF30=true`。该脚本还从测量产物写入 SF100 估算报告。

## 观测到的 SF1 结果

预期 SF1 `person_knows` 结果：

- CSV 数据行数：`180623`
- 双向导入后的有向记录数：`361246`
- 此文件中至少有一条 knows 边的顶点数：`9163`

观测到的 SF1 `all-topology` 冒烟结果：

- 访问的受支持 CSV 源输入行数：`19308214`
- 有向拓扑边记录数：`17436661`
- 默认 64 MiB MemGraph 压缩前产生 `9` 个 L0 CSR 文件。

观测到的 SF1 `snb-full` 结果：

- 包含反向查找边的有向拓扑边记录数：`34692699`
- 默认 64 MiB MemGraph 压缩前产生 `17` 个 L0 CSR 文件。
- 持久化 SNB 邻接缓存：`snb_adjacency.bin`，`11330230` 个组，约 `611M`。
- `snb-validate --max-lines 500` 对 IC1-IC14 通过 `500/500`。
- 直接分割验证检查了 IC1-IC14 各前 `100` 行，通过 `1400/1400`。
- 混合 Tugraph 验证检查了 `validation_params_tugraph.csv` 前 `500` 行：`476` 次读取通过，`24` 次更新应用，`0` 次失败。
- 复制的 Java LDBC 驱动混合验证检查了 `validation_params_tugraph.csv` 前 `100` 行：执行了 `23` 种操作类型，`0` 次崩溃，`0` 次错误，`BUILD SUCCESS`。
- Java LDBC 驱动验证针对 `snb-server` 处理 IC2 完整分割：`1428` 次操作，`0` 次错误。

观测到的 I/O 后端冒烟结果：

- `--io-backend direct` 导入并验证了 SF1 `person_knows`：`361246/361246`。
- `--io-backend uring` 导入了 SF1 `person_knows`；扫描返回 `361246`。
- `uring` 当前为正确性测试每个操作创建一个 ring。在添加持久化 ring worker 之前，完整随机邻接验证比阻塞后端慢得多。

## 后续工作

- 将大型 SNB 查询模块拆分为 `ic`、`is`、`iu`、`validation` 和 `http_adapter` 模块。
- 使用混合 IC/IS/IU 验证属性运行 Java LDBC 驱动。
- mmap SNB 邻接缓存以减少验证器启动内存复制成本。
- 用持久化 ring worker 和批量提交替换功能性 `uring` 后端。
