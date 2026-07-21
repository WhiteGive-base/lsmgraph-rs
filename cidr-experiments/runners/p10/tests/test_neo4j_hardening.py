#!/usr/bin/env python3
"""Container-free tests for Neo4j lineage and lifecycle hardening."""

from __future__ import annotations

import copy
import hashlib
import json
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
from p10_contract import (  # noqa: E402
    ContractError,
    EXTERNAL_PROCESS_LIFETIME,
    _validate_neo4j_provenance,
    sha256_file,
    validate_neo4j_p31_binding,
)

IMAGE_DIGEST = "sha256:" + "1" * 64
IMAGE_ID = "sha256:" + "2" * 64


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def artifact(path: Path) -> dict[str, object]:
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
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
    def test_tree_hashes_each_sentinel_only_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-freezer-tree-") as raw:
            root = Path(raw)
            first = root / "databases" / "neo4j" / "nodes"
            second = root / "databases" / "neo4j" / "relationships"
            first.parent.mkdir(parents=True)
            first.write_bytes(b"nodes")
            second.write_bytes(b"relationships")
            real_hash = freezer.sha256_file
            calls: list[Path] = []

            def counted(path: Path) -> str:
                calls.append(path.resolve())
                return real_hash(path)

            with mock.patch.object(freezer, "sha256_file", side_effect=counted):
                _summary, records = freezer.tree_manifest(root)
            self.assertEqual(calls.count(first.resolve()), 1)
            self.assertEqual(calls.count(second.resolve()), 1)
            self.assertEqual(set(records), {"databases/neo4j/nodes", "databases/neo4j/relationships"})

    def test_running_alias_container_with_parent_mount_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-freezer-mount-") as raw:
            root = Path(raw) / "store"
            root.mkdir()
            listed = subprocess.CompletedProcess([], 0, stdout="abc\n", stderr="")
            inspected = subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps(
                    [
                        {
                            "Id": "abc",
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

    def test_manifest_marks_legacy_import_as_unverified_without_rehashing_sentinel(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-freezer-manifest-") as raw:
            root = Path(raw)
            store = root / "store"
            sentinel = store / "databases" / "neo4j" / "neostore"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_bytes(b"store")
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
                import_image_ref="neo4j:5.26.24",
                import_image_identity="unverified-tag-only",
                import_image_digest=None,
                database_name="neo4j",
                index_name="v_id",
                node_label="V",
                id_property="id",
                sentinel=["databases/neo4j/neostore"],
                assert_container_stopped=None,
                output=output,
            )
            real_hash = freezer.sha256_file
            calls: list[Path] = []

            def counted(path: Path) -> str:
                calls.append(path.resolve())
                return real_hash(path)

            with mock.patch.object(freezer, "assert_store_offline"), mock.patch.object(
                freezer, "sha256_file", side_effect=counted
            ):
                freezer.run(args)
            value = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(value["schema_version"], "p10-neo4j-store-manifest-v2")
            self.assertEqual(value["snapshot_phase"], "offline-prestart-v1")
            self.assertEqual(value["import_image_identity"]["status"], "unverified-tag-only")
            self.assertIsNone(value["import_image_identity"]["image_digest"])
            self.assertEqual(calls.count(sentinel.resolve()), 1)


class Neo4jAdapterPureTests(unittest.TestCase):
    def container(self, root: Path, *, restart_count: int = 0, pid: int = 123) -> dict[str, object]:
        return {
            "Id": "container-id",
            "Name": "/neo4j-test",
            "Image": IMAGE_ID,
            "RestartCount": restart_count,
            "State": {"Running": True, "Pid": pid, "StartedAt": "2026-07-22T00:00:00Z"},
            "HostConfig": {"RestartPolicy": {"Name": "no"}},
            "Config": {
                "Image": "neo4j:5.26.24",
                "Env": [
                    "NEO4J_AUTH=none",
                    "NEO4J_server_databases_default__to__read__only=true",
                ],
            },
            "Mounts": [
                {
                    "Destination": "/data",
                    "Source": str(root.resolve()),
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
            image = {"RepoDigests": [f"neo4j@{IMAGE_DIGEST}"]}
            with mock.patch.object(
                adapter,
                "run_json",
                side_effect=[[self.container(root)], [image]],
            ):
                value = adapter.validate_container(
                    "neo4j-test",
                    "neo4j:5.26.24",
                    IMAGE_DIGEST,
                    root,
                    "bolt://127.0.0.1:17687",
                    False,
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
            with mock.patch.object(adapter, "run_json", return_value=[self.container(Path(raw), restart_count=1)]):
                with self.assertRaisesRegex(ContractError, "RestartCount=0"):
                    adapter.validate_container(
                        "neo4j-test",
                        "neo4j:5.26.24",
                        IMAGE_DIGEST,
                        Path(raw),
                        "bolt://127.0.0.1:17687",
                        True,
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
    def fixture(self, root: Path) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        binary = root / "python"
        module = root / "neo4j.py"
        truth = root / "truth.tsv"
        dataset_manifest = root / "dataset-manifest.json"
        store_manifest = root / "store-manifest.json"
        dataset_root = root / "dataset"
        store_root = root / "store"
        dataset_root.mkdir()
        store_root.mkdir()
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
            "repo_digests": [f"neo4j@{IMAGE_DIGEST}"],
            "expected_repo_digest": IMAGE_DIGEST,
            "data_mount": str(store_root),
            "bolt_host": "127.0.0.1",
            "bolt_port": 17687,
            "restart_policy": "no",
            "read_only_default": True,
        }
        sentinel = {"path": "databases/neo4j/neostore", "size_bytes": 1, "sha256": "c" * 64}
        store_lineage = {
            "schema_version": "p10-neo4j-store-manifest-v2",
            "snapshot_phase": "offline-prestart-v1",
            "mutable_runtime_paths": ["logs/**", "server_id", "transactions/**"],
            "store_root": str(store_root),
            "store_sha256": store_sha,
            "truth_sha256": request["truth"]["sha256"],
            "dataset_sha256": dataset_sha,
            "runtime_compatibility": {
                "neo4j_version": "5.26.24",
                "image_ref": "neo4j:5.26.24",
                "image_digest": IMAGE_DIGEST,
            },
            "import_image_identity": {
                "status": "unverified-tag-only",
                "image_ref": "neo4j:5.26.24",
                "image_digest": None,
            },
            "database_contract": {
                "database_name": "neo4j",
                "node_label": "V",
                "id_property": "id",
                "required_index": {"name": "v_id", "state": "ONLINE", "type": "RANGE"},
            },
            "sentinel_files": [sentinel],
        }
        provenance: dict[str, object] = {
            "schema_version": "p10-neo4j-adapter-provenance-v2",
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
                "package_tree_sha256": "d" * 64,
                "hash_method": "sha256-tree-v1(relative-path,size,file-sha256)",
                "file_count": 1,
                "total_bytes": module.stat().st_size,
                "module": artifact(module),
            },
            "server_agent": "Neo4j/5.26.24",
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
            "dataset_input": request["dataset"],
            "dataset": {
                "reference": artifact(dataset_manifest),
                "lineage": {"dataset_root": str(dataset_root), "dataset_sha256": dataset_sha},
            },
            "truth": artifact(truth),
            "store": {
                "reference": artifact(store_manifest),
                "lineage": store_lineage,
                "validated_sentinels": [dict(sentinel, absolute_path=str(store_root / sentinel["path"]))],
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
        }
        return request, provenance, system

    def test_provenance_and_p31_exact_binding_accept_then_reject_pid_or_container_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-contract-") as raw:
            request, provenance, system = self.fixture(Path(raw))
            _validate_neo4j_provenance(provenance, request, system, EXTERNAL_PROCESS_LIFETIME)
            p31 = {
                "collector": {"containers": ["neo4j-test"], "extra_pids": []},
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
                    }
                ],
                "repo": {"git_sha": "head", "dirty": False},
            }
            formal_provenance = copy.deepcopy(provenance)
            formal_provenance["repo"] = {"head": "head", "clean": True}
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


if __name__ == "__main__":
    unittest.main()
