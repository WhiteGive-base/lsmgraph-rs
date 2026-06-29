# SemL0 W10 Artifact README

Date: 2026-06-15

This artifact README describes the source-ready W10 SemL0 package. It is a
lightweight packaging manifest: it does not copy large SF30/SF100 stores into
the repository and it does not claim that a final venue PDF exists.

## What Is Included

| Group | Paths |
|---|---|
| Rust source | `src/`, `tests/`, `Cargo.toml`, `Cargo.lock` |
| Paper source | `paper/main.tex`, `paper/references.bib`, `paper/figures/`, `paper/tables/w10-*.tex` |
| Paper package docs | `paper/artifact-checklist.md`, `paper/package-manifest.md` |
| W10 renderer | `baseline/render_w10_sigmod_tables_20260615.py` |
| W6 scripts | `baseline/run_w6_sf100_matrix_20260613.sh`, `baseline/summarize_w6_sf100_matrix_20260613.py` |
| W7 scripts | `baseline/run_w7_sf30_workload_shift_20260615.sh`, `baseline/summarize_w7_sf30_workload_shift_20260615.py` |
| W8 scripts | `baseline/run_w8_property_2hop_20260612.sh`, `baseline/summarize_w8_property_2hop_20260615.py` |
| W9 scripts | `baseline/run_w9_steady_state_20260612.sh`, `baseline/summarize_w9_steady_state_20260615.py` |
| W13 scripts | `baseline/run_w13_schema_evolution_20260613.sh` |
| W10 audit docs | `baseline/w10-evidence-inventory-20260615-cn.md`, `baseline/w10-final-caveat-table-20260615-cn.md`, `baseline/w10-artifact-checklist-20260615-cn.md`, `baseline/w10-writing-freeze-progress-20260613-cn.md`, `baseline/seml0-current-progress-cn.md` |

## Large Evidence Roots

These roots are expected to remain on the Linux evidence host. They are not
copied into a small submission source bundle.

| Block | Root | Marker expectation |
|---|---|---|
| W6 | `remote-logs/w6-sf100-matrix-20260613-132325` | `DONE`, no `FAILED` |
| W7 | `remote-logs/w7-sf30-workload-shift-formal-20260615-1536` | `DONE`, no `FAILED` |
| W8 | `remote-logs/w8-property-2hop-20260614-2025` | `DONE`, no `FAILED` |
| W9 | `remote-logs/w9-steady-state-formal-20260615-1200` | `DONE`, no `FAILED` |
| W13 | `remote-logs/w13-schema-evolution-20260614-0004` | `DONE`, no `FAILED` |

## Regenerate Paper Tables

Run from the repository root on the Linux evidence host:

```bash
python3 baseline/summarize_w6_sf100_matrix_20260613.py \
  --log-dir remote-logs/w6-sf100-matrix-20260613-132325 \
  --out baseline/sf100-matrix-20260613-cn.md
python3 baseline/summarize_w7_sf30_workload_shift_20260615.py \
  --input remote-logs/w7-sf30-workload-shift-formal-20260615-1536/w7-sf30-workload-shift.json \
  --output baseline/w7-sf30-workload-shift-summary-20260615-cn.md \
  --source-label remote-logs/w7-sf30-workload-shift-formal-20260615-1536
python3 baseline/summarize_w8_property_2hop_20260615.py \
  --log-root remote-logs/w8-property-2hop-20260614-2025 \
  --output baseline/w8-property-2hop-summary-20260615-cn.md
python3 baseline/summarize_w9_steady_state_20260615.py \
  --log-root remote-logs/w9-steady-state-formal-20260615-1200 \
  --output baseline/w9-steady-state-summary-20260615-cn.md
python3 baseline/render_w10_sigmod_tables_20260615.py
```

## Claim Boundaries

| Claim | Boundary |
|---|---|
| SF100 latency | Gate 1 is FALLBACK; do not claim stable latency superiority |
| SF100 RSS | Gate 2 is GO; RSS is measured, not theoretical |
| W7 self-tuning | real-SF30-derived formal evidence, not production trace |
| W8 property/2-hop | property positive; 2-hop candidate reduction is mixed |
| W9 steady state | workload coverage plus metric-specific deltas only |
| W13 schema evolution | bounded additive/fixed-width/schema-epoch correctness |
| BACH/property range | BACH is motivation; `Like.time` range pruning is future work |

## Current Submission Status

```text
experiments_frozen=yes
w10_source_ready=yes
paper_pdf_ready=no
venue_template_ready=no
page_budget_ready=no
visual_inspection_ready=no
final_submission_ready=no
```

The next required work is non-experimental: ACM/SIGMOD template conversion,
TeX/PDF build, page-budget check, PDF visual inspection, and packaging the
source plus evidence summaries for review.
