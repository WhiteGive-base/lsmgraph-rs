#!/usr/bin/env python3

from __future__ import annotations

import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_e01_fresh_asset_admission as admission


class FreshAssetAdmissionTests(unittest.TestCase):
    def test_false_eligibility_rejected(self) -> None:
        with self.assertRaises(admission.AdmissionError):
            admission.false_eligibility(
                {
                    "formal_eligible": True,
                    "performance_eligible": False,
                    "paper_claim_eligible": False,
                },
                "fixture",
            )

    def test_file_ref_detects_small_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text("{}\n", encoding="utf-8")
            ref = admission.file_ref(path, "fixture")
            self.assertEqual(ref["size_bytes"], 3)
            self.assertEqual(len(ref["sha256"]), 64)

    def test_writable_store_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            root.mkdir()
            root.chmod(0o755)
            receipt = Path(directory) / "seal.json"
            receipt.write_text(
                json.dumps(
                    {
                        "schema_version": admission.STORE_SCHEMA,
                        "state": "PASS",
                        "variant": "naive",
                        "hash_method": "sha256-tree-v1(relative-path,size,file-sha256)",
                        "source_modified": False,
                        "formal_data_collected": False,
                        "formal_eligible": False,
                        "performance_eligible": False,
                        "paper_claim_eligible": False,
                        "tree_sha256": "a" * 64,
                        "fresh_source_tree_sha256": "a" * 64,
                        "fresh_target_tree_sha256": "a" * 64,
                        "immutable_root": str(root),
                        "immutable_directory_mode": "0755",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(admission.AdmissionError, "writable"):
                admission.validate_store(
                    receipt,
                    "naive",
                    {"candidate_tree_sha256": "a" * 64},
                )

    def test_read_only_store_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            root.mkdir()
            root.chmod(0o555)
            receipt = Path(directory) / "seal.json"
            value = {
                "schema_version": admission.STORE_SCHEMA,
                "state": "PASS",
                "variant": "naive",
                "hash_method": "sha256-tree-v1(relative-path,size,file-sha256)",
                "source_modified": False,
                "formal_data_collected": False,
                "formal_eligible": False,
                "performance_eligible": False,
                "paper_claim_eligible": False,
                "tree_sha256": "a" * 64,
                "fresh_source_tree_sha256": "a" * 64,
                "fresh_target_tree_sha256": "a" * 64,
                "immutable_root": str(root),
                "immutable_directory_mode": "0555",
            }
            receipt.write_text(json.dumps(value), encoding="utf-8")
            actual, ref = admission.validate_store(
                receipt,
                "naive",
                {"candidate_tree_sha256": "a" * 64},
            )
            self.assertEqual(actual["state"], "PASS")
            self.assertEqual(ref["size_bytes"], receipt.stat().st_size)


if __name__ == "__main__":
    unittest.main()
