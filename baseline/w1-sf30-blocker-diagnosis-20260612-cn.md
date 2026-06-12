# W1 SF30 阻塞根因诊断（2026-06-12 深夜）

> 结论：W1 暴露的是 06-12 C-pass **新引入的 3 个性能 bug + 1 个元数据放大器**（均不影响正确性，
> 所以 debug 单测全绿）。修复规格见 §3；W1 的 SF1 结论在 Bug C 修复前无效，复跑判据见 §4。
> 证据目录：`remote-logs/w1-smoke-20260612/`、残留 store `store/w1-smoke-20260612/`（验证后可删）。

## §1 症状回顾

- SF30 schema import：输入阶段正常（1132s，约 537K rows/s，与旧码持平），**收尾 flush 阶段 >56min 未完成**，
  75min abort（status=143）。对照：e11 旧码 SF30 schema_only 全程 24:01；06-10 旧码 SF100 schema
  收尾 flush 仅 72s。≥3 倍真实回归，且全部在 flush/收尾路径。
- stderr 69 次 `failed to persist degree directory sidecar: No such file or directory`（SF1 也各有 3 次）。
- SF1 bench 病态：avg 11,149μs/查询（比 SF100 的 4,906μs 还慢），每轮 read_bytes 8.27GB（store 才 1.5GB），
  `offset_cache_hits=0 / misses=7,481`（100% miss，容量 4096、文件仅 69 个）。

## §2 根因链（全部已实锤，file:line 为当前未提交工作树）

**Bug A（SF30 阻塞主因）——每次 flush 全量序列化 degree-directory sidecar，O(n²)**
- C11 在增量更新 `update_semantic_indexes_with_l0_metas`（graph.rs:2936，flush apply 经 graph.rs:1388 调用）
  尾部新增 `persist_degree_directory_sidecar_best_effort()`（graph.rs:2979，本 pass 新增）。
- `write_degree_directory_sidecar`（graph.rs:399）每次把**整个目录** collect + `sort_by_key`（:408）
  + 全量写盘 + `sync_all()`（:428）。
- 目录在 SF30 schema import 中长到 **1.86GB ≈ 1.43 亿条目**（`store/w1-smoke-20260612/sf30-schema/DEGREE_DIRECTORY`），
  后期每次 flush 的 persist 要排序 1.4 亿条 + 写 1.8GB + fsync ≈ 数十秒～分钟级 → flush 跟不上输入 →
  收尾积压 56min 仍在排队（heartbeat 中 store 体积 45.04→46.09→45.05GB 振荡 = compaction/flush 仍在缓慢推进）。

**放大器——schema/edge-type-only 布局的文件带"碰巧 exact"的 degree 标注**
- 目录准入谓词 `degree_directory_class_for_l0`（graph.rs:305）本身正确（要求 `degree_class_exact` 且非 Mixed）。
- 但 writer 按内容计算 degree，小分区内容碰巧均匀 → `degree_class_exact=true` → schema 文件大量准入
  （schema 的目录本应为空）。**C4 已在 budgeted 路径修过一模一样的问题**（graph.rs ~1865 的
  `CsrSegmentSemanticOverrides{degree_class: Mixed, degree_class_exact: false}`，见注释
  "segment content can be coincidentally uniform..."），但 schema / edge-type-only 布局构造器没有套用。
- 副作用：目录 RAM ~7-10GB@SF30 import（C4 想省的内存回来了）+ 未付预算的 degree pruning（审稿风险同 budgeted 当时的论证）。

**Bug B（69 次 ENOENT）——persist 并发竞态**
- `max_background_flushes=2` + compaction 后全量 rebuild（graph.rs:2724/2822→2932 也 persist），
  多个 persist 并发共享同一 `DEGREE_DIRECTORY.tmp`（graph.rs:328）：A 的 `rename` 把 tmp 拿走后
  B 的 `rename`/`remove_file` 报 `No such file or directory`。报错本身无害（另一方已写成），
  但说明 persist 完全没有互斥，且败方丢失本次持久化。

**Bug C（bench 全废）——C7 的 `lookup_offset` miss 后从不入缓存**
- 新查询路径 reader.rs:412 `lookup_offset`（由 get_neighbors 两个变体 reader.rs:209/240 调用）：
  cache miss 时读 header（128B）+ **整段持久化 SourceBloom（MB 级，且经 `record_offset_read` 计入 offset_bytes，
  reader.rs:560）** + 24B 二分 pread，然后**直接返回，不插入缓存**。
- 只有 `metadata_for`（reader.rs:453，:469 才插缓存）会填缓存，而查询路径已不再走它 → 缓存永远空 →
  每次探测重读 ~1.1MB bloom。SF1 实测：7,481 misses × ~1.1MB ≈ 8.26GB/轮 ✓，
  offset_reads 39,728 ≈ 每 miss 1 次 bloom + ~4 次二分 pread ✓，avg 11ms/op ✓。
- 推论：**W1 报告的"SF1 semantic read_bytes 仅比 schema 低 0.24%"与"CV 5.63%"在此 bug 下无意义**
  （两个变体都被 bloom 重读地板淹没）；SF1 本来也不是验证 C7 的合适规模（文件数 << 缓存容量）。

## §3 修复规格（建议作为独立工单 "W1-fix"，先于一切合并/复跑）

1. **Bug A**：graph.rs:2979 的 per-flush persist 去掉，改为节流 + 关键点持久化：
   - 保留 compaction/全量 rebuild 处（graph.rs:2932）；
   - 增加 Engine 显式 `persist_semantic_sidecars()` API，import 在 final flush 完成后调用一次；
   - 可选节流（每 N=256 次 flush 或距上次 ≥60s 才 persist）。sidecar 过期无害：
     open 时 expected_file_ids 校验不匹配会自动回退 rebuild（现有语义）。
2. **放大器**：schema / edge-type-only（以及任何不物化 degree 维度的布局）的 flush 构造器套用
   budgeted 同款 override（degree_class=Mixed、degree_class_exact=false，复用 graph.rs ~1865 的结构）。
   回归测试：schema 布局 import 后 degree_directory 条目数 = 0、sidecar 仅 header。
3. **Bug B**：persist 加 `Mutex<()>` 串行化 + tmp 文件名加唯一后缀（pid/seq），双保险。
4. **Bug C**：缓存条目支持轻量档：`CachedCsrMetadata.offsets: Option<Arc<[EdgeOffset]>>`（或 Light/Full 双态）。
   `lookup_offset` miss 后插入 Light{header, bloom}；后续命中走 RAM bloom（负探测零 IO，正探测仅二分 pread）；
   `read_offsets`/`metadata_for` 需要全量时升级为 Full。可选：命中 K 次后升级 Full（热文件回到全内存二分）。
5. **测试纪律（本次教训）**：C-pass 单测只断言了行为语义，没断言 IO/缓存计数器，所以三个 bug 全绿通过。
   修复必须带计数器断言单测：同一文件第二次 lookup 必须 `offset_cache_hits+1`；
   warm 轮 offset_bytes 应为 ~0（仅二分 pread）；schema import 全程 persist 调用次数 ≤ 上限。

## §4 W1 复跑判据修正

- SF1 仅验证：cache 命中率 >0、avg 延迟回到 µs 级、read_bytes 量级合理（≪ store 大小）。
  **不要**在 SF1 看 C7 的 read_bytes 降幅。
- C7 的 read_bytes 验证放 SF30：semantic / budg-b1024（metadata cache 压力下）vs 旧表
  （SF100 semantic 曾 9994MiB vs schema 150MiB；SF30 旧值见 e11 traces）。
- SF30 schema import 预算重设：修复后预期 ~25-30min（e11 为 24min），abort 线 60min。
- 增加检查项：import 全程 sidecar persist 次数（应 ≈ compaction 次数 + 1 次收尾）；
  schema sidecar 大小 ≈ KB 级、semantic 为大文件（设计如此）；schema import RSS 不应再含 GB 级目录。
- 残留 store `store/w1-smoke-20260612/`（~47GB）在复跑前删除回收磁盘。

## §5 验证用证据指针

- 100% miss + 8.26GB/轮：`sf1-schema-bench.json` → `benchmarks[0].rounds[*].neighbor_metrics.csr.{offset_cache_*,offset_bytes,offset_reads}`
- 收尾阶段心跳与 abort：`progress.log`（store_bytes 振荡、4501s 超时）
- 新旧收尾对比：`remote-logs/qslsm-sf100-strong-baseline-20260610/schema-import.stderr`（"flush complete elapsed_s=72.0"）
- sidecar 体积：`ls -la store/w1-smoke-20260612/*/DEGREE_DIRECTORY`
- 历史 SF30 import 墙钟：`baseline/semL0-ablation-results/traces/e11-sf30-schema_only-20260608/time.log`（24:01）
