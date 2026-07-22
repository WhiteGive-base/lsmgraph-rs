#!/usr/bin/env python3
"""Container-free tests for the Neo4j repeat launcher/stopper receipts."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

P10_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(P10_DIR))

from adapters import launch_neo4j_runtime as lifecycle  # noqa: E402
from adapters import freeze_neo4j_store as freezer  # noqa: E402
from adapters.neo4j_store_contract import (  # noqa: E402
    CANONICAL_SENTINELS,
    KNOWN_MUTABLE_PATTERNS,
    TREE_HASH_METHOD,
    stable_tree_manifest,
)
from p10_contract import ContractError, sha256_file  # noqa: E402

IMAGE_DIGEST = lifecycle.IMAGE_REPO_DIGEST
IMAGE_ID = "sha256:" + "2" * 64
CONTAINER_ID = "3" * 64
HOST = {"hostname": "formal-host", "fingerprint_sha256": "4" * 64}


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def artifact(path: Path) -> dict[str, object]:
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def offline_gate(root: Path) -> dict[str, object]:
    root_stat = root.stat()
    lock_path = root / "databases" / "store_lock"
    lock_stat = lock_path.stat()
    root_identity = {
        "path": str(root.resolve()),
        "device": root_stat.st_dev,
        "inode": root_stat.st_ino,
        "uid": root_stat.st_uid,
        "gid": root_stat.st_gid,
        "mode": "0700",
    }
    lock_identity = {
        "path": str(lock_path.resolve()),
        "device": lock_stat.st_dev,
        "inode": lock_stat.st_ino,
        "uid": lock_stat.st_uid,
        "gid": lock_stat.st_gid,
        "mode": "0600",
        "size_bytes": lock_stat.st_size,
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
        "proof_method": "owner-only-root-exclusive-store-lock-no-docker-mount-v1",
        "before_hash": copy.deepcopy(snapshot),
        "after_hash": copy.deepcopy(snapshot),
    }


class LifecycleFixture:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.source = self.root / "source-store"
        self.repeat = self.root / "repeat-01"
        self.store = self.repeat / "data"
        self.logs = self.repeat / "logs"
        for path in (self.source, self.store, self.logs):
            path.mkdir(parents=True)
        self.source.chmod(0o700)
        self.store.chmod(0o700)
        self.logs.chmod(0o700)
        for index, relative in enumerate(CANONICAL_SENTINELS):
            for tree in (self.source, self.store):
                path = tree / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"sentinel-{index}".encode())
        for tree_root in (self.source, self.store):
            lock = tree_root / "databases" / "store_lock"
            lock.write_bytes(b"")
            lock.chmod(0o600)
        self.tree = stable_tree_manifest(self.store)
        self.request = self.root / "request.json"
        write_json(self.request, {"schema_version": "p10-adapter-request-v1", "fixture": True})
        self.manifest = self.root / "store-manifest.json"
        self.immutable_sha = self.tree["immutable_store_sha256"]
        write_json(
            self.manifest,
            {
                "schema_version": "p10-neo4j-store-manifest-v3",
                "store_root": str(self.store),
                **{
                    key: self.tree[key]
                    for key in (
                        "store_sha256", "file_count", "total_bytes",
                        "immutable_store_sha256", "immutable_file_count", "immutable_total_bytes",
                    )
                },
            },
        )
        self.preflight = self.root / "store-preflight.json"
        write_json(
            self.preflight,
            {
                "schema_version": "p10-neo4j-runtime-store-audit-v1",
                "stage": "pre",
                "request": artifact(self.request),
                "store_manifest": artifact(self.manifest),
                "store_root": str(self.store),
                "hash_method": TREE_HASH_METHOD,
                "known_mutable_patterns": list(KNOWN_MUTABLE_PATTERNS),
                "cache_eviction": "posix-fadvise-dontneed-v1",
                "known_mutable_changes_tolerated": False,
                "offline_gate": offline_gate(self.store),
                "stop_receipt": None,
                "audit": {
                    "tree": {
                        **{
                            key: self.tree[key]
                            for key in (
                                "store_sha256", "file_count", "total_bytes",
                                "immutable_store_sha256", "immutable_file_count", "immutable_total_bytes",
                            )
                        },
                    },
                    "mutable_deltas": [],
                },
            },
        )
        self.output = self.repeat / "launch-receipt.json"
        self.stop_output = self.repeat / "stop-receipt.json"
        self.image = {
            "configured_ref": lifecycle.IMAGE_REF,
            "image_id": IMAGE_ID,
            "repo_digests": [f"neo4j@{IMAGE_DIGEST}"],
            "selected_repo_digest": IMAGE_DIGEST,
        }
        self.memory = {"heap_initial": "8G", "heap_max": "8G", "pagecache": "16G"}
        self.contract = lifecycle.runtime_contract(
            name="cidr-p10-neo4j-sf10-r01",
            repeat_index=1,
            clone_id="sf10-clone-r01",
            image=self.image,
            store_root=self.store,
            logs_root=self.logs,
            bolt_port=17687,
            log_driver="none",
            memory=self.memory,
        )

    def launch_args(self):
        return lifecycle.parse_args(
            [
                "launch",
                "--repeat-index", "1",
                "--clone-id", "sf10-clone-r01",
                "--repeat-root", str(self.repeat),
                "--source-store-root", str(self.source),
                "--store-root", str(self.store),
                "--logs-root", str(self.logs),
                "--store-manifest", str(self.manifest),
                "--store-manifest-sha256", sha256_file(self.manifest),
                "--store-preflight", str(self.preflight),
                "--store-preflight-sha256", sha256_file(self.preflight),
                "--container-name", self.contract["container_name"],
                "--bolt-port", str(self.contract["bolt_port"]),
                "--heap-initial", self.memory["heap_initial"],
                "--heap-max", self.memory["heap_max"],
                "--pagecache", self.memory["pagecache"],
                "--image-digest", IMAGE_DIGEST,
                "--log-driver", "none",
                "--output", str(self.output),
            ]
        )

    def inspect(self, phase: str, *, exit_code: int = 0) -> dict[str, object]:
        running = phase == "running"
        started = phase != "created"
        finished_at = "2026-07-22T01:10:00Z" if phase == "stopped" else "0001-01-01T00:00:00Z"
        started_at = "2026-07-22T01:00:00Z" if started else "0001-01-01T00:00:00Z"
        binding = {"7687/tcp": [{"HostIp": "127.0.0.1", "HostPort": "17687"}]}
        environment = ["PATH=/usr/local/bin"] + [
            f"{key}={value}" for key, value in sorted(self.contract["environment"].items())
        ]
        return {
            "Id": CONTAINER_ID,
            "Name": f"/{self.contract['container_name']}",
            "Image": IMAGE_ID,
            "RestartCount": 0,
            "State": {
                "Running": running,
                "Pid": 4242 if running else 0,
                "StartedAt": started_at,
                "FinishedAt": finished_at,
                "ExitCode": exit_code,
            },
            "Config": {
                "Image": lifecycle.IMAGE_REF,
                "User": self.contract["container_user"],
                "Entrypoint": ["tini", "-g", "--", "/startup/docker-entrypoint.sh"],
                "Cmd": ["neo4j"],
                "Env": environment,
                "Labels": dict(self.contract["labels"]),
            },
            "HostConfig": {
                "RestartPolicy": {"Name": "no"},
                "NetworkMode": "bridge",
                "Memory": 0,
                "MemorySwap": 0,
                "PortBindings": binding,
                "LogConfig": {"Type": "none", "Config": {}},
            },
            "Mounts": [
                {
                    "Type": "bind", "Source": str(self.store), "Destination": "/data",
                    "RW": True, "Mode": "", "Propagation": "rprivate",
                },
                {
                    "Type": "bind", "Source": str(self.logs), "Destination": "/logs",
                    "RW": True, "Mode": "", "Propagation": "rprivate",
                },
            ],
            "NetworkSettings": {"Ports": binding if running else {"7687/tcp": None}},
        }

    def run_launch(self) -> dict[str, object]:
        create = subprocess.CompletedProcess([], 0, stdout=CONTAINER_ID + "\n", stderr="")
        start = subprocess.CompletedProcess([], 0, stdout=CONTAINER_ID + "\n", stderr="")
        clone_proof = {
            "method": "full-content-tree-sha256-stable-no-hardlinks-v2",
            "file_count": self.tree["file_count"],
            "total_bytes": self.tree["total_bytes"],
            "shared_inode_count": 0,
            "source_tree": {
                key: self.tree[key] for key in ("store_sha256", "file_count", "total_bytes")
            },
            "runtime_tree": {
                key: self.tree[key] for key in ("store_sha256", "file_count", "total_bytes")
            },
            "source_offline_gate": offline_gate(self.source),
            "runtime_offline_gate": offline_gate(self.store),
        }
        with mock.patch.object(lifecycle, "image_identity", return_value=self.image), mock.patch.object(
            lifecycle, "ensure_container_absent"
        ), mock.patch.object(lifecycle, "ensure_port_available"), mock.patch.object(
            lifecycle, "docker_call", side_effect=[create, start]
        ), mock.patch.object(
            lifecycle, "docker_inspect_container", side_effect=[self.inspect("created"), self.inspect("running")]
        ) as inspected, mock.patch.object(
            lifecycle, "prove_independent_clone", return_value=clone_proof
        ), mock.patch.object(lifecycle, "current_host", return_value=HOST), mock.patch.object(
            lifecycle, "remove_failed_container"
        ) as cleanup:
            document = lifecycle.run_launch(self.launch_args())
        cleanup.assert_not_called()
        if [call.args[0] for call in inspected.call_args_list] != [CONTAINER_ID, CONTAINER_ID]:
            raise AssertionError("launch inspect calls were not exact-ID bound")
        return document


class Neo4jRuntimeLifecycleTests(unittest.TestCase):
    def test_launch_receipt_binds_exact_runtime_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-runtime-launch-") as raw:
            fixture = LifecycleFixture(Path(raw))
            document = fixture.run_launch()
            self.assertEqual(document["runtime_contract"]["bolt_host"], "127.0.0.1")
            self.assertEqual(document["runtime_contract"]["memory"], fixture.memory)
            self.assertEqual(document["runtime_contract"]["log_driver"], "none")
            self.assertEqual(document["runtime_contract"]["container_user"], f"{os.geteuid()}:{os.getegid()}")
            self.assertEqual(
                document["docker"]["running"]["config"]["container_user"],
                document["runtime_contract"]["container_user"],
            )
            self.assertIn("--user", document["docker"]["create"]["command"])
            self.assertEqual(document["runtime_contract"]["memory_limit_bytes"], 0)
            self.assertEqual(document["runtime_contract"]["memory_swap_limit_bytes"], 0)
            self.assertEqual(
                document["runtime_contract"]["expected_entrypoint"],
                lifecycle.EXPECTED_ENTRYPOINT,
            )
            self.assertEqual(
                document["runtime_contract"]["expected_command"],
                lifecycle.EXPECTED_COMMAND,
            )
            self.assertEqual(
                {mount["destination"] for mount in document["docker"]["running"]["config"]["mounts"]},
                {"/data", "/logs"},
            )
            self.assertEqual(document["docker"]["running"]["runtime"]["pid"], 4242)
            self.assertEqual(document["docker"]["running"]["runtime"]["restart_count"], 0)
            self.assertTrue(fixture.output.is_file())
            lifecycle.validate_launch_receipt(document, verify_artifacts=True, expected_host=HOST)
            wrong_receipt = copy.deepcopy(document)
            wrong_receipt["docker"]["running"]["config"]["entrypoint"] = ["/startup/docker-entrypoint.sh"]
            with self.assertRaisesRegex(ContractError, "entrypoint differs"):
                lifecycle.validate_launch_receipt(wrong_receipt, verify_artifacts=False, expected_host=HOST)
            extra_receipt = copy.deepcopy(document)
            extra_receipt["docker"]["created"]["config"]["command"].append("console")
            with self.assertRaisesRegex(ContractError, "command differs"):
                lifecycle.validate_launch_receipt(extra_receipt, verify_artifacts=False, expected_host=HOST)

    def test_container_rejects_nested_data_mount_and_directory_override(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-runtime-inspect-") as raw:
            fixture = LifecycleFixture(Path(raw))
            nested = fixture.inspect("running")
            nested["Mounts"].append(
                {
                    "Type": "bind", "Source": str(fixture.root), "Destination": "/data/databases",
                    "RW": True, "Mode": "", "Propagation": "rprivate",
                }
            )
            with self.assertRaisesRegex(ContractError, "exactly /data and /logs"):
                lifecycle.validate_container_inspect(nested, fixture.contract, expected_running=True)
            overridden = fixture.inspect("running")
            overridden["Config"]["Env"].append("NEO4J_server_directories_data=/elsewhere")
            with self.assertRaisesRegex(ContractError, "directory override"):
                lifecycle.validate_container_inspect(overridden, fixture.contract, expected_running=True)
            legacy_override = fixture.inspect("running")
            legacy_override["Config"]["Env"].append("NEO4J_dbms_directories_data=/elsewhere")
            with self.assertRaisesRegex(ContractError, "directory override"):
                lifecycle.validate_container_inspect(legacy_override, fixture.contract, expected_running=True)
            wrong_entrypoint = fixture.inspect("running")
            wrong_entrypoint["Config"]["Entrypoint"] = ["/startup/docker-entrypoint.sh"]
            with self.assertRaisesRegex(ContractError, "Config.Entrypoint"):
                lifecycle.validate_container_inspect(wrong_entrypoint, fixture.contract, expected_running=True)
            extra_entrypoint = fixture.inspect("running")
            extra_entrypoint["Config"]["Entrypoint"].append("--unexpected")
            with self.assertRaisesRegex(ContractError, "Config.Entrypoint"):
                lifecycle.validate_container_inspect(extra_entrypoint, fixture.contract, expected_running=True)
            wrong_command = fixture.inspect("running")
            wrong_command["Config"]["Cmd"] = ["neo4j-admin"]
            with self.assertRaisesRegex(ContractError, "Config.Cmd"):
                lifecycle.validate_container_inspect(wrong_command, fixture.contract, expected_running=True)
            extra_command = fixture.inspect("running")
            extra_command["Config"]["Cmd"].append("console")
            with self.assertRaisesRegex(ContractError, "Config.Cmd"):
                lifecycle.validate_container_inspect(extra_command, fixture.contract, expected_running=True)

    def test_container_rejects_wrong_log_driver_and_nonlocal_port(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-runtime-config-") as raw:
            fixture = LifecycleFixture(Path(raw))
            wrong_log = fixture.inspect("running")
            wrong_log["HostConfig"]["LogConfig"] = {"Type": "local", "Config": {}}
            with self.assertRaisesRegex(ContractError, "log driver"):
                lifecycle.validate_container_inspect(wrong_log, fixture.contract, expected_running=True)
            public = fixture.inspect("running")
            public["HostConfig"]["PortBindings"]["7687/tcp"][0]["HostIp"] = "0.0.0.0"
            with self.assertRaisesRegex(ContractError, "Bolt binding"):
                lifecycle.validate_container_inspect(public, fixture.contract, expected_running=True)
            limited = fixture.inspect("running")
            limited["HostConfig"]["Memory"] = 8 * 1024**3
            with self.assertRaisesRegex(ContractError, "Memory must"):
                lifecycle.validate_container_inspect(limited, fixture.contract, expected_running=True)
            wrong_user = fixture.inspect("running")
            wrong_user["Config"]["User"] = "7474:7474"
            with self.assertRaisesRegex(ContractError, "container user"):
                lifecycle.validate_container_inspect(wrong_user, fixture.contract, expected_running=True)

    def test_launch_rejects_hardlinked_clone_and_wrong_frozen_digest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-runtime-clone-") as raw:
            fixture = LifecycleFixture(Path(raw))
            relative = CANONICAL_SENTINELS[0]
            runtime_file = fixture.store / relative
            runtime_file.unlink()
            os.link(fixture.source / relative, runtime_file)
            with mock.patch.object(
                freezer,
                "_docker_mount_audit",
                return_value={
                    "running_container_count": 0,
                    "inspected_container_ids": [],
                    "overlapping_mounts": [],
                },
            ):
                with self.assertRaisesRegex(ContractError, "shares file inodes"):
                    lifecycle.prepare_clone_identity(fixture.launch_args())

        with tempfile.TemporaryDirectory(prefix="neo4j-runtime-cross-hardlink-") as raw:
            fixture = LifecycleFixture(Path(raw))
            source_relative = CANONICAL_SENTINELS[0]
            runtime_relative = CANONICAL_SENTINELS[1]
            runtime_file = fixture.store / runtime_relative
            runtime_file.unlink()
            os.link(fixture.source / source_relative, runtime_file)
            with mock.patch.object(
                freezer,
                "_docker_mount_audit",
                return_value={
                    "running_container_count": 0,
                    "inspected_container_ids": [],
                    "overlapping_mounts": [],
                },
            ):
                with self.assertRaisesRegex(ContractError, "shares file inodes"):
                    lifecycle.prepare_clone_identity(fixture.launch_args())

        with tempfile.TemporaryDirectory(prefix="neo4j-runtime-digest-") as raw:
            fixture = LifecycleFixture(Path(raw))
            args = fixture.launch_args()
            args.image_digest = "sha256:" + "9" * 64
            with self.assertRaisesRegex(ContractError, "frozen 5.26.24 image"):
                lifecycle.run_launch(args)

    def test_same_size_different_content_clone_is_rejected_before_p31(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-runtime-content-drift-") as raw:
            fixture = LifecycleFixture(Path(raw))
            target = fixture.store / CANONICAL_SENTINELS[0]
            original = target.read_bytes()
            target.write_bytes(bytes([original[0] ^ 1]) + original[1:])
            docker_audit = {
                "running_container_count": 0,
                "inspected_container_ids": [],
                "overlapping_mounts": [],
            }
            with mock.patch.object(freezer, "_docker_mount_audit", return_value=docker_audit):
                with self.assertRaisesRegex(ContractError, "content trees differ"):
                    lifecycle.prove_independent_clone(fixture.source, fixture.store)

    def test_create_without_full_id_never_cleans_up_by_name(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-runtime-create-no-id-") as raw:
            fixture = LifecycleFixture(Path(raw))
            roots = {
                "repeat_root": fixture.repeat,
                "source_root": fixture.source,
                "store_root": fixture.store,
                "logs_root": fixture.logs,
            }
            malformed = subprocess.CompletedProcess([], 0, stdout="not-a-full-id\n", stderr="")
            with mock.patch.object(
                lifecycle, "prepare_clone_identity", return_value=({}, roots)
            ), mock.patch.object(
                lifecycle, "image_identity", return_value=fixture.image
            ), mock.patch.object(lifecycle, "ensure_container_absent"), mock.patch.object(
                lifecycle, "ensure_port_available"
            ), mock.patch.object(
                lifecycle, "docker_call", return_value=malformed
            ), mock.patch.object(lifecycle, "remove_failed_container") as cleanup:
                with self.assertRaisesRegex(ContractError, "full container ID"):
                    lifecycle.run_launch(fixture.launch_args())
            cleanup.assert_not_called()

    def test_pinned_launch_receipt_detects_tamper(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-runtime-tamper-") as raw:
            fixture = LifecycleFixture(Path(raw))
            document = fixture.run_launch()
            pinned_sha = sha256_file(fixture.output)
            document["docker"]["running"]["runtime"]["pid"] = 9999
            write_json(fixture.output, document)
            with mock.patch.object(lifecycle, "current_host", return_value=HOST):
                with self.assertRaisesRegex(ContractError, "SHA-256 mismatch"):
                    lifecycle.read_pinned_launch_receipt(fixture.output, pinned_sha)

    def test_cross_repeat_validator_rejects_reused_port(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-runtime-repeats-") as raw:
            fixture = LifecycleFixture(Path(raw))
            first = fixture.run_launch()
            second = copy.deepcopy(first)
            repeat_two = fixture.root / "repeat-02"
            store_two = repeat_two / "data"
            logs_two = repeat_two / "logs"
            second["repeat"] = {"repeat_index": 2, "clone_id": "sf10-clone-r02", "repeat_root": str(repeat_two)}
            second["clone"]["clone_id"] = "sf10-clone-r02"
            second["clone"]["repeat_index"] = 2
            second["clone"]["repeat_root"] = str(repeat_two)
            second["clone"]["runtime_store_root"] = {
                "path": str(store_two), "device": 1, "inode": 222,
                "uid": os.geteuid(), "gid": os.getegid(), "mode": "0700",
            }
            second["clone"]["logs_root"] = {
                "path": str(logs_two), "device": 1, "inode": 223,
                "uid": os.geteuid(), "gid": os.getegid(), "mode": "0700",
            }
            for phase in ("before_hash", "after_hash"):
                runtime_gate = second["clone"]["independence_audit"]["runtime_offline_gate"][phase]
                runtime_gate["root_identity"].update(
                    {"path": str(store_two), "device": 1, "inode": 222}
                )
                runtime_gate["store_lock"].update(
                    {"path": str(store_two / "databases" / "store_lock"), "device": 1, "inode": 224}
                )
            contract = second["runtime_contract"]
            contract.update(
                {
                    "container_name": "cidr-p10-neo4j-sf10-r02",
                    "repeat_index": 2,
                    "clone_id": "sf10-clone-r02",
                    "store_root": str(store_two),
                    "logs_root": str(logs_two),
                    "bolt_port": 17688,
                    "labels": lifecycle.expected_labels(2, "sf10-clone-r02"),
                }
            )
            for phase in ("created", "running"):
                config = second["docker"][phase]["config"]
                config["container_name"] = contract["container_name"]
                config["container_id"] = "7" * 64
                config["labels"] = dict(contract["labels"])
                config["port_bindings"] = {"7687/tcp": [{"HostIp": "127.0.0.1", "HostPort": "17688"}]}
                for mount in config["mounts"]:
                    if mount["destination"] == "/data":
                        mount["source"] = str(store_two)
                    else:
                        mount["source"] = str(logs_two)
            second["docker"]["create"]["command"] = lifecycle.build_create_command(contract)
            second["docker"]["create"]["stdout"] = "7" * 64
            second["docker"]["start"]["command"] = ["docker", "container", "start", "7" * 64]
            second["docker"]["start"]["stdout"] = "7" * 64
            lifecycle.validate_launch_receipts_independent([first, second])
            different_golden = copy.deepcopy(second)
            different_golden["clone"]["independence_audit"]["source_tree"]["store_sha256"] = "8" * 64
            different_golden["clone"]["independence_audit"]["runtime_tree"]["store_sha256"] = "8" * 64
            with self.assertRaisesRegex(ContractError, "one identical golden source"):
                lifecycle.validate_launch_receipts_independent([first, different_golden])
            second["runtime_contract"]["bolt_port"] = first["runtime_contract"]["bolt_port"]
            for phase in ("created", "running"):
                second["docker"][phase]["config"]["port_bindings"] = copy.deepcopy(
                    first["docker"]["running"]["config"]["port_bindings"]
                )
            second["docker"]["create"]["command"] = lifecycle.build_create_command(second["runtime_contract"])
            with self.assertRaisesRegex(ContractError, "reuse bolt_port"):
                lifecycle.validate_launch_receipts_independent([first, second])

    def test_stop_receipt_binds_graceful_zero_exit_and_post_inspect(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-runtime-stop-") as raw:
            fixture = LifecycleFixture(Path(raw))
            launch = fixture.run_launch()
            launch_sha = sha256_file(fixture.output)
            stop_result = subprocess.CompletedProcess([], 0, stdout=CONTAINER_ID + "\n", stderr="")
            remove_result = subprocess.CompletedProcess([], 0, stdout=CONTAINER_ID + "\n", stderr="")
            absent_result = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            args = lifecycle.parse_args(
                [
                    "stop",
                    "--launch-receipt", str(fixture.output),
                    "--launch-receipt-sha256", launch_sha,
                    "--timeout-s", "60",
                    "--output", str(fixture.stop_output),
                ]
            )
            with mock.patch.object(lifecycle, "current_host", return_value=HOST), mock.patch.object(
                lifecycle, "docker_inspect_container", side_effect=[fixture.inspect("running"), fixture.inspect("stopped")]
            ) as inspected, mock.patch.object(
                lifecycle, "docker_call", side_effect=[stop_result, remove_result, absent_result]
            ):
                stop = lifecycle.run_stop(args)
            self.assertEqual(stop["launch_identity"]["pid"], launch["docker"]["running"]["runtime"]["pid"])
            self.assertFalse(stop["post_stop_inspect"]["runtime"]["running"])
            self.assertEqual(stop["post_stop_inspect"]["runtime"]["exit_code"], 0)
            self.assertEqual(stop["stop"]["command"][-1], CONTAINER_ID)
            self.assertEqual(stop["remove"]["command"], ["docker", "container", "rm", CONTAINER_ID])
            self.assertTrue(stop["outcome"]["container_absent"])
            self.assertEqual(
                stop["post_stop_filesystem"]["logs_root"]["mode"],
                "0700",
            )
            self.assertEqual([call.args[0] for call in inspected.call_args_list], [CONTAINER_ID, CONTAINER_ID])
            lifecycle.validate_stop_receipt(
                stop,
                verify_artifacts=True,
                expected_host=HOST,
                launch_document=launch,
            )
            wrong_owner_mode = copy.deepcopy(stop)
            wrong_owner_mode["post_stop_filesystem"]["logs_root"]["mode"] = "0755"
            with self.assertRaisesRegex(ContractError, "mode must be 0700"):
                lifecycle.validate_stop_receipt(
                    wrong_owner_mode,
                    verify_artifacts=False,
                    expected_host=HOST,
                    launch_document=launch,
                )

    def test_stop_rejects_nonzero_neo4j_exit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="neo4j-runtime-stop-fail-") as raw:
            fixture = LifecycleFixture(Path(raw))
            fixture.run_launch()
            args = lifecycle.parse_args(
                [
                    "stop",
                    "--launch-receipt", str(fixture.output),
                    "--launch-receipt-sha256", sha256_file(fixture.output),
                    "--output", str(fixture.stop_output),
                ]
            )
            stop_result = subprocess.CompletedProcess([], 0, stdout=CONTAINER_ID + "\n", stderr="")
            with mock.patch.object(lifecycle, "current_host", return_value=HOST), mock.patch.object(
                lifecycle, "docker_inspect_container",
                side_effect=[fixture.inspect("running"), fixture.inspect("stopped", exit_code=143)],
            ), mock.patch.object(lifecycle, "docker_call", return_value=stop_result):
                with self.assertRaisesRegex(ContractError, "exit cleanly"):
                    lifecycle.run_stop(args)
            self.assertFalse(fixture.stop_output.exists())


if __name__ == "__main__":
    unittest.main()
