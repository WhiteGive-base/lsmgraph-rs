# LSMGraph 项目启动说明书

> 本文档说明 LSMGraph 项目的编译、启动、数据导入和 LDBC SNB 测试流程。
> 记录时间：2026-06-06
> 最近修订：2026-06-07 —— 区分**当前 BaseGraph 路径**（`base-build` + `snb-server`/`snb-validate*`，均走 `open_dynamic`）与 **Legacy 路径**（`import --relation snb-full` + `snb-cache`，已不在运行链上、待清理）

---

## 1. 环境依赖

### 1.1 必需依赖

| 依赖 | 要求 | 说明 |
|------|------|------|
| **Rust** | >= 1.70 | `rustc` + `cargo`，建议通过 `rustup` 安装 |
| **Cargo** | 最新稳定版 | Rust 包管理器 |
| **Java 11** | OpenJDK 11 | 用于运行 LDBC Java Driver |
| **Maven** | 3.x | 用于编译 LDBC Java Driver |
| **Python 3** | 任意版本 | 用于 locate_first_failure.sh 脚本 |
| **curl** | 任意版本 | 用于健康检查 |

### 1.2 可选依赖

| 依赖 | 用途 | 编译选项 |
|------|------|----------|
| **libc (dev)** | O_DIRECT 支持 | `--features direct-io` |
| **io-uring** | 异步 I/O | `--features uring` |
| **Linux 5.x** | io_uring 需要 Linux 5.1+ | 仅在启用 uring 时需要 |

### 1.3 路径约定

假设项目根目录为 `$ROOT`：

```bash
ROOT=/data/WorkSpace/lsmgraph-rs
export PATH="$HOME/.cargo/bin:$PATH"
export JAVA_HOME="${JAVA_HOME:-/usr/lib/jvm/java-11-openjdk-amd64}"
export PATH="$JAVA_HOME/bin:$PATH"
```

---

## 2. 编译

### 2.1 仅编译基础版本

```bash
cd $ROOT
cargo build --release
```

产物：`target/release/lsmgraph`

### 2.2 编译全功能版本（推荐）

```bash
cd $ROOT
cargo build --release --features direct-io,uring
```

产物：`target/release/lsmgraph`（同时支持三种 I/O 后端）

### 2.3 运行测试

```bash
cd $ROOT
cargo test
```

### 2.4 仅编译（不运行）

```bash
cargo build --release --features direct-io,uring --locked
```

---

## 3. I/O 后端选择

启动时通过 `--io-backend` 指定：

| 参数值 | 说明 | 适用场景 |
|--------|------|----------|
| `blocking`（默认） | 标准 `pread(2)` | 通用场景 |
| `direct` | `O_DIRECT`，绕过页缓存 | BaseGraph 读，避免双重缓冲 |
| `uring` | Linux io_uring 异步 I/O | 高并发（当前实验性） |

```bash
# 示例：使用 direct-io 后端
target/release/lsmgraph --io-backend direct snb-server ...
```

---

## 4. 数据导入

### 4.1 LDBC SNB 数据准备

数据来源可以是：

1. **已有数据**（推荐直接使用）：
   - SF1: `/data/WorkSpace/dgs/data/social_network_tugraph`
   - SF10: `/data/WorkSpace/ldbc-sf10/social_network`
   - SF30: `/data/WorkSpace/ldbc-sf30/social_network`

2. **自行生成**（需要 LDBC SNB Data Generator）：
   ```bash
   # 参考 LDBC 官方文档生成 CSV 数据
   ```

### 4.2 导入模式一览

| 导入命令 | 产物 | 说明 |
|----------|------|------|
| `--relation person_knows` | L0 segments | 仅 Knows 边，用于快速测试 |
| `--relation all-topology` | L0 segments | 所有拓扑关系 |
| `--relation snb-full` | L0 segments + SNB cache | 完整拓扑 + 属性 + adjacency cache（**Legacy 路径**）|

> **路径说明（重要）**：当前推荐 / 实际使用的是 **BaseGraph 路径**——`base-build` 构建只读 CSR
> 快照（见 §4.7），再由 `snb-server`（`open_dynamic`）服务，validation / benchmark 也走这条链。
> 上表 `import --relation snb-full` 与 adjacency cache（`snb-cache`）属 **Legacy 路径**：代码仍可
> 编译、`snb-cache` 仍可手动触发，但已不在 server / validation / benchmark 链上（待清理）。

### 4.3 导入 person_knows（轻量测试）

```bash
cd $ROOT
target/release/lsmgraph import \
  --input /data/WorkSpace/dgs/data/social_network_tugraph \
  --data-dir ./store/sf1-person-knows \
  --relation person_knows
```

### 4.4 导入完整拓扑（all-topology）

```bash
cd $ROOT
target/release/lsmgraph import \
  --input /data/WorkSpace/dgs/data/social_network_tugraph \
  --data-dir ./store/sf1-topology \
  --relation all-topology
```

### 4.5 导入完整数据（snb-full，用于正式验证）

```bash
cd $ROOT
target/release/lsmgraph import \
  --input /data/WorkSpace/dgs/data/social_network_tugraph \
  --data-dir ./store/sf1-full \
  --relation snb-full
```

### 4.6 导入后 compaction（可选）

```bash
# 导入后，将 L0 compact 到 L1，提升读性能
target/release/lsmgraph compact \
  --data-dir ./store/sf1-full
```

### 4.7 一键准备完整验证 store（推荐）

```bash
cd $ROOT
FORCE=1 \
INPUT=/data/WorkSpace/dgs/data/social_network_tugraph \
DATA_DIR=./store/sf1-full-validation \
deps/ldbc_snb_interactive_impls/lsmgraph/prepare_validation_store.sh
```

该脚本自动：
1. 删除旧 store（如 FORCE=1）
2. 构建 BaseGraph（CSR 快照，`base-build`）

> 注：脚本现在**只构建 BaseGraph**，不再构建 SNB adjacency cache（Dynamic 路径用不到）。

---

## 5. 启动服务

### 5.1 启动 SNB HTTP 适配器

```bash
cd $ROOT
DATA_DIR=./store/sf1-full-validation \
HOST=127.0.0.1 \
PORT=9090 \
LSMGRAPH_METRICS_INTERVAL_SECS=30 \
deps/ldbc_snb_interactive_impls/lsmgraph/start_server.sh
```

等价命令：

```bash
target/release/lsmgraph snb-server \
  --data-dir ./store/sf1-full-validation \
  --host 127.0.0.1 \
  --port 9090
```

### 5.2 启动参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--data-dir` | 必需 | store 路径 |
| `--host` | 127.0.0.1 | 监听地址 |
| `--port` | 9090 | 监听端口 |
| `--io-backend` | blocking | I/O 后端 |

### 5.3 验证服务启动成功

```bash
curl http://127.0.0.1:9090/
# 期望返回: {"status":"ok"}

curl http://127.0.0.1:9090/metrics
# 期望返回: JSON metrics
```

---

## 6. LDBC SNB 测试

### 6.1 测试类型总览

| 类型 | 说明 | 工具 |
|------|------|------|
| **Validation** | 验证查询结果正确性 | LDBC Java Driver |
| **Benchmark** | 性能基准测试 | LDBC Java Driver |
| **Storage Microbench** | 底层存储性能测试 | Rust 内置工具 |

### 6.2 LDBC Java Driver Validation（标准验证路径）

#### 6.2.1 启动服务

终端 1：

```bash
cd $ROOT
DATA_DIR=./store/sf1-full-validation \
deps/ldbc_snb_interactive_impls/lsmgraph/start_server.sh
```

#### 6.2.2 运行混合验证（IC + IS + IU）

终端 2：

```bash
cd $ROOT/deps/ldbc_snb_interactive_impls/dgs

# 验证前 100 行（烟雾测试）
MAX_LINES=100 \
deps/ldbc_snb_interactive_impls/lsmgraph/run_driver_validate_mixed.sh

# 验证全部行
MAX_LINES=0 \
deps/ldbc_snb_interactive_impls/lsmgraph/run_driver_validate_mixed.sh
```

#### 6.2.3 运行单查询验证

```bash
cd $ROOT

# 验证 IC1
QUERIES=ic1 deps/ldbc_snb_interactive_impls/lsmgraph/run_driver_validate_ic.sh

# 验证多个 IC
QUERIES=ic1,ic2,ic3,ic14 deps/ldbc_snb_interactive_impls/lsmgraph/run_driver_validate_ic.sh

# 验证 IS
QUERIES=is1 deps/ldbc_snb_interactive_impls/lsmgraph/run_driver_validate_ic.sh

# 验证 IU
QUERIES=iu5 deps/ldbc_snb_interactive_impls/lsmgraph/run_driver_validate_ic.sh
```

#### 6.2.4 直接使用 Java Driver properties 文件

```bash
cd $ROOT/deps/ldbc_snb_interactive_impls/dgs

# 混合 IC/IS/IU 验证
bash run.sh interactive-validate.properties

# 指定 SF 的 benchmark
bash run.sh interactive-benchmark-sf10.properties
bash run.sh interactive-benchmark-sf30.properties
```

### 6.3 LDBC Java Driver Benchmark（性能基准）

```bash
cd $ROOT/deps/ldbc_snb_interactive_impls/dgs

# SF1 Benchmark
bash run.sh interactive-benchmark-sf1.properties

# SF10 Benchmark
bash run.sh interactive-benchmark-sf10.properties

# SF30 Benchmark
bash run.sh interactive-benchmark-sf30.properties
```

Benchmark 会输出：
- Throughput (op/s)
- Per-query latency (mean, p50, p99)
- Schedule audit 结果

### 6.4 Storage Microbench（底层存储性能测试）

不启动 server，直接测试底层 CSR/L0 读性能：

```bash
cd $ROOT

# SF1 Storage Microbench
SAMPLES=100 \
EDGE_TYPE=1 \   # 1=Knows, 2=HasCreator, 3=HasTag 等
DATA_DIR=./store/sf1-full-validation \
deps/ldbc_snb_interactive_impls/lsmgraph/storage_bench.sh
```

关键输出指标：
- `get_neighbors_elapsed_ms` — typed 查询耗时
- `candidate_l0_segments` — 候选 L0 segment 数
- `matched_l0_segments` — 实际匹配的 L0 segment 数
- `body_reads` — 实际读取的 body 数量

---

## 7. 快速启动流程（推荐顺序）

### 第一次运行（SF1 烟雾测试）

```bash
cd $ROOT
export PATH="$HOME/.cargo/bin:$PATH"
export JAVA_HOME="${JAVA_HOME:-/usr/lib/jvm/java-11-openjdk-amd64}"
export PATH="$JAVA_HOME/bin:$PATH"

# 1. 编译
cargo build --release --features direct-io,uring

# 2. 准备数据（一次性）
FORCE=1 \
INPUT=/data/WorkSpace/dgs/data/social_network_tugraph \
DATA_DIR=./store/sf1-full-validation \
deps/ldbc_snb_interactive_impls/lsmgraph/prepare_validation_store.sh

# 3. 启动服务
DATA_DIR=./store/sf1-full-validation \
deps/ldbc_snb_interactive_impls/lsmgraph/start_server.sh &
sleep 5

# 4. 验证服务健康
curl http://127.0.0.1:9090/

# 5. 运行 LDBC 验证
cd ./deps/ldbc_snb_interactive_impls/dgs
MAX_LINES=100 ../lsmgraph/run_driver_validate_mixed.sh

# 6. 停止服务
kill %1
```

### 重复运行（数据已存在）

```bash
# 跳过 prepare，直接启动服务和验证
DATA_DIR=./store/sf1-full-validation \
deps/ldbc_snb_interactive_impls/lsmgraph/start_server.sh &
sleep 5

curl http://127.0.0.1:9090/

cd ./deps/ldbc_snb_interactive_impls/dgs
MAX_LINES=100 ../lsmgraph/run_driver_validate_mixed.sh
```

---

## 8. 调试工具

### 8.1 定位第一个失败样例

```bash
cd $ROOT

# 验证 1500 行，自动定位第一个失败
MAX_LINES=1500 \
deps/ldbc_snb_interactive_impls/lsmgraph/locate_first_failure.sh
```

输出包含：
- Server log: `logs/first-failure-YYYYMMDD_HHMMSS-server.log`
- Driver log: `logs/first-failure-YYYYMMDD_HHMMSS-driver.log`
- 第一个失败的实际结果 vs 期望结果

### 8.2 拆分验证

```bash
# 仅验证 IC1-IC14
MAX_LINES_PER_QUERY=100 \
QUERIES=ic1,ic2,ic3,ic4,ic5,ic6,ic7,ic8,ic9,ic10,ic11,ic12,ic13,ic14 \
deps/ldbc_snb_interactive_impls/lsmgraph/validate_splits.sh

# 验证全部行
MAX_LINES_PER_QUERY=0 \
QUERIES=ic1,ic14 \
deps/ldbc_snb_interactive_impls/lsmgraph/validate_splits.sh
```

### 8.3 内部 Rust 验证器

```bash
# IC1-IC14 验证
target/release/lsmgraph snb-validate \
  --data-dir ./store/sf1-full \
  --validation-params ./deps/ldbc_snb_interactive_impls/dgs/validation_params.csv \
  --max-lines 500

# 混合 IC/IS/IU 验证
target/release/lsmgraph snb-validate-mixed \
  --data-dir ./store/sf1-full-validation \
  --validation-params ./deps/ldbc_snb_interactive_impls/dgs/validation_params_tugraph.csv \
  --max-lines 500

# Person-Knows 边验证
target/release/lsmgraph validate-knows \
  --input /data/WorkSpace/dgs/data/social_network_tugraph \
  --data-dir ./store/sf1 \
  --max-vertices 100000
```

### 8.4 Storage 状态检查

```bash
# 查看 levels 和 metrics
target/release/lsmgraph stats \
  --data-dir ./store/sf1-full-validation

# 扫描所有边
target/release/lsmgraph scan \
  --data-dir ./store/sf1-full-validation

# 查看邻接关系
target/release/lsmgraph neighbors \
  --data-dir ./store/sf1-full-validation \
  --src 72057594037927936
```

### 8.5 I/O 后端对比

```bash
# Blocking
/usr/bin/time -v target/release/lsmgraph --io-backend blocking scan \
  --data-dir ./store/sf1-full-validation

# Direct IO
/usr/bin/time -v target/release/lsmgraph --io-backend direct scan \
  --data-dir ./store/sf1-full-validation

# io_uring
/usr/bin/time -v target/release/lsmgraph --io-backend uring scan \
  --data-dir ./store/sf1-full-validation
```

---

## 9. 常用配置参数

### 9.1 编译参数

| Feature | 效果 |
|---------|------|
| `direct-io` | 启用 O_DIRECT 支持（需要 Linux + libc-dev） |
| `uring` | 启用 io_uring 支持（需要 Linux 5.1+） |

### 9.2 Server 环境变量

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `DATA_DIR` | `store/sf1-full-validation` | Store 路径 |
| `HOST` | `127.0.0.1` | 监听地址 |
| `PORT` | `9090` | 监听端口 |
| `BIN` | `target/release/lsmgraph` | 二进制路径 |
| `IO_BACKEND` | `blocking` | I/O 后端 |
| `LSMGRAPH_METRICS_INTERVAL_SECS` | `30` | Metrics 打印间隔（秒） |

### 9.3 Store 目录结构

```
store/<name>/
├── MANIFEST              # LSM Manifest 日志
├── levels/
│   ├── L0/              # L0 segments
│   └── L1/               # L1 segments
├── delta/                # DeltaGraph 目录
│   ├── MANIFEST
│   └── levels/
└── base_graph/           # BaseGraph CSR 快照（Dynamic 路径的全部数据来源）
    ├── catalog.json
    ├── vertices/
    ├── edges/
    ├── single_edges/
    └── derived_indexes/

# Legacy 路径产物（由 import --relation snb-full / snb-cache 生成）；Dynamic 路径既不生成也不读取：
#   snb_vertices.jsonl, snb_edge_props.jsonl, snb_adjacency.bin, snb_adjacency.jsonl
```

---

## 10. 常见问题

### Q1: 编译报错 "cannot find -lc"

安装 libc 开发库：

```bash
# Debian/Ubuntu
sudo apt install libc6-dev

# 或在 Cargo.toml 中移除 direct-io/uring feature，仅使用默认的 blocking 后端
cargo build --release
```

### Q2: io_uring 报错 "operation not supported"

`io_uring` 需要 Linux 5.1+ 内核。当前内核版本可能不支持，或使用了不支持的 filesystem。改用 blocking 或 direct 后端。

### Q3: Java Driver 连接拒绝

确认服务已启动且端口正确：

```bash
curl http://127.0.0.1:9090/
# 应返回 {"status":"ok"}

# 检查端口占用
ss -tlnp | grep 9090
```

### Q4: Validation 全部 skipped

Dynamic（BaseGraph）路径下 validation 读取的是 BaseGraph，确认其存在：

```bash
ls -la ./store/sf1-full-validation/base_graph/catalog.json
# 如果不存在，用 base-build 重建（见 §4.7 prepare_validation_store.sh）
FORCE=1 INPUT=<csv_root> DATA_DIR=./store/sf1-full-validation \
  deps/ldbc_snb_interactive_impls/lsmgraph/prepare_validation_store.sh
```

> 旧文档让你检查 `snb_adjacency.bin` / 跑 `snb-cache` —— 那是 Legacy 路径，已不适用于当前的
> `snb-validate*` / `snb-server`（均走 `open_dynamic`）。

### Q5: prepare_validation_store.sh 报错 store 已存在

```bash
# 删除旧 store 并重建
FORCE=1 deps/ldbc_snb_interactive_impls/lsmgraph/prepare_validation_store.sh

# 或手动删除
rm -rf ./store/sf1-full-validation
```

### Q6: 需要处理更大的 SF（SF10/SF30）

```bash
# SF10
FORCE=1 \
INPUT=/data/WorkSpace/ldbc-sf10/social_network \
DATA_DIR=./store/sf10-full-validation \
deps/ldbc_snb_interactive_impls/lsmgraph/prepare_validation_store.sh

# SF30
FORCE=1 \
INPUT=/data/WorkSpace/ldbc-sf30/social_network \
DATA_DIR=./store/sf30-full-validation \
deps/ldbc_snb_interactive_impls/lsmgraph/prepare_validation_store.sh
```

# SF100
FORCE=1 \
INPUT=/data/WorkSpace/ldbc-sf100/social_network \
DATA_DIR=./store/sf30-full-validation \
deps/ldbc_snb_interactive_impls/lsmgraph/prepare_validation_store.sh

> 注意：SF30 数据量大，prepare 脚本可能需要数小时。
