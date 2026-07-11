#!/usr/bin/env python3
"""Regenerate every canonical CIDR figure through its standalone generator.

Run from anywhere:

    python baseline/seml0-lifecycle-paper/cidr/scripts/plot_cidr_figures.py

Each generator writes matching PDF, SVG, and PNG files. Keeping this entry point
as a thin orchestrator prevents figure logic and embedded measurements from
drifting between two implementations.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUT_DIR = ROOT / "images"

GENERATORS = (
    "plot_fig1_system_control_plane.py",
    "plot_fig2_semantic_evidence_lifecycle.py",
    "plot_fig2_sf100_read_budget.py",
    "plot_fig3_c2_lifecycle_retention.py",
    "plot_fig4_dynamic_sf30_p99.py",
    "plot_fig5_baseline_positioning.py",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate all canonical CIDR figures.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    out_dir = args.out_dir.resolve()

    for generator in GENERATORS:
        subprocess.run(
            [sys.executable, str(SCRIPT_DIR / generator), "--out-dir", str(out_dir)],
            check=True,
        )

    print(f"Wrote canonical PDF/SVG/PNG figures to {out_dir}")


if __name__ == "__main__":
    main()
