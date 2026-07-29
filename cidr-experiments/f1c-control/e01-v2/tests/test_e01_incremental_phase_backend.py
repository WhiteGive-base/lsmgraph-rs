#!/usr/bin/env python3
"""Light tests for receipt-bound phase execution and backend blocking."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    value = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(value)
    return value


phase = module("phase", "execute_e01_incremental_cell_phase.py")
builder = module("builder", "build_e01_incremental_ready_backend.py")


def write_json(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


class PhaseBackendTests(unittest.TestCase):
    def test_metadata_manifest_does_not_read_content_and_detects_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a").mkdir()
            (root / "a" / "x").write_bytes(b"abc")
            first = phase.metadata_manifest(root)
            self.assertFalse(first["content_hashed"])
            self.assertEqual(first["file_count"], 1)
            (root / "a" / "x").write_bytes(b"xyz")
            second = phase.metadata_manifest(root)
            self.assertEqual(first, second)
            (root / "a" / "x").write_bytes(b"longer")
            self.assertNotEqual(first["sha256"], phase.metadata_manifest(root)["sha256"])

    def test_exact_cleanup_rejects_inode_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            target = parent / "clone"
            target.mkdir()
            stat = target.stat()
            with self.assertRaises(phase.PhaseError):
                phase.exact_cleanup(target, parent, stat.st_dev, stat.st_ino + 1)
            self.assertTrue(target.exists())

    def test_full_copy_argv_is_byte_exact_and_rejects_tamper(self) -> None:
        policy = {"copy_argv": list(phase.FULL_COPY_ARGV_TEMPLATE)}
        argv = phase.full_copy_argv(policy, Path("/source"), Path("/target"))
        self.assertEqual(
            argv,
            [
                "/bin/cp", "--archive", "--reflink=never", "--one-file-system",
                "--", "/source/.", "/target",
            ],
        )
        policy["copy_argv"].insert(2, "--sparse=always")
        with self.assertRaisesRegex(phase.PhaseError, "argv contract drift"):
            phase.full_copy_argv(policy, Path("/source"), Path("/target"))

    def test_file_identity_separation_rejects_any_inode_overlap(self) -> None:
        source = {"files": [
            {"path": "a", "dev": 1, "inode": 10, "size_bytes": 3},
            {"path": "b", "dev": 1, "inode": 11, "size_bytes": 4},
        ]}
        clone = {"files": [
            {"path": "a", "dev": 1, "inode": 20, "size_bytes": 3},
            {"path": "b", "dev": 1, "inode": 10, "size_bytes": 4},
        ]}
        with self.assertRaisesRegex(phase.PhaseError, "inode overlap"):
            phase.validate_file_identity_separation(source, clone)

    def test_full_copy_capacity_requires_source_allocation_plus_reserve(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "x").write_bytes(b"abc")
            original = phase.os.statvfs
            phase.os.statvfs = lambda unused: type(
                "StatVfs", (), {"f_bavail": 1, "f_frsize": 4096}
            )()
            try:
                with self.assertRaisesRegex(phase.PhaseError, "14.4GB reserve"):
                    phase.full_copy_capacity_evidence(root, root)
            finally:
                phase.os.statvfs = original

    def test_thaw_manifest_requires_complete_unique_owner_write_only(self) -> None:
        source = {"entries": [
            {"path": ".", "kind": "directory", "uid": 1, "gid": 2, "mode": "0555"},
            {"path": "a", "kind": "file", "uid": 1, "gid": 2, "mode": "0444"},
        ]}
        clone = {"entries": [
            {"path": ".", "kind": "directory", "uid": 1, "gid": 2, "mode": "0755"},
            {"path": "a", "kind": "file", "uid": 1, "gid": 2, "mode": "0644"},
        ]}
        rows = [
            {"path": ".", "kind": "directory", "uid": 1, "gid": 2, "dev": 1, "inode": 20,
             "mode_before": "0555", "mode_after": "0755"},
            {"path": "a", "kind": "file", "uid": 1, "gid": 2, "dev": 1, "inode": 21,
             "mode_before": "0444", "mode_after": "0644"},
        ]
        clone["entries"][0].update({"dev": 1, "inode": 20})
        clone["entries"][1].update({"dev": 1, "inode": 21})
        phase.validate_thaw_manifest(source, clone, rows)
        with self.assertRaisesRegex(phase.PhaseError, "cover"):
            phase.validate_thaw_manifest(source, clone, rows[:-1])
        tampered = json.loads(json.dumps(rows))
        tampered[1]["mode_after"] = "0664"
        clone_tampered = json.loads(json.dumps(clone))
        clone_tampered["entries"][1]["mode"] = "0664"
        with self.assertRaisesRegex(phase.PhaseError, "beyond owner write"):
            phase.validate_thaw_manifest(source, clone_tampered, tampered)

    def test_clone_precheck_rejects_dangling_target_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            source = parent / "source"
            source.mkdir()
            target = parent / "clone"
            target.symlink_to(parent / "missing", target_is_directory=True)
            with self.assertRaisesRegex(phase.PhaseError, "dangling symlink"):
                phase._safe_clone_roots(source, target, parent)

    def test_executor_binding_checks_self_sha(self) -> None:
        executor = Path(phase.__file__).resolve()
        ref = {
            "path": str(executor),
            "sha256": phase.sha256_file(executor),
            "size_bytes": executor.stat().st_size,
        }
        phase.verify_executor_binding({"phase_executor": ref})
        ref["sha256"] = "0" * 64
        with self.assertRaises(phase.PhaseError):
            phase.verify_executor_binding({"phase_executor": ref})

    def test_clone_dry_run_copies_verifies_and_removes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            output = root / "dryrun"
            source.mkdir()
            (source / "x").write_bytes(b"abc")
            os.chmod(source / "x", 0o444)
            os.chmod(source, 0o555)
            source_tree_sha = phase.content_tree_manifest(source)["sha256"]
            failed = write_json(
                root / "FAILED.json",
                {
                    "schema_version": "cidr-e01-mutable-clone-dry-run-v1",
                    "state": "FAILED_RETAINED",
                    "reason": "Operation not supported",
                    "timing_generated": False,
                },
            )
            failed_ref = {
                "path": str(failed.resolve()),
                "sha256": phase.sha256_file(failed),
                "size_bytes": failed.stat().st_size,
            }
            plan_path = root / "plan.json"
            plan = {
                "schema_version": phase.PLAN_SCHEMA,
                "synthetic_test_only": False,
                "campaign_root": str(root / "formal-campaign"),
                "clone_fallback_predecessor": failed_ref,
                "cells": [{
                    "cell_key": "seml0:bridge-canary",
                    "runtime": {"clone_policy": {
                        "source_root": str(source),
                        "source_seal": {
                            "path": "/fixture/source-seal.json",
                            "sha256": "c" * 64,
                            "size_bytes": 1,
                        },
                        "tree_sha256": source_tree_sha,
                        "copy_mode": "explicit-full-copy-ext4-v1",
                        "filesystem_contract": {
                            "mount": "/data",
                            "filesystem_type": "ext4",
                            "reflink_supported": False,
                            "evidence": failed_ref,
                        },
                        "copy_argv": list(phase.FULL_COPY_ARGV_TEMPLATE),
                    }},
                }],
            }
            write_json(plan_path, plan)
            value = phase.load_json(plan_path, "plan")
            plan_ref = {
                "path": str(plan_path.resolve()),
                "sha256": phase.sha256_file(plan_path),
                "size_bytes": plan_path.stat().st_size,
            }
            original = phase.revalidate_target
            phase.revalidate_target = lambda unused: {
                "path": "/fixture/target.json",
                "sha256": "b" * 64,
                "size_bytes": 1,
            }
            try:
                phase.clone_dry_run(value, value["cells"][0], plan_ref, output)
            finally:
                phase.revalidate_target = original
            receipt = phase.load_json(output / "CLONE-DRYRUN.json", "receipt")
            self.assertEqual(receipt["state"], "PASS")
            self.assertFalse((output / "mutable-store").exists())
            self.assertFalse(receipt["performance_eligible"])
            self.assertEqual(receipt["backend_plan"], plan_ref)
            self.assertEqual(receipt["source_tree_pre"], receipt["source_tree_post"])
            self.assertEqual(receipt["clone_tree"]["sha256"], source_tree_sha)
            self.assertTrue(receipt["thaw_manifest"])
            self.assertGreater(receipt["clone_space"]["allocated_bytes"], 0)
            self.assertTrue(receipt["clone_root_absent_after_cleanup"])
            lifecycle = phase.load_json(output / "STATE.json", "lifecycle")
            self.assertEqual(lifecycle["state"], "PASS")
            self.assertFalse((output / "RUNNING.json").exists())

    def test_clone_dry_run_failure_terminalizes_running_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            output = root / "dryrun"
            source.mkdir()
            (source / "x").write_bytes(b"abc")
            os.chmod(source / "x", 0o444)
            os.chmod(source, 0o555)
            failed = write_json(root / "failed.json", {"state": "FAILED_RETAINED"})
            failed_ref = {
                "path": str(failed.resolve()),
                "sha256": phase.sha256_file(failed),
                "size_bytes": failed.stat().st_size,
            }
            plan_path = write_json(root / "plan.json", {})
            plan_ref = {
                "path": str(plan_path.resolve()),
                "sha256": phase.sha256_file(plan_path),
                "size_bytes": plan_path.stat().st_size,
            }
            plan = {
                "campaign_root": str(root / "campaign"),
                "clone_fallback_predecessor": failed_ref,
            }
            cell = {
                "cell_key": "seml0:bridge-canary",
                "runtime": {
                    "clone_policy": {
                        "source_root": str(source),
                        "source_seal": failed_ref,
                        "tree_sha256": "0" * 64,
                        "copy_mode": "explicit-full-copy-ext4-v1",
                        "filesystem_contract": {
                            "filesystem_type": "ext4", "reflink_supported": False,
                        },
                        "copy_argv": list(phase.FULL_COPY_ARGV_TEMPLATE),
                    }
                },
            }
            original = phase.revalidate_target
            phase.revalidate_target = lambda unused: failed_ref
            try:
                with self.assertRaisesRegex(phase.PhaseError, "source pre-copy tree SHA drift"):
                    phase.clone_dry_run(plan, cell, plan_ref, output)
            finally:
                phase.revalidate_target = original
                os.chmod(source, 0o755)
                os.chmod(source / "x", 0o644)
            self.assertTrue((output / "FAILED.json").is_file())
            self.assertEqual(phase.load_json(output / "STATE.json", "state")["state"], "FAILED_RETAINED")
            self.assertFalse((output / "RUNNING.json").exists())

    def test_clone_dry_run_allows_only_exact_clone_receipt_hold(self) -> None:
        cells = [
            {
                "cell_key": key,
                "phase_commands": {phase_name: None for phase_name in phase.PHASES},
            }
            for key in phase.CELL_VARIANTS
        ]
        value = {
            "state": "HOLD",
            "execution_state": "BLOCKED",
            "blockers": list(phase.CLONE_DRY_RUN_BLOCKERS),
            "cells": cells,
        }
        phase.validate_clone_dry_run_plan(value)
        value["blockers"] = ["another production gate missing"]
        with self.assertRaisesRegex(phase.PhaseError, "blockers beyond"):
            phase.validate_clone_dry_run_plan(value)

    def test_clone_dry_run_is_bridge_only_and_output_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = {"campaign_root": str(root / "campaign")}
            with self.assertRaisesRegex(phase.PhaseError, "bridge-only"):
                phase.clone_dry_run(
                    plan,
                    {"cell_key": "seml0-naive:r1"},
                    {},
                    root / "dryrun",
                )
            with self.assertRaisesRegex(phase.PhaseError, "overlap"):
                phase.clone_dry_run(
                    plan,
                    {"cell_key": "seml0:bridge-canary", "runtime": {"clone_policy": {}}},
                    {},
                    root / "campaign" / "dryrun",
                )

    def test_prepare_all_four_cells_uses_verified_full_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "x").write_bytes(b"abc")
            os.chmod(source / "x", 0o444)
            os.chmod(source, 0o555)
            tree = phase.content_tree_manifest(source)["sha256"]
            seal = write_json(
                root / "seal.json",
                {
                    "state": "PASS",
                    "tree_sha256": tree,
                    "immutable_root": str(source.resolve()),
                },
            )
            seal_ref = {
                "path": str(seal.resolve()),
                "sha256": phase.sha256_file(seal),
                "size_bytes": seal.stat().st_size,
            }
            original = phase.revalidate_target
            phase.revalidate_target = lambda unused: {
                "path": "/fixture/target.json", "sha256": "b" * 64, "size_bytes": 1,
            }
            try:
                for ordinal, (key, variant) in enumerate(phase.CELL_VARIANTS.items(), start=1):
                    cwd = root / f"cell-{ordinal}"
                    cwd.mkdir()
                    cell = {
                        "cell_key": key,
                        "ordinal": ordinal,
                        "final_cell_root": str(root / f"final-{ordinal}"),
                        "runtime": {
                            "variant": variant,
                            "request": {},
                            "binary_argv": ["/bin/true", "{MUTABLE_CLONE}"],
                            "p31_argv": ["/bin/true", "{REQUEST}", "{BINARY_ARGV_JSON}"],
                            "clone_policy": {
                                "source_root": str(source),
                                "source_seal": seal_ref,
                                "tree_sha256": tree,
                                "mutable_clone": str(cwd / "mutable-store"),
                                "copy_mode": "explicit-full-copy-ext4-v1",
                                "filesystem_contract": {
                                    "filesystem_type": "ext4",
                                    "reflink_supported": False,
                                },
                                "copy_argv": list(phase.FULL_COPY_ARGV_TEMPLATE),
                            },
                        },
                    }
                    phase.prepare({}, cell, "a" * 64, cwd)
                    receipt = phase.load_json(cwd / "receipts/store-clone.json", "clone")
                    self.assertTrue(receipt["full_content_hash_performed"])
                    verification = receipt["verification"]
                    self.assertEqual(verification["clone_tree"]["sha256"], tree)
                    phase.validate_file_identity_separation(
                        verification["source_files_post"], verification["clone_files"]
                    )
                    phase.exact_cleanup(
                        Path(receipt["target"]), cwd,
                        receipt["target_dev"], receipt["target_inode"],
                    )
            finally:
                phase.revalidate_target = original
                os.chmod(source, 0o755)
                os.chmod(source / "x", 0o644)

    def test_clone_bootstrap_contract_binds_plan_targets_and_copy_argv(self) -> None:
        admission = {"path": "/evidence/admission.json", "sha256": "a" * 64, "size_bytes": 1}
        targets = {
            "budg-b64": {"path": "/evidence/budg.json", "sha256": "b" * 64, "size_bytes": 1},
            "naive": {"path": "/evidence/naive.json", "sha256": "c" * 64, "size_bytes": 1},
        }
        source_seal = {"path": "/evidence/seal.json", "sha256": "d" * 64, "size_bytes": 1}
        failed_ref = {"path": "/evidence/failed.json", "sha256": "8" * 64, "size_bytes": 1}
        executor_ref = {"path": "/evidence/executor.py", "sha256": "6" * 64, "size_bytes": 1}
        stores = {
            "budg-b64": {"fresh_store_seal": source_seal, "tree_sha256": "e" * 64},
            "naive": {"fresh_store_seal": {}, "tree_sha256": "f" * 64},
        }
        source = "/immutable/budg"
        target = "/results/clone-dry-run/mutable-store"
        copy_argv = list(phase.FULL_COPY_ARGV_TEMPLATE)
        filesystem_contract = {
            "mount": "/data",
            "filesystem_type": "ext4",
            "reflink_supported": False,
            "evidence": failed_ref,
        }
        variants = ("budg-b64", "naive", "naive", "naive")
        hold = {
            "schema_version": builder.SCHEMA,
            "state": "HOLD",
            "execution_state": "BLOCKED",
            "blockers": ["mutable clone lifecycle dry-run receipt absent"],
            "admission_bundle": admission,
            "target_p02b": targets,
            "clone_fallback_predecessor": failed_ref,
            "phase_executor": executor_ref,
            "cells": [
                {
                    "cell_key": key,
                    "runtime": {
                        "variant": variant,
                        "clone_policy": {
                            "source_root": source,
                            "copy_mode": "explicit-full-copy-ext4-v1",
                            "filesystem_contract": filesystem_contract,
                            "copy_argv": copy_argv,
                        },
                    },
                }
                for key, variant in zip(builder.CELL_ORDER, variants)
            ],
        }
        hold_ref = {"path": "/evidence/hold.json", "sha256": "1" * 64, "size_bytes": 1}
        dryrun = {
            "backend_plan_sha256": hold_ref["sha256"],
            "cell_key": "seml0:bridge-canary",
            "failed_reflink_predecessor": failed_ref,
            "copy_mode": "explicit-full-copy-ext4-v1",
            "filesystem_contract": filesystem_contract,
            "target_p02b": targets["budg-b64"],
            "source_seal": source_seal,
            "source_tree_sha256": stores["budg-b64"]["tree_sha256"],
            "clone": {
                "source": source,
                "target": target,
                "copy_argv": [
                    "/bin/cp",
                    "--archive",
                    "--reflink=never",
                    "--one-file-system",
                    "--",
                    source + "/.",
                    target,
                ],
                "metadata_manifest": {"content_hashed": False},
            },
            "source_tree_pre": {
                "sha256": stores["budg-b64"]["tree_sha256"],
                "full_tree_hash_performed": True,
            },
            "source_tree_post": {
                "sha256": stores["budg-b64"]["tree_sha256"],
                "full_tree_hash_performed": True,
            },
            "source_identity_pre": {
                "sha256": "7" * 64,
                "writable_entries": [],
                "entries": [{"path": ".", "kind": "directory", "uid": 1, "gid": 1, "mode": "0555"}],
            },
            "source_identity_post": {
                "sha256": "7" * 64,
                "writable_entries": [],
                "entries": [{"path": ".", "kind": "directory", "uid": 1, "gid": 1, "mode": "0555"}],
            },
            "source_files_pre": {
                "files": [{"path": "x", "dev": 1, "inode": 10, "size_bytes": 3}],
            },
            "source_files_post": {
                "files": [{"path": "x", "dev": 1, "inode": 10, "size_bytes": 3}],
            },
            "clone_files": {
                "files": [{"path": "x", "dev": 1, "inode": 20, "size_bytes": 3}],
            },
            "clone_identity_after_thaw": {
                "entries": [{"path": ".", "kind": "directory", "uid": 1, "gid": 1, "mode": "0755"}],
            },
            "clone_tree": {
                "sha256": stores["budg-b64"]["tree_sha256"],
                "full_tree_hash_performed": True,
            },
            "clone_space": {"logical_file_bytes": 3, "allocated_bytes": 4096},
            "capacity_evidence": {
                "state": "PASS",
                "free_bytes_before": 20_000_000_000,
                "source_allocated_bytes": 4096,
                "source_logical_bytes": 3,
                "reserve_bytes": 14_400_000_000,
                "minimum_bytes": 14_400_004_096,
            },
            "thaw_manifest": [
                {
                    "path": ".",
                    "kind": "directory",
                    "uid": 1,
                    "gid": 1,
                    "dev": 1,
                    "inode": 2,
                    "mode_before": "0555",
                    "mode_after": "0755",
                }
            ],
        }
        dryrun["verification"] = {
            "copy": dryrun["clone"],
            "capacity_evidence": dryrun["capacity_evidence"],
            "source_tree_pre": dryrun["source_tree_pre"],
            "source_tree_post": dryrun["source_tree_post"],
            "source_identity_pre": dryrun["source_identity_pre"],
            "source_identity_post": dryrun["source_identity_post"],
            "source_files_pre": dryrun["source_files_pre"],
            "source_files_post": dryrun["source_files_post"],
            "clone_files": dryrun["clone_files"],
            "clone_identity_after_thaw": dryrun["clone_identity_after_thaw"],
            "clone_tree": dryrun["clone_tree"],
            "clone_space": dryrun["clone_space"],
            "thaw_manifest": dryrun["thaw_manifest"],
            "full_content_hash_performed": True,
            "hash_outside_p31": True,
        }
        builder.validate_clone_bootstrap_contract(
            dryrun=dryrun,
            hold_plan=hold,
            hold_ref=hold_ref,
            admission_ref=admission,
            target_refs=targets,
            stores=stores,
            failed_clone_ref=failed_ref,
            executor_ref=executor_ref,
        )
        changed = json.loads(json.dumps(dryrun))
        changed["backend_plan_sha256"] = "2" * 64
        with self.assertRaisesRegex(builder.BuildError, "backend SHA drift"):
            builder.validate_clone_bootstrap_contract(
                dryrun=changed,
                hold_plan=hold,
                hold_ref=hold_ref,
                admission_ref=admission,
                target_refs=targets,
                stores=stores,
                failed_clone_ref=failed_ref,
                executor_ref=executor_ref,
            )
        changed = json.loads(json.dumps(hold))
        changed["target_p02b"]["naive"]["sha256"] = "9" * 64
        with self.assertRaisesRegex(builder.BuildError, "target P02B drift"):
            builder.validate_clone_bootstrap_contract(
                dryrun=dryrun,
                hold_plan=changed,
                hold_ref=hold_ref,
                admission_ref=admission,
                target_refs=targets,
                stores=stores,
                failed_clone_ref=failed_ref,
                executor_ref=executor_ref,
            )
        changed = json.loads(json.dumps(hold))
        changed["phase_executor"]["sha256"] = "9" * 64
        with self.assertRaisesRegex(builder.BuildError, "phase executor ref drift"):
            builder.validate_clone_bootstrap_contract(
                dryrun=dryrun,
                hold_plan=changed,
                hold_ref=hold_ref,
                admission_ref=admission,
                target_refs=targets,
                stores=stores,
                failed_clone_ref=failed_ref,
                executor_ref=executor_ref,
            )
        changed = json.loads(json.dumps(dryrun))
        changed["clone"]["copy_argv"][0] = "/bin/false"
        changed["verification"]["copy"] = changed["clone"]
        with self.assertRaisesRegex(builder.BuildError, "copy argv drift"):
            builder.validate_clone_bootstrap_contract(
                dryrun=changed,
                hold_plan=hold,
                hold_ref=hold_ref,
                admission_ref=admission,
                target_refs=targets,
                stores=stores,
                failed_clone_ref=failed_ref,
                executor_ref=executor_ref,
            )

    def test_builder_validator_requires_hold_commands_null(self) -> None:
        value = {
            "schema_version": builder.SCHEMA,
            "state": "HOLD",
            "execution_state": "BLOCKED",
            "synthetic_test_only": False,
            "strict_serial": True,
            "large_content_rehashed_now": False,
            "adapter_invoked": False,
            "timing_generated": False,
            "blockers": ["P02B binding mismatch"],
            "clone_fallback_predecessor": {
                "path": "/fixture/failed.json",
                "sha256": "a" * 64,
                "size_bytes": 1,
            },
            "production_scheduler": {
                "path": "/fixture/scheduler.py",
                "sha256": "b" * 64,
                "size_bytes": 1,
            },
            "canary_checkpoint": {
                "schema_version": "cidr-e01-incremental-canary-checkpoint-contract-v2",
                "evaluator": {
                    "path": "/fixture/evaluator.py",
                    "sha256": "c" * 64,
                    "size_bytes": 1,
                },
                "mixed_lineage_plan": {
                    "path": "/fixture/mixed.json",
                    "sha256": "d" * 64,
                    "size_bytes": 1,
                },
                "comparability_contract_sha256": "e" * 64,
                "legacy_protocol": {
                    "clock": "CLOCK_MONOTONIC",
                    "concurrency": 1,
                    "interface_scope": "typed-neighbor-dense-id-v1",
                    "measured_passes": 1,
                    "per_query_timeout_ms": 30000,
                    "process_lifetime": "prebuilt-store-query-process-lifetime-v1",
                    "timing_boundary": "typed-neighbor-call-plus-result-materialization-and-digest-v1",
                    "warmup_passes": 1,
                },
                "legacy_adapter_requests": {
                    key: {"path": f"/fixture/{key}.json", "sha256": "f" * 64, "size_bytes": 1}
                    for key in ("seml0:r1", "seml0:r2", "seml0:r3")
                },
                "bridge_request_argv_binding": {
                    "request_protocol_exact": True,
                    "binary_argv_exact": True,
                    "warmup_runs": 1,
                    "measured_repeats": 1,
                    "process_lifetime": "prebuilt-store-query-process-lifetime-v1",
                    "per_query_timeout_ms": 30000,
                },
                "evidence_schema": "cidr-e01-bridge-canary-evidence-v2",
                "evidence_path": "/fixture/CANARY-EVIDENCE.json",
                "evidence_binding": {
                    "cell_key": "seml0:bridge-canary",
                    "backend_plan": True,
                    "bridge_cell_done": True,
                    "bridge_receipts": [
                        "prepared_command",
                        "store_clone",
                        "p31",
                        "command_topology",
                        "validated_result",
                        "correctness",
                        "fairness",
                        "cleanup",
                    ],
                },
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
                "pending_path": "/fixture/CANARY-PENDING.json",
                "comparability_path": "/fixture/CANARY-COMPARABILITY.json",
                "evaluation_path": "/fixture/CANARY-EVALUATION.json",
                "accepted_path": "/fixture/CANARY-ACCEPTED.json",
                "first_launch_max_completed_cells": 1,
                "resume_requires_evaluation_state": "PASS",
                "matrix_done_before_acceptance": False,
            },
            "cells": [
                {
                    "cell_key": key,
                    "phase_commands": {phase_name: None for phase_name in builder.PHASES},
                }
                for key in builder.CELL_ORDER
            ],
            **builder.FALSE_ELIGIBILITY,
        }
        builder.validate(value)
        value["cells"][0]["phase_commands"]["prepare"] = ["/bin/false"]
        with self.assertRaises(builder.BuildError):
            builder.validate(value)

    def test_builder_ready_arming_gate_binds_written_plan_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "ready.json"
            gate_path = root / "ready.ARMING-GATE.json"
            executor = write_json(root / "executor.json", {})
            executor_ref = {
                "path": str(executor.resolve()),
                "sha256": phase.sha256_file(executor),
                "size_bytes": executor.stat().st_size,
            }
            value = {
                "schema_version": builder.SCHEMA,
                "state": "READY",
                "execution_state": "READY",
                "blockers": [],
                "campaign_gates": {"backend_arming": {"path": str(gate_path)}},
                "_arming_gate": {
                    "state": "PASS",
                    "phase_executor": executor_ref,
                },
            }
            original_build = builder.build
            original_validate = builder.validate
            builder.build = lambda unused: value
            builder.validate = lambda unused: None
            try:
                result = builder.main([
                    "--admission-bundle", str(root / "admission"),
                    "--phase-executor", str(executor),
                    "--failed-clone-attempt", str(root / "failed"),
                    "--campaign-root", str(root / "campaign"),
                    "--output", str(output),
                ])
            finally:
                builder.build = original_build
                builder.validate = original_validate
            self.assertEqual(result, 0)
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
            self.assertEqual(
                gate["backend_plan"],
                {
                    "path": str(output.resolve()),
                    "sha256": phase.sha256_file(output),
                    "size_bytes": output.stat().st_size,
                },
            )

    def test_direct_phase_rejects_non_ready_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path = root / "plan.json"
            write_json(plan_path, {
                "schema_version": phase.PLAN_SCHEMA,
                "state": "HOLD",
                "execution_state": "BLOCKED",
                "strict_serial": True,
                "synthetic_test_only": False,
                "cells": [
                    {"cell_key": key, "runtime": {"variant": variant}, "staging_cell_root": str(root / key.replace(":", "-"))}
                    for key, variant in phase.CELL_VARIANTS.items()
                ],
            })
            self.assertEqual(
                phase.main(["--backend-plan", str(plan_path), "--cell-key", "seml0:bridge-canary", "--phase", "prepare"]),
                2,
            )

    def test_direct_phase_rejects_cell_variant_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path = root / "plan.json"
            cells = [
                {"cell_key": key, "runtime": {"variant": variant}, "staging_cell_root": str(root / key.replace(":", "-"))}
                for key, variant in phase.CELL_VARIANTS.items()
            ]
            cells[0]["runtime"]["variant"] = "naive"
            write_json(plan_path, {
                "schema_version": phase.PLAN_SCHEMA,
                "state": "READY",
                "execution_state": "READY",
                "strict_serial": True,
                "synthetic_test_only": False,
                "cells": cells,
            })
            self.assertEqual(
                phase.main(["--backend-plan", str(plan_path), "--cell-key", "seml0:bridge-canary", "--phase", "prepare"]),
                2,
            )

    def test_p31_recomputes_and_rejects_prepared_argv_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cwd = Path(temporary)
            request = write_json(cwd / "adapter-request.json", {})
            final = cwd.parent / "final"
            runtime = {
                "request": {},
                "binary_argv": ["/bin/true", "--store", "{MUTABLE_CLONE}"],
                "p31_argv": ["/bin/true", "--request", "{REQUEST}"],
            }
            cell = {
                "cell_key": "seml0:bridge-canary",
                "ordinal": 1,
                "final_cell_root": str(final),
                "runtime": runtime,
            }
            request_ref = phase.published_file_ref(request, cwd, cell)
            write_json(cwd / "receipts/prepared-command.json", {
                "backend_plan_sha256": "a" * 64,
                "request": request_ref,
                "binary_argv": ["/bin/false"],
                "p31_argv": ["/bin/false"],
            })
            original = phase.revalidate_target
            phase.revalidate_target = lambda unused: {"path": "/fixture", "sha256": "b" * 64, "size_bytes": 1}
            try:
                with self.assertRaisesRegex(phase.PhaseError, "prepared binary argv drift"):
                    phase.run_p31({}, cell, "a" * 64, cwd)
            finally:
                phase.revalidate_target = original

    def test_p31_rejects_adapter_request_content_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cwd = Path(temporary)
            request = write_json(cwd / "adapter-request.json", {"truth": {"query_count": 1}})
            final = cwd.parent / "final"
            runtime = {
                "request": {"truth": {"query_count": 1700}},
                "binary_argv": ["/bin/true"],
                "p31_argv": ["/bin/true", "--request", "{REQUEST}"],
            }
            cell = {
                "cell_key": "seml0:bridge-canary",
                "ordinal": 1,
                "final_cell_root": str(final),
                "runtime": runtime,
            }
            request_ref = phase.published_file_ref(request, cwd, cell)
            write_json(cwd / "receipts/prepared-command.json", {
                "backend_plan_sha256": "a" * 64,
                "request": request_ref,
                "binary_argv": ["/bin/true"],
                "p31_argv": ["/bin/true", "--request", str(request.resolve())],
            })
            original = phase.revalidate_target
            phase.revalidate_target = lambda unused: {"path": "/fixture", "sha256": "b" * 64, "size_bytes": 1}
            try:
                with self.assertRaisesRegex(phase.PhaseError, "adapter request drift"):
                    phase.run_p31({}, cell, "a" * 64, cwd)
            finally:
                phase.revalidate_target = original

    def test_process_lifetime_binding_only_fills_frozen_missing_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = write_json(
                Path(temporary) / "adapter-result.json",
                {
                    "schema_version": "cidr-p10-adapter-result-v1",
                    "measured": {},
                    "per_query_timeout_ms": 30000,
                },
            )
            receipt = phase.bind_adapter_process_lifetime(
                path,
                {
                    "process_lifetime": "prebuilt-store-query-process-lifetime-v1",
                    "timing": {"per_query_timeout_ms": 30000},
                },
                {
                    "state": "PASS",
                    "process_lifetime": "prebuilt-store-query-process-lifetime-v1",
                    "single_binary_invocation": True,
                    "warmup_measured_same_process": True,
                    "process_model": "single-storage-bench-process-warmup-and-measured-v1",
                    "per_query_timeout_ms": 30000,
                    "self_ref": {"path": "/final/command-topology.json", "sha256": "a" * 64, "size_bytes": 1},
                },
            )
            self.assertEqual(receipt["state"], "PASS")
            self.assertTrue(receipt["source_field_missing"])
            self.assertFalse(receipt["metrics_modified"])
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["process_lifetime"],
                "prebuilt-store-query-process-lifetime-v1",
            )
            tampered = write_json(
                Path(temporary) / "tampered.json",
                {
                    "process_lifetime": "one-process-per-query",
                    "per_query_timeout_ms": 30000,
                },
            )
            with self.assertRaisesRegex(phase.PhaseError, "invalid/conflicting"):
                phase.bind_adapter_process_lifetime(
                    tampered,
                    {
                        "process_lifetime": "prebuilt-store-query-process-lifetime-v1",
                        "timing": {"per_query_timeout_ms": 30000},
                    },
                    {
                        "state": "PASS",
                        "process_lifetime": "prebuilt-store-query-process-lifetime-v1",
                        "single_binary_invocation": True,
                        "warmup_measured_same_process": True,
                        "process_model": "single-storage-bench-process-warmup-and-measured-v1",
                        "per_query_timeout_ms": 30000,
                    },
                )

    def test_process_lifetime_binding_rejects_null_bool_number_and_weak_topology(self) -> None:
        topology = {
            "state": "PASS",
            "process_lifetime": "prebuilt-store-query-process-lifetime-v1",
            "single_binary_invocation": True,
            "warmup_measured_same_process": True,
            "process_model": "single-storage-bench-process-warmup-and-measured-v1",
            "per_query_timeout_ms": 30000,
        }
        request = {
            "process_lifetime": "prebuilt-store-query-process-lifetime-v1",
            "timing": {"per_query_timeout_ms": 30000},
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, invalid in enumerate((None, False, 1)):
                path = write_json(
                    root / f"invalid-{index}.json",
                    {"process_lifetime": invalid, "per_query_timeout_ms": 30000},
                )
                with self.assertRaisesRegex(phase.PhaseError, "invalid/conflicting"):
                    phase.bind_adapter_process_lifetime(path, request, topology)
            missing = write_json(
                root / "missing.json",
                {"metrics": {"qps": 1}, "per_query_timeout_ms": 30000},
            )
            weak = dict(topology)
            weak["single_binary_invocation"] = False
            with self.assertRaisesRegex(phase.PhaseError, "does not prove"):
                phase.bind_adapter_process_lifetime(missing, request, weak)

    def test_adapter_artifacts_are_rewritten_to_final_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / "staging" / "01-cell"
            final = root / "cells" / "01-cell"
            artifact = staging / "adapter-output" / "query-observations.tsv"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("status\nok\n", encoding="utf-8")
            source_ref = {
                "path": str(artifact.resolve()),
                "sha256": phase.sha256_file(artifact),
                "size_bytes": artifact.stat().st_size,
            }
            published = phase.publish_adapter_artifacts(
                {"adapter_artifacts": {"query": source_ref}},
                staging,
                {"final_cell_root": str(final.resolve())},
            )
            self.assertEqual(
                published["query"]["path"],
                str((final / "adapter-output" / artifact.name).resolve()),
            )
            self.assertEqual(published["query"]["sha256"], source_ref["sha256"])

    def test_deadline_chain_rejects_1000_null_bool_number_and_result_drift(self) -> None:
        request = {"timing": {"per_query_timeout_ms": 30000}}
        topology = {"per_query_timeout_ms": 30000}
        result = {"per_query_timeout_ms": 30000}
        validated = {"per_query_timeout_ms": 30000}
        self.assertEqual(
            phase.validate_deadline_chain(request, topology, result, validated),
            30000,
        )
        for invalid in (1000, None, False, 1.5):
            bad_topology = dict(topology)
            bad_topology["per_query_timeout_ms"] = invalid
            with self.assertRaisesRegex(phase.PhaseError, "request/command deadline drift"):
                phase.validate_deadline_chain(request, bad_topology, result)
        for invalid in (1000, None, False, 1.5):
            bad_result = {"per_query_timeout_ms": invalid}
            with self.assertRaisesRegex(phase.PhaseError, "request/result deadline drift"):
                phase.validate_deadline_chain(request, topology, bad_result)
        for invalid in (1000, None, False, 1.5):
            bad_request = {"timing": {"per_query_timeout_ms": invalid}}
            if invalid == 1000:
                with self.assertRaisesRegex(
                    phase.PhaseError, "request/command deadline drift"
                ):
                    phase.validate_deadline_chain(bad_request, topology, result)
            else:
                with self.assertRaisesRegex(
                    phase.PhaseError, "request deadline invalid"
                ):
                    phase.validate_deadline_chain(bad_request, topology, result)


if __name__ == "__main__":
    unittest.main()
