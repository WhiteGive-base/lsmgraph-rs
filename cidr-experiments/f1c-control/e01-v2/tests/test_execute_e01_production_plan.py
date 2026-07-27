#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


HERE = Path(__file__).resolve()
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

EXECUTOR_SPEC = importlib.util.spec_from_file_location(
    "execute_e01_production_plan",
    ROOT / "execute_e01_production_plan.py",
)
assert EXECUTOR_SPEC is not None and EXECUTOR_SPEC.loader is not None
executor = importlib.util.module_from_spec(EXECUTOR_SPEC)
EXECUTOR_SPEC.loader.exec_module(executor)

PLAN_TEST_SPEC = importlib.util.spec_from_file_location(
    "e01_plan_tests_for_executor",
    HERE.with_name("test_e01_production_command_plan.py"),
)
assert PLAN_TEST_SPEC is not None and PLAN_TEST_SPEC.loader is not None
plan_tests = importlib.util.module_from_spec(PLAN_TEST_SPEC)
PLAN_TEST_SPEC.loader.exec_module(plan_tests)


FALSE = {
    "formal_eligible": False,
    "performance_eligible": False,
    "paper_claim_eligible": False,
}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


class E01ProductionExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = plan_tests.E01ProductionCommandPlanTests(
            "test_valid_plan_has_21_explicit_argv_and_creates_no_campaign_root"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.plan = self.fixture.build()
        self.plan_path = self.fixture.output
        self.manifest = self.fixture.manifest
        self.manifest_path = self.fixture.manifest_path
        self.manifest_sha = executor.sha256_file(self.manifest_path)
        self.plan_sha = executor.sha256_file(self.plan_path)
        self.root = self.fixture.campaign_root
        self.run = self.manifest["runs"][0]

    def test_preflight_reports_all_static_execution_blockers_pre_root(self) -> None:
        result = executor.production_preflight(
            self.manifest_path, self.plan_path, self.root
        )
        self.assertEqual(result["state"], "BLOCKED")
        self.assertEqual(result["resume"], {"state": "NEW", "completed_cells": 0})
        self.assertIn(
            "command_plan.execution_state=NOT_IMPLEMENTED", result["blockers"]
        )
        self.assertIn(
            "admission.production_executor_state=NOT_IMPLEMENTED",
            result["blockers"],
        )
        self.assertIn(
            "adapter_identity.execution_state=NOT_IMPLEMENTED",
            result["blockers"],
        )
        self.assertFalse(result["adapter_invoked"])
        self.assertFalse(result["timing_generated"])
        self.assertFalse(self.root.exists())

    def test_execute_and_scheduler_route_refuse_before_root_creation(self) -> None:
        with self.assertRaises(executor.ProductionBlocked):
            executor.execute_production_plan(
                self.manifest_path, self.plan_path, self.root
            )
        self.assertFalse(self.root.exists())
        with self.assertRaises(executor.scheduler.ContractError):
            executor.scheduler.run_matrix(
                self.manifest_path,
                self.root,
                synthetic_test_mode=False,
                production_command_plan=self.plan_path,
            )
        self.assertFalse(self.root.exists())

    def test_production_entry_requires_command_plan_pre_root(self) -> None:
        with self.assertRaises(executor.scheduler.ContractError):
            executor.scheduler.run_matrix(
                self.manifest_path,
                self.root,
                synthetic_test_mode=False,
            )
        self.assertFalse(self.root.exists())

    def test_existing_empty_resume_root_is_rejected_without_mutation(self) -> None:
        self.root.mkdir()
        marker = self.root / "preserve.txt"
        marker.write_text("preserve\n", encoding="utf-8")
        with self.assertRaises(executor.ProductionError):
            executor.production_preflight(
                self.manifest_path, self.plan_path, self.root
            )
        self.assertEqual(marker.read_text(encoding="utf-8"), "preserve\n")

    def test_failed_root_is_immutable_and_preserved(self) -> None:
        self.root.mkdir()
        marker = self.root / "MATRIX-FAILED.json"
        marker.write_text('{"state":"FAIL"}\n', encoding="utf-8")
        before = marker.read_bytes()
        with self.assertRaisesRegex(
            executor.ProductionError, "failed production root"
        ):
            executor.production_preflight(
                self.manifest_path, self.plan_path, self.root
            )
        self.assertEqual(marker.read_bytes(), before)

    def make_staging_cell(self) -> tuple[Path, Path]:
        cells = self.root / "cells"
        cells.mkdir(parents=True, exist_ok=True)
        final = Path(self.plan["cells"][0]["cell_root"])
        staging = cells / f".{final.name}.tmp-unit"
        staging.mkdir()
        write_json(
            staging / "validated-result.json",
            {
                "schema_version": "cidr-e01-production-cell-result-v1",
                "state": "PASS",
                "mode": "production",
                "synthetic_test_only": False,
                "adapter_invoked": True,
                "timing_generated": True,
                "run_key": self.run["run_key"],
                "ordinal": self.run["ordinal"],
                "launch_manifest_sha256": self.manifest_sha,
                **FALSE,
            },
        )
        for role in executor.evidence.ROLES:
            write_json(
                staging / "source" / f"{role}.json",
                {
                    "schema_version": f"cidr-e01-{role}-source-evidence-test-v1",
                    "state": "PASS",
                    "structural_test_only": True,
                    **FALSE,
                },
            )
        return staging, final

    def produce_valid_receipts(
        self, staging: Path, *, include_cleanup: bool = True
    ) -> None:
        common = {
            "cell_root": staging,
            "run_key": self.run["run_key"],
            "ordinal": self.run["ordinal"],
            "manifest_sha": self.manifest_sha,
        }
        source = staging / "source"
        executor.produce_command_receipt(
            source_evidence=source / "command.json",
            returncode=0,
            adapter_invoked=True,
            timing_generated=True,
            **common,
        )
        executor.produce_adapter_receipt(
            source_evidence=source / "adapter.json",
            adapter_invoked=True,
            **common,
        )
        executor.produce_p31_receipt(
            source_evidence=source / "p31.json",
            resource_validation_pass=True,
            timing_generated=True,
            **common,
        )
        executor.produce_correctness_receipt(
            source_evidence=source / "correctness.json",
            mismatch_count=0,
            **common,
        )
        executor.produce_fairness_receipt(
            source_evidence=source / "fairness.json",
            fairness_pass=True,
            strict_serial=True,
            **common,
        )
        executor.produce_cgroup_receipt(
            source_evidence=source / "cgroup.json",
            allocation_pass=True,
            cpuset="2-7",
            **common,
        )
        if include_cleanup:
            executor.produce_cleanup_receipt(
                source_evidence=source / "cleanup.json",
                cleanup_pass=True,
                residual_processes=0,
                **common,
            )

    def test_seven_receipt_interfaces_and_atomic_cell_done(self) -> None:
        staging, final = self.make_staging_cell()
        self.produce_valid_receipts(staging)
        validation = executor.finalize_production_cell(
            staging,
            final,
            expected_run=self.run,
            manifest_sha=self.manifest_sha,
        )
        self.assertEqual(validation["state"], "PASS")
        self.assertEqual(validation["mode"], "production")
        self.assertFalse(staging.exists())
        self.assertTrue((final / "CELL-DONE.json").is_file())
        self.assertEqual(
            {path.stem for path in (final / "receipts").glob("*.json")},
            set(executor.evidence.ROLES),
        )

    def test_receipt_overwrite_is_rejected_and_original_preserved(self) -> None:
        staging, _ = self.make_staging_cell()
        source = staging / "source" / "command.json"
        kwargs = {
            "cell_root": staging,
            "source_evidence": source,
            "run_key": self.run["run_key"],
            "ordinal": self.run["ordinal"],
            "manifest_sha": self.manifest_sha,
            "returncode": 0,
            "adapter_invoked": True,
            "timing_generated": True,
        }
        executor.produce_command_receipt(**kwargs)
        receipt = staging / "receipts" / "command.json"
        before = receipt.read_bytes()
        with self.assertRaises(executor.ProductionError):
            executor.produce_command_receipt(**kwargs)
        self.assertEqual(receipt.read_bytes(), before)

    def test_cleanup_failure_blocks_cell_done_and_preserves_staging(self) -> None:
        staging, final = self.make_staging_cell()
        self.produce_valid_receipts(staging, include_cleanup=False)
        bad_cleanup = executor._receipt_common(
            role="cleanup",
            cell_root=staging,
            source_evidence=staging / "source" / "cleanup.json",
            run_key=self.run["run_key"],
            ordinal=self.run["ordinal"],
            manifest_sha=self.manifest_sha,
        )
        bad_cleanup.update({"cleanup_pass": False, "residual_processes": 1})
        executor.atomic_json_exclusive(
            staging / "receipts" / "cleanup.json", bad_cleanup
        )
        with self.assertRaises(
            (executor.ProductionError, executor.evidence.EvidenceError)
        ):
            executor.finalize_production_cell(
                staging,
                final,
                expected_run=self.run,
                manifest_sha=self.manifest_sha,
            )
        self.assertTrue(staging.is_dir())
        self.assertFalse(final.exists())
        self.assertFalse((staging / "CELL-DONE.json").exists())
        self.assertTrue((staging / "receipts" / "cleanup.json").is_file())

    def test_failed_staging_root_is_preserved_without_cell_done(self) -> None:
        staging, final = self.make_staging_cell()
        failed = staging / "FAILED.json"
        failed.write_text('{"state":"FAIL"}\n', encoding="utf-8")
        before = failed.read_bytes()
        with self.assertRaisesRegex(
            executor.ProductionError, "failed staging root"
        ):
            executor.finalize_production_cell(
                staging,
                final,
                expected_run=self.run,
                manifest_sha=self.manifest_sha,
            )
        self.assertEqual(failed.read_bytes(), before)
        self.assertTrue(staging.exists())
        self.assertFalse(final.exists())
        self.assertFalse((staging / "CELL-DONE.json").exists())

    def test_resume_accepts_only_atomic_completed_cell_and_rejects_residue(
        self,
    ) -> None:
        staging, final = self.make_staging_cell()
        self.produce_valid_receipts(staging)
        executor.finalize_production_cell(
            staging,
            final,
            expected_run=self.run,
            manifest_sha=self.manifest_sha,
        )
        write_json(
            self.root / "MATRIX-START.json",
            {
                "schema_version": executor.MATRIX_START_SCHEMA,
                "state": "PASS",
                "mode": "production",
                "synthetic_test_only": False,
                "strict_serial": True,
                "launch_manifest_sha256": self.manifest_sha,
                "production_command_plan_sha256": self.plan_sha,
                **FALSE,
            },
        )
        resume = executor.inspect_resume_root(
            self.root,
            manifest=self.manifest,
            manifest_sha=self.manifest_sha,
            plan=self.plan,
            plan_sha=self.plan_sha,
        )
        self.assertEqual(resume, {"state": "RESUME", "completed_cells": 1})
        residue = self.root / "cells" / ".002-seml0-r2.tmp-crash"
        residue.mkdir()
        marker = residue / "preserve.txt"
        marker.write_text("preserve\n", encoding="utf-8")
        with self.assertRaisesRegex(
            executor.ProductionError, "unknown/incomplete"
        ):
            executor.inspect_resume_root(
                self.root,
                manifest=self.manifest,
                manifest_sha=self.manifest_sha,
                plan=self.plan,
                plan_sha=self.plan_sha,
            )
        self.assertEqual(marker.read_text(encoding="utf-8"), "preserve\n")


if __name__ == "__main__":
    unittest.main()
