# 计划：运行 SF30+SF100 最新代码基准测试 + SF100 可行性/运行时间判断

## 背景

架构文档（`LSMGRAPH-ARCHITECTURE.md`，§12/§14）指出系统仅在 **SF30** 上经过验证， dominating cost 是**旧版 SNB 邻接表缓存**路径（SF30：冷启动约 15 分钟，RSS ~100 GB，17.57 op/s）。近期工作已更改该路径：

- 提交 `6e9f74d`（06-07）：移除了旧版 LSM `fallback_engine`，将所有邻居查询路由到 BaseGraph CSR + Delta，并限制了 IC14 路径枚举（修复了全最短路径 OOM 问题）。已在 **SF1** 上验证（约 1 GB RSS）。
- **未提交的** `src/snb/props.rs`：将 Post/Comment 顶点属性缓存从 AoS（`Vec<PostProps>`/`Vec<CommentProps>`，含浪费的空 `String` 存根）压缩为紧凑的 SoA i64 列——即"压缩缓存"的改动。

用户需求：(1) 确认当前代码是否有已记录的基准测试 trace；(2) 如果没有，运行它并**留下 trace**；(3) 预测 **SF100** 是否支持以及运行时间。已确定的范围：**重新测量 SF30（在新 BaseGraph 路径上的校准）+ SF100**，仅基准测试**新压缩代码**。

### 已调查确定的结论

- **不存在新的 trace。** 今天（06-07）仅在 SF1 上进行了测试（重建 store，运行了验证并产生了 `*-failed-*.json`）。唯一的 SF100 trace 是 `logs/benchmarks-sf100-basegraph-20260602_180258/`——它早于 `6e9f74d` 和缓存压缩，对于当前问题来说是过期的。
- **发布二进制文件已过期：** 构建于 `06-07 05:41`，但 `props.rs` 编辑于 `17:01`。压缩改动**未包含在当前二进制中** → 必须重新构建（runner 在步骤 1 会自动处理）。
- **SF100 BaseGraph store 已存在**（`store/sf100-base-graph`，66 GB）；SF30 也有（`store/sf30-base-graph`，21 GB）。无需 37 分钟的重建。
- **硬件资源充足：** 503 GB RAM（449 可用），64 核，550 GB 磁盘可用。

### 参考数据（用于与新运行结果对比）

| 运行 | 路径 | 启动时间 | 服务器 RSS | 吞吐量 |
|------|------|----------|------------|--------|
| SF10（05-30）| 旧版缓存 | 285 s 缓存加载 | — | 49.19 op/s |
| SF30（06-01）| **旧版** `sf30-bench`（112 GB）| ~15分10秒（910 s）| ~100 GB | 17.57 op/s |
| SF100（06-02）| base-graph，**修复前** | ~394 s（114 s 视图 + 278 s / 282 M 顶点）| ~82 GB | 仅 smoke；IC12 14.7 s / IC13 10.4 s / IC14 11.2 s（易 OOM）|
| SF100 base 构建（06-02）| 一次性 | 37.5 分钟 | 85 GB | 已完成 |

## 计划

### 第一步：飞行前检查（只读）

- 确认 `store/sf30-base-graph/base_graph/catalog.json` 和 `store/sf100-base-graph/base_graph/catalog.json` 存在（→ 复用 base build，不重建）。
- 确认 `free -g` 仍显示约 ≥400 GB 可用，`df -h /data` ≥100 GB 可用。
- 确认 `cargo`、`java`、`mvn` 在 PATH 中（按 `LSMGRAPH-STARTUP-GUIDE.md` §1.3）。

### 第二步：重新构建 + 运行基准测试（核心操作；产生 trace）

使用现有的 runner `deps/ldbc_snb_interactive_impls/lsmgraph/run_linux_benchmarks.sh`（它会先重新构建二进制文件 → 包含 `props.rs` 的压缩改动；当 `FORCE_BASE_BUILD=false` 时复用现有 base stores；输出 `progress.log` + 每 scale 的 server/driver 日志 + `*.time.txt` + `dynamic-stats.json` + storage-bench JSON）。

**在后台运行**，并轮询 `progress.log`：

```bash
cd /data/WorkSpace/lsmgraph-rs
export PATH="$HOME/.cargo/bin:$PATH"
export JAVA_HOME="${JAVA_HOME:-/usr/lib/jvm/java-11-openjdk-amd64}"; export PATH="$JAVA_HOME/bin:$PATH"
OUT_DIR=logs/benchmarks-sf30-sf100-current-$(date +%Y%m%d_%H%M%S) \
RUN_SF10=false \
RUN_SF30=true \          # 必须显式指定："auto" 在 SF10 关闭时会跳过 SF30
RUN_SF100=true \
RUN_SF100_SMOKE=true \
RUN_BASE_BUILD=true FORCE_BASE_BUILD=false \   # 复用现有的 21G/66G stores
RUN_STORAGE_BENCH=true \
RUN_DRIVER=true \
PROGRESS_INTERVAL=30 \   # 比默认 60s 更细粒度的 server RSS 采样
SERVER_READY_TIMEOUT=1200 \
deps/ldbc_snb_interactive_impls/lsmgraph/run_linux_benchmarks.sh
```

关键捕获点（runner 已自动生成）：
- **服务器启动时间**：`${scale}-server.log` 中的 `[snb-server] ... total_elapsed_s=`
- **服务器峰值 RSS**：`progress.log` 中 `running ... ps=[...]` 行的 `rss=` 字段（KB）。与 6 月 2 日的约 82 GB 进行对比。
- **吞吐量**：从 `${scale}-driver.log` 解析 warmup + run 的 `op/s`
- **慢查询 / 正确性**：driver 日志中每操作摘要；服务器的 `/metrics`
- **CSR 存储读取**：`${scale}-io-{blocking,direct}.json`

预计墙上时钟时间：约 40–60 分钟（构建 ~1–3 分钟；SF30 ~8–12 分钟；SF100 ~22–30 分钟）。

### 第三步：留下痕迹（留痕）

- 上述 `OUT_DIR/` 即为主要机器可读的 trace。
- 在仓库根目录编写人类可读摘要 `lsmgraph-test-run-summary-20260607.md`，格式参照 `lsmgraph-test-run-summary-20250530.md`：按 scale 分列服务器启动时间、**峰值 RSS（新压缩缓存）vs 6 月 2 日约 82 GB 基线**、warmup/run 吞吐量、慢查询表、storage-bench 数据，以及 SF100 结论。
- 在 `LSMGRAPH-ARCHITECTURE.md` §12 追加一行，说明 SF100 现在可在 BaseGraph 路径上端到端运行（RSS X GB，启动 Y s，Z op/s）。
- 保存一个 `project` memory 记录 SF100 可行性结论 + trace 路径，并创建 `MEMORY.md` 索引（两者目前均不存在）。

### 第四步：SF100 预测（基于新测得数据）

报告，填入实测值：
- **可行性**：内存余量（实测 RSS vs 503 GB）以及完整 driver 现在能否完成（IC14 OOM 已修复）。预期：**是，支持** ——瓶颈现在是复杂读取延迟，而非内存。
- **运行时间**：服务器启动（约 5–7 分钟，顶点属性加载主导）+ driver 运行；store 预建情况下端到端约 40–60 分钟可复现。
- **SF300 前瞻**：约 3× 数据量 → 预估 RSS ~165–210 GB（可放入 503 GB），但需要一次性约 2 小时 BaseGraph 构建和约 200 GB store；标记为"可行但尚未构建"。

## 验证

- `progress.log` 以 `script_exit status=0` 结尾。
- 每个 `${scale}-server.log` 包含 `{"server":"lsmgraph-snb",...}`（服务器已绑定）和 `total_elapsed_s=` 启动行。
- 每个 `${scale}-driver.log` 显示 Maven `BUILD SUCCESS` 和解析出的 `op/s`。
- 从 `progress.log` 捕获 SF100 服务器的峰值 `rss=`；与约 82 GB 对比。
- SF100 完整 driver 无 OOM 完成（IC14 修复已生效）；记录任何 `FAILED SCHEDULE AUDIT`（在 `time_compression_ratio=0.001` 下预期，不算崩溃）。
- 摘要文档 + memory 已写入；`OUT_DIR` 产物存在。

## 注意事项 / 风险

- 如果 `cargo build` 在未提交的 `props.rs` 上失败，在运行前修复编译错误（该二进制从未基于此编辑构建过）。
- `interactive-benchmark-sf{30,100}.properties` 是单线程，warmup=100，operation_count=250——较小，所以 driver 阶段较短；主要开销在服务器启动。
- 如果仅需要端到端可行性/吞吐量，可禁用 storage-bench（`RUN_STORAGE_BENCH=false`）以节省约 5–8 分钟。
