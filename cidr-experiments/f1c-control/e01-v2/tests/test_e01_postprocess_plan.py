#!/usr/bin/env python3
"""Tests for the fail-closed E01 postprocess interface plan."""

from __future__ import annotations

import csv
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

import build_e01_postprocess_plan as post
import run_e01_incremental_production as production


def write_json(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


class PostprocessPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.mixed = write_json(
            self.root / "mixed.json",
            {"schema_version": "cidr-e01-mixed-lineage-composition-v1", "state": "HOLD"},
        )
        self.boundary = write_json(
            self.root / "boundary.json",
            {"schema_version": post.BOUNDARY_SCHEMA, "state": "HOLD"},
        )
        self.tools = []
        for name in ("backend.py", "canary.py", "normalizer.py", "renderer.py", "support.py", "qa.py"):
            path = self.root / name
            path.write_text("# fixture\n", encoding="utf-8")
            self.tools.append(path)
        self.requirements = self.root / "requirements.tsv"
        with self.requirements.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["figure_id", "field_name", "required"],
                delimiter="\t",
            )
            writer.writeheader()
            for field in ("run_id", "system", "variant", "load_wall_s", "final_disk_bytes"):
                writer.writerow(
                    {"figure_id": "F1", "field_name": field, "required": "yes"}
                )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build(self) -> dict:
        return post.build(
            mixed_plan_path=self.mixed,
            boundary_plan_path=self.boundary,
            production_backend_path=self.tools[0],
            canary_evaluator_path=self.tools[1],
            normalizer_path=self.tools[2],
            renderer_path=self.tools[3],
            plot_support_path=self.tools[4],
            render_validator_path=self.tools[5],
            figure_requirements_path=self.requirements,
            output_root=self.root / "future-output",
        )

    def test_current_normalizer_cannot_be_fed_directly_to_renderer(self) -> None:
        value = post.validate(self.build())
        missing = set(
            value["interfaces"]["frozen_tidy_transform"]["missing_after_current_normalizer"]
        )
        self.assertTrue({"run_id", "system", "variant", "load_wall_s", "final_disk_bytes"} <= missing)
        self.assertEqual(value["interfaces"]["frozen_tidy_transform"]["state"], "NOT_IMPLEMENTED")

    def test_cost_and_claim_gates_stay_closed(self) -> None:
        value = self.build()
        self.assertEqual(
            value["execution_state"], "CORE_IMPLEMENTED_REMAINDER_HOLD"
        )
        self.assertEqual(
            value["interfaces"]["matrix_evidence_adapter"]["state"],
            "IMPLEMENTED_NOT_RUN",
        )
        self.assertEqual(
            value["interfaces"]["pass_composition_assembler"]["state"],
            "IMPLEMENTED_NOT_RUN",
        )
        cost = value["interfaces"]["cost_lineage_admission"]
        self.assertTrue(cost["historical_cost_implicit_reuse_forbidden"])
        self.assertTrue(cost["fresh_or_explicitly_admitted_cost_receipt_required"])
        self.assertFalse(value["interfaces"]["claim_receipt"]["paper_claim_eligible"])
        self.assertFalse(value["renderer_invoked"])
        self.assertFalse(value["qa_invoked"])

    def test_existing_output_root_is_rejected(self) -> None:
        (self.root / "future-output").mkdir()
        with self.assertRaises(post.PostprocessError):
            self.build()

    def test_missing_f1_contract_is_rejected(self) -> None:
        self.requirements.write_text(
            "figure_id\tfield_name\trequired\nF4\tx\tyes\n", encoding="utf-8"
        )
        with self.assertRaises(post.PostprocessError):
            self.build()

    def test_real_snapshot_keeps_renderer_and_claim_closed(self) -> None:
        snapshot = ROOT / "E01-postprocess-interface-plan-HOLD-v1.json"
        if not snapshot.exists():
            self.skipTest("real postprocess snapshot is not installed")
        value = post.validate(snapshot)
        self.assertEqual(value["state"], "HOLD")
        self.assertFalse(value["renderer_invoked"])
        self.assertFalse(value["qa_invoked"])
        self.assertFalse(
            value["interfaces"]["claim_receipt"]["paper_claim_eligible"]
        )
        self.assertFalse(Path(value["output_root"]).exists())

    def test_real_ready9_validates_against_frozen_mixed_snapshot(self) -> None:
        ready9_raw = os.environ.get("E01_REAL_READY9")
        if not ready9_raw:
            self.skipTest("E01_REAL_READY9 is not configured")
        ready9 = Path(ready9_raw).resolve()
        value = production.validate_backend_plan(ready9)
        mixed_ref = value["canary_checkpoint"]["mixed_lineage_plan"]
        frozen = (ROOT / "E01-mixed-lineage-plan-v1.json").resolve()
        self.assertEqual(
            mixed_ref,
            {
                "path": str(frozen),
                "sha256": "337ba616ef3dc6a97264c147f307d3d68007e73a4269fee0ce97c33c8bf106db",
                "size_bytes": 100588,
            },
        )
        payload = frozen.read_bytes()
        self.assertEqual(len(payload), mixed_ref["size_bytes"])
        self.assertEqual(hashlib.sha256(payload).hexdigest(), mixed_ref["sha256"])


if __name__ == "__main__":
    unittest.main()
