## Appendix

### A. Claim → evidence map
- C1 read-amp/candidate reduction → `baseline/sf100-matrix-20260613-cn.md` (W6), `baseline/w8-property-2hop-summary-20260615-cn.md` (W8).
- C2 lifecycle retention → `baseline/path-b-c2/stage6-gate-verdict-20260618-cn.md`, `stage5-scale-summary-20260618-cn.md`, raw `stage4-smoke-sf1-20260618.json` / `stage5-scale-sf10class-20260618.json`; real SF30 `remote-logs/c2-sf30-real-20260619/` + `baseline/path-b-c2/stage5-sf30-real-summary-20260619-cn.md`; tables `tables/table-c2-retention.md` + `tables/external-baseline-md.md`.
- C3 schema/snapshot correctness → `baseline/w13-schema-evolution-summary-20260614.md`; lifecycle/reopen engine tests.
- Gate decisions → `PLAN-SEML0-SIGMOD2027-CODEX-CN.md` §3, `baseline/path-b-c2/`.

### B. Reproduction pointers
- Correctness gate: `cargo test --lib` (69), `cargo test --test engine_tests` (61), `cargo test --bin lsmgraph` (4).
- C2 runner: `cargo run --bin c2-merge-retention -- --sources <N> --edge-types <list> --output <json>`.
- Code anchors: `SemanticL0Index` + `LevelMergePolicy` + `compact_level_to_next` + `split_semantic_compaction_segments` (src/graph.rs); `CsrSegmentMeta` + `signature_pruning_decision` + `SegmentSemanticState` + `PruningSurfaceSummary` (src/csr/format.rs); `SchemaCatalog` (src/schema.rs).

### C. Reviewer question bank
See `reviewer-question-bank.md`.

### D. Open submission steps (Stage 10)
Target venue + template, TeX/PDF compilation (no LaTeX engine in the current environment), page-budget fit, PDF visual inspection, bib citations for related work.

### E. Figures (planned; data in `tables/plot-data-md.md`)
Three figures, drawn at TeX assembly. Each only visualizes data already in §3–§7 and adds no new claim.

- **Figure 1 — Pipeline / lifecycle diagram** (`figures/seml0-pipeline.pdf`): the four cooperating loops of §3 (write/flush → read/prune → feedback/compaction → schema/snapshot safety) over the shared semantic metadata, showing where the pruning surface is created, exploited, maintained, and resolved.
- **Figure 2 — Schema-snapshot timeline** (`figures/schema-snapshot-timeline.pdf`): the §6 timeline — old segment written at epoch `e0`, catalog adds an edge label at `e1`, future writes enter new L0 segments, a read resolves the new label under `e1`, old exact-disjoint segments auto-skip, and unknown/mixed old segments are read conservatively — with no rewrite at the schema-change boundary.
- **Figure 3 — C2 retention before/after bar** (`figures/c2-retention.pdf`): the §5 microbenchmark — `exact_surface_ratio` and typed-neighbor read bytes before vs after an L1→L2 merge under naive vs semantic, with the bounded `write_amp` cost (≈1.85 vs ≈1.14) shown alongside.
