# E11 Claim Boundary — SemL0 Baseline-Strengthening Experiment

Date: 2026-06-06

## Purpose

This document maps E11's claims to three categories:

- **Measured**: claims supported by data E11 will produce internally
- **Planned**: claims requiring external system artifacts or runs not yet executed
- **Blocked**: claims that cannot be supported in the current artifact (missing implementation, no measurement path, or external dependency)

The goal is to keep paper claims honest and to make approval gates explicit before final submission.

---

## Claim → Status Matrix

### RQ0: Is SemL0 more than an LSMGraph engineering extension?

| Claim | Status | Evidence Source | Blocking Reason |
|-------|--------|----------------|----------------|
| naive L0 scan has worst read amplification | **Measured** | E11 Matrix B: naive variant read bytes | — |
| LSMGraph-style LSM-CSR provides a meaningful baseline (key/range only) | **Measured** | E11 Matrix B: lsmgraph-style variant | — |
| individual semantic dimensions contribute independently | **Measured** | E11 Matrix B: label-only, edge-type-only, degree-only variants | — |
| combining dimensions is necessary for full benefit | **Measured** | E11 Matrix B: label+edge-type, label+edge-type+degree, full semantic variants | — |
| oracle semantic pruning provides theoretical upper bound | **Measured** | E11 Matrix B: oracle variant (ground-truth offline) | — |
| SemL0 outperforms existing dynamic graph systems | **Planned** | External system runs (LDBC SNB DGS reference, LiveGraph, Teseo, etc.) | Artifact not yet available; external system builds not in scope for current window |
| RocksDB-style KV-LSM baseline provides KV-system context | **Planned** | E11 Matrix A: kv-lsm variant | Requires `--l0-layout kv-lsm` CLI implementation |
| full-compact eliminates L0 overhead at write cost | **Measured** | E11 Matrix B/C: full-compact variant | — |
| materialization overhead is bounded | **Measured** | E11 Matrix B/C: benefit-scored vs full semantic | — |
| RocksDB-style KV baseline was implemented | **Blocked** | — | `--l0-layout kv-lsm` not implemented in `src/config.rs` |
| materialization overhead (memory/CPU) measured per query | **Blocked** | — | No per-query memory or CPU instrumentation in current benchmark harness |
| external systems (LiveGraph, Teseo, LLAMA) measured in same harness | **Blocked** | — | External system builds and harness integration out of scope for E11 |

---

## Claim → Variant Mapping

### Measured Claims (internal variants)

| Claim | Variants Required | Metric |
|-------|------------------|--------|
| naive shows L0 read amplification severity | `naive` | candidate L0, read bytes |
| LSMGraph-style is a meaningful baseline | `lsmgraph-style` | candidate L0, read bytes vs SemL0 |
| single-dimension semantic pruning has partial effect | `label-only`, `edge-type-only`, `degree-only` | candidate L0, read bytes |
| multi-dimension combination is stronger | `label+edge-type`, `label+edge-type+degree` | candidate L0, read bytes vs single-dimension |
| full semantic is upper bound | `full_semantic` | candidate L0, read bytes |
| benefit-scored balances cost and benefit | `benefit-scored` | candidate L0, read bytes, l0_files, manifest_bytes |
| oracle provides theoretical upper bound | `oracle` | candidate L0 (offline ground truth) |
| full-compact eliminates L0 at write cost | `full-compact` | candidate L0 = N/A, import time, store bytes |
| import / storage cost is bounded | all variants | import_s, store bytes, l0_files, manifest MB |

### Planned Claims (requires implementation or external systems)

| Claim | Dependency | Status |
|-------|-----------|--------|
| SemL0 vs RocksDB-style KV-LSM | `--l0-layout kv-lsm` implementation | **Not implemented** — needs CLI flag + key=(src,etype,dst,ts) encoding logic |
| SemL0 vs DGS / external graph stores | External system builds | **Not started** — requires separate repository setup, build, and harness integration |

### Blocked Claims (no implementation path or measurement)

| Claim | Reason |
|-------|--------|
| RocksDB-style KV baseline performance | CLI `--l0-layout kv-lsm` not implemented; KV encoding path missing |
| Per-query materialization memory/CPU overhead | Current harness has no per-query memory or CPU instrumentation |
| External system comparison (LiveGraph, Teseo, LLAMA) | Out of scope for E11; requires separate build pipelines and harness integration |
| Production write-stall characterization for baselines | Requires concurrent workload + long-running run; out of scope for microbenchmark |
| Range/string/compound property predicate baselines | Property predicate support limited to fixed-width equality in current implementation |

---

## Baseline Variant Implementation Status

| Variant | CLI flag | Implementation status | Notes |
|---------|----------|---------------------|-------|
| `naive` | `--l0-layout naive` | **Not implemented** | Single L0 segment; no semantic pruning |
| `lsmgraph-style` | `--l0-layout lsmgraph-style` | **Not implemented** | CSR segments, no semantic L0 index |
| `kv-lsm` | `--l0-layout kv-lsm` | **Not implemented** | Key=(src,etype,dst,ts), range scan only |
| `schema-only` | `--l0-layout schema` | **Available** | Schema/source-label pruning only |
| `label-only` | `--l0-layout label-only` | **Available** | src_label semantic only |
| `edge-type-only` | `--l0-layout edge-type-only` | **Available** | edge_type semantic only |
| `degree-only` | `--l0-layout degree-only` | **Available** | degree_class semantic only |
| `label+edge-type` | derived from `--l0-layout semantic` | **Available** | Derived by disabling degree |
| `label+edge-type+degree` | `--l0-layout semantic` | **Available** | Full semantic |
| `full_semantic` | `--l0-layout semantic` | **Available** | All dimensions, full materialization |
| `benefit-scored` | `--l0-layout semantic-budgeted` | **Available** | Current paper row |
| `oracle` | `--l0-layout oracle` | **Not implemented** | Offline ground truth; needs oracle query interface |
| `full-compact` | `--l0-layout full-compact` | **Not implemented** | L0 eliminated; all data directly in L1 |
| `materialized_adjacency_cache` | independent build mode | **Not in scope** | Separate materialized hot-adjacency system |

> **Note**: Variants marked "Not implemented" must be implemented before their corresponding experiments can run. See `experiment-completion-plan-cn.md` Section E11 for CLI flag design.

---

## RQ0 Answer per Claim Status

### If all measured variants run successfully

RQ0 can answer:

```
SemL0's benefit over naive L0 scan is measured. SemL0's benefit over
LSMGraph-style LSM-CSR is measured (key/range only vs semantic L0 index).
Individual semantic dimensions contribute partially; combination is stronger.
Full semantic materialization is the upper bound; benefit scoring is the
practical balance. Oracle provides theoretical upper bound.
```

### If external system variants remain Planned

The paper must **not** claim:

```
SemL0 outperforms LiveGraph/Teseo/LLAMA/DGS
SemL0 is faster than any external graph store
```

The paper **can** claim:

```
SemL0's contribution is isolated from LSMGraph-style LSM+CSR by
mechanism-level ablation. External system comparison is future work.
```

### If blocked variants remain blocked

The paper must **not** claim:

```
SemL0's materialization overhead is measured at query-level memory/CPU granularity
SemL0's RocksDB-style KV baseline is available
```

The paper **can** claim:

```
Import/store/rewrite cost proxies are bounded per variant. Full per-query
materialization overhead is beyond current harness scope. KV-LSM baseline
comparison is planned but not yet executed.
```

---

## Artifact Availability Gates

| Gate | Current status | Unblocks |
|------|---------------|----------|
| `naive` variant implemented | Not implemented | naive L0 baseline comparison |
| `lsmgraph-style` variant implemented | Not implemented | Primary RQ0 claim |
| `oracle` variant implemented | Not implemented | Theoretical upper bound |
| `full-compact` variant implemented | Not implemented | L0 elimination extreme |
| `kv-lsm` variant implemented | Not implemented | KV-system context |
| External system builds | Not started | System-level comparison |
| Per-query overhead instrumentation | Not implemented | Materialization cost granularity |

---

## Summary

E11's internal mechanism-level claims (naive, LSMGraph-style, single-dimension, multi-dimension, full semantic, benefit-scored, oracle, full-compact) are **measured** if the corresponding variants are implemented and run. The paper's RQ0 section can honestly answer "SemL0 is more than an LSMGraph engineering extension" based on these internal comparisons.

External system claims (LiveGraph, Teseo, LLAMA, DGS) are **planned** pending artifact availability.

KV-baseline, oracle, and per-query overhead claims are **blocked** pending implementation or instrumentation.
