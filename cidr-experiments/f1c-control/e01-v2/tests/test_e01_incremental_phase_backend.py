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
                        "copy_argv": ["/bin/cp", "--archive", "--sparse=always", "--", "{SOURCE}", "{TARGET}"],
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

    def test_clone_bootstrap_contract_binds_plan_targets_and_copy_argv(self) -> None:
        admission = {"path": "/evidence/admission.json", "sha256": "a" * 64, "size_bytes": 1}
        targets = {
            "budg-b64": {"path": "/evidence/budg.json", "sha256": "b" * 64, "size_bytes": 1},
            "naive": {"path": "/evidence/naive.json", "sha256": "c" * 64, "size_bytes": 1},
        }
        source_seal = {"path": "/evidence/seal.json", "sha256": "d" * 64, "size_bytes": 1}
        failed_ref = {"path": "/evidence/failed.json", "sha256": "8" * 64, "size_bytes": 1}
        stores = {
            "budg-b64": {"fresh_store_seal": source_seal, "tree_sha256": "e" * 64},
            "naive": {"fresh_store_seal": {}, "tree_sha256": "f" * 64},
        }
        source = "/immutable/budg"
        target = "/results/clone-dry-run/mutable-store"
        copy_argv = ["/bin/cp", "--archive", "--sparse=always", "--", "{SOURCE}", "{TARGET}"]
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
                    "--sparse=always",
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
            "source_identity_pre": {"sha256": "7" * 64, "writable_entries": []},
            "source_identity_post": {"sha256": "7" * 64, "writable_entries": []},
            "clone_tree": {
                "sha256": stores["budg-b64"]["tree_sha256"],
                "full_tree_hash_performed": True,
            },
            "clone_space": {"logical_file_bytes": 3, "allocated_bytes": 4096},
            "thaw_manifest": [
                {
                    "path": ".",
                    "mode_before": "0555",
                    "mode_after": "0755",
                }
            ],
        }
        builder.validate_clone_bootstrap_contract(
            dryrun=dryrun,
            hold_plan=hold,
            hold_ref=hold_ref,
            admission_ref=admission,
            target_refs=targets,
            stores=stores,
            failed_clone_ref=failed_ref,
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
            )
        changed = json.loads(json.dumps(dryrun))
        changed["clone"]["copy_argv"][0] = "/bin/false"
        with self.assertRaisesRegex(builder.BuildError, "copy argv drift"):
            builder.validate_clone_bootstrap_contract(
                dryrun=changed,
                hold_plan=hold,
                hold_ref=hold_ref,
                admission_ref=admission,
                target_refs=targets,
                stores=stores,
                failed_clone_ref=failed_ref,
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
            request_ref = {
                "path": str(request.resolve()),
                "sha256": phase.sha256_file(request),
                "size_bytes": request.stat().st_size,
            }
            runtime = {
                "request": {},
                "binary_argv": ["/bin/true", "--store", "{MUTABLE_CLONE}"],
                "p31_argv": ["/bin/true", "--request", "{REQUEST}"],
            }
            cell = {"cell_key": "seml0:bridge-canary", "ordinal": 1, "runtime": runtime}
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
            request_ref = {"path": str(request.resolve()), "sha256": phase.sha256_file(request), "size_bytes": request.stat().st_size}
            runtime = {
                "request": {"truth": {"query_count": 1700}},
                "binary_argv": ["/bin/true"],
                "p31_argv": ["/bin/true", "--request", "{REQUEST}"],
            }
            cell = {"cell_key": "seml0:bridge-canary", "ordinal": 1, "runtime": runtime}
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


if __name__ == "__main__":
    unittest.main()
