#!/usr/bin/env python3
"""Tests for the formal E01 matrix evidence adapter and PASS assembler."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent
for item in (ROOT, TEST_ROOT):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

import build_e01_mixed_lineage as builder
import normalize_e01_mixed_lineage as normalizer
import postprocess_e01_incremental as post
from test_e01_mixed_lineage import attach_completed_evidence, make_fixture


def write_json(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


class IncrementalPostprocessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="e01-postprocess-")
        self.root = Path(self.temporary.name)
        source, dataset, dense = make_fixture(self.root)
        self.hold = builder.build_composition(
            source, dataset, dense, created_at_utc="2026-07-29T00:00:00Z"
        )
        self.completed = attach_completed_evidence(self.root, self.hold)
        self.seed_evidence = self.completed["incremental_plan"]["incremental_evidence"]
        self.anchor = Path(self.seed_evidence["postprocess_anchor"]["path"])
        self.backend = Path(self.seed_evidence["formal_backend_plan"]["path"])
        self.matrix = Path(self.seed_evidence["matrix_done"]["path"])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def adapt(self) -> dict:
        return post.matrix_evidence_adapter(self.anchor, self.backend, self.matrix)

    def test_anchor_builder_reproduces_frozen_external_anchor(self) -> None:
        self.assertEqual(
            post.build_postprocess_anchor(self.backend),
            json.loads(self.anchor.read_text(encoding="utf-8")),
        )

    def test_realistic_four_cell_adapter_assembler_normalizer_e2e(self) -> None:
        evidence = self.adapt()
        self.assertEqual(evidence["schema_version"], post.EVIDENCE_SCHEMA)
        self.assertEqual(len(evidence["cells"]), 4)
        hold_path = write_json(self.root / "hold.json", self.hold)
        evidence_path = write_json(self.root / "evidence.json", evidence)
        composition = post.pass_composition_assembler(hold_path, evidence_path)
        composition_path = write_json(self.root / "composition.json", composition)
        result = normalizer.normalize(composition_path, None)
        self.assertEqual(len(result["rows"]), 21)

    def test_missing_matrix_is_rejected(self) -> None:
        self.matrix.unlink()
        with self.assertRaises(post.EvidenceError):
            self.adapt()

    def test_failed_matrix_is_rejected(self) -> None:
        value = json.loads(self.matrix.read_text(encoding="utf-8"))
        value["state"] = "FAILED"
        write_json(self.matrix, value)
        with self.assertRaises(post.EvidenceError):
            self.adapt()

    def test_partial_and_non_four_cell_matrices_are_rejected(self) -> None:
        original = json.loads(self.matrix.read_text(encoding="utf-8"))
        for mode in ("partial", "wrong-key"):
            with self.subTest(mode=mode):
                value = copy.deepcopy(original)
                if mode == "partial":
                    value["completed_cells"] = 3
                    value["cells"] = value["cells"][:3]
                else:
                    value["cells"][3]["cell_key"] = "unexpected:r3"
                write_json(self.matrix, value)
                with self.assertRaises(post.EvidenceError):
                    self.adapt()
                write_json(self.matrix, original)

    def test_missing_cell_done_is_rejected(self) -> None:
        first = self.seed_evidence["cells"][0]["cell_done"]
        Path(first["path"]).unlink()
        with self.assertRaises(post.EvidenceError):
            self.adapt()

    def test_external_anchor_rejects_synchronized_backend_chain_rewrite(self) -> None:
        plan = json.loads(self.backend.read_text(encoding="utf-8"))
        plan["lineage_note"] = "synchronized-tamper"
        write_json(self.backend, plan)
        plan_ref = post.file_ref(self.backend, "tampered backend")
        plan_sha = plan_ref["sha256"]
        arming_path = Path(plan["campaign_gates"]["backend_arming"]["path"])
        arming = json.loads(arming_path.read_text(encoding="utf-8"))
        arming["backend_plan"] = plan_ref
        write_json(arming_path, arming)
        new_cell_shas = []
        for row in plan["cells"]:
            final = Path(row["final_cell_root"])
            provenance_path = final / "adapter-output/adapter-provenance.json"
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            provenance["split_phase_binding"]["backend_plan"] = plan_ref
            write_json(provenance_path, provenance)
            provenance_ref = post.file_ref(provenance_path, "rewritten provenance")
            validated_path = final / "adapter-output/validated-repeat.json"
            validated = json.loads(validated_path.read_text(encoding="utf-8"))
            validated["backend_plan"] = plan_ref
            validated["adapter_provenance"] = provenance
            validated["adapter_artifacts"]["adapter-provenance.json"] = provenance_ref
            write_json(validated_path, validated)
            validated_ref = post.file_ref(validated_path, "rewritten validated repeat")
            receipt_path = final / "validated-result.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["backend_plan_sha256"] = plan_sha
            receipt["adapter_result"] = validated_ref
            receipt["adapter_provenance"] = provenance_ref
            write_json(receipt_path, receipt)
            receipt_ref = post.file_ref(receipt_path, "rewritten validated receipt")
            rewritten = {"validated_result": receipt_ref}
            for role in ("cleanup", "correctness"):
                path = final / post.RECEIPT_PATHS[role]
                value = json.loads(path.read_text(encoding="utf-8"))
                value["backend_plan_sha256"] = plan_sha
                write_json(path, value)
                rewritten[role] = post.file_ref(path, f"rewritten {role}")
            done_path = final / "CELL-DONE.json"
            done = json.loads(done_path.read_text(encoding="utf-8"))
            done["backend_plan_sha256"] = plan_sha
            for role, ref in rewritten.items():
                done["receipts"][role] = {
                    "path": post.RECEIPT_PATHS[role],
                    "sha256": ref["sha256"],
                    "size_bytes": ref["size_bytes"],
                }
            write_json(done_path, done)
            new_cell_shas.append(post.sha256_file(done_path))
        start_path = Path(plan["campaign_root"]) / "MATRIX-START.json"
        start = json.loads(start_path.read_text(encoding="utf-8"))
        start["backend_plan_sha256"] = plan_sha
        write_json(start_path, start)
        matrix = json.loads(self.matrix.read_text(encoding="utf-8"))
        matrix["backend_plan_sha256"] = plan_sha
        for item, digest in zip(matrix["cells"], new_cell_shas):
            item["cell_done_sha256"] = digest
        write_json(self.matrix, matrix)
        with self.assertRaisesRegex(post.EvidenceError, "external postprocess anchor drift"):
            self.adapt()

    def test_producer_and_consumer_reject_each_consistently_rehashed_role(self) -> None:
        direct_fields = {
            "validated_result": "validated_result_receipt",
            "p31": "p31_receipt",
            "command_topology": "command_topology",
            "store_clone": "store_clone_receipt",
            "cleanup": "cleanup_receipt",
        }
        for role in post.RECEIPT_PATHS:
            for mutation in ("state", "schema"):
                with self.subTest(role=role, mutation=mutation):
                    completed = attach_completed_evidence(self.root, self.hold)
                    evidence = completed["incremental_plan"]["incremental_evidence"]
                    cell = evidence["cells"][0]
                    final = Path(cell["cell_done"]["path"]).parent
                    path = final / post.RECEIPT_PATHS[role]
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if mutation == "state":
                        value["state"] = "FAILED"
                    else:
                        value["schema_version"] = "tampered"
                    write_json(path, value)
                    rewritten_ref = post.file_ref(path, f"rewritten {role}")
                    done_path = final / "CELL-DONE.json"
                    done = json.loads(done_path.read_text(encoding="utf-8"))
                    done["receipts"][role] = {
                        "path": post.RECEIPT_PATHS[role],
                        "sha256": rewritten_ref["sha256"],
                        "size_bytes": rewritten_ref["size_bytes"],
                    }
                    write_json(done_path, done)
                    done_ref = post.file_ref(done_path, "rewritten CELL-DONE")
                    cell["cell_done"] = done_ref
                    if role in direct_fields:
                        cell[direct_fields[role]] = rewritten_ref
                    matrix_path = Path(evidence["matrix_done"]["path"])
                    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
                    matrix["cells"][0]["cell_done_sha256"] = done_ref["sha256"]
                    write_json(matrix_path, matrix)
                    evidence["matrix_done"] = post.file_ref(
                        matrix_path, "rewritten MATRIX-DONE"
                    )
                    with self.assertRaises(post.EvidenceError):
                        post.matrix_evidence_adapter(
                            Path(evidence["postprocess_anchor"]["path"]),
                            Path(evidence["formal_backend_plan"]["path"]),
                            matrix_path,
                        )
                    composition_path = write_json(
                        self.root / f"tampered-{role}-{mutation}.json", completed
                    )
                    with self.assertRaises(builder.CompositionError):
                        normalizer.normalize(composition_path, None)


if __name__ == "__main__":
    unittest.main()
