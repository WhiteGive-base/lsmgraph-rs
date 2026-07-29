#!/usr/bin/env python3
"""Synthetic tests for the E01 mixed-lineage composition/normalizer bridge."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Tuple


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import build_e01_mixed_lineage as builder
import normalize_e01_mixed_lineage as normalizer


DENSE_SHA = "9727fed3d710a3f0897b1ee7379411a091e0568bef05902de8b1787efc39d258"
NAIVE_SHA = "1727fed3d710a3f0897b1ee7379411a091e0568bef05902de8b1787efc39d258"
SOURCE_SHA = "baa7c4b9701936253ebaeb4b3436c16403373af0b48c4276dacef780479952c5"
TRUTH_SHA = "876ec4be45bb7c220ce595b8d8efd5c79d13db285569cc0d6ae3e8bc19a1c788"
DIGEST_SHA = "67ed60fd93ac69e52675a8f89311b4413d88797b240376868e6109a3828539cf"
HOST_SHA = "f460de3abf394d86cb8b1730a2db7e676dd75f20f045ef04a3ba26423bf11122"


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_json(root: Path, relative: str, value: Dict[str, Any]) -> Dict[str, Any]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.write_bytes(payload)
    return {"path": str(path.resolve()), "sha256": _sha(payload), "size_bytes": len(payload)}


def write_text(root: Path, relative: str, value: str) -> Dict[str, Any]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = value.encode()
    path.write_bytes(payload)
    return {"path": str(path.resolve()), "sha256": _sha(payload), "size_bytes": len(payload)}


def make_fixture(root: Path) -> Tuple[Path, Path, Path]:
    dataset_manifest = root / "lineage/sf10-dataset-manifest.json"
    write_json(
        root,
        "lineage/sf10-dataset-manifest.json",
        {
            "schema_version": "p02b-dataset-manifest-v1",
            "dataset_root": "/fixture/ldbc-sf10",
            "dataset_sha256": SOURCE_SHA,
            "file_count": 166,
            "total_bytes": 9492415008,
            "hash_method": "sha256-tree-v1(relative-path,size,file-sha256)",
        },
    )
    dense_summary = root / "lineage/convert-summary.json"
    write_json(
        root,
        "lineage/convert-summary.json",
        {
            "input": "/fixture/edges-raw.tsv",
            "output": "/fixture/edges-dense.txt",
            "vertex_count": 29987835,
            "edge_count": 355185382,
            "edge_types": {"1": 1},
        },
    )
    repeats = []
    for system in builder.LEGACY_SYSTEMS:
        for repeat in (1, 2, 3):
            prefix = f"cells/{system}/r{repeat}"
            provenance = write_json(
                root,
                f"{prefix}/adapter-provenance.json",
                {"schema_version": "fixture-provenance-v1", "state": "PASS"},
            )
            request_value = {
                "schema_version": "cidr-p10-adapter-request-v2",
                "system_id": system,
                "repeat_index": repeat,
                "execution_mode": "formal",
                "interface_scope": "typed-neighbor-dense-id-v1",
                "dataset": {
                    "path": "/fixture/input",
                    "sha256": SOURCE_SHA if system == "neo4j" else DENSE_SHA,
                },
                "truth": {
                    "path": "/fixture/truth.tsv",
                    "sha256": TRUTH_SHA,
                    "query_count": 1700,
                },
                "binary": {
                    "path": "/fixture/bin",
                    "sha256": ("%064x" % (builder.LEGACY_SYSTEMS.index(system) + 1)),
                },
                "process_lifetime": "fixture-process-lifetime-v1",
                "timing": {
                    "warmup_passes": 1,
                    "measured_passes": 1,
                    "per_query_timeout_ms": 30000,
                    "clock": "CLOCK_MONOTONIC",
                    "concurrency": 1,
                    "timing_boundary": "typed-neighbor-call-plus-result-materialization-and-digest-v1",
                },
            }
            request = write_json(root, f"{prefix}/adapter-request.json", request_value)
            validation_state = "FAILED" if system in ("neo4j", "nebulagraph") else "PASS"
            validation = write_json(
                root,
                f"{prefix}/p31/validation.json",
                {
                    "schema_version": "cidr-run-manifest-v1",
                    "state": validation_state,
                    "errors": [] if validation_state == "PASS" else ["fixture preserved failure"],
                    "warnings": [],
                },
            )
            done = write_json(
                root,
                f"{prefix}/p31/DONE",
                {"state": "PASS", "validation_sha256": validation["sha256"]},
            )
            validated_value = {
                "schema_version": "cidr-p10-validated-repeat-v1",
                "system_id": system,
                "display_name": system,
                "repeat_index": repeat,
                "system_version": f"{system}-fixture-v1",
                "interface_scope": "typed-neighbor-dense-id-v1",
                "concurrency": 1,
                "query_count": 1700,
                "completed_queries": 1700,
                "timeout_queries": 0,
                "mismatch_queries": 0,
                "expected_digest_sha256": DIGEST_SHA,
                "actual_digest_sha256": DIGEST_SHA,
                "latency_p50_us": 100.0 + repeat,
                "latency_p95_us": 200.0 + repeat,
                "latency_p99_us": 300.0 + repeat,
                "measurement_s": 30.0,
                "warmup_s": 30.0,
                "qps": 1700.0 / 30.0,
                "warmup_passes": 1,
                "measured_passes": 1,
                "per_query_timeout_ms": 30000,
                "clock": "CLOCK_MONOTONIC",
                "timing_boundary": "typed-neighbor-call-plus-result-materialization-and-digest-v1",
                "process_lifetime": "fixture-process-lifetime-v1",
                "import_wall_s": 10.0,
                "import_store_logical_bytes": 1000,
                "import_store_allocated_bytes": 1024,
                "request": {"path": request["path"], "sha256": request["sha256"]},
                "adapter_artifacts": {"adapter-provenance.json": provenance},
                "p31": {
                    "host": {"fingerprint_sha256": HOST_SHA},
                    "repo": {"git_sha": "22cb1e7bae51904f9e45b03d8fe38b4ef0a20f64"},
                    "validation_sha256": validation["sha256"],
                },
            }
            validated = write_json(root, f"{prefix}/validated-result.json", validated_value)
            failed = write_text(root, f"roots/{system}/FAILED", "preserved\n")
            item: Dict[str, Any] = {
                "system_id": system,
                "repeat_index": repeat,
                "source_root": str((root / f"roots/{system}").resolve()),
                "source_root_failed_marker_preserved": failed,
                "validated_result": validated,
            }
            if system in ("neo4j", "nebulagraph"):
                p31_failed = write_text(root, f"{prefix}/p31/FAILED", "preserved\n")
                conditional = write_json(
                    root,
                    f"{prefix}/conditional-p31-revalidation.json",
                    {
                        "schema_version": "fixture-read-only-revalidation-v1",
                        "state": "PASS",
                        "formal_eligible": False,
                        "performance_eligible": False,
                        "paper_claim_eligible": False,
                        "original_artifacts_modified": False,
                        "adapter_provenance": provenance,
                    },
                )
                post = write_json(
                    root,
                    f"{prefix}/post-timing-revalidation.json",
                    {
                        "schema_version": "fixture-post-timing-v1",
                        "state": "PASS",
                        "formal_eligible": False,
                        "performance_eligible": False,
                        "paper_claim_eligible": False,
                    },
                )
                item.update(
                    {
                        "conditional_p31_revalidation": conditional,
                        "post_timing_revalidation": post,
                        "original_p31_validation_preserved": validation,
                        "original_p31_failed_marker_preserved": p31_failed,
                    }
                )
            else:
                item.update({"p31_validation": validation, "p31_done": done})
            repeats.append(item)
    source = root / "L5-CONDITIONAL-COMBINED.json"
    source_value = {
        "schema_version": builder.SOURCE_SCHEMA,
        "state": "PASS",
        "candidate_head": "22cb1e7bae51904f9e45b03d8fe38b4ef0a20f64",
        "completed_at_utc": "2026-07-25T00:00:00Z",
        "repeat_count": 18,
        "systems": list(builder.LEGACY_SYSTEMS),
        "repeats": repeats,
        "source_data_copied": False,
        "source_failed_markers_preserved": True,
        "formal_eligible": False,
        "performance_eligible": False,
        "paper_claim_eligible": False,
    }
    source.write_text(json.dumps(source_value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return source, dataset_manifest, dense_summary


def attach_completed_evidence(root: Path, value: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(value)
    result["state"] = "PASS"
    result["incremental_plan"]["state"] = "PASS"
    repo_root = root / "fresh/repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    id_map_root = root / "fresh/id-map"
    id_map_root.mkdir(parents=True, exist_ok=True)
    binary = write_text(root, "fresh/bin/lsmgraph", "fixture-binary\n")
    truth = write_text(root, "fresh/truth.tsv", "edge_type\tsrc\tdigest\n1\t1\t0\n")
    p31_wrapper = write_text(root, "fresh/run_with_resources.sh", "#!/bin/sh\n")
    adapter_tool = write_text(root, "fresh/seml0_adapter.py", "# fixture\n")
    backend_plan = write_json(root, "fresh/backend-plan.json", {"state": "READY"})
    repo_head = "a" * 40
    gates = {}
    target_refs = {}
    target_bundles = {}
    for variant, tree_sha, hint in (
        ("budg-b64", DENSE_SHA, True),
        ("naive", NAIVE_SHA, False),
    ):
        lease = write_json(root, f"fresh/p02b/{variant}-lease.json", {"schema_version": "cidr-batch-lease-v2", "state": "PASS"})
        query = write_json(root, f"fresh/p02b/{variant}-plan.json", {"semantic_degree_hint": hint})
        store_manifest = write_json(
            root, f"fresh/p02b/{variant}-store-manifest.json", {"state": "PASS"}
        )
        tree = {
            "sha256": tree_sha,
            "full_tree_hash_performed": True,
            "file_count": 1,
            "total_bytes": 1,
        }
        target_bundles[variant] = {
            "schema_version": normalizer.TARGET_P02B_SCHEMA,
            "state": "PASS",
            "variant": variant,
            "store_unchanged": True,
            "store_pre": tree,
            "store_post": tree,
            "lease": lease,
            "static_inputs": {
                "query_plan": query,
                "repo_head": repo_head,
                "store_manifest": store_manifest,
                "store_tree_sha256": tree_sha,
                "bound_inputs": {
                    "repo_root": {"path": str(repo_root.resolve())},
                    "binary": binary,
                    "truth": truth,
                    "id_map_dir": {"path": str(id_map_root.resolve())},
                    "p31_wrapper": p31_wrapper,
                },
            },
        }
        target_refs[variant] = write_json(
            root,
            f"fresh/p02b/{variant}.json",
            target_bundles[variant],
        )
    for gate in normalizer.REQUIRED_GATES:
        body = {"state": "PASS", "gate": gate}
        if gate == "fresh_p02b":
            body["target_p02b"] = target_refs
        receipt = write_json(root, f"fresh/gates/{gate}.json", body)
        gates[gate] = {"state": "PASS", "receipt": receipt}
    cells = []
    for ordinal, (key, system, repeat, role, included) in enumerate(
        builder.INCREMENTAL_CELLS, start=1
    ):
        safe = key.replace(":", "-")
        target_variant = "budg-b64" if key == "seml0:bridge-canary" else "naive"
        final_relative = f"fresh/campaign/cells/{ordinal:02d}-{safe}"
        final_root = root / final_relative
        request = write_json(
            root,
            f"{final_relative}/adapter-request.json",
            {
                "execution_mode": "formal",
                "process_lifetime": "prebuilt-store-query-process-lifetime-v1",
                "binary": {"path": binary["path"], "sha256": binary["sha256"]},
                "truth": {
                    "path": truth["path"],
                    "sha256": truth["sha256"],
                    "query_count": 1700,
                },
            },
        )
        clone = write_json(
            root,
            f"{final_relative}/receipts/store-clone.json",
            {"state": "PASS", "source_tree_sha256": target_bundles[target_variant]["store_pre"]["sha256"]},
        )
        run_manifest = write_json(
            root,
            f"{final_relative}/p31/run-manifest.json",
            {"state": "PASS", "root_pid": 12345, "host": {"fingerprint_sha256": HOST_SHA}},
        )
        argv = ["/bin/true"]
        argv_sha = hashlib.sha256(
            json.dumps(argv, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        topology = write_json(
            root,
            f"{final_relative}/receipts/command-topology.json",
            {
                "state": "PASS",
                "root_pid": 12345,
                "argv": argv,
                "argv_sha256": argv_sha,
                "process_lifetime": "prebuilt-store-query-process-lifetime-v1",
            },
        )
        p31 = write_json(
            root,
            f"{final_relative}/receipts/p31.json",
            {
                "state": "PASS",
                "run_manifest": run_manifest,
                "command_topology": topology,
            },
        )
        adapter_result = write_json(
            root, f"{final_relative}/adapter-output/adapter-result.json", {"state": "PASS"}
        )
        observations = write_text(
            root, f"{final_relative}/adapter-output/query-observations.tsv", "status\nok\n"
        )
        events = write_text(
            root, f"{final_relative}/adapter-output/phase-events.jsonl", "{}\n"
        )
        raw_result = write_json(
            root,
            f"{final_relative}/adapter-output/seml0-raw/p10-raw-result.json",
            {"state": "PASS"},
        )
        raw_observations = write_text(
            root,
            f"{final_relative}/adapter-output/seml0-raw/p10-raw-observations.tsv",
            "status\nok\n",
        )
        raw_events = write_text(
            root,
            f"{final_relative}/adapter-output/seml0-raw/p10-raw-phase-events.jsonl",
            "{}\n",
        )
        validated_artifacts = {
            "adapter-result.json": adapter_result,
            "query-observations.tsv": observations,
            "phase-events.jsonl": events,
        }
        provenance_value = {
            "schema_version": "p10-seml0-adapter-provenance-v1",
            "mode": "formal",
            "variant": target_variant,
            "process_lifetime": "prebuilt-store-query-process-lifetime-v1",
            "request": request,
            "repo": {
                "root": str(repo_root.resolve()),
                "head": repo_head,
                "clean": True,
                "status_sha256": hashlib.sha256(b"").hexdigest(),
            },
            "binary": binary,
            "store": {
                "tree_sha256": target_bundles[target_variant]["store_pre"]["sha256"],
                "manifest": target_bundles[target_variant]["static_inputs"]["store_manifest"],
                "clone_receipt": clone,
                "mutable_clone_removed_before_cell_publication": True,
            },
            "truth": truth,
            "sample_plan": {
                **target_bundles[target_variant]["static_inputs"]["query_plan"],
                "query_count": 1700,
            },
            "id_map": {
                "directory": str(id_map_root.resolve()),
                "bound_by_target_p02b": target_refs[target_variant],
            },
            "p02b": {"target_bundle": target_refs[target_variant]},
            "p31_wrapper": p31_wrapper,
            "command": {
                "argv": argv,
                "argv_sha256": argv_sha,
                "invocations": 1,
                "exit_code": 0,
                "root_pid": 12345,
                "run_manifest": run_manifest,
                "command_topology": topology,
            },
            "raw_artifacts": {
                "p10-raw-result.json": raw_result,
                "p10-raw-observations.tsv": raw_observations,
                "p10-raw-phase-events.jsonl": raw_events,
            },
            "split_phase_binding": {
                "schema_version": "cidr-e01-split-phase-provenance-binding-v1",
                "backend_plan": backend_plan,
                "target_p02b": target_refs[target_variant],
                "request": request,
                "p31_receipt": p31,
                "p31_run_manifest": run_manifest,
                "command_topology": topology,
                "adapter_tool": adapter_tool,
                "adapter_result": adapter_result,
                "validated_artifacts": validated_artifacts,
                "clone_receipt": clone,
                "cell_key": key,
                "ordinal": ordinal,
                "campaign_root": str((root / "fresh/campaign").resolve()),
                "final_cell_root": str(final_root.resolve()),
            },
        }
        provenance = write_json(
            root,
            f"{final_relative}/adapter-output/adapter-provenance.json",
            provenance_value,
        )
        validated = write_json(
            root,
            f"{final_relative}/adapter-output/validated-repeat.json",
            {
                "state": "PASS",
                "backend_plan": backend_plan,
                "target_p02b": target_refs[target_variant],
                "request": request,
                "p31": {
                    "receipt": p31,
                    "run_manifest": run_manifest,
                    "host": {"fingerprint_sha256": HOST_SHA},
                    "command_topology": topology,
                },
                "process_lifetime_binding": {"command_topology": topology},
                "adapter_artifacts": {
                    **validated_artifacts,
                    "adapter-provenance.json": provenance,
                },
                "adapter_provenance": provenance_value,
            },
        )
        cleanup = write_json(
            root, f"{final_relative}/receipts/cleanup.json", {"state": "PASS"}
        )
        cells.append(
            {
                "ordinal": ordinal,
                "cell_key": key,
                "system_id": system,
                "repeat_index": repeat,
                "role": role,
                "included_in_figure_rows": included,
                "classification": {
                    "formal_eligible": True,
                    "performance_eligible": True,
                    "paper_claim_eligible": False,
                },
                "outcome": {
                    "state": "PASS",
                    "mismatch_queries": 0,
                    "timeout_queries": 0,
                },
                "validated_result": validated,
                "p31_receipt": p31,
                "cleanup_receipt": cleanup,
                "prepared_request": request,
                "store_clone_receipt": clone,
                "command_topology": topology,
                "adapter_tool": adapter_tool,
                "adapter_provenance": provenance,
                "target_p02b": target_refs[target_variant],
                "target_query_plan": target_bundles[target_variant]["static_inputs"]["query_plan"],
                "target_lease": target_bundles[target_variant]["lease"],
                "identity": {
                    "host_fingerprint": HOST_SHA,
                    "git_sha": repo_head,
                    "binary_sha256": binary["sha256"],
                    "physical_input_sha256": DENSE_SHA if key == "seml0:bridge-canary" else NAIVE_SHA,
                    "truth_sha256": truth["sha256"],
                },
                "protocol": {
                    "interface_scope": "typed-neighbor-dense-id-v1",
                    "concurrency": 1,
                    "query_count": 1700,
                    "warmup_passes": 1,
                    "measured_passes": 1,
                    "process_lifetime": "prebuilt-store-query-process-lifetime-v1",
                },
                "metrics": {
                    "measurement_s": 30.0,
                    "warmup_s": 30.0,
                    "latency_p50_us": 1.0,
                    "latency_p95_us": 2.0,
                    "latency_p99_us": 3.0,
                    "qps": 1700.0 / 30.0,
                    "completed_queries": 1700,
                },
            }
        )
    contract_sha = result["incremental_plan"][
        "bridge_canary_comparability_contract"
    ]["contract_sha256"]
    canary_receipt = write_json(
        root,
        "fresh/canary-comparability.json",
        {"state": "PASS", "contract_sha256": contract_sha},
    )
    result["incremental_plan"]["incremental_evidence"] = {
        "state": "PASS",
        "campaign_gates": gates,
        "cells": cells,
        "bridge_canary_comparability": {
            "state": "PASS",
            "canary_cell_key": "seml0:bridge-canary",
            "legacy_reference_keys": ["seml0:r1", "seml0:r2", "seml0:r3"],
            "pre_registered_rule": True,
            "contract_sha256": contract_sha,
            "receipt": canary_receipt,
        },
    }
    return result


class MixedLineageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="e01-mixed-")
        self.root = Path(self.temporary.name)
        self.source, self.dataset_manifest, self.dense_summary = make_fixture(self.root)
        self.value = builder.build_composition(
            self.source,
            self.dataset_manifest,
            self.dense_summary,
            created_at_utc="2026-07-27T00:00:00Z",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_composition(self, value: Dict[str, Any], name: str = "composition.json") -> Path:
        path = self.root / name
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def test_builder_freezes_legacy18_and_minimal_four_cell_plan(self) -> None:
        self.assertEqual(self.value["state"], "HOLD")
        self.assertEqual(len(self.value["legacy_cells"]), 18)
        self.assertEqual(
            [cell["cell_key"] for cell in self.value["incremental_plan"]["cells"]],
            [item[0] for item in builder.INCREMENTAL_CELLS],
        )
        self.assertEqual(self.value["incremental_plan"]["figure_row_count"], 3)
        self.assertEqual(self.value["incremental_plan"]["canary_count"], 1)
        self.assertTrue(
            self.value["incremental_plan"]["bridge_canary_comparability_contract"][
                "frozen_before_timing"
            ]
        )
        self.assertFalse(self.value["classification"]["formal_eligible"])

    def test_builder_discloses_two_physical_representations(self) -> None:
        logical = self.value["logical_dataset_identity"]
        self.assertFalse(logical["physical_byte_identity_required"])
        self.assertEqual(
            {item["sha256"] for item in logical["physical_representations"]},
            {DENSE_SHA, SOURCE_SHA},
        )
        neo = next(item for item in logical["physical_representations"] if item["sha256"] == SOURCE_SHA)
        self.assertEqual(neo["systems"], ["neo4j"])

    def test_builder_freezes_direct_and_read_only_p31_bridges(self) -> None:
        modes = {
            cell["system_id"]: cell["receipts"]["p31_bridge"]["mode"]
            for cell in self.value["legacy_cells"]
        }
        self.assertEqual(modes["seml0"], "direct_pass_receipts")
        self.assertEqual(modes["neo4j"], "read_only_post_timing_revalidation")
        self.assertEqual(modes["nebulagraph"], "read_only_post_timing_revalidation")

    def test_builder_rejects_validated_receipt_drift(self) -> None:
        first = json.loads(self.source.read_text(encoding="utf-8"))["repeats"][0]
        Path(first["validated_result"]["path"]).write_text("{}\n", encoding="utf-8")
        with self.assertRaises(builder.CompositionError):
            builder.build_composition(self.source, self.dataset_manifest, self.dense_summary)

    def test_pending_plan_is_pre_output_rejected_without_directory(self) -> None:
        path = self.write_composition(self.value)
        output = self.root / "must-not-exist"
        with self.assertRaises(builder.CompositionError):
            normalizer.normalize(path, output)
        self.assertFalse(output.exists())

    def test_completed_incremental_evidence_normalizes_legacy18_plus_fresh3(self) -> None:
        completed = attach_completed_evidence(self.root, self.value)
        path = self.write_composition(completed)
        result = normalizer.normalize(path, None)
        self.assertEqual(len(result["rows"]), 21)
        self.assertEqual(
            sum(row["lineage_class"] == "legacy_validated" for row in result["rows"]),
            18,
        )
        self.assertEqual(
            sum(row["lineage_class"] == "fresh_incremental_formal" for row in result["rows"]),
            3,
        )
        self.assertFalse(result["receipt"]["classification"]["formal_eligible"])

    def test_mixed_consumer_rejects_consistent_provenance_chain_tamper(self) -> None:
        completed = attach_completed_evidence(self.root, self.value)
        cell = completed["incremental_plan"]["incremental_evidence"]["cells"][0]
        provenance_path = Path(cell["adapter_provenance"]["path"])
        validated_path = Path(cell["validated_result"]["path"])
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        provenance["mode"] = "fixture"
        provenance_payload = (
            json.dumps(provenance, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        provenance_path.write_bytes(provenance_payload)
        provenance_ref = {
            "path": str(provenance_path.resolve()),
            "sha256": _sha(provenance_payload),
            "size_bytes": len(provenance_payload),
        }
        validated = json.loads(validated_path.read_text(encoding="utf-8"))
        validated["adapter_provenance"] = provenance
        validated["adapter_artifacts"]["adapter-provenance.json"] = provenance_ref
        validated_payload = (
            json.dumps(validated, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        validated_path.write_bytes(validated_payload)
        cell["adapter_provenance"] = provenance_ref
        cell["validated_result"] = {
            "path": str(validated_path.resolve()),
            "sha256": _sha(validated_payload),
            "size_bytes": len(validated_payload),
        }
        path = self.write_composition(completed)
        with self.assertRaisesRegex(builder.CompositionError, "top-level identity drift"):
            normalizer.normalize(path, None)

    def test_missing_fresh_gate_is_pre_output_rejected(self) -> None:
        completed = attach_completed_evidence(self.root, self.value)
        del completed["incremental_plan"]["incremental_evidence"]["campaign_gates"][
            "fresh_batch_lease"
        ]
        path = self.write_composition(completed)
        with self.assertRaises(builder.CompositionError):
            normalizer.normalize(path, None)

    def test_target_p02b_cell_backlink_tamper_is_rejected(self) -> None:
        completed = attach_completed_evidence(self.root, self.value)
        completed["incremental_plan"]["incremental_evidence"]["cells"][1]["target_lease"] = (
            completed["incremental_plan"]["incremental_evidence"]["cells"][0]["target_lease"]
        )
        path = self.write_composition(completed)
        with self.assertRaises(builder.CompositionError):
            normalizer.normalize(path, None)

    def test_bridge_canary_cannot_become_a_figure_row(self) -> None:
        completed = attach_completed_evidence(self.root, self.value)
        completed["incremental_plan"]["incremental_evidence"]["cells"][0][
            "included_in_figure_rows"
        ] = True
        path = self.write_composition(completed)
        with self.assertRaises(builder.CompositionError):
            normalizer.normalize(path, None)

    def test_frozen_bridge_canary_contract_drift_is_rejected(self) -> None:
        completed = attach_completed_evidence(self.root, self.value)
        completed["incremental_plan"]["bridge_canary_comparability_contract"][
            "performance_rule"
        ]["completed_qps_min"] = 0.10
        path = self.write_composition(completed)
        with self.assertRaises(builder.CompositionError):
            normalizer.normalize(path, None)

    def test_legacy_eligibility_cannot_be_promoted(self) -> None:
        completed = attach_completed_evidence(self.root, self.value)
        completed["legacy_cells"][0]["classification"]["formal_eligible"] = True
        path = self.write_composition(completed)
        with self.assertRaises(builder.CompositionError):
            normalizer.normalize(path, None)

    def test_real_snapshot_is_hold_and_binds_frozen_legacy_source(self) -> None:
        snapshot = ROOT / "E01-mixed-lineage-plan-v1.json"
        if not snapshot.exists():
            self.skipTest("real read-only snapshot is not installed in this checkout")
        value = json.loads(snapshot.read_text(encoding="utf-8"))
        self.assertEqual(value["state"], "HOLD")
        self.assertEqual(
            value["legacy_source"]["sha256"],
            "3996ab4b8d99bb192904db850e418f4d66b518be6d771cec2e8fbcc9340c9138",
        )
        self.assertEqual(len(value["legacy_cells"]), 18)
        self.assertEqual(
            [cell["cell_key"] for cell in value["incremental_plan"]["cells"]],
            [item[0] for item in builder.INCREMENTAL_CELLS],
        )
        self.assertIsNone(value["incremental_plan"]["incremental_evidence"])


if __name__ == "__main__":
    unittest.main()
