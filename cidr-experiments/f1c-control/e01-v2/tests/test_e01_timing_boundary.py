#!/usr/bin/env python3
"""Tests for the exact P31/no-repeat-hash HOLD contract."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import build_e01_timing_boundary_plan as boundary


def write_json(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


class TimingBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.campaign = self.root / "future-campaign"
        cells = []
        lifecycle_cells = []
        keys = (
            ("seml0:bridge-canary", "budg-b64"),
            ("seml0-naive:r1", "naive"),
            ("seml0-naive:r2", "naive"),
            ("seml0-naive:r3", "naive"),
        )
        for ordinal, (key, variant) in enumerate(keys, start=1):
            cell_root = self.campaign / "cells" / f"{ordinal:02d}-{key.replace(':', '-')}"
            cells.append(
                {
                    "ordinal": ordinal,
                    "cell_key": key,
                    "variant": variant,
                    "cell_root": str(cell_root.resolve()),
                }
            )
            lifecycle_cells.append(
                {
                    "ordinal": ordinal,
                    "cell_key": key,
                    "variant": variant,
                    "mutable_clone": str((cell_root / "mutable-store").resolve()),
                }
            )
        self.command = write_json(
            self.root / "command.json",
            {
                "schema_version": boundary.COMMAND_SCHEMA,
                "state": "HOLD",
                "execution_state": "BLOCKED",
                "adapter_invoked": False,
                "timing_generated": False,
                "campaign_root": str(self.campaign.resolve()),
                "cells": cells,
            },
        )
        self.lifecycle = write_json(
            self.root / "lifecycle.json",
            {
                "schema_version": boundary.LIFECYCLE_SCHEMA,
                "state": "HOLD",
                "strict_serial": True,
                "campaign_root": str(self.campaign.resolve()),
                "cells": lifecycle_cells,
            },
        )
        self.bridge = self.root / "bridge.sh"
        self.wrapper = self.root / "wrapper.sh"
        for path in (self.bridge, self.wrapper):
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(0o755)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build(self) -> dict:
        return boundary.build(
            command_plan_path=self.command,
            store_lifecycle_path=self.lifecycle,
            p31_bridge_path=self.bridge,
            p31_wrapper_path=self.wrapper,
        )

    def test_only_binary_process_is_inside_p31(self) -> None:
        value = boundary.validate(self.build())
        self.assertTrue(value["global_boundary"]["adapter_monolithic_formal_path_forbidden"])
        for row in value["cells"]:
            timing = row["timing"]
            self.assertEqual(timing["process_scope"], "lsmgraph-storage-bench-only")
            self.assertFalse(timing["python_adapter_process_inside_boundary"])
            self.assertFalse(timing["result_conversion_inside_boundary"])
            self.assertFalse(timing["correctness_validation_inside_boundary"])

    def test_per_cell_large_hash_count_is_zero(self) -> None:
        value = self.build()
        for row in value["cells"]:
            self.assertEqual(set(row["large_content_hashes_per_cell"].values()), {0})
            self.assertFalse(row["pre_timing"]["dataset_content_hash"])
            self.assertFalse(row["pre_timing"]["store_content_hash"])
            self.assertFalse(row["pre_timing"]["id_map_content_hash"])
            self.assertEqual(row["timing"]["dataset_sha256_mode"], "declared-no-read-v1")

    def test_plan_stays_hold_until_split_backend_exists(self) -> None:
        value = self.build()
        self.assertEqual(value["execution_state"], "NOT_IMPLEMENTED")
        self.assertEqual(
            set(value["required_backend_interfaces"].values()), {"NOT_IMPLEMENTED"}
        )
        self.assertTrue(all(row["timing"]["binary_argv"] is None for row in value["cells"]))
        self.assertFalse(value["adapter_invoked"])
        self.assertFalse(value["timing_generated"])

    def test_rejects_command_lifecycle_campaign_drift(self) -> None:
        value = json.loads(self.lifecycle.read_text(encoding="utf-8"))
        value["campaign_root"] = str((self.root / "other").resolve())
        write_json(self.lifecycle, value)
        with self.assertRaises(boundary.BoundaryError):
            self.build()

    def test_rejects_non_executable_p31_component(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX executable-bit contract")
        self.wrapper.chmod(0o644)
        with self.assertRaises(boundary.BoundaryError):
            self.build()

    def test_output_is_exclusive(self) -> None:
        output = self.root / "boundary.json"
        value = self.build()
        boundary.atomic_write(output, value)
        with self.assertRaises(boundary.BoundaryError):
            boundary.atomic_write(output, value)

    def test_real_snapshot_is_pre_root_hold(self) -> None:
        snapshot = ROOT / "E01-exact-timing-boundary-plan-HOLD-v1.json"
        if not snapshot.exists():
            self.skipTest("real timing-boundary snapshot is not installed")
        value = boundary.validate(snapshot)
        self.assertEqual(value["state"], "HOLD")
        self.assertEqual(value["execution_state"], "NOT_IMPLEMENTED")
        self.assertTrue(
            all(
                row["timing"]["process_scope"] == "lsmgraph-storage-bench-only"
                for row in value["cells"]
            )
        )
        self.assertTrue(
            all(
                sum(row["large_content_hashes_per_cell"].values()) == 0
                for row in value["cells"]
            )
        )
        self.assertFalse(Path(value["campaign_root"]).exists())


if __name__ == "__main__":
    unittest.main()
