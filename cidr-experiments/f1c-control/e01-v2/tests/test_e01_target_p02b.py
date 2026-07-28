#!/usr/bin/env python3
"""Light tests for target-specific P02B contracts."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import sys
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    value = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(value)
    return value


plan_tool = module("naive_plan", "build_e01_naive_p02b_plan.py")
target = module("target_p02b", "run_e01_target_p02b.py")


class TargetP02BTests(unittest.TestCase):
    def test_naive_plan_changes_only_hint(self) -> None:
        samples = [{"src": index, "degree": index + 1} for index in range(1700)]
        source = {"version": 1, "semantic_degree_hint": True, "entries": [{"edge_type": 1, "samples": samples}]}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            original = plan_tool.SOURCE_PLAN_SHA256
            plan_tool.SOURCE_PLAN_SHA256 = plan_tool.sha256_file(path)
            try:
                result, receipt = plan_tool.build(path)
            finally:
                plan_tool.SOURCE_PLAN_SHA256 = original
        self.assertFalse(result["semantic_degree_hint"])
        self.assertEqual(result["entries"], source["entries"])
        self.assertEqual(receipt["source_sequence_sha256"], receipt["target_sequence_sha256"])

    def test_naive_plan_rejects_non_1700(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.json"
            path.write_text(json.dumps({"semantic_degree_hint": True, "entries": [{"edge_type": 1, "samples": []}]}), encoding="utf-8")
            with self.assertRaises(plan_tool.PlanError):
                plan_tool.build(path)

    def test_naive_plan_rejects_unfrozen_source_sha(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.json"
            path.write_text(json.dumps({
                "semantic_degree_hint": True,
                "entries": [{"edge_type": 1, "samples": [{"src": i, "degree": i} for i in range(1700)]}],
            }), encoding="utf-8")
            with self.assertRaisesRegex(plan_tool.PlanError, "source plan SHA drift"):
                plan_tool.build(path)

    def test_tree_manifest_detects_same_length_content_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "x"
            path.write_bytes(b"abc")
            first = target.tree_manifest(root)
            path.write_bytes(b"xyz")
            second = target.tree_manifest(root)
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertEqual(first["total_bytes"], second["total_bytes"])

    def test_permission_evidence_rejects_writable_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "x").write_bytes(b"x")
            with self.assertRaisesRegex(target.TargetError, "permission drift"):
                target.permission_evidence(root)

    def test_tree_contract_rejects_manifest_count_drift(self) -> None:
        tree = {
            "sha256": "a" * 64,
            "file_count": 2,
            "total_bytes": 3,
            "immutable_permissions_pass": True,
        }
        with self.assertRaisesRegex(target.TargetError, "file_count drift"):
            target.validate_tree_contract(tree, {
                "store_sha256": "a" * 64,
                "file_count": 1,
                "total_bytes": 3,
            })

    @unittest.skipUnless(os.name == "posix", "process-group semantics require POSIX")
    def test_timeout_terminates_runner_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pid_file = root / "child.pid"
            script = (
                "import subprocess,sys,time;"
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
                f"open({str(pid_file)!r},'w').write(str(p.pid));"
                "time.sleep(60)"
            )
            with self.assertRaisesRegex(target.TargetError, "process group terminated"):
                target.run_process_group([sys.executable, "-c", script], root, 1)
            child_pid = int(pid_file.read_text())
            for _ in range(20):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail("grandchild survived process-group timeout cleanup")

    def test_canonical_lease_admission_rejects_consumer_swap(self) -> None:
        lease_ref = {"path": "/lease.json", "sha256": "a" * 64, "size_bytes": 1}
        value = {
            "schema_version": "cidr-batch-lease-admission-v2",
            "state": "PASS",
            "consumer": "P20",
            "lease": lease_ref["path"],
            "lease_sha256": lease_ref["sha256"],
            "repo_head": "b" * 40,
            "binary_sha256": "c" * 64,
            "expires_at_utc": "2026-07-29T00:00:00Z",
        }
        with self.assertRaisesRegex(target.TargetError, "consumer drift"):
            target.validate_lease_admission(
                value, consumer="P10", lease_ref=lease_ref, repo_head="b" * 40,
                binary_sha256="c" * 64, expires_at_utc="2026-07-29T00:00:00Z",
            )

    def test_canonical_lease_admission_rejects_lease_sha_drift(self) -> None:
        lease_ref = {"path": "/lease.json", "sha256": "a" * 64, "size_bytes": 1}
        value = {
            "schema_version": "cidr-batch-lease-admission-v2",
            "state": "PASS",
            "consumer": "P10",
            "lease": lease_ref["path"],
            "lease_sha256": "f" * 64,
            "repo_head": "b" * 40,
            "binary_sha256": "c" * 64,
            "expires_at_utc": "2026-07-29T00:00:00Z",
        }
        with self.assertRaisesRegex(target.TargetError, "lease drift"):
            target.validate_lease_admission(
                value, consumer="P10", lease_ref=lease_ref, repo_head="b" * 40,
                binary_sha256="c" * 64, expires_at_utc="2026-07-29T00:00:00Z",
            )

    def test_static_contract_rejects_layout_hint_mismatch(self) -> None:
        args = type("Args", (), {})()
        args.variant = "naive"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = root / "store"
            store.mkdir()
            (store / "x").write_bytes(b"x")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "schema_version": "p02b-store-manifest-v1",
                "store_path": str(store),
                "store_sha256": "a" * 64,
                "hash_method": target.TREE_METHOD,
            }), encoding="utf-8")
            seal = root / "seal.json"
            seal.write_text(json.dumps({
                "state": "PASS", "variant": "naive", "immutable_root": str(store),
                "tree_sha256": "a" * 64,
            }), encoding="utf-8")
            config = root / "config.json"
            config.write_text(json.dumps({
                "schema_version": "p02b-sf10-sentinel-config-v2",
                "fixture_mode": False, "expected_queries": 1700,
                "l0_layout": "schema", "semantic_degree_hint": False,
            }), encoding="utf-8")
            plan = root / "plan.json"
            plan.write_text(json.dumps({
                "semantic_degree_hint": False,
                "entries": [{"edge_type": 1, "samples": [{"src": i, "degree": 0} for i in range(1700)]}],
            }), encoding="utf-8")
            args.store = store
            args.store_manifest = manifest
            args.store_seal = seal
            args.config = config
            args.query_plan = plan
            with self.assertRaises(target.TargetError):
                target.validate_static(args)

    def test_static_contract_rejects_wrong_plan_hint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self._valid_args(root, "naive")
            plan = json.loads(args.query_plan.read_text(encoding="utf-8"))
            plan["semantic_degree_hint"] = True
            args.query_plan.write_text(json.dumps(plan), encoding="utf-8")
            with self.assertRaises(target.TargetError):
                target.validate_static(args)

    def test_static_contract_rejects_seal_sha_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self._valid_args(root, "budg-b64")
            seal = json.loads(args.store_seal.read_text(encoding="utf-8"))
            seal["tree_sha256"] = "b" * 64
            args.store_seal.write_text(json.dumps(seal), encoding="utf-8")
            with self.assertRaises(target.TargetError):
                target.validate_static(args)

    def test_reordered_samples_change_sequence_sha(self) -> None:
        samples = [{"src": index, "degree": index + 1} for index in range(1700)]
        source = {"semantic_degree_hint": True, "entries": [{"edge_type": 1, "samples": samples}]}
        first = plan_tool.sequence_sha(plan_tool.sequence(source))
        source["entries"][0]["samples"][0], source["entries"][0]["samples"][1] = (
            source["entries"][0]["samples"][1],
            source["entries"][0]["samples"][0],
        )
        second = plan_tool.sequence_sha(plan_tool.sequence(source))
        self.assertNotEqual(first, second)

    def _valid_args(self, root: Path, variant: str):
        expected = target.VARIANTS[variant]
        args = type("Args", (), {})()
        args.variant = variant
        store = root / "store"
        store.mkdir()
        (store / "x").write_bytes(b"x")
        actual = target.tree_manifest(store)["sha256"]
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps({
            "schema_version": "p02b-store-manifest-v1",
            "store_path": str(store),
            "store_sha256": actual,
            "hash_method": target.TREE_METHOD,
        }), encoding="utf-8")
        seal = root / "seal.json"
        seal.write_text(json.dumps({
            "state": "PASS",
            "variant": variant,
            "immutable_root": str(store),
            "tree_sha256": actual,
        }), encoding="utf-8")
        config = root / "config.json"
        config.write_text(json.dumps({
            "schema_version": "p02b-sf10-sentinel-config-v2",
            "fixture_mode": False,
            "expected_queries": 1700,
            "l0_layout": expected["layout"],
            "semantic_degree_hint": expected["hint"],
        }), encoding="utf-8")
        plan = root / "plan.json"
        plan.write_text(json.dumps({
            "semantic_degree_hint": expected["hint"],
            "entries": [{"edge_type": 1, "samples": [{"src": i, "degree": i} for i in range(1700)]}],
        }), encoding="utf-8")
        args.store = store
        args.store_manifest = manifest
        args.store_seal = seal
        args.config = config
        args.query_plan = plan
        return args


if __name__ == "__main__":
    unittest.main()
