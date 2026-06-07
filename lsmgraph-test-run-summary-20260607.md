# LSMGraph 测试运行总览 — SF30 + SF100（当前 BaseGraph 路径）

记录时间：2026-06-07
项目路径：`/data/WorkSpace/lsmgraph-rs`

被测代码：
```text
commit 6e9f74d  : 移除 legacy LSM fallback_engine，邻接全部走 BaseGraph CSR + Delta；修复 IC14 all-shortest-paths OOM
未提交 props.rs : Post/Comment 顶点属性缓存 AoS(Vec<PostProps>/Vec<CommentProps>) → 紧凑 SoA i64 列（去掉空 String 占位）
二进制         : target/release/lsmgraph（2026-06-07 20:07 重新编译，已含上述压缩）
原始日志       : logs/sf30-sf100-current-20260607/
```

运行环境：503 GiB RAM / 64 core / NVMe（/data 余 550 GB）。`io-backend=direct`，复用已存在的
`store/sf30-base-graph`（21G）、`store/sf100-base-graph`（66G），未重建 BaseGraph。
Driver 配置（`interactive-benchmark-sf{30,100}.properties`）：`thread_count=1, warmup=100,
operation_count=250, time_compression_ratio=0.001`，IC1-14 / IS1-7 / IU1-8 全开。

---

## 0. 结论速览

```text
SF30 / SF100 均可端到端跑通（server 启动 + 完整 LDBC driver），全程无 OOM，exit=0。

SF100 内存不再是瓶颈：
  加载期瞬时峰值 RSS ~87 GiB，稳态服务 RSS ~50 GiB，机器 503 GiB → 6~10x 余量。
SF100 唯一瓶颈是少数重型复杂读（IC12 15.3s / IC14 11.3s / IC13 ~11s / IC10 4.8s），
  导致 run 阶段 throughput 仅 5.15 op/s 且 schedule audit FAIL；
  短查询(IS) / 更新(IU) / 轻量复杂读 在 SF100 下依然 1~17ms。
IC14 OOM 已修复：本次 11.3s 有界完成，不再吃掉上百 GB。

SF30 在新 BaseGraph 路径上相对 legacy adjacency cache 路径大幅改善：
  throughput 17.57 → 468.21 op/s（~27x）
  冷启动     ~15m10s → 113.9s（~8x）
  server RSS ~100 GB → 稳态 ~16.5 GiB
```

---

## 1. SF30 结果（PASSED SCHEDULE AUDIT）

Server 启动（`sf30-server.log`）：
```text
base/delta view ready              : 32.2s
vertex properties (88,789,833)     : 81.7s
total open / http bind             : 113.9s
```

Server RSS（`progress.log`，30s 采样）：
```text
view 打开期        : ~6.6 GiB
属性加载峰值        : ~27.2 GiB
稳态服务（driver 中）: ~16.5 GiB   ← 加载期约 ~11 GiB 瞬时缓冲在加载完成后释放
```

Throughput（`sf30-driver.log`）：
```text
warmup : 155.49 op/s (102 ops)
run    : 468.21 op/s (243 ops)   PASSED SCHEDULE AUDIT
```

慢查询（run，ms，仅列最慢几项；其余 IC/IS 均 <5ms）：
```text
IC7 14 | IC4 8 | IC12 5 | IS3 4.4 | IS2 2.4
更新 IU6 21 | IU3 19.2 | IU7 15.3 | IU2 15.1 | IU5 9.1
```

---

## 2. SF100 结果（run 阶段 FAILED SCHEDULE AUDIT — 复杂读过慢；完整跑完、无 OOM）

Server 启动（`sf100-server.log`，full run）：
```text
base/delta view ready              : 106.3s
vertex properties (282,637,871)    : 269.3s
total open / http bind             : 375.6s  (~6.3 min)   (smoke 同步为 384.3s)
```

Server RSS（`progress.log`，30s 采样，pid 846207）：
```text
属性加载期         : 78.1 → 80.9 → 83.6 → 85.6 → 87.1 GiB   ← 峰值 ~87 GiB
稳态服务（driver 中）: ~49.6 GiB                              ← 加载期 ~37 GiB 瞬时缓冲释放
对比 2026-06-02 旧版（未压缩、未修复）: server RSS ~82 GiB
```

Throughput（`sf100-driver.log`）：
```text
warmup : 56.12 op/s (100 ops)   PASSED
run    :  5.15 op/s (247 ops)   FAILED SCHEDULE AUDIT (Late 18 > 13 tolerated)
```

Per-query（run，ms，count/mean/p99/max）：
```text
IC12   1   15288   15288   15288     <- 单条 15.3s
IC14   1   11257   11257   11257     <- OOM 修复后有界完成
IC13   2    7702   10904   10904
IC10   1    4850    4850    4850
IC11   2     143     162     162
IC4    2     124     131     131
IC1    1      38      38      38
IS1-7        1.2 ~ 2.6（mean）       <- 短查询 SF100 下依然很快
IU2-8        3 ~ 17（mean）          <- 更新也很快
```

判读：SF100 的代价完全集中在「读巨大邻接 / 多跳」的少数复杂读（IC10/12/13/14）。
这些查询在 BaseGraph CSR 上要从磁盘拉取百万级邻居，单条数秒；其余 90%+ 操作毫秒级。
`time_compression_ratio=0.001` 把整个 schedule 压到 ~0.5s 内发完，于是几条秒级查询就把
Late Count 顶过阈值 → audit FAIL。这是**延迟/吞吐问题，不是可行性/内存问题**。

---

## 3. 横向对比

| 维度 | SF10 (05-30, legacy) | SF30 (06-01, legacy cache) | **SF30 (06-07, BaseGraph)** | **SF100 (06-07, BaseGraph)** |
|---|---:|---:|---:|---:|
| Server 冷启动 | 285s(cache) | ~910s (~15m10s) | **113.9s** | **375.6s (~6.3m)** |
| Server 峰值 RSS | — | ~100 GB | **~27 GiB** | **~87 GiB（稳态 ~50）** |
| run throughput | 49.19 op/s | 17.57 op/s | **468.21 op/s** | **5.15 op/s** |
| schedule audit | FAIL(17) | —(legacy) | **PASS** | FAIL(18) |
| 端到端可跑 | 是 | 是(慢) | 是 | **是（无 OOM）** |

---

## 4. Storage microbench（`base-storage-bench --io-backend direct --samples 5000`）

SF100 部分 CSR 关系 direct-IO 单批（5000 采样源点）耗时摘要（`sf100-io-direct.json`）：
```text
COMMENT_CHILD_COMMENTS : elapsed 2553ms, direct_reads 1704, cache_hit/miss 3296/1704
COMMENT_HAS_TAG/IN     : elapsed 2727ms, direct_reads 3019, neighbor_edges 97,937,925
```
说明底层 CSR direct 读本身是健康的（块对齐 + worker block cache 命中过半）；SF100 复杂读
慢的根因是「一次查询要触达的邻接总量」，而非单次 CSR 读放大。

---

## 5. SF100 可行性 & 运行时间判断

```text
可行性：是。
  - 内存：峰值 ~87 GiB / 稳态 ~50 GiB ≪ 503 GiB，余量充足。
  - 启动：~6.3 min（瓶颈是 2.8 亿顶点属性列加载 ~269s，约 1µs/vertex）。
  - 正确性/稳定性：完整 IC/IS/IU driver 跑完，无 OOM（IC14 已封顶）。

运行时间：
  - 本次 SF30+SF100 全流程（含 storage-bench + smoke + full）：26.6 min（build 为增量空跑）。
  - SF100 单独一次：server 启动 ~6.3min + driver ~1.5min ≈ 8min/次。
  - 如需从 CSV 重建 BaseGraph：一次性 +~37.5 min（store 66G，已存在故跳过）。

真正的瓶颈：复杂读延迟（IC10/12/13/14），不是内存、也不是启动。
```

## 6. SF300 前瞻（外推，未实测）

```text
顶点/边 ~3× SF100 → 稳态 RSS ~150 GiB、加载峰值 ~260 GiB（503 GiB 仍可容纳，但余量收窄）。
startup ~19min（~8.5 亿顶点属性加载）；BaseGraph build ~2h、store ~200 GiB（磁盘 550 GB 够）。
复杂读会进一步变慢（IC12/13/14 → 数十秒），SF300 的「墙」依旧是复杂读延迟。
```

## 7. 下一步建议

```text
1. 优化重型复杂读 IC10/12/13/14：BaseGraph CSR 大邻接的并行/预取/结果裁剪、IC14 缓存复用。
2. 降低加载期瞬时 RSS（~37 GiB 缓冲）：流式构建列、复用 read buffer，为 SF300 留余量。
3. 提升并发：thread_count>1 看短查询/更新吞吐扩展（当前单线程）。
4. 缩短启动：顶点属性列按需/惰性加载，避免一次性 269s 全量装载。
```

原始日志与产物：`logs/sf30-sf100-current-20260607/`
（`progress.log` / `sf{30,100}-server.log` / `sf{30,100}-driver.log` / `*-io-{blocking,direct}.json`
/ `*.time.txt` / `dynamic-stats.json`；driver 结果 JSON 见 `deps/.../dgs/results/LDBC-SNB-DGS-SF{30,100}-results.json`）。
