#!/usr/bin/env python3
"""No-binary tests for LiveGraph's deferred post-P31 block/WAL seal."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


P10_DIR = Path(__file__).resolve().parents[1]
ADAPTER = P10_DIR / "adapters/livegraph_adapter.py"
SPEC = importlib.util.spec_from_file_location("livegraph_post_p31_adapter", ADAPTER)
assert SPEC is not None and SPEC.loader is not None
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)

import p10_contract as contract  # noqa: E402


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def artifact(path: Path) -> dict[str, object]:
    return {
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


class LiveGraphPostP31StoreSealTests(unittest.TestCase):
    def test_terminal_dataset_identity_rejects_same_size_replacement_without_rehash(self) -> None:
        with tempfile.TemporaryDirectory(prefix="livegraph-dataset-terminal-") as raw:
            root = Path(raw)
            dataset = root / "dense.bin"
            dataset.write_bytes(b"original-dense")
            identity = adapter.file_identity(dataset, "fixture dataset")
            dataset_ref = {
                "path": str(dataset.resolve()),
                "size_bytes": dataset.stat().st_size,
                "sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
                "identity_before_and_after": identity,
            }
            seal_document = {
                "schema_version": "p10-livegraph-dataset-seal-v1",
                "state": "PASS",
                "dataset": {
                    "path": str(dataset.resolve()),
                    "sha256": dataset_ref["sha256"],
                    "identity": identity,
                },
            }
            seal_path = root / "dataset-seal.json"
            write_json(seal_path, seal_document)
            provenance = {
                "schema_version": "p10-livegraph-adapter-provenance-v2",
                "artifacts": {"dataset": dataset_ref},
                "dataset_seal": {**artifact(seal_path), "content": seal_document},
            }
            real_hash = contract.sha256_file

            def reject_dataset_hash(path: Path) -> str:
                if Path(path).resolve() == dataset.resolve():
                    raise AssertionError("terminal validation re-read dense payload")
                return real_hash(Path(path))

            with mock.patch.object(contract, "sha256_file", side_effect=reject_dataset_hash):
                contract.validate_livegraph_dataset_terminal_identity(provenance)

            replacement = root / "replacement.bin"
            replacement.write_bytes(b"tampered-dense")
            self.assertEqual(replacement.stat().st_size, dataset.stat().st_size)
            os.replace(replacement, dataset)
            with mock.patch.object(contract, "sha256_file", side_effect=reject_dataset_hash):
                with self.assertRaisesRegex(contract.ContractError, "changed after"):
                    contract.validate_livegraph_dataset_terminal_identity(provenance)

    def fixture(self) -> dict[str, object]:
        temporary = tempfile.TemporaryDirectory(prefix="livegraph-post-p31-seal-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        store = root / "store"
        output = root / "adapter-output"
        p31_dir = root / "p31"
        store.mkdir(mode=0o700)
        output.mkdir()
        p31_dir.mkdir()
        block = store / "livegraph-block"
        wal = store / "livegraph-wal"
        block.write_bytes(b"block-payload\x00" * 32)
        wal.write_bytes(b"wal-payload\x01" * 8)
        formal_store = adapter.freeze_formal_store(store, block, wal)

        def thaw() -> None:
            if store.exists():
                store.chmod(0o700)
                for path in (block, wal):
                    if path.exists() and not path.is_symlink():
                        path.chmod(0o600)

        self.addCleanup(thaw)

        pid = 2**30
        start_ticks = 123456
        start = output / "worker-start.json"
        exit_path = output / "worker-exit.json"
        write_json(
            start,
            {
                "schema_version": "p10-livegraph-worker-pid-v2",
                "state": "RUNNING",
                "pid": pid,
                "proc_start_ticks": start_ticks,
                "process_group_id": pid,
                "started_at_utc": "2026-07-22T00:00:00.000Z",
                "started_monotonic_ns": 100,
                "argv": ["/fixture/livegraph-worker"],
                "cwd": str(output.resolve()),
            },
        )
        exit_identity = {
            "schema_version": "p10-livegraph-worker-pid-v2",
            "state": "EXITED",
            "pid": pid,
            "proc_start_ticks": start_ticks,
            "process_group_id": pid,
            "started_at_utc": "2026-07-22T00:00:00.000Z",
            "ended_at_utc": "2026-07-22T00:00:01.000Z",
            "started_monotonic_ns": 100,
            "ended_monotonic_ns": 200,
            "returncode": 0,
            "same_process_alive_after_wait": False,
            "process_group_members_after_wait": [],
        }
        write_json(exit_path, exit_identity)
        provenance = {
            "schema_version": "p10-livegraph-adapter-provenance-v2",
            "suite_id": "suite",
            "run_id": "run",
            "repeat_index": 1,
            "worker_lifecycle": {
                "start": artifact(start),
                "exit": artifact(exit_path),
                "identity": exit_identity,
            },
            "store": formal_store,
        }
        provenance_path = output / "adapter-provenance.json"
        write_json(provenance_path, provenance)

        for name in ("run-manifest.json", "validation.json", "DONE", "collector-status.json"):
            write_json(p31_dir / name, {"name": name, "state": "PASS" if name != "collector-status.json" else "DONE"})
        p31 = {
            "run_dir": str(p31_dir.resolve()),
            "manifest_sha256": artifact(p31_dir / "run-manifest.json")["sha256"],
            "validation_sha256": artifact(p31_dir / "validation.json")["sha256"],
            "done_sha256": artifact(p31_dir / "DONE")["sha256"],
            "collector_result": {"status_artifact": artifact(p31_dir / "collector-status.json")},
        }
        return {
            "root": root,
            "store": store,
            "block": block,
            "wal": wal,
            "provenance": provenance,
            "provenance_path": provenance_path,
            "p31": p31,
            "seal_path": root / "livegraph-post-p31-store-seal.json",
        }

    def test_adapter_terminal_store_identity_never_hashes_payload(self) -> None:
        fixture = self.fixture()
        fixture["store"].chmod(0o700)
        fixture["block"].chmod(0o600)
        fixture["wal"].chmod(0o600)
        with mock.patch.object(adapter, "sha256_file", side_effect=AssertionError("payload hash")):
            sealed = adapter.freeze_formal_store(
                fixture["store"], fixture["block"], fixture["wal"]
            )
        self.assertNotIn("sha256", sealed["block"])
        self.assertNotIn("sha256", sealed["wal"])
        self.assertEqual(sealed["block"]["identity_after_worker_exit"]["mode_bits"], 0o444)

    def test_external_seal_hashes_each_payload_once_then_stat_only_validates(self) -> None:
        fixture = self.fixture()
        real_fd_hash = contract._sha256_fd
        calls: list[int] = []

        def counted(descriptor: int) -> str:
            calls.append(descriptor)
            return real_fd_hash(descriptor)

        with mock.patch.object(contract, "_sha256_fd", side_effect=counted):
            sealed = contract.publish_livegraph_post_p31_store_seal(
                fixture["provenance_path"], fixture["provenance"], fixture["p31"], fixture["seal_path"]
            )
        self.assertEqual(len(calls), 2)
        real_path_hash = contract.sha256_file

        def reject_store_payload(path: Path) -> str:
            resolved = Path(path).resolve()
            if resolved in {fixture["block"].resolve(), fixture["wal"].resolve()}:
                raise AssertionError("validator re-read store payload")
            return real_path_hash(Path(path))

        with mock.patch.object(contract, "sha256_file", side_effect=reject_store_payload):
            validated = contract.validate_livegraph_post_p31_store_seal(
                fixture["provenance"], fixture["p31"], sealed
            )
        self.assertEqual(validated["content"]["store"]["block"]["sha256"], hashlib.sha256(fixture["block"].read_bytes()).hexdigest())

    def test_writable_or_live_worker_rejects_before_payload_hash(self) -> None:
        fixture = self.fixture()
        fixture["block"].chmod(0o644)
        with mock.patch.object(contract, "_sha256_fd") as hashed:
            with self.assertRaises(contract.ContractError):
                contract.publish_livegraph_post_p31_store_seal(
                    fixture["provenance_path"], fixture["provenance"], fixture["p31"], fixture["seal_path"]
                )
        hashed.assert_not_called()

        fixture = self.fixture()
        pid = fixture["provenance"]["worker_lifecycle"]["identity"]["pid"]
        ticks = fixture["provenance"]["worker_lifecycle"]["identity"]["proc_start_ticks"]
        with mock.patch.object(contract, "_livegraph_proc_identity", return_value=(pid, ticks)), mock.patch.object(
            contract, "_sha256_fd"
        ) as hashed:
            with self.assertRaisesRegex(contract.ContractError, "still alive"):
                contract.publish_livegraph_post_p31_store_seal(
                    fixture["provenance_path"], fixture["provenance"], fixture["p31"], fixture["seal_path"]
                )
        hashed.assert_not_called()

        fixture = self.fixture()
        with mock.patch.object(
            contract,
            "_livegraph_process_group_members",
            side_effect=contract.ContractError("injected proc enumeration failure"),
        ), mock.patch.object(contract, "_sha256_fd") as hashed:
            with self.assertRaisesRegex(contract.ContractError, "proc enumeration"):
                contract.publish_livegraph_post_p31_store_seal(
                    fixture["provenance_path"], fixture["provenance"], fixture["p31"], fixture["seal_path"]
                )
        hashed.assert_not_called()

    def test_identity_drift_during_hash_and_after_seal_is_fail_closed(self) -> None:
        fixture = self.fixture()
        real_fd_hash = contract._sha256_fd
        changed = False

        def mutate_after_hash(descriptor: int) -> str:
            nonlocal changed
            digest = real_fd_hash(descriptor)
            if not changed:
                changed = True
                fixture["block"].chmod(0o600)
                with fixture["block"].open("ab") as handle:
                    handle.write(b"tamper")
            return digest

        with mock.patch.object(contract, "_sha256_fd", side_effect=mutate_after_hash):
            with self.assertRaisesRegex(contract.ContractError, "changed during"):
                contract.publish_livegraph_post_p31_store_seal(
                    fixture["provenance_path"], fixture["provenance"], fixture["p31"], fixture["seal_path"]
                )
        self.assertFalse(fixture["seal_path"].exists())

        fixture = self.fixture()
        sealed = contract.publish_livegraph_post_p31_store_seal(
            fixture["provenance_path"], fixture["provenance"], fixture["p31"], fixture["seal_path"]
        )
        fixture["wal"].chmod(0o600)
        with fixture["wal"].open("ab") as handle:
            handle.write(b"late-tamper")
        real_path_hash = contract.sha256_file

        def reject_store_payload(path: Path) -> str:
            if Path(path).resolve() in {fixture["block"].resolve(), fixture["wal"].resolve()}:
                raise AssertionError("validator re-read store payload")
            return real_path_hash(Path(path))

        with mock.patch.object(contract, "sha256_file", side_effect=reject_store_payload):
            with self.assertRaises(contract.ContractError):
                contract.validate_livegraph_post_p31_store_seal(
                    fixture["provenance"], fixture["p31"], sealed
                )

    def test_missing_or_ambiguous_p31_terminal_is_rejected(self) -> None:
        fixture = self.fixture()
        Path(fixture["p31"]["run_dir"], "FAILED").write_text("failed\n", encoding="utf-8")
        with mock.patch.object(contract, "_sha256_fd") as hashed:
            with self.assertRaisesRegex(contract.ContractError, "FAILED"):
                contract.publish_livegraph_post_p31_store_seal(
                    fixture["provenance_path"], fixture["provenance"], fixture["p31"], fixture["seal_path"]
                )
        hashed.assert_not_called()

    def test_concurrent_seal_target_is_never_overwritten(self) -> None:
        fixture = self.fixture()
        real_fd_hash = contract._sha256_fd
        injected = False

        def create_target_during_hash(descriptor: int) -> str:
            nonlocal injected
            digest = real_fd_hash(descriptor)
            if not injected:
                injected = True
                fixture["seal_path"].write_bytes(b"concurrent-owner\n")
            return digest

        with mock.patch.object(contract, "_sha256_fd", side_effect=create_target_during_hash):
            with self.assertRaisesRegex(contract.ContractError, "refusing to overwrite"):
                contract.publish_livegraph_post_p31_store_seal(
                    fixture["provenance_path"], fixture["provenance"], fixture["p31"], fixture["seal_path"]
                )
        self.assertEqual(fixture["seal_path"].read_bytes(), b"concurrent-owner\n")


if __name__ == "__main__":
    unittest.main()
