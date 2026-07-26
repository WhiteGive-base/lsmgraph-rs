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

COMMAND_TEST_SPEC = importlib.util.spec_from_file_location(
    "command_plan_tests_for_spec", HERE.with_name("test_e01_production_command_plan.py")
)
assert COMMAND_TEST_SPEC is not None and COMMAND_TEST_SPEC.loader is not None
command_tests = importlib.util.module_from_spec(COMMAND_TEST_SPEC)
COMMAND_TEST_SPEC.loader.exec_module(command_tests)


class E01ProductionSpecAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = command_tests.E01ProductionCommandPlanTests(
            "test_valid_plan_has_21_explicit_argv_and_creates_no_campaign_root"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.validator = command_tests.plan_builder.production_spec
        self.contract_schema = ROOT / "e01-production-command-spec-v1.schema.json"

    def rewrite(self) -> None:
        self.fixture.rewrite_spec()

    def validate(self):
        return self.validator.validate_production_spec(
            self.fixture.spec_path,
            manifest_path=self.fixture.manifest_path,
            evidence_schema_path=self.fixture.schema_path,
            contract_schema_path=self.contract_schema,
        )

    def test_valid_real_spec_interface_binds_full_admission_chain(self) -> None:
        result = self.validate()
        binding = result["admission_binding"]
        self.assertEqual(
            binding["batch_lease_sha256"],
            self.fixture.manifest["admission"]["batch_lease"]["sha256"],
        )
        self.assertEqual(result["execution_state"], "NOT_IMPLEMENTED")
        self.assertFalse(result["formal_eligible"])

    def test_missing_admission_binding_is_rejected(self) -> None:
        del self.fixture.spec["admission_binding"]
        self.rewrite()
        with self.assertRaises(self.validator.SpecError):
            self.validate()

    def test_lease_binding_drift_is_rejected(self) -> None:
        self.fixture.spec["admission_binding"]["batch_lease_sha256"] = "9" * 64
        self.rewrite()
        with self.assertRaises(self.validator.SpecError):
            self.validate()

    def test_p03_binding_drift_is_rejected(self) -> None:
        self.fixture.spec["admission_binding"]["p03_clean_ready_sha256"] = "9" * 64
        self.rewrite()
        with self.assertRaises(self.validator.SpecError):
            self.validate()

    def test_cell_evidence_schema_binding_drift_is_rejected(self) -> None:
        self.fixture.spec["admission_binding"]["cell_evidence_schema_sha256"] = "9" * 64
        self.rewrite()
        with self.assertRaises(self.validator.SpecError):
            self.validate()

    def test_executor_cannot_be_promoted_from_not_implemented(self) -> None:
        self.fixture.spec["admission_binding"]["production_executor_state"] = "READY"
        self.rewrite()
        with self.assertRaises(self.validator.SpecError):
            self.validate()

    def test_fixture_or_synthetic_admission_is_rejected(self) -> None:
        self.fixture.spec["admission_binding"]["fixture_only"] = True
        self.rewrite()
        with self.assertRaises(self.validator.SpecError):
            self.validate()

    def test_admission_eligibility_promotion_is_rejected(self) -> None:
        self.fixture.spec["admission_binding"]["performance_eligible"] = True
        self.rewrite()
        with self.assertRaises(self.validator.SpecError):
            self.validate()

    def test_plan_validator_rejects_admission_binding_drift(self) -> None:
        plan = self.fixture.build()
        plan["admission_binding"]["lease_marker_sha256"] = "9" * 64
        with self.assertRaises(command_tests.plan_builder.PlanError):
            command_tests.plan_builder.validate_command_plan(plan, None)

    def test_contract_schema_declares_ineligible_not_implemented_binding(self) -> None:
        schema = json.loads(self.contract_schema.read_text())
        binding = schema["properties"]["admission_binding"]["properties"]
        self.assertEqual(binding["production_executor_state"]["const"], "NOT_IMPLEMENTED")
        self.assertEqual(binding["formal_eligible"]["const"], False)
        self.assertEqual(binding["fixture_only"]["const"], False)


if __name__ == "__main__":
    unittest.main()
