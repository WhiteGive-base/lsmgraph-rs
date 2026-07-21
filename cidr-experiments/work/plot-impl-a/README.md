# Figure 1--3 plotting implementation

The scripts consume wide tidy TSV rows governed by
`../figure-design/FIGURE-DATA-REQUIREMENTS.tsv`. They do not contain result
constants and refuse to render rows that fail the digest/mismatch gate.

```text
python plot_figure1_end_to_end.py --input E01-results.tsv --out-dir output
python plot_figure2_component_ablation.py --input E03-results.tsv --out-dir output
python plot_figure3_resource_pareto.py --input E04-results.tsv --out-dir output
```

Each successful command uses `../../figures/scripts/figure_common.py` and emits
PNG, SVG, PDF, and a `.layout.json` QA report. Figure 3 defaults to the
`warm_read` phase and the `uniform` workload for its persistent-resource
breakdown; those selectors and the separate 5-run performance/3-run resource
minimums are explicit CLI options.

Validation is intentionally strict:

- all `required=yes` COMMON and figure-specific contract columns must exist;
- the requested experiment ID must be present;
- at least one row must have `digest_pass=true` and `mismatch_count=0`;
- every plotted comparison cell must meet its configured independent-run minimum;
- matched traces, data, cache state, and concurrency must agree;
- missing, zero, timeout, unsupported, proxy, and full-execution values are not
  silently interchanged.
