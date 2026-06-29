## 2. Background and Problem

**LSM graph layout.** Writes land in an in-memory buffer and are flushed to immutable CSR-like L0 segments; background compaction merges segments down a level hierarchy. L0 segments overlap in source-vertex space, so a read may probe many of them. Compaction reduces overlap but, if blind to query semantics, yields fewer yet *coarser* segments.

**Graph query signatures.** Unlike opaque key-value reads, a property-graph access carries semantics: source label, edge type, direction, degree class, destination label, property presence/value, snapshot, and schema epoch. These are available before any segment body is read.

**False-negative risk.** Pruning is useful only if it is *safe*. A segment may be skipped solely when its metadata *proves* it cannot contain a visible match for the query; otherwise it must be read. A merely "probably irrelevant" decision is unacceptable, because a missed edge is a silent correctness bug.

**The shared safety contract** (used by every later section):

> Prune a segment only when its metadata proves disjointness or absence; otherwise read conservatively.

Everything in §4–§6 is a way to (a) record enough metadata to make such proofs at flush time, (b) preserve that metadata through compaction, and (c) fall back to conservative reads exactly when a proof is unavailable — under mixed/unknown metadata, tombstones, or uncertain schema epochs.
