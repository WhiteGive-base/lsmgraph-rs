# W2 工单：C10 固定开销剖析与消减 + 引擎冻结 + W6 就绪（10 小时执行计划，2026-06-13）

> 给单个 Codex 会话的完整执行计划。目标三件事：
> (1) 把每次 CSR 探测的固定开销分解清楚并消减（这是把「read_bytes 降 99% 但延迟只降 9-32%」
> 变成「延迟数倍收益」的杠杆，同时直接产出 Gate 1 需要的机制解释数据）；
> (2) 到点冻结引擎（打 tag），之后只许 bug fix；
> (3) 写好并干跑 W6 runner，让下一个会话可以直接发射 SF100 战役。
> **本会话禁止启动 W6 / 任何 SF100 级任务。**

## 前置状态（已核实，勿重做）

- HEAD = `ab24ce4`（C-pass 锚 f0378f8 + W1-fix + W5/W3/W4 全部合入，Linux 全测通过）。
- W1-fix 复跑：SF30 schema import 19:35.82、final flush 1.6s、MaxRSS 2,289,660KB、
  `DEGREE_DIRECTORY`=24B、sidecar 警告 0。Bug A/B/C（见 `baseline/w1-sf30-blocker-diagnosis-20260612-cn.md`）已修复。
- 机器：/data ~647G 可用、MemAvailable ~449GiB、无残留进程。
- C10 原始靶子：旧码 SF100 budg-b64 `csr_get_neighbors_avg_us=391`，而每次 body 读仅 ~180B。
  注意该数字测于旧读路径（全量 metadata cache 命中、RAM 二分），W1-fix 后的现状数字必须重测，
  不要拿 391μs 直接对账。

## 硬规约（全程）

- /data <200GiB 或 MemAvailable <80GiB → 硬停报告。构建/测试 `nice -n 10`、`-j 16`。
- 单 SF100 任务原则本会话退化为：**不开任何 SF100 store**。SF30 级 import（~20min）启动前在
  run-notes 里写 ETA + abort 线（60min）+ 进度信号（store du 心跳），沿用 W1 runner 模式。
- 所有跑批留痕 `remote-logs/w2-c10-20260613/` + 收尾写 `baseline/w2-c10-profile-20260613-cn.md`。
- perf 若因 `perf_event_paranoid` 不可用：不要 sudo 折腾，直接以插桩计数器为主（这正是 T1 先做插桩的原因）。
- release 构建需要符号时用环境变量，不改 Cargo.toml：`CARGO_PROFILE_RELEASE_DEBUG=true cargo build --release`。

## 时间表（T+ 为会话内小时:分；每段含验收）

### T0 0:00–0:20 自检与基线核对
- `git log --oneline -5` 确认 HEAD=ab24ce4、worktree clean；df/free/ps 三查；
  删除遗留 `store/w1-smoke-20260612/`（若 W1-fix 复跑未删，~47GB，先 `du -sh` 确认再删）。

### T1 0:20–1:40 分阶段计时插桩（冻结前最重要的代码增量）
- 在 reader 探测路径加 per-probe 阶段累计计数器（挂入现有 metrics 原子计数器体系，常开、廉价）：
  `probe_reader_setup_us / probe_bloom_us / probe_offset_lookup_us / probe_body_read_us / probe_total_us`
  + 各阶段次数；进 `snapshot_json()`，bench JSON 自动带出。
- 区分三类探测结局并分别计数：bloom-negative（应零 IO）、offset-miss、body-hit。
- 单测（计数器断言，沿用 W1-fix 的纪律）：单文件单源探测后各阶段计数 >0 且 total≈各阶段和；
  bloom-negative 探测的 offset/body 阶段计数为 0。
- 验收：`nice -n 10 cargo test -j 16` 全绿；新计数器出现在 storage-bench JSON。

### T2 1:40–2:10 带符号 release 构建 + SF1 烟测
- `CARGO_PROFILE_RELEASE_DEBUG=true nice -n 10 cargo build --release -j 16`。
- SF1 schema import + `storage-bench --warmup-runs 1 --repeats 3 --samples 500`：
  验收 = cache 命中率 >0（W1-fix 生效）、avg 在 µs–低 ms 级、阶段计数器非零且自洽。

### T3 2:10–3:00 SF30 双店导入（剖析用，保留到会话结束）
- 串行 import `schema` 与 `budg-b64` 两个 SF30 store（各 ~20min，abort 60min，心跳监控）。
- store 放 `store/w2-c10-20260613/`，**优化迭代期间复用，不删**（磁盘 ~90GB，富余）。

### T4 3:00–4:15 基线测量 + 剖析
- 两店各跑 `storage-bench --samples 5000 --warmup-runs 1 --repeats 3`，存 JSON。
- 产出**阶段分解表**（per-probe 平均：setup/bloom/offset/body，bloom-negative 占比）——
  schema vs budg-b64 各一行；这张表同时是 Gate 1 的机制解释素材，论文直接可用。
- `perf record -g --call-graph dwarf -F 499 -- <bench 命令>`（或 `-p` attach 一轮），
  `perf report --stdio | head -60` 存档；perf 不可用则跳过，仅靠插桩。
- **决策点 D1**：若 per-probe 固定开销（total 减 body）已 < ~50μs 且 avg_us 与 candidate 数已成比例
  → 说明 W1-fix 已吃掉大头，**跳过 T5 直接去 T6 冻结**，把省下的时间给 T7 的 W6 runner 打磨
  （多干跑一轮、补 oracle/kv-lsm 路径细节）。

### T5 4:15–7:30 优化时间盒（最多两个 fix，每个 ≤90–120min，独立子时间盒）
按剖析结果从下面候选挑（按 gap-analysis 预判的优先序，以实测为准）：
1. **每查询新建 CsrReader / 探测路径堆分配**（graph.rs:2043 一带）：reader 复用（engine 持有或 per-query 一次）、
   热路径去 Vec/Box 分配、Arc clone 精简。
2. **async 每探测开销**：bloom-negative 探测做同步快路径（纯 RAM 判定不应跨 await）；
   候选段探测批量化，减少 per-probe future/调度开销。
3. **小 pread 合并**：offset 二分的 24B preads 若占大头，改读一个 4KB 对齐窗口做窗口内二分
  （把 ~5 次 syscall 变 1-2 次）。
- 每个 fix 流程：实现 → `cargo test` 全绿 → SF30 两店复测（同协议）→ 与 T4 基线比：
  **avg_us 改善 ≥15% 或某阶段计数明确归零才保留，否则 revert**。
- 严禁：改任何布局/预算/语义行为；动 W3/W4/W5 的功能面。

### T6 7:30–8:15 冻结
- 终表：SF30 schema/budg-b64 优化前后对照（avg、p50/p99、阶段分解、candidate、read_bytes）。
- `cargo fmt` + 全量测试 → commit（消息注明 W2 范围）→ **`git tag engine-freeze-sigmod2027`**。
- 冻结含义写进 commit/状态文档：此后到 W6 数据落地，只允许 bug fix，禁改性能/布局行为。

### T7 8:15–9:40 W6 runner 编写 + SF1 干跑（不启动 W6 本体）
- 写 `baseline/run_w6_sf100_matrix_20260613.sh`，按 PLAN §2-W6 规格：
  9 变体 naive(先建后删,锚)/schema/edge-type-only/semantic/budg-b64/b256/b1024/kv-lsm/oracle；
  每变体 import(/usr/bin/time -v) → bench(--samples 5000 --warmup-runs 1 --repeats 3) →
  csr-compare vs naive(s100) → 记维护行 → 删 store；A/B/A 哨兵（schema 末尾复跑）；
  心跳/ETA/abort（单变体 import >3h 或资源线触发即停）；DONE 标记；输出目录带日期。
- **SF1 干跑全矩阵**（每变体分钟级）：验收 = 9 变体全部跑通、JSON 字段齐全（含 T1 新计数器）、
  compare 0 mismatch、kv-lsm 与 oracle 路径真实可用、汇总脚本能从 JSON 再生表格。
- 干跑发现的 runner bug 当场修（这是干跑的目的）。

### T8 9:40–10:00 留痕收尾
- 写 `baseline/w2-c10-profile-20260613-cn.md`：阶段分解表、perf 摘要、已收/已回滚的优化、
  冻结 tag、W6 放行清单（剩余条件：无；或列出未尽事项）。
- 追加 implementation-status / rerun-status 对应小节；确认 worktree clean、无残留进程、店已清理
 （保留 SF30 双店与否：默认删除回收 90GB，若 24h 内将跑 W6 且磁盘富余可保留并在文档注明）。

## 产出物清单（下个会话靠这些放行 W6）
1. tag `engine-freeze-sigmod2027`（含插桩 + 已验收的优化）。
2. `baseline/w2-c10-profile-20260613-cn.md`（阶段分解 + 前后对照 + 决策记录）。
3. `baseline/run_w6_sf100_matrix_20260613.sh` + SF1 干跑全绿 trace。
4. 全量测试绿、机器干净的收尾记录。

## 不做清单
- 不启动 W6/SF100 级任务；不跑 LiveGraph/LSMGraph；不改论文文本；
- 优化不碰语义/布局/预算行为；D1 触发时不为了优化而优化——按期冻结比榨干收益更重要
 （SF100 战役只打一次，时间盒纪律高于单点收益）。
