## 8. Related Work

SemL0 sits at the intersection of LSM storage maintenance, query-driven physical
design, graph storage, and schema evolution. We position it against each, and state
the one property that distinguishes it: **the control signal is a property-graph
query signature, and the maintained object is a segment-level semantic pruning
surface that is kept conservative-correct across the LSM lifecycle.**

### LSM / KV compaction and tuning
RocksDB-style engines and the LSM literature optimize compaction for write
amplification, space amplification, and read cost (e.g. the RUM trade-off), and a
large body of work auto-tunes LSM knobs — size ratios, compaction triggers,
bloom-filter bits — including the RocksDB / RUM / Dostoevsky line and learned,
workload-aware tuners. These operate on
*size and access-frequency* signals and on opaque key-value records. SemL0 is not a
general compaction tuner: its compaction decisions and segment layout are driven by
*graph query semantics* (label, edge type, direction, degree, property), and its
outputs carry exact/conservative semantic metadata that the read path can prove
disjointness against. Standard compaction tuning is orthogonal and composable with
SemL0.

### Query-driven physical design / adaptive indexing
Database cracking and adaptive indexing reorganize physical layout in response to
the query stream. SemL0 shares the "let queries shape the bytes" philosophy but
differs in physical object and safety boundary: the object is an LSM-CSR segment's
semantic summary (not an index over a column), and the safety contract is
*exact-proof pruning with conservative fallback* under tombstones, snapshots, and
schema epochs — pruning may only ever cause extra reads, never a missed edge.

### Graph storage and LSM-based graph stores
Topology-aware graph stores and LSM-based graph systems — notably **LSMGraph**
(VLDB'24; a multi-level CSR design, open source) and **BACH** (PVLDB'25; which
transforms layout between adjacency and CSR forms, no public code) — target update
locality and traversal efficiency at the *layout* level. SemL0 is
orthogonal: it exposes query semantics to the LSM read/maintenance path and can be
layered on top of such layouts. In particular, prior LSM-graph layout work does not
treat a property-graph query signature as first-class segment metadata, nor maintain
a prunable semantic surface across flush and compaction.

### External baseline: LiveGraph (measured, SF10)
Beyond positioning, we measure against **LiveGraph**, a transactional graph store,
as an external-system baseline at SF10 (LDBC SNB; 355,185,382 edges / 29,987,835
vertices): load 1,502.6 s, peak RSS ≈45.7 GiB, on-disk 37.58 GB block store + 1 GB
WAL; typed-neighbor scan over positive core edge types averages 2.74 µs (edge_type 1:
avg 3.38 µs, p99 15.2 µs). The full SemL0-vs-LiveGraph SF10 table is in
`baseline/seml0-baseline-defense-20260619/sf10-main-baseline-table.md`; consolidated
numbers in `tables/external-baseline-md.md`. Both systems serve the same typed
neighbor scans, but occupy different points: LiveGraph is an in-memory transactional
store (≈45.7 GiB RSS for SF10), whereas SemL0 is a disk-resident LSM design whose
contribution is *semantic pruning maintained across the lifecycle*; the comparison
establishes that SemL0's pruning operates against a real external system's footprint
and scan profile, not only internal variants. The external comparison is **SF10 only**
— LiveGraph SF100 load was infeasible (extrapolated 17–21 days), an honest scope limit
(§9). We additionally report an internal RocksDB-*style* KV-LSM layout (not official
RocksDB) and an archived KV-style encoding *simulation*; these are an internal baseline
and a simulation, labeled as such, not external systems.

### Schema evolution / versioned interpretation / MVCC
Versioned schema catalogs and snapshot/MVCC systems provide epoch-aware
interpretation and snapshot-consistent reads. SemL0 applies versioned
interpretation specifically to graph-LSM segment metadata and semantic pruning:
old segments keep their `schema_epoch`, additive changes route only future writes,
and pruning degrades to conservative reads whenever an epoch/encoding/tombstone
interaction makes exactness unprovable (§6). The contribution is the *interaction*
of schema versioning with semantic pruning safety, not a general migration engine.

> Do not expand related work into new claims. Each paragraph ends at "orthogonal /
> composable / different object+boundary."
>
> Named comparison points (bibkeys inserted at TeX assembly): LSMGraph (VLDB'24,
> multi-level CSR, open source); BACH (PVLDB'25, adjacency↔CSR layout transformation,
> no public code); the RocksDB / RUM / Dostoevsky and learned-LSM-tuning line; database
> cracking / adaptive indexing; LiveGraph (transactional graph store, our measured SF10
> baseline); versioned-catalog / MVCC systems. All are orthogonal or composable; SemL0's
> distinguishing object is the query-semantic pruning surface maintained across the lifecycle.
