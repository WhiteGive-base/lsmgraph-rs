# LSMGraph Clean Kernel Architecture

> Branch target: `codex/k4-clean-kernel`
>
> Scope: clean DB kernel only. This document describes the engine, storage
> format, query path, update path, semantic-pruning boundary, and the current
> K4 lifecycle API. It intentionally excludes paper drafts, benchmark logs,
> one-off experiment runners, and submission artifacts.

## 中文主线摘要

这个分支先完成了干净 DB 内核基线，然后在内核里补了一个最小完整的
K4 lifecycle API。它只保留 LSMGraph 的内核源码、测试、Cargo 构建文件、
README/启动文档，以及本架构文档；历史实验脚本、baseline 结果、论文草稿、
图片和一次性 benchmark runner 都不属于这个分支的 tracked tree。

当前带入的内核改动只限于 L0/semantic pruning 已经比较确定的部分：
`src/csr/format.rs` 的 pruning decision/reason、
`src/metrics.rs` 的 pruning reason metrics，以及
`src/semantic.rs` 的 `with_dst_label()` 查询语义签名扩展。

当前 K4 API 覆盖的 DB 链路是：

```text
write -> flush -> semantic metadata -> read pruning -> feedback
      -> merge/compaction -> schema-safe recovery/reopen
```

实现边界在 `Engine::run_k4_lifecycle_with_repetitions()`：
它会 flush 当前 MemGraph、持久化 semantic sidecar、执行 query signatures
产生 L0 feedback、用 feedback 选择 L0 partition 合并到 L1，并返回
`K4LifecycleReport`。SNB HTTP server 还没有自动把 mixed update workload
接入这条 lifecycle，这是后续集成点。

## 1. Scope

This branch is the clean kernel line for LSMGraph. It keeps the database engine
and the minimum product-facing CLI/server code needed to build, test, import,
query, and serve the graph store.

The branch now includes the minimum DB-native K4 lifecycle API. K4 means:

```text
write -> flush -> semantic metadata -> read pruning -> feedback
      -> merge/compaction -> schema-safe recovery/reopen
```

The current clean branch preserves the kernel foundation, exposes the K4
lifecycle through `Engine`, and keeps research artifacts out of the branch.

## 2. Repository Boundary

Kept in this branch:

| Path | Role |
|---|---|
| `src/` | DB kernel, SNB service path, storage engine, IO backends |
| `tests/` | Kernel and engine regression tests |
| `Cargo.toml`, `Cargo.lock` | Rust package definition |
| `README.md`, `README-cn.md` | User-facing project entry points |
| `LSMGRAPH-ARCHITECTURE.md` | Clean kernel architecture |
| `LSMGRAPH-STARTUP-GUIDE.md` | Basic startup guide |

Removed from this branch:

| Path/type | Reason |
|---|---|
| `baseline/` | Research scripts, reports, raw summaries, and experiment plans |
| `scripts/` | One-off experiment and analysis runners |
| `figures/` | Paper figures, not kernel source |
| `docs/` | Historical evidence archive, not clean kernel docs |
| top-level paper/planning markdown | Submission and experiment process material |
| external-system reports | Baseline study artifacts, not engine source |
| W/P experiment binaries under `src/bin` | Not part of the clean DB product surface |

## 3. Layered Architecture

```text
Application / Benchmark / DGS HTTP Client
        |
        v
SNB server and query layer
  - DGS-compatible HTTP endpoints
  - LDBC SNB query/update mapping
        |
        v
Dynamic graph view
  - BaseGraph immutable CSR snapshot
  - DeltaGraph LSM update region
        |
        v
Engine
  - MemGraph write buffer
  - VersionManager MVCC snapshots
  - L0/L1 CSR segment metadata
  - semantic pruning and metrics
        |
        v
CSR storage + IO backend
  - segment manifest
  - offset arrays and edge bodies
  - blocking/direct/io_uring backends
```

## 4. Core Modules

| Module | Responsibility |
|---|---|
| `src/graph.rs` | Engine state, writes, flush, levels, snapshots, query entry points |
| `src/version.rs` | Immutable versions and read snapshot pinning |
| `src/memgraph/` | In-memory write buffer for recent edge updates |
| `src/csr/` | Segment format, reader, writer, manifest, metadata cache |
| `src/semantic.rs` | Query-facing graph access signatures and semantic predicates |
| `src/schema.rs` | Logical schema catalog and schema epoch model |
| `src/property_encoding.rs` | Property value encoding boundary |
| `src/metrics.rs` | Engine, IO, CSR, HTTP, partition, and pruning metrics |
| `src/base_graph/` | Immutable base graph build/read path |
| `src/delta/` | Thin DeltaGraph wrapper over the LSM engine |
| `src/dynamic_view/` | BaseGraph + DeltaGraph merged view |
| `src/snb/` | LDBC SNB import, property layer, query layer, DGS server |
| `src/io/` | Pluggable IO backends |
| `src/bin/lsmgraph.rs` | Product CLI and server binary |

## 5. Storage Model

LSMGraph stores a dynamic graph as:

```text
BaseGraph: immutable CSR snapshot for stable graph data
DeltaGraph: MemGraph + LSM CSR segments for recent updates
```

The read path merges the immutable base graph with delta updates. The write
path appends updates into MemGraph, flushes immutable CSR-like segments to L0,
and publishes a new immutable version.

### 5.1 Versioning

The engine uses MVCC-style immutable versions:

```text
Version {
  id,
  memgraphs,
  levels,
}
```

Readers pin the current version. Writers publish a new version after flush or
level changes. This keeps read and write paths structurally separate.

### 5.2 MemGraph

MemGraph is the write buffer. It groups edge records by source and keeps recent
updates in memory until a flush boundary is reached. Frozen memgraphs are
flushed to CSR segments.

### 5.3 CSR Segment

A segment consists of:

```text
CsrHeader
EdgeOffset[]  // source -> edge body range
DiskEdgeBody[] // destination, timestamp, marker/property/type fields
```

`CsrSegmentMeta` records the segment-level proof surface used before reading a
body. Important metadata includes source label, destination label when known,
edge-type partition, direction, source range, schema epoch, property summary,
degree-class summary, tombstone flag, and summary completeness.

## 6. Query Semantics

The storage-facing query abstraction is `GraphAccessSignature`.

It can encode:

| Field | Meaning |
|---|---|
| source vertex/source label | source-side graph constraint |
| destination label | optional destination-side graph constraint |
| edge type | typed edge constraint such as `LIKES` or `KNOWS` |
| direction | outgoing/incoming/both boundary |
| degree class | optional degree-range class |
| property predicate | currently exact presence/absence/equality boundary |
| timestamp range | snapshot/time pruning boundary |
| schema epoch | interpretation boundary for segment metadata |

The kernel rule is:

```text
Only prune a segment when metadata proves disjointness or exact absence.
Otherwise keep the segment as a candidate.
```

This keeps the failure mode as extra reads, not false negatives.

## 7. Semantic Pruning Boundary

`CsrSegmentMeta::signature_pruning_decision()` evaluates a segment against a
`GraphAccessSignature` and returns:

```text
SignaturePruningDecision {
  pruned: bool,
  reason: &'static str,
}
```

Typical prune reasons:

| Reason | Meaning |
|---|---|
| `time` | segment timestamp range cannot match |
| `src_label` | source label is disjoint |
| `edge_type` | exact edge-type partition is disjoint |
| `direction` | direction is disjoint |
| `degree` | exact degree class is disjoint |
| `dst_label` | destination label is disjoint |
| `property_absence` | segment exactly lacks a required property |

Typical keep/fallback reasons:

| Reason | Meaning |
|---|---|
| `kept_candidate` | no proof of disjointness |
| `mixed_unknown_fallback` | metadata is mixed, unknown, or not safe for pruning |
| `budgeted_not_materialized` | budgeted layout did not keep exact degree partition |
| `schema_tombstone_fallback` | property absence cannot prune due to tombstone risk |

These reasons are also exposed through `Metrics` as pruning-reason counters.

## 8. Metrics

The clean kernel keeps metrics that are useful for engine operation and K4
lifecycle reports:

| Metric group | Examples |
|---|---|
| storage | flush count, compaction count, read/write latency |
| IO | read/write bytes and backend latency |
| CSR | probe setup, offset lookup, body reads, bloom probes |
| L0 partitions | query/probe history grouped by semantic partition |
| pruning reasons | pruned/kept segment counts and estimated saved bytes |
| HTTP | endpoint-level request and latency counters |

These are kernel metrics, not paper-only counters.

## 9. K4 Lifecycle Boundary

The clean kernel supports a minimum K4 lifecycle through:

```rust
Engine::run_k4_lifecycle(signatures)
Engine::run_k4_lifecycle_with_repetitions(signatures, repetitions)
```

The lifecycle does not create a separate experiment path. It uses the same
production Engine operations:

| K4 phase | Engine operation |
|---|---|
| write | `insert_edge*` / `delete_edge` into `MemGraph` |
| flush | `flush_active()` writes L0 CSR segments |
| semantic metadata | CSR segment metadata plus degree-directory sidecar |
| read pruning | `get_neighbors_by_signature()` with exact-proof pruning |
| feedback | `Metrics::record_l0_partition_query/probe` |
| merge/compaction | `compact_best_l0_partition_by_score()` |
| schema-safe recovery/reopen | manifest + schema catalog + sidecar replay through `Engine::open()` |

Current status:

| Capability | Status |
|---|---|
| write to MemGraph | present |
| flush to L0 CSR segment | present |
| segment-level semantic metadata | present |
| exact-proof read pruning | present |
| query feedback counters | present |
| feedback-selected L0 merge | present |
| snapshot/tombstone-safe merge retention | present |
| schema catalog recovery | present |
| K4 lifecycle report | present |
| automatic SNB server integration | not implemented |
| multi-level semantic proof propagation beyond L1 | not implemented |

The K4 API is intentionally an Engine boundary. SNB validation can run before
and after this lifecycle, but the Java driver path is not yet wired to trigger
K4 automatically during mixed update workloads.

## 10. Schema and Tombstone Safety

Segment metadata carries a schema epoch. Queries compiled under a newer schema
may still read old segments. Exact disjointness can prune old segments only when
the metadata is safe under the relevant schema interpretation.

Tombstone-sensitive segments are conservative candidates when a delete may
suppress older visible inserts. This is why exact property absence does not
automatically prune tombstone-bearing segments.

## 11. CLI Surface

The clean branch exposes a single product binary:

```text
target/release/lsmgraph
```

Experiment-specific binaries such as `p3-feedback-bench`, sustained feedback
runners, and W-series workload-shift runners are intentionally removed from the
clean branch.

The CLI remains the entry point for import, validation, storage benchmarks,
server startup, base graph building, and maintenance commands that are part of
the DB product surface.

## 12. Remaining K4 Gaps

The branch contains a functional minimum K4 lifecycle, but it is not the final
SIGMOD-grade lifecycle system.

Remaining additions:

| K4 area | Required kernel addition |
|---|---|
| semantic state machine | explicit exact/mixed/unknown/tombstone/schema-uncertain states |
| merge metadata propagation | deterministic metadata downgrade rules across all levels |
| LmergePolicy | production policy for scheduling K4 cycles continuously |
| retention metrics | exact ratio, fallback ratio, pruning surface before/after merge |
| write lifecycle metrics | flush latency, write amp, stall, backlog, open/recovery |
| schema lifecycle | schema-change cost, epoch fallback, metadata repair |
| SNB integration | Java driver mixed updates should drive DB-native K4, not only cache updates |

The current correctness proof is the engine-level test
`k4_lifecycle_flushes_feedback_compacts_and_reopens_schema_safe`.
