# lsmgraph-rs

LSMGraph 原型，用于在 LDBC SNB SF1 数据集
（位于 `/data/WorkSpace/dgs/data/social_network_tugraph`）
上复现存储引擎核心。

本项目有意不读取或依赖现有 DGS 的 `dgs_db`。DGS 仅用作架构和查询参考实现。

## 已实现功能

- 基于 Tokio 的引擎外壳。
- `IoBackend` 抽象层，支持有界阻塞 `pread`/`pwrite`。
- MemGraph，支持活跃/冻结轮换、低出度内联段和高出度 `BTreeMap` 溢出。
- CSR 写入器/读取器，采用稀疏按源偏移量和固定二进制边体。
- Manifest 重放，用于重新打开持久化存储。
- L0 版本链和可见邻居合并。
- L0 到 L1 的压缩。
- L1+ 的简单多级索引。
- `person_knows` CSV 导入和原始 CSV 验证。

## 命令

```bash
cd /data/WorkSpace/lsmgraph-rs

# 构建/测试
/home/ydl/.cargo/bin/cargo test
/home/ydl/.cargo/bin/cargo build --release

# 可选的 I/O 后端
/home/ydl/.cargo/bin/cargo build --release --features direct-io,uring

# 将 SF1 person_knows 导入全新的存储
target/release/lsmgraph import \
  --input /data/WorkSpace/dgs/data/social_network_tugraph \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1 \
  --relation person_knows

# 或导入当前支持的所有 SNB 拓扑关系
target/release/lsmgraph import \
  --input /data/WorkSpace/dgs/data/social_network_tugraph \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-topology \
  --relation all-topology

# 导入标签编码的 SNB 拓扑及顶点/边属性，用于 IC 验证
target/release/lsmgraph import \
  --input /data/WorkSpace/dgs/data/social_network_tugraph \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full \
  --relation snb-full

# 验证所有 person_knows 邻接表与原始 CSV
target/release/lsmgraph validate-knows \
  --input /data/WorkSpace/dgs/data/social_network_tugraph \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1 \
  --max-vertices 100000

# 扫描当前快照
target/release/lsmgraph scan \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1

# 将 L0 压缩到 L1 并再次验证
target/release/lsmgraph compact \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1

target/release/lsmgraph neighbors \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1 \
  --src 933

# 针对 LDBC validation_params.csv 验证迁移后的 IC1-IC14 适配器。
# 验证器在存在持久化 SNB 邻接缓存时优先使用它。
target/release/lsmgraph snb-validate \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full \
  --validation-params /data/WorkSpace/dgs/deps/ldbc_snb_interactive_impls/dgs/validation_params.csv \
  --max-lines 100

# 为现有 snb-full 存储重建持久化 SNB 邻接缓存
target/release/lsmgraph snb-cache \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full

# 选择存储 I/O 后端。blocking 为默认后端。
target/release/lsmgraph --io-backend direct scan \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-direct-smoke

target/release/lsmgraph --io-backend uring scan \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-uring-smoke
```

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
- `snb-validate --max-lines 100` 检查了 `100` 条 IC1-IC14 记录，全部通过。
- `snb-validate --max-lines 500` 检查了 `500` 条 IC1-IC14 记录，全部通过。
- 持久化 SNB 邻接缓存：`snb_adjacency.bin`，`11330230` 个组，约 `611M`。
- 使用持久化缓存后，`snb-validate --max-lines 100` 在约 `31s` 内全部通过 `100/100`。

I/O 后端烟雾测试结果：

- `--io-backend direct` 导入并验证了 SF1 `person_knows`：`361246/361246`。
- `--io-backend uring` 导入了 SF1 `person_knows`；scan 返回了 `361246`。
- `uring` 目前每次操作创建一个 ring 用于正确性测试。在添加持久化 ring worker 之前，完全随机邻居验证比 blocking 后端慢得多。

## 下一步工作

- 添加兼容 LDBC interactive driver 的持久化 HTTP 端点。
- mmap SNB 邻接缓存，以减少验证器启动时的内存拷贝开销。
- 将功能性的 `uring` 后端替换为持久化 ring worker 和批量提交。

## Linux 服务器快速命令

下面这些命令都在服务器上执行：

```bash
cd /data/WorkSpace/lsmgraph-rs
export PATH="$HOME/.cargo/bin:$PATH"
```

### 构建

```bash
cargo test
cargo build --release --features direct-io,uring
```

### 后台服务式启动

当前版本还是 CLI storage/query prototype，没有常驻 HTTP LDBC driver server。
如果要把一次导入、验证或性能测试当后台任务跑，可以这样启动：

```bash
mkdir -p logs

nohup target/release/lsmgraph snb-validate \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full \
  --validation-params /data/WorkSpace/dgs/deps/ldbc_snb_interactive_impls/dgs/validation_params.csv \
  --max-lines 500 \
  > logs/snb-validate-500.log 2>&1 &

tail -f logs/snb-validate-500.log
```

### 结果验证

```bash
# 存储烟雾测试
target/release/lsmgraph scan \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full

# IC1-IC14 快速验证
target/release/lsmgraph snb-validate \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full \
  --validation-params /data/WorkSpace/dgs/deps/ldbc_snb_interactive_impls/dgs/validation_params.csv \
  --max-lines 100

# 更广的验证样本
target/release/lsmgraph snb-validate \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full \
  --validation-params /data/WorkSpace/dgs/deps/ldbc_snb_interactive_impls/dgs/validation_params.csv \
  --max-lines 500

# 完整验证，运行时间较长
target/release/lsmgraph snb-validate \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full \
  --validation-params /data/WorkSpace/dgs/deps/ldbc_snb_interactive_impls/dgs/validation_params.csv \
  --max-lines 19992
```

现有 SF1 存储上观察到：`snb-validate --max-lines 500` 在 IC1-IC14 上全部通过 `500/500`。IC14 验证在语义上比较等权最短路径，因为 LDBC 按 `pathWeight` 排序且未定义等权情况下的打破规则。

### 性能测试

```bash
# 查询验证 wall time 和内存
/usr/bin/time -v target/release/lsmgraph snb-validate \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full \
  --validation-params /data/WorkSpace/dgs/deps/ldbc_snb_interactive_impls/dgs/validation_params.csv \
  --max-lines 500

# 存储扫描吞吐量烟雾测试
/usr/bin/time -v target/release/lsmgraph scan \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full

# 检查 manifest/文件级指标
target/release/lsmgraph stats \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full

# Blocking/direct/io_uring 后端烟雾对比
/usr/bin/time -v target/release/lsmgraph --io-backend blocking scan \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full

/usr/bin/time -v target/release/lsmgraph --io-backend direct scan \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full

/usr/bin/time -v target/release/lsmgraph --io-backend uring scan \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full
```
