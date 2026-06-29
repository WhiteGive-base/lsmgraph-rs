# W10 writing-freeze progress

## Session status

Status: SOURCE-READY; VENUE/PDF PACKAGING OPEN.

Reason: W6, W7, W8, W9, and W13 are available as regenerated evidence blocks. W10 claim safety, paper source assembly, W10 TeX table generation, BACH/SemL0 positioning, artifact checklist, caveat table, package manifest, and lightweight artifact README are complete. Remaining work is venue/template/PDF packaging, not more experiments or W10 claim work.

## Goal

Freeze the SemL0 submission evidence and claims:

- regenerate all main tables from raw JSON/TSV, not hand-filled numbers
- mark every row as measured/simulated/TODO
- replace old simulated kv-style rows with measured W6 `kv-lsm` evidence if W6 completes
- apply Gate 1 latency decision and Gate 2 RSS decision
- make title and claims match actual evidence
- produce submission draft, appendix draft, artifact checklist, and final caveat table
- stop expanding experiments after Tier 1+2 is reached

## Estimated time

- Table regeneration and source audit: 0.5-1 day.
- Paper claim rewrite and caveat pass: 1-2 days.
- Appendix/artifact checklist: 0.5-1 day.

## Start preconditions

- W6 SF100 matrix DONE or explicit W6 fallback decision recorded. Current: DONE.
- Gate 1 and Gate 2 have `GO / FALLBACK` conclusions. Current: Gate 1 = FALLBACK, Gate 2 = GO.
- W13 schema-evolution evidence has DONE or explicit fallback wording. Current: DONE.
- W7/W8/W9 Tier 2 evidence either DONE or explicitly moved to limitation/fallback. Current: W7 real-SF30-derived formal DONE with Gate=GO; W8 DONE; W9 DONE.
- Every result directory used by the paper has `DONE` or `FAILED`. Current: W6/W7/W8/W9/W13 cited result directories have DONE and no FAILED.

## Planned commands / artifacts

Primary inputs:

- `baseline/sf100-matrix-<date>-cn.md`
- `baseline/w6-progress-<date>-cn.md`
- `baseline/w7-workload-shift-progress-<date>-cn.md`
- `baseline/w8-property-2hop-progress-<date>-cn.md`
- `baseline/w9-steady-state-progress-<date>-cn.md`
- `baseline/w13-schema-evolution-progress-<date>-cn.md`

Primary outputs:

- updated paper draft: `baseline/seml0-w10-submission-freeze-draft-cn-20260615.md`
- appendix draft
- artifact checklist
- final caveat table
- this progress document with `submission-ready / not-ready`

## Claim safety rules

- If Gate 1 is `GO`, latency advantage may be written with measured mean/stddev/p50/p90/p99 context.
- If Gate 1 is `FALLBACK`, do not claim stable latency superiority; write candidate/read-amp and avoid full-semantic latency cliff claims.
- If Gate 2 is `GO`, RSS can appear in the main evaluation.
- If Gate 2 is `FALLBACK`, RSS goes to caveat/limitation.
- If W8 property/2-hop is weak or missing, use `Query-Semantic L0 Design` rather than broader `Query-Semantic Physical Design`.
- If W7 formal remains synthetic-only, describe it as controlled self-tuning evidence, not SF30 production evidence.
- If W9 formal remains missing, mixed read/write steady-state must be limitation, not a main claim.
- If W13 tests pass, schema evolution can be claimed within the implemented additive/fixed-width boundary; no full migration/reclamation claim.

## Stop conditions

- Any table cannot be regenerated from raw artifacts.
- Any paper claim points to a missing/FAILED result directory.
- Any measured/simulated/TODO label is ambiguous.
- A required artifact has no reproducible command or path.
- Claims require W11/W12 or new experiments beyond the recommended line; stop and write limitation instead.

## Live status

- 2026-06-13 22:18: Created W10 progress skeleton while W6 `kv-lsm` bench was running. W10 was not started. Current blockers: W6 matrix/Gates not done; W7/W8/W9/W13 formal evidence not done.
- 2026-06-15 14:22: W10 was ready to start. Evidence status at that timestamp: W6 DONE with Gate 1 FALLBACK and Gate 2 GO; W8 DONE with property/2-hop caveats; W9 DONE and regenerated as `baseline/w9-steady-state-summary-20260615-cn.md`; W13 DONE; W7 formal evidence was not yet available and needed either implementation or controlled-evidence wording. Next action was evidence inventory and table-regeneration audit.
- 2026-06-15 14:34: Started W10 evidence-inventory session. Session goal: map W6/W7/W8/W9/W13 evidence to raw artifacts, summary scripts, DONE/FAILED markers, and paper-safe claim boundaries. Estimate: 1-2 hours for inventory and caveat seed, not full paper rewrite. Resource snapshot: `/data` 307G free, MemAvailable 448GiB, no SF30/SF100 long task running. Commands planned: inspect summary reports, raw log directories, marker files, and paper draft candidates; create `baseline/w10-evidence-inventory-20260615-cn.md`. Stop conditions: missing DONE/FAILED marker for cited evidence, table not regenerable from raw artifacts, ambiguous measured/simulated/TODO label, or claim requiring new W11/W12 experiments.
- 2026-06-15 14:36: While checking W13 runner help, `baseline/run_w13_schema_evolution_20260613.sh --help` executed the runner because no help mode exists. It completed successfully as supplemental sanity rerun: run id `w13-schema-evolution-20260615-143649`, 10 tests passed, `DONE` exists and `FAILED` absent. Treat this as supplemental verification; the official W13 evidence remains the recorded W13 DONE block unless W10 chooses to cite the newer run.
- 2026-06-15 14:40: Created `baseline/w10-evidence-inventory-20260615-cn.md`. It maps W6/W7/W8/W9/W13 evidence to raw artifact roots, marker states, regenerated reports, reproduction commands, safe claims, and caveat seeds. Evidence inventory is complete enough to start paper/table rewriting, but the actual draft, appendix, artifact checklist, final caveat table, and submission verdict are still not done.
- 2026-06-15 14:47: Created W10 standalone caveat/artifact drafts: `baseline/w10-final-caveat-table-20260615-cn.md` and `baseline/w10-artifact-checklist-20260615-cn.md`. These are ready to be merged into paper/appendix/artifact materials. Paper integration and final submission verdict are still not done.
- 2026-06-15 14:57: Created `baseline/seml0-w10-submission-freeze-draft-cn-20260615.md`. This draft supersedes the unsafe quantitative claims in `baseline/seml0-linux-main-paper-draft-cn-20260611.md`: title narrowed to query-semantic L0 design; every evaluation row is labeled measured/controlled; W6 Gate 1 FALLBACK and Gate 2 GO wording is applied; W7 is downgraded to controlled evidence/limitation; W8/W9/W13 caveats are integrated. Estimate for this pass was 1-2 hours; completed within the time box. No long experiment was started.
- 2026-06-15 15:38: W7 real-SF30-derived formal run completed and changed the W10 evidence surface. Run id `w7-sf30-workload-shift-formal-20260615-1536`; summary copied to `baseline/w7-sf30-workload-shift-summary-20260615-cn.md`; Gate=GO. The follow-up action was to revise the W10 materials to W7 formal-with-caveat wording and then stop expanding experiments.
- 2026-06-15 15:52: W10 pass-2 documentation update completed. The submission-freeze draft, final caveat table, artifact checklist, and current progress table now cite W7 formal Gate=GO with a derived-workload caveat. Added BACH/SemL0 positioning: BACH addresses AL-vs-CSR physical locality, while SemL0 adds exact segment-level semantic pruning before scan. Numeric property-range pruning such as `Like.time` remains future work/limitation unless measured separately. No new experiment was started.
- 2026-06-15 16:05: Started W10 final source-assembly session. Goal: replace the stale SF1/no-SF100 `paper/main.tex` with a W10 source-ready SIGMOD-oriented draft, generate W10 paper table sources from frozen W6/W7/W8/W9/W13 evidence, and refresh paper artifact/package docs. Estimate: 1.5-3 hours for source assembly and static validation; no benchmark or experiment will run. Planned commands: inspect `paper/main.tex`, W10 summaries, and paper package docs; edit TeX/markdown sources; run static grep checks for stale SF1/no-SF100 and unsafe claims; sync to Linux and verify paths. Stop conditions: any table lacks a raw/regenerated evidence source, any paper claim points to a missing/FAILED result directory, `paper/main.tex` still claims SF30/SF100 future-only, or a required final-submission action needs TeX/template/environment changes.
- 2026-06-15 16:31: W10 source assembly completed locally. Replaced `paper/main.tex` with a W10 source-ready paper using title `Query-Semantic L0 Design`; added `baseline/render_w10_sigmod_tables_20260615.py`; generated 11 `paper/tables/w10-*.tex` inputs from regenerated evidence reports; refreshed `paper/artifact-checklist.md` and `paper/package-manifest.md`. Static checks passed locally: no missing `\input{}` files, no missing citation keys, no non-ASCII in edited paper/package files, and no stale SF1/no-SF100 main-claim wording in `paper/main.tex`. No experiments were started.
- 2026-06-15 16:37: Created lightweight artifact bundle README `artifact/README-W10-SemL0.md` and referenced it from `paper/artifact-checklist.md` and `paper/package-manifest.md`. This records source/package contents, large evidence roots, table regeneration commands, claim boundaries, and remaining non-experiment submission blockers. No large stores or logs were copied.
- 2026-06-15 16:43: Synced W10 source package to Linux and verified it there. `python3 baseline/render_w10_sigmod_tables_20260615.py` succeeded; `paper/main.tex` has no missing `\input{}` files and no missing citation keys; edited paper/package files have no non-ASCII; stale SF1/no-SF100 and W7-missing wording was not found. W6/W7/W8/W9/W13 primary `DONE` markers exist, and a simple `FAILED` path check produced no FAILED files. `pdflatex`, `latexmk`, and `tectonic` were not found on PATH, so PDF build remains a TeX-backend blocker.
- 2026-06-15 16:52: Created completion audit `baseline/seml0-completion-audit-20260615-cn.md`. Audit verdict: Tier 1 and recommended Tier 2 are source-ready with caveats; W10 evidence/source freeze is complete; remaining venue/template/PDF/page/archive work is submission engineering and must not reopen experiments.

## Completed

- W10 start preconditions recorded.
- Claim safety rules recorded.
- Stop conditions recorded.
- W6/W8/W9/W13 evidence availability recorded.
- W7 formal evidence recorded as real-SF30-derived self-tuning evidence with a full-store production-trace caveat.
- W10 evidence-inventory session start recorded.
- Supplemental W13 sanity rerun recorded.
- Evidence inventory and claim-safety seed created: `baseline/w10-evidence-inventory-20260615-cn.md`.
- Standalone final caveat table draft created: `baseline/w10-final-caveat-table-20260615-cn.md`.
- Standalone artifact checklist draft created: `baseline/w10-artifact-checklist-20260615-cn.md`.
- W10-safe paper draft created: `baseline/seml0-w10-submission-freeze-draft-cn-20260615.md`.
- Paper-facing table rewrite pass 1 completed in the W10 draft: W6/W8/W9/W13 rows cite regenerated reports and raw roots.
- Gate 1 claim rewrite completed in the W10 draft: no stable latency-superiority claim for `budg-b64` over schema.
- Gate 2 RSS rewrite completed in the W10 draft: RSS appears as measured result with schema/naive=0.99x and `budg-b64`/naive=0.91x.
- W7 real-SF30-derived formal evidence recorded: `baseline/w7-sf30-workload-shift-summary-20260615-cn.md`, Gate=GO.
- W8/W9/W13 claim decisions integrated into the W10 draft with caveats.
- W10 pass-2 W7 integration completed in the submission-freeze draft, caveat table, artifact checklist, evidence inventory, and current progress table.
- BACH/SemL0 positioning recorded with property-range pruning kept as limitation/future work.
- W10 source-ready paper assembly completed in `paper/main.tex`.
- W10 TeX tables generated from regenerated W6/W7/W8/W9/W13 reports under `paper/tables/w10-*.tex`.
- W10 paper artifact checklist and package manifest refreshed.
- Lightweight artifact bundle README created: `artifact/README-W10-SemL0.md`.
- Linux sync and static source verification completed for W10 paper/package files.
- Completion audit created: `baseline/seml0-completion-audit-20260615-cn.md`.
- Current submission-readiness verdict recorded below.

## Not yet completed outside W10 evidence/source freeze

- Venue/template conversion and page-budget editing.
- PDF build and visual inspection.
- Venue-ready artifact archive packaging.

## Current verdict

Minimum-line verdict: `source-ready with narrowed claims`. The W10 paper source now uses the safe title, W6 Gate 1 fallback wording, W6 Gate 2 RSS wording, W7 derived-workload caveat, W8/W9 caveats, W13 boundary, and BACH/property-range limitation.

Recommended-line evidence verdict: `source-ready with caveats`. W6/W7/W8/W9/W13 evidence blocks are complete and reflected in `paper/main.tex`. W7 is real-SF30-derived, not a full-store production trace. BACH/property-range positioning is claim-safe: BACH is related-work motivation, while unmeasured numeric property-range pruning remains future work. Overall venue-specific submission is still `not-ready` until venue/template conversion, PDF build, page-budget check, visual inspection, and final artifact archive packaging are complete.

## Next minimal action

Proceed only with non-experiment submission packaging if requested: choose/import the ACM/SIGMOD template route, provide a TeX backend, build PDF, check page budget, visually inspect, and create a venue-ready archive. Do not start W11/W12 or new external-baseline experiments.
