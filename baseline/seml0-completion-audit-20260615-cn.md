# SemL0 completion audit: W6/W7/W8/W9/W13/W10 convergence

Generated: 2026-06-15 16:52 CST

This audit checks the active convergence objective requirement by requirement.
It uses the Linux evidence tree under `/data/WorkSpace/lsmgraph-rs` as the
authoritative state. It does not start new experiments.

## Session Scope

| item | value |
|---|---|
| Session goal | Prove whether the SemL0 Tier 1+2 evidence line and W10 claim-safety/source-freeze line are complete. |
| Estimate | 30-60 minutes for audit and progress-doc cleanup. |
| Commands used | inspect progress docs, package docs, paper source, evidence markers, W10 table renderer, and TeX backend availability. |
| Stop conditions | Missing DONE marker, present FAILED marker, missing W10 table source, stale unsafe claim wording, or claim requiring W11/W12/new experiments. |
| Long tasks | none |

## Requirement Audit

| Requirement | Status | Evidence | Notes |
|---|---|---|---|
| W6 SF100 matrix completed | PROVED | `remote-logs/w6-sf100-matrix-20260613-132325/DONE`; `baseline/sf100-matrix-20260613-cn.md` | Full W6 matrix report regenerated from raw JSON/TSV. |
| W6 Gate 1 decided | PROVED: FALLBACK | `baseline/sf100-matrix-20260613-cn.md`, Gate Candidates | `budg-b64` mean latency is lower than schema, but 1-stddev intervals overlap. No stable latency-superiority claim. |
| W6 Gate 2 decided | PROVED: GO | `baseline/sf100-matrix-20260613-cn.md`, Import RSS | schema/naive RSS=0.99x and `budg-b64`/naive RSS=0.91x. |
| W7 workload shift evidence | PROVED WITH CAVEAT | `remote-logs/w7-sf30-workload-shift-formal-20260615-1536/DONE`; `baseline/w7-sf30-workload-shift-summary-20260615-cn.md` | Gate=GO. Evidence is real-SF30-derived, not a full-store production trace. |
| W8 property + 2-hop evidence | PROVED WITH CAVEATS | `remote-logs/w8-property-2hop-20260614-2025/DONE`; `baseline/w8-property-2hop-summary-20260615-cn.md` | Property predicates improve candidate/body/read/elapsed. 2-hop improves body/read/elapsed but candidate L0 increases. |
| W9 mixed read/write steady-state | PROVED WITH CAVEATS | `remote-logs/w9-steady-state-formal-20260615-1200/DONE`; `baseline/w9-steady-state-summary-20260615-cn.md` | All variants have final done events; writer_errors=0, slow_ops=0. Claims must follow delta table. |
| W13 schema evolution | PROVED WITH BOUNDARY | `remote-logs/w13-schema-evolution-20260614-0004/DONE`; summary has 10/10 tests pass | Supports additive/fixed-width/schema-epoch correctness, not full migration/reclamation. |
| W10 evidence inventory | PROVED | `baseline/w10-evidence-inventory-20260615-cn.md` | Maps W6/W7/W8/W9/W13 raw roots, markers, summaries, and safe claims. |
| W10 caveat table | PROVED | `baseline/w10-final-caveat-table-20260615-cn.md` | Includes Gate 1, W7, W8, W9, W13, and BACH/property-range boundaries. |
| W10 artifact checklist | PROVED | `baseline/w10-artifact-checklist-20260615-cn.md`; `paper/artifact-checklist.md` | Source package and evidence roots are listed. |
| W10 paper source assembly | PROVED | `paper/main.tex` | Title narrowed to `Query-Semantic L0 Design`; W6/W7/W8/W9/W13 reflected in paper source. |
| W10 paper tables are regenerated, not hand-filled | PROVED | `baseline/render_w10_sigmod_tables_20260615.py`; `paper/tables/w10-*.tex` | Linux command `python3 baseline/render_w10_sigmod_tables_20260615.py` succeeded. |
| W10 source static checks | PROVED | Linux check: no missing `\input{}` files, no missing citation keys, no non-ASCII in edited paper/package files | Stale SF1/no-SF100 and W7-missing wording was not found in W10 source/package docs. |
| Lightweight artifact README | PROVED | `artifact/README-W10-SemL0.md` | Lists source, scripts, evidence roots, table regeneration, claim boundaries, and submission blockers. |
| Stop expanding experiments | PROVED | `baseline/seml0-current-progress-cn.md`; no running SF30/SF100 task recorded | W11/W12 and new external baseline experiments are explicitly stopped. |
| Progress docs updated | PROVED | `baseline/seml0-current-progress-cn.md`; `baseline/w10-writing-freeze-progress-20260613-cn.md` | Both now distinguish source-ready convergence from venue/PDF packaging blockers. |

## Source-Ready Verdict

The SemL0 evidence and claim-safety convergence target is complete at
`source-ready with caveats`:

- Tier 1 source-ready line: W6 + W13 + W10 is reached.
- Recommended source-ready line: W6 + W7 + W8 + W9 + W13 + W10 is reached.
- The experiment stop line is reached; do not start W11/W12 or new external
  baseline experiments for this submission source.

## Remaining Work That Is Not An Experiment Gap

These items are still open before a final conference upload package exists:

| Gate | Current evidence | Why it remains open |
|---|---|---|
| ACM/SIGMOD template conversion | `paper/main.tex` is source-ready but still uses the local article-style source path | A target template route has not been applied in this package. |
| TeX backend | Linux PATH check found no `pdflatex`, `latexmk`, or `tectonic` | PDF cannot be built on the current host without a TeX backend or approved Docker image. |
| PDF build | `paper/main.pdf` is not produced by this audit | Blocked by TeX backend/template route. |
| Page budget | no built PDF page count | Requires venue template and PDF. |
| Visual inspection | not performed | Requires PDF. |
| Venue-ready archive | lightweight README exists, archive not built | Should be created after template/PDF/page checks. |

## Final Recommendation

Stop experiments now. The next work, if the user wants an uploadable package,
is purely submission engineering:

1. choose/import the ACM/SIGMOD template route;
2. provide a TeX backend or approved TeX-capable Docker image;
3. build `paper/main.pdf`;
4. check page budget and visually inspect tables/figures/citations;
5. create the venue-ready source/artifact archive.

Do not use the open PDF/template/archive gates as a reason to reopen W6/W7/W8/W9/W13
or add W11/W12.
