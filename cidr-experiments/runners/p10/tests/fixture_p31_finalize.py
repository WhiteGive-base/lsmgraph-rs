#!/usr/bin/env python3
"""Publish the minimal, explicitly non-performance P31 fixture surface."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    run_dir = Path(sys.argv[1])
    manifest = {
        "schema_version": "fixture-p31-v1",
        "state": "PASS",
        "performance_eligible_declared": False,
        "summary": {
            "resources": {
                "peak_rss_bytes": 1048576,
                "peak_pss_bytes": 786432,
                "process_user_cpu_s": 0.01,
                "process_sys_cpu_s": 0.005,
                "process_read_bytes": 4096,
                "process_write_bytes": 2048,
            },
            "disk": {
                "peak_store_total_bytes": 128,
                "peak_temp_bytes": 0,
                "roots": {},
            },
        },
    }
    (run_dir / "run-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (run_dir / "DONE").write_text(
        json.dumps({"state": "PASS", "fixture_only": True}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
