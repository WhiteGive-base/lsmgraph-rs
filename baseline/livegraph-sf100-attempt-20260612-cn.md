# LiveGraph SF100 stream 尝试终止记录（2026-06-12）

> 结论：LiveGraph SF100 batch-load 在本机（64 核 / 503GiB RAM / 2TB 盘）上**不具备实际可完成性**——
> 按尾段实测速率外推还需 17–21 天，于 2026-06-12 20:54 由用户主动 SIGTERM 终止。
> 本文件是该尝试的 dated trace 存档；SF100 外部基线在论文中降级为
> 「LiveGraph SF10 完整实测 + SF100 load 不可行性观察（limitation/observation）」。

## 运行事实

- run_id：`livegraph-sf100-stream-20260612`，输出目录 `remote-logs/livegraph-sf100-stream-20260612/`。
- 流水线：`baseline/run_livegraph_sf100_stream_pipeline_20260612.sh`
  （scan --dump-edges → convert_livegraph_edges.py → livegraph_driver_stream）。
- 启动资源门限通过：MemAvailable=449GiB、/data free=692GiB（launcher.log）。
- scan 完成：`scan.json` snapshot=3,570,968,680，directed_edges=3,570,968,680。
- convert 完成：`convert-summary.json` vertex_count=282,637,871、edge_count=3,570,968,680，
  dense edge list 72,750,684,584 bytes。
- driver（`baseline/external-drivers/livegraph_driver_stream`，流式读边，避免旧 driver 的全量边表驻留）
  于 15:44 启动。

## 退化与终止依据

- 19:31–19:42 采样：dense 输入 fd 偏移仅前进约 7.6MB，但 write_bytes 增加约 0.89GB，CPU 持续 ~105%，
  判定为 LiveGraph batch-load 后段插入/写回退化（非 OOM、非磁盘不足）。
- 20:16–20:22 采样：fd 偏移前进约 2.7MB/6min，write_bytes +342MB。
- 终止时刻状态（livegraph.stderr 的 GNU time 统计）：
  - wall 5:09:56，user 18717s，sys 914s，CPU 105%；
  - Max RSS = 320,174,300 KB（~305GiB，主要为 block 文件 mmap 页）；
  - major page faults 75,361,083，file system outputs 640,927,128（~306GiB 写回）；
  - 被 signal 15 终止，输入推进约 59.07 / 72.75 GB（~81%，但尾段速率崩塌）。
- 外推：按 20:41 前后最近 1 小时速率，剩余 ~13.7GB 输入需要 **17–21 天**——超出任何合理预算，
  且期间机器禁止一切 SF100 级任务（单 SF100 任务原则），机会成本不可接受。
- 决策：用户拍板「直接释放」，20:54 SIGTERM；`livegraph-block`（314.6GB）、`livegraph-wal`（1GB）、
  `edges-dense.txt`（72.7GB）已删除，/data 从 337GiB 回升到 692GiB 可用。
  证据文件（scan.json / convert-summary.json / launcher.log / livegraph.stderr / progress.log）保留。

## 论文可用结论

1. **SF10 完整实测**（见 `remote-logs/livegraph-sf10-20260612/`）：
   edge_count=355,185,382 三方对账一致；load_s=1502.59；driver wall=27:17；
   peak_rss_kb=47,908,592；block=37,580,963,840B + wal=1,073,741,824B。
2. **SF100 不可行性观察**：LiveGraph 的 1GB 粒度 block mmap + 后段插入退化使 SF100
   （3.57B 有向边）batch-load 在 503GiB RAM / 本盘上不可完成（5 小时仅 81% 输入，尾速外推 17–21 天，
   写放大 >306GiB，RSS ~305GiB）。可作为外部基线小节的 limitation/observation 叙述，
   与我方引擎同机完成 SF100 import（~1.3h/变体，峰值 RSS ~207-217GiB）形成对照。
3. 该结果不宜表述为「LiveGraph 性能差 X 倍」（未跑完，非同负载），只能表述为资源/时间可行性边界。

## 教训 → 新硬规约（已写入 Codex 交接文档 §1）

任何预计 >30min 的任务，启动前必须：
(a) 用小规模/采样外推给出 ETA；(b) runner 内置进度信号（fd offset / write_bytes / 输出行数）；
(c) 预设 abort 条件（如「N 分钟内推进 < X」即停）。本次 SF100 尝试若有 (c)，可在 19:42 即止损。
