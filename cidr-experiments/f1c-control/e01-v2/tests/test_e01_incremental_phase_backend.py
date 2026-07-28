#!/usr/bin/env python3
"""Light tests for receipt-bound phase execution and backend blocking."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    value = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(value)
    return value


phase = module("phase", "execute_e01_incremental_cell_phase.py")
builder = module("builder", "build_e01_incremental_ready_backend.py")


def write_json(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


class PhaseBackendTests(unittest.TestCase):
    def test_metadata_manifest_does_not_read_content_and_detects_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a").mkdir()
            (root / "a" / "x").write_bytes(b"abc")
            first = phase.metadata_manifest(root)
            self.assertFalse(first["content_hashed"])
            self.assertEqual(first["file_count"], 1)
            (root / "a" / "x").write_bytes(b"xyz")
            second = phase.metadata_manifest(root)
            self.assertEqual(first, second)
            (root / "a" / "x").write_bytes(b"longer")
            self.assertNotEqual(first["sha256"], phase.metadata_manifest(root)["sha256"])

    def test_exact_cleanup_rejects_inode_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            target = parent / "clone"
            target.mkdir()
            stat = target.stat()
            with self.assertRaises(phase.PhaseError):
                phase.exact_cleanup(target, parent, stat.st_dev, stat.st_ino + 1)
            self.assertTrue(target.exists())

    def test_clone_dry_run_copies_verifies_and_removes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            output = root / "dryrun"
            source.mkdir()
            (source / "x").write_bytes(b"abc")
            plan_path = root / "plan.json"
            plan = {
                "schema_version": phase.PLAN_SCHEMA,
                "synthetic_test_only": False,
                "cells": [{
                    "cell_key": "seml0:bridge-canary",
                    "runtime": {"clone_policy": {
                        "source_root": str(source),
                        "tree_sha256": "a" * 64,
                        "copy_argv": ["/bin/cp", "--archive", "--", "{SOURCE}", "{TARGET}"],
                    }},
                }],
            }
            write_json(plan_path, plan)
            value = phase.load_json(plan_path, "plan")
            phase.clone_dry_run(value, value["cells"][0], phase.sha256_file(plan_path), output)
            receipt = phase.load_json(output / "CLONE-DRYRUN.json", "receipt")
            self.assertEqual(receipt["state"], "PASS")
            self.assertFalse((output / "mutable-store").exists())
            self.assertFalse(receipt["performance_eligible"])

    def test_builder_validator_requires_hold_commands_null(self) -> None:
        value = {
            "schema_version": builder.SCHEMA,
            "state": "HOLD",
            "execution_state": "BLOCKED",
            "synthetic_test_only": False,
            "strict_serial": True,
            "large_content_rehashed_now": False,
            "adapter_invoked": False,
            "timing_generated": False,
            "blockers": ["P02B binding mismatch"],
            "cells": [
                {
                    "cell_key": key,
                    "phase_commands": {phase_name: None for phase_name in builder.PHASES},
                }
                for key in builder.CELL_ORDER
            ],
            **builder.FALSE_ELIGIBILITY,
        }
        builder.validate(value)
        value["cells"][0]["phase_commands"]["prepare"] = ["/bin/false"]
        with self.assertRaises(builder.BuildError):
            builder.validate(value)


if __name__ == "__main__":
    unittest.main()
