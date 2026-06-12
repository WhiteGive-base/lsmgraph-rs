# SemL0 Implementation Status After 2026-06-12 Pass

## Resource Gate

- Remote host checked before debug work: 64 CPU cores, 503 GiB RAM.
- During the latest C4/C11/C13/C12 pass, MemAvailable stayed above 295 GiB and `/data` stayed above 227 GiB free. `/tmp` remained tight at about 3.6 GiB free, so no large temporary outputs were written there.
- Existing user workloads were monitored; the active SF100 import reached about 140 GiB RSS and the LiveGraph driver about 45 GiB RSS during validation. Both were left untouched.
- Current resource hold point: `/data` was about 222 GiB free, root `/tmp` about 3.6 GiB free, MemAvailable about 289 GiB, and the active SF100 import about 157 GiB RSS. This is still above the hard stop thresholds, but close enough that further cargo/test/bench work should pause until `/data` recovers or the user workload finishes.
- Resume criteria for heavier work: re-check `free -h`, `df -h /data/WorkSpace /tmp`, and top RSS processes first; only resume cargo/full debug tests if `/data` has comfortably more than 200 GiB free and MemAvailable remains well above 80 GiB. SF100/kv-style/open-time/RSS measurements still require explicit scheduling after the user import completes.
- Existing release workloads were left untouched. No `cargo build --release` was run, and no SF100 store was opened by this pass.
- Validation used only `target/debug` via `nice -n 10 cargo test -j 16`.

## Completed Or Already Present

- Gap-analysis report and zero-cost SF100 funnel/p99 tables exist under `baseline/seml0-gap-analysis-20260612/`.
- C1 budget reopen accounting: only exact-degree L0 partitions are charged against the budget.
- C2 IO error propagation: semantic index and degree-directory rebuild paths now surface `read_offsets` errors.
- C3 CSR metadata cache LRU: queue growth is bounded and hot entries survive eviction.
- C5 Oracle index: fixed range/type/label precedence bug and removed the O(E x F) contains path.
- C6 kv-style summary labeling: model-derived rows are marked `simulated(model)` in regenerated summaries.
- C8 CSR metadata cache capacity: added `--csr-metadata-cache-entries` and wired it through Engine open paths.
- C4 first-stage degree-directory memory reduction: `degree_directory` now tracks only L0 files with exact semantic summaries, real edge-type partitions, and exact non-Mixed degree classes. Schema-style/budgeted non-exact degree-Mixed files no longer force full offset reads or per-source directory entries.
- C4 packed degree-directory representation: `degree_directory` now stores one `u8` bitmask per `(src, edge_type)` instead of a heap-allocated `Vec<DegreeClass>`, preserving conservative Low/Medium/High query widening while removing per-entry vector allocation, sorting, and deduplication.
- C11 degree-directory sidecar: Engine open now tries to load a compact `DEGREE_DIRECTORY` sidecar keyed by the tracked exact-L0 file set before falling back to the old offset-scan rebuild path. Rebuild/update writes the sidecar best-effort; stale or corrupt sidecars are ignored and rebuilt from CSR offsets.
- C13 shared offset metadata: cached CSR metadata now stores offsets as `Arc<[EdgeOffset]>`, and `CsrReader::read_offsets()` returns that shared slice instead of cloning the full offset vector. L1+ index rebuild, semantic rebuild, and incremental exact-L0 degree-directory updates now iterate the shared offsets directly.
- C12 first-stage feedback-to-budget loop: budgeted semantic L0 now folds observed L0 partition feedback into edge-type candidate scoring. When `(src_label, edge_type)` has runtime query/probe history, its feedback weight is `query_count * max(avg_candidate_segments, 1)` and can promote future flushes through `feedback_score_gate`; cold-start behavior still falls back to the configured static weights.
- C7 precise offset read + persisted SourceBloom: `get_neighbors` and property-bearing neighbor reads no longer load the full offset array on metadata-cache miss. New CSR segments persist a source Bloom section in the file tail and manifest metadata; cache-miss reads consult it before doing 24-byte offset-section binary-search preads, then read only the matching body range. Legacy segments with no Bloom metadata fall back to the binary-search path.

## Implementation Matrix

| Item | Current state | Evidence | Remaining work |
|---|---|---|---|
| C1 budget reopen accounting | Implemented and debug-tested | `initial_semantic_budget_used_extra_l0_files()` counts only exact non-Mixed L0 partitions; unit/integration tests pass | Empirical budget-reopen RSS/behavior on larger stores after resource gate |
| C2 IO error propagation | Implemented and debug-tested | semantic/index rebuild paths propagate `read_offsets` errors | None known beyond larger-store regression |
| C3 CSR metadata cache LRU | Implemented and debug-tested | cache queue bounded with lazy invalidation tests | None known |
| C4 degree directory memory reduction | Implemented in three stages | exact-L0 admission predicate, packed `DegreeClassMask`, sidecar support | SF1/SF30/SF100 open-time/RSS measurement after user workload |
| C5 Oracle index bug/perf | Implemented and debug-tested | oracle index tests cover precedence/range/type behavior | Oracle baseline table still needs empirical run |
| C6 kv-style labeling | Implemented in summary path | regenerated summaries mark simulated/model rows | Real kv-style import/bench still blocked |
| C7 precise offset read + persisted SourceBloom | Implemented and debug-tested | cache-miss offset binary search and persisted Bloom tests pass | SF1/SF30/SF100 latency/read-byte reruns |
| C8 metadata cache capacity | Implemented | CLI exposes `--csr-metadata-cache-entries`, Engine open paths use it | Byte-budgeted cache is still future work |
| C9 bench protocol | Implemented | warmups/repeats/cache-state summaries and finer latency buckets exist | Release bench validation and SF100 matrix reruns blocked |
| C10 CSR fixed per-probe cost | Not implemented | status intentionally only records C13 rebuild-time clone removal | Needs perf/profile pass when machine is free |
| C11 open-time rebuild elimination | First implementation complete | valid `DEGREE_DIRECTORY` sidecar avoids degree-directory offset scan | Measure sidecar hit/miss open cost on larger stores |
| C12 feedback-to-budget | First-stage implementation complete | feedback-promoted non-core edge-type regression passes | Sustained workload validation and policy tuning remain |
| C13 read_offsets Vec clone | Implemented and debug-tested | cached offsets are `Arc<[EdgeOffset]>`; repeated reads share the same Arc | None known beyond larger-store memory validation |

## Added In This Pass

- C9 storage-bench protocol:
  - added `--warmup-runs` and `--repeats`;
  - records per-round measured output under `rounds`;
  - records warmup output under `warmup_rounds`;
  - adds repeat mean/stddev/min/max summaries for latency, read bytes, body reads, and candidate counts;
  - records lightweight `/proc/meminfo` and loadavg cache-state snapshots before and after the run;
  - keeps old top-level benchmark fields populated from the last measured round for default-compatible output.
- C9 latency buckets: refined latency histogram buckets from 16 coarse buckets to 32 finer buckets.
- C2 completion: `rebuild_index()` for L1+ lookup now also propagates `read_offsets` errors instead of using `unwrap_or_default()`.
- C4 first-stage implementation:
  - added `degree_directory_class_for_l0()` as the single directory-admission predicate;
  - both full semantic-index rebuild and incremental L0 metadata updates skip non-exact degree-Mixed files;
  - added unit coverage proving schema-style degree-Mixed segments remain conservative semantic-index candidates without a directory entry.
- C7 first-stage implementation:
  - added a cache-aware offset lookup path in `CsrReader`;
  - cache hits keep the previous full-metadata + SourceBloom behavior;
  - cache misses and no-cache readers use precise on-disk binary search rather than `metadata_for()`;
  - full scans and `read_offsets()` still use full offset arrays for index rebuild paths.
- C7 persisted SourceBloom implementation:
  - added serde-defaulted `source_bloom_offset`, `source_bloom_len`, and `source_bloom_bit_count` to `CsrSegmentMeta`;
  - added `SourceBloom` word encoding/decoding;
  - `CsrWriter` appends a source Bloom section after body/property sections for new CSR segments;
  - `CsrReader` reads the persisted Bloom on cache miss and skips offset binary search for Bloom-negative sources.
- C4 packed directory implementation:
  - added `DegreeClassMask` helpers for Low/Medium/High directory state;
  - changed full rebuild and incremental update paths to combine masks with bitwise OR;
  - changed semantic L0 candidate routing to accept the mask directly;
  - kept Mixed/Unknown as conservative candidate classes rather than directory entries.
- C11 sidecar implementation:
  - added compact binary `DEGREE_DIRECTORY` read/write helpers with magic, tracked file IDs, and `(src, edge_type, mask)` entries;
  - `Engine::open` now calls `load_or_rebuild_semantic_indexes()` so a valid sidecar avoids the degree-directory offset scan;
  - full semantic rebuild and incremental exact-L0 updates persist the sidecar best-effort;
  - added sidecar round-trip/stale/corrupt unit coverage and a flush/reopen integration regression.
- C13 offset-sharing implementation:
  - changed `CachedCsrMetadata.offsets` from `Vec<EdgeOffset>` to `Arc<[EdgeOffset]>`;
  - changed `CsrReader::read_offsets()` to return `Arc<[EdgeOffset]>`;
  - updated rebuild/index paths to iterate the shared slice without materializing another full offset copy;
  - added a cache-backed unit test proving repeated `read_offsets()` calls return the same offset `Arc`.
- C12 first-stage implementation:
  - added `semantic_budget_feedback_edge_type_weights()` to aggregate `Metrics::l0_partition_snapshots()` by `(src_label, edge_type)`;
  - `evaluate_budgeted_semantic_edge_type()` now scores candidates with `max(configured_static_weight, feedback_weight)`;
  - diagnostics mark feedback-promoted selections with `feedback_score_gate`;
  - added an integration regression where a non-core edge type is cold on the first flush, queried repeatedly, and exact-promoted on the next flush by feedback.
- W3 C12 second-stage implementation:
  - added opt-in `semantic_budget_feedback_only` config and `--semantic-budget-feedback-only` CLI flag; in this mode static LDBC edge-type weights are ignored and cold start uses uniform query weight;
  - added `semantic_budget_disable_feedback` / `--semantic-budget-disable-feedback` for runner no-feedback controls without changing defaults;
  - extended `budgeted-edge-candidates.tsv` with appended static/feedback weight and mode columns while preserving existing column positions;
  - added `w3-workload-shift` synthetic runner for phase A to phase B edge-type workload shifts across feedback-only, static-budgeted, and no-feedback variants;
  - added `baseline/run_w7_workload_shift_20260612.sh` with ETA, progress, resource gate, and timeout abort logic for future W7 SF30 scheduling.
- Test repair during this pass: an intermediate stale local `engine_tests.rs` sync briefly restored outdated budgeted-layout assertions. The file was restored to the current test baseline, then updated so unselected budgeted edge types assert schema-style `edge_type + degree=Mixed` behavior, matching the implemented layout.

## Verification

- `cargo test -j 16` under `nice -n 10`: passed.
  - lib tests: 59 passed.
  - integration tests: 51 passed, 1 existing ignored.
  - doc/bin test targets: no runnable tests.
- Targeted C4 packed-mask test filter `cargo test --lib degree_class -j 16`: passed, 1 test.
- Targeted C4 semantic override test filter `cargo test --lib semantic_l0_index_degree_override -j 16`: passed, 1 test.
- Targeted C4 test filter `cargo test --lib degree_directory -j 16`: passed, 2 tests.
- Targeted C11 sidecar unit filter `cargo test --lib degree_directory_sidecar -j 16`: passed, 1 test.
- Targeted C11 sidecar integration filter `cargo test --test engine_tests semantic_l0_degree_directory_sidecar_survives_reopen -j 16`: passed, 1 test.
- Targeted C13 offset-sharing filter `cargo test --lib read_offsets_reuses_cached_arc_without_cloning_offset_vec -j 16`: passed, 1 test.
- Targeted C12 feedback filter `cargo test --test engine_tests budgeted_semantic_feedback_promotes_hot_non_core_edge_type -j 16`: passed, 1 test.
- Targeted W3 feedback-only filter `cargo +nightly-2025-12-08-x86_64-pc-windows-msvc test --test engine_tests budgeted_semantic_feedback_only -j 1`: passed, 2 tests.
- W3 runner compile checks passed with MSVC toolchain and short temp target:
  - `cargo +nightly-2025-12-08-x86_64-pc-windows-msvc check --bin w3-workload-shift -j 1`
  - `cargo +nightly-2025-12-08-x86_64-pc-windows-msvc check --bin lsmgraph -j 1`
- W3 synthetic SF1-class smoke passed:
  - `cargo +nightly-2025-12-08-x86_64-pc-windows-msvc run --bin w3-workload-shift -- --store-dir target\w3-workload-shift-smoke-default --output target\w3-workload-shift-smoke-default.json --reset-store`
  - feedback-only phase B promoted the new hot non-core edge type by `feedback_score_gate` at snapshot 16 with feedback weight 18.0 and score 0.5625.
- Full local test on this Windows worktree passed with the usable MSVC toolchain and temp target: `cargo +nightly-2025-12-08-x86_64-pc-windows-msvc test -j 16` passed 59 lib tests, 53 integration tests, 1 ignored integration test, and bin/doc test targets. The requested `nice -n 10 cargo test -j 16` could not be run locally because PowerShell had no `nice`, and the active GNU Rust toolchain lacked `dlltool.exe` for proc-macro linking.
- Targeted budgeted semantic filter `cargo test --test engine_tests budgeted_semantic -j 16`: passed, 8 tests.
- Targeted rebuild/open filters passed:
  - `cargo test --test engine_tests incremental_semantic_index_matches_reopen_full_rebuild -j 16`
  - `cargo test --test engine_tests semantic_l0_degree_directory_sidecar_survives_reopen -j 16`
- Targeted repaired budget/schema filters passed:
  - `budgeted_semantic_l0_keeps_edge_type_for_unselected_small_partitions`
  - `budgeted_semantic_edge_type_score_preserves_hot_core_edge`
  - `budgeted_semantic_edge_type_allowlist_limits_exact_materialization`
  - `budgeted_semantic_hard_file_budget`
  - `new_edge_label_prunes_exact_segments_but_reads_mixed_segments`
- Targeted C7 test filter `cargo test --lib get_neighbors_cache_miss_reads_precise_offset_without_full_offset_array -j 16`: passed, 1 test.
- Targeted C7 persisted-Bloom test filter `cargo test --lib source_bloom -j 16`: passed, 1 test.
- CLI smoke:
  - `target/debug/lsmgraph --help` exposes `--csr-metadata-cache-entries`.
  - `target/debug/lsmgraph storage-bench --help` exposes `--warmup-runs` and `--repeats`.
- Source/test/status `git diff --check -- src/csr/cache.rs src/csr/reader.rs src/graph.rs tests/engine_tests.rs baseline/seml0-gap-analysis-20260612/implementation-status-20260612.md`: passed after `cargo fmt`.
- Full-worktree `git diff --check` previously reported trailing whitespace in generated TSV files with existing modified SF100 table data; source files passed formatting via `cargo fmt`.

## Still Open

- C7 still needs SF1/SF30/SF100 empirical reruns to quantify the impact; the code path is implemented and debug-tested.
- C4/C11 still need empirical open-time/RSS measurements on SF1/SF30/SF100 once user workloads finish; the sidecar code path is implemented and debug-tested.
- C10 perf profiling of the CSR fixed per-probe cost remains open; C13 removes one rebuild-time offset clone but does not claim to solve per-query CSR fixed overhead.
- C12 still needs W7 formal SF30 workload-shift validation and policy tuning; W3 now has the feedback-only code path, unit coverage, and SF1-class synthetic runner smoke.
- SF100 matrix reruns and real kv-style runs remain blocked on user workload completion and explicit scheduling.
