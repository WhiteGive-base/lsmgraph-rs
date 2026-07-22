#!/usr/bin/env python3
"""Regression tests for receipt-only Neo4j validation inside formal P31."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

P10_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(P10_DIR))

from adapters import neo4j_adapter as adapter  # noqa: E402
from adapters import neo4j_store_contract as store_contract  # noqa: E402
from p10_contract import ContractError, sha256_file  # noqa: E402


IMAGE_DIGEST = "sha256:" + "1" * 64


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class StopAtSealedReceipt(Exception):
    """Sentinel used to inspect the adapter call before later store checks."""


class Neo4jSealedReceiptTests(unittest.TestCase):
    def test_formal_request_rejects_a_dataset_file_without_hashing_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-no-dataset-rehash-") as raw:
            root = Path(raw)
            dataset = root / "large.csv"
            dataset.write_bytes(b"large-csv-placeholder")
            binary = Path(sys.executable).resolve()
            request_path = root / "request.json"
            request = {
                "schema_version": "cidr-p10-adapter-request-v1",
                "contract_version": "cidr-typed-neighbor-adapter-v1",
                "suite_id": "sealed-input-test",
                "run_id": "sealed-input-test-r01",
                "execution_mode": "formal",
                "system_id": "neo4j",
                "group": "client-server",
                "system_version": "5.26.24",
                "interface_scope": "typed-neighbor-dense-id-v1",
                "repeat_index": 1,
                "process_lifetime": "external-prestarted-query-process-lifetime-v1",
                "binary": {"path": str(binary), "sha256": sha256_file(binary)},
                "dataset": {"path": str(dataset), "sha256": "a" * 64},
                "runtime_libraries": [],
                "store_roots": [],
                "truth": {},
                "timing": {},
                "external_service": {},
            }
            write_json(request_path, request)
            real_hash = adapter.sha256_file
            hashed: list[Path] = []

            def tracked(path: Path) -> str:
                resolved = path.resolve()
                hashed.append(resolved)
                if resolved == dataset.resolve():
                    raise AssertionError("formal P31 attempted to hash the dataset CSV")
                return real_hash(path)

            with mock.patch.object(adapter, "sha256_file", side_effect=tracked):
                with self.assertRaisesRegex(ContractError, "sealed P02B dataset root directory"):
                    adapter.validate_request(request_path, "formal", "graph")
            self.assertNotIn(dataset.resolve(), hashed)

    def test_sealed_input_artifact_mode_neither_stats_nor_hashes_csv(self) -> None:
        missing_large_csv = Path("/definitely/not/materialized/large.csv")
        value = {"path": str(missing_large_csv), "sha256": "b" * 64, "size_bytes": 10**12}
        with mock.patch.object(
            store_contract,
            "sha256_file",
            side_effect=AssertionError("sealed CSV must not be hashed"),
        ) as digest:
            observed = store_contract._artifact(
                value,
                "sealed controlled import input",
                verify_file=False,
            )
        digest.assert_not_called()
        self.assertEqual(observed["sha256"], "b" * 64)
        self.assertEqual(observed["size_bytes"], 10**12)

    def test_adapter_consumes_controlled_import_receipt_without_input_rehash(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-sealed-receipt-") as raw:
            root = Path(raw)
            store = root / "store"
            store.mkdir()
            manifest_path = root / "store-manifest.json"
            dataset_sha = "c" * 64
            truth_sha = "d" * 64
            tree_sha = "e" * 64
            dataset_manifest_sha = "f" * 64
            import_receipt_path = root / "controlled-import-receipt.json"
            import_receipt_path.write_text("{}\n", encoding="utf-8")
            import_receipt_sha = sha256_file(import_receipt_path)
            import_reference = {
                "path": str(import_receipt_path.resolve()),
                "sha256": import_receipt_sha,
                "size_bytes": import_receipt_path.stat().st_size,
            }
            manifest = {
                "schema_version": "p10-neo4j-store-manifest-v3",
                "store_root": str(store.resolve()),
                "store_sha256": tree_sha,
                "file_count": 1,
                "total_bytes": 1,
                "immutable_store_sha256": tree_sha,
                "immutable_file_count": 1,
                "immutable_total_bytes": 1,
                "files": [],
                "hash_method": store_contract.TREE_HASH_METHOD,
                "snapshot_phase": adapter.SNAPSHOT_PHASE,
                "known_mutable_patterns": list(store_contract.KNOWN_MUTABLE_PATTERNS),
                "canonical_sentinels": list(store_contract.CANONICAL_SENTINELS),
                "runtime_compatibility": {
                    "neo4j_version": "5.26.24",
                    "image_ref": "neo4j:5.26.24",
                    "image_digest": IMAGE_DIGEST,
                },
                "import_provenance": {
                    "status": "controlled-import-receipt-v3",
                    "reference": import_reference,
                },
                "database_contract": {},
                "dataset_manifest_sha256": dataset_manifest_sha,
                "dataset_sha256": dataset_sha,
                "truth_sha256": truth_sha,
                "relationship_model": adapter.RELATIONSHIP_MODEL,
                "sentinel_files": [],
                "offline_audit": {},
            }
            write_json(manifest_path, manifest)
            seen: dict[str, object] = {}

            def stop(*_args: object, **kwargs: object) -> dict[str, object]:
                seen.update(kwargs)
                raise StopAtSealedReceipt

            with mock.patch.object(adapter, "validate_recorded_tree", return_value={}), mock.patch.object(
                adapter,
                "p31_host_facts",
                return_value={"hostname": "host", "fingerprint_sha256": "0" * 64},
            ), mock.patch.object(adapter, "validate_controlled_import_receipt", side_effect=stop):
                with self.assertRaises(StopAtSealedReceipt):
                    adapter.validate_store_manifest(
                        manifest_path,
                        sha256_file(manifest_path),
                        store,
                        tree_sha,
                        {"reference": {"sha256": dataset_manifest_sha}},
                        {
                            "dataset": {"sha256": dataset_sha},
                            "truth": {"sha256": truth_sha},
                        },
                        IMAGE_DIGEST,
                        "neo4j:5.26.24",
                        "5.26.24",
                        True,
                        import_receipt_path,
                        import_receipt_sha,
                    )
            self.assertIs(seen.get("verify_input_files"), False)


if __name__ == "__main__":
    unittest.main()
