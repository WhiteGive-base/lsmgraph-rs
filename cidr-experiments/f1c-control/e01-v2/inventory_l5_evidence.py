#!/usr/bin/env python3
"""Build a read-only inventory of the legacy L5 18-cell conditional fixture.

The inventory is intentionally separate from E01 normalization.  It records
lineage and missing artifacts, but never changes eligibility or emits tidy
Figure 1 rows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Sequence


EXPECTED_SCHEMA = "cidr-l5-conditional-split-lineage-composition-v1"
EXPECTED_SYSTEMS = ("aster", "livegraph", "nebulagraph", "neo4j", "seml0", "tugraph")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inventory(source: Path) -> Dict[str, Any]:
    source = source.resolve()
    if not source.is_file() or source.is_symlink():
        raise RuntimeError(f"source must be a regular file: {source}")
    value = json.loads(source.read_text(encoding="utf-8"))
    if value.get("schema_version") != EXPECTED_SCHEMA:
        raise RuntimeError("source is not the frozen L5 conditional composition")
    if value.get("state") != "PASS" or value.get("repeat_count") != 18:
        raise RuntimeError("source does not describe the expected 18-cell PASS fixture")
    if any(value.get(name) is not False for name in (
        "formal_eligible",
        "performance_eligible",
        "paper_claim_eligible",
    )):
        raise RuntimeError("conditional source eligibility drifted from false")
    repeats = value.get("repeats")
    if not isinstance(repeats, list) or len(repeats) != 18:
        raise RuntimeError("conditional source repeat list is not exactly 18 cells")
    cells = []
    seen = set()
    for index, repeat in enumerate(repeats):
        if not isinstance(repeat, dict):
            raise RuntimeError(f"repeat {index}: object required")
        system_id = repeat.get("system_id")
        repeat_index = repeat.get("repeat_index")
        key = (system_id, repeat_index)
        if key in seen:
            raise RuntimeError(f"duplicate conditional cell: {key}")
        seen.add(key)
        validated = repeat.get("validated_result")
        p31 = repeat.get("p31_validation")
        done = repeat.get("p31_done")
        failed = repeat.get("source_root_failed_marker_preserved")
        cells.append(
            {
                "cell_key": f"{system_id}:r{repeat_index}",
                "system_id": system_id,
                "repeat_index": repeat_index,
                "lineage": {
                    "validated_result": validated,
                    "p31_validation": p31,
                    "p31_done": done,
                    "source_failed_marker_preserved": failed,
                },
                "eligibility": {
                    "formal_eligible": False,
                    "performance_eligible": False,
                    "paper_claim_eligible": False,
                    "conditional_fixture_only": True,
                },
            }
        )
    systems = sorted({cell["system_id"] for cell in cells})
    if tuple(systems) != EXPECTED_SYSTEMS:
        raise RuntimeError(f"unexpected conditional systems: {systems}")
    return {
        "schema_version": "cidr-e01-existing-evidence-inventory-v1",
        "state": "PASS",
        "purpose": "conditional_fixture_only",
        "source": {
            "path": str(source),
            "sha256": sha256(source),
            "size_bytes": source.stat().st_size,
            "schema_version": value["schema_version"],
            "state": value["state"],
        },
        "observed": {
            "system_count": len(systems),
            "systems": systems,
            "repeat_count": len(cells),
            "cells": cells,
        },
        "formal_e01_gap": {
            "required_system_count": 7,
            "required_repeat_count": 21,
            "missing_systems": ["seml0-naive"],
            "missing_run_keys": ["seml0-naive:r1", "seml0-naive:r2", "seml0-naive:r3"],
            "old_cells_may_be_used_as_fixture": True,
            "old_cells_may_be_mixed_into_formal": False,
        },
        "eligibility": {
            "formal_eligible": False,
            "performance_eligible": False,
            "paper_claim_eligible": False,
            "conditional_execution_may_proceed": True,
            "formal_upgrade_performed": False,
        },
        "checks": {
            "source_sha_recorded": True,
            "source_failed_markers_preserved": value.get(
                "source_failed_markers_preserved"
            )
            is True,
            "failed_marker_reference_count": sum(
                cell["lineage"]["source_failed_marker_preserved"] is not None
                for cell in cells
            ),
            "selective_18_plus_3_merge": "FORBIDDEN",
            "normalizer_input": "NOT_ACCEPTED",
        },
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    default_source = here.parents[1] / "l6-control" / "sample-l5" / "L5-CONDITIONAL-COMBINED.json"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=default_source)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        value = inventory(args.source)
        payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
        if args.output is None:
            print(payload, end="")
            return 0
        output = args.output.resolve()
        if output.exists():
            raise RuntimeError(f"refusing to overwrite inventory: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
        print(json.dumps({"state": "PASS", "output": str(output)}, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
