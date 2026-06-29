## 3. System Overview

SemL0 is organized as four cooperating loops over the same semantic metadata.

1. **Write / flush.** The write path emits CSR-like L0 segments. At flush, the engine derives each segment's semantic metadata — source/destination label, edge-type partition, degree class, property presence, schema epoch — and an exact/conservative/unknown completeness flag (§4). This is where the pruning surface is created.

2. **Read / prune.** The read path compiles a query predicate into a storage-facing `GraphAccessSignature` and, for each candidate segment, calls a pruning decision that either proves the segment disjoint (skip, recording the reason) or keeps it for a CSR bloom/offset/body probe (§4). Unprovable cases fall back to conservative reads.

3. **Feedback / compaction.** Runtime metrics (query counts, candidate fanout, read and rewrite proxies) feed an event-driven maintenance scheduler. It selects hot/costly semantic partitions and runs a semantics-aware merge that retains or rebuilds the pruning surface across levels rather than collapsing it (§5), recording retention and cost side by side.

4. **Schema / snapshot safety.** A versioned schema catalog and per-segment schema epoch let new schema interpretations coexist with old segments; tombstone and snapshot metadata bound what compaction may discard. Pruning degrades to conservative reads whenever epoch/encoding/tombstone interactions make exactness unprovable (§6).

The same metadata object — summarized as an orthogonal `SegmentSemanticState` (topology / degree / property / tombstone / schema) — is produced at flush, consumed at read, measured at compaction, and resolved against the schema catalog. Maintaining this single surface across the whole lifecycle is what distinguishes SemL0 from L0-only semantic indexing.
