#!/usr/bin/env python3
"""Tests for the fail-closed E01 postprocess interface plan."""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import build_e01_postprocess_plan as post


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


if __name__ == "__main__":
    unittest.main()
