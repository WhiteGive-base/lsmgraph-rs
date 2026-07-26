#!/usr/bin/env python3
from __future__ import annotations

import copy
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

PLAN_SPEC = importlib.util.spec_from_file_location(
    "build_e01_production_command_plan",
    ROOT / "build_e01_production_command_plan.py",
)
assert PLAN_SPEC is not None and PLAN_SPEC.loader is not None
plan_builder = importlib.util.module_from_spec(PLAN_SPEC)
PLAN_SPEC.loader.exec_module(plan_builder)

BUILDER_TEST_SPEC = importlib.util.spec_from_file_location(
    "e01_builder_tests_for_plan", HERE.with_name("test_build_e01_formal_manifest.py")
)
assert BUILDER_TEST_SPEC is not None and BUILDER_TEST_SPEC.loader is not None
builder_tests = importlib.util.module_from_spec(BUILDER_TEST_SPEC)
BUILDER_TEST_SPEC.loader.exec_module(builder_tests)


FALSE = {
    "formal_eligible": False,
    "performance_eligible": False,
    "paper_claim_eligible": False,
}


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class E01ProductionCommandPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = builder_tests.E01FormalManifestBuilderTests(
            "test_valid_manifest_freezes_21_runs_in_fixed_order"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.manifest = self.fixture.build()
        self.manifest_path = self.fixture.output
        self.campaign_root = self.fixture.root / "production-root-must-not-exist"
        self.p31 = self.fixture.root / "bin/p31-wrapper.py"
        self.cgroup = self.fixture.root / "bin/cgroup-wrapper.py"
        self.p31.parent.mkdir(parents=True)
        self.p31.write_text("#!/usr/bin/env python3\n")
        self.cgroup.write_text("#!/usr/bin/env python3\n")
        systems = []
        for key, _ in plan_builder.manifest_builder.SYSTEMS:
            entry = self.fixture.root / f"bin/{key}-adapter.py"
            entry.write_text("#!/usr/bin/env python3\n")
            systems.append(
                {
                    "system_key": key,
                    "cwd_relative": "work",
                    "adapter_entry": str(entry),
                    "adapter_kind": "python-script",
                    "argv": [
                        str(entry),
                        "--manifest",
                        "{manifest}",
                        "--run-key",
                        "{run_key}",
                        "--result",
                        "{validated_result}",
                        "--correctness-receipt",
                        "{correctness_receipt}",
                        "--cleanup-receipt",
                        "{cleanup_receipt}",
                    ],
                }
            )
        self.spec = {
            "schema_version": plan_builder.SPEC_SCHEMA,
            "state": "READY",
            "campaign_id": self.manifest["campaign_id"],
            "campaign_root": str(self.campaign_root),
            "environment": {"LC_ALL": "C", "LANG": "C", "TZ": "UTC"},
            "p31_wrapper": str(self.p31),
            "cgroup_wrapper": str(self.cgroup),
            "systems": systems,
            "classification": dict(FALSE),
        }
        self.spec_path = self.fixture.root / "command-spec.json"
        self.schema_path = ROOT / "e01-cell-evidence-v1.schema.json"
        self.output = self.fixture.root / "command-plan.json"
        write_json(self.spec_path, self.spec)

    def rewrite_spec(self) -> None:
        write_json(self.spec_path, self.spec)

    def build(self) -> dict:
        return plan_builder.build_command_plan(
            self.manifest_path, self.spec_path, self.schema_path, self.output
        )

    def test_valid_plan_has_21_explicit_argv_and_creates_no_campaign_root(self) -> None:
        plan = self.build()
        self.assertEqual(plan["cell_count"], 21)
        self.assertEqual(plan["execution_state"], "NOT_IMPLEMENTED")
        self.assertFalse(self.campaign_root.exists())
        self.assertEqual(len({cell["cell_root"] for cell in plan["cells"]}), 21)
        self.assertTrue(all(type(cell["command_argv"]) is list for cell in plan["cells"]))
        plan_builder.validate_command_plan(self.output, self.output)

    def test_shell_command_string_is_rejected(self) -> None:
        self.spec["systems"][0]["argv"] = "adapter --run"
        self.rewrite_spec()
        with self.assertRaises(plan_builder.PlanError):
            self.build()

    def test_cwd_path_traversal_is_rejected(self) -> None:
        self.spec["systems"][0]["cwd_relative"] = "../outside"
        self.rewrite_spec()
        with self.assertRaises(plan_builder.PlanError):
            self.build()

    def test_nonallowlisted_environment_is_rejected(self) -> None:
        self.spec["environment"]["LD_PRELOAD"] = "/tmp/not-allowed.so"
        self.rewrite_spec()
        with self.assertRaises(plan_builder.PlanError):
            self.build()

    def test_existing_output_is_not_overwritten(self) -> None:
        self.output.write_text("preserve\n")
        with self.assertRaises(FileExistsError):
            self.build()
        self.assertEqual(self.output.read_text(), "preserve\n")
        self.assertFalse(self.campaign_root.exists())

    def test_duplicate_cell_root_is_rejected_by_validator(self) -> None:
        plan = self.build()
        plan["cells"][1]["cell_root"] = plan["cells"][0]["cell_root"]
        plan["cells"][1]["cwd"] = plan["cells"][0]["cwd"]
        plan["cells"][1]["targets"] = copy.deepcopy(plan["cells"][0]["targets"])
        with self.assertRaises(plan_builder.PlanError):
            plan_builder.validate_command_plan(plan, None)

    def test_target_path_outside_root_is_rejected(self) -> None:
        plan = self.build()
        plan["cells"][0]["targets"]["cleanup_receipt"] = str(
            self.fixture.root / "outside.json"
        )
        with self.assertRaises(plan_builder.PlanError):
            plan_builder.validate_command_plan(plan, None)

    def test_eligibility_promotion_is_rejected(self) -> None:
        plan = self.build()
        plan["cells"][0]["performance_eligible"] = True
        with self.assertRaises(plan_builder.PlanError):
            plan_builder.validate_command_plan(plan, None)

    def test_adapter_entry_sha_drift_is_rejected(self) -> None:
        plan = self.build()
        entry = Path(plan["cells"][0]["adapter"]["entry"]["path"])
        entry.write_text(entry.read_text() + "# drift\n")
        with self.assertRaises(plan_builder.PlanError):
            plan_builder.validate_command_plan(plan, None)

    def test_unattested_argv_entry_is_rejected(self) -> None:
        plan = self.build()
        plan["cells"][0]["adapter"]["argv"][0] = "/tmp/unattested-adapter"
        with self.assertRaises(plan_builder.PlanError):
            plan_builder.validate_command_plan(plan, None)

    def test_production_execution_remains_pre_root_fail_closed(self) -> None:
        with self.assertRaises(plan_builder.scheduler.ContractError):
            plan_builder.scheduler.run_matrix(
                self.manifest_path,
                self.campaign_root,
                synthetic_test_mode=False,
            )
        self.assertFalse(self.campaign_root.exists())


if __name__ == "__main__":
    unittest.main()
