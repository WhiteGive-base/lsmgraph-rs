#!/usr/bin/env python3
"""Tests for the no-I/O E01 store lifecycle contract."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import build_e01_store_lifecycle_plan as lifecycle


def write_json(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def ref(path: Path) -> dict:
    return {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size_bytes": path.stat().st_size,
    }


class StoreLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source_budg = self.root / "source/budg-b64"
        self.source_naive = self.root / "source/naive"
        self.source_budg.mkdir(parents=True)
        self.source_naive.mkdir(parents=True)
        self.manifest_budg = write_json(self.root / "m/budg.json", {"legacy": True})
        self.manifest_naive = write_json(self.root / "m/naive.json", {"legacy": True})
        self.inventory = write_json(
            self.root / "inventory.json",
            {
                "schema_version": lifecycle.INVENTORY_SCHEMA,
                "stores": [
                    {
                        "variant": "budg-b64",
                        "candidate_root": str(self.source_budg.resolve()),
                        "candidate_tree_sha256": "1" * 64,
                        "recorded_file_count": 2,
                        "recorded_total_bytes": 20,
                        "tree_rehashed_now": False,
                        "manifest": ref(self.manifest_budg),
                    },
                    {
                        "variant": "naive",
                        "candidate_root": str(self.source_naive.resolve()),
                        "candidate_tree_sha256": "2" * 64,
                        "recorded_file_count": 3,
                        "recorded_total_bytes": 30,
                        "tree_rehashed_now": False,
                        "manifest": ref(self.manifest_naive),
                    },
                ],
            },
        )
        self.campaign = self.root / "future-campaign"
        self.command = write_json(
            self.root / "command.json",
            {
                "schema_version": lifecycle.COMMAND_SCHEMA,
                "state": "HOLD",
                "execution_state": "BLOCKED",
                "timing_generated": False,
                "campaign_root": str(self.campaign.resolve()),
                "stores": [
                    {
                        "variant": "budg-b64",
                        "root": str(self.source_budg.resolve()),
                        "tree_sha256": "1" * 64,
                    },
                    {
                        "variant": "naive",
                        "root": str(self.source_naive.resolve()),
                        "tree_sha256": "2" * 64,
                    },
                ],
            },
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build(self) -> dict:
        return lifecycle.build(
            asset_inventory_path=self.inventory,
            command_plan_path=self.command,
            immutable_root=self.root / "future-immutable",
            campaign_root=self.campaign,
        )

    def test_plan_is_hold_and_performs_no_large_io(self) -> None:
        value = lifecycle.validate(self.build())
        self.assertEqual(value["state"], "HOLD")
        self.assertFalse(value["large_content_read_now"])
        self.assertFalse(value["large_copy_performed_now"])
        self.assertFalse(value["source_store_modified_now"])
        self.assertEqual(value["max_live_mutable_clones"], 1)
        self.assertEqual(
            [row["one_time_validation"]["full_content_hash_count"] for row in value["stores"]],
            [1, 1],
        )

    def test_cell_clones_are_reflink_only_and_never_rehash(self) -> None:
        value = self.build()
        self.assertEqual(
            [(row["cell_key"], row["variant"]) for row in value["cells"]],
            list(lifecycle.CELL_ORDER),
        )
        for row in value["cells"]:
            self.assertEqual(row["clone_policy"]["method"], "cp-reflink-always")
            self.assertFalse(row["clone_policy"]["full_copy_fallback"])
            self.assertFalse(row["clone_policy"]["content_rehash_per_cell"])
            self.assertTrue(row["cleanup_policy"]["failed_staging_root_preserved"])
            self.assertTrue(row["cleanup_policy"]["immutable_source_deletion_forbidden"])

    def test_fresh_manifest_requires_store_path_not_legacy_store_root(self) -> None:
        value = self.build()
        self.assertTrue(
            all(
                row["one_time_validation"]["fresh_manifest_root_key"] == "store_path"
                for row in value["stores"]
            )
        )

    def test_rejects_command_store_sha_drift(self) -> None:
        value = json.loads(self.command.read_text(encoding="utf-8"))
        value["stores"][0]["tree_sha256"] = "9" * 64
        write_json(self.command, value)
        with self.assertRaises(lifecycle.LifecycleError):
            self.build()

    def test_rejects_existing_immutable_or_campaign_root(self) -> None:
        immutable = self.root / "future-immutable"
        immutable.mkdir()
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.build(
                asset_inventory_path=self.inventory,
                command_plan_path=self.command,
                immutable_root=immutable,
                campaign_root=self.campaign,
            )
        immutable.rmdir()
        self.campaign.mkdir()
        with self.assertRaises(lifecycle.LifecycleError):
            self.build()

    def test_output_is_exclusive(self) -> None:
        output = self.root / "plan.json"
        value = self.build()
        lifecycle.atomic_write(output, value)
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.atomic_write(output, value)

    def test_real_snapshot_remains_hold_and_does_not_allocate_roots(self) -> None:
        snapshot = ROOT / "E01-store-lifecycle-plan-HOLD-v1.json"
        if not snapshot.exists():
            self.skipTest("real store lifecycle snapshot is not installed")
        value = lifecycle.validate(snapshot)
        self.assertEqual(
            [row["expected_tree_sha256"] for row in value["stores"]],
            [
                "ae77255c03c40d9d7e55071374ab3adc1dc67942f9443ad7d97dacacbe78c8b5",
                "133e2ab535dd2c915d93e6e5ded65295151e199ec7268609f0a3e2f65387ddd2",
            ],
        )
        self.assertEqual(
            sum(row["expected_total_bytes"] for row in value["stores"]),
            28_711_503_691,
        )
        self.assertFalse(Path(value["immutable_asset_root"]).exists())
        self.assertFalse(Path(value["campaign_root"]).exists())


if __name__ == "__main__":
    unittest.main()
