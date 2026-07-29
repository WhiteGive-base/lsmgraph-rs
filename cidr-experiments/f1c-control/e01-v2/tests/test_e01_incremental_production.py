#!/usr/bin/env python3
"""Negative and state-machine tests for the production backend."""

from __future__ import annotations

import hashlib
import json
import os
import statistics
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import run_e01_incremental_production as production
import build_e01_incremental_ready_backend as builder
import evaluate_e01_bridge_canary as evaluator


def write_json(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


class IncrementalProductionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.campaign = self.root / "campaign"
        self.plan_sha = "a" * 64
        executor = self.root / "phase-executor.py"
        executor.write_text("#!/usr/bin/python3\n", encoding="utf-8")
        self.executor_ref = {
            "path": str(executor.resolve()),
            "sha256": production.sha256_file(executor),
            "size_bytes": executor.stat().st_size,
        }
        scheduler = Path(production.__file__).resolve()
        self.scheduler_ref = {
            "path": str(scheduler),
            "sha256": production.sha256_file(scheduler),
            "size_bytes": scheduler.stat().st_size,
        }
        evaluator_path = Path(evaluator.__file__).resolve()
        self.evaluator_ref = {
            "path": str(evaluator_path),
            "sha256": production.sha256_file(evaluator_path),
            "size_bytes": evaluator_path.stat().st_size,
        }
        frozen_mixed_path = ROOT / "E01-mixed-lineage-plan-v1.json"
        mixed_value = json.loads(frozen_mixed_path.read_text(encoding="utf-8"))
        mixed_value["logical_dataset_identity"]["truth_sha256"] = hashlib.sha256(
            b"edge_type\tsrc\tdigest\n1\t1\t0\n"
        ).hexdigest()
        mixed_path = write_json(self.root / "mixed-lineage.json", mixed_value)
        self.mixed_ref = production.external_file_ref(mixed_path)
        self.formal_contract = mixed_value["incremental_plan"][
            "bridge_canary_comparability_contract"
        ]
        self.mixed_value = mixed_value
        self.reference_protocol = next(
            cell["protocol"]
            for cell in mixed_value["legacy_cells"]
            if cell["cell_key"] == "seml0:r1"
        )
        self.reference_identity = next(
            cell["identity"]
            for cell in mixed_value["legacy_cells"]
            if cell["cell_key"] == "seml0:r1"
        )
        self.legacy_protocol, self.legacy_request_refs = production._frozen_legacy_protocol(mixed_value)
        self.contract_sha = self.formal_contract["contract_sha256"]
        self.checkpoint = {
            "schema_version": "cidr-e01-incremental-canary-checkpoint-contract-v2",
            "evaluator": self.evaluator_ref,
            "mixed_lineage_plan": self.mixed_ref,
            "comparability_contract_sha256": self.contract_sha,
            "legacy_protocol": self.legacy_protocol,
            "legacy_adapter_requests": self.legacy_request_refs,
            "bridge_request_argv_binding": {
                "request_protocol_exact": True,
                "binary_argv_exact": True,
                "warmup_runs": self.legacy_protocol["warmup_passes"],
                "measured_repeats": self.legacy_protocol["measured_passes"],
                "process_lifetime": self.legacy_protocol["process_lifetime"],
                "per_query_timeout_ms": self.legacy_protocol["per_query_timeout_ms"],
            },
            "evidence_schema": evaluator.CHECKPOINT_EVIDENCE_SCHEMA,
            "evidence_path": str(self.campaign / "CANARY-EVIDENCE.json"),
            "evidence_binding": {
                "cell_key": "seml0:bridge-canary",
                "backend_plan": True,
                "bridge_cell_done": True,
                "bridge_receipts": sorted(production.RECEIPT_PATHS),
            },
            "full_v2_contract": evaluator.full_v2_contract(),
            "validated_output_binding": {
                "receipt_schema": "cidr-e01-incremental-validated-result-receipt-v1",
                "adapter_schema": "cidr-p10-validated-repeat-v1",
                "prepared_request_ref": True,
                "p31_receipt_ref": True,
                "p31_run_manifest_ref": True,
                "command_topology_ref": True,
                "deadline_exact": True,
                "backend_plan_ref": True,
                "target_p02b_ref": True,
                "final_cell_root": True,
            },
            "pending_path": str(self.campaign / "CANARY-PENDING.json"),
            "comparability_path": str(self.campaign / "CANARY-COMPARABILITY.json"),
            "evaluation_path": str(self.campaign / "CANARY-EVALUATION.json"),
            "accepted_path": str(self.campaign / "CANARY-ACCEPTED.json"),
            "first_launch_max_completed_cells": 1,
            "resume_requires_evaluation_state": "PASS",
            "matrix_done_before_acceptance": False,
        }
        self.plan = {
            "schema_version": production.BACKEND_SCHEMA,
            "state": "HOLD",
            "execution_state": "BLOCKED",
            "synthetic_test_only": False,
            "strict_serial": True,
            "campaign_root": str(self.campaign.resolve()),
            "campaign_gates": {"fresh_resource_gate": None},
            "phase_executor": self.executor_ref,
            "production_scheduler": self.scheduler_ref,
            "canary_checkpoint": self.checkpoint,
            "blockers": ["test HOLD"],
            "cells": [
                {
                    "ordinal": ordinal,
                    "cell_key": key,
                    "final_cell_root": str(
                        (self.campaign / "cells" / f"{ordinal:02d}-{key.replace(':', '-')}").resolve()
                    ),
                    "staging_cell_root": str(
                        (self.campaign / "staging" / f"{ordinal:02d}-{key.replace(':', '-')}").resolve()
                    ),
                    "phase_commands": {phase: None for phase in production.PHASE_ORDER},
                }
                for ordinal, key in enumerate(production.CELL_ORDER, start=1)
            ],
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_receipts(self, staging: Path, key: str, ordinal: int) -> None:
        clone = staging / "mutable-store"
        for role, relative in production.RECEIPT_PATHS.items():
            value = {
                "schema_version": f"cidr-e01-incremental-{role.replace('_', '-')}-receipt-v1",
                "state": "PASS",
                "mode": "synthetic",
                "synthetic_test_only": True,
                "fixture_only": True,
                "cell_key": key,
                "ordinal": ordinal,
                "backend_plan_sha256": self.plan_sha,
            }
            if role == "p31":
                value.update(timing_generated=False, binary_only_boundary=True)
            if role == "command_topology":
                value.update(
                    single_binary_invocation=True,
                    warmup_measured_same_process=True,
                    process_model="single-storage-bench-process-warmup-and-measured-v1",
                    per_query_timeout_ms=self.legacy_protocol["per_query_timeout_ms"],
                )
            if role == "correctness":
                value.update(mismatch_queries=0, timeout_queries=0)
            if role == "cleanup":
                value.update(mutable_clone_removed=True, mutable_clone=str(clone.resolve()))
            write_json(staging / relative, value)

    def initialize_root(self) -> None:
        (self.campaign / "cells").mkdir(parents=True)
        (self.campaign / "staging").mkdir()
        write_json(
            self.campaign / "MATRIX-START.json",
            {
                "schema_version": production.START_SCHEMA,
                "state": "PASS",
                "strict_serial": True,
                "backend_plan_sha256": self.plan_sha,
            },
        )

    def test_committed_shape_cannot_execute_while_hold(self) -> None:
        value = production.validate_backend_plan(self.plan)
        self.assertEqual(value["state"], "HOLD")
        path = write_json(self.root / "plan.json", self.plan)
        with self.assertRaises(production.BackendError):
            production.execute_production(path)
        self.assertFalse(self.campaign.exists())

    def test_old_v1_schema_and_phase_executor_ref_drift_are_rejected(self) -> None:
        old = json.loads(json.dumps(self.plan))
        old["schema_version"] = "cidr-e01-incremental-backend-plan-v1"
        with self.assertRaisesRegex(production.BackendError, "schema drift"):
            production.validate_backend_plan(old)
        drift = json.loads(json.dumps(self.plan))
        drift["phase_executor"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(production.BackendError, "path/size/SHA drift"):
            production.validate_backend_plan(drift)

    def test_canary_evaluation_attempt_retains_dangling_output(self) -> None:
        output = self.root / "CANARY-EVALUATION.json"
        output.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(evaluator.CanaryError, "retained receipt"):
            evaluator.atomic_write(output, {"state": "PASS"})
        self.assertTrue(output.is_symlink())

    def test_phase_command_key_order_is_irrelevant_but_key_drift_rejected(self) -> None:
        serialized = write_json(self.root / "hold.json", self.plan)
        loaded = json.loads(serialized.read_text(encoding="utf-8"))
        self.assertEqual(
            tuple(loaded["cells"][0]["phase_commands"]),
            ("cleanup", "finalize", "p31", "prepare"),
        )
        production.validate_backend_plan(serialized)
        loaded["cells"][0]["phase_commands"]["extra"] = None
        with self.assertRaisesRegex(production.BackendError, "phase key set drift"):
            production.validate_backend_plan(loaded)

    def test_builder_serialized_v3_ready_plan_dispatches_exact_four_phases(self) -> None:
        query_path = write_json(self.root / "query.json", {})
        query_ref = production.external_file_ref(query_path)
        truth_path = self.root / "truth.tsv"
        truth_path.write_text("edge_type\tsrc\tdigest\n1\t1\t0\n", encoding="utf-8")
        truth_ref = production.external_file_ref(truth_path)
        store_manifest_path = write_json(self.root / "IMMUTABLE-MANIFEST.json", {"state": "PASS"})
        store_manifest_ref = production.external_file_ref(store_manifest_path)
        id_map_dir = self.root / "id-map"
        id_map_dir.mkdir()
        binary_ref = production.external_file_ref(Path("/bin/true"))
        repo_head = "1" * 40
        store_tree_sha = "2" * 64
        lease_path = write_json(
            self.root / "lease.json",
            {"state": "PASS", "expires_at_utc": "2099-01-01T00:00:00Z"},
        )
        lease_ref = production.external_file_ref(lease_path)
        targets = {}
        for variant in {"budg-b64", "naive"}:
            path = write_json(
                self.root / f"{variant}.target.json",
                {
                    "schema_version": production.TARGET_P02B_SCHEMA,
                    "state": "PASS",
                    "variant": variant,
                    "static_inputs": {
                        "query_plan": query_ref,
                        "repo_head": repo_head,
                        "store_manifest": store_manifest_ref,
                        "store_tree_sha256": store_tree_sha,
                        "bound_inputs": {
                            "repo_root": {"path": str(self.root.resolve())},
                            "binary": binary_ref,
                            "truth": truth_ref,
                            "id_map_dir": {"path": str(id_map_dir.resolve())},
                            "p31_wrapper": self.executor_ref,
                        },
                    },
                    "lease": lease_ref,
                },
            )
            targets[variant] = {
                "path": str(path.resolve()),
                "sha256": production.sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        plan_path = self.root / "ready-v3.json"
        gate_path = self.root / "ready-v3.ARMING-GATE.json"
        ready = {
            "schema_version": production.BACKEND_SCHEMA,
            "state": "READY",
            "execution_state": "READY",
            "synthetic_test_only": False,
            "strict_serial": True,
            "campaign_root": str(self.campaign.resolve()),
            "campaign_gates": {"backend_arming": {"path": str(gate_path.resolve())}},
            "phase_executor": self.executor_ref,
            "production_scheduler": self.scheduler_ref,
            "canary_checkpoint": json.loads(json.dumps(self.checkpoint)),
            "blockers": [],
            "cells": [],
        }
        variants = ("budg-b64", "naive", "naive", "naive")
        for ordinal, (key, variant) in enumerate(zip(production.CELL_ORDER, variants), start=1):
            staging = self.campaign / "staging" / f"{ordinal:02d}-{key.replace(':', '-')}"
            final = self.campaign / "cells" / f"{ordinal:02d}-{key.replace(':', '-')}"
            commands = {
                phase_name: [
                    "/usr/bin/python3", "-B", self.executor_ref["path"],
                    "--backend-plan", str(plan_path.resolve()),
                    "--cell-key", key, "--phase", phase_name,
                ]
                for phase_name in production.PHASE_ORDER
            }
            ready["cells"].append(
                {
                    "ordinal": ordinal,
                    "cell_key": key,
                    "staging_cell_root": str(staging.resolve()),
                    "final_cell_root": str(final.resolve()),
                    "phase_commands": commands,
                    "runtime": {
                        "variant": variant,
                        "adapter_tool": self.executor_ref,
                        "target_p02b": targets[variant],
                        "target_query_plan": query_ref,
                        "target_lease": lease_ref,
                        "request": {
                            "execution_mode": "formal",
                            "interface_scope": self.legacy_protocol["interface_scope"],
                            "process_lifetime": self.legacy_protocol["process_lifetime"],
                            "timing": {
                                "warmup_passes": self.legacy_protocol["warmup_passes"],
                                "measured_passes": self.legacy_protocol["measured_passes"],
                                "per_query_timeout_ms": self.legacy_protocol["per_query_timeout_ms"],
                                "clock": self.legacy_protocol["clock"],
                                "concurrency": self.legacy_protocol["concurrency"],
                                "timing_boundary": self.legacy_protocol["timing_boundary"],
                            },
                        },
                        "binary_argv": [
                            "/bin/true",
                            "--warmup-runs",
                            str(self.legacy_protocol["warmup_passes"]),
                            "--repeats",
                            str(self.legacy_protocol["measured_passes"]),
                            "--p10-per-query-timeout-ms",
                            str(self.legacy_protocol["per_query_timeout_ms"]),
                        ],
                    },
                }
            )
        builder.atomic(plan_path, ready)
        plan_ref = production.external_file_ref(plan_path)
        write_json(
            gate_path,
            {
                "state": "PASS",
                "synthetic_test_only": False,
                "fixture_only": False,
                "phase_executor": self.executor_ref,
                "production_scheduler": self.scheduler_ref,
                "canary_evaluator": self.evaluator_ref,
                "canary_checkpoint": ready["canary_checkpoint"],
                "backend_plan": plan_ref,
            },
        )
        reloaded = production.validate_backend_plan(plan_path)
        self.assertEqual(reloaded["schema_version"], production.BACKEND_SCHEMA)
        request_deadline_drift = json.loads(json.dumps(reloaded))
        request_deadline_drift["cells"][0]["runtime"]["request"]["timing"][
            "per_query_timeout_ms"
        ] = 1000
        with self.assertRaises(production.BackendError):
            production.validate_backend_plan(request_deadline_drift)
        argv_deadline_drift = json.loads(json.dumps(reloaded))
        timeout_index = argv_deadline_drift["cells"][0]["runtime"]["binary_argv"].index(
            "--p10-per-query-timeout-ms"
        )
        argv_deadline_drift["cells"][0]["runtime"]["binary_argv"][
            timeout_index + 1
        ] = "1000"
        with self.assertRaises(production.BackendError):
            production.validate_backend_plan(argv_deadline_drift)
        checkpoint_deadline_drift = json.loads(json.dumps(reloaded))
        checkpoint_deadline_drift["canary_checkpoint"][
            "bridge_request_argv_binding"
        ]["per_query_timeout_ms"] = 1000
        with self.assertRaises(production.BackendError):
            production.validate_backend_plan(checkpoint_deadline_drift)
        legacy_request_mismatch = json.loads(json.dumps(reloaded))
        legacy_request_mismatch["canary_checkpoint"]["legacy_adapter_requests"][
            "seml0:r1"
        ]["sha256"] = "0" * 64
        with self.assertRaises(production.BackendError):
            production.validate_backend_plan(legacy_request_mismatch)
        phase_drift = json.loads(json.dumps(reloaded))
        phase_drift["cells"][0]["phase_commands"]["prepare"][2] = "/bin/false"
        with self.assertRaisesRegex(production.BackendError, "dispatch drift"):
            production.validate_backend_plan(phase_drift)
        gate_value = json.loads(gate_path.read_text(encoding="utf-8"))
        gate_drift = json.loads(json.dumps(gate_value))
        gate_drift["backend_plan"]["sha256"] = "0" * 64
        write_json(gate_path, gate_drift)
        with self.assertRaisesRegex(production.BackendError, "backend plan backlink drift"):
            production.validate_backend_plan(plan_path)
        write_json(gate_path, gate_value)
        plan_sha = production.sha256_file(plan_path)
        dispatched = []
        by_staging = {Path(row["staging_cell_root"]): row for row in ready["cells"]}

        def fake_runner(argv, cwd, stdout, stderr):
            row = by_staging[cwd]
            phase_name = argv[-1]
            dispatched.append((row["cell_key"], phase_name))
            final_root = Path(row["final_cell_root"])

            def future_ref(path: Path) -> dict:
                relative = path.resolve().relative_to(cwd.resolve())
                return {
                    "path": str((final_root / relative).resolve()),
                    "sha256": production.sha256_file(path),
                    "size_bytes": path.stat().st_size,
                }

            base = {
                "state": "PASS",
                "mode": "production",
                "synthetic_test_only": False,
                "fixture_only": False,
                "cell_key": row["cell_key"],
                "ordinal": row["ordinal"],
                "backend_plan_sha256": plan_sha,
            }
            roles = {
                "prepare": ("prepared_command", "store_clone"),
                "p31": ("command_topology", "p31"),
                "finalize": ("validated_result", "correctness", "fairness"),
                "cleanup": ("cleanup",),
            }[phase_name]
            for role in roles:
                value = {
                    **base,
                    "schema_version": (
                        f"cidr-e01-incremental-{role.replace('_', '-')}-receipt-v1"
                    ),
                }
                if role in {"store_clone", "p31", "validated_result", "cleanup"}:
                    value["target_p02b"] = row["runtime"]["target_p02b"]
                if role == "prepared_command":
                    request_path = write_json(
                        cwd / "adapter-request.json",
                        {
                            "state": "PASS",
                            "cell_key": row["cell_key"],
                            "execution_mode": "formal",
                            "process_lifetime": self.legacy_protocol["process_lifetime"],
                            "binary": {
                                "path": binary_ref["path"],
                                "sha256": binary_ref["sha256"],
                            },
                            "truth": {
                                "path": truth_ref["path"],
                                "sha256": self.mixed_value[
                                    "logical_dataset_identity"
                                ]["truth_sha256"],
                                "query_count": 1700,
                            },
                            "timing": {
                                "per_query_timeout_ms": self.legacy_protocol[
                                    "per_query_timeout_ms"
                                ]
                            },
                        },
                    )
                    value["request"] = future_ref(request_path)
                    value["binary_argv"] = ["/bin/true"]
                if role == "p31":
                    manifest_path = write_json(
                        cwd / "p31" / "run-manifest.json",
                        {
                            "state": "PASS",
                            "host": {
                                "fingerprint_sha256": self.reference_identity[
                                    "host_fingerprint"
                                ]
                            },
                            "root_pid": 12345,
                            "command_exit_code": 0,
                        },
                    )
                    value.update(timing_generated=True, binary_only_boundary=True)
                    value["run_manifest"] = future_ref(manifest_path)
                    value["command_topology"] = future_ref(
                        cwd / production.RECEIPT_PATHS["command_topology"]
                    )
                if role == "command_topology":
                    observed_argv = ["/bin/true"]
                    value.update(
                        single_binary_invocation=True,
                        warmup_measured_same_process=True,
                        process_model="single-storage-bench-process-warmup-and-measured-v1",
                        warmup_runs=self.legacy_protocol["warmup_passes"],
                        measured_repeats=self.legacy_protocol["measured_passes"],
                        process_lifetime=self.legacy_protocol["process_lifetime"],
                        per_query_timeout_ms=self.legacy_protocol["per_query_timeout_ms"],
                        root_pid=12345,
                        argv=observed_argv,
                        argv_sha256=production.canonical_sha(observed_argv),
                    )
                if role == "correctness":
                    value.update(
                        query_count=1700,
                        mismatch_queries=0,
                        timeout_queries=0,
                    )
                if role == "store_clone":
                    value["source_tree_sha256"] = store_tree_sha
                if role == "validated_result":
                    prepared = json.loads(
                        (cwd / production.RECEIPT_PATHS["prepared_command"]).read_text(
                            encoding="utf-8"
                        )
                    )
                    p31_receipt_path = cwd / production.RECEIPT_PATHS["p31"]
                    p31_receipt = json.loads(
                        p31_receipt_path.read_text(encoding="utf-8")
                    )
                    output = cwd / "adapter-output"
                    raw_adapter_path = write_json(
                        output / "adapter-result.json",
                        {
                            "schema_version": "cidr-p10-adapter-result-v1",
                            "per_query_timeout_ms": self.legacy_protocol[
                                "per_query_timeout_ms"
                            ],
                        },
                    )
                    artifact_path = output / "query-observations.tsv"
                    artifact_path.write_text("status\nok\n", encoding="utf-8")
                    events_path = output / "phase-events.jsonl"
                    events_path.write_text("{}\n", encoding="utf-8")
                    raw_dir = output / "seml0-raw"
                    raw_result_path = write_json(
                        raw_dir / "p10-raw-result.json", {"state": "PASS"}
                    )
                    raw_observations_path = raw_dir / "p10-raw-observations.tsv"
                    raw_observations_path.write_text("status\nok\n", encoding="utf-8")
                    raw_events_path = raw_dir / "p10-raw-phase-events.jsonl"
                    raw_events_path.write_text("{}\n", encoding="utf-8")
                    validated_artifacts = {
                        "adapter-result.json": future_ref(raw_adapter_path),
                        "query-observations.tsv": future_ref(artifact_path),
                        "phase-events.jsonl": future_ref(events_path),
                    }
                    provenance = {
                        "schema_version": production.SEML0_PROVENANCE_SCHEMA,
                        "mode": "formal",
                        "variant": row["runtime"]["variant"],
                        "process_lifetime": self.legacy_protocol["process_lifetime"],
                        "request": prepared["request"],
                        "repo": {
                            "root": str(self.root.resolve()),
                            "head": repo_head,
                            "clean": True,
                            "status_sha256": hashlib.sha256(b"").hexdigest(),
                        },
                        "binary": binary_ref,
                        "store": {
                            "tree_sha256": store_tree_sha,
                            "manifest": store_manifest_ref,
                            "clone_receipt": future_ref(
                                cwd / production.RECEIPT_PATHS["store_clone"]
                            ),
                            "mutable_clone_removed_before_cell_publication": True,
                        },
                        "truth": truth_ref,
                        "sample_plan": {**query_ref, "query_count": 1700},
                        "id_map": {
                            "directory": str(id_map_dir.resolve()),
                            "bound_by_target_p02b": row["runtime"]["target_p02b"],
                        },
                        "p02b": {
                            "target_bundle": row["runtime"]["target_p02b"],
                        },
                        "p31_wrapper": self.executor_ref,
                        "command": {
                            "argv": ["/bin/true"],
                            "argv_sha256": production.canonical_sha(["/bin/true"]),
                            "invocations": 1,
                            "exit_code": 0,
                            "root_pid": 12345,
                            "run_manifest": p31_receipt["run_manifest"],
                            "command_topology": p31_receipt["command_topology"],
                        },
                        "raw_artifacts": {
                            "p10-raw-result.json": future_ref(raw_result_path),
                            "p10-raw-observations.tsv": future_ref(
                                raw_observations_path
                            ),
                            "p10-raw-phase-events.jsonl": future_ref(
                                raw_events_path
                            ),
                        },
                        "split_phase_binding": {
                            "schema_version": production.SPLIT_PROVENANCE_SCHEMA,
                            "backend_plan": plan_ref,
                            "target_p02b": row["runtime"]["target_p02b"],
                            "request": prepared["request"],
                            "p31_receipt": future_ref(p31_receipt_path),
                            "p31_run_manifest": p31_receipt["run_manifest"],
                            "command_topology": p31_receipt["command_topology"],
                            "adapter_tool": row["runtime"]["adapter_tool"],
                            "adapter_result": validated_artifacts[
                                "adapter-result.json"
                            ],
                            "validated_artifacts": validated_artifacts,
                            "clone_receipt": future_ref(
                                cwd / production.RECEIPT_PATHS["store_clone"]
                            ),
                            "cell_key": row["cell_key"],
                            "ordinal": row["ordinal"],
                            "campaign_root": str(self.campaign.resolve()),
                            "final_cell_root": str(final_root.resolve()),
                        },
                    }
                    provenance_path = write_json(
                        output / "adapter-provenance.json", provenance
                    )
                    adapter_artifacts = {
                        **validated_artifacts,
                        "adapter-provenance.json": future_ref(provenance_path),
                    }
                    adapter_path = write_json(
                        output / "validated-repeat.json",
                        {
                            "schema_version": "cidr-p10-validated-repeat-v1",
                            "system_id": "seml0",
                            "cell_key": row["cell_key"],
                            "ordinal": row["ordinal"],
                            "final_cell_root": str(final_root.resolve()),
                            "backend_plan": plan_ref,
                            "target_p02b": row["runtime"]["target_p02b"],
                            "request": prepared["request"],
                            "p31": {
                                "receipt": future_ref(p31_receipt_path),
                                "run_manifest": p31_receipt["run_manifest"],
                                "host": {
                                    "fingerprint_sha256": self.reference_identity[
                                        "host_fingerprint"
                                    ]
                                },
                                "command_topology": p31_receipt["command_topology"],
                            },
                            "process_lifetime_binding": {
                                "command_topology": p31_receipt["command_topology"],
                            },
                            "per_query_timeout_ms": self.legacy_protocol["per_query_timeout_ms"],
                            "query_count": self.reference_protocol["query_count"],
                            "interface_scope": self.reference_protocol["interface_scope"],
                            "concurrency": self.reference_protocol["concurrency"],
                            "warmup_passes": self.legacy_protocol["warmup_passes"],
                            "measured_passes": self.legacy_protocol["measured_passes"],
                            "clock": self.reference_protocol["clock"],
                            "timing_boundary": self.reference_protocol["timing_boundary"],
                            "completed_queries": 1700,
                            "timeout_queries": 0,
                            "mismatch_queries": 0,
                            "expected_digest_sha256": self.mixed_value[
                                "logical_dataset_identity"
                            ]["expected_digest_sha256"],
                            "actual_digest_sha256": self.mixed_value[
                                "logical_dataset_identity"
                            ]["expected_digest_sha256"],
                            "qps": statistics.median(
                                cell["metrics"]["qps"]
                                for cell in self.mixed_value["legacy_cells"]
                                if cell["cell_key"]
                                in ("seml0:r1", "seml0:r2", "seml0:r3")
                            ),
                            "latency_p50_us": statistics.median(
                                cell["metrics"]["latency_p50_us"]
                                for cell in self.mixed_value["legacy_cells"]
                                if cell["cell_key"]
                                in ("seml0:r1", "seml0:r2", "seml0:r3")
                            ),
                            "latency_p95_us": statistics.median(
                                cell["metrics"]["latency_p95_us"]
                                for cell in self.mixed_value["legacy_cells"]
                                if cell["cell_key"]
                                in ("seml0:r1", "seml0:r2", "seml0:r3")
                            ),
                            "latency_p99_us": statistics.median(
                                cell["metrics"]["latency_p99_us"]
                                for cell in self.mixed_value["legacy_cells"]
                                if cell["cell_key"]
                                in ("seml0:r1", "seml0:r2", "seml0:r3")
                            ),
                            "adapter_artifacts": adapter_artifacts,
                            "adapter_provenance": provenance,
                        },
                    )
                    value["adapter_result"] = future_ref(adapter_path)
                    value["adapter_provenance"] = future_ref(provenance_path)
                    value["request"] = prepared["request"]
                    value["p31_receipt"] = future_ref(p31_receipt_path)
                    value["command_topology"] = p31_receipt["command_topology"]
                    value["per_query_timeout_ms"] = self.legacy_protocol["per_query_timeout_ms"]
                if role == "cleanup":
                    value.update(
                        mutable_clone_removed=True,
                        mutable_clone=str((cwd / "mutable-store").resolve()),
                    )
                write_json(cwd / production.RECEIPT_PATHS[role], value)
            stdout.write_text("", encoding="utf-8")
            stderr.write_text("", encoding="utf-8")
            return 0

        pending_result = production.execute_production(plan_path, runner=fake_runner)
        self.assertEqual(pending_result["state"], "CANARY_PENDING")
        self.assertEqual(len(dispatched), 4)
        self.assertFalse((self.campaign / "MATRIX-DONE.json").exists())
        self.assertTrue((self.campaign / "CANARY-PENDING.json").is_file())
        restart_pending = production.execute_production(plan_path, runner=fake_runner)
        self.assertEqual(restart_pending["state"], "CANARY_PENDING")
        self.assertEqual(len(dispatched), 4)
        bridge = ready["cells"][0]
        bridge_final = Path(bridge["final_cell_root"])
        provenance_path = bridge_final / "adapter-output/adapter-provenance.json"
        validated_path = bridge_final / "adapter-output/validated-repeat.json"
        validated_receipt_path = bridge_final / "validated-result.json"
        done_path = bridge_final / "CELL-DONE.json"
        original_provenance = json.loads(provenance_path.read_text(encoding="utf-8"))

        def validate_bridge() -> None:
            production.validate_final_cell(
                bridge_final,
                cell_key=bridge["cell_key"],
                ordinal=bridge["ordinal"],
                plan_sha=plan_sha,
                expected_mode="production",
                target_p02b=bridge["runtime"]["target_p02b"],
                target_query_plan=bridge["runtime"]["target_query_plan"],
                target_lease=bridge["runtime"]["target_lease"],
                adapter_tool=bridge["runtime"]["adapter_tool"],
            )

        def rewrite_provenance_chain(provenance_value: dict) -> None:
            write_json(provenance_path, provenance_value)
            validated_value = json.loads(validated_path.read_text(encoding="utf-8"))
            validated_value["adapter_provenance"] = provenance_value
            validated_value["adapter_artifacts"][
                "adapter-provenance.json"
            ] = production.external_file_ref(provenance_path)
            write_json(validated_path, validated_value)
            receipt_value = json.loads(
                validated_receipt_path.read_text(encoding="utf-8")
            )
            receipt_value["adapter_provenance"] = production.external_file_ref(
                provenance_path
            )
            receipt_value["adapter_result"] = production.external_file_ref(
                validated_path
            )
            write_json(validated_receipt_path, receipt_value)
            done_value = json.loads(done_path.read_text(encoding="utf-8"))
            done_value["receipts"]["validated_result"] = production.file_ref(
                validated_receipt_path, bridge_final, "validated_result"
            )
            write_json(done_path, done_value)

        validate_bridge()
        provenance_path.unlink()
        with self.assertRaises(production.BackendError):
            validate_bridge()
        rewrite_provenance_chain(original_provenance)
        stale = json.loads(json.dumps(original_provenance))
        stale["mode"] = "tampered"
        write_json(provenance_path, stale)
        with self.assertRaises(production.BackendError):
            validate_bridge()
        rewrite_provenance_chain(original_provenance)
        mutations = []
        wrong_schema = json.loads(json.dumps(original_provenance))
        wrong_schema["schema_version"] = "wrong"
        mutations.append(wrong_schema)
        wrong_mode = json.loads(json.dumps(original_provenance))
        wrong_mode["mode"] = "fixture"
        mutations.append(wrong_mode)
        wrong_variant = json.loads(json.dumps(original_provenance))
        wrong_variant["variant"] = "naive"
        mutations.append(wrong_variant)
        wrong_lifetime = json.loads(json.dumps(original_provenance))
        wrong_lifetime["process_lifetime"] = "per-query-process"
        mutations.append(wrong_lifetime)
        wrong_repo = json.loads(json.dumps(original_provenance))
        wrong_repo["repo"]["head"] = "9" * 40
        mutations.append(wrong_repo)
        wrong_binary = json.loads(json.dumps(original_provenance))
        wrong_binary["binary"] = query_ref
        mutations.append(wrong_binary)
        wrong_store = json.loads(json.dumps(original_provenance))
        wrong_store["store"]["tree_sha256"] = "9" * 64
        mutations.append(wrong_store)
        wrong_store_manifest = json.loads(json.dumps(original_provenance))
        wrong_store_manifest["store"]["manifest"] = truth_ref
        mutations.append(wrong_store_manifest)
        wrong_truth = json.loads(json.dumps(original_provenance))
        wrong_truth["truth"] = query_ref
        mutations.append(wrong_truth)
        wrong_sample_plan = json.loads(json.dumps(original_provenance))
        wrong_sample_plan["sample_plan"]["query_count"] = 1699
        mutations.append(wrong_sample_plan)
        wrong_id_map = json.loads(json.dumps(original_provenance))
        wrong_id_map["id_map"]["directory"] = str(self.root.resolve())
        mutations.append(wrong_id_map)
        wrong_p02b = json.loads(json.dumps(original_provenance))
        wrong_p02b["p02b"]["target_bundle"] = targets["naive"]
        mutations.append(wrong_p02b)
        wrong_p31_wrapper = json.loads(json.dumps(original_provenance))
        wrong_p31_wrapper["p31_wrapper"] = query_ref
        mutations.append(wrong_p31_wrapper)
        wrong_raw_artifact = json.loads(json.dumps(original_provenance))
        wrong_raw_artifact["raw_artifacts"]["p10-raw-result.json"] = (
            wrong_raw_artifact["raw_artifacts"]["p10-raw-observations.tsv"]
        )
        mutations.append(wrong_raw_artifact)
        wrong_binding_schema = json.loads(json.dumps(original_provenance))
        wrong_binding_schema["split_phase_binding"]["schema_version"] = "wrong"
        mutations.append(wrong_binding_schema)
        wrong_cell = json.loads(json.dumps(original_provenance))
        wrong_cell["split_phase_binding"]["cell_key"] = "seml0-naive:r1"
        mutations.append(wrong_cell)
        wrong_attempt = json.loads(json.dumps(original_provenance))
        wrong_attempt["split_phase_binding"]["campaign_root"] = str(
            self.root / "wrong-attempt"
        )
        mutations.append(wrong_attempt)
        staging_ref = json.loads(json.dumps(original_provenance))
        staging_ref["split_phase_binding"]["request"]["path"] = str(
            self.campaign
            / "staging"
            / bridge_final.name
            / "adapter-request.json"
        )
        mutations.append(staging_ref)
        wrong_sha = json.loads(json.dumps(original_provenance))
        wrong_sha["split_phase_binding"]["adapter_result"]["sha256"] = "0" * 64
        mutations.append(wrong_sha)
        wrong_backend_ref = json.loads(json.dumps(original_provenance))
        wrong_backend_ref["split_phase_binding"]["backend_plan"] = query_ref
        mutations.append(wrong_backend_ref)
        wrong_target_ref = json.loads(json.dumps(original_provenance))
        wrong_target_ref["split_phase_binding"]["target_p02b"] = targets["naive"]
        mutations.append(wrong_target_ref)
        wrong_request_ref = json.loads(json.dumps(original_provenance))
        wrong_request_ref["split_phase_binding"]["request"] = wrong_request_ref[
            "split_phase_binding"
        ]["p31_run_manifest"]
        wrong_request_ref["request"] = wrong_request_ref["split_phase_binding"][
            "p31_run_manifest"
        ]
        mutations.append(wrong_request_ref)
        wrong_p31_ref = json.loads(json.dumps(original_provenance))
        wrong_p31_ref["split_phase_binding"]["p31_receipt"] = wrong_p31_ref[
            "split_phase_binding"
        ]["command_topology"]
        mutations.append(wrong_p31_ref)
        wrong_manifest_ref = json.loads(json.dumps(original_provenance))
        wrong_manifest_ref["split_phase_binding"]["p31_run_manifest"] = (
            wrong_manifest_ref["split_phase_binding"]["command_topology"]
        )
        wrong_manifest_ref["command"]["run_manifest"] = wrong_manifest_ref[
            "split_phase_binding"
        ]["command_topology"]
        mutations.append(wrong_manifest_ref)
        wrong_topology_ref = json.loads(json.dumps(original_provenance))
        wrong_topology_ref["split_phase_binding"]["command_topology"] = (
            wrong_topology_ref["split_phase_binding"]["p31_receipt"]
        )
        wrong_topology_ref["command"]["command_topology"] = wrong_topology_ref[
            "split_phase_binding"
        ]["p31_receipt"]
        mutations.append(wrong_topology_ref)
        wrong_adapter_tool_ref = json.loads(json.dumps(original_provenance))
        wrong_adapter_tool_ref["split_phase_binding"]["adapter_tool"] = query_ref
        mutations.append(wrong_adapter_tool_ref)
        wrong_adapter_result_ref = json.loads(json.dumps(original_provenance))
        wrong_adapter_result_ref["split_phase_binding"]["adapter_result"] = (
            wrong_adapter_result_ref["split_phase_binding"]["validated_artifacts"][
                "query-observations.tsv"
            ]
        )
        mutations.append(wrong_adapter_result_ref)
        wrong_validated_ref = json.loads(json.dumps(original_provenance))
        wrong_validated_ref["split_phase_binding"]["validated_artifacts"][
            "adapter-result.json"
        ] = wrong_validated_ref["split_phase_binding"]["validated_artifacts"][
            "query-observations.tsv"
        ]
        mutations.append(wrong_validated_ref)
        wrong_clone_ref = json.loads(json.dumps(original_provenance))
        wrong_clone_ref["split_phase_binding"]["clone_receipt"] = wrong_clone_ref[
            "split_phase_binding"
        ]["p31_receipt"]
        wrong_clone_ref["store"]["clone_receipt"] = wrong_clone_ref[
            "split_phase_binding"
        ]["p31_receipt"]
        mutations.append(wrong_clone_ref)
        command_tamper = json.loads(json.dumps(original_provenance))
        command_tamper["command"]["argv"] = ["/bin/false"]
        mutations.append(command_tamper)
        command_pid_tamper = json.loads(json.dumps(original_provenance))
        command_pid_tamper["command"]["root_pid"] = 54321
        mutations.append(command_pid_tamper)
        for mutation in mutations:
            rewrite_provenance_chain(mutation)
            with self.assertRaises(production.BackendError):
                validate_bridge()
            rewrite_provenance_chain(original_provenance)
        validate_bridge()
        jump_row = ready["cells"][1]
        jump_staging = Path(jump_row["staging_cell_root"])
        jump_final = Path(jump_row["final_cell_root"])
        jump_staging.mkdir()
        for phase_name in production.PHASE_ORDER:
            fake_runner(
                jump_row["phase_commands"][phase_name],
                jump_staging,
                jump_staging / f"{phase_name}.stdout.log",
                jump_staging / f"{phase_name}.stderr.log",
            )
        production.finalize_staging_cell(
            jump_staging,
            jump_final,
            cell_key=jump_row["cell_key"],
            ordinal=jump_row["ordinal"],
            plan_sha=plan_sha,
            expected_mode="production",
            target_p02b=jump_row["runtime"]["target_p02b"],
            target_query_plan=jump_row["runtime"]["target_query_plan"],
            target_lease=jump_row["runtime"]["target_lease"],
        )
        with self.assertRaisesRegex(production.BackendError, "without accepted canary"):
            production.inspect_resume_root(self.campaign, ready, plan_sha)
        jump_final.rename(self.root / "malicious-jump-retained")
        del dispatched[-4:]
        pending_path = self.campaign / "CANARY-PENDING.json"
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        backend_ref = production.external_file_ref(plan_path)
        evidence_path = Path(ready["canary_checkpoint"]["evidence_path"])
        evidence = evaluator.build_checkpoint_evidence(
            backend_plan_path=plan_path,
            campaign_root=self.campaign,
            mixed_plan_path=Path(self.mixed_ref["path"]),
            evidence_path=evidence_path,
        )
        write_json(evidence_path, evidence)
        self.assertEqual(set(evidence), set(evaluator.FULL_V2_TOP_LEVEL_GROUPS))
        with self.assertRaisesRegex(evaluator.CanaryError, "path occupied"):
            evaluator.build_checkpoint_evidence(
                backend_plan_path=plan_path,
                campaign_root=self.campaign,
                mixed_plan_path=Path(self.mixed_ref["path"]),
                evidence_path=evidence_path,
            )
        missing_group = json.loads(json.dumps(evidence))
        missing_group.pop("metrics")
        write_json(evidence_path, missing_group)
        with self.assertRaisesRegex(evaluator.CanaryError, "top-level groups drift"):
            evaluator.validate_checkpoint_inputs(
                backend_plan_path=plan_path,
                campaign_root=self.campaign,
                mixed_plan_path=Path(self.mixed_ref["path"]),
                evidence_path=evidence_path,
            )
        extra_group = json.loads(json.dumps(evidence))
        extra_group["handmade"] = True
        write_json(evidence_path, extra_group)
        with self.assertRaisesRegex(evaluator.CanaryError, "top-level groups drift"):
            evaluator.validate_checkpoint_inputs(
                backend_plan_path=plan_path,
                campaign_root=self.campaign,
                mixed_plan_path=Path(self.mixed_ref["path"]),
                evidence_path=evidence_path,
            )
        extra_source_key = json.loads(json.dumps(evidence))
        extra_source_key["metrics"]["handmade"] = 1
        write_json(evidence_path, extra_source_key)
        with self.assertRaisesRegex(evaluator.CanaryError, "metrics keys drift"):
            evaluator.validate_checkpoint_inputs(
                backend_plan_path=plan_path,
                campaign_root=self.campaign,
                mixed_plan_path=Path(self.mixed_ref["path"]),
                evidence_path=evidence_path,
            )
        source_tamper = json.loads(json.dumps(evidence))
        source_tamper["metrics"]["completed_qps"] += 1
        write_json(evidence_path, source_tamper)
        with self.assertRaisesRegex(
            evaluator.CanaryError, "evidence/validated metric drift"
        ):
            evaluator.evaluate(
                Path(self.mixed_ref["path"]),
                evidence_path,
                expected_evidence_schema=evaluator.CHECKPOINT_EVIDENCE_SCHEMA,
            )
        write_json(evidence_path, evidence)
        evaluator_chain_tamper = json.loads(json.dumps(original_provenance))
        evaluator_chain_tamper["mode"] = "fixture"
        rewrite_provenance_chain(evaluator_chain_tamper)
        with self.assertRaises(production.BackendError):
            evaluator.validate_checkpoint_inputs(
                backend_plan_path=plan_path,
                campaign_root=self.campaign,
                mixed_plan_path=Path(self.mixed_ref["path"]),
                evidence_path=evidence_path,
            )
        rewrite_provenance_chain(original_provenance)
        handmade_adapter = write_json(
            self.campaign / "handmade-adapter-result.json",
            {
                "schema_version": "cidr-p10-validated-repeat-v1",
                "system_id": "seml0",
            },
        )
        handmade_evidence = json.loads(json.dumps(evidence))
        handmade_evidence["validated_result"] = production.external_file_ref(
            handmade_adapter
        )
        write_json(evidence_path, handmade_evidence)
        with self.assertRaisesRegex(
            evaluator.CanaryError, "evidence/receipt adapter-result mismatch"
        ):
            evaluator.validate_checkpoint_inputs(
                backend_plan_path=plan_path,
                campaign_root=self.campaign,
                mixed_plan_path=Path(self.mixed_ref["path"]),
                evidence_path=evidence_path,
            )
        receipt_mismatch = json.loads(json.dumps(evidence))
        receipt_mismatch["validated_result"] = pending["bridge_receipts"][
            "prepared_command"
        ]
        write_json(evidence_path, receipt_mismatch)
        with self.assertRaisesRegex(
            evaluator.CanaryError, "evidence/receipt adapter-result mismatch"
        ):
            evaluator.validate_checkpoint_inputs(
                backend_plan_path=plan_path,
                campaign_root=self.campaign,
                mixed_plan_path=Path(self.mixed_ref["path"]),
                evidence_path=evidence_path,
            )
        adapter_path = Path(evidence["validated_result"]["path"])
        original_adapter = adapter_path.read_bytes()
        adapter_path.write_bytes(original_adapter + b" ")
        write_json(evidence_path, evidence)
        with self.assertRaisesRegex(production.BackendError, "path/size/SHA drift"):
            evaluator.validate_checkpoint_inputs(
                backend_plan_path=plan_path,
                campaign_root=self.campaign,
                mixed_plan_path=Path(self.mixed_ref["path"]),
                evidence_path=evidence_path,
            )
        adapter_path.write_bytes(original_adapter)
        alternate_mixed = write_json(self.root / "alternate-mixed.json", {})
        with self.assertRaisesRegex(evaluator.CanaryError, "mixed-lineage plan drift"):
            evaluator.validate_checkpoint_inputs(
                backend_plan_path=plan_path,
                campaign_root=self.campaign,
                mixed_plan_path=alternate_mixed,
                evidence_path=evidence_path,
            )
        wrong_contract = json.loads(json.dumps(evidence))
        wrong_contract["contract_sha256"] = "0" * 64
        write_json(evidence_path, wrong_contract)
        with self.assertRaisesRegex(evaluator.CanaryError, "evidence contract drift"):
            evaluator.validate_checkpoint_inputs(
                backend_plan_path=plan_path,
                campaign_root=self.campaign,
                mixed_plan_path=Path(self.mixed_ref["path"]),
                evidence_path=evidence_path,
            )
        wrong_evidence = json.loads(json.dumps(evidence))
        wrong_evidence["validated_result_receipt"]["sha256"] = "0" * 64
        write_json(evidence_path, wrong_evidence)
        with self.assertRaisesRegex(
            evaluator.CanaryError, "validated-result receipt drift"
        ):
            evaluator.validate_checkpoint_inputs(
                backend_plan_path=plan_path,
                campaign_root=self.campaign,
                mixed_plan_path=Path(self.mixed_ref["path"]),
                evidence_path=evidence_path,
            )
        write_json(evidence_path, evidence)
        evidence_ref = production.external_file_ref(evidence_path)
        comparability = {
            "schema_version": evaluator.RECEIPT_SCHEMA,
            "state": "PASS",
            "mixed_lineage_plan": self.mixed_ref,
            "canary_evidence": evidence_ref,
            "contract_sha256": self.contract_sha,
            "failures": [],
            "identity_checks": {
                key: {"state": "PASS"}
                for key in self.formal_contract["identity_exact_match"]
            },
            "correctness_checks": {
                key: "PASS" for key in self.formal_contract["correctness"]
            },
            "performance_checks": {
                key: {"state": "PASS"} for key in evaluator.METRICS
            },
            "normalizer_release": True,
        }
        with self.assertRaisesRegex(evaluator.CanaryError, "comparability schema"):
            evaluator._validate_comparability_receipt(
                {"state": "PASS", "normalizer_release": True},
                contract=ready["canary_checkpoint"],
                evidence_ref=evidence_ref,
            )
        failed_comparability = json.loads(json.dumps(comparability))
        failed_comparability["failures"] = ["performance.completed_qps"]
        with self.assertRaisesRegex(evaluator.CanaryError, "failures must be empty"):
            evaluator._validate_comparability_receipt(
                failed_comparability,
                contract=ready["canary_checkpoint"],
                evidence_ref=evidence_ref,
            )
        comparability_path = Path(ready["canary_checkpoint"]["comparability_path"])
        evaluation_path = self.campaign / "CANARY-EVALUATION.json"
        evidence_path.unlink()
        checkpoint = evaluator.run_production_checkpoint(
            backend_plan_path=plan_path,
            campaign_root=self.campaign,
            mixed_plan_path=Path(self.mixed_ref["path"]),
            evidence_path=evidence_path,
            evaluation_path=evaluation_path,
        )
        self.assertTrue(evidence_path.is_file())
        self.assertTrue(comparability_path.is_file())
        with self.assertRaises(evaluator.CanaryError):
            evaluator.atomic_write(evaluation_path, checkpoint)
        original_checkpoint = json.loads(evaluation_path.read_text(encoding="utf-8"))
        failed_checkpoint = json.loads(json.dumps(original_checkpoint))
        failed_checkpoint["state"] = "FAILED_RETAINED"
        write_json(evaluation_path, failed_checkpoint)
        with self.assertRaisesRegex(production.BackendError, "did not PASS"):
            production.execute_production(plan_path, runner=fake_runner)
        hold_checkpoint = json.loads(json.dumps(original_checkpoint))
        hold_checkpoint["state"] = "HOLD"
        write_json(evaluation_path, hold_checkpoint)
        with self.assertRaisesRegex(production.BackendError, "did not PASS"):
            production.execute_production(plan_path, runner=fake_runner)
        stale_checkpoint = json.loads(json.dumps(original_checkpoint))
        stale_checkpoint["backend_plan"]["sha256"] = "0" * 64
        write_json(evaluation_path, stale_checkpoint)
        with self.assertRaisesRegex(production.BackendError, "plan drift/replay"):
            production.execute_production(plan_path, runner=fake_runner)
        replay_checkpoint = json.loads(json.dumps(original_checkpoint))
        replay_checkpoint["canary_pending"]["sha256"] = "0" * 64
        write_json(evaluation_path, replay_checkpoint)
        with self.assertRaisesRegex(production.BackendError, "pending drift/replay"):
            production.execute_production(plan_path, runner=fake_runner)
        write_json(evaluation_path, original_checkpoint)
        done = production.execute_production(plan_path, runner=fake_runner)
        self.assertEqual(done["state"], "PASS")
        self.assertEqual(
            dispatched,
            [
                (key, phase_name)
                for key in production.CELL_ORDER
                for phase_name in production.PHASE_ORDER
            ],
        )
        self.assertEqual(len(dispatched), 16)
        self.assertTrue((self.campaign / "CANARY-ACCEPTED.json").is_file())
        self.assertTrue((self.campaign / "MATRIX-DONE.json").is_file())
        expected_done = json.loads(
            (self.campaign / "MATRIX-DONE.json").read_text(encoding="utf-8")
        )
        (self.campaign / "MATRIX-DONE.json").unlink()
        repaired = production.execute_production(plan_path, runner=fake_runner)
        self.assertEqual(repaired, expected_done)
        self.assertTrue((self.campaign / "MATRIX-DONE.json").is_file())

    def test_finalize_requires_cleanup_and_atomically_publishes(self) -> None:
        self.initialize_root()
        row = self.plan["cells"][0]
        staging = Path(row["staging_cell_root"])
        final = Path(row["final_cell_root"])
        staging.mkdir()
        self.make_receipts(staging, row["cell_key"], row["ordinal"])
        done = production.finalize_staging_cell(
            staging,
            final,
            cell_key=row["cell_key"],
            ordinal=row["ordinal"],
            plan_sha=self.plan_sha,
            expected_mode="synthetic",
        )
        self.assertEqual(done["state"], "PASS")
        self.assertFalse(staging.exists())
        self.assertTrue((final / "CELL-DONE.json").is_file())

    def test_finalize_rejects_cleanup_claim_when_clone_exists(self) -> None:
        self.initialize_root()
        row = self.plan["cells"][0]
        staging = Path(row["staging_cell_root"])
        final = Path(row["final_cell_root"])
        staging.mkdir()
        self.make_receipts(staging, row["cell_key"], row["ordinal"])
        (staging / "mutable-store").mkdir()
        with self.assertRaises(production.BackendError):
            production.finalize_staging_cell(
                staging,
                final,
                cell_key=row["cell_key"],
                ordinal=row["ordinal"],
                plan_sha=self.plan_sha,
                expected_mode="synthetic",
            )
        self.assertTrue(staging.exists())
        self.assertFalse(final.exists())

    def test_unpublished_cell_done_is_created_only_after_deep_validation(self) -> None:
        self.initialize_root()
        row = self.plan["cells"][0]
        staging = Path(row["staging_cell_root"])
        final = Path(row["final_cell_root"])
        staging.mkdir()
        self.make_receipts(staging, row["cell_key"], row["ordinal"])
        pending = production.finalize_staging_cell(
            staging,
            final,
            cell_key=row["cell_key"],
            ordinal=row["ordinal"],
            plan_sha=self.plan_sha,
            expected_mode="synthetic",
            publish_done=False,
        )
        self.assertFalse((final / "CELL-DONE.json").exists())
        correctness = final / production.RECEIPT_PATHS["correctness"]
        correctness.write_text(
            correctness.read_text(encoding="utf-8") + "\n", encoding="utf-8"
        )
        with self.assertRaises(production.BackendError):
            production.validate_final_cell(
                final,
                cell_key=row["cell_key"],
                ordinal=row["ordinal"],
                plan_sha=self.plan_sha,
                expected_mode="synthetic",
                pending_done=pending,
            )
        self.assertFalse((final / "CELL-DONE.json").exists())
        self.assertTrue(final.exists())

    def test_cleanup_admission_rejects_dangling_clone_symlink(self) -> None:
        self.initialize_root()
        row = self.plan["cells"][0]
        staging = Path(row["staging_cell_root"])
        final = Path(row["final_cell_root"])
        staging.mkdir()
        self.make_receipts(staging, row["cell_key"], row["ordinal"])
        (staging / "mutable-store").symlink_to(staging / "missing", target_is_directory=True)
        with self.assertRaisesRegex(production.BackendError, "lexists"):
            production.finalize_staging_cell(
                staging, final, cell_key=row["cell_key"], ordinal=row["ordinal"],
                plan_sha=self.plan_sha, expected_mode="synthetic",
            )

    def test_resume_accepts_only_completed_prefix(self) -> None:
        self.initialize_root()
        for row in self.plan["cells"][:2]:
            staging = Path(row["staging_cell_root"])
            staging.mkdir()
            self.make_receipts(staging, row["cell_key"], row["ordinal"])
            production.finalize_staging_cell(
                staging,
                Path(row["final_cell_root"]),
                cell_key=row["cell_key"],
                ordinal=row["ordinal"],
                plan_sha=self.plan_sha,
                expected_mode="synthetic",
            )
        state = production.inspect_resume_root(
            self.campaign, self.plan, self.plan_sha, expected_mode="synthetic"
        )
        self.assertEqual(state["completed_cells"], 2)
        first = Path(self.plan["cells"][0]["final_cell_root"])
        displaced = self.root / "first-saved"
        first.rename(displaced)
        with self.assertRaises(production.BackendError):
            production.inspect_resume_root(
                self.campaign, self.plan, self.plan_sha, expected_mode="synthetic"
            )

    def test_resume_rejects_and_preserves_failed_staging(self) -> None:
        self.initialize_root()
        staging = Path(self.plan["cells"][0]["staging_cell_root"])
        staging.mkdir()
        write_json(staging / "FAILED.json", {"state": "FAILED"})
        with self.assertRaises(production.BackendError):
            production.inspect_resume_root(
                self.campaign, self.plan, self.plan_sha, expected_mode="synthetic"
            )
        self.assertTrue((staging / "FAILED.json").is_file())

    def test_cell_receipt_sha_drift_blocks_resume(self) -> None:
        self.initialize_root()
        row = self.plan["cells"][0]
        staging = Path(row["staging_cell_root"])
        staging.mkdir()
        self.make_receipts(staging, row["cell_key"], row["ordinal"])
        production.finalize_staging_cell(
            staging,
            Path(row["final_cell_root"]),
            cell_key=row["cell_key"],
            ordinal=row["ordinal"],
            plan_sha=self.plan_sha,
            expected_mode="synthetic",
        )
        receipt = Path(row["final_cell_root"]) / production.RECEIPT_PATHS["correctness"]
        receipt.write_text(receipt.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaises(production.BackendError):
            production.inspect_resume_root(
                self.campaign, self.plan, self.plan_sha, expected_mode="synthetic"
            )

    def test_production_target_backlink_tamper_blocks_final_validation(self) -> None:
        self.initialize_root()
        row = self.plan["cells"][0]
        staging = Path(row["staging_cell_root"])
        final = Path(row["final_cell_root"])
        staging.mkdir()
        self.make_receipts(staging, row["cell_key"], row["ordinal"])
        target_ref = {"path": "/target.json", "sha256": "b" * 64, "size_bytes": 1}
        query_ref = {"path": "/plan.json", "sha256": "c" * 64, "size_bytes": 1}
        lease_ref = {"path": "/lease.json", "sha256": "d" * 64, "size_bytes": 1}
        for role, relative in production.RECEIPT_PATHS.items():
            path = staging / relative
            value = json.loads(path.read_text(encoding="utf-8"))
            value.update(mode="production", synthetic_test_only=False, fixture_only=False)
            if role == "p31":
                value["timing_generated"] = True
            if role in {"store_clone", "p31", "validated_result", "cleanup"}:
                value["target_p02b"] = target_ref
            write_json(path, value)
        production.finalize_staging_cell(
            staging, final, cell_key=row["cell_key"], ordinal=row["ordinal"],
            plan_sha=self.plan_sha, expected_mode="production",
            target_p02b=target_ref, target_query_plan=query_ref, target_lease=lease_ref,
        )
        done_path = final / "CELL-DONE.json"
        done = json.loads(done_path.read_text(encoding="utf-8"))
        done["target_lease"] = {"path": "/wrong", "sha256": "e" * 64, "size_bytes": 1}
        write_json(done_path, done)
        with self.assertRaisesRegex(production.BackendError, "target lease backlink drift"):
            production.validate_final_cell(
                final, cell_key=row["cell_key"], ordinal=row["ordinal"],
                plan_sha=self.plan_sha, expected_mode="production",
                target_p02b=target_ref, target_query_plan=query_ref, target_lease=lease_ref,
            )


if __name__ == "__main__":
    unittest.main()
