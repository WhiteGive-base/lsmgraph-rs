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
    "command_plan_tests_for_identity",
    HERE.with_name("test_e01_production_command_plan.py"),
)
assert COMMAND_TEST_SPEC is not None and COMMAND_TEST_SPEC.loader is not None
command_tests = importlib.util.module_from_spec(COMMAND_TEST_SPEC)
COMMAND_TEST_SPEC.loader.exec_module(command_tests)


class E01AdapterArtifactIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = command_tests.E01ProductionCommandPlanTests(
            "test_valid_plan_has_21_explicit_argv_and_creates_no_campaign_root"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.validator = command_tests.plan_builder.artifact_identity
        self.system = self.fixture.spec["systems"][0]
        self.receipt_path = Path(self.system["artifact_identity_receipt"])
        self.entry_path = Path(self.system["adapter_entry"])

    def rewrite(self, value: dict) -> None:
        command_tests.write_json(self.receipt_path, value)

    def value(self) -> dict:
        return json.loads(self.receipt_path.read_text())

    def validate(self):
        return self.validator.validate_identity_receipt(
            self.receipt_path,
            manifest=self.fixture.manifest,
            system_key="seml0",
            expected_entry=self.entry_path,
            expected_kind="python-script",
        )

    def test_valid_identity_binds_entry_engine_store_git_and_protocol(self) -> None:
        result = self.validate()
        self.assertEqual(result["adapter_entry"]["path"], str(self.entry_path.resolve()))
        self.assertEqual(result["execution_state"], "NOT_IMPLEMENTED")
        self.assertFalse(result["formal_eligible"])

    def test_entry_sha_drift_is_rejected(self) -> None:
        value = self.value()
        value["adapter_entry"]["sha256"] = "9" * 64
        self.rewrite(value)
        with self.assertRaises(self.validator.IdentityError):
            self.validate()

    def test_engine_binary_identity_drift_is_rejected(self) -> None:
        value = self.value()
        value["engine_binary_sha256"] = "9" * 64
        self.rewrite(value)
        with self.assertRaises(self.validator.IdentityError):
            self.validate()

    def test_store_identity_drift_is_rejected(self) -> None:
        value = self.value()
        value["store_sha256"] = "9" * 64
        self.rewrite(value)
        with self.assertRaises(self.validator.IdentityError):
            self.validate()

    def test_git_or_protocol_drift_is_rejected(self) -> None:
        value = self.value()
        value["protocol_sha256"] = "9" * 64
        self.rewrite(value)
        with self.assertRaises(self.validator.IdentityError):
            self.validate()

    def test_fixture_or_synthetic_identity_is_rejected(self) -> None:
        value = self.value()
        value["fixture_only"] = True
        self.rewrite(value)
        with self.assertRaises(self.validator.IdentityError):
            self.validate()

    def test_identity_eligibility_promotion_is_rejected(self) -> None:
        value = self.value()
        value["performance_eligible"] = True
        self.rewrite(value)
        with self.assertRaises(self.validator.IdentityError):
            self.validate()

    def test_adapter_entry_symlink_is_rejected(self) -> None:
        link = self.fixture.fixture.root / "bin/seml0-link.py"
        try:
            link.symlink_to(self.entry_path)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        value = self.value()
        value["adapter_entry"]["path"] = str(link)
        self.rewrite(value)
        with self.assertRaises(self.validator.IdentityError):
            self.validate()

    def test_invalid_identity_blocks_plan_before_output_or_formal_root(self) -> None:
        value = self.value()
        value["store_sha256"] = "9" * 64
        self.rewrite(value)
        with self.assertRaises(command_tests.plan_builder.PlanError):
            self.fixture.build()
        self.assertFalse(self.fixture.output.exists())
        self.assertFalse(self.fixture.campaign_root.exists())

    def test_plan_validator_rechecks_identity_receipt_sha_and_contents(self) -> None:
        plan = self.fixture.build()
        value = self.value()
        value["engine_binary_sha256"] = "9" * 64
        self.rewrite(value)
        with self.assertRaises(command_tests.plan_builder.PlanError):
            command_tests.plan_builder.validate_command_plan(plan, None)


if __name__ == "__main__":
    unittest.main()
