#!/usr/bin/env python3
"""Pure tempdir/mock gates for the NebulaGraph formal cluster lifecycle."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

P10_DIR = Path(__file__).resolve().parents[1]
NEBULA_DIR = P10_DIR / "adapters" / "nebulagraph"
sys.path.insert(0, str(P10_DIR))
sys.path.insert(0, str(NEBULA_DIR))

import formal_cluster as cluster  # noqa: E402
import nebula_adapter as adapter  # noqa: E402
import run_suite  # noqa: E402
import store_contract as contract  # noqa: E402
from p10_contract import ContractError, sha256_file  # noqa: E402


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class NebulaGraphFormalClusterTest(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        real_reader = cluster.process_start_ticks

        def fixture_reader(pid: int) -> int:
            if 1000 <= pid <= 1002:
                return 500_000 + pid
            return real_reader(pid)

        patcher = mock.patch.object(cluster, "process_start_ticks", side_effect=fixture_reader)
        patcher.start()
        self.addCleanup(patcher.stop)

    @unittest.skipIf(run_suite.fcntl is None, "POSIX flock is required")
    def test_sealed_admission_binds_tree_stat_lock_owner_and_detects_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nebula-sealed-admission-") as raw:
            root = Path(raw)
            request = root / "request.json"
            runtime = root / "runtime.json"
            store_manifest = root / "store.json"
            dataset = root / "dense.txt"
            truth = root / "truth.tsv"
            imported = root / "golden"
            store = root / "clone"
            imported.mkdir()
            store.mkdir()
            (imported / "part").write_text("golden\n", encoding="utf-8")
            (store / "part").write_text("clone\n", encoding="utf-8")
            for path, value in (
                (request, {"request": True}),
                (runtime, {"runtime": True}),
                (store_manifest, {"store": True}),
            ):
                write_json(path, value)
            dataset.write_text("dense\n", encoding="utf-8")
            truth.write_text("truth\n", encoding="utf-8")
            import_receipt = root / "import.json"
            clone_receipt = root / "clone.json"
            write_json(import_receipt, {"import": True})
            write_json(clone_receipt, {"clone": True})
            lock = run_suite.acquire_nebulagraph_store_lock(root / "store.lock")
            try:
                receipt = {
                    "schema_version": cluster.SEALED_ADMISSION_SCHEMA,
                    "state": "PASS",
                    "created_at_utc": cluster.utc_now(),
                    "producer": contract.file_ref(Path(cluster.__file__)),
                    "repo": {"root": str(root), "head": "a" * 40, "clean": True, "status_sha256": "b" * 64},
                    "request": {
                        **contract.file_ref(request),
                        "stat": cluster.stat_seal(request, "test request"),
                    },
                    "artifacts": {
                        name: {**contract.file_ref(path), "stat": cluster.stat_seal(path, f"test {name}")}
                        for name, path in (
                            ("runtime_manifest", runtime),
                            ("store_manifest", store_manifest),
                            ("dataset", dataset),
                            ("truth", truth),
                        )
                    },
                    "store": {
                        "path": str(store.resolve()),
                        "root_stat": cluster.stat_seal(store, "test store", directory=True),
                        "tree": {"path": str(store.resolve()), **contract.tree_identity(store)},
                    },
                    "lock": {
                        "stat": cluster.stat_seal(lock["path"], "test lock"),
                        "owner": lock["owner"],
                    },
                    "lineage": {
                        "import_receipt": contract.file_ref(import_receipt),
                        "clone_receipt": contract.file_ref(clone_receipt),
                        "golden_store": {"path": str(imported.resolve()), **contract.tree_identity(imported)},
                        "clone_source": {"path": str(imported.resolve()), **contract.tree_identity(imported)},
                        "clone_target": {"path": str(store.resolve()), **contract.tree_identity(store)},
                        "clone_run_id": "sealed-run",
                        "clone_repeat_index": 1,
                    },
                    "validated": {
                        "system_version": contract.SYSTEM_VERSION,
                        "run_id": "sealed-run",
                        "repeat_index": 1,
                        "edge_type_labels": [],
                        "p02b": {},
                    },
                }
                receipt_path = root / "sealed.json"
                write_json(receipt_path, receipt)
                with mock.patch.object(cluster, "validate_current_repo"):
                    parsed_path, parsed = cluster.validate_sealed_admission_receipt(
                        receipt_path,
                        sha256_file(receipt_path),
                        request_path=request,
                        request_sha256=sha256_file(request),
                    )
                self.assertEqual(parsed_path, receipt_path.resolve())
                self.assertEqual(parsed["store"]["tree"]["sha256"], contract.tree_identity(store)["sha256"])
                dataset.write_text("dense-drift\n", encoding="utf-8")
                with mock.patch.object(cluster, "validate_current_repo"):
                    with self.assertRaisesRegex(ContractError, "dataset stat.*drift"):
                        cluster.validate_sealed_admission_receipt(
                            receipt_path,
                            sha256_file(receipt_path),
                            request_path=request,
                            request_sha256=sha256_file(request),
                        )
            finally:
                run_suite.release_nebulagraph_store_lock({"_store_lock": lock})

    def _images(self) -> list[dict[str, str]]:
        images = []
        for index, role in enumerate(("graphd", "metad", "storaged"), start=1):
            expected = contract.EXPECTED_IMAGES[role]
            images.append(
                {
                    "role": role,
                    "tag": expected["tag"],
                    "repo_digest": f"{expected['tag'].split(':', 1)[0]}@{expected['digest']}",
                    "image_id": "sha256:" + str(index) * 64,
                }
            )
        return images

    def _repo(self, root: Path) -> dict[str, object]:
        return {
            "root": str(root.resolve()),
            "head": "a" * 40,
            "clean": True,
            "status_sha256": hashlib.sha256(b"").hexdigest(),
        }

    def _fixture(
        self,
        root: Path,
        *,
        run_id: str = "run007",
        repeat_index: int = 2,
        identity_token: str = "run007",
        graph_port: int = 19669,
    ) -> tuple[dict[str, object], str]:
        root.mkdir(parents=True, exist_ok=True)
        data_root = root / "clone"
        logs_root = root / "logs"
        client_root = root / "client"
        data_root.mkdir()
        logs_root.mkdir()
        client_root.mkdir()
        for name in ("meta", "storage", "graph"):
            (data_root / name).mkdir()
        (client_root / "nebula3.py").write_text("VERSION = '3.8.3'\n", encoding="utf-8")
        containers = {
            "metad": f"cidr-nebula-meta-{identity_token}",
            "storaged": f"cidr-nebula-storage-{identity_token}",
            "graphd": f"cidr-nebula-graph-{identity_token}",
        }
        request = {
            "execution_mode": "formal",
            "run_id": run_id,
            "repeat_index": repeat_index,
            "external_service": {
                "containers": [containers[role] for role in ("graphd", "metad", "storaged")]
            },
        }
        runtime = {
            "images": self._images(),
            "client": {"tree": {"path": str(client_root.resolve()), **adapter.tree_identity(client_root)}},
        }
        password = "formal-secret"
        store = {
            "data_root": {"path": str(data_root.resolve())},
            "containers": containers,
            "logical_hosts": dict(contract.LOGICAL_HOSTS),
            "network": f"cidr-nebula-net-{identity_token}",
            "graph_endpoint": {"host": "127.0.0.1", "port": graph_port},
            "space": "sf10",
            "authentication": {
                "user": "root",
                "password_env": "CIDR_NEBULA_PASSWORD",
                "password_sha256": hashlib.sha256(password.encode("utf-8")).hexdigest(),
            },
            "edge_type_labels": [{"edge_type": index, "label": f"E_{index:02d}"} for index in range(34)],
        }
        spec = cluster.build_launch_spec(request, runtime, store, logs_root, query_timeout_ms=5000)
        cluster.validate_launch_spec(spec)
        return spec, password

    def _inspect(
        self,
        spec: dict[str, object],
        role: str,
        preflight_sha: str,
        container_id: str | None = None,
    ) -> dict[str, object]:
        item = spec["roles"][role]
        cid = container_id or ({"metad": "1", "storaged": "2", "graphd": "3"}[role] * 64)
        labels = {
            **spec["labels"],
            "cidr.p10.role": role,
            "cidr.p10.preflight_sha256": preflight_sha,
        }
        mounts = [
            {
                "Type": mount["type"],
                "Source": mount["source"],
                "Destination": mount["destination"],
                "RW": not mount["read_only"],
            }
            for mount in reversed(item["mounts"])
        ]
        return {
            "Id": cid,
            "Name": "/" + item["container_name"],
            "Image": item["image"]["image_id"],
            "Config": {
                "Image": item["image"]["repo_digest"],
                "Cmd": item["command"],
                "Hostname": item["logical_host"],
                "Labels": labels,
            },
            "HostConfig": {
                "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                "NetworkMode": spec["network"]["name"],
                "PortBindings": item["port_bindings"],
            },
            "State": {
                "Status": "running",
                "Running": True,
                "Pid": 1000 + cluster.ROLE_ORDER.index(role),
                "StartedAt": "2026-07-22T00:00:00Z",
                "Restarting": False,
                "Paused": False,
                "Dead": False,
            },
            "RestartCount": 0,
            "Mounts": mounts,
            "NetworkSettings": {
                "Networks": {
                    spec["network"]["name"]: {
                        "Aliases": [item["logical_host"], item["container_name"]],
                        "IPAddress": "172.30.0." + str(cluster.ROLE_ORDER.index(role) + 2),
                    }
                }
            },
        }

    def _network(
        self, spec: dict[str, object], preflight_sha: str, ids: dict[str, str]
    ) -> dict[str, object]:
        return {
            "Id": "9" * 64,
            "Name": spec["network"]["name"],
            "Driver": "bridge",
            "Labels": {**spec["labels"], "cidr.p10.preflight_sha256": preflight_sha},
            "Containers": {container_id: {} for container_id in ids.values()},
        }

    def _live_results(self, spec: dict[str, object]) -> dict[str, object]:
        roles = spec["roles"]
        storage = roles["storaged"]["logical_host"]
        results: dict[str, object] = {
            "SHOW HOSTS META": [
                {"Host": roles["metad"]["logical_host"], "Port": 9559, "Status": "ONLINE"}
            ],
            "SHOW HOSTS STORAGE": [
                {"Host": storage, "Port": 9779, "Status": "ONLINE"}
            ],
            "SHOW HOSTS GRAPH": [
                {"Host": roles["graphd"]["logical_host"], "Port": 9669, "Status": "ONLINE"}
            ],
            "SHOW PARTS": [
                {
                    "Partition ID": partition,
                    "Leader": f"{storage}:9779",
                    "Peers": f"[{storage}:9779]",
                    "Losts": "[]",
                }
                for partition in range(1, 65)
            ],
            f"DESCRIBE SPACE {spec['space']}": [
                {
                    "Name": spec["space"],
                    "Partition Number": 64,
                    "Replica Factor": 1,
                    "Vid Type": "INT64",
                }
            ],
            "SHOW EDGES": [{"Name": name} for name in spec["edge_labels"]],
            "SHOW TAGS": [],
            "SHOW EDGE INDEXES": [],
            "SHOW TAG INDEXES": [],
        }
        return results

    def _live_snapshot(self, spec: dict[str, object]) -> dict[str, object]:
        snapshot = cluster.validate_live_results(spec, self._live_results(spec))
        snapshot.update(
            {
                "attempts": 1,
                "query_timeout_ms": spec["query_timeout_ms"],
                "startup_timeout_seconds": 30,
                "captured_at_utc": "2026-07-22T00:01:00Z",
            }
        )
        return snapshot

    def _snapshots(self, spec: dict[str, object], preflight_sha: str) -> dict[str, object]:
        return {
            role: cluster.validate_container_inspect(
                spec, role, self._inspect(spec, role, preflight_sha), preflight_sha256=preflight_sha
            )
            for role in cluster.ROLE_ORDER
        }

    def test_launch_spec_separates_frozen_hosts_and_unique_container_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spec, _password = self._fixture(Path(directory))
            names = {spec["roles"][role]["container_name"] for role in cluster.ROLE_ORDER}
            aliases = {spec["roles"][role]["logical_host"] for role in cluster.ROLE_ORDER}
            self.assertEqual(3, len(names))
            self.assertEqual(set(contract.LOGICAL_HOSTS.values()), aliases)
            self.assertTrue(names.isdisjoint(aliases))
            self.assertIn(
                f"--meta_server_addrs={contract.LOGICAL_HOSTS['metad']}:9559",
                spec["roles"]["graphd"]["command"],
            )

    def test_repeat_isolation_requires_unique_store_logs_network_names_ports_and_containers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, _password = self._fixture(
                root / "r1",
                run_id="campaign",
                repeat_index=1,
                identity_token="campaign-r01",
                graph_port=21001,
            )
            second, _password = self._fixture(
                root / "r2",
                run_id="campaign",
                repeat_index=2,
                identity_token="campaign-r02",
                graph_port=21002,
            )
            second["client"] = copy.deepcopy(first["client"])
            proof = cluster.validate_repeat_isolation([second, first])
            self.assertEqual([1, 2], proof["repeat_indices"])
            self.assertEqual(2, len(set(proof["store_roots"])))
            self.assertEqual(2, len(set(proof["logs_roots"])))
            self.assertEqual(2, len(set(proof["networks"])))
            self.assertEqual(2, len(set(proof["graph_ports"])))
            self.assertEqual(6, len(set(proof["container_names"])))

            def duplicate_port(value: dict[str, object]) -> None:
                value["endpoint"]["port"] = first["endpoint"]["port"]
                value["roles"]["graphd"]["port_bindings"] = copy.deepcopy(
                    first["roles"]["graphd"]["port_bindings"]
                )

            def duplicate_network(value: dict[str, object]) -> None:
                value["network"]["name"] = first["network"]["name"]

            def duplicate_container(value: dict[str, object]) -> None:
                value["roles"]["graphd"]["container_name"] = first["roles"]["graphd"]["container_name"]

            def duplicate_store(value: dict[str, object]) -> None:
                for role in cluster.ROLE_ORDER:
                    value["roles"][role]["mounts"][0]["source"] = first["roles"][role]["mounts"][0][
                        "source"
                    ]

            def duplicate_logs(value: dict[str, object]) -> None:
                value["logs_root"] = first["logs_root"]
                for role in cluster.ROLE_ORDER:
                    value["roles"][role]["mounts"][1]["source"] = str(
                        Path(first["logs_root"]) / role
                    )

            for name, mutate in {
                "host port": duplicate_port,
                "network": duplicate_network,
                "container": duplicate_container,
                "store root": duplicate_store,
                "logs root": duplicate_logs,
            }.items():
                with self.subTest(name=name):
                    drifted = copy.deepcopy(second)
                    mutate(drifted)
                    with self.assertRaises(ContractError):
                        cluster.validate_repeat_isolation([first, drifted])

    def test_docker_argv_uses_repo_digest_exact_aliases_mounts_and_graph_only_port(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spec, _password = self._fixture(Path(directory))
            preflight_sha = "f" * 64
            for role in cluster.ROLE_ORDER:
                argv = cluster.docker_run_argv(spec, role, preflight_sha)
                item = spec["roles"][role]
                self.assertEqual(item["image"]["repo_digest"], argv[-len(item["command"]) - 1])
                self.assertIn(item["container_name"], argv)
                self.assertIn(item["logical_host"], argv)
                self.assertIn("--pull=never", argv)
                self.assertIn("--restart=no", argv)
                self.assertNotIn(item["image"]["tag"], argv)
                self.assertFalse(any(value.endswith(",rw") for value in argv))
                self.assertEqual(role == "graphd", "--publish" in argv)

    def test_container_inspect_accepts_exact_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spec, _password = self._fixture(Path(directory))
            for role in cluster.ROLE_ORDER:
                snapshot = cluster.validate_container_inspect(
                    spec, role, self._inspect(spec, role, "f" * 64), preflight_sha256="f" * 64
                )
                self.assertEqual(spec["roles"][role]["container_name"], snapshot["name"])
                self.assertTrue(snapshot["running"])

    def test_container_inspect_rejects_every_launch_or_lifecycle_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spec, _password = self._fixture(Path(directory))
            base = self._inspect(spec, "graphd", "f" * 64)
            mutations = {
                "tag instead of RepoDigest": lambda value: value["Config"].__setitem__(
                    "Image", spec["roles"]["graphd"]["image"]["tag"]
                ),
                "extra mount": lambda value: value["Mounts"].append(
                    {"Type": "bind", "Source": "/tmp/extra", "Destination": "/extra", "RW": True}
                ),
                "port drift": lambda value: value["HostConfig"].__setitem__("PortBindings", {}),
                "command drift": lambda value: value["Config"].__setitem__("Cmd", ["--help"]),
                "restart": lambda value: value.__setitem__("RestartCount", 1),
                "invalid StartedAt": lambda value: value["State"].__setitem__("StartedAt", "not-a-time"),
                "missing alias": lambda value: value["NetworkSettings"]["Networks"][
                    spec["network"]["name"]
                ].__setitem__("Aliases", ["wrong"]),
                "extra network": lambda value: value["NetworkSettings"]["Networks"].__setitem__(
                    "other", {"Aliases": [], "IPAddress": "172.1.1.1"}
                ),
            }
            for name, mutate in mutations.items():
                with self.subTest(name=name):
                    value = copy.deepcopy(base)
                    mutate(value)
                    with self.assertRaises(ContractError):
                        cluster.validate_container_inspect(
                            spec, "graphd", value, preflight_sha256="f" * 64
                        )

    def test_live_gate_accepts_exact_hosts_partitions_space_and_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spec, _password = self._fixture(Path(directory))
            snapshot = cluster.validate_live_results(spec, self._live_results(spec))
            self.assertEqual(list(range(1, 65)), snapshot["partition_ids"])
            self.assertEqual(spec["edge_labels"], snapshot["edge_labels"])
            self.assertEqual(0, snapshot["tag_count"])

    def test_live_gate_rejects_topology_partition_space_or_schema_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spec, _password = self._fixture(Path(directory))
            base = self._live_results(spec)

            def offline(value: dict[str, object]) -> None:
                value["SHOW HOSTS META"][0]["Status"] = "OFFLINE"

            def missing_partition(value: dict[str, object]) -> None:
                value["SHOW PARTS"].pop()

            def lost_peer(value: dict[str, object]) -> None:
                value["SHOW PARTS"][0]["Losts"] = "[lost:9779]"

            def leader_substring_confusion(value: dict[str, object]) -> None:
                storage = spec["roles"]["storaged"]["logical_host"]
                value["SHOW PARTS"][0]["Leader"] = f"evil-{storage}:9779"

            def extra_peer(value: dict[str, object]) -> None:
                storage = spec["roles"]["storaged"]["logical_host"]
                value["SHOW PARTS"][0]["Peers"] = (
                    f"[{storage}:9779,unexpected-storage:9779]"
                )

            def duplicate_peer(value: dict[str, object]) -> None:
                storage = spec["roles"]["storaged"]["logical_host"]
                value["SHOW PARTS"][0]["Peers"] = [
                    f"{storage}:9779", f"{storage}:9779"
                ]

            def replica_drift(value: dict[str, object]) -> None:
                value[f"DESCRIBE SPACE {spec['space']}"][0]["Replica Factor"] = 2

            def vid_drift(value: dict[str, object]) -> None:
                value[f"DESCRIBE SPACE {spec['space']}"][0]["Vid Type"] = "FIXED_STRING(32)"

            def extra_edge(value: dict[str, object]) -> None:
                value["SHOW EDGES"].append({"Name": "EXTRA"})

            def hidden_index(value: dict[str, object]) -> None:
                value["SHOW EDGE INDEXES"] = [{"Name": "idx"}]

            for name, mutate in {
                "offline host": offline,
                "63 partitions": missing_partition,
                "lost peer": lost_peer,
                "leader substring confusion": leader_substring_confusion,
                "extra peer": extra_peer,
                "duplicate peer": duplicate_peer,
                "replica factor": replica_drift,
                "VID type": vid_drift,
                "extra edge": extra_edge,
                "hidden index": hidden_index,
            }.items():
                with self.subTest(name=name):
                    value = copy.deepcopy(base)
                    mutate(value)
                    with self.assertRaises(ContractError):
                        cluster.validate_live_results(spec, value)

    def test_launch_validator_rehashes_client_and_rejects_command_or_timeout_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spec, _password = self._fixture(Path(directory))
            client_file = Path(spec["client"]["path"]) / "nebula3.py"
            client_file.write_text("mutated\n", encoding="utf-8")
            with self.assertRaises(ContractError):
                cluster.validate_launch_spec(spec)

        with tempfile.TemporaryDirectory() as directory:
            spec, _password = self._fixture(Path(directory))
            spec["query_timeout_ms"] = 10001
            with self.assertRaises(ContractError):
                cluster.validate_launch_spec(spec)

        with tempfile.TemporaryDirectory() as directory:
            spec, _password = self._fixture(Path(directory))
            spec["roles"]["storaged"]["command"] = ["--help"]
            with self.assertRaises(ContractError):
                cluster.validate_launch_spec(spec)

    def test_canonical_json_sha_is_independent_of_object_member_order(self) -> None:
        self.assertEqual(
            contract.canonical_json_sha({"b": 2, "a": {"d": 4, "c": 3}}),
            contract.canonical_json_sha({"a": {"c": 3, "d": 4}, "b": 2}),
        )

    def test_capture_live_gate_uses_bounded_client_timeout_without_real_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spec, password = self._fixture(Path(directory))
            results = self._live_results(spec)

            class FakeResult:
                def __init__(self, rows: object = None, ok: bool = True) -> None:
                    self.rows = [] if rows is None else rows
                    self.ok = ok

                def is_succeeded(self) -> bool:
                    return self.ok

                def as_primitive(self) -> object:
                    return self.rows

                def error_msg(self) -> str:
                    return "fake error"

            class FakeConfig:
                instances: list[object] = []

                def __init__(self) -> None:
                    self.max_connection_pool_size = 0
                    self.timeout = 0
                    self.instances.append(self)

            class FakeSession:
                released = False

                def execute(self, statement: str) -> FakeResult:
                    if statement.startswith("USE "):
                        return FakeResult()
                    return FakeResult(results[statement])

                def release(self) -> None:
                    self.released = True

            class FakePool:
                instances: list[object] = []

                def __init__(self) -> None:
                    self.config = None
                    self.session = FakeSession()
                    self.closed = False
                    self.instances.append(self)

                def init(self, endpoints: object, config: object) -> bool:
                    self.endpoints = endpoints
                    self.config = config
                    return True

                def get_session(self, user: str, supplied_password: str) -> FakeSession:
                    self.credentials = (user, supplied_password)
                    return self.session

                def close(self) -> None:
                    self.closed = True

            nebula_module = types.ModuleType("nebula3")
            config_module = types.ModuleType("nebula3.Config")
            config_module.Config = FakeConfig
            gclient_module = types.ModuleType("nebula3.gclient")
            net_module = types.ModuleType("nebula3.gclient.net")
            net_module.ConnectionPool = FakePool
            modules = {
                "nebula3": nebula_module,
                "nebula3.Config": config_module,
                "nebula3.gclient": gclient_module,
                "nebula3.gclient.net": net_module,
            }
            with mock.patch.object(adapter, "import_client", return_value="3.8.3"), mock.patch.dict(
                sys.modules, modules
            ):
                snapshot = cluster.capture_live_gate(
                    spec, password=password, startup_timeout_seconds=30
                )
            self.assertEqual("PASS", snapshot["state"])
            self.assertGreaterEqual(FakeConfig.instances[0].timeout, 1)
            self.assertLessEqual(FakeConfig.instances[0].timeout, spec["query_timeout_ms"])
            self.assertTrue(FakePool.instances[0].closed)
            self.assertTrue(FakePool.instances[0].session.released)

    def test_capture_live_gate_rejects_exhausted_overall_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spec, password = self._fixture(Path(directory))
            with self.assertRaisesRegex(ContractError, "deadline was exhausted"):
                cluster.capture_live_gate(
                    spec,
                    password=password,
                    startup_timeout_seconds=30,
                    overall_deadline=time.monotonic() - 0.01,
                )

    def test_capture_live_gate_hard_deadline_interrupts_a_blocked_query(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spec, password = self._fixture(Path(directory))

            class FakeConfig:
                pass

            class SlowSession:
                def execute(self, _statement: str) -> object:
                    time.sleep(5)
                    raise AssertionError("hard deadline failed to interrupt the query")

                def release(self) -> None:
                    pass

            class FakePool:
                def init(self, _endpoints: object, _config: object) -> bool:
                    return True

                def get_session(self, _user: str, _password: str) -> SlowSession:
                    return SlowSession()

                def close(self) -> None:
                    pass

            nebula_module = types.ModuleType("nebula3")
            config_module = types.ModuleType("nebula3.Config")
            config_module.Config = FakeConfig
            gclient_module = types.ModuleType("nebula3.gclient")
            net_module = types.ModuleType("nebula3.gclient.net")
            net_module.ConnectionPool = FakePool
            modules = {
                "nebula3": nebula_module,
                "nebula3.Config": config_module,
                "nebula3.gclient": gclient_module,
                "nebula3.gclient.net": net_module,
            }
            started = time.monotonic()
            with mock.patch.object(adapter, "import_client", return_value="3.8.3"), mock.patch.dict(
                sys.modules, modules
            ), self.assertRaisesRegex(ContractError, "hard overall deadline"):
                cluster.capture_live_gate(spec, password=password, startup_timeout_seconds=1)
            self.assertLess(time.monotonic() - started, 2.0)

    def _preflight_receipt(
        self, root: Path, spec: dict[str, object]
    ) -> tuple[Path, dict[str, object], dict[str, object]]:
        repo_root = root / "repo"
        repo_root.mkdir()
        repo = self._repo(repo_root)
        references = {}
        for name in ("request", "runtime_manifest", "store_manifest"):
            path = root / f"{name}.json"
            path.write_text(name + "\n", encoding="utf-8")
            references[name] = contract.file_ref(path)
        docker = root / "docker"
        docker.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        docker.chmod(0o755)
        p02b = {}
        for name in (
            "result", "validator", "binary", "pass_marker", "provenance", "canonical_dataset_manifest"
        ):
            path = root / f"p02b-{name}.json"
            path.write_text(name + "\n", encoding="utf-8")
            if name in ("validator", "binary"):
                path.chmod(0o755)
            p02b[name] = contract.file_ref(path)
        p02b.update(
            {
                "validator_argv_sha256": "d" * 64,
                "current_host": {"hostname": "formal-host", "fingerprint_sha256": "e" * 64},
                "max_age_seconds": 21600,
            }
        )
        value = {
            "schema_version": cluster.PREFLIGHT_SCHEMA,
            "state": "PASS",
            "created_at_utc": cluster.utc_now(),
            "repo": repo,
            "inputs": references,
            "docker": contract.file_ref(docker),
            "spec": spec,
            "spec_sha256": contract.canonical_json_sha(spec),
            "p02b": p02b,
            "preconditions": {
                "store_and_logs_offline": True,
                "owned_names_absent": True,
                "graph_port_available": True,
                "role_log_paths_absent": True,
            },
        }
        path = root / "preflight.json"
        write_json(path, value)
        return path, value, repo

    def test_preflight_receipt_revalidates_sha_repo_host_and_spec(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec, _password = self._fixture(root)
            path, value, repo = self._preflight_receipt(root, spec)
            host = {"hostname": "formal-host", "fingerprint_sha256": "e" * 64}
            with mock.patch.object(contract, "current_git_state", return_value=repo), mock.patch.object(
                adapter, "p31_host_facts", return_value=host
            ):
                _receipt_path, loaded, docker = cluster.validate_preflight_receipt(
                    path, sha256_file(path), max_age_seconds=600
                )
            self.assertEqual(value["spec_sha256"], loaded["spec_sha256"])
            self.assertEqual(Path(value["docker"]["path"]), docker)

            (Path(spec["client"]["path"]) / "nebula3.py").write_text("drift\n", encoding="utf-8")
            with mock.patch.object(contract, "current_git_state", return_value=repo), mock.patch.object(
                adapter, "p31_host_facts", return_value=host
            ), self.assertRaises(ContractError):
                cluster.validate_preflight_receipt(path, sha256_file(path), max_age_seconds=600)

    def _start_value(
        self,
        spec: dict[str, object],
        repo: dict[str, object],
        preflight_path: Path,
    ) -> dict[str, object]:
        preflight_sha = sha256_file(preflight_path)
        return {
            "schema_version": cluster.START_SCHEMA,
            "state": "PASS",
            "created_at_utc": "2026-07-22T00:01:00Z",
            "repo": repo,
            "preflight": {"path": str(preflight_path.resolve()), "sha256": preflight_sha},
            "spec_sha256": contract.canonical_json_sha(spec),
            "network": {"name": spec["network"]["name"], "network_id": "9" * 64, "driver": "bridge"},
            "containers": self._snapshots(spec, preflight_sha),
            "live_gate": self._live_snapshot(spec),
            "lifecycle_stable": True,
            "timing_scope": "pre-adapter-outside-timed-region-v1",
        }

    def _partial_value(
        self,
        spec: dict[str, object],
        repo: dict[str, object],
        preflight_path: Path,
        *,
        network: dict[str, object],
        containers: dict[str, dict[str, object]],
    ) -> dict[str, object]:
        return {
            "schema_version": cluster.PARTIAL_START_SCHEMA,
            "state": "FAILED",
            "created_at_utc": cluster.utc_now(),
            "repo": repo,
            "preflight": {
                "path": str(preflight_path.resolve()),
                "sha256": sha256_file(preflight_path),
            },
            "spec_sha256": contract.canonical_json_sha(spec),
            "failed_stage": "mock-live-gate",
            "failure": {"error_type": "ContractError", "message": "mock start failure"},
            "resources": {"network": network, "containers": containers},
            "timing_scope": "pre-adapter-outside-timed-region-v1",
        }

    def test_start_receipt_validator_binds_exact_live_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec, _password = self._fixture(root)
            repo_root = root / "repo"
            repo_root.mkdir()
            repo = self._repo(repo_root)
            preflight_path = root / "preflight-ref.json"
            preflight_path.write_text("{}\n", encoding="utf-8")
            preflight_value = {"spec": spec, "spec_sha256": contract.canonical_json_sha(spec)}
            value = self._start_value(spec, repo, preflight_path)
            path = root / "start.json"
            write_json(path, value)
            with mock.patch.object(contract, "current_git_state", return_value=repo):
                cluster.validate_start_receipt(
                    path,
                    sha256_file(path),
                    preflight_path=preflight_path,
                    preflight_sha256=sha256_file(preflight_path),
                    preflight_value=preflight_value,
                )

            value["live_gate"]["hosts"]["metad"]["status"] = "OFFLINE"
            bad = root / "bad-start.json"
            write_json(bad, value)
            with mock.patch.object(contract, "current_git_state", return_value=repo), self.assertRaises(
                ContractError
            ):
                cluster.validate_start_receipt(
                    bad,
                    sha256_file(bad),
                    preflight_path=preflight_path,
                    preflight_sha256=sha256_file(preflight_path),
                    preflight_value=preflight_value,
                )

    def test_start_uses_only_bound_names_ids_and_writes_receipt_with_mocked_docker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec, password = self._fixture(root)
            repo_root = root / "repo"
            repo_root.mkdir()
            repo = self._repo(repo_root)
            preflight_path = root / "preflight-ref.json"
            preflight_path.write_text("{}\n", encoding="utf-8")
            preflight_sha = sha256_file(preflight_path)
            preflight_value = {"repo": repo, "spec": spec, "spec_sha256": contract.canonical_json_sha(spec)}
            ids = {"metad": "1" * 64, "storaged": "2" * 64, "graphd": "3" * 64}
            calls: list[list[str]] = []

            def fake_docker_call(_docker: Path, argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
                calls.append(list(argv))
                if argv[:2] == ["network", "create"]:
                    stdout = "9" * 64 + "\n"
                elif argv[0] == "run":
                    name = argv[argv.index("--name") + 1]
                    role = next(role for role in cluster.ROLE_ORDER if spec["roles"][role]["container_name"] == name)
                    stdout = ids[role] + "\n"
                else:
                    raise AssertionError(f"unexpected Docker call: {argv}")
                return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

            def fake_inspect(
                _docker: Path, name_or_id: str, **_kwargs: object
            ) -> dict[str, object]:
                role = next(
                    role
                    for role in cluster.ROLE_ORDER
                    if name_or_id in (spec["roles"][role]["container_name"], ids[role])
                )
                return self._inspect(spec, role, preflight_sha, ids[role])

            output = root / "artifacts" / "start.json"
            output.parent.mkdir()
            args = argparse.Namespace(
                preflight=preflight_path,
                preflight_sha256=preflight_sha,
                max_preflight_age_seconds=600,
                startup_timeout_seconds=30,
                partial_output=output.parent / "partial.json",
                cleanup_output=output.parent / "cleanup.json",
                cleanup_stop_timeout_seconds=7,
                output=output,
            )
            live = self._live_snapshot(spec)
            with mock.patch.object(
                cluster, "validate_preflight_receipt", return_value=(preflight_path, preflight_value, Path("/docker"))
            ), mock.patch.object(contract, "assert_offline"), mock.patch.object(
                cluster, "assert_owned_names_absent"
            ), mock.patch.object(cluster, "assert_port_available"), mock.patch.object(
                cluster, "docker_call", side_effect=fake_docker_call
            ), mock.patch.object(cluster, "_wait_running"), mock.patch.object(
                cluster, "docker_inspect_one", side_effect=fake_inspect
            ), mock.patch.object(
                cluster,
                "_network_inspect_one",
                return_value=self._network(spec, preflight_sha, ids),
            ), mock.patch.object(cluster, "capture_live_gate", return_value=live), mock.patch.dict(
                os.environ, {"CIDR_NEBULA_PASSWORD": password}, clear=False
            ):
                cluster.start(args)
            receipt = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(ids, {role: receipt["containers"][role]["container_id"] for role in ids})
            self.assertEqual(1, sum(argv[:2] == ["network", "create"] for argv in calls))
            self.assertEqual(3, sum(argv[0] == "run" for argv in calls))
            self.assertFalse(args.partial_output.exists())
            self.assertFalse(args.cleanup_output.exists())
            for role in cluster.ROLE_ORDER:
                self.assertTrue((Path(spec["logs_root"]) / role).is_dir())

    def test_partial_start_with_known_ids_is_cleaned_in_reverse_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec, password = self._fixture(root)
            repo_root = root / "repo"
            repo_root.mkdir()
            repo = self._repo(repo_root)
            preflight_path = root / "preflight-ref.json"
            preflight_path.write_text("{}\n", encoding="utf-8")
            preflight_sha = sha256_file(preflight_path)
            preflight_value = {"repo": repo, "spec": spec, "spec_sha256": contract.canonical_json_sha(spec)}
            ids = {"metad": "1" * 64, "storaged": "2" * 64, "graphd": "3" * 64}
            present_containers = set(ids.values())
            network_present = {"value": True}
            calls: list[list[str]] = []

            def fake_docker_call(_docker: Path, argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
                calls.append(list(argv))
                if argv[:2] == ["network", "create"]:
                    stdout = "9" * 64 + "\n"
                elif argv[0] == "run":
                    name = argv[argv.index("--name") + 1]
                    role = next(role for role in cluster.ROLE_ORDER if spec["roles"][role]["container_name"] == name)
                    stdout = ids[role] + "\n"
                elif argv[:2] == ["container", "stop"]:
                    self.assertIn(argv[-1], present_containers)
                    stdout = argv[-1] + "\n"
                elif argv[:2] == ["container", "rm"]:
                    present_containers.remove(argv[-1])
                    stdout = argv[-1] + "\n"
                elif argv[:2] == ["network", "rm"]:
                    self.assertEqual("9" * 64, argv[-1])
                    network_present["value"] = False
                    stdout = argv[-1] + "\n"
                else:
                    raise AssertionError(f"unexpected Docker call: {argv}")
                return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

            def fake_inspect(
                _docker: Path, name_or_id: str, **_kwargs: object
            ) -> dict[str, object]:
                role = next(
                    role
                    for role in cluster.ROLE_ORDER
                    if name_or_id in (spec["roles"][role]["container_name"], ids[role])
                )
                return self._inspect(spec, role, preflight_sha, ids[role])

            def optional(_docker: Path, kind: str, identifier: str) -> dict[str, object] | None:
                if kind == "container":
                    if identifier not in present_containers:
                        return None
                    role = next(role for role in cluster.ROLE_ORDER if ids[role] == identifier)
                    return self._inspect(spec, role, preflight_sha, identifier)
                if not network_present["value"]:
                    return None
                return self._network(
                    spec,
                    preflight_sha,
                    {role: identifier for role, identifier in ids.items() if identifier in present_containers},
                )

            artifacts = root / "artifacts"
            artifacts.mkdir()
            args = argparse.Namespace(
                preflight=preflight_path,
                preflight_sha256=preflight_sha,
                max_preflight_age_seconds=600,
                startup_timeout_seconds=30,
                partial_output=artifacts / "partial.json",
                cleanup_output=artifacts / "cleanup.json",
                cleanup_stop_timeout_seconds=7,
                output=artifacts / "start.json",
            )
            with mock.patch.object(
                cluster, "validate_preflight_receipt", return_value=(preflight_path, preflight_value, Path("/docker"))
            ), mock.patch.object(contract, "assert_offline"), mock.patch.object(
                cluster, "assert_owned_names_absent"
            ), mock.patch.object(cluster, "assert_port_available"), mock.patch.object(
                cluster, "docker_call", side_effect=fake_docker_call
            ), mock.patch.object(cluster, "_wait_running"), mock.patch.object(
                cluster, "docker_inspect_one", side_effect=fake_inspect
            ), mock.patch.object(
                cluster,
                "_network_inspect_one",
                return_value=self._network(spec, preflight_sha, ids),
            ), mock.patch.object(
                cluster, "capture_live_gate", side_effect=RuntimeError("mock live gate failure")
            ), mock.patch.object(
                cluster, "_inspect_optional_exact_id", side_effect=optional
            ), mock.patch.object(contract, "current_git_state", return_value=repo), mock.patch.dict(
                os.environ, {"CIDR_NEBULA_PASSWORD": password}, clear=False
            ), self.assertRaisesRegex(RuntimeError, "mock live gate failure"):
                cluster.start(args)
            self.assertFalse(args.output.exists())
            partial = json.loads(args.partial_output.read_text(encoding="utf-8"))
            cleanup = json.loads(args.cleanup_output.read_text(encoding="utf-8"))
            self.assertEqual("FAILED", partial["state"])
            self.assertTrue(all(item["state"] == "KNOWN" for item in partial["resources"]["containers"].values()))
            self.assertEqual("PASS", cleanup["state"])
            self.assertTrue(cleanup["verification"]["all_present_identities_revalidated"])
            self.assertEqual(set(), present_containers)
            self.assertFalse(network_present["value"])
            cleanup_commands = cleanup["commands"]
            expected = [
                ["container", "stop", "--time", "7", ids[role]] for role in cluster.STOP_ORDER
            ]
            expected.extend([["container", "rm", ids[role]] for role in cluster.STOP_ORDER])
            expected.append(["network", "rm", "9" * 64])
            self.assertEqual(expected, cleanup_commands)
            forged = copy.deepcopy(cleanup)
            forged["commands"][0][-1] = spec["roles"]["graphd"]["container_name"]
            forged_path = artifacts / "forged-cleanup.json"
            write_json(forged_path, forged)
            partial_value = json.loads(args.partial_output.read_text(encoding="utf-8"))
            with mock.patch.object(contract, "current_git_state", return_value=repo), self.assertRaises(
                ContractError
            ):
                cluster.validate_cleanup_receipt(
                    forged_path,
                    sha256_file(forged_path),
                    preflight_path=preflight_path,
                    preflight_sha256=preflight_sha,
                    partial_path=args.partial_output,
                    partial_sha256=sha256_file(args.partial_output),
                    preflight_value=preflight_value,
                    partial_value=partial_value,
                )

    def test_uncertain_partial_identity_is_blocked_without_inspect_or_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec, _password = self._fixture(root)
            repo_root = root / "repo"
            repo_root.mkdir()
            repo = self._repo(repo_root)
            preflight_path = root / "preflight-ref.json"
            preflight_path.write_text("{}\n", encoding="utf-8")
            preflight_sha = sha256_file(preflight_path)
            preflight_value = {"repo": repo, "spec": spec, "spec_sha256": contract.canonical_json_sha(spec)}
            partial = self._partial_value(
                spec,
                repo,
                preflight_path,
                network={"state": "KNOWN", "id": "9" * 64},
                containers={
                    "metad": {"state": "KNOWN", "id": "1" * 64},
                    "storaged": {"state": "UNCERTAIN", "id": None},
                    "graphd": {"state": "NOT_ATTEMPTED", "id": None},
                },
            )
            partial_path = root / "partial.json"
            write_json(partial_path, partial)
            output = root / "artifacts" / "cleanup.json"
            output.parent.mkdir()
            args = argparse.Namespace(
                preflight=preflight_path,
                preflight_sha256=preflight_sha,
                partial_start=partial_path,
                partial_start_sha256=sha256_file(partial_path),
                max_preflight_age_seconds=604800,
                stop_timeout_seconds=7,
                output=output,
            )
            with mock.patch.object(
                cluster, "validate_preflight_receipt", return_value=(preflight_path, preflight_value, Path("/docker"))
            ), mock.patch.object(contract, "current_git_state", return_value=repo), mock.patch.object(
                cluster, "_inspect_optional_exact_id"
            ) as inspect_exact, mock.patch.object(cluster, "docker_call") as docker, self.assertRaisesRegex(
                ContractError, "BLOCKED"
            ):
                cluster.cleanup(args)
            inspect_exact.assert_not_called()
            docker.assert_not_called()
            receipt = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("BLOCKED", receipt["state"])
            self.assertIn("storaged", receipt["blocked_reason"])
            self.assertFalse(receipt["verification"]["name_lookup_used"])
            self.assertEqual([], receipt["commands"])

    def test_name_reuse_cannot_be_deleted_when_old_exact_ids_are_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec, _password = self._fixture(root)
            repo_root = root / "repo"
            repo_root.mkdir()
            repo = self._repo(repo_root)
            preflight_path = root / "preflight-ref.json"
            preflight_path.write_text("{}\n", encoding="utf-8")
            preflight_value = {"repo": repo, "spec": spec, "spec_sha256": contract.canonical_json_sha(spec)}
            ids = {"metad": "1" * 64, "storaged": "2" * 64, "graphd": "3" * 64}
            partial = self._partial_value(
                spec,
                repo,
                preflight_path,
                network={"state": "KNOWN", "id": "9" * 64},
                containers={role: {"state": "KNOWN", "id": ids[role]} for role in cluster.ROLE_ORDER},
            )
            partial_path = root / "partial.json"
            write_json(partial_path, partial)
            output = root / "artifacts" / "cleanup.json"
            output.parent.mkdir()
            queried: list[tuple[str, str]] = []

            def absent_by_id(_docker: Path, kind: str, identifier: str) -> None:
                queried.append((kind, identifier))
                return None

            with mock.patch.object(
                cluster, "_inspect_optional_exact_id", side_effect=absent_by_id
            ), mock.patch.object(cluster, "docker_call") as docker:
                receipt = cluster.perform_partial_cleanup(
                    docker=Path("/docker"),
                    preflight_path=preflight_path,
                    preflight_value=preflight_value,
                    partial_path=partial_path,
                    partial_value=partial,
                    output=output,
                    stop_timeout_seconds=7,
                )
            self.assertEqual("PASS", receipt["state"])
            docker.assert_not_called()
            expected_ids = set(ids.values()) | {"9" * 64}
            self.assertTrue(all(identifier in expected_ids for _kind, identifier in queried))
            forbidden_names = {spec["roles"][role]["container_name"] for role in cluster.ROLE_ORDER}
            self.assertTrue(forbidden_names.isdisjoint(identifier for _kind, identifier in queried))
            self.assertEqual([], receipt["commands"])

    def test_start_bound_recovery_uses_only_exact_ids_and_preserves_start_ticks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec, _password = self._fixture(root)
            repo_root = root / "repo"
            repo_root.mkdir()
            repo = self._repo(repo_root)
            preflight_path = root / "preflight.json"
            start_path = root / "start.json"
            preflight_path.write_text("{}\n", encoding="utf-8")
            start_path.write_text("{}\n", encoding="utf-8")
            preflight_sha = sha256_file(preflight_path)
            preflight_value = {
                "repo": repo,
                "spec": spec,
                "spec_sha256": contract.canonical_json_sha(spec),
            }
            start_value = self._start_value(spec, repo, preflight_path)
            ids = {
                role: start_value["containers"][role]["container_id"]
                for role in cluster.ROLE_ORDER
            }
            observations: dict[tuple[str, str], int] = {}

            def inspect_exact(
                _docker: Path, kind: str, identifier: str
            ) -> dict[str, object] | None:
                key = (kind, identifier)
                observations[key] = observations.get(key, 0) + 1
                if observations[key] > 1:
                    return None
                if kind == "network":
                    return self._network(spec, preflight_sha, ids)
                role = next(role for role, value in ids.items() if value == identifier)
                return self._inspect(spec, role, preflight_sha, identifier)

            calls: list[list[str]] = []

            def docker_call(
                _docker: Path, command: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess:
                calls.append(list(command))
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            output = root / "artifacts" / "start-cleanup.json"
            output.parent.mkdir()
            with mock.patch.object(cluster, "_inspect_optional_exact_id", side_effect=inspect_exact), mock.patch.object(
                cluster, "docker_call", side_effect=docker_call
            ):
                receipt = cluster.perform_start_bound_cleanup(
                    docker=Path("/usr/bin/docker"),
                    preflight_path=preflight_path,
                    preflight_value=preflight_value,
                    start_path=start_path,
                    start_value=start_value,
                    output=output,
                    stop_timeout_seconds=7,
                )
            self.assertEqual("PASS", receipt["state"])
            forbidden_names = {
                spec["network"]["name"],
                *(spec["roles"][role]["container_name"] for role in cluster.ROLE_ORDER),
            }
            self.assertTrue(all(forbidden_names.isdisjoint(command) for command in calls))
            for role in cluster.ROLE_ORDER:
                self.assertEqual(
                    start_value["containers"][role]["process_start_ticks"],
                    receipt["verification"]["before_cleanup"][role]["process_start_ticks"],
                )

    def test_start_bound_recovery_blocks_identity_drift_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec, _password = self._fixture(root)
            repo_root = root / "repo"
            repo_root.mkdir()
            repo = self._repo(repo_root)
            preflight_path = root / "preflight.json"
            start_path = root / "start.json"
            preflight_path.write_text("{}\n", encoding="utf-8")
            start_path.write_text("{}\n", encoding="utf-8")
            preflight_value = {
                "repo": repo,
                "spec": spec,
                "spec_sha256": contract.canonical_json_sha(spec),
            }
            start_value = self._start_value(spec, repo, preflight_path)
            graph_id = start_value["containers"]["graphd"]["container_id"]
            drifted = self._inspect(spec, "graphd", sha256_file(preflight_path), graph_id)
            drifted["Name"] = "/reused-by-another-owner"
            output = root / "artifacts" / "start-cleanup-blocked.json"
            output.parent.mkdir()
            with mock.patch.object(
                cluster, "_inspect_optional_exact_id", return_value=drifted
            ), mock.patch.object(cluster, "docker_call") as docker:
                receipt = cluster.perform_start_bound_cleanup(
                    docker=Path("/usr/bin/docker"),
                    preflight_path=preflight_path,
                    preflight_value=preflight_value,
                    start_path=start_path,
                    start_value=start_value,
                    output=output,
                    stop_timeout_seconds=7,
                )
            self.assertEqual("BLOCKED", receipt["state"])
            self.assertIn("identity revalidation failed", receipt["blocked_reason"])
            docker.assert_not_called()

    def test_known_id_with_identity_drift_is_blocked_before_any_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec, _password = self._fixture(root)
            repo_root = root / "repo"
            repo_root.mkdir()
            repo = self._repo(repo_root)
            preflight_path = root / "preflight-ref.json"
            preflight_path.write_text("{}\n", encoding="utf-8")
            preflight_value = {"repo": repo, "spec": spec, "spec_sha256": contract.canonical_json_sha(spec)}
            partial = self._partial_value(
                spec,
                repo,
                preflight_path,
                network={"state": "KNOWN", "id": "9" * 64},
                containers={
                    "metad": {"state": "KNOWN", "id": "1" * 64},
                    "storaged": {"state": "NOT_ATTEMPTED", "id": None},
                    "graphd": {"state": "NOT_ATTEMPTED", "id": None},
                },
            )
            partial_path = root / "partial.json"
            write_json(partial_path, partial)
            output = root / "artifacts" / "cleanup.json"
            output.parent.mkdir()
            drifted = self._inspect(spec, "metad", sha256_file(preflight_path), "1" * 64)
            drifted["Config"]["Labels"]["cidr.p10.run_id"] = "another-run"

            def inspect_drift(_docker: Path, kind: str, identifier: str) -> dict[str, object] | None:
                self.assertEqual(("container", "1" * 64), (kind, identifier))
                return drifted

            with mock.patch.object(
                cluster, "_inspect_optional_exact_id", side_effect=inspect_drift
            ), mock.patch.object(cluster, "docker_call") as docker:
                receipt = cluster.perform_partial_cleanup(
                    docker=Path("/docker"),
                    preflight_path=preflight_path,
                    preflight_value=preflight_value,
                    partial_path=partial_path,
                    partial_value=partial,
                    output=output,
                    stop_timeout_seconds=7,
                )
            self.assertEqual("BLOCKED", receipt["state"])
            self.assertIn("identity revalidation failed", receipt["blocked_reason"])
            self.assertEqual([], receipt["commands"])
            docker.assert_not_called()

    def test_stop_uses_exact_bound_ids_preserves_logs_and_validates_post_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec, _password = self._fixture(root)
            for role in cluster.ROLE_ORDER:
                (Path(spec["logs_root"]) / role).mkdir()
            repo_root = root / "repo"
            repo_root.mkdir()
            repo = self._repo(repo_root)
            preflight_path = root / "preflight-ref.json"
            start_path = root / "start-ref.json"
            preflight_path.write_text("{}\n", encoding="utf-8")
            start_path.write_text("{}\n", encoding="utf-8")
            preflight_sha = sha256_file(preflight_path)
            preflight_value = {"repo": repo, "spec": spec, "spec_sha256": contract.canonical_json_sha(spec)}
            start_value = self._start_value(spec, repo, preflight_path)
            start_value["created_at_utc"] = "2000-01-01T00:00:00Z"
            ids = {role: start_value["containers"][role]["container_id"] for role in cluster.ROLE_ORDER}
            calls: list[list[str]] = []

            def fake_docker_call(_docker: Path, argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
                calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            def fake_inspect(
                _docker: Path, name_or_id: str, **_kwargs: object
            ) -> dict[str, object]:
                role = next(role for role in cluster.ROLE_ORDER if ids[role] == name_or_id)
                return self._inspect(spec, role, preflight_sha, ids[role])

            output = root / "artifacts" / "stop.json"
            output.parent.mkdir()
            args = argparse.Namespace(
                preflight=preflight_path,
                preflight_sha256=preflight_sha,
                start_receipt=start_path,
                start_receipt_sha256=sha256_file(start_path),
                stop_timeout_seconds=7,
                output=output,
            )
            with mock.patch.object(
                cluster, "validate_preflight_receipt", return_value=(preflight_path, preflight_value, Path("/docker"))
            ), mock.patch.object(
                cluster, "validate_start_receipt", return_value=(start_path, start_value)
            ), mock.patch.object(cluster, "docker_inspect_one", side_effect=fake_inspect), mock.patch.object(
                cluster,
                "_network_inspect_one",
                return_value=self._network(spec, preflight_sha, ids),
            ), mock.patch.object(cluster, "docker_call", side_effect=fake_docker_call), mock.patch.object(
                cluster, "assert_owned_names_absent"
            ) as absent:
                cluster.stop(args)
            expected = [
                ["container", "stop", "--time", "7", ids[role]] for role in cluster.STOP_ORDER
            ]
            expected.extend([["container", "rm", ids[role]] for role in cluster.STOP_ORDER])
            expected.append(["network", "rm", "9" * 64])
            self.assertEqual(expected, calls)
            absent.assert_called_once()
            with mock.patch.object(contract, "current_git_state", return_value=repo):
                cluster.validate_stop_receipt(
                    output,
                    sha256_file(output),
                    preflight_path=preflight_path,
                    preflight_sha256=preflight_sha,
                    start_path=start_path,
                    start_sha256=sha256_file(start_path),
                    preflight_value=preflight_value,
                    start_value=start_value,
                )


if __name__ == "__main__":
    unittest.main()
