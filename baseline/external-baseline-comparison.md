# External baseline comparison: LiveGraph, Teseo, GraphOne

This document records the external baseline position for SemL0. These systems are included as design baselines by default; they should enter the numeric performance table only if a reproducible source tree, build path, data loader, and comparable benchmark driver are available.

## Summary table

| System | Primary design target | Core storage idea | Update model | Query path relevance to SemL0 | Reproducibility status |
| --- | --- | --- | --- | --- | --- |
| LiveGraph | Transactional graph storage with fast scans | Transactional Edge Log (TEL), sequential adjacency list scans | Transactional updates with concurrency control | Strong external contrast for sequential adjacency scans vs LSM L0 pruning | Local source + build present: `deps/LiveGraph/build/liblivegraph.so` (+ LinkBench loader, `bind/`); driver+loader for the shared SF100 edge set is WIP |
| Teseo | In-memory dynamic structural graph analysis | Sparse arrays, large arrays with gaps, fat tree | Transactional dynamic graph updates | Contrast for in-memory dynamic graph layout vs persistent LSM-CSR | Local source + build present: `deps/teseo/build/libteseo.a` (autotools); driver for the shared neighbor workload is WIP |
| GraphOne | Real-time analytics on evolving graphs | Hybrid edge-list + adjacency-list with dual versioning | High-rate ingestion with graph views | Contrast for hybrid evolving-graph store vs query-semantic L0 layout | Local source present, CMake-configured: `deps/GraphOne/build/` (`example.cpp`/`main.cpp` show the `batch_edge`/graph-view API); driver+loader is WIP |
| LLAMA | Out-of-core CSR snapshots for evolving graphs | Multi-versioned CSR (LLAMA) | Snapshot/delta merge | Contrast for snapshotted CSR vs query-semantic L0 | Source present under `deps/`; artifact-report template `external-system-LLAMA-artifact-report.md` |

> 2026-06-10 update: the earlier "No local source/build found" was stale. All four
> systems now have local source trees (and pre-built libs for LiveGraph/Teseo) under
> `deps/`. They still enter the numeric table only after passing the reproducibility
> gate below (same SF100 edge set + same sampled neighbor workload + sampled
> correctness). Until a driver+loader lands, keep them in the qualitative table.

## LiveGraph

LiveGraph is a transactional graph storage system designed so adjacency list scans are sequential even with concurrent transactions. Its core structure is the Transactional Edge Log (TEL). It is a strong design baseline for the question: can graph-aware physical layout avoid random access during adjacency scans?

Use in paper:

- Qualitative baseline for transactional graph storage and sequential adjacency scan design.
- Do not compare numeric latency unless an official or faithful implementation is built and loaded with the same dataset.

Source:

- https://arxiv.org/abs/1910.05773

## Teseo

Teseo targets dynamic structural graphs in memory. It uses sparse arrays, arrays interleaved with gaps, and a fat tree. It is a strong design baseline for update-friendly in-memory graph analytics, but it is not a persistent LSM design.

Use in paper:

- Qualitative baseline for in-memory dynamic graph layout.
- Keep separate from storage-path read amplification claims unless an experiment is reproduced.

Source:

- https://ir.cwi.nl/pub/32921

## GraphOne

GraphOne targets real-time analytics on evolving graphs. It combines edge-list and adjacency-list storage and uses dual versioning to decouple updates from graph computations. It is a useful contrast to SemL0 because it optimizes evolving graph access with hybrid views rather than query-semantic L0 pruning.

Use in paper:

- Qualitative baseline for hybrid evolving graph stores.
- Report as external design comparison unless source and loader are reproduced.

Source:

- https://www.usenix.org/conference/fast19/presentation/kumar

## Reproducibility gate

Before adding any external system to the numeric table, require all of:

- Public source or internally archived source with license permission.
- Successful build on the Linux host.
- Loader for the same LDBC SNB edge set or a documented converter.
- Same sampled neighbor workload or a justified comparable benchmark.
- Correctness check against schema baseline on sampled sources.
- Store size, load time, memory, and query-latency metrics.

If any item fails, keep the system in the qualitative table only.
