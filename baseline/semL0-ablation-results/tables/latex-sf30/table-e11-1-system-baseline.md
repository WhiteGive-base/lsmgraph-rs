# Table E11-1: System-Level Baseline Comparison

| System | Layout principle | Update model | Query semantics visible to L0 | QPS | P95 us | P99 us | Update throughput | Store GB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SemL0 (benefit-scored) | Semantic benefit scoring | Append-only L0 | Yes (src_label, edge_type, degree) | N/A | N/A | N/A | N/A | N/A |
| LSMGraph-style LSM-CSR | Range-partitioned CSR | Compaction-based | No | N/A | N/A | N/A | N/A | N/A |
| RocksDB-style KV | Key/range only | LSM-KV | No | N/A | N/A | N/A | N/A | N/A |
| Naive L0 scan | Unstructured L0 | Append-only L0 | No | N/A | N/A | N/A | N/A | N/A |
| Oracle semantic pruning | Ideal semantic oracle | N/A | Theoretical | N/A | N/A | N/A | N/A | N/A |
| Full L0->L1 compact | No L0 | Full compaction | No | N/A | N/A | N/A | N/A | N/A |
| LiveGraph | Dynamic adjacency | Incremental | N/A | 50–450k (SF100, 16-thread) | N/A | N/A | N/A | N/A |
| Teseo | Delta layers | Append-only delta | N/A | 200–500k (SF100) | N/A | N/A | N/A | N/A |
| GraphOne | Versioned snapshots | Delta + periodic snapshot | N/A | N/A | N/A | N/A | N/A | N/A |
| LLAMA | CSR + delta | Delta + async compact | N/A | N/A | N/A | N/A | N/A | N/A |
