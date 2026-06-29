# W10 evidence inventory and claim-safety seed

Generated: 2026-06-15 15:52 CST

Purpose: freeze the current SemL0 evidence surface before paper rewriting. This is not the final submission draft; it is the audit map that W10 should use to regenerate tables, mark measured/controlled/fallback rows, and make claims safe.

## Session scope

| item | value |
|---|---|
| W10 session goal | Evidence inventory + table-regeneration audit seed |
| Estimate | 1-2 hours for this inventory; full W10 writing freeze remains 1-3 days |
| Resource snapshot | `/data` 307G free; MemAvailable 448GiB; no SF30/SF100 long task running |
| Stop conditions | Missing DONE/FAILED marker for cited evidence; table not regenerable from raw artifacts; ambiguous measured/simulated/TODO label; claim requiring W11/W12/new experiments |
| Current verdict | W10 pass 2 documentation update is complete. W6/W7/W8/W9/W13 are usable; W7 is real-SF30-derived formal evidence with a full-store production caveat. Final SIGMOD-format paper/appendix assembly remains. |

## Artifact Status Matrix

| Block | Evidence class | Raw artifact root | Marker state | Regenerated report | Regeneration command | Paper status |
|---|---|---|---|---|---|---|
| W6 SF100 matrix | measured SF100 | `remote-logs/w6-sf100-matrix-20260613-132325` | `DONE` yes; `FAILED`/`ABORT` absent | `baseline/sf100-matrix-20260613-cn.md` | `python3 baseline/summarize_w6_sf100_matrix_20260613.py --log-dir remote-logs/w6-sf100-matrix-20260613-132325 --out baseline/sf100-matrix-20260613-cn.md` | Main-table eligible with Gate caveats |
| W7 workload shift | real-SF30-derived formal | `remote-logs/w7-sf30-workload-shift-formal-20260615-1536` | `DONE` yes; `FAILED`/`ABORT` absent | `baseline/w7-sf30-workload-shift-summary-20260615-cn.md` | `RUN_ID=w7-sf30-workload-shift-formal-20260615-1536 MODE=formal RUN_FORMAL=1 bash baseline/run_w7_sf30_workload_shift_20260615.sh` | Main/appendix eligible with derived-workload caveat |
| W8 property + 2-hop | measured SF30 | `remote-logs/w8-property-2hop-20260614-2025` | `DONE` yes; `FAILED`/`ABORT` absent | `baseline/w8-property-2hop-summary-20260615-cn.md` | `python3 baseline/summarize_w8_property_2hop_20260615.py --log-root remote-logs/w8-property-2hop-20260614-2025 --output baseline/w8-property-2hop-summary-20260615-cn.md --source-label remote-logs/w8-property-2hop-20260614-2025` | Main/appendix eligible with 2-hop candidate caveat |
| W9 mixed read/write | measured SF30 | `remote-logs/w9-steady-state-formal-20260615-1200` | `DONE` yes; `FAILED`/`ABORT` absent | `baseline/w9-steady-state-summary-20260615-cn.md` | `python3 baseline/summarize_w9_steady_state_20260615.py --log-root remote-logs/w9-steady-state-formal-20260615-1200 --output baseline/w9-steady-state-summary-20260615-cn.md --source-label remote-logs/w9-steady-state-formal-20260615-1200` | Main/appendix eligible with candidate/L0-files caveat |
| W13 schema evolution | measured test evidence | `remote-logs/w13-schema-evolution-20260614-0004` | `DONE` yes; `FAILED`/`ABORT` absent | `remote-logs/w13-schema-evolution-20260614-0004/summary.md` | `RUN_ID=w13-schema-evolution-20260614-0004 bash baseline/run_w13_schema_evolution_20260613.sh` | Claim-map eligible within bounded schema-evolution scope |
| W13 supplemental rerun | supplemental test evidence | `remote-logs/w13-schema-evolution-20260615-143649` | `DONE` yes; `FAILED` absent | `remote-logs/w13-schema-evolution-20260615-143649/summary.md` | Triggered by `bash baseline/run_w13_schema_evolution_20260613.sh --help`; runner lacks help mode and executed tests | Supplemental sanity only; cite primary W13 unless using latest rerun consistently |

## Regenerable Table Map

| Table / row family | Source report | Raw source | Status label | W10 action |
|---|---|---|---|---|
| SF100 aggregate matrix: schema/naive/kv-lsm/edge-type-only/semantic/budg/oracle | `baseline/sf100-matrix-20260613-cn.md` | W6 bench JSON + manifest under W6 run dir | measured | Use as primary scale table. Keep `kv-lsm` as measured replacement for old simulated kv-style row. |
| SF100 correctness compares | `baseline/sf100-matrix-20260613-cn.md` | W6 `compare-naive-vs-*.json` | measured | Use as correctness table or appendix; all checked=45,000 and mismatches=0. |
| SF100 import RSS | `baseline/sf100-matrix-20260613-cn.md` | W6 import stdout/stderr + summary script | measured | Gate 2 GO: RSS can be main result. Phrase as measured RSS, not theoretical memory bound. |
| SF100 latency advantage | `baseline/sf100-matrix-20260613-cn.md` | W6 bench JSON | measured but Gate 1 FALLBACK | Do not claim stable budg-b64 latency advantage over schema; 1-stddev intervals overlap. |
| SF100 read bytes | `baseline/sf100-matrix-20260613-cn.md` | W6 bench JSON | measured with caveat | Keep as secondary unless reader-overread caveat is explicitly resolved in text. |
| W8 property predicates | `baseline/w8-property-2hop-summary-20260615-cn.md` | W8 wrapped JSON outputs | measured | Main/appendix table eligible. Safe claim: presence/equality/absent-default improve candidate/body/read/elapsed. |
| W8 required-property exact prune | `baseline/w8-property-2hop-summary-20260615-cn.md` | W8 property-required JSON | measured | Treat as exact-prune sanity case: zero candidates/body reads for all variants. |
| W8 2-hop typed expansion | `baseline/w8-property-2hop-summary-20260615-cn.md` | W8 2hop JSON | measured with caveat | Safe claim: body/read/elapsed improve. Unsafe claim: universal candidate reduction. |
| W9 steady-state checkpoint trace | `baseline/w9-steady-state-summary-20260615-cn.md` | W9 JSONL | measured | Use for mixed read/write coverage and stability. |
| W9 deltas vs schema | `baseline/w9-steady-state-summary-20260615-cn.md` | W9 JSONL | measured with caveat | `budg-b64` candidate L0 -4.1%, p99 -1.2%, p50 +1.8%; `semantic` p50 -63.9%, p99 -82.5%, but candidate L0 +86.3% and last L0 files +93.4%. |
| W13 schema evolution tests | W13 `summary.md` | W13 test dirs and `tests.tsv` | measured test evidence | Use as claim-map appendix/table. Boundary: additive/fixed-width/schema-epoch correctness only. |
| W7 self-tuning | `baseline/w7-sf30-workload-shift-summary-20260615-cn.md` | W7 formal JSON under `remote-logs/w7-sf30-workload-shift-formal-20260615-1536` | real-SF30-derived measured with caveat | Use as workload-shift/self-tuning evidence. Caveat: derived from SF30 CSV into a small formal store, not a full-store production trace. |

## Claim Safety Matrix

| Claim candidate | Decision | Evidence | Required wording |
|---|---|---|---|
| SemL0 reduces SF100 read amplification/candidates vs naive | OK with caveat | W6 aggregate matrix | Use measured candidate/read-byte numbers; keep read bytes secondary if reader-overread caveat remains. |
| SemL0/budg-b64 has stable latency superiority over schema at SF100 | Do not claim | W6 Gate 1 FALLBACK | Say latency advantage is not statistically stable under current repeats; emphasize candidate/read-amp and avoiding full-semantic cliff. |
| SemL0/budg-b64 RSS is same order as naive/schema | OK | W6 Gate 2 GO | Say measured RSS: schema/naive=0.99x, budg-b64/naive=0.91x. |
| kv-style baseline row is measured | OK | W6 `kv-lsm` import/bench/compare | Replace old simulated kv-style row with measured W6 `kv-lsm`. |
| Self-tuning adapts under real-SF30-derived workload shifts | OK with caveat | W7 formal Gate=GO | Say feedback-only selects hot semantic partitions in phase A/B and the selected range changes after workload shift. Caveat: derived workload, not full-store production trace. |
| Property predicates are covered | OK | W8 property results | Say property presence/equality/absent-default reduce candidate/body/read/elapsed. |
| 2-hop always reduces candidates | Do not claim | W8 2-hop deltas | Say 2-hop reduces body reads/read bytes/elapsed, but candidate L0 is higher than schema. |
| Mixed read/write steady-state is covered | OK | W9 final summary | Say all three variants completed SF30 steady-state checkpoints with no writer errors/slow ops. |
| Semantic steady-state reduces candidates/L0 files | Do not claim | W9 delta table | Semantic improves p50/p99 but increases candidate L0 and final L0 files. |
| Schema evolution is safe | OK within boundary | W13 tests | Say schema epochs, old-segment readability, stable id/alias/drop behavior, fixed-width property encoding, and conservative exact pruning are tested. Do not claim full migration/reclamation. |
| Title can be broad `Query-Semantic Physical Design` | Do not use by default | W8/W9 caveats and unmeasured property-range pruning | Safer default remains `Query-Semantic L0 Design`. Broaden only if the final paper explicitly absorbs W8/W9 caveats and keeps property-range pruning out of measured claims. |
| BACH shows SemL0's gap | OK as positioning | W6/W7/W8/W9/W13 plus related-work framing | Say BACH improves AL-vs-CSR physical locality after a relevant region is reached; SemL0 adds exact semantic segment pruning before scan. Do not claim measured `Like.time` range pruning without a dedicated range-metadata experiment. |

## Caveat Seed For Paper

| Caveat | Where to put | Text to preserve |
|---|---|---|
| Gate 1 latency fallback | Main evaluation + caveat table | SF100 budg-b64 latency is lower on mean but not stable under 1-stddev overlap; no stable latency-superiority claim. |
| Reader over-read / read bytes | Evaluation footnote or caveat table | Treat read bytes as measured but secondary unless reader-overread caveat is resolved. |
| W7 derived-workload boundary | Limitations or self-tuning subsection | W7 formal uses SF30 CSV-derived workload stores, not copied full W8/W9 113G production stores. |
| W8 2-hop candidate increase | Evaluation text | 2-hop body/read/elapsed improve, but candidate L0 increases relative to schema. |
| W9 semantic candidate/L0-files increase | Steady-state subsection | Semantic improves p50/p99 under mixed workload but increases candidate L0 and final L0 files; only claim latency/stability there. |
| W13 boundary | Schema-evolution section | Tests cover implemented additive/fixed-width/schema-epoch behavior, not full migration/reclamation. |

## W10 Next Actions

1. Update the paper draft tables so every numeric row cites one source report and one raw root.
2. Add measured/controlled/simulated/TODO labels to every evaluation row.
3. Replace old simulated kv-style row with W6 measured `kv-lsm`.
4. Apply Gate 1/2 wording exactly.
5. Integrate W7 formal Gate=GO into the W10 paper draft with the derived-workload caveat.
6. Produce final caveat table and artifact checklist.
7. Write `submission-ready / not-ready` in `baseline/w10-writing-freeze-progress-20260613-cn.md`.
