#!/usr/bin/env python3
"""Tests for the formal E01 matrix evidence adapter and PASS assembler."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
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

    def rehash_role_chain(
        self, completed: dict, role: str, value: dict, label: str
    ) -> tuple[Path, Path]:
        direct_fields = {
            "validated_result": "validated_result_receipt",
            "p31": "p31_receipt",
            "command_topology": "command_topology",
            "store_clone": "store_clone_receipt",
            "cleanup": "cleanup_receipt",
        }
        evidence = completed["incremental_plan"]["incremental_evidence"]
        cell = evidence["cells"][0]
        final = Path(cell["cell_done"]["path"]).parent
        path = final / post.RECEIPT_PATHS[role]
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
        evidence["matrix_done"] = post.file_ref(matrix_path, "rewritten MATRIX-DONE")
        composition_path = write_json(self.root / f"tampered-{label}.json", completed)
        return matrix_path, composition_path

    def assert_both_consumers_reject(
        self, completed: dict, role: str, value: dict, label: str
    ) -> None:
        evidence = completed["incremental_plan"]["incremental_evidence"]
        matrix_path, composition_path = self.rehash_role_chain(
            completed, role, value, label
        )
        with self.assertRaises(post.EvidenceError):
            post.matrix_evidence_adapter(
                Path(evidence["postprocess_anchor"]["path"]),
                Path(evidence["formal_backend_plan"]["path"]),
                matrix_path,
            )
        with self.assertRaises(builder.CompositionError):
            normalizer.normalize(composition_path, None)

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

    def test_real_phase_executor_receipts_cross_role_e2e(self) -> None:
        root_value = os.environ.get("E01_REAL_RECEIPT_ROOT")
        if not root_value:
            self.skipTest("E01_REAL_RECEIPT_ROOT not configured")
        source = Path(root_value).resolve()
        final = self.root / "relocated-real-phase-cell"
        shutil.copytree(source, final)
        request_ref = post.file_ref(final / "adapter-request.json", "real request")
        command_file_ref = post.file_ref(
            final / "p31/command.txt", "real command file"
        )
        manifest_path = final / "p31/run-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["command"] = command_file_ref
        write_json(manifest_path, manifest)
        manifest_ref = post.file_ref(manifest_path, "real run manifest")
        topology_path = final / post.RECEIPT_PATHS["command_topology"]
        topology = json.loads(topology_path.read_text(encoding="utf-8"))
        topology["run_manifest"] = manifest_ref
        topology["command_file"] = command_file_ref
        write_json(topology_path, topology)
        topology_ref = post.file_ref(topology_path, "real topology")
        p31_path = final / post.RECEIPT_PATHS["p31"]
        p31 = json.loads(p31_path.read_text(encoding="utf-8"))
        p31["run_manifest"] = manifest_ref
        p31["command_topology"] = topology_ref
        write_json(p31_path, p31)
        p31_ref = post.file_ref(p31_path, "real P31")
        prepared_path = final / post.RECEIPT_PATHS["prepared_command"]
        prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
        prepared["request"] = request_ref
        config_index = prepared["p31_argv"].index("--config")
        prepared["p31_argv"][config_index + 1] = request_ref["path"]
        write_json(prepared_path, prepared)
        clone_path = final / post.RECEIPT_PATHS["store_clone"]
        clone = json.loads(clone_path.read_text(encoding="utf-8"))
        absent_clone = str((final / "removed-mutable-store").resolve())
        clone["target"] = absent_clone
        write_json(clone_path, clone)
        clone_ref = post.file_ref(clone_path, "real clone")
        cleanup_path = final / post.RECEIPT_PATHS["cleanup"]
        cleanup = json.loads(cleanup_path.read_text(encoding="utf-8"))
        cleanup["mutable_clone"] = absent_clone
        cleanup["mutable_clone_lexists_after"] = False
        write_json(cleanup_path, cleanup)
        fairness_path = final / post.RECEIPT_PATHS["fairness"]
        fairness = json.loads(fairness_path.read_text(encoding="utf-8"))
        fairness["p31_receipt_sha256"] = p31_ref["sha256"]
        fairness["single_binary_process"] = True
        fairness["asset_hash_inside_boundary"] = False
        fairness["clone_inside_boundary"] = False
        write_json(fairness_path, fairness)

        validated_artifacts = {
            name: post.file_ref(final / "adapter-output" / name, f"real {name}")
            for name in (
                "adapter-result.json",
                "query-observations.tsv",
                "phase-events.jsonl",
            )
        }
        raw_artifacts = {
            name: post.file_ref(
                final / "adapter-output/seml0-raw" / name, f"real raw {name}"
            )
            for name in (
                "p10-raw-result.json",
                "p10-raw-observations.tsv",
                "p10-raw-phase-events.jsonl",
            )
        }
        provenance_path = final / "adapter-output/adapter-provenance.json"
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        provenance["request"] = request_ref
        provenance["store"]["clone_receipt"] = clone_ref
        provenance["command"]["run_manifest"] = manifest_ref
        provenance["command"]["command_topology"] = topology_ref
        provenance["raw_artifacts"] = raw_artifacts
        binding = provenance["split_phase_binding"]
        binding["request"] = request_ref
        binding["p31_receipt"] = p31_ref
        binding["p31_run_manifest"] = manifest_ref
        binding["command_topology"] = topology_ref
        binding["adapter_result"] = validated_artifacts["adapter-result.json"]
        binding["validated_artifacts"] = validated_artifacts
        binding["clone_receipt"] = clone_ref
        binding["final_cell_root"] = str(final.resolve())
        binding["campaign_root"] = str(final.parent.parent.resolve())
        write_json(provenance_path, provenance)
        provenance_ref = post.file_ref(provenance_path, "real provenance")
        validated_path = final / "adapter-output/validated-repeat.json"
        validated = json.loads(validated_path.read_text(encoding="utf-8"))
        validated["request"] = request_ref
        validated["final_cell_root"] = str(final.resolve())
        validated["p31"]["receipt"] = p31_ref
        validated["p31"]["run_manifest"] = manifest_ref
        validated["p31"]["command_topology"] = topology_ref
        validated["process_lifetime_binding"]["command_topology"] = topology_ref
        validated["adapter_artifacts"] = {
            **validated_artifacts,
            "adapter-provenance.json": provenance_ref,
        }
        validated["adapter_provenance"] = provenance
        write_json(validated_path, validated)
        validated_ref = post.file_ref(validated_path, "real validated repeat")
        validated_receipt_path = final / post.RECEIPT_PATHS["validated_result"]
        validated_receipt = json.loads(
            validated_receipt_path.read_text(encoding="utf-8")
        )
        validated_receipt["request"] = request_ref
        validated_receipt["p31_receipt"] = p31_ref
        validated_receipt["command_topology"] = topology_ref
        validated_receipt["adapter_result"] = validated_ref
        validated_receipt["adapter_provenance"] = provenance_ref
        write_json(validated_receipt_path, validated_receipt)

        done_path = final / "CELL-DONE.json"
        done = json.loads(done_path.read_text(encoding="utf-8"))
        for role, relative in post.RECEIPT_PATHS.items():
            ref = post.file_ref(final / relative, f"real {role}")
            done["receipts"][role] = {
                "path": relative,
                "sha256": ref["sha256"],
                "size_bytes": ref["size_bytes"],
            }
        write_json(done_path, done)
        refs = {
            role: post.file_ref(final / relative, f"real {role}")
            for role, relative in post.RECEIPT_PATHS.items()
        }
        receipt_values = {
            role: post.validate_role_receipt(
                Path(ref["path"]),
                role,
                key=done["cell_key"],
                ordinal=done["ordinal"],
                plan_sha=done["backend_plan_sha256"],
                target_p02b=clone["target_p02b"],
            )
            for role, ref in refs.items()
        }
        validated_receipt = receipt_values["validated_result"]
        validated = post.load_json(
            Path(validated_receipt["adapter_result"]["path"]), "real validated repeat"
        )
        provenance = post.load_json(
            Path(validated_receipt["adapter_provenance"]["path"]), "real provenance"
        )
        post.validate_cross_role_bindings(
            final=final,
            refs=refs,
            receipt_values=receipt_values,
            validated=validated,
            provenance=provenance,
            target_p02b=clone["target_p02b"],
            key=done["cell_key"],
        )

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
                    self.assert_both_consumers_reject(
                        completed, role, value, f"{role}-{mutation}"
                    )

    def test_cross_role_semantic_rehash_attacks_are_rejected(self) -> None:
        cases = (
            ("prepared_command", "request"),
            ("prepared_command", "binary_argv"),
            ("prepared_command", "p31_wrapper"),
            ("prepared_command", "p31_binary_suffix"),
            ("p31", "command_topology"),
            ("command_topology", "run_manifest"),
            ("command_topology", "root_pid"),
            ("command_topology", "argv"),
            ("store_clone", "tree"),
            ("cleanup", "clone_target"),
        )
        for role, mutation in cases:
            with self.subTest(role=role, mutation=mutation):
                completed = attach_completed_evidence(self.root, self.hold)
                evidence = completed["incremental_plan"]["incremental_evidence"]
                cell = evidence["cells"][0]
                final = Path(cell["cell_done"]["path"]).parent
                path = final / post.RECEIPT_PATHS[role]
                value = json.loads(path.read_text(encoding="utf-8"))
                if mutation == "request":
                    source = Path(value["request"]["path"])
                    alternate = final / "alternate-request.json"
                    write_json(
                        alternate,
                        json.loads(source.read_text(encoding="utf-8")),
                    )
                    value["request"] = post.file_ref(alternate, "alternate request")
                elif mutation == "binary_argv":
                    value["binary_argv"] = ["/bin/false"]
                elif mutation == "p31_wrapper":
                    alternate = final / "alternate-p31-wrapper.sh"
                    alternate.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    value["p31_argv"][0] = str(alternate.resolve())
                elif mutation == "p31_binary_suffix":
                    value["p31_argv"][-1] = "/bin/false"
                elif mutation == "command_topology":
                    source = final / post.RECEIPT_PATHS["command_topology"]
                    alternate = final / "alternate-topology.json"
                    write_json(
                        alternate,
                        json.loads(source.read_text(encoding="utf-8")),
                    )
                    value["command_topology"] = post.file_ref(
                        alternate, "alternate topology"
                    )
                elif mutation == "run_manifest":
                    source = Path(value["run_manifest"]["path"])
                    alternate = final / "alternate-run-manifest.json"
                    write_json(
                        alternate,
                        json.loads(source.read_text(encoding="utf-8")),
                    )
                    value["run_manifest"] = post.file_ref(
                        alternate, "alternate run manifest"
                    )
                elif mutation == "root_pid":
                    value["root_pid"] += 1
                elif mutation == "argv":
                    value["argv"] = ["/bin/false"]
                    value["argv_sha256"] = hashlib.sha256(
                        json.dumps(
                            value["argv"], sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest()
                elif mutation == "tree":
                    value["source_tree_sha256"] = "f" * 64
                    value["verification"]["clone_tree"]["sha256"] = "f" * 64
                elif mutation == "clone_target":
                    value["mutable_clone"] = str(
                        (final / "other-absent-clone").resolve()
                    )
                self.assert_both_consumers_reject(
                    completed, role, value, f"{role}-{mutation}"
                )


if __name__ == "__main__":
    unittest.main()
