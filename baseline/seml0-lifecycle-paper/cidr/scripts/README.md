# CIDR Figure Scripts

Each figure has an individual standalone script. The `plot_fig*.py` files each
contain their own data, styling, plotting code, and save helpers, so they can
be run independently without importing `plot_cidr_figures.py`.

Run all figures:

```bash
python3 plot_cidr_figures.py
```

Run one figure:

```bash
python3 plot_fig1_system_control_plane.py
python3 plot_fig2_semantic_evidence_lifecycle.py
python3 plot_fig2_sf100_read_budget.py
python3 plot_fig3_c2_lifecycle_retention.py
python3 plot_fig4_dynamic_sf30_p99.py
python3 plot_fig5_baseline_positioning.py
```

Linux generation path:

```text
/data/WorkSpace/lsmgraph-rs-cidr-paper/baseline/seml0-lifecycle-paper/cidr
```

Outputs are written to `../images` as matching PDF, SVG, and PNG files. The
orchestrator contains no plotting data or duplicate figure logic; it invokes
the standalone generators in a fixed order. LaTeX consumes PDF, while the
Chinese reading edition embeds PNG.

The detailed evidence-lifecycle, dynamic SF30, and baseline-positioning figures
are artifact-only material. The six-page English paper and its one-to-one
Chinese translation both keep the merged control-plane, budget/resource, and
C2 figures.
