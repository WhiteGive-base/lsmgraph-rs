# Formal Figure 1--6 plotting implementation

These scripts consume wide tidy TSV rows governed by the frozen contract at
`../../../plan/FIGURE-DATA-REQUIREMENTS.tsv` and the design in
`../../../plan/FIGURE-SPEC-CN.md`. They contain design constants (orders, styles,
and fixed gates), but no experiment-result constants.

```text
python plot_figure1_end_to_end.py --input E01-results.tsv --out-dir output
python plot_figure2_component_ablation.py --input E03-results.tsv --out-dir output
python plot_figure3_resource_pareto.py --input E04-results.tsv --out-dir output
python plot_figure4_dynamic_compaction.py --input E05-results.tsv --out-dir output
python plot_figure5_workload_coverage.py --input E06-results.tsv --out-dir output
python plot_figure6_scalability.py --input E07-E08-results.tsv --out-dir output
```

Each successful command uses `../figure_common.py` and emits
PNG, SVG, PDF, and a `.layout.json` QA report. Figures 1--3 expose their run
minimums/selectors as CLI options. Figure 4 uses E05 and fixed 30-second windows;
Figure 5 uses E06 and defaults its fallback panel to `budg-b64`; Figure 6 expects
E07 scaling plus E08 concurrency rows in the same TSV (IDs are configurable).

Validation is intentionally strict:

- all `required=yes` COMMON and figure-specific contract columns must exist;
- the requested experiment ID must be present;
- at least one row must have `digest_pass=true` and `mismatch_count=0`;
- every plotted comparison cell must meet its configured independent-run minimum;
- matched traces, data, cache state, and concurrency must agree;
- missing, zero, timeout, unsupported, proxy, and full-execution values are not
  silently interchanged.

Figure-specific gates include:

- Figure 1 locks the host across the campaign and locks source revision, binary,
  and system version within each underlying engine; SemL0 variants therefore
  cannot come from different builds.
- Figure 2 requires strict JSON `feature_switches` and verifies that every
  A0--A6 transition preserves all enabled switches while adding exactly one.
- Figures 2, 3, and 5 lock host, Git revision, and binary across the campaign;
  Figure 6 applies the same lock before splitting E07 and E08.
- Figure 4 excludes an entire run on any digest/mismatch/writer error, requires at
  least three paired repeats across all four policies, and leaves missing windows
  as real line/step gaps without interpolation.
- Figure 5 requires at least five matched repeats for numeric cells, renders
  unsupported (`////`) separately from missing (`xx`), and exits nonzero after
  saving a diagnostic figure if a fallback scenario fails correctness.
- Figure 6 uses actual directed-edge counts, requires at least five runs per
  uncensored point, retains OOM/timeout censoring, and labels a log-log slope only
  with at least four successful scale points and `R^2 >= 0.9`.
