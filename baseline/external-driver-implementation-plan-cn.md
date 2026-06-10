# 外部系统对照 driver 实现方案（LiveGraph / Teseo / GraphOne / LLAMA）

目标：给 SemL0 补**真实数值**外部对照。本文把实现决策、公平性方法学、各系统 API、构建/运行命令固定下来，
让 driver 编码可直接执行；实际 SF100 加载需在内部 SF100 主跑释放内存后进行（见末尾资源说明）。

## 0. 现状（已就绪）
- `deps/LiveGraph/build/liblivegraph.so`(+`libcorelib.a`, `bind/livegraph.hpp` 干净 API)
- `deps/teseo/build/libteseo.a`（autotools）
- `deps/GraphOne/build/graphone32`,`graphone64`（已构建；TBB 已装 `/usr/lib/x86_64-linux-gnu/libtbb.so`）
- 工具链 g++ 9.4 / cmake 3.16。

## 1. 公平性方法学（关键，写进论文 methodology）
异构系统**不**对齐逐条 vertex id，而是**等价工作负载对照**：
- 同一份 LDBC SF100 边集（与 lsmgraph 相同的 `dynamic/`+`static/`，相同 9 个 edge type）。
- 每个 edge type 抽 K 个 source（与 lsmgraph storage-bench 同 K、同度数分层 low/med/high），跑
  `get_neighbors(src, edge_type)` 全量扫描。
- 报告：**p50/p99/avg 延迟、吞吐、峰值 RSS、加载时间、内存/磁盘占用**。
  **不**跨系统比 "read bytes"（lsmgraph 的 read-amp 是 LSM 概念；Teseo/GraphOne 内存态无可比物）。
- 正确性：抽样 source 的 neighbor 计数与 lsmgraph 对齐（同一 edge 集应得同样度数）→ sanity check。

## 2. 全局 vertex id 重映射（最硬的一步）
LDBC id 是**按实体类型分段**的（Person 100 ≠ Comment 100）。外部系统要全局 id：
- 方案：单遍扫描所有边端点，`(entity_type, ldbc_id) -> dense_global_id`（连续计数）。
  用一个 `unordered_map<__int128/struct,uint64>` 或每类型一个 `unordered_map<int64,uint64>`。
  SF100 约 282M 顶点 → ~GB 级内存，可接受。
- LiveGraph `vertex_t=uint64`，默认 `max_vertex_id=1<<40`(~1.1e12) 足够 dense id。
- 把重映射表落盘（`deps/_external_work/sf100-idmap.bin`）供各系统 driver 复用 + 把 lsmgraph 抽样的
  source 也翻译到同一 dense 空间（用 lsmgraph 的 (label, external_id) → dense_global_id 查表）。

## 3. edge type 编号（与 lsmgraph 一致，见 src/snb/queries.rs:511）
1=Knows 2=HasCreator 3=HasTag 7=LikesComment 8=LikesPost 9=ReplyOfComment 10=ReplyOfPost
11=ContainerOf 12=HasMember（workload 子集）。其余 4..6,13..17 非 workload。
LDBC CSV 源（src/snb/full_loader.rs）：person_knows_person→1；comment/post_hasCreator_person→2；
*_hasTag_tag→3；person_likes_comment→7；person_likes_post→8；comment_replyOf_comment→9；
comment_replyOf_post→10；forum_containerOf_post→11；forum_hasMember_person→12。

## 3b. 边集来源（2026-06-10 复查，重要）
LDBC SF1/SF100 是**复合实体文件**格式：hasCreator/replyOf/containerOf **内嵌在 comment/post/forum
实体 CSV 的列里**，不是独立 edge 文件（只有 knows/hasTag/likes/hasMember 是独立文件）。直接重解析复杂
且容易与 lsmgraph loader 不一致。lsmgraph 当前**无批量边导出**（`scan` 只报指标、`neighbors` 单点）。
→ **干净的 enabler：给 lsmgraph 加 `scan --dump-edges <file>`**（scan 已遍历全部边，仅需把
`(src, edge_type, dst)` 写出）。好处：外部系统加载与 lsmgraph **完全相同的边集 + 相同 vertex-id 空间**，
sample plan 的 source 直接对齐，无需重解析 LDBC。该改动是**纯增量**（不碰 import/storage-bench），安全。
注意：**不要在 SF100 主跑进行时重建 `target/release/lsmgraph`**（主跑会为后续变体 spawn 新进程，
即便增量改动也应等主跑结束再 rebuild，杜绝风险）。→ 外部实现整体安排在主跑结束后。

## 4. 共享转换器（消费 `scan --dump-edges` 输出，供 4 个系统复用）
`deps/_external_work/convert_ldbc_edges.py`：
- 入参 `--input <ldbc-sfN/social_network> --edge-types 1,2,3,7,8,9,10,11,12 --out <dir>`。
- 输出：`edges.bin`（packed `u64 src_global, u16 etype, u64 dst_global`）+ `idmap.bin` +
  `sources-by-etype.bin`（每 etype 的 source 列表，供抽样）。
- 复用 lsmgraph 已生成的 sample plan 的 source（翻译到 dense id）以保证与内部对照同样的 source 分布。

## 5. 各系统 driver（薄 C++，统一输出 JSON：load_s, peak_rss_kb, 每 etype p50/p99/avg/throughput）
### LiveGraph `deps/_external_work/livegraph_driver.cpp`
```
lg::Graph g(block_path, wal_path);
auto tx = g.begin_batch_loader();
for each edge: tx.put_edge(src, (lg::label_t)etype, dst, std::string_view());
tx.commit();
auto r = g.begin_read_only_transaction();
for each sampled src,etype: auto it=r.get_edges(src,etype); while(it.valid()){++cnt; it.next();}  // 计时
```
构建：`g++ -O2 -std=c++17 -I deps/LiveGraph/bind livegraph_driver.cpp -L deps/LiveGraph/build -llivegraph -lpthread -o livegraph_driver`（运行设 `LD_LIBRARY_PATH=deps/LiveGraph/build`）。

### Teseo `deps/_external_work/teseo_driver.cpp`
API 见 `deps/teseo/README.md`/`include/teseo.hpp`：`teseo::Teseo`，`start_transaction()`，
`insert_edge`，`scan`/`iterator`。链接 `-L deps/teseo/build -lteseo -ltbb -lpthread`（静态 .a）。

### GraphOne `deps/_external_work/graphone_driver.cpp`
仿 `deps/GraphOne/example.cpp`/`main.cpp`：`schema(g)`、`batch_edge`、`graph_view`/`get_nebrs`。
也可直接用已构建的 `graphone32` 喂转换后的 bin（其 loader 是 -s text/bin）。链接 TBB。

### LLAMA（可选，第 4 个）
`external-system-LLAMA-artifact-report.md` 模板已在仓库；多版本 CSR 快照，作为 snapshotted-CSR 对照。

## 6. 运行顺序与资源
- 先 **SF10** 验证全链路（4G 输入，加载快，不与 SF100 主跑抢内存）。
- SF100 外部加载（LiveGraph/GraphOne 内存态可能 100GB+）必须在内部 SF100 主跑（峰值 ~219GB）**结束后**串行跑，避免 OOM（机器 503GB，单系统安全）。
- 产物落 `remote-logs/external-<system>-<scale>-<date>/`，汇总进 `baseline/external-baseline-comparison.md` 数值表（过复现门槛者）。

## 6b. 各系统语义（2026-06-10 实测 API，影响公平性）
- **LiveGraph**（最匹配）：有 `label`(=edge type)、有向、持久事务。load=`begin_batch_loader`+`new_vertex×N`
  （必须先建稠密顶点）+`put_edge(src,label,dst,"")`+`commit`；read=`get_edges(src,label)` 迭代。
  **driver 已完成并通过合成图冒烟**（`baseline/external-drivers/livegraph_driver.cpp`+Makefile）。
- **Teseo**：**无向、带权(double)、无 label、无多重边/自环**，顶点 ∈[0,2^64-2]。
  → 每个 edge type 建**独立 Teseo 实例**（只灌该类型边，weight=1.0）；`insert_vertex`→`insert_edge(s,d,w)`；
  read=`tx.iterator().edges(v, logical=false, cb(dst[,w]))`。**caveat：无向**（扫 src 得双向邻居）与 lsmgraph
  有向 get_neighbors 不同，需在论文注明；自环/多重边需在转换器里去除。纯内存（比延迟/RSS，不比 read bytes）。
- **GraphOne**：`batch_edge` + typekv schema + `graph_view`/`get_nebrs`（见 `deps/GraphOne/example.cpp`）；
  也可直接用已构建的 `graphone32` 喂转换后的 bin。纯内存流式。
- 结论：LiveGraph 进数值表最干净；Teseo/GraphOne 进表需带"无向/无 label/内存态"注脚，或保持定性。
  → 建议主文：LiveGraph 数值 + Teseo/GraphOne 带注脚数值或定性。

## 7. 进入数值表的门槛（复用 external-baseline-comparison.md）
构建+加载同一 SF100 边集+抽样 neighbor 工作负载+抽样正确性 → 通过才进表；否则保持定性。
