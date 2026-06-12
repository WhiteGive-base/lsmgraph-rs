# SemL0 Linux 补跑清单（2026-06-11）

> 原则：一切性能事实以 Linux 端 `/data/WorkSpace/lsmgraph-rs` 为准。本地文件只作为整理和写作副本。

## 0. 当前状态

- 远端分支：`codex/sf100-basegraph-bench`
- 远端 HEAD：`e3c178dd09935ce22982b5832f0a0717699ea4b7`
- 主 baseline 目录：`/data/WorkSpace/lsmgraph-rs/baseline`
- SF100 主结果目录：`/data/WorkSpace/lsmgraph-rs/remote-logs/qslsm-sf100-strong-baseline-20260610`
- `qslsm-sf100-strong-baseline-20260610` 主变体已完成：
  - schema
  - edge-type-only
  - semantic
  - budg-b64
  - budg-b256
  - budg-b1024
  - kv-style
- `naive@s5000` 已完成。主结果：read 25.44 GiB / 26052.88 MiB，candidate L0 49,257,601，L0 files 1,703，avg 5382.8 us，import 3801.7 s。

## P0. 接入 naive@s5000（已完成，需确认文档/提交）

目的：

```text
锁定 SF100 主表中相对 naive 的最终 read/candidate/latency 降幅。
```

已定位产物：

- `remote-logs/qslsm-sf100-strong-baseline-20260610/naive-bench.json`
- `remote-logs/qslsm-sf100-strong-baseline-20260610/naive-stats.json`
- `remote-logs/qslsm-sf100-strong-baseline-20260610/naive-s5000.launcher.log`
- `baseline/sf100-results-s5000.tsv`
- `baseline/sf100-strong-baseline-summary.md`
- `baseline/sf100-strong-baseline-traces/summary-sf100.tsv`

检查命令：

```bash
cd /data/WorkSpace/lsmgraph-rs
bash baseline/run-status.sh
```

论文可填数字：

```text
naive@s5000: 25.44 GiB / 26052.88 MiB, candidate L0 49,257,601, L0 files 1,703, avg 5382.8 us.
schema vs naive: read -99.43%, candidate -87.96%, avg latency -8.9%.
budg-b64 vs naive: read -99.44%, candidate -87.77%, avg latency -32.0%.
```

剩余事项：把相关修改 commit；`baseline/sf100-strong-baseline-traces/summary-sf100.md` 如仍无 naive 行，可重新渲染或注明以 `baseline/sf100-strong-baseline-summary.md` 和 TSV 为准。

## P1. SF100 correctness compare

目的：

```text
给 SF100 关键变体补 exact-proof 或 sampled correctness，避免审稿人质疑语义剪枝漏边。
```

最低要跑：

- schema vs edge-type-only
- schema vs budg-b64
- schema vs budg-b256
- schema vs semantic

指标：

- checked sources
- checked edge types
- mismatches
- missing/extra neighbor count

论文目标：

```text
Key SF100 semantic variants are sampled-checked against schema with zero mismatches.
```

如果 SF100 全量 compare 太重，接受 sampled compare，但必须写清 sample plan 与 storage-bench 一致。

## P2. 维护代价补表

目的：

```text
回答“是不是用更多文件、更高写放大换读性能”的顶会审稿问题。
```

主表字段：

| Field | 说明 |
|---|---|
| import_s | 已有 |
| throughput_e/s | 已有 |
| store_bytes | 大部分已有 |
| L0 files | 已有 |
| manifest_bytes | 需要统一提取 |
| max RSS | 部分已有 |
| flush time | 需要补 |
| compaction rewrite bytes | 需要补 |
| update throughput | 需要补 |

最低可接受版本：

```text
schema / edge-type-only / semantic / budg-b64 / budg-b256 / budg-b1024 的 import, store, L0 files, manifest, max RSS。
```

更强版本：

```text
加入 foreground update workload，报告 update throughput 和 p99 write latency/stall proxy。
```

## P3. reader over-read 处理

现象：

- SF100 semantic read MiB = 9994.25
- SF100 budg-b1024 read MiB = 700.07
- schema/edge-type/budg-b64/b256 约 146-150 MiB

已确认根因：

- metadata cache 4096 entries；
- full semantic 6615 L0 files；
- reader 读取整段 offset array；
- `read_bytes` 被 cache 抖动和 offset-array 读粒度污染。

两条路线：

1. 不修，主文加注脚：
   - 主指标用 candidate L0、latency、L0 files、store/import。
   - `read_bytes` 对 degree-heavy SF100 variants 加 caveat。
2. 修 precise offset read：
   - 不读整段 offset array，只读目标 source 需要的 offset entry 或加稀疏索引。
   - 重跑 semantic 和 budg-b1024。

建议：

```text
如果目标是先成稿，走路线 1；如果要减少审稿风险，做路线 2。
```

## P4. LiveGraph 外部 baseline

目的：

```text
顶会主文至少需要一个真实外部系统数值对照。
```

当前状态：

- `deps/LiveGraph/build/liblivegraph.so` 可用。
- `baseline/external-drivers/livegraph_driver.cpp` 已有。
- 还缺同一边集导出和同一 sample plan 跑通。

推荐顺序：

1. 增加或确认 `scan --dump-edges <file>`。
2. 用 lsmgraph 导出 `(src, edge_type, dst)`。
3. SF10 smoke：
   - load
   - sampled get_neighbors
   - correctness count check
   - latency/RSS
4. SF100 串行跑 LiveGraph。

公平指标：

- load time
- peak RSS
- memory/store footprint
- avg/p50/p99 latency
- throughput
- sampled correctness

不要跨系统比较：

```text
read_bytes
```

因为 read_bytes 是 LSM 内部读放大指标，LiveGraph/Teseo/GraphOne 不同构。

## P5. end-to-end LDBC 对照

目的：

```text
把 storage-bench 的内部收益连接到真实查询入口。
```

最低建议：

- SF10 或 SF30。
- schema vs budg-b64。
- LDBC driver 或 server path。
- 报告 IC/IS/IU 分类 latency、throughput、RSS。

如果时间有限：

```text
只跑 query-semantic 相关的 typed-neighbor-heavy subset，但要说明该 subset 对应本文 storage signature。
```

## P6. sustained feedback run

目的：

```text
把 feedback 从 microbenchmark 提升到系统证据。
```

建议实验：

- 30 到 60 分钟。
- phase A -> phase B -> phase C。
- feedback vs no-feedback。
- 每 5 分钟记录：
  - candidate L0
  - latency
  - rewrite bytes
  - write bytes
  - L0 files
  - selected ranges

论文价值：

```text
Controlled long-run trace shows that feedback compaction follows workload shifts without uncontrolled rewrite growth.
```

如果不跑，主文只能写 controlled microbenchmark。

## P7. full-compact@SF100

状态：

- 旧 full-compact SF100 单次合并峰值约 472 GB，OOM。
- 流式合并已在 SF1 验证 mismatch=0。

建议：

- 若资源允许，补跑 stream full-compact@SF100。
- 如果仍失败，保留为 boundary：

```text
Full L0-to-L1 compact is an extreme write-cost baseline. It is complete at SF30 but exceeds current SF100 compaction resource bounds.
```

不要把 full-compact 缺失说成 SemL0 失败，它是极端 baseline 的实现/资源边界。

## 性能是否足够

当前性能足够支撑：

- SemL0 方向成立；
- 内部 layout 消融强；
- SF100 真实规模可跑；
- budgeted materialization 有必要性；
- `budg-b64` 是强主表点。

当前性能还不足以直接支撑：

- 顶会完整实验闭环；
- 外部系统优于/接近结论；
- 生产级写停顿安全；
- 完整 end-to-end workload 收益；
- broad schema migration。

最短补强路径：

```text
P0 naive@s5000
P1 SF100 correctness compare
P2 maintenance cost table
P4 LiveGraph SF10/SF100
P3 reader over-read 注脚或修复
```
