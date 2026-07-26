#!/usr/bin/env python3
from __future__ import annotations

import hashlib
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

VALIDATOR_SPEC = importlib.util.spec_from_file_location(
    "validate_e01_cell_evidence", ROOT / "validate_e01_cell_evidence.py"
)
assert VALIDATOR_SPEC is not None and VALIDATOR_SPEC.loader is not None
validator = importlib.util.module_from_spec(VALIDATOR_SPEC)
VALIDATOR_SPEC.loader.exec_module(validator)

SCHEDULER_TEST_SPEC = importlib.util.spec_from_file_location(
    "e01_scheduler_tests", HERE.with_name("test_run_e01_formal_matrix.py")
)
assert SCHEDULER_TEST_SPEC is not None and SCHEDULER_TEST_SPEC.loader is not None
scheduler_tests = importlib.util.module_from_spec(SCHEDULER_TEST_SPEC)
SCHEDULER_TEST_SPEC.loader.exec_module(scheduler_tests)


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class E01CellEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = scheduler_tests.E01SyntheticMatrixTests(
            "test_strict_serial_synthetic_matrix_completes_21"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        with self.assertRaises(scheduler_tests.launcher.SyntheticStop):
            self.fixture.execute_matrix(fail_after=1)
        self.cell = self.fixture.root / "001-seml0-r1"
        self.row = self.fixture.manifest["runs"][0]
        self.manifest_sha = sha(self.fixture.manifest_path)

    def validate(self, mode: str = "synthetic") -> dict:
        return validator.validate_cell_evidence(
            self.cell,
            expected_run=self.row,
            manifest_sha=self.manifest_sha,
            expected_mode=mode,
        )

    def rewrite_receipt(self, role: str, mutate) -> None:
        done_path = self.cell / "CELL-DONE.json"
        done = json.loads(done_path.read_text())
        receipt_path = self.cell / done["receipts"][role]["path"]
        receipt = json.loads(receipt_path.read_text())
        mutate(receipt)
        write_json(receipt_path, receipt)
        done["receipts"][role]["sha256"] = sha(receipt_path)
        done["receipts"][role]["size_bytes"] = receipt_path.stat().st_size
        write_json(done_path, done)

    def promote_to_production(self, *, adapter_invoked: bool, timing_generated: bool) -> None:
        synthetic_result = self.cell / "synthetic-result.json"
        validated_result = self.cell / "validated-result.json"
        result = json.loads(synthetic_result.read_text())
        result.update(
            {
                "schema_version": "cidr-e01-production-cell-result-v1",
                "mode": "production",
                "synthetic_test_only": False,
                "adapter_invoked": adapter_invoked,
                "timing_generated": timing_generated,
            }
        )
        write_json(validated_result, result)
        done_path = self.cell / "CELL-DONE.json"
        done = json.loads(done_path.read_text())
        done.update(
            {
                "mode": "production",
                "synthetic_test_only": False,
                "adapter_invoked": adapter_invoked,
                "timing_generated": timing_generated,
                "result_sha256": sha(validated_result),
            }
        )
        for role in validator.ROLES:
            receipt_path = self.cell / done["receipts"][role]["path"]
            receipt = json.loads(receipt_path.read_text())
            receipt["mode"] = "production"
            receipt["synthetic_test_only"] = False
            if role == "command":
                receipt["adapter_invoked"] = adapter_invoked
                receipt["timing_generated"] = timing_generated
            elif role == "adapter":
                receipt["adapter_invoked"] = adapter_invoked
            elif role == "p31":
                receipt["timing_generated"] = timing_generated
            elif role == "cgroup":
                receipt["cpuset"] = "0-31"
            write_json(receipt_path, receipt)
            done["receipts"][role]["sha256"] = sha(receipt_path)
            done["receipts"][role]["size_bytes"] = receipt_path.stat().st_size
        write_json(done_path, done)

    def test_valid_synthetic_bundle_uses_all_seven_receipts(self) -> None:
        result = self.validate()
        self.assertEqual(set(result["receipt_sha256"]), set(validator.ROLES))
        self.assertTrue(result["synthetic_test_only"])

    def test_missing_receipt_is_rejected(self) -> None:
        done = json.loads((self.cell / "CELL-DONE.json").read_text())
        (self.cell / done["receipts"]["cleanup"]["path"]).unlink()
        with self.assertRaises(validator.EvidenceError):
            self.validate()

    def test_receipt_sha_drift_is_rejected(self) -> None:
        done = json.loads((self.cell / "CELL-DONE.json").read_text())
        path = self.cell / done["receipts"]["p31"]["path"]
        path.write_text(path.read_text() + " ")
        with self.assertRaises(validator.EvidenceError):
            self.validate()

    def test_receipt_eligibility_promotion_is_rejected(self) -> None:
        self.rewrite_receipt(
            "fairness", lambda value: value.__setitem__("performance_eligible", True)
        )
        with self.assertRaises(validator.EvidenceError):
            self.validate()

    def test_adapter_not_invoked_cannot_masquerade_as_production(self) -> None:
        self.promote_to_production(adapter_invoked=False, timing_generated=True)
        with self.assertRaises(validator.EvidenceError):
            self.validate("production")

    def test_timing_not_generated_cannot_masquerade_as_production(self) -> None:
        self.promote_to_production(adapter_invoked=True, timing_generated=False)
        with self.assertRaises(validator.EvidenceError):
            self.validate("production")

    def test_complete_production_shape_is_accepted_as_contract_fixture_only(self) -> None:
        self.promote_to_production(adapter_invoked=True, timing_generated=True)
        result = self.validate("production")
        self.assertFalse(result["synthetic_test_only"])
        self.assertFalse(result["formal_eligible"])

    def test_schema_lists_exact_receipt_roles(self) -> None:
        schema = json.loads((ROOT / "e01-cell-evidence-v1.schema.json").read_text())
        required = schema["properties"]["receipts"]["required"]
        self.assertEqual(set(required), set(validator.ROLES))
        self.assertEqual(schema["properties"]["formal_eligible"]["const"], False)


if __name__ == "__main__":
    unittest.main()
