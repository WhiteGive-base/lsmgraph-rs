#!/usr/bin/env python3
"""Negative and state-machine tests for the production backend."""

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

import run_e01_incremental_production as production
import build_e01_incremental_ready_backend as builder


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
        executor = self.root / "phase-executor.py"
        executor.write_text("#!/usr/bin/python3\n", encoding="utf-8")
        self.executor_ref = {
            "path": str(executor.resolve()),
            "sha256": production.sha256_file(executor),
            "size_bytes": executor.stat().st_size,
        }
        self.plan = {
            "schema_version": production.BACKEND_SCHEMA,
            "state": "HOLD",
            "execution_state": "BLOCKED",
            "synthetic_test_only": False,
            "strict_serial": True,
            "campaign_root": str(self.campaign.resolve()),
            "campaign_gates": {"fresh_resource_gate": None},
            "phase_executor": self.executor_ref,
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

    def test_old_v1_schema_and_phase_executor_ref_drift_are_rejected(self) -> None:
        old = json.loads(json.dumps(self.plan))
        old["schema_version"] = "cidr-e01-incremental-backend-plan-v1"
        with self.assertRaisesRegex(production.BackendError, "schema drift"):
            production.validate_backend_plan(old)
        drift = json.loads(json.dumps(self.plan))
        drift["phase_executor"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(production.BackendError, "path/size/SHA drift"):
            production.validate_backend_plan(drift)

    def test_phase_command_key_order_is_irrelevant_but_key_drift_rejected(self) -> None:
        serialized = write_json(self.root / "hold.json", self.plan)
        loaded = json.loads(serialized.read_text(encoding="utf-8"))
        self.assertEqual(
            tuple(loaded["cells"][0]["phase_commands"]),
            ("cleanup", "finalize", "p31", "prepare"),
        )
        production.validate_backend_plan(serialized)
        loaded["cells"][0]["phase_commands"]["extra"] = None
        with self.assertRaisesRegex(production.BackendError, "phase key set drift"):
            production.validate_backend_plan(loaded)

    def test_builder_serialized_v3_ready_plan_dispatches_exact_four_phases(self) -> None:
        query_ref = {"path": "/fixture/query.json", "sha256": "1" * 64, "size_bytes": 1}
        lease_ref = {"path": "/fixture/lease.json", "sha256": "2" * 64, "size_bytes": 1}
        targets = {}
        for variant in {"budg-b64", "naive"}:
            path = write_json(
                self.root / f"{variant}.target.json",
                {
                    "schema_version": production.TARGET_P02B_SCHEMA,
                    "state": "PASS",
                    "variant": variant,
                    "static_inputs": {"query_plan": query_ref},
                    "lease": lease_ref,
                },
            )
            targets[variant] = {
                "path": str(path.resolve()),
                "sha256": production.sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        plan_path = self.root / "ready-v3.json"
        gate_path = self.root / "ready-v3.ARMING-GATE.json"
        ready = {
            "schema_version": production.BACKEND_SCHEMA,
            "state": "READY",
            "execution_state": "READY",
            "synthetic_test_only": False,
            "strict_serial": True,
            "campaign_root": str(self.campaign.resolve()),
            "campaign_gates": {"backend_arming": {"path": str(gate_path.resolve())}},
            "phase_executor": self.executor_ref,
            "blockers": [],
            "cells": [],
        }
        variants = ("budg-b64", "naive", "naive", "naive")
        for ordinal, (key, variant) in enumerate(zip(production.CELL_ORDER, variants), start=1):
            staging = self.campaign / "staging" / f"{ordinal:02d}-{key.replace(':', '-')}"
            final = self.campaign / "cells" / f"{ordinal:02d}-{key.replace(':', '-')}"
            commands = {
                phase_name: [
                    "/usr/bin/python3", "-B", self.executor_ref["path"],
                    "--backend-plan", str(plan_path.resolve()),
                    "--cell-key", key, "--phase", phase_name,
                ]
                for phase_name in production.PHASE_ORDER
            }
            ready["cells"].append(
                {
                    "ordinal": ordinal,
                    "cell_key": key,
                    "staging_cell_root": str(staging.resolve()),
                    "final_cell_root": str(final.resolve()),
                    "phase_commands": commands,
                    "runtime": {
                        "variant": variant,
                        "target_p02b": targets[variant],
                        "target_query_plan": query_ref,
                        "target_lease": lease_ref,
                    },
                }
            )
        builder.atomic(plan_path, ready)
        plan_ref = production.external_file_ref(plan_path)
        write_json(
            gate_path,
            {
                "state": "PASS",
                "synthetic_test_only": False,
                "fixture_only": False,
                "phase_executor": self.executor_ref,
                "backend_plan": plan_ref,
            },
        )
        reloaded = production.validate_backend_plan(plan_path)
        self.assertEqual(reloaded["schema_version"], production.BACKEND_SCHEMA)
        phase_drift = json.loads(json.dumps(reloaded))
        phase_drift["cells"][0]["phase_commands"]["prepare"][2] = "/bin/false"
        with self.assertRaisesRegex(production.BackendError, "dispatch drift"):
            production.validate_backend_plan(phase_drift)
        gate_value = json.loads(gate_path.read_text(encoding="utf-8"))
        gate_drift = json.loads(json.dumps(gate_value))
        gate_drift["backend_plan"]["sha256"] = "0" * 64
        write_json(gate_path, gate_drift)
        with self.assertRaisesRegex(production.BackendError, "backend plan backlink drift"):
            production.validate_backend_plan(plan_path)
        write_json(gate_path, gate_value)
        plan_sha = production.sha256_file(plan_path)
        dispatched = []
        by_staging = {Path(row["staging_cell_root"]): row for row in ready["cells"]}

        def fake_runner(argv, cwd, stdout, stderr):
            row = by_staging[cwd]
            phase_name = argv[-1]
            dispatched.append((row["cell_key"], phase_name))
            base = {
                "state": "PASS",
                "mode": "production",
                "synthetic_test_only": False,
                "fixture_only": False,
                "cell_key": row["cell_key"],
                "ordinal": row["ordinal"],
                "backend_plan_sha256": plan_sha,
            }
            roles = {
                "prepare": ("prepared_command", "store_clone"),
                "p31": ("p31",),
                "finalize": ("validated_result", "correctness", "fairness"),
                "cleanup": ("cleanup",),
            }[phase_name]
            for role in roles:
                value = {
                    **base,
                    "schema_version": (
                        f"cidr-e01-incremental-{role.replace('_', '-')}-receipt-v1"
                    ),
                }
                if role in {"store_clone", "p31", "validated_result", "cleanup"}:
                    value["target_p02b"] = row["runtime"]["target_p02b"]
                if role == "p31":
                    value.update(timing_generated=True, binary_only_boundary=True)
                if role == "correctness":
                    value.update(mismatch_queries=0, timeout_queries=0)
                if role == "cleanup":
                    value.update(
                        mutable_clone_removed=True,
                        mutable_clone=str((cwd / "mutable-store").resolve()),
                    )
                write_json(cwd / production.RECEIPT_PATHS[role], value)
            stdout.write_text("", encoding="utf-8")
            stderr.write_text("", encoding="utf-8")
            return 0

        done = production.execute_production(plan_path, runner=fake_runner)
        self.assertEqual(done["state"], "PASS")
        self.assertEqual(
            dispatched,
            [
                (key, phase_name)
                for key in production.CELL_ORDER
                for phase_name in production.PHASE_ORDER
            ],
        )
        self.assertEqual(len(dispatched), 16)

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

    def test_cleanup_admission_rejects_dangling_clone_symlink(self) -> None:
        self.initialize_root()
        row = self.plan["cells"][0]
        staging = Path(row["staging_cell_root"])
        final = Path(row["final_cell_root"])
        staging.mkdir()
        self.make_receipts(staging, row["cell_key"], row["ordinal"])
        (staging / "mutable-store").symlink_to(staging / "missing", target_is_directory=True)
        with self.assertRaisesRegex(production.BackendError, "lexists"):
            production.finalize_staging_cell(
                staging, final, cell_key=row["cell_key"], ordinal=row["ordinal"],
                plan_sha=self.plan_sha, expected_mode="synthetic",
            )

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
