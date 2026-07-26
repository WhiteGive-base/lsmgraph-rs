#!/usr/bin/env python3
from __future__ import annotations

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

PRODUCER_SPEC = importlib.util.spec_from_file_location(
    "produce_e01_receipt_fixtures", ROOT / "produce_e01_receipt_fixtures.py"
)
assert PRODUCER_SPEC is not None and PRODUCER_SPEC.loader is not None
producer = importlib.util.module_from_spec(PRODUCER_SPEC)
PRODUCER_SPEC.loader.exec_module(producer)

SCHEDULER_TEST_SPEC = importlib.util.spec_from_file_location(
    "scheduler_tests_for_producer", HERE.with_name("test_run_e01_formal_matrix.py")
)
assert SCHEDULER_TEST_SPEC is not None and SCHEDULER_TEST_SPEC.loader is not None
scheduler_tests = importlib.util.module_from_spec(SCHEDULER_TEST_SPEC)
SCHEDULER_TEST_SPEC.loader.exec_module(scheduler_tests)


class E01ReceiptFixtureProducerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="e01-receipt-producer-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cell = self.root / "cell"
        self.cell.mkdir()
        self.manifest_sha = "a" * 64

    def produce(self, **overrides):
        values = {
            "run_key": "seml0:r1",
            "ordinal": 1,
            "manifest_sha": self.manifest_sha,
            "fixture_only": True,
            "receipt_dir_relative": "receipts",
        }
        values.update(overrides)
        return producer.produce_fixture_receipts(self.cell, **values)

    def test_produces_exactly_seven_small_attested_synthetic_receipts(self) -> None:
        refs = self.produce()
        self.assertEqual(set(refs), set(producer.evidence.ROLES))
        for role, ref in refs.items():
            path = self.cell / ref["path"]
            value = json.loads(path.read_text())
            self.assertLessEqual(path.stat().st_size, producer.evidence.MAX_RECEIPT_BYTES)
            self.assertTrue(value["synthetic_test_only"])
            self.assertTrue(value["fixture_only"])
            self.assertFalse(value["formal_eligible"])
            self.assertFalse(value["performance_eligible"])
            self.assertFalse(value["paper_claim_eligible"])

    def test_production_mode_fails_before_output_root(self) -> None:
        missing = self.root / "must-not-be-created"
        with self.assertRaises(producer.ProducerError):
            producer.produce_fixture_receipts(
                missing,
                run_key="seml0:r1",
                ordinal=1,
                manifest_sha=self.manifest_sha,
                fixture_only=False,
            )
        self.assertFalse(missing.exists())

    def test_existing_receipt_directory_is_not_overwritten(self) -> None:
        self.produce()
        command = self.cell / "receipts/command.json"
        before = command.read_bytes()
        with self.assertRaises(producer.ProducerError):
            self.produce()
        self.assertEqual(command.read_bytes(), before)

    def test_receipt_directory_traversal_is_rejected(self) -> None:
        outside = self.root / "outside"
        with self.assertRaises(producer.ProducerError):
            self.produce(receipt_dir_relative="../outside")
        self.assertFalse(outside.exists())

    def test_bad_manifest_sha_is_rejected_without_partial_directory(self) -> None:
        with self.assertRaises(producer.ProducerError):
            self.produce(manifest_sha="bad")
        self.assertFalse((self.cell / "receipts").exists())
        self.assertEqual(list(self.cell.iterdir()), [])

    def test_run_ordinal_drift_is_rejected(self) -> None:
        with self.assertRaises(producer.ProducerError):
            self.produce(ordinal=2)
        self.assertFalse((self.cell / "receipts").exists())

    def test_symlink_cell_root_is_rejected(self) -> None:
        link = self.root / "cell-link"
        try:
            link.symlink_to(self.cell, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        with self.assertRaises(producer.ProducerError):
            producer.produce_fixture_receipts(
                link,
                run_key="seml0:r1",
                ordinal=1,
                manifest_sha=self.manifest_sha,
                fixture_only=True,
            )

    def test_fixture_bundle_cannot_satisfy_production_validator(self) -> None:
        fixture = scheduler_tests.E01SyntheticMatrixTests(
            "test_strict_serial_synthetic_matrix_completes_21"
        )
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with self.assertRaises(scheduler_tests.launcher.SyntheticStop):
            fixture.execute_matrix(fail_after=1)
        cell = fixture.root / "001-seml0-r1"
        with self.assertRaises(producer.evidence.EvidenceError):
            producer.evidence.validate_cell_evidence(
                cell,
                expected_run=fixture.manifest["runs"][0],
                manifest_sha=producer.sha256_file(fixture.manifest_path),
                expected_mode="production",
            )


if __name__ == "__main__":
    unittest.main()
