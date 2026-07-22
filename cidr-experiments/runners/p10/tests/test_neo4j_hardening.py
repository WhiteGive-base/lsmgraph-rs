#!/usr/bin/env python3
"""Container-free tests for Neo4j lineage and lifecycle hardening."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

P10_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(P10_DIR))

from adapters import freeze_neo4j_store as freezer  # noqa: E402
from adapters import neo4j_adapter as adapter  # noqa: E402
from adapters import neo4j_store_contract as store_contract  # noqa: E402
import run_suite as orchestrator  # noqa: E402
from p10_contract import (  # noqa: E402
    ContractError,
    EXTERNAL_PROCESS_LIFETIME,
    _validate_neo4j_provenance,
    audit_neo4j_runtime_store,
    sha256_file,
    validate_neo4j_p31_binding,
)
from run_suite import materialize_external_repeat_binding  # noqa: E402

IMAGE_DIGEST = "sha256:" + "1" * 64
IMAGE_ID = "sha256:" + "2" * 64
IMPORT_CONTAINER_ID = "3" * 64
SCHEMA_CONTAINER_ID = "4" * 64
RUNTIME_CONTAINER_ID = "5" * 64


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def artifact(path: Path) -> dict[str, object]:
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def prepare_owner_only_store(store: Path) -> None:
    """Materialize the formal offline-proof boundary for a tiny fixture."""

    store.chmod(0o700)
    lock = store / "databases" / "store_lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch(exist_ok=True)
    lock.chmod(0o600)


def recorded_offline_gate(store: Path) -> dict[str, object]:
    """Return a self-consistent, validator-facing owner/lock proof fixture."""

    root_identity = {
        "path": str(store.resolve()),
        "device": 1,
        "inode": 2,
        "uid": os.geteuid(),
        "gid": os.getegid(),
        "mode": "0700",
    }
    lock_identity = {
        "path": str((store / "databases" / "store_lock").resolve()),
        "device": 1,
        "inode": 3,
        "uid": os.geteuid(),
        "gid": os.getegid(),
        "mode": "0600",
        "size_bytes": 0,
        "method": "fcntl-lockf-exclusive-nonblocking-v1",
        "acquired_exclusive": True,
    }
    snapshot = {
        "root_identity": root_identity,
        "docker_mount_audit": {
            "running_container_count": 0,
            "inspected_container_ids": [],
            "overlapping_mounts": [],
        },
        "store_lock": lock_identity,
    }
    return {
        "proof_method": freezer.OFFLINE_PROOF_METHOD,
        "before_hash": copy.deepcopy(snapshot),
        "after_hash": copy.deepcopy(snapshot),
    }


class FakeRecord:
    def __init__(self, value: dict[str, object]) -> None:
        self.value = value

    def data(self) -> dict[str, object]:
        return dict(self.value)


class FakeSession:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def run(self, _statement: str, **_parameters: object) -> list[FakeRecord]:
        return [FakeRecord(row) for row in self.rows]


class FakeDriver:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.closed = False

    def session(self, **_kwargs: object) -> FakeSession:
        return FakeSession(self.rows)

    def close(self) -> None:
        self.closed = True


class FakePlan:
    def __init__(self, operator_type: str, *, arguments: dict[str, object] | None = None, children: list["FakePlan"] | None = None) -> None:
        self.operator_type = operator_type
        self.arguments = arguments or {}
        self.identifiers: list[str] = []
        self.children = children or []


class FakeResult:
    def __init__(self, rows: list[dict[str, object]], plan: FakePlan | None = None) -> None:
        self.rows = rows
        self._summary = type("Summary", (), {"plan": plan})()

    def __iter__(self):
        return iter([FakeRecord(row) for row in self.rows])

    def consume(self):
        return self._summary


class RuntimeSession:
    def __init__(self, plan: FakePlan) -> None:
        self.plan = plan

    def __enter__(self) -> "RuntimeSession":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def run(self, statement: str, **_parameters: object) -> FakeResult:
        if statement.startswith("SHOW DATABASES"):
            return FakeResult([{
                "name": "neo4j", "currentStatus": "online",
                "requestedStatus": "online", "access": "read-only",
            }])
        if statement.startswith("SHOW SETTINGS"):
            return FakeResult([
                {"name": "server.memory.heap.initial_size", "value": "8.00GiB"},
                {"name": "server.memory.heap.max_size", "value": "8.00GiB"},
                {"name": "server.memory.pagecache.size", "value": "16.00GiB"},
            ])
        if statement.startswith("EXPLAIN"):
            return FakeResult([], self.plan)
        raise AssertionError(statement)


class RuntimeDriver:
    def __init__(self, plan: FakePlan) -> None:
        self.plan = plan

    def session(self, **_kwargs: object) -> RuntimeSession:
        return RuntimeSession(self.plan)


class FakeReadyDriver:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.closed = False

    def verify_connectivity(self) -> None:
        if self.error is not None:
            raise self.error

    def get_server_info(self) -> object:
        return type("ServerInfo", (), {"agent": "Neo4j/5.26.24"})()

    def close(self) -> None:
        self.closed = True


class Neo4jFreezerTests(unittest.TestCase):
    def test_prelaunch_audit_is_offline_strict_and_rejects_mutable_delta(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-prelaunch-audit-") as raw:
            root = Path(raw)
            store = root / "store"
            for index, relative in enumerate(store_contract.CANONICAL_SENTINELS):
                path = store / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"sentinel-{index}".encode())
            mutable = store / "logs" / "runtime.log"
            mutable.parent.mkdir(parents=True)
            mutable.write_bytes(b"before")
            prepare_owner_only_store(store)
            tree = store_contract.stable_tree_manifest(store)
            manifest_path = root / "store-manifest.json"
            write_json(
                manifest_path,
                {
                    "schema_version": "p10-neo4j-store-manifest-v3",
                    "store_root": str(store.resolve()),
                    "known_mutable_patterns": list(store_contract.KNOWN_MUTABLE_PATTERNS),
                    **{key: tree[key] for key in (
                        "store_sha256", "file_count", "total_bytes",
                        "immutable_store_sha256", "immutable_file_count", "immutable_total_bytes", "files",
                    )},
                },
            )
            request_path = root / "request.json"
            write_json(request_path, {"repeat_index": 1})
            docker_audit = {
                "running_container_count": 0,
                "inspected_container_ids": [],
                "overlapping_mounts": [],
            }
            with mock.patch.object(freezer, "_docker_mount_audit", return_value=docker_audit) as gate:
                audit = audit_neo4j_runtime_store(
                    stage="pre",
                    store_root=store,
                    store_manifest_path=manifest_path,
                    store_manifest_sha256=sha256_file(manifest_path),
                    request_path=request_path,
                    output_path=root / "pre.json",
                )
            self.assertEqual(gate.call_count, 2)
            self.assertFalse(audit["known_mutable_changes_tolerated"])
            self.assertEqual(audit["audit"]["mutable_deltas"], [])
            self.assertIsNone(audit["stop_receipt"])

            mutable.write_bytes(b"after")
            with mock.patch.object(freezer, "_docker_mount_audit", return_value=docker_audit):
                with self.assertRaisesRegex(ContractError, "differs from its frozen full-tree"):
                    audit_neo4j_runtime_store(
                        stage="pre",
                        store_root=store,
                        store_manifest_path=manifest_path,
                        store_manifest_sha256=sha256_file(manifest_path),
                        request_path=request_path,
                        output_path=root / "pre-tampered.json",
                    )

    def test_tree_hashes_each_sentinel_only_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-freezer-tree-") as raw:
            root = Path(raw)
            first = root / store_contract.CANONICAL_SENTINELS[0]
            second = root / store_contract.CANONICAL_SENTINELS[1]
            first.parent.mkdir(parents=True)
            first.write_bytes(b"nodes")
            second.write_bytes(b"relationships")
            real_hash = store_contract._hash_file
            calls: list[Path] = []

            def counted(path: Path, *, evict_cache: bool) -> str:
                calls.append(path.resolve())
                return real_hash(path, evict_cache=evict_cache)

            with mock.patch.object(store_contract, "_hash_file", side_effect=counted):
                tree = store_contract.stable_tree_manifest(root)
            self.assertEqual(calls.count(first.resolve()), 1)
            self.assertEqual(calls.count(second.resolve()), 1)
            self.assertEqual(
                {record["path"] for record in tree["files"]},
                set(store_contract.CANONICAL_SENTINELS),
            )

    def test_running_alias_container_with_parent_mount_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-freezer-mount-") as raw:
            root = Path(raw) / "store"
            root.mkdir()
            prepare_owner_only_store(root)
            container_id = "a" * 64
            listed = subprocess.CompletedProcess([], 0, stdout=f"{container_id}\n", stderr="")
            inspected = subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps(
                    [
                        {
                            "Id": container_id,
                            "Name": "/alias",
                            "Mounts": [
                                {
                                    "Type": "bind",
                                    "Source": str(root.parent.resolve()),
                                    "Destination": "/workspace",
                                }
                            ],
                        }
                    ]
                ),
                stderr="",
            )
            with mock.patch.object(freezer.subprocess, "run", side_effect=[listed, inspected]):
                with self.assertRaisesRegex(ContractError, "overlapping bind mount"):
                    freezer.assert_store_offline(root, None)

    def test_offline_guard_rejects_unavailable_exclusive_store_lock(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-freezer-lock-") as raw:
            store = Path(raw) / "store"
            store.mkdir()
            prepare_owner_only_store(store)
            with mock.patch.object(freezer.fcntl, "lockf", side_effect=OSError("busy")):
                with self.assertRaisesRegex(ContractError, "not exclusively acquirable"):
                    with freezer.store_offline_guard(store, None):
                        self.fail("busy store lock must not enter the hash region")

    def test_recorded_offline_gate_rejects_non_owner_only_mode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-offline-receipt-") as raw:
            store = Path(raw) / "store"
            store.mkdir()
            gate = recorded_offline_gate(store)
            gate["after_hash"]["root_identity"]["mode"] = "0750"
            with self.assertRaisesRegex(ContractError, "root mode is not 0700"):
                store_contract.validate_owner_only_offline_gate(gate, store, "fixture gate")

    def test_manifest_marks_legacy_import_as_unverified_without_rehashing_sentinel(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-freezer-manifest-") as raw:
            root = Path(raw)
            store = root / "store"
            sentinels = [store / relative for relative in store_contract.CANONICAL_SENTINELS]
            sentinels[0].parent.mkdir(parents=True)
            for index, sentinel in enumerate(sentinels):
                sentinel.write_bytes(f"store-{index}".encode())
            prepare_owner_only_store(store)
            dataset = root / "dataset.json"
            write_json(
                dataset,
                {
                    "schema_version": "p02b-dataset-manifest-v1",
                    "dataset_sha256": "a" * 64,
                },
            )
            truth = root / "truth.tsv"
            truth.write_text("query_index\n", encoding="utf-8")
            output = root / "manifest.json"
            args = Namespace(
                store_root=store,
                dataset_manifest=dataset,
                truth=truth,
                neo4j_version="5.26.24",
                runtime_image_ref="neo4j:5.26.24",
                runtime_image_digest=IMAGE_DIGEST,
                import_receipt=None,
                import_receipt_sha256=None,
                historical_import_image_ref="neo4j:5.26.24",
                assert_container_stopped=None,
                output=output,
            )
            real_hash = store_contract._hash_file
            calls: list[Path] = []

            def counted(path: Path, *, evict_cache: bool) -> str:
                calls.append(path.resolve())
                return real_hash(path, evict_cache=evict_cache)

            docker_audit = {
                "running_container_count": 0,
                "inspected_container_ids": [],
                "overlapping_mounts": [],
            }
            with mock.patch.object(freezer, "_docker_mount_audit", return_value=docker_audit), mock.patch.object(
                store_contract, "_hash_file", side_effect=counted
            ):
                freezer.run(args)
            value = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(value["schema_version"], "p10-neo4j-store-manifest-v3")
            self.assertEqual(value["snapshot_phase"], "offline-prestart-v1")
            self.assertEqual(value["import_provenance"]["status"], "unverified-historical-store")
            self.assertIsNone(value["import_provenance"]["image"]["selected_repo_digest"])
            for sentinel in sentinels:
                self.assertEqual(calls.count(sentinel.resolve()), 1)

    def test_controlled_import_receipt_binds_actual_stages_and_rejects_exit_tamper(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-import-receipt-") as raw:
            root = Path(raw)
            store = root / "store"
            logs_root = root / "container-logs"
            inputs_root = root / "inputs"
            store.mkdir()
            logs_root.mkdir()
            inputs_root.mkdir()
            store.chmod(0o700)
            logs_root.chmod(0o700)
            for relative in store_contract.CANONICAL_SENTINELS:
                target = store / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(relative.encode())
            nodes = inputs_root / "nodes.csv"
            relationships = inputs_root / "rels.csv"
            nodes.write_text("id:ID(V)\n0\n", encoding="utf-8")
            relationships.write_text(":START_ID(V),:END_ID(V),:TYPE\n", encoding="utf-8")
            dataset_manifest = root / "dataset.json"
            write_json(dataset_manifest, {"schema_version": "p02b-dataset-manifest-v1", "dataset_sha256": "a" * 64})
            import_log = root / "import.log"
            schema_log = root / "schema.log"
            import_log.write_text("ok\n", encoding="utf-8")
            schema_log.write_text("ok\n", encoding="utf-8")
            tree = store_contract.stable_tree_manifest(store)
            input_files = [
                {"role": "nodes", "relative_path": "nodes.csv", **artifact(nodes)},
                {"role": "relationships", "relative_path": "rels.csv", **artifact(relationships)},
            ]
            input_digest = hashlib.sha256()
            for item in input_files:
                input_digest.update(
                    f"{item['role']}\0{item['relative_path']}\0{item['size_bytes']}\0{item['sha256']}\n".encode()
                )
            image = {
                "configured_ref": "neo4j:5.26.24",
                "image_id": IMAGE_ID,
                "repo_digests": [f"neo4j@{IMAGE_DIGEST}"],
                "selected_repo_digest": IMAGE_DIGEST,
            }
            data_mount = {"type": "bind", "source": str(store), "destination": "/data", "rw": True}
            logs_mount = {"type": "bind", "source": str(logs_root), "destination": "/logs", "rw": True}
            import_mount = {"type": "bind", "source": str(inputs_root), "destination": "/import", "rw": False}
            common_runtime = {
                "running": False,
                "started_at": "2026-07-22T00:00:00Z",
                "finished_at": "2026-07-22T00:01:00Z",
                "exit_code": 0,
                "restart_count": 0,
            }
            database_contract = {
                "database_name": "neo4j",
                "node_label": "V",
                "id_property": "id",
                "required_index": {
                    "name": "v_id", "state": "ONLINE", "type": "RANGE",
                    "entityType": "NODE", "labelsOrTypes": ["V"], "properties": ["id"],
                },
            }
            import_name = "cidr-p10-neo4j-import-fixture"
            schema_name = "cidr-p10-neo4j-schema-fixture"
            container_user = f"{os.geteuid()}:{os.getegid()}"
            def root_identity(path: Path) -> dict[str, object]:
                metadata = path.stat()
                return {
                    "path": str(path.resolve()), "device": metadata.st_dev, "inode": metadata.st_ino,
                    "uid": metadata.st_uid, "gid": metadata.st_gid, "mode": "0700",
                }
            receipt = {
                "schema_version": "p10-neo4j-controlled-import-receipt-v3",
                "producer": artifact(Path(adapter.__file__).with_name("import_neo4j_store.py")),
                "host": {"hostname": "host", "fingerprint_sha256": "f" * 64},
                "image": image,
                "docker_config_baseline": {
                    "entrypoint": list(store_contract.EXPECTED_ENTRYPOINT),
                    "command": list(store_contract.EXPECTED_COMMAND),
                },
                "logs_root": str(logs_root),
                "filesystem_identity": {
                    "container_user": container_user,
                    "store_root": root_identity(store),
                    "logs_root": root_identity(logs_root),
                },
                "input": {
                    "root": str(inputs_root),
                    "dataset_manifest": artifact(dataset_manifest),
                    "dataset_sha256": "a" * 64,
                    "files": input_files,
                    "input_sha256": input_digest.hexdigest(),
                },
                "stages": [
                    {
                        "name": "full-import",
                        "config": {
                            "configured_image": "neo4j:5.26.24", "image_id": IMAGE_ID,
                            "container_user": container_user,
                            "restart_policy": "no", "container_id": IMPORT_CONTAINER_ID,
                            "container_name": import_name,
                            "entrypoint": list(store_contract.EXPECTED_ENTRYPOINT),
                            "network_mode": "none", "mounts": [data_mount, import_mount, logs_mount],
                            "command": [
                                "neo4j-admin", "database", "import", "full",
                                "--overwrite-destination=true", "--id-type=integer",
                                "--nodes=V=/import/nodes.csv", "--relationships=/import/rels.csv",
                                "--", "neo4j",
                            ],
                        },
                        "runtime": dict(common_runtime),
                        "log": artifact(import_log),
                        "lifecycle": {
                            "create": {
                                "command": [
                                    "docker", "container", "create", "--name", import_name,
                                    "--user", container_user,
                                ],
                                "container_id": IMPORT_CONTAINER_ID,
                            },
                            "start": {
                                "command": [
                                    "docker", "container", "start", "--attach", IMPORT_CONTAINER_ID,
                                ],
                            },
                            "stop": None,
                            "remove": {
                                "command": ["docker", "container", "rm", IMPORT_CONTAINER_ID],
                                "stdout": IMPORT_CONTAINER_ID,
                            },
                            "absence_probe": {
                                "command": [
                                    "docker", "container", "ls", "--all", "--filter",
                                    f"name=^/{import_name}$", "--format", "{{.ID}}",
                                ],
                                "stdout": "",
                            },
                        },
                    },
                    {
                        "name": "schema-finalize",
                        "config": {
                            "configured_image": "neo4j:5.26.24", "image_id": IMAGE_ID,
                            "container_user": container_user,
                            "restart_policy": "no", "container_id": SCHEMA_CONTAINER_ID,
                            "container_name": schema_name,
                            "entrypoint": list(store_contract.EXPECTED_ENTRYPOINT),
                            "command": list(store_contract.EXPECTED_COMMAND),
                            "network_mode": "default", "mounts": [data_mount, logs_mount],
                            "environment": ["NEO4J_AUTH=none"],
                        },
                        "runtime": dict(common_runtime),
                        "database_contract": database_contract,
                        "log": artifact(schema_log),
                        "lifecycle": {
                            "create": {
                                "command": [
                                    "docker", "container", "create", "--name", schema_name,
                                    "--user", container_user,
                                ],
                                "container_id": SCHEMA_CONTAINER_ID,
                            },
                            "start": {
                                "command": ["docker", "container", "start", SCHEMA_CONTAINER_ID],
                            },
                            "stop": {
                                "command": [
                                    "docker", "container", "stop", "--time", "60", SCHEMA_CONTAINER_ID,
                                ],
                                "stdout": SCHEMA_CONTAINER_ID,
                            },
                            "remove": {
                                "command": ["docker", "container", "rm", SCHEMA_CONTAINER_ID],
                                "stdout": SCHEMA_CONTAINER_ID,
                            },
                            "absence_probe": {
                                "command": [
                                    "docker", "container", "ls", "--all", "--filter",
                                    f"name=^/{schema_name}$", "--format", "{{.ID}}",
                                ],
                                "stdout": "",
                            },
                        },
                    },
                ],
                "database_contract": database_contract,
                "final_store": {
                    "root": str(store), "hash_method": store_contract.TREE_HASH_METHOD,
                    **{key: tree[key] for key in (
                        "store_sha256", "file_count", "total_bytes", "immutable_store_sha256",
                        "immutable_file_count", "immutable_total_bytes",
                    )},
                },
                "known_mutable_patterns": list(store_contract.KNOWN_MUTABLE_PATTERNS),
                "canonical_sentinels": list(store_contract.CANONICAL_SENTINELS),
                "store_created_from_empty": True,
                "outcome": {"completed": True, "exit_code": 0},
                "neo4j_driver_version": "5.28.3",
            }
            receipt_path = root / "receipt.json"
            write_json(receipt_path, receipt)
            validated = store_contract.validate_controlled_import_receipt(
                receipt_path,
                sha256_file(receipt_path),
                store_root=store,
                dataset_manifest=artifact(dataset_manifest),
                dataset_sha256="a" * 64,
                image_ref="neo4j:5.26.24",
                image_digest=IMAGE_DIGEST,
                offline_tree=tree,
                importer_path=Path(adapter.__file__).with_name("import_neo4j_store.py"),
                current_host={"hostname": "host", "fingerprint_sha256": "f" * 64},
                verify_input_files=True,
            )
            self.assertEqual(validated["image"]["image_id"], IMAGE_ID)
            tampered = copy.deepcopy(receipt)
            tampered["stages"][0]["runtime"]["exit_code"] = 1
            write_json(receipt_path, tampered)
            with self.assertRaisesRegex(ContractError, "did not exit cleanly"):
                store_contract.validate_controlled_import_receipt(
                    receipt_path, sha256_file(receipt_path), store_root=store,
                    dataset_manifest=artifact(dataset_manifest), dataset_sha256="a" * 64,
                    image_ref="neo4j:5.26.24", image_digest=IMAGE_DIGEST, offline_tree=tree,
                    importer_path=Path(adapter.__file__).with_name("import_neo4j_store.py"),
                    current_host={"hostname": "host", "fingerprint_sha256": "f" * 64},
                    verify_input_files=True,
                )

            tampered = copy.deepcopy(receipt)
            tampered["stages"][0]["lifecycle"]["start"]["command"][-1] = import_name
            write_json(receipt_path, tampered)
            with self.assertRaisesRegex(ContractError, "start is not exact-ID bound"):
                store_contract.validate_controlled_import_receipt(
                    receipt_path, sha256_file(receipt_path), store_root=store,
                    dataset_manifest=artifact(dataset_manifest), dataset_sha256="a" * 64,
                    image_ref="neo4j:5.26.24", image_digest=IMAGE_DIGEST, offline_tree=tree,
                    importer_path=Path(adapter.__file__).with_name("import_neo4j_store.py"),
                    current_host={"hostname": "host", "fingerprint_sha256": "f" * 64},
                    verify_input_files=True,
                )

            tampered = copy.deepcopy(receipt)
            tampered["stages"][0]["config"]["container_user"] = "7474:7474"
            write_json(receipt_path, tampered)
            with self.assertRaisesRegex(ContractError, "container user mismatch"):
                store_contract.validate_controlled_import_receipt(
                    receipt_path, sha256_file(receipt_path), store_root=store,
                    dataset_manifest=artifact(dataset_manifest), dataset_sha256="a" * 64,
                    image_ref="neo4j:5.26.24", image_digest=IMAGE_DIGEST, offline_tree=tree,
                    importer_path=Path(adapter.__file__).with_name("import_neo4j_store.py"),
                    current_host={"hostname": "host", "fingerprint_sha256": "f" * 64},
                    verify_input_files=True,
                )

            tampered = copy.deepcopy(receipt)
            tampered["stages"][0]["config"]["entrypoint"] = ["/startup/docker-entrypoint.sh"]
            write_json(receipt_path, tampered)
            with self.assertRaisesRegex(ContractError, "Entrypoint baseline"):
                store_contract.validate_controlled_import_receipt(
                    receipt_path, sha256_file(receipt_path), store_root=store,
                    dataset_manifest=artifact(dataset_manifest), dataset_sha256="a" * 64,
                    image_ref="neo4j:5.26.24", image_digest=IMAGE_DIGEST, offline_tree=tree,
                    importer_path=Path(adapter.__file__).with_name("import_neo4j_store.py"),
                    current_host={"hostname": "host", "fingerprint_sha256": "f" * 64},
                    verify_input_files=True,
                )

            tampered = copy.deepcopy(receipt)
            tampered["stages"][1]["config"]["command"].append("console")
            write_json(receipt_path, tampered)
            with self.assertRaisesRegex(ContractError, "command baseline"):
                store_contract.validate_controlled_import_receipt(
                    receipt_path, sha256_file(receipt_path), store_root=store,
                    dataset_manifest=artifact(dataset_manifest), dataset_sha256="a" * 64,
                    image_ref="neo4j:5.26.24", image_digest=IMAGE_DIGEST, offline_tree=tree,
                    importer_path=Path(adapter.__file__).with_name("import_neo4j_store.py"),
                    current_host={"hostname": "host", "fingerprint_sha256": "f" * 64},
                    verify_input_files=True,
                )


class Neo4jAdapterPureTests(unittest.TestCase):
    def test_formal_launch_receipt_is_consumed_and_cross_bound_before_queries(self) -> None:
        from tests.test_neo4j_runtime_lifecycle import (
            HOST as LIFECYCLE_HOST,
            IMAGE_DIGEST as LIFECYCLE_IMAGE_DIGEST,
            LifecycleFixture,
            artifact as lifecycle_artifact,
        )

        with tempfile.TemporaryDirectory(prefix="neo4j-adapter-launch-") as raw:
            fixture = LifecycleFixture(Path(raw))
            fixture.run_launch()
            request = {
                "repeat_index": 1,
                "external_service": {
                    "containers": [fixture.contract["container_name"]],
                    "image_digests": [LIFECYCLE_IMAGE_DIGEST],
                },
            }
            with mock.patch.object(
                adapter,
                "p31_host_facts",
                return_value={**LIFECYCLE_HOST, "mem_total_bytes": 1024},
            ):
                launch = adapter.validate_runtime_launch(
                    fixture.output,
                    sha256_file(fixture.output),
                    formal=True,
                    request=request,
                    store=fixture.store,
                    logs_root=fixture.logs,
                    store_manifest={"reference": lifecycle_artifact(fixture.manifest)},
                    preflight={"reference": lifecycle_artifact(fixture.preflight)},
                    uri="bolt://127.0.0.1:17687",
                    image_digest=LIFECYCLE_IMAGE_DIGEST,
                )
            self.assertEqual(launch["receipt"]["docker"]["running"]["runtime"]["pid"], 4242)
            bad_request = copy.deepcopy(request)
            bad_request["external_service"]["containers"] = ["wrong-r01"]
            with mock.patch.object(
                adapter,
                "p31_host_facts",
                return_value={**LIFECYCLE_HOST, "mem_total_bytes": 1024},
            ), self.assertRaisesRegex(ContractError, "container differs"):
                adapter.validate_runtime_launch(
                    fixture.output,
                    sha256_file(fixture.output),
                    formal=True,
                    request=bad_request,
                    store=fixture.store,
                    logs_root=fixture.logs,
                    store_manifest={"reference": lifecycle_artifact(fixture.manifest)},
                    preflight={"reference": lifecycle_artifact(fixture.preflight)},
                    uri="bolt://127.0.0.1:17687",
                    image_digest=LIFECYCLE_IMAGE_DIGEST,
                )

    def container(self, store: Path, logs: Path, *, restart_count: int = 0, pid: int = 123) -> dict[str, object]:
        store.chmod(0o700)
        logs.chmod(0o700)
        return {
            "Id": RUNTIME_CONTAINER_ID,
            "Name": "/neo4j-test",
            "Image": IMAGE_ID,
            "RestartCount": restart_count,
            "State": {"Running": True, "Pid": pid, "StartedAt": "2026-07-22T00:00:00Z"},
            "HostConfig": {
                "RestartPolicy": {"Name": "no"},
                "LogConfig": {"Type": "none", "Config": {}},
                "Memory": 0,
                "MemorySwap": 0,
            },
            "Config": {
                "Image": "neo4j:5.26.24",
                "User": f"{os.geteuid()}:{os.getegid()}",
                "Entrypoint": list(adapter.EXPECTED_ENTRYPOINT),
                "Cmd": ["neo4j"],
                "Env": [
                    "NEO4J_AUTH=none",
                    "NEO4J_server_databases_default__to__read__only=true",
                    "NEO4J_server_memory_heap_initial__size=8G",
                    "NEO4J_server_memory_heap_max__size=8G",
                    "NEO4J_server_memory_pagecache_size=16G",
                ],
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Destination": "/data",
                    "Source": str(store.resolve()),
                    "RW": True,
                },
                {
                    "Type": "bind",
                    "Destination": "/logs",
                    "Source": str(logs.resolve()),
                    "RW": True,
                }
            ],
            "NetworkSettings": {
                "Ports": {"7687/tcp": [{"HostIp": "127.0.0.1", "HostPort": "17687"}]}
            },
        }

    def test_container_snapshot_binds_pid_started_at_and_restart_count(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-container-") as raw:
            root = Path(raw)
            store = root / "store"
            logs = root / "logs"
            store.mkdir()
            logs.mkdir()
            image = {"RepoDigests": [f"neo4j@{IMAGE_DIGEST}"]}
            with mock.patch.object(
                adapter,
                "run_json",
                side_effect=[[self.container(store, logs)], [image]],
            ):
                value = adapter.validate_container(
                    "neo4j-test",
                    "neo4j-test",
                    "neo4j:5.26.24",
                    IMAGE_DIGEST,
                    store,
                    logs,
                    "bolt://127.0.0.1:17687",
                    False,
                    adapter.expected_memory_contract("8G", "8G", "16G"),
                )
            self.assertEqual(value["pid"], 123)
            self.assertEqual(value["restart_count"], 0)
            self.assertEqual(value["started_at"], "2026-07-22T00:00:00Z")

            changed = dict(value)
            changed["pid"] = 456
            with self.assertRaisesRegex(ContractError, "pid changed"):
                adapter.validate_container_stability(value, changed)

    def test_nonzero_restart_count_is_rejected_before_queries(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-restart-") as raw:
            root = Path(raw)
            store = root / "store"
            logs = root / "logs"
            store.mkdir()
            logs.mkdir()
            with mock.patch.object(
                adapter, "run_json", return_value=[self.container(store, logs, restart_count=1)]
            ) as inspect:
                with self.assertRaisesRegex(ContractError, "RestartCount=0"):
                    adapter.validate_container(
                        RUNTIME_CONTAINER_ID,
                        "neo4j-test",
                        "neo4j:5.26.24",
                        IMAGE_DIGEST,
                        store,
                        logs,
                        "bolt://127.0.0.1:17687",
                        True,
                        adapter.expected_memory_contract("8G", "8G", "16G"),
                    )
            self.assertEqual(
                inspect.call_args.args[0],
                ["docker", "container", "inspect", RUNTIME_CONTAINER_ID],
            )

    def test_nested_data_mount_and_directory_override_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-mount-tamper-") as raw:
            root = Path(raw)
            store = root / "store"
            logs = root / "logs"
            store.mkdir()
            logs.mkdir()
            image = {"RepoDigests": [f"neo4j@{IMAGE_DIGEST}"]}
            nested = self.container(store, logs)
            nested["Mounts"].append(
                {"Type": "bind", "Destination": "/data/databases", "Source": str(store / "databases"), "RW": True}
            )
            with mock.patch.object(adapter, "run_json", side_effect=[[nested], [image]]):
                with self.assertRaisesRegex(ContractError, "exact allowlist"):
                    adapter.validate_container(
                        RUNTIME_CONTAINER_ID, "neo4j-test", "neo4j:5.26.24", IMAGE_DIGEST, store, logs,
                        "bolt://127.0.0.1:17687", True,
                        adapter.expected_memory_contract("8G", "8G", "16G"),
                    )
            override = self.container(store, logs)
            override["Config"]["Env"].append("NEO4J_server_directories_data=/other")
            with mock.patch.object(adapter, "run_json", side_effect=[[override], [image]]):
                with self.assertRaisesRegex(ContractError, "overrides storage directories"):
                    adapter.validate_container(
                        RUNTIME_CONTAINER_ID, "neo4j-test", "neo4j:5.26.24", IMAGE_DIGEST, store, logs,
                        "bolt://127.0.0.1:17687", True,
                        adapter.expected_memory_contract("8G", "8G", "16G"),
                    )
            wrong_entrypoint = self.container(store, logs)
            wrong_entrypoint["Config"]["Entrypoint"] = ["/startup/docker-entrypoint.sh"]
            with mock.patch.object(adapter, "run_json", return_value=[wrong_entrypoint]):
                with self.assertRaisesRegex(ContractError, "Config.Entrypoint"):
                    adapter.validate_container(
                        RUNTIME_CONTAINER_ID, "neo4j-test", "neo4j:5.26.24", IMAGE_DIGEST, store, logs,
                        "bolt://127.0.0.1:17687", True,
                        adapter.expected_memory_contract("8G", "8G", "16G"),
                    )
            extra_command = self.container(store, logs)
            extra_command["Config"]["Cmd"].append("console")
            with mock.patch.object(adapter, "run_json", return_value=[extra_command]):
                with self.assertRaisesRegex(ContractError, "Config.Cmd"):
                    adapter.validate_container(
                        RUNTIME_CONTAINER_ID, "neo4j-test", "neo4j:5.26.24", IMAGE_DIGEST, store, logs,
                        "bolt://127.0.0.1:17687", True,
                        adapter.expected_memory_contract("8G", "8G", "16G"),
                    )

    def test_database_and_online_range_index_are_exact(self) -> None:
        valid = {
            "name": "v_id",
            "state": "ONLINE",
            "type": "RANGE",
            "entityType": "NODE",
            "labelsOrTypes": ["V"],
            "properties": ["id"],
        }
        observed = adapter.verify_database_contract(FakeDriver([valid]), "neo4j")
        self.assertEqual(observed["required_index"], valid)
        offline = dict(valid, state="POPULATING")
        with self.assertRaisesRegex(ContractError, "state mismatch"):
            adapter.verify_database_contract(FakeDriver([offline]), "neo4j")
        with self.assertRaisesRegex(ContractError, "database must be"):
            adapter.verify_database_contract(FakeDriver([valid]), "system")

    def test_live_database_memory_and_index_seek_plan_are_required(self) -> None:
        seek = FakePlan("NodeIndexSeek", arguments={"Details": "RANGE INDEX s:V(id)"})
        root = FakePlan("ProduceResults", children=[seek])
        observed = adapter.verify_live_runtime_contract(
            RuntimeDriver(root),
            "neo4j",
            adapter.expected_memory_contract("8G", "8G", "16G"),
            {"edge_type": 1, "src": 0},
        )
        self.assertEqual(observed["database_status"]["access"], "read-only")
        self.assertEqual(observed["memory"]["live"]["pagecache"]["bytes"], 16 * 1024**3)
        scan = FakePlan("NodeByLabelScan", arguments={"Details": "s:V"})
        with self.assertRaisesRegex(ContractError, "lacks NodeIndexSeek"):
            adapter.verify_live_runtime_contract(
                RuntimeDriver(FakePlan("ProduceResults", children=[scan])),
                "neo4j",
                adapter.expected_memory_contract("8G", "8G", "16G"),
                {"edge_type": 1, "src": 0},
            )

    def test_readiness_retries_share_one_bounded_deadline(self) -> None:
        failing = FakeReadyDriver(error=RuntimeError("not ready"))
        with mock.patch.object(adapter.GraphDatabase, "driver", return_value=failing) as factory, mock.patch.object(
            adapter.time, "monotonic", side_effect=[100.0, 100.0, 101.1]
        ), mock.patch.object(adapter.time, "sleep"):
            with self.assertRaisesRegex(ContractError, "timed out after 1 attempts"):
                adapter.open_ready_driver("bolt://127.0.0.1:17687", "Neo4j/5.26.24", 1)
        self.assertTrue(failing.closed)
        self.assertEqual(factory.call_count, 1)
        self.assertLessEqual(factory.call_args.kwargs["connection_timeout"], 1.0)


class Neo4jContractTests(unittest.TestCase):
    def test_repeat_binding_replaces_every_runtime_identity(self) -> None:
        flags = (
            "--uri", "--logs-root", "--store-manifest", "--store-manifest-sha256",
            "--import-receipt", "--import-receipt-sha256",
        )
        args: list[str] = []
        for flag in flags:
            args.extend([flag, f"r1-{flag}"])

        def binding(index: int) -> dict[str, object]:
            return {
                "repeat_index": index,
                "clone_id": f"clone-r{index}",
                "repeat_root": f"/repeat-r{index}",
                "source_store_root": "/source",
                "launcher": {"path": "/launcher", "sha256": "f" * 64},
                "store_root": {"label": "neo4j-runtime", "path": f"/store-r{index}", "sha256": str(index) * 64},
                "logs_root": {"label": "neo4j-logs", "path": f"/logs-r{index}"},
                "container": f"neo4j-r{index}",
                "uri": f"bolt://127.0.0.1:{17000 + index}",
                "store_manifest": {"path": f"/manifest-r{index}", "sha256": "a" * 64},
                "import_receipt": {"path": f"/import-r{index}", "sha256": "b" * 64},
            }

        system = {
            "id": "neo4j",
            "adapter": {"path": "/adapter", "sha256": "d" * 64, "args": args},
            "repeat_bindings": [binding(1), binding(2)],
        }
        effective = materialize_external_repeat_binding(system, 2)
        self.assertEqual(effective["containers"], ["neo4j-r2"])
        self.assertEqual(effective["store_roots"][0]["path"], "/store-r2")
        self.assertEqual(effective["temp_roots"][0]["path"], "/logs-r2")
        effective_args = effective["adapter"]["args"]
        for flag, expected in (
            ("--uri", "bolt://127.0.0.1:17002"),
            ("--logs-root", "/logs-r2"),
            ("--store-manifest", "/manifest-r2"),
            ("--import-receipt", "/import-r2"),
        ):
            position = effective_args.index(flag)
            self.assertEqual(effective_args[position + 1], expected)
        self.assertNotIn("--launch-receipt", effective_args)
        self.assertEqual(effective["active_repeat_binding"]["clone_id"], "clone-r2")

    def fixture(self, root: Path) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        binary = root / "python"
        module = root / "neo4j.py"
        truth = root / "truth.tsv"
        dataset_manifest = root / "dataset-manifest.json"
        store_manifest = root / "store-manifest.json"
        dataset_root = root / "dataset"
        store_root = root / "store"
        logs_root = root / "logs"
        dataset_root.mkdir()
        store_root.mkdir()
        logs_root.mkdir()
        for path, value in (
            (binary, b"python"),
            (module, b"neo4j"),
            (truth, b"truth"),
            (dataset_manifest, b"dataset-manifest"),
            (store_manifest, b"store-manifest"),
        ):
            path.write_bytes(value)
        dataset_sha = "a" * 64
        store_sha = "b" * 64
        request: dict[str, object] = {
            "execution_mode": "fixture",
            "process_lifetime": EXTERNAL_PROCESS_LIFETIME,
            "binary": {"path": str(binary), "sha256": sha256_file(binary)},
            "dataset": {"path": str(dataset_root), "sha256": dataset_sha},
            "truth": {
                "path": str(truth),
                "sha256": sha256_file(truth),
                "query_count": 2,
                "digest_algorithm": "mix64-dense-dst-count-sum-xor-v1",
            },
            "store_roots": [
                {"label": "neo4j-runtime", "path": str(store_root), "sha256": store_sha}
            ],
            "external_service": {
                "containers": ["neo4j-test"],
                "extra_pids": [],
                "image_digests": [IMAGE_DIGEST],
            },
        }
        request_path = root / "request.json"
        write_json(request_path, request)
        index = {
            "name": "v_id",
            "state": "ONLINE",
            "type": "RANGE",
            "entityType": "NODE",
            "labelsOrTypes": ["V"],
            "properties": ["id"],
        }
        container = {
            "name": "neo4j-test",
            "container_id": "container-id",
            "pid": 123,
            "started_at": "2026-07-22T00:00:00Z",
            "restart_count": 0,
            "image_id": IMAGE_ID,
            "configured_image": "neo4j:5.26.24",
            "container_user": f"{os.geteuid()}:{os.getegid()}",
            "repo_digests": [f"neo4j@{IMAGE_DIGEST}"],
            "expected_repo_digest": IMAGE_DIGEST,
            "data_mount": str(store_root),
            "logs_mount": str(logs_root),
            "bolt_host": "127.0.0.1",
            "bolt_port": 17687,
            "restart_policy": "no",
            "read_only_default": True,
            "docker_log_config": {"type": "none", "config": {}},
            "docker_memory_limit_bytes": 0,
            "docker_memory_swap_bytes": 0,
            "memory_configuration": adapter.expected_memory_contract("8G", "8G", "16G"),
            "entrypoint": ["/startup/docker-entrypoint.sh"],
            "command": ["neo4j"],
        }
        sentinel_files = [
            {"path": relative, "size_bytes": index + 1, "sha256": str(index + 3) * 64}
            for index, relative in enumerate(store_contract.CANONICAL_SENTINELS)
        ]
        tree_summary = store_contract.summarize_records(sentinel_files)
        store_sha = tree_summary["store_sha256"]
        request["store_roots"][0]["sha256"] = store_sha
        write_json(request_path, request)
        historical_import = {
            "status": "unverified-historical-store",
            "reference": None,
            "producer": None,
            "host": None,
            "image": {
                "configured_ref": "neo4j:5.26.24",
                "image_id": None,
                "repo_digests": [],
                "selected_repo_digest": None,
            },
            "input": None,
            "stages": None,
            "database_contract": None,
            "final_store": None,
        }
        store_lineage = {
            "schema_version": "p10-neo4j-store-manifest-v3",
            "snapshot_phase": "offline-prestart-v1",
            "known_mutable_patterns": list(store_contract.KNOWN_MUTABLE_PATTERNS),
            "canonical_sentinels": list(store_contract.CANONICAL_SENTINELS),
            "store_root": str(store_root),
            **tree_summary,
            "files": sentinel_files,
            "hash_method": store_contract.TREE_HASH_METHOD,
            "truth_sha256": request["truth"]["sha256"],
            "dataset_sha256": dataset_sha,
            "dataset_manifest_sha256": sha256_file(dataset_manifest),
            "relationship_model": "dense-edge-type-as-outgoing-relationship-type-v1",
            "runtime_compatibility": {
                "neo4j_version": "5.26.24",
                "image_ref": "neo4j:5.26.24",
                "image_digest": IMAGE_DIGEST,
            },
            "import_provenance": historical_import,
            "database_contract": {
                "database_name": "neo4j",
                "node_label": "V",
                "id_property": "id",
                "required_index": index,
            },
            "sentinel_files": sentinel_files,
            "offline_audit": {
                **recorded_offline_gate(store_root),
                "per_file_stat_stability": True,
                "owner_only_root": True,
                "exclusive_store_lock_held_across_hash": True,
                "docker_mount_rescan": True,
                "proc_scan_used": False,
            },
        }
        provenance: dict[str, object] = {
            "schema_version": "p10-neo4j-adapter-provenance-v4",
            "performance_eligible": False,
            "execution_mode": "fixture",
            "group": "client-server",
            "system_version": "Neo4j Community 5.26.24",
            "process_lifetime": EXTERNAL_PROCESS_LIFETIME,
            "request": artifact(request_path),
            "repo": None,
            "p02b": None,
            "python_binary": request["binary"],
            "python_driver": {
                "version": "5.28.3",
                "expected_package_tree_sha256": "d" * 64,
                "package_tree_sha256": "d" * 64,
                "hash_method": "sha256-tree-v1(relative-path,size,file-sha256)",
                "file_count": 1,
                "total_bytes": module.stat().st_size,
                "module": artifact(module),
            },
            "server_agent": "Neo4j/5.26.24",
            "launch": None,
            "container_lifecycle": {
                "stable": True,
                "before": dict(container),
                "after": dict(container),
            },
            "readiness": {"timeout_s": 180, "attempts": 1, "elapsed_ns": 1},
            "database_contract": {
                "database_name": "neo4j",
                "node_label": "V",
                "id_property": "id",
                "required_index": index,
            },
            "runtime_contract": {
                "database_status": {
                    "name": "neo4j",
                    "currentStatus": "online",
                    "requestedStatus": "online",
                    "access": "read-only",
                },
                "memory": {
                    "preregistered": adapter.expected_memory_contract("8G", "8G", "16G"),
                    "live": {
                        role: {"name": adapter.MEMORY_SETTING_NAMES[role], "value": value, "bytes": size}
                        for role, value, size in (
                            ("heap_initial", "8.00GiB", 8 * 1024**3),
                            ("heap_max", "8.00GiB", 8 * 1024**3),
                            ("pagecache", "16.00GiB", 16 * 1024**3),
                        )
                    },
                },
                "representative_plan": {
                    "cypher": "EXPLAIN MATCH (s:V {id: $src})-[:E_P1]->(d:V) RETURN d.id AS dst",
                    "parameters": {"src": 0},
                    "required_operator": "NodeIndexSeek",
                    "plan": {
                        "operator_type": "ProduceResults",
                        "identifiers": [],
                        "arguments": {},
                        "children": [
                            {
                                "operator_type": "NodeIndexSeek",
                                "identifiers": ["s"],
                                "arguments": {"Details": "RANGE INDEX s:V(id)"},
                                "children": [],
                            }
                        ],
                    },
                },
                "host": {"hostname": "host", "fingerprint_sha256": "e" * 64, "mem_total_bytes": 1024},
            },
            "jvm_lifecycle": {
                "stable": True,
                "before": {
                    "container_pid": 123,
                    "pid": 124,
                    "java_executable": "/java/bin/java",
                    "gc": "G1GC",
                    "argv": ["/java/bin/java", "-XX:+UseG1GC"],
                    "argv_sha256": "f" * 64,
                },
                "after": {
                    "container_pid": 123,
                    "pid": 124,
                    "java_executable": "/java/bin/java",
                    "gc": "G1GC",
                    "argv": ["/java/bin/java", "-XX:+UseG1GC"],
                    "argv_sha256": "f" * 64,
                },
            },
            "dataset_input": request["dataset"],
            "dataset": {
                "reference": artifact(dataset_manifest),
                "lineage": {"dataset_root": str(dataset_root), "dataset_sha256": dataset_sha},
            },
            "truth": artifact(truth),
            "store": {
                "reference": artifact(store_manifest),
                "lineage": store_lineage,
                "validated_sentinels": [
                    dict(sentinel, absolute_path=str((store_root / sentinel["path"]).resolve()))
                    for sentinel in sentinel_files
                ],
                "import_receipt": None,
                "preflight": None,
            },
            "query_contract": {
                "cypher_shape": "MATCH (s:V {id: $src})-[:E_{P|N}<type>]->(d:V) RETURN d.id AS dst",
                "relationship_model": "dense-edge-type-as-outgoing-relationship-type-v1",
                "database_name": "neo4j",
                "required_index": index,
                "clock": "CLOCK_MONOTONIC",
                "timing_boundary": "typed-neighbor-call-plus-result-materialization-and-digest-v1",
                "warmup_before_measured": True,
                "process_reuse_between_phases": True,
                "concurrency": 1,
            },
        }
        system = {
            "id": "neo4j",
            "group": "client-server",
            "system_version": "Neo4j Community 5.26.24",
            "display_name": "Neo4j Community",
            "fixture_only": False,
            "adapter": {
                "args": [
                    "--expected-driver-tree-sha256", "d" * 64,
                    "--logs-root", str(logs_root),
                ],
            },
        }
        return request, provenance, system

    def test_provenance_and_p31_exact_binding_accept_then_reject_pid_or_container_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-contract-") as raw:
            request, provenance, system = self.fixture(Path(raw))
            _validate_neo4j_provenance(provenance, request, system, EXTERNAL_PROCESS_LIFETIME)
            p31 = {
                "collector": {"containers": ["neo4j-test"], "extra_pids": []},
                "collector_result": {
                    "containers_seen": {"neo4j-test": 123},
                    "container_identity_unique_set": {
                        "neo4j-test": [
                            {
                                "container_id": "container-id",
                                "pid": 123,
                                "process_start_ticks": 456789,
                                "started_at": "2026-07-22T00:00:00Z",
                                "restart_count": 0,
                            }
                        ]
                    },
                    "container_identity_history": {
                        "neo4j-test": [
                            {
                                "container_id": "container-id",
                                "pid": 123,
                                "process_start_ticks": 456789,
                                "started_at": "2026-07-22T00:00:00Z",
                                "restart_count": 0,
                                "observed_at_utc": "2026-07-22T00:00:01Z",
                                "before_sample_index": 0,
                            }
                        ]
                    },
                    "ready": {
                        "containers": {
                            "neo4j-test": {
                                "container_id": "container-id",
                                "pid": 123,
                                "process_start_ticks": 456789,
                                "started_at": "2026-07-22T00:00:00Z",
                                "restart_count": 0,
                            }
                        }
                    },
                },
                "inputs": {
                    "binary": provenance["python_binary"],
                    "dataset": provenance["dataset_input"],
                    "truth": provenance["truth"],
                    "query_or_trace": provenance["truth"],
                },
                "disk_roots": [
                    {
                        "role": "store",
                        "path": provenance["store"]["lineage"]["store_root"],
                    },
                    {
                        "role": "temp",
                        "path": provenance["container_lifecycle"]["before"]["logs_mount"],
                    },
                ],
                "repo": {"git_sha": "head", "dirty": False},
                "host": {"fingerprint_sha256": "e" * 64, "mem_total_bytes": 1024},
            }
            formal_provenance = copy.deepcopy(provenance)
            formal_provenance["repo"] = {"head": "head", "clean": True}
            before = formal_provenance["container_lifecycle"]["before"]
            formal_provenance["launch"] = {
                "receipt": {
                    "docker": {
                        "running": {
                            "config": {
                                "container_name": before["name"],
                                "container_id": before["container_id"],
                                "container_user": before["container_user"],
                            },
                            "runtime": {
                                "pid": before["pid"],
                                "started_at": before["started_at"],
                                "restart_count": before["restart_count"],
                            },
                        }
                    },
                    "runtime_contract": {
                        "store_root": before["data_mount"],
                        "logs_root": before["logs_mount"],
                        "bolt_port": before["bolt_port"],
                        "container_user": before["container_user"],
                    },
                }
            }
            validate_neo4j_p31_binding(formal_provenance, p31)

            drift = copy.deepcopy(provenance)
            drift["container_lifecycle"]["after"]["pid"] = 999
            with self.assertRaisesRegex(ContractError, "pid changed"):
                _validate_neo4j_provenance(drift, request, system, EXTERNAL_PROCESS_LIFETIME)

            bad_p31 = copy.deepcopy(p31)
            bad_p31["collector"]["containers"] = ["other"]
            with self.assertRaisesRegex(ContractError, "container coverage"):
                validate_neo4j_p31_binding(formal_provenance, bad_p31)

            p31_drift = copy.deepcopy(formal_provenance)
            p31_drift["container_lifecycle"]["after"]["started_at"] = "later"
            with self.assertRaisesRegex(ContractError, "identity changed"):
                validate_neo4j_p31_binding(p31_drift, p31)


class Neo4jFailureCleanupTests(unittest.TestCase):
    TARGET = "cidr-p10-neo4j-sf10-r01"
    TARGET_ID = "1" * 64
    OTHER = "unrelated-user-container"
    OTHER_ID = "2" * 64

    @classmethod
    def lifecycle(cls, root: Path) -> tuple[dict[str, object], dict[str, object]]:
        system: dict[str, object] = {
            "id": "neo4j",
            "active_repeat_binding": {"container": cls.TARGET},
        }
        lifecycle: dict[str, object] = {
            "receipt_root": root / "receipts",
            "launch_path": root / "receipts" / "launch-receipt.json",
            "launch_sha256": "a" * 64,
            "launch": {
                "runtime_contract": {"container_name": cls.TARGET},
                "docker": {"running": {"config": {"container_id": cls.TARGET_ID}}},
            },
        }
        return system, lifecycle

    @staticmethod
    def inspect_document(name: str, container_id: str, running: bool) -> dict[str, object]:
        return {
            "Id": container_id,
            "Name": f"/{name}",
            "RestartCount": 0,
            "State": {
                "Running": running,
                "Pid": 1234 if running else 0,
                "ExitCode": 0,
            },
        }

    def test_cleanup_gracefully_stops_and_removes_only_launch_bound_target(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-failure-cleanup-") as raw:
            root = Path(raw)
            system, lifecycle = self.lifecycle(root)
            registry = {
                self.TARGET: {"id": self.TARGET_ID, "running": True},
                self.OTHER: {"id": self.OTHER_ID, "running": True},
            }
            commands: list[list[str]] = []

            def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                commands.append(list(command))
                if command[:3] == ["docker", "container", "inspect"]:
                    container_id = command[3]
                    name = next(name for name, entry in registry.items() if entry["id"] == container_id)
                    entry = registry[name]
                    document = self.inspect_document(name, str(entry["id"]), bool(entry["running"]))
                    return subprocess.CompletedProcess(command, 0, json.dumps([document]), "")
                if command[:5] == ["docker", "container", "stop", "--time", "60"]:
                    container_id = command[5]
                    name = next(name for name, entry in registry.items() if entry["id"] == container_id)
                    registry[name]["running"] = False
                    return subprocess.CompletedProcess(command, 0, container_id + "\n", "")
                if command[:3] == ["docker", "container", "rm"]:
                    container_id = command[3]
                    name = next(name for name, entry in registry.items() if entry["id"] == container_id)
                    registry.pop(name)
                    return subprocess.CompletedProcess(command, 0, container_id + "\n", "")
                if command[:4] == ["docker", "container", "ls", "--all"]:
                    name = self.TARGET
                    stdout = "" if name not in registry else str(registry[name]["id"]) + "\n"
                    return subprocess.CompletedProcess(command, 0, stdout, "")
                raise AssertionError(command)

            with mock.patch.object(orchestrator.subprocess, "run", side_effect=fake_run):
                cleaned = orchestrator.cleanup_failed_neo4j_repeat(
                    system=system,
                    repeat_dir=root,
                    lifecycle=lifecycle,
                    failure=ContractError("readiness failed"),
                )

            self.assertNotIn(self.TARGET, registry)
            self.assertEqual(registry[self.OTHER], {"id": self.OTHER_ID, "running": True})
            self.assertTrue(cleaned["failure_cleanup"]["outcome"]["gracefully_stopped"])
            self.assertTrue(cleaned["failure_cleanup"]["outcome"]["container_absent"])
            self.assertTrue((root / "neo4j-failure-cleanup-intent.json").is_file())
            self.assertTrue((root / "neo4j-failure-cleanup.json").is_file())
            flattened = json.dumps(commands)
            self.assertIn(self.TARGET_ID, flattened)
            self.assertNotIn(self.OTHER, flattened)
            stop = [command for command in commands if command[:3] == ["docker", "container", "stop"]]
            self.assertEqual(
                stop,
                [["docker", "container", "stop", "--time", "60", self.TARGET_ID]],
            )
            remove = [command for command in commands if command[:3] == ["docker", "container", "rm"]]
            self.assertEqual(remove, [["docker", "container", "rm", self.TARGET_ID]])

    def test_name_reuse_is_rejected_before_stop_or_remove(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-failure-name-reuse-") as raw:
            root = Path(raw)
            system, lifecycle = self.lifecycle(root)
            wrong = self.inspect_document(self.TARGET, self.OTHER_ID, True)
            completed = subprocess.CompletedProcess([], 0, json.dumps([wrong]), "")
            with mock.patch.object(orchestrator.subprocess, "run", return_value=completed):
                with self.assertRaisesRegex(ContractError, "ID differs"):
                    orchestrator.cleanup_failed_neo4j_repeat(
                        system=system,
                        repeat_dir=root,
                        lifecycle=lifecycle,
                        failure=ContractError("post-audit failed"),
                    )
            self.assertFalse((root / "neo4j-failure-cleanup.json").exists())

    def test_all_fault_injections_recover_fake_launch_and_preserve_primary_failure(self) -> None:
        injections: list[tuple[str, object, type[BaseException]]] = [
            ("readiness", subprocess.CompletedProcess(["fake-p31"], 2), ContractError),
            ("adapter-p31", OSError("adapter/P31 failed"), ContractError),
            ("outer-timeout", subprocess.TimeoutExpired(["fake-p31"], 1), ContractError),
            ("outer-interrupt", KeyboardInterrupt("interrupted"), KeyboardInterrupt),
            ("post-audit", subprocess.CompletedProcess(["fake-p31"], 0), ContractError),
        ]
        for stage, runner_result, expected_exception in injections:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory(
                prefix=f"neo4j-inject-{stage}-"
            ) as raw:
                root = Path(raw)
                run_root = root / "run"
                run_root.mkdir()
                repeat_dir = run_root / "systems" / "neo4j" / "repeat-01"
                resolved_manifest = run_root / "resolved.json"
                write_json(resolved_manifest, {"fixture": "failure-injection"})
                store_root = root / "repeat-root" / "neo4j-runtime"
                logs_root = root / "repeat-root" / "logs"
                source_root = root / "source-store"
                store_root.mkdir(parents=True)
                logs_root.mkdir()
                source_root.mkdir()
                store_manifest = root / "repeat-root" / "store-manifest.json"
                import_receipt = root / "repeat-root" / "import-receipt.json"
                write_json(store_manifest, {"fixture": "store"})
                write_json(import_receipt, {"fixture": "import"})
                adapter_args = [
                    "--uri", "bolt://127.0.0.1:17687",
                    "--logs-root", str(logs_root),
                    "--store-manifest", str(store_manifest),
                    "--store-manifest-sha256", sha256_file(store_manifest),
                    "--import-receipt", str(import_receipt),
                    "--import-receipt-sha256", sha256_file(import_receipt),
                ]
                binding = {
                    "repeat_index": 1,
                    "clone_id": "cidr-p10-neo4j-sf10-r01",
                    "repeat_root": str((root / "repeat-root").resolve()),
                    "source_store_root": str(source_root.resolve()),
                    "launcher": {"path": str(root / "fake-launcher"), "sha256": "a" * 64},
                    "store_root": {
                        "label": "neo4j-runtime",
                        "path": str(store_root.resolve()),
                        "sha256": "b" * 64,
                    },
                    "logs_root": {"label": "neo4j-logs", "path": str(logs_root.resolve())},
                    "container": self.TARGET,
                    "uri": "bolt://127.0.0.1:17687",
                    "store_manifest": artifact(store_manifest),
                    "import_receipt": artifact(import_receipt),
                }
                system = {
                    "id": "neo4j",
                    "display_name": "Neo4j",
                    "group": "client-server",
                    "system_version": "Neo4j Community 5.26.24",
                    "service_lifecycle": "external-prestarted",
                    "process_lifetime": "external-prestarted-query-process-lifetime-v1",
                    "adapter": {"path": str(root / "adapter"), "sha256": "c" * 64, "args": adapter_args},
                    "binary": {"path": sys.executable, "sha256": "d" * 64},
                    "runtime_libraries": [],
                    "store_roots": [dict(binding["store_root"])],
                    "temp_roots": [dict(binding["logs_root"])],
                    "containers": [self.TARGET],
                    "extra_pids": [],
                    "image_digests": [IMAGE_DIGEST],
                    "repeat_bindings": [binding],
                }
                suite = {
                    "suite_id": "neo4j-finally-fault-injection",
                    "dataset": {"path": str(root / "dataset"), "sha256": "e" * 64},
                    "truth": {
                        "path": str(root / "truth"),
                        "sha256": "f" * 64,
                        "query_count": 1,
                        "digest_algorithm": "mix64-dense-dst-count-sum-xor-v1",
                    },
                    "protocol": {
                        "cache_policy": "full-shared-trace-before-each-measured-repeat-v1",
                        "warmup_passes": 1,
                        "measured_passes": 1,
                        "concurrency": 1,
                        "per_query_timeout_ms": 100,
                        "max_timeouts": 0,
                    },
                    "resources": {
                        "device": "fixture",
                        "data_mount": str(root),
                        "interval_s": 1,
                        "disk_interval_s": 1,
                        "min_samples": 2,
                    },
                }
                _, lifecycle = self.lifecycle(root)
                registry = {
                    self.TARGET: {"id": self.TARGET_ID, "running": True},
                    self.OTHER: {"id": self.OTHER_ID, "running": True},
                }
                stop_calls: list[str] = []

                def fake_audit(**kwargs: object) -> dict[str, object]:
                    if kwargs["stage"] == "post-stop" and stage == "post-audit":
                        raise ContractError("post-audit failed")
                    write_json(Path(kwargs["output_path"]), {"stage": kwargs["stage"]})
                    return {"stage": kwargs["stage"]}

                def fake_stop(**kwargs: object) -> dict[str, object]:
                    target = kwargs["system"]["active_repeat_binding"]["container"]
                    stop_calls.append(target)
                    self.assertEqual(target, self.TARGET)
                    self.assertTrue(registry[target]["running"])
                    registry[target]["running"] = False
                    stop_path = root / "stop-receipt.json"
                    write_json(stop_path, {"fixture": "graceful-stop", "target": target})
                    lifecycle.update(
                        {
                            "stop": {"outcome": {"container_running": False}},
                            "stop_path": stop_path,
                            "stop_sha256": sha256_file(stop_path),
                        }
                    )
                    return lifecycle

                def fake_cleanup(**kwargs: object) -> dict[str, object]:
                    self.assertFalse(registry[self.TARGET]["running"])
                    registry.pop(self.TARGET)
                    write_json(
                        Path(kwargs["repeat_dir"]) / "neo4j-failure-cleanup.json",
                        {
                            "target": self.TARGET,
                            "gracefully_stopped": True,
                            "container_absent": True,
                        },
                    )
                    return dict(kwargs["lifecycle"])

                run_side_effect = runner_result if isinstance(runner_result, BaseException) else None
                run_return_value = None if run_side_effect is not None else runner_result
                with mock.patch.object(
                    orchestrator, "audit_neo4j_runtime_store", side_effect=fake_audit
                ), mock.patch.object(
                    orchestrator, "validate_neo4j_store_audit", side_effect=lambda value, **_kwargs: value
                ), mock.patch.object(
                    orchestrator, "validate_neo4j_store_audit_pair"
                ), mock.patch.object(
                    orchestrator, "launch_neo4j_repeat", return_value=lifecycle
                ), mock.patch.object(
                    orchestrator, "stop_neo4j_repeat", side_effect=fake_stop
                ) as graceful_stop, mock.patch.object(
                    orchestrator, "p31_command", return_value=["fake-p31"]
                ), mock.patch.object(
                    orchestrator.subprocess,
                    "run",
                    side_effect=run_side_effect,
                    return_value=run_return_value,
                ), mock.patch.object(
                    orchestrator, "_recover_neo4j_lifecycle", return_value=(system, lifecycle)
                ) as recover, mock.patch.object(
                    orchestrator, "cleanup_failed_neo4j_repeat", side_effect=fake_cleanup
                ) as cleanup:
                    with self.assertRaises(expected_exception):
                        orchestrator.execute_repeat(
                            suite=suite,
                            system=system,
                            truth_rows=[],
                            repeat_index=1,
                            run_root=run_root,
                            resolved_manifest=resolved_manifest,
                            p31_wrapper=root / "p31.sh",
                            mode="formal",
                        )
                self.assertEqual(graceful_stop.call_count, 1)
                self.assertEqual(stop_calls, [self.TARGET])
                self.assertEqual(recover.call_count, 1)
                self.assertEqual(cleanup.call_count, 1)
                self.assertNotIn(self.TARGET, registry)
                self.assertEqual(registry[self.OTHER], {"id": self.OTHER_ID, "running": True})
                context = json.loads(
                    (repeat_dir / "neo4j-failure-context.json").read_text(encoding="utf-8")
                )
                self.assertEqual(context["failure"]["type"], expected_exception.__name__)
                self.assertTrue((repeat_dir / "neo4j-failure-cleanup.json").is_file())
                self.assertFalse((run_root / "DONE").exists())
                self.assertFalse((run_root / "PARTIAL-DONE").exists())

    def test_outer_interrupt_publishes_failed_and_never_done(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-run-interrupt-") as raw:
            run_root = Path(raw) / "run"
            suite = {
                "protocol": {"repeats": 1},
                "fixture_only": True,
            }
            selected = [{"id": "seml0"}]
            args = Namespace(
                manifest=P10_DIR / "tests" / "fixture-suite.json",
                run_root=run_root,
                p31_wrapper=orchestrator.DEFAULT_P31,
                mode="fixture",
                clean_ready_file=None,
                system=[],
                group=None,
                repeat_index=None,
            )
            with mock.patch.object(
                orchestrator, "load_suite_manifest", return_value=(suite, [])
            ), mock.patch.object(
                orchestrator, "select_systems", return_value=selected
            ), mock.patch.object(
                orchestrator, "execute_repeat", side_effect=KeyboardInterrupt("outer interrupt")
            ):
                with self.assertRaises(KeyboardInterrupt):
                    orchestrator.run(args)
            failed = json.loads((run_root / "FAILED").read_text())
            self.assertEqual(failed["error_type"], "KeyboardInterrupt")
            self.assertFalse((run_root / "DONE").exists())
            self.assertFalse((run_root / "PARTIAL-DONE").exists())
            self.assertFalse((run_root / "RUNNING").exists())


if __name__ == "__main__":
    unittest.main()
