#!/usr/bin/env python3
from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve()
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

LAUNCHER_SPEC = importlib.util.spec_from_file_location(
    "run_e01_formal_matrix", ROOT / "run_e01_formal_matrix.py"
)
assert LAUNCHER_SPEC is not None and LAUNCHER_SPEC.loader is not None
launcher = importlib.util.module_from_spec(LAUNCHER_SPEC)
LAUNCHER_SPEC.loader.exec_module(launcher)

BUILDER_TEST_SPEC = importlib.util.spec_from_file_location(
    "e01_builder_tests", HERE.with_name("test_build_e01_formal_manifest.py")
)
assert BUILDER_TEST_SPEC is not None and BUILDER_TEST_SPEC.loader is not None
builder_tests = importlib.util.module_from_spec(BUILDER_TEST_SPEC)
BUILDER_TEST_SPEC.loader.exec_module(builder_tests)


class E01SyntheticMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = builder_tests.E01FormalManifestBuilderTests(
            "test_valid_manifest_freezes_21_runs_in_fixed_order"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.manifest = self.fixture.build()
        self.manifest_path = self.fixture.output
        self.root = self.fixture.root / "matrix"

    def rewrite_manifest(self) -> None:
        self.manifest_path.write_text(
            json.dumps(self.manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def execute_matrix(self, *, fail_after: int | None = None) -> dict:
        return launcher.run_matrix(
            self.manifest_path,
            self.root,
            synthetic_test_mode=True,
            synthetic_fail_after=fail_after,
        )

    def test_strict_serial_synthetic_matrix_completes_21(self) -> None:
        done = self.execute_matrix()
        self.assertEqual(done["completed_cells"], 21)
        self.assertTrue(done["strict_serial"])
        self.assertTrue(done["synthetic_test_only"])
        self.assertEqual(len(list(self.root.glob("*/CELL-DONE.json"))), 21)
        self.assertTrue((self.root / "MATRIX-DONE.json").is_file())

    def test_resume_revalidates_sha_and_does_not_rerun_cells(self) -> None:
        with self.assertRaises(launcher.SyntheticStop):
            self.execute_matrix(fail_after=3)
        first = self.root / "001-seml0-r1" / "CELL-DONE.json"
        before = first.read_bytes()
        before_time = first.stat().st_mtime_ns
        done = self.execute_matrix()
        self.assertEqual(done["completed_cells"], 21)
        self.assertEqual(first.read_bytes(), before)
        self.assertEqual(first.stat().st_mtime_ns, before_time)

    def test_incomplete_existing_cell_is_rejected_without_overwrite(self) -> None:
        with self.assertRaises(launcher.SyntheticStop):
            self.execute_matrix(fail_after=1)
        incomplete = self.root / "002-seml0-r2"
        incomplete.mkdir()
        marker = incomplete / "preserve.txt"
        marker.write_text("preserve\n")
        with self.assertRaises(launcher.ContractError):
            self.execute_matrix()
        self.assertEqual(marker.read_text(), "preserve\n")
        self.assertFalse((self.root / "MATRIX-DONE.json").exists())

    def test_corrupt_cell_done_sha_is_rejected(self) -> None:
        with self.assertRaises(launcher.SyntheticStop):
            self.execute_matrix(fail_after=1)
        result = self.root / "001-seml0-r1" / "synthetic-result.json"
        result.write_text(result.read_text().replace('"PASS"', '"FAIL"', 1))
        with self.assertRaises(launcher.ContractError):
            self.execute_matrix()

    def test_nonpass_formal_manifest_is_rejected_before_root_creation(self) -> None:
        self.manifest["state"] = "FROZEN"
        self.rewrite_manifest()
        with self.assertRaises(launcher.ContractError):
            self.execute_matrix()
        self.assertFalse(self.root.exists())

    def test_manifest_eligibility_promotion_is_rejected(self) -> None:
        self.manifest["formal_eligible"] = True
        self.rewrite_manifest()
        with self.assertRaises(launcher.ContractError):
            self.execute_matrix()
        self.assertFalse(self.root.exists())

    def test_stale_receipt_sha_is_rejected(self) -> None:
        p03 = Path(self.manifest["admission"]["p03_clean_ready"]["path"])
        p03.write_text(p03.read_text().replace('"fresh": true', '"fresh": false'))
        with self.assertRaises(launcher.ContractError):
            self.execute_matrix()
        self.assertFalse(self.root.exists())

    def test_missing_store_seal_is_rejected(self) -> None:
        seal = Path(self.manifest["systems"][0]["store_seal"]["path"])
        seal.unlink()
        with self.assertRaises(launcher.ContractError):
            self.execute_matrix()
        self.assertFalse(self.root.exists())

    def test_production_entry_is_fail_closed(self) -> None:
        with self.assertRaises(launcher.ContractError):
            launcher.run_matrix(
                self.manifest_path, self.root, synthetic_test_mode=False
            )
        self.assertFalse(self.root.exists())

    def test_premature_matrix_done_is_rejected(self) -> None:
        with self.assertRaises(launcher.SyntheticStop):
            self.execute_matrix(fail_after=2)
        (self.root / "MATRIX-DONE.json").write_text(
            json.dumps(
                {
                    "schema_version": "cidr-e01-matrix-done-v1",
                    "state": "PASS",
                    "completed_cells": 2,
                }
            )
            + "\n"
        )
        with self.assertRaises(launcher.ContractError):
            self.execute_matrix(fail_after=2)

    def test_unknown_crash_residue_is_rejected(self) -> None:
        with self.assertRaises(launcher.SyntheticStop):
            self.execute_matrix(fail_after=1)
        residue = self.root / ".002-seml0-r2.tmp-crash"
        residue.mkdir()
        (residue / "preserve.txt").write_text("preserve\n")
        with self.assertRaises(launcher.ContractError):
            self.execute_matrix()
        self.assertEqual((residue / "preserve.txt").read_text(), "preserve\n")


if __name__ == "__main__":
    unittest.main()
