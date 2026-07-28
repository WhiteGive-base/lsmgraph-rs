#!/usr/bin/env python3
"""Negative and state-machine tests for the production backend."""

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

import run_e01_incremental_production as production


def write_json(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


class IncrementalProductionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.campaign = self.root / "campaign"
        self.plan_sha = "a" * 64
        self.plan = {
            "schema_version": production.BACKEND_SCHEMA,
            "state": "HOLD",
            "execution_state": "BLOCKED",
            "synthetic_test_only": False,
            "strict_serial": True,
            "campaign_root": str(self.campaign.resolve()),
            "campaign_gates": {"fresh_resource_gate": None},
            "blockers": ["test HOLD"],
            "cells": [
                {
                    "ordinal": ordinal,
                    "cell_key": key,
                    "final_cell_root": str(
                        (self.campaign / "cells" / f"{ordinal:02d}-{key.replace(':', '-')}").resolve()
                    ),
                    "staging_cell_root": str(
                        (self.campaign / "staging" / f"{ordinal:02d}-{key.replace(':', '-')}").resolve()
                    ),
                    "phase_commands": {phase: None for phase in production.PHASE_ORDER},
                }
                for ordinal, key in enumerate(production.CELL_ORDER, start=1)
            ],
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_receipts(self, staging: Path, key: str, ordinal: int) -> None:
        clone = staging / "mutable-store"
        for role, relative in production.RECEIPT_PATHS.items():
            value = {
                "schema_version": f"cidr-e01-incremental-{role.replace('_', '-')}-receipt-v1",
                "state": "PASS",
                "mode": "synthetic",
                "synthetic_test_only": True,
                "fixture_only": True,
                "cell_key": key,
                "ordinal": ordinal,
                "backend_plan_sha256": self.plan_sha,
            }
            if role == "p31":
                value.update(timing_generated=False, binary_only_boundary=True)
            if role == "correctness":
                value.update(mismatch_queries=0, timeout_queries=0)
            if role == "cleanup":
                value.update(mutable_clone_removed=True, mutable_clone=str(clone.resolve()))
            write_json(staging / relative, value)

    def initialize_root(self) -> None:
        (self.campaign / "cells").mkdir(parents=True)
        (self.campaign / "staging").mkdir()
        write_json(
            self.campaign / "MATRIX-START.json",
            {
                "schema_version": production.START_SCHEMA,
                "state": "PASS",
                "strict_serial": True,
                "backend_plan_sha256": self.plan_sha,
            },
        )

    def test_committed_shape_cannot_execute_while_hold(self) -> None:
        value = production.validate_backend_plan(self.plan)
        self.assertEqual(value["state"], "HOLD")
        path = write_json(self.root / "plan.json", self.plan)
        with self.assertRaises(production.BackendError):
            production.execute_production(path)
        self.assertFalse(self.campaign.exists())

    def test_finalize_requires_cleanup_and_atomically_publishes(self) -> None:
        self.initialize_root()
        row = self.plan["cells"][0]
        staging = Path(row["staging_cell_root"])
        final = Path(row["final_cell_root"])
        staging.mkdir()
        self.make_receipts(staging, row["cell_key"], row["ordinal"])
        done = production.finalize_staging_cell(
            staging,
            final,
            cell_key=row["cell_key"],
            ordinal=row["ordinal"],
            plan_sha=self.plan_sha,
            expected_mode="synthetic",
        )
        self.assertEqual(done["state"], "PASS")
        self.assertFalse(staging.exists())
        self.assertTrue((final / "CELL-DONE.json").is_file())

    def test_finalize_rejects_cleanup_claim_when_clone_exists(self) -> None:
        self.initialize_root()
        row = self.plan["cells"][0]
        staging = Path(row["staging_cell_root"])
        final = Path(row["final_cell_root"])
        staging.mkdir()
        self.make_receipts(staging, row["cell_key"], row["ordinal"])
        (staging / "mutable-store").mkdir()
        with self.assertRaises(production.BackendError):
            production.finalize_staging_cell(
                staging,
                final,
                cell_key=row["cell_key"],
                ordinal=row["ordinal"],
                plan_sha=self.plan_sha,
                expected_mode="synthetic",
            )
        self.assertTrue(staging.exists())
        self.assertFalse(final.exists())

    def test_resume_accepts_only_completed_prefix(self) -> None:
        self.initialize_root()
        for row in self.plan["cells"][:2]:
            staging = Path(row["staging_cell_root"])
            staging.mkdir()
            self.make_receipts(staging, row["cell_key"], row["ordinal"])
            production.finalize_staging_cell(
                staging,
                Path(row["final_cell_root"]),
                cell_key=row["cell_key"],
                ordinal=row["ordinal"],
                plan_sha=self.plan_sha,
                expected_mode="synthetic",
            )
        state = production.inspect_resume_root(
            self.campaign, self.plan, self.plan_sha, expected_mode="synthetic"
        )
        self.assertEqual(state["completed_cells"], 2)
        first = Path(self.plan["cells"][0]["final_cell_root"])
        displaced = self.root / "first-saved"
        first.rename(displaced)
        with self.assertRaises(production.BackendError):
            production.inspect_resume_root(
                self.campaign, self.plan, self.plan_sha, expected_mode="synthetic"
            )

    def test_resume_rejects_and_preserves_failed_staging(self) -> None:
        self.initialize_root()
        staging = Path(self.plan["cells"][0]["staging_cell_root"])
        staging.mkdir()
        write_json(staging / "FAILED.json", {"state": "FAILED"})
        with self.assertRaises(production.BackendError):
            production.inspect_resume_root(
                self.campaign, self.plan, self.plan_sha, expected_mode="synthetic"
            )
        self.assertTrue((staging / "FAILED.json").is_file())

    def test_cell_receipt_sha_drift_blocks_resume(self) -> None:
        self.initialize_root()
        row = self.plan["cells"][0]
        staging = Path(row["staging_cell_root"])
        staging.mkdir()
        self.make_receipts(staging, row["cell_key"], row["ordinal"])
        production.finalize_staging_cell(
            staging,
            Path(row["final_cell_root"]),
            cell_key=row["cell_key"],
            ordinal=row["ordinal"],
            plan_sha=self.plan_sha,
            expected_mode="synthetic",
        )
        receipt = Path(row["final_cell_root"]) / production.RECEIPT_PATHS["correctness"]
        receipt.write_text(receipt.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaises(production.BackendError):
            production.inspect_resume_root(
                self.campaign, self.plan, self.plan_sha, expected_mode="synthetic"
            )

    def test_production_target_backlink_tamper_blocks_final_validation(self) -> None:
        self.initialize_root()
        row = self.plan["cells"][0]
        staging = Path(row["staging_cell_root"])
        final = Path(row["final_cell_root"])
        staging.mkdir()
        self.make_receipts(staging, row["cell_key"], row["ordinal"])
        target_ref = {"path": "/target.json", "sha256": "b" * 64, "size_bytes": 1}
        query_ref = {"path": "/plan.json", "sha256": "c" * 64, "size_bytes": 1}
        lease_ref = {"path": "/lease.json", "sha256": "d" * 64, "size_bytes": 1}
        for role, relative in production.RECEIPT_PATHS.items():
            path = staging / relative
            value = json.loads(path.read_text(encoding="utf-8"))
            value.update(mode="production", synthetic_test_only=False, fixture_only=False)
            if role == "p31":
                value["timing_generated"] = True
            if role in {"store_clone", "p31", "validated_result", "cleanup"}:
                value["target_p02b"] = target_ref
            write_json(path, value)
        production.finalize_staging_cell(
            staging, final, cell_key=row["cell_key"], ordinal=row["ordinal"],
            plan_sha=self.plan_sha, expected_mode="production",
            target_p02b=target_ref, target_query_plan=query_ref, target_lease=lease_ref,
        )
        done_path = final / "CELL-DONE.json"
        done = json.loads(done_path.read_text(encoding="utf-8"))
        done["target_lease"] = {"path": "/wrong", "sha256": "e" * 64, "size_bytes": 1}
        write_json(done_path, done)
        with self.assertRaisesRegex(production.BackendError, "target lease backlink drift"):
            production.validate_final_cell(
                final, cell_key=row["cell_key"], ordinal=row["ordinal"],
                plan_sha=self.plan_sha, expected_mode="production",
                target_p02b=target_ref, target_query_plan=query_ref, target_lease=lease_ref,
            )


if __name__ == "__main__":
    unittest.main()
