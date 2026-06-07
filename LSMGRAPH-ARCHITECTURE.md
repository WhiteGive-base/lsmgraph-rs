# LSMGraph 架构设计文档

> 本文档基于源码分析整理，涵盖系统的完整架构设计。
> 记录时间：2026-06-06
> 最近修订：2026-06-07 —— 同步 `6e9f74d`（移除 legacy LSM fallback、邻接统一走 BaseGraph CSR、顶点属性缓存 AoS→SoA 压缩、IC14 路径枚举封顶修复 OOM）

---

## 1. 项目概述

**LSMGraph** 是一个针对 LDBC SNB（Social Network Benchmark）场景设计的图存储引擎，基于 LSM（Log-Structured Merge）树的写优化思路和 CSR（Compressed Sparse Row）邻接表存储格式，旨在实现高效的图数据读写和查询。

核心技术特点：

- **Graph-aware L0 分区**：将 LSM 分层结构与图的类型化查询语义结合，通过 `src_label` + `edge_type_partition` 对 L0 数据按图结构分区，显著降低 typed 查询的读放大
- **RA-score Targeted Compaction**：基于查询热度和候选 segment 数的打分模型，实现细粒度的增量 compaction
- **双层存储架构**：BaseGraph（不可变 CSR 快照）+ DeltaGraph（LSM 增量更新），分别优化只读查询和写负载
- **SNB 查询引擎**：完整实现 LDBC SNB Interactive 查询集（IC1-IC14、IS1-IS7、IU1-IU8），兼容 DGS HTTP 协议
- **BaseGraph 服务路径**：SNB 邻接查询统一走 BaseGraph CSR + DeltaGraph 合并，顶点属性以紧凑列式（SoA）常驻内存；运行链（server / validation / benchmark）不再依赖 legacy 全量 adjacency cache（Legacy / `snb-cache` 仅为未清理代码），SF100 可端到端运行（详见 §7.3、§12）

---

## 2. 系统整体架构

```
┌──────────────────────────────────────────────────────────────────┐
│                        Application Layer                           │
│           LDBC Driver / Java HTTP Client / CLI Bench               │
└───────────────────────────┬──────────────────────────────────────┘
                            │ HTTP (DGS Protocol)
┌───────────────────────────▼──────────────────────────────────────┐
│                    SNB Query & Server Layer                       │
│                                                                   │
│   ┌─────────────────────────────────────────────────────────────┐  │
│   │              DGS-Compatible HTTP Server                     │  │
│   │   HTTP POST /query/interactive_complex_read_*             │  │
│   │   HTTP POST /query/interactive_short_read_*               │  │
│   │   HTTP POST /query/interactive_update_*                   │  │
│   │   HTTP GET  /metrics                                     │  │
│   └─────────────────────────────────────────────────────────────┘  │
│                                                                   │
│   ┌─────────────────────────────────────────────────────────────┐  │
│   │              SNB Query Engine (queries.rs)                 │  │
│   │   IC1-IC14 复杂读查询 (BFS、聚合、路径)                 │  │
│   │   IS1-IS7  短查询 (点查)                                  │  │
│   │   IU1-IU8  更新操作                                        │  │
│   └─────────────────────────────────────────────────────────────┘  │
│                                                                   │
│   ┌─────────────────────────────────────────────────────────────┐  │
│   │         SnbGraph (props.rs) — SNB 数据抽象层              │  │
│   │   DynamicSnbGraph: 列式顶点缓存 + BaseGraph CSR + Delta │  │
│   └─────────────────────────────────────────────────────────────┘  │
└───────────────────────────┬──────────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────────┐
│                    Dynamic View Layer                             │
│                  (dynamic_view/mod.rs)                            │
│                                                                   │
│   DynamicGraphView = BaseGraph (可选) + DeltaGraph (可选)        │
│                                                                   │
│   读路径: BaseGraph.scan_csr() 合并  DeltaGraph.get_neighbors()  │
│   写路径: DeltaGraph.insert_edge() -> Engine.write_edge()         │
└───────────────────────────┬──────────────────────────────────────┘
                            │
         ┌──────────────────┴──────────────────┐
         │                                     │
         ▼                                     ▼
┌─────────────────────┐            ┌─────────────────────┐
│     BaseGraph       │            │     DeltaGraph      │
│  (base_graph/)      │            │   (delta/mod.rs)   │
│                     │            │                    │
│  不可变 CSR 快照     │            │  LSM 增量存储       │
│  Columnar 属性存储   │            │  MemGraph + L0/L1 │
│  预构建索引          │            │                    │
└─────────────────────┘            └─────────┬─────────┘
                                              │
                                              ▼
                                    ┌─────────────────────┐
                                    │   Engine (graph.rs)  │
                                    │                     │
                                    │  MemGraph (in-memory) │
                                    │  L0 segments        │
                                    │  L1 segments        │
                                    │  Compaction         │
                                    └─────────────────────┘
                                              │
                                              ▼
                                    ┌─────────────────────┐
                                    │   CSR Storage        │
                                    │  (csr/format.rs)    │
                                    │                     │
                                    │  Header (128B)       │
                                    │  Offsets (src→edge) │
                                    │  Edge Bodies        │
                                    └─────────────────────┘
                                              │
                                              ▼
                                    ┌─────────────────────┐
                                    │  IO Backend          │
                                    │  (io/mod.rs)         │
                                    │                     │
                                    │  Blocking (default)  │
                                    │  Direct IO          │
                                    │  io_uring           │
                                    └─────────────────────┘
```

---

## 3. 存储层：Engine（LSMGraph Core）

### 3.1 版本管理

核心数据结构 `Engine` 通过 `VersionManager` + `VersionGuard` 实现了 MVCC（多版本并发控制）：

```rust
// version.rs
pub struct Version {
    pub id: u64,
    pub memgraphs: Vec<Arc<MemGraph>>,   // 内存表，按新→旧排列
    pub levels: Vec<Vec<CsrSegmentMeta>>, // 磁盘层，从 L0 到 Ln
}

pub struct VersionManager {
    current: RwLock<Arc<Version>>,  // 当前版本指针
}
```

**设计要点**：

- `Version` 是不可变的，每次修改（flush、compaction）都 publish 新版本
- 读写操作通过 `pin_current()` 获取版本快照，不阻塞写操作
- `memgraphs` 按时间从新到旧排列，查找时遍历直到找到

### 3.2 MemGraph（内存表）

```rust
// memgraph/mod.rs
pub struct MemGraph {
    inline_segment_capacity: usize,  // 默认 8
    capacity_bytes: usize,          // 默认 64MB
    vertex_map: HashMap<VertexId, AdjRef>,
    low_degree_segments: Vec<Vec<EdgeRecord>>,  // Inline 段
    high_degree: HashMap<VertexId, BTreeMap<...>>, // Overflow 段
}
```

**写入路径**：

1. 插入边到活跃 MemGraph
2. 满时冻结（Frozen），创建新的 MemGraph
3. 发布新版本（memgraphs 头部加入新表）
4. 异步 flush 到磁盘 L0

**存储策略**：采用 inline/overflow 分层——低度数顶点用紧凑向量，高度数顶点用 BTreeMap。

### 3.3 CSR 文件格式

每个 segment 是一个独立的 `.edge` 文件：

```
+------------------------+
|   CsrHeader (128B)     |  magic=0x474C4D4C, version=1
+------------------------+
|   Offsets Array        |  EdgeOffset × N (24B each)
|   [src, first_idx, cnt]|  按 src 有序，支持二分查找
+------------------------+
|   Edge Bodies Array    |  DiskEdgeBody × M (32B each)
|   [dst, ts, prop, type]|  按 src 分组连续存储
+------------------------+
```

关键字段：
- `src_label`：segment 所属的源点标签（Person=1, Post=3 等）
- `edge_type_partition`：segment 覆盖的边类型（0 = MIXED，表示混合）
- `min_src / max_src`：源点 ID 范围

### 3.4 Graph-aware L0 分区

这是 LSMGraph 的核心创新。

**Naive L0 vs Graph-aware L0**：

| 维度 | Naive L0 | Graph-aware L0 |
|------|----------|----------------|
| 每次 flush | 所有边写入 1 个 segment | 按 `(src_label, edge_type)` 分组 |
| Segment 数量 | 少（大批量） | 多（小粒度） |
| Typed 查询候选 | 所有 L0 segment | 仅匹配 label+type 的 segment |
| 适用场景 | 少量 L0 file | 大量 L0 但查询有类型过滤 |

Flush 时 `build_l0_flush_segments` 按以下逻辑分区：

```
if graph_aware_l0:
    edges → 按 (src_label, edge_type) 分组
    if group.size >= min_l0_partition_bytes (4MB):
        保留独立的 (src_label, edge_type) segment
    else:
        合并为 MIXED segment
    按 segment_target_bytes (64MB) 切分
```

### 3.5 读路径：`get_neighbors_typed`

```rust
async fn get_neighbors_typed_internal(src, edge_type, snapshot):
    1. 遍历 memgraphs（从新到旧），收集 insert/delete 记录
    2. 遍历 L0 segments：
       - may_contain_partition(src, edge_type)? → label + type 过滤
       - range_filtered → src 不在 [min_src, max_src]
       - bloom_filtered → SourceBloom 返回 false
       - body_read → 实际读取 edge bodies
    3. 遍历 L1+ segments（通过 MultiLevelIndex 定位）
    4. merge_visible(updates, snapshot) → 去重、时间戳过滤
```

**三级过滤**：

1. **Partition 过滤**：`src_label` + `edge_type_partition` 不匹配则跳过
2. **Range 过滤**：src 不在 segment 的 `[min_src, max_src]` 范围内则跳过
3. **Bloom 过滤**：`SourceBloom`（segment 内所有源点的 Bloom Filter）判断是否可能包含该 src

### 3.6 Compaction 策略

#### 3.6.1 全量 L0→L1 Compact

当 L0 segment 数量超过 `l0_file_threshold`（默认 4）时，触发全量 compact：

- 读取所有 L0 + L1 segments
- `merge_latest_records` 去重（保留最新时间戳的记录）
- 写出一个或多个 L1 segment
- 删除被 compact 的 segments

#### 3.6.2 RA-score Targeted Compaction

基于查询热度的细粒度 compaction：

```rust
score = query_count
      × avg_candidate_segments
      × (1 + offset_cache_miss_rate)
      / estimated_rewrite_bytes_MiB
```

每次查询时，`L0PartitionKey`（src_label × edge_type × range_bucket）记录候选 segment 数和 offset cache miss 情况。`compact_best_l0_partition_by_score` 选择得分最高的 partition 进行 compact，仅重写相关 segments 而非全量。

### 3.7 CSR Metadata Cache

```rust
// csr/cache.rs
CachedCsrMetadata {
    header: CsrHeader,
    offsets: Vec<EdgeOffset>,    // 全部加载到内存
    src_filter: SourceBloom,       // 3-hash Bloom Filter
}

// CsrMetadataCache: LRU cache，max_entries=4096
```

每个 L0 segment 的 offsets 数组（约 24B × 唯一源点数）全部缓存在内存中，配合 Bloom Filter 实现快速的"该 segment 是否可能包含某 src"判断。

---

## 4. 只读存储：BaseGraph

### 4.1 架构

BaseGraph 是从 SNB CSV 数据一次性构建的不可变 CSR 快照，专门优化读路径：

```
base_graph/
├── catalog.json              # 全局目录（所有 CSR/列/索引的元数据）
├── vertices/
│   └── Person/
│       ├── first_name       # String 列（offset_blob 格式）
│       ├── last_name
│       ├── birthday
│       └── ...
├── edges/
│   └── KNOWS/
│       └── OUT/
│           ├── offsets.bin   # CSR offset 数组
│           ├── neighbors.bin # 邻接目标 ID
│           └── meta.json
├── single_edges/
│   └── HAS_CREATOR/
│       └── comment_creator.col  # 单值列
└── derived_indexes/
    └── MESSAGE_ROOT_POST/
        └── post_root_post.col   # 派生列
```

### 4.2 Catalog

```rust
// base_graph/catalog.rs
BaseGraphCatalog {
    vertex_labels: HashMap<String, VertexCatalogEntry>,
    csr_adjacencies: HashMap<String, CsrCatalogEntry>,
    single_columns: HashMap<String, SingleColumnEntry>,
    derived_columns: HashMap<String, DerivedColumnEntry>,
    vertex_properties: HashMap<String, VertexPropertyEntry>,
}
```

定义了所有 8 种顶点类型、22 种 CSR 邻接关系及其属性。

### 4.3 CSR Adjacency 读路径

```rust
// base_graph/csr.rs
CsrAdjacency {
    offsets: Vec<u64>,         // 全量加载到内存
    neighbors_file: CsrDataFile,
    sort_order: SortOrder,
    block_bytes: usize,         // 默认 256KB
    block_caches: Arc<Mutex<HashMap<worker_id, CsrBlockCache>>>,
    prop_block_caches: Arc<Mutex<HashMap<(worker_id, prop_name), CsrBlockCache>>>,
}
```

**读放大控制策略**：

1. `offsets` 全量内存化 → O(1) 定位源点的邻接范围
2. **Block Cache**：每个 worker 独立的 LRU block 缓存（64 blocks × 256KB）
3. **Chunk 策略**：根据度数选择读取粒度
   - 小度数（≤128）：按需读取精确条数
   - 大度数：按 block 对齐读取（256KB / 4B = 65536 neighbors/block）
4. **Direct IO 支持**：通过 `O_DIRECT` 绕过页缓存，避免与 BaseGraph 自身缓存的双重缓冲

### 4.4 Build 流水线

```rust
build_from_snb(csv_root, output_dir, config):
    1. build_id_maps       → 外部 ID → 本地 0-based u32
    2. build_vertex_properties → 顶点属性列
    3. build_single_edges  → 单值边（HasCreator, IsLocatedIn 等）
    4. build_multi_edges   → CSR 邻接表（Knows, Likes, HasTag 等）
    5. build_derived_indexes → 派生索引（post_root_post 等）
```

所有 CSR 以双向存储（OUT + IN），支持高效的正向和反向图遍历。

---

## 5. 增量存储：DeltaGraph

```rust
// delta/mod.rs
DeltaGraph {
    dir: PathBuf,
    engine: Arc<Engine>,  // 独立的 LSMGraph 实例
}
```

DeltaGraph 是一个独立的 Engine 实例，专门处理增量更新（IU1-IU8 操作）：

- 所有更新通过 `insert_edge` / `delete_edge` 写入
- 自动 flush 到 L0 segments（可选 graph-aware 分区）
- 支持 compact 到 L1

---

## 6. 动态视图：DynamicGraphView

```rust
// dynamic_view/mod.rs
DynamicGraphView {
    base: Option<Arc<BaseGraph>>,
    delta: Option<DeltaGraph>,
}

async fn scan_neighbors(base_csr, base_src, delta_src, ...):
    # Base 图路径
    if base_csr && base_src:
        base.scan_csr(csr, src, read_ctx, f)
    # Delta 图路径
    if delta:
        delta.get_neighbors(delta_src, edge_type, snapshot)
```

**读合并策略**：BaseGraph CSR 扫描 + DeltaGraph LSM 查询，结果在应用层合并（排序去重）。

---

## 7. SNB 查询引擎

### 7.1 DGS HTTP Server

基于 tokio async 的轻量 HTTP 服务器：

- 读查询：获取 RwLock read guard，调用 SNB query 实现
- 写查询：获取 RwLock write guard，调用 SNB update 实现
- 所有请求记录 latency：lock_wait_time + exec_time

### 7.2 SNB 查询分类

| 类别 | 查询 | 特点 |
|------|------|------|
| 复杂读 IC1 | 1-hop BFS + 过滤 | BFS 3 层、按 firstName 过滤 |
| 复杂读 IC2 | 好友消息聚合 | 先获取好友，再按时间过滤 |
| 复杂读 IC3 | XY 地点聚合 | 2-hop BFS + Place 过滤 |
| 复杂读 IC4-IC6 | Tag/Forum 聚合 | 2-hop + 集合操作 |
| 复杂读 IC7 | Likes 分析 | 反向边 + 时间窗口 |
| 复杂读 IC8-IC9 | 回复链 | 派生索引扫描 |
| 复杂读 IC10 | 共同兴趣 | FOF + Tag 交集 |
| 复杂读 IC11 | WorkAt 过滤 | 2-hop + 属性过滤 |
| 复杂读 IC12 | TagClass 继承 | 派生索引 + Tag 过滤 |
| 复杂读 IC13 | 最短路径 | BFS |
| 复杂读 IC14 | Top-K 最短路径 | DAG + IC14Cache；路径枚举封顶 `IC14_MAX_PATHS=1M` 防 OOM |
| 短读 IS1-IS7 | 点查 | Dynamic 路径直接读列式顶点缓存 / BaseGraph |
| 更新 IU1-8 | 写入 | Dynamic 路径写入 DeltaGraph（无 adjacency cache）|

### 7.3 SnbGraph 服务层：Dynamic / Legacy 两种模式

`SnbGraph` 是查询层与存储层之间的数据抽象，有两种实现：

```rust
// snb/props.rs
pub enum SnbGraph {
    Legacy(LegacySnbGraph),   // 旧路径：全量 adjacency cache（已废弃，仅兼容保留）
    Dynamic(DynamicSnbGraph), // 默认路径：BaseGraph CSR + DeltaGraph
}
```

**Dynamic（`open_dynamic`，默认）** —— SF1~SF100 实际使用的路径：

- **邻接查询**：`base_neighbors_by_type` 直接走 BaseGraph CSR（OUT/IN 双向），再与 DeltaGraph
  增量合并；**已移除 legacy LSM `fallback_engine`**。BaseGraph 未覆盖的 `(src_label, edge_type)`
  记入 `fallback_hits` 计数器（经 `/metrics` 与 validate stderr `[fallback-hits]` 暴露），按
  空结果 + Delta 合并处理，而非回探 LSM（`6e9f74d`）。
- **顶点属性**：`BaseSnbProperties` 以**紧凑列式（SoA）**常驻内存——Post/Comment 的标量字段按列
  存为 `Vec<i64>`，字符串字段（content / image_file / location_ip / browser_used / language）从
  `message_strings` **惰性**取出，不再为每行存空 `String` 占位（SF30+ 的纯内存浪费）。
- **更新**：`insert_edge_by_type` 直接写 DeltaGraph；`insert_vertex` 更新顶点缓存并维护 delta 侧的
  message/reply 索引（`delta_messages_by_creator` 等）。**无 adjacency cache 写入**。

**Legacy（`SnbGraph::open` → `LegacySnbGraph`）** —— 未清理的死 / 半死代码，**不在运行链上**：

- 当前 `snb-server`、`snb-validate` / `snb-validate-batch` / `snb-validate-mixed` 一律走
  Dynamic（`open_dynamic`），都不构造 Legacy。
- `SnbGraph::open`（Legacy serving）仅被 `queries.rs` 中旧的 `validate_ic1_ic14` /
  `validate_ic_batch` / `validate_mixed_tugraph`（非-dynamic 版本）调用，而这些 helper 已无
  CLI 入口 → 实际为死代码。
- 但 `snb-cache` 子命令仍可**手动触发** `build_adjacency_cache`，生成全量
  `adjacency_cache: HashMap<(VertexId, EdgeType), Vec<VertexId>>`（SF30 约需 ~100GB RSS、~15min）。
- 即：Legacy 仍随仓库编译、`snb-cache` 仍可手动跑，但**不参与 validation / benchmark / server**，
  属待清理代码，不应再用于 SF30 以上规模。

### 7.4 ID 编码

顶点 ID 高 56 位存储标签，低 56 位存储外部 ID：

```rust
pub fn encode_vid(label: VertexLabel, external_id: i64) -> VertexId {
    ((label as u64) << 56) | (external_id as u64 & ((1 << 56) - 1))
}
```

标签 1-8 对应 Person, Comment, Post, Forum, Organisation, Place, Tag, TagClass。

---

## 8. IO 后端

```rust
// io/mod.rs
pub trait IoBackend {
    fn read_at(path, offset, len) -> Bytes;
    fn write_at(path, offset, buf) -> usize;
    fn create(path) -> PathBuf;
    fn sync(path);
    fn remove(path);
}
```

三种实现：

| Backend | 特性 | 适用场景 |
|---------|------|----------|
| `BlockingPreadBackend` | 标准 `pread(2)` | 默认选项 |
| `DirectIoBackend` | `O_DIRECT`，绕过页缓存 | BaseGraph 读，避免双重缓冲 |
| `UringBackend` | io_uring async I/O | 高并发异步读 |

---

## 9. 配置参数

### 9.1 Engine 配置

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `memgraph_capacity_bytes` | 64MB | MemGraph 满容量阈值 |
| `inline_segment_capacity` | 8 | Inline 段最大边数 |
| `segment_target_bytes` | 64MB | Segment 目标大小 |
| `min_l0_partition_bytes` | 4MB | Graph-aware 分区最小阈值 |
| `max_l0_segments_per_flush` | 128 | 每次 flush 最大 segment 数 |
| `l0_file_threshold` | 4 | 触发全量 compact 的 L0 文件数 |
| `graph_aware_l0` | false | 是否启用 graph-aware 分区 |
| `auto_compaction` | false | flush 后是否自动 compact |
| `l0_ra_range_bucket_size` | 1M | RA 打分的 range bucket 大小 |
| `l0_ra_min_queries` | 10 | RA 打分最小查询数阈值 |
| `l0_ra_min_score` | 10.0 | RA 打分最小分数阈值 |
| `l0_ra_min_l0_segments` | 2 | RA 打分最小 L0 segment 数 |

### 9.2 BaseGraph IO 配置

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `neighbor_block_bytes` | 256KB | CSR 邻接 block 大小 |
| `prop_block_bytes` | 64KB | 属性 block 大小 |
| `cache_mb_per_worker` | 256MB | 每 worker 缓存预算 |
| `small_degree_threshold` | 128 | 小度数阈值 |
| `huge_degree_threshold` | 65536 | 大度数阈值 |

---

## 10. 目录结构

```
lsmgraph-rs/
├── src/
│   ├── lib.rs                    # 公开 API 入口
│   ├── graph.rs                  # Engine（核心 LSM 存储引擎）
│   ├── levels.rs                 # Level 辅助函数
│   ├── memgraph/
│   │   └── mod.rs                # MemGraph（内存表）
│   ├── csr/
│   │   ├── format.rs            # CSR 文件格式
│   │   ├── reader.rs            # CSR 读取
│   │   ├── writer.rs            # CSR 写入
│   │   ├── cache.rs             # Metadata Cache + Bloom Filter
│   │   └── manifest.rs          # MANIFEST 日志
│   ├── index.rs                  # MultiLevelIndex + VertexLockTable
│   ├── version.rs                # MVCC 版本管理
│   ├── delta/
│   │   └── mod.rs               # DeltaGraph（增量存储）
│   ├── dynamic_view/
│   │   └── mod.rs               # DynamicGraphView（读写合并视图）
│   ├── base_graph/
│   │   ├── mod.rs
│   │   ├── graph.rs             # BaseGraph（只读 CSR 快照）
│   │   ├── builder.rs           # Build 流水线
│   │   ├── catalog.rs           # Catalog 定义
│   │   ├── csr.rs               # CSR 邻接表读写
│   │   ├── column.rs            # 列存储读写
│   │   ├── io.rs               # IO 配置
│   │   └── ids.rs              # ID 类型定义
│   ├── snb/
│   │   ├── mod.rs
│   │   ├── props.rs             # SnbGraph（~3600 行）
│   │   ├── queries.rs           # IC/IS/IU 查询实现（~2900 行）
│   │   ├── server.rs           # DGS HTTP Server
│   │   └── full_loader.rs      # SNB 全量数据导入
│   ├── loader/
│   │   └── snb.rs              # Person-Knows 单独导入
│   ├── io/
│   │   ├── mod.rs
│   │   ├── backend.rs           # IoBackend trait
│   │   ├── blocking_pread.rs   # 标准 pread
│   │   ├── direct_io.rs        # O_DIRECT
│   │   └── uring.rs            # io_uring
│   ├── config.rs                # Engine 配置
│   ├── types.rs                # 核心类型（EdgeRecord, VertexLabel 等）
│   ├── metrics.rs              # Metrics 收集（~800 行）
│   ├── error.rs                # 错误类型
│   └── bin/lsmgraph.rs         # CLI 入口
└── deps/
    └── ldbc_snb_interactive_impls/  # LDBC 驱动和验证脚本
```

---

## 11. 数据流总览

### 11.1 导入流程

```
LDBC CSV (dynamic/static) 
  → BaseGraph.build_from_snb() 
  → base_graph/  (CSR + 列存储, 不可变)

LDBC CSV (person_knows)
  → Engine.import_person_knows() 
  → MemGraph → L0 segments → L1 (可选)
  → delta/ (LSM 增量)
```

### 11.2 读查询流程（SNB IC1 为例，Dynamic 路径）

```
HTTP POST /query/interactive_complex_read_1
  → run_dgs_http_query()
  → ic1(snb, person_id, first_name)
    → encode_vid(Person, person_id) → VertexId
    → knows_neighbors(snb, vid) → DynamicSnbGraph.out_neighbors_cached(vid, Knows)
      → base_neighbors_by_type(vid, Knows)
        → BaseGraph.scan_csr("KNOWS/OUT", local_src)   # ReadContext + Block Cache + Direct IO
          （base miss → 记 fallback_hits，按空处理，不再回探 LSM）
      → 合并 DeltaGraph 增量
        → DeltaGraph.get_neighbors() → Engine.get_neighbors_typed()
          → MemGraph 遍历 / L0 探测（三级过滤）/ L1+（MultiLevelIndex 定位）
    → BFS 3 层扩展
    → 属性过滤 + 聚合（顶点属性按列读取，字符串惰性）
  → JSON 响应
```

### 11.3 更新流程（IU8 Knows 为例，Dynamic 路径）

```
HTTP POST /query/interactive_update_8
  → run_dgs_http_update()
  → insert_update_edge(snb, person1, person2, Knows, prop, bidirectional=true)
    → DynamicSnbGraph.insert_edge_by_type(src, dst, Knows, prop)
      → DeltaGraph.insert_edge        # 无 adjacency cache 写入
        → MemGraph.insert
        → Engine.flush_active()（满时）
          → build_l0_flush_segments()
          → CsrWriter.write_segment(L0)
          → MANIFEST append
```

---

## 12. Scale Factor 支持

系统已从 **legacy SNB adjacency cache 路径** 迁移到 **BaseGraph 路径**
（`6e9f74d` 移除 LSM fallback + 顶点属性缓存 AoS→SoA 压缩）。以下以**当前 BaseGraph 路径**
实测为准，legacy 数据仅作历史对比。

### 12.1 当前支持矩阵（BaseGraph 路径）

| SF | store | 冷启动 | 稳态 RSS | 峰值 RSS(加载) | run throughput | audit | 端到端 |
|---|:--:|---:|---:|---:|---:|:--:|:--:|
| SF1   | 已建 | ~3s     | ~1 GiB    | —         | —              | —    | ✅ 充分验证 |
| SF10  | 已建 | —       | —         | —         | 未在新路径实测 | —    | — |
| SF30  | 已建 | 113.9s  | ~16.5 GiB | ~27 GiB   | 468.21 op/s    | PASS | ✅ |
| SF100 | 已建 | ~6.3min | ~50 GiB   | ~87 GiB   | 5.15 op/s      | FAIL | ✅ 无 OOM |
| SF300 | 未建 | ~19min* | ~150 GiB* | ~260 GiB* | —              | —    | ⏳ 仅外推 |

\* SF300 为按 ~3× SF100 的外推值；BaseGraph 尚未构建（build ~2h、store ~200 GiB，503 GiB 仍可容纳）。
实测来源：`lsmgraph-test-run-summary-20260607.md`、`logs/sf30-sf100-current-20260607/`。

**核心结论**：内存与启动已不再是瓶颈——SF100 稳态 ~50 GiB ≪ 503 GiB，冷启动 ~6.3min
（主要是 2.8 亿顶点属性列加载 ~269s），完整 IC/IS/IU 跑通无 OOM。

### 12.2 当前瓶颈：少数重型复杂读

代价集中在多跳 / 大邻接复杂读，其余 90%+ 操作毫秒级：

| Query | SF100 单条 | 备注 |
|---|---:|---|
| IC12 | 15.3s | 多跳聚合 |
| IC14 | 11.3s | all-shortest-paths，OOM 已修复（封顶），有界完成 |
| IC13 | ~11s  | 最短路径 |
| IC10 | 4.8s  | 共同好友 / FOF |
| IS1-7 | 1~2.6ms | 短查询，SF100 下依然快 |
| IU2-8 | 3~17ms  | 更新，SF100 下依然快 |

run audit FAIL 仅因 `time_compression_ratio=0.001` 把整段调度压到 ~0.5s 内发完，几条秒级查询
即超过 late 阈值（18 > 13）——属**延迟**问题，非可行性 / 内存问题。

---

## 13. 核心创新总结

| 创新点 | 描述 | 收益 |
|--------|------|------|
| Graph-aware L0 | flush 时按 `(src_label, edge_type)` 分区而非全局混合 | SF10 typed 查询 QPS 提升 77.7x（17,000→500 candidate segments） |
| RA-score Targeted Compaction | 基于查询热度选择 partition compact | 减少不必要的 compaction 写放大 |
| SourceBloom Filter | segment 级 Bloom Filter 判断 src 是否存在 | 减少无效 offset 探测 |
| CSR Metadata Cache | segment offsets 全量缓存 | offset 查找 O(1)，避免重复 IO |
| Direct IO for BaseGraph | `O_DIRECT` 绕过页缓存 | 避免与 BaseGraph block cache 双重缓冲 |
| Block-aligned CSR Reads | 按 256KB block 对齐读取大度数顶点 | 减少 syscall 次数 |
| BaseGraph CSR 邻接路由 | 邻接查询统一走 BaseGraph CSR + Delta 合并，移除 legacy LSM fallback | SF100 端到端可跑、冷启动 ~6.3min、无 OOM |
| 列式顶点缓存（SoA） | Post/Comment 标量按列存、字符串惰性，取代 AoS 空串占位 | SF100 顶点属性稳态 RSS ~50 GiB（旧 ~82 GiB）|

---

## 14. 已知局限

1. **最大 Level 数**：当前仅支持 L0 和 L1 两层，compaction 直接从 L0 到 L1，未实现多级层级结构
2. **无 LSM 写放大分析**：compaction 会重写整个 L0，当前实现中 merge 阶段是全量内存 sort
3. **复杂读延迟**：SF100 下少数重型复杂读（IC10/12/13/14）单条数秒，是当前主要瓶颈（已非内存 / 启动）
4. **加载期瞬时内存**：Dynamic 顶点属性加载期约有 ~37 GiB 瞬时缓冲（加载完即释放），SF300 需流式构建列以留余量
5. **无图索引**：仅 MultiLevelIndex（L1+ 段的 vertex→segment 映射），无更高级的图索引结构
