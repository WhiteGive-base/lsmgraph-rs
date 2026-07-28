#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parent.parent / "materialize_e01_immutable_store.py"
SPEC = importlib.util.spec_from_file_location("materialize_e01_immutable_store", MODULE_PATH)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MOD)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


class ImmutableStoreTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, Path, dict]:
        source = root / "source"
        source.mkdir()
        (source / "a").write_bytes(b"alpha")
        nested = source / "nested"
        nested.mkdir()
        (nested / "b").write_bytes(b"beta")
        expected = MOD.tree_manifest(source)
        old_manifest = root / "old-manifest.json"
        write_json(
            old_manifest,
            {
                "schema_version": MOD.MANIFEST_SCHEMA,
                "store_root": str(source),
                "store_sha256": expected["store_sha256"],
                "hash_method": MOD.HASH_METHOD,
                "file_count": expected["file_count"],
                "total_bytes": expected["total_bytes"],
            },
        )
        immutable = root / "immutable"
        attempt = Path("/tmp") / f"e01-seal-test-{root.name}"
        plan_path = root / "plan.json"
        write_json(
            plan_path,
            {
                "schema_version": MOD.PLAN_SCHEMA,
                "state": "HOLD",
                "strict_serial": True,
                "max_live_mutable_clones": 1,
                "immutable_asset_root": str(immutable),
                "stores": [
                    {
                        "variant": "budg-b64",
                        "source_root": str(source),
                        "source_manifest": MOD.file_ref(old_manifest),
                        "expected_tree_sha256": expected["store_sha256"],
                        "expected_file_count": expected["file_count"],
                        "expected_total_bytes": expected["total_bytes"],
                        "staging_root": str(immutable / "staging" / "budg-b64.tmp"),
                        "immutable_root": str(immutable / "stores" / "budg-b64"),
                    }
                ],
            },
        )
        return plan_path, attempt, expected

    def test_tree_hash_matches_frozen_record_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "store"
            store.mkdir()
            (store / "x").write_bytes(b"x")
            file_sha = hashlib.sha256(b"x").hexdigest()
            record = f"file\0x\0{1}\0{file_sha}\n".encode()
            expected = hashlib.sha256(record).hexdigest()
            self.assertEqual(MOD.tree_manifest(store)["store_sha256"], expected)

    def test_existing_target_blocks_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan, attempt, _ = self.fixture(Path(directory))
            value = json.loads(plan.read_text())
            Path(value["stores"][0]["immutable_root"]).mkdir(parents=True)
            with mock.patch.object(MOD, "free_bytes", return_value=10**15):
                with self.assertRaisesRegex(MOD.SealError, "target exists"):
                    MOD.preflight(plan, variant="budg-b64", attempt_root=attempt)

    def test_fresh_source_mismatch_fails_before_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan, attempt, _ = self.fixture(Path(directory))
            source = Path(json.loads(plan.read_text())["stores"][0]["source_root"])
            (source / "a").write_bytes(b"drift")
            copier = mock.Mock()
            try:
                with mock.patch.object(MOD, "free_bytes", return_value=10**15):
                    with self.assertRaisesRegex(MOD.SealError, "fresh source SHA mismatch"):
                        MOD.execute(
                            plan,
                            variant="budg-b64",
                            attempt_root=attempt,
                            copier=copier,
                        )
                copier.assert_not_called()
                failure = json.loads((attempt / "FAILED.json").read_text())
                self.assertEqual(failure["state"], "FAILED_RETAINED")
                self.assertFalse(failure["source_modified"])
            finally:
                shutil.rmtree(attempt, ignore_errors=True)

    def test_tiny_reflink_fixture_publishes_exact_seal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan, attempt, expected = self.fixture(Path(directory))

            def copy_fixture(source: Path, staging: Path, stdout: Path, stderr: Path) -> None:
                shutil.copytree(source, staging)
                stdout.write_bytes(b"fixture")
                stderr.write_bytes(b"")

            try:
                with mock.patch.object(MOD, "free_bytes", return_value=10**15):
                    seal = MOD.execute(
                        plan,
                        variant="budg-b64",
                        attempt_root=attempt,
                        copier=copy_fixture,
                    )
                self.assertEqual(seal["state"], "PASS")
                self.assertEqual(seal["tree_sha256"], expected["store_sha256"])
                target = Path(seal["immutable_root"])
                self.assertTrue(target.is_dir())
                self.assertEqual(target.stat().st_mode & 0o777, 0o555)
                self.assertEqual((target / "a").stat().st_mode & 0o777, 0o444)
                self.assertTrue((attempt / "SEAL-DONE.json").is_file())
            finally:
                target = None
                if plan.exists():
                    target = Path(json.loads(plan.read_text())["stores"][0]["immutable_root"])
                if target is not None:
                    for path in sorted(target.rglob("*"), reverse=True) if target.exists() else []:
                        path.chmod(0o755 if path.is_dir() else 0o644)
                    if target.exists():
                        target.chmod(0o755)
                shutil.rmtree(attempt, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
