#!/usr/bin/env python3
"""Prove P31 never re-reads a formally sealed large dataset for SHA-256."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


P31_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(P31_DIR))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manifest_tool = load("p31_declared_manifest", P31_DIR / "run_manifest.py")
validator = load("p31_declared_validator", P31_DIR / "validate_resource_run.py")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class P31DeclaredDatasetTests(unittest.TestCase):
    def test_manifest_declared_dataset_does_not_call_sha256_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p31-declared-dataset-") as raw:
            dataset = Path(raw) / "dense.bin"
            dataset.write_bytes(b"sealed outside P31")
            with mock.patch.object(manifest_tool, "sha256_file", side_effect=AssertionError("dense rehash")):
                ref = manifest_tool.artifact_ref(
                    str(dataset), digest(dataset), declare_file_sha=True
                )
            self.assertEqual(ref["content_sha256_mode"], "declared-no-read-v1")
            self.assertEqual(ref["sha256"], digest(dataset))

    def test_formal_validator_stats_but_does_not_hash_declared_dataset(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p31-declared-validator-") as raw:
            root = Path(raw)
            binary, config, dataset = root / "bin", root / "config", root / "dense"
            binary.write_bytes(b"bin"); config.write_bytes(b"config"); dataset.write_bytes(b"dense")

            def ref(path: Path, *, declared: bool = False) -> dict:
                value = {
                    "path": str(path), "kind": "file", "exists": True,
                    "size_bytes": path.stat().st_size, "sha256": digest(path),
                }
                if declared:
                    value["content_sha256_mode"] = "declared-no-read-v1"
                return value

            manifest = {
                "performance_eligible_declared": True,
                "repo": {"git_sha_command_ok": True, "status_command_ok": True, "git_sha": "1" * 40, "dirty": False},
                "host": {"pidstat_version_command_ok": True, "iostat_version_command_ok": True},
                "storage_host": {"device": {"exists": True}, "findmnt_command_ok": True, "findmnt": "device ext4 rw"},
                "collector": {"require_aux_tools": True},
                "inputs": {
                    "binary": ref(binary), "dataset": ref(dataset, declared=True),
                    "config": ref(config),
                    "truth": {"path": "", "kind": "unspecified", "exists": False, "size_bytes": None, "sha256": ""},
                    "query_or_trace": {"path": "", "kind": "unspecified", "exists": False, "size_bytes": None, "sha256": ""},
                },
            }

            def audited_hash(path: Path) -> str:
                if path.resolve() == dataset.resolve():
                    raise AssertionError("formal P31 validator rehashed dense dataset")
                return digest(path)

            errors: list[str] = []
            with mock.patch.object(validator, "sha256_file", side_effect=audited_hash):
                validator.validate_formal_provenance(manifest, errors)
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
