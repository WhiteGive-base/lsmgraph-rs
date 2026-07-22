#!/usr/bin/env python3
"""Real NebulaGraph v3.8.0 tiny fixture and fail-closed P10 tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

P10_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = P10_DIR.parents[2]
NEBULA_DIR = P10_DIR / "adapters" / "nebulagraph"
sys.path.insert(0, str(P10_DIR))
sys.path.insert(0, str(NEBULA_DIR))

from nebula_adapter import EXPECTED_IMAGES, consume_p02b  # noqa: E402
from p10_contract import (  # noqa: E402
    CLOCK_NAME,
    CONTRACT_VERSION,
    FIXTURE_PROCESS_LIFETIME,
    INTERFACE_SCOPE,
    REQUEST_SCHEMA_VERSION,
    SEQUENCE_DIGEST_ALGORITHM,
    TIMING_BOUNDARY,
    TRUTH_DIGEST_ALGORITHM,
    ContractError,
    read_truth,
    sha256_file,
    validate_adapter_outputs,
    validate_nebulagraph_p31_binding,
)


class NebulaGraphAdapterTest(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.client_root = Path(
            os.environ.get(
                "NEBULA_P10_CLIENT_ROOT",
                "/data/WorkSpace/lsmgraph-rs/baseline/external-baselines-20260626/3plus3-baselines/systems/nebulagraph/pydeps",
            )
        ).resolve()
        if not cls.client_root.is_dir() or subprocess.run(
            ["docker", "version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
        ).returncode != 0:
            raise unittest.SkipTest("NebulaGraph pydeps or Docker daemon is unavailable")
        for image in EXPECTED_IMAGES.values():
            if subprocess.run(
                ["docker", "image", "inspect", image["tag"]],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode != 0:
                raise unittest.SkipTest(f"exact image is unavailable: {image['tag']}")
        cls.adapter = NEBULA_DIR / "nebula_adapter.py"
        cls.runtime_builder = NEBULA_DIR / "build_runtime_manifest.py"
        cls.store_builder = NEBULA_DIR / "build_store_manifest.py"
        cls.dataset = REPO_ROOT / "baseline" / "shared-truth" / "fixtures" / "dense-edges.txt"
        cls.truth = REPO_ROOT / "baseline" / "shared-truth" / "fixtures" / "truth.tsv"
        cls.password = "nebula"
        cls.password_sha = hashlib.sha256(cls.password.encode()).hexdigest()

    def prepare(self, root: Path) -> tuple[dict, Path, Path, Path, str]:
        store = root / "store"
        store.mkdir()
        runtime_manifest = root / "runtime.json"
        built_runtime = subprocess.run(
            [
                sys.executable,
                "-B",
                str(self.runtime_builder),
                "--client-root",
                str(self.client_root),
                "--output",
                str(runtime_manifest),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=120,
        )
        self.assertEqual(built_runtime.returncode, 0, built_runtime.stderr)
        prefix = f"cidr-nebula-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        store_manifest = root / "store.json"
        built_store = subprocess.run(
            [
                sys.executable,
                "-B",
                str(self.store_builder),
                "--mode",
                "fixture",
                "--data-root",
                str(store),
                "--dataset",
                str(self.dataset),
                "--truth",
                str(self.truth),
                "--space",
                "cidr_p10_fixture",
                "--container-prefix",
                prefix,
                "--password-sha256",
                self.password_sha,
                "--output",
                str(store_manifest),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=120,
        )
        self.assertEqual(built_store.returncode, 0, built_store.stderr)
        binary = Path(sys.executable).resolve()
        metadata = next(self.client_root.glob("nebula3_python-*.dist-info/METADATA"))
        request = {
            "schema_version": REQUEST_SCHEMA_VERSION,
            "contract_version": CONTRACT_VERSION,
            "suite_id": "p10-nebulagraph-real-tiny-fixture",
            "run_id": f"nebulagraph-{uuid.uuid4().hex}",
            "execution_mode": "fixture",
            "system_id": "nebulagraph",
            "group": "client-server",
            "system_version": "NebulaGraph 3.8.0",
            "interface_scope": INTERFACE_SCOPE,
            "repeat_index": 1,
            "process_lifetime": FIXTURE_PROCESS_LIFETIME,
            "binary": {"path": str(binary), "sha256": sha256_file(binary)},
            "dataset": {"path": str(self.dataset), "sha256": sha256_file(self.dataset)},
            "runtime_libraries": [{"path": str(metadata), "sha256": sha256_file(metadata)}],
            "store_roots": [{"label": "nebulagraph", "path": str(store), "sha256": ""}],
            "truth": {
                "path": str(self.truth),
                "sha256": sha256_file(self.truth),
                "query_count": 2,
                "digest_algorithm": TRUTH_DIGEST_ALGORITHM,
            },
            "timing": {
                "timing_boundary": TIMING_BOUNDARY,
                "clock": CLOCK_NAME,
                "cache_policy": "full-shared-trace-before-each-measured-repeat-v1",
                "process_reuse_between_phases": True,
                "warmup_passes": 2,
                "measured_passes": 2,
                "concurrency": 1,
                "per_query_timeout_ms": 5000,
                "sequence_digest_algorithm": SEQUENCE_DIGEST_ALGORITHM,
            },
            "external_service": {
                "service_lifecycle": "external-prestarted",
                "containers": [f"{prefix}-graph", f"{prefix}-meta", f"{prefix}-storage"],
                "extra_pids": [],
                "image_digests": [
                    EXPECTED_IMAGES[role]["digest"]
                    for role in ("graphd", "metad", "storaged")
                ],
            },
        }
        return request, runtime_manifest, store_manifest, store, prefix

    def invoke(
        self,
        root: Path,
        request: dict,
        runtime_manifest: Path,
        store_manifest: Path,
        output_name: str,
    ) -> subprocess.CompletedProcess[str]:
        request_path = root / f"{output_name}-request.json"
        request_path.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
        output = root / output_name
        output.mkdir()
        env = os.environ.copy()
        env["CIDR_NEBULA_PASSWORD"] = self.password
        return subprocess.run(
            [
                sys.executable,
                "-B",
                str(self.adapter),
                "--service-mode",
                "managed-fixture",
                "--runtime-manifest",
                str(runtime_manifest),
                "--runtime-manifest-sha256",
                sha256_file(runtime_manifest),
                "--store-manifest",
                str(store_manifest),
                "--store-manifest-sha256",
                sha256_file(store_manifest),
                "--request",
                str(request_path),
                "--output-dir",
                str(output),
            ],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=240,
        )

    def test_real_ngql_fixture_warmup_then_measured_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-nebulagraph-real-") as raw:
            root = Path(raw)
            request, runtime, store_manifest, store, prefix = self.prepare(root)
            completed = self.invoke(root, request, runtime, store_manifest, "output")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            manifest_value = json.loads(store_manifest.read_text(encoding="utf-8"))
            validated = validate_adapter_outputs(
                output_dir=root / "output",
                request=request,
                system={
                    "id": "nebulagraph",
                    "display_name": "NebulaGraph",
                    "group": "client-server",
                    "system_version": "NebulaGraph 3.8.0",
                    "fixture_only": True,
                    "image_digests": [item["digest"] for item in EXPECTED_IMAGES.values()],
                    "containers": list(manifest_value["containers"].values()),
                },
                truth_rows=read_truth(self.truth, 2),
                max_timeouts=0,
            )
            self.assertEqual(validated["completed_queries"], 4)
            self.assertEqual(validated["mismatch_queries"], 0)
            self.assertEqual(validated["timeout_queries"], 0)
            provenance = validated["adapter_provenance"]
            self.assertEqual(provenance["client"]["version"], "3.8.3")
            self.assertEqual(provenance["service_session"]["restart_count"], 0)
            self.assertTrue(provenance["service_session"]["warmup_measured_same_session"])
            self.assertGreater(provenance["graph_endpoint"]["port"], 0)
            self.assertEqual(list(store.iterdir()), [])
            for suffix in ("meta", "storage", "graph"):
                self.assertNotEqual(
                    subprocess.run(
                        ["docker", "container", "inspect", f"{prefix}-{suffix}"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    ).returncode,
                    0,
                )
            self.assertNotEqual(
                subprocess.run(
                    ["docker", "network", "inspect", f"{prefix}-net"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                ).returncode,
                0,
            )

    def test_image_digest_drift_is_rejected_before_container_start(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-nebulagraph-drift-") as raw:
            root = Path(raw)
            request, runtime, store_manifest, store, _prefix = self.prepare(root)
            value = json.loads(runtime.read_text(encoding="utf-8"))
            value["images"][0]["repo_digest"] = "vesoft/nebula-graphd@sha256:" + "0" * 64
            runtime.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
            completed = self.invoke(root, request, runtime, store_manifest, "output")
            self.assertEqual(completed.returncode, 2)
            self.assertIn("repository digest drift", completed.stderr)
            self.assertEqual(list(store.iterdir()), [])

    def test_store_dataset_drift_is_rejected_before_container_start(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-nebulagraph-store-drift-") as raw:
            root = Path(raw)
            request, runtime, store_manifest, store, _prefix = self.prepare(root)
            value = json.loads(store_manifest.read_text(encoding="utf-8"))
            value["dataset"]["sha256"] = "0" * 64
            store_manifest.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
            completed = self.invoke(root, request, runtime, store_manifest, "output")
            self.assertEqual(completed.returncode, 2)
            self.assertIn("dataset SHA differs", completed.stderr)
            self.assertEqual(list(store.iterdir()), [])

    def test_formal_p02b_admission_is_mandatory(self) -> None:
        args = argparse.Namespace(
            p02b_result=None,
            p02b_result_sha256=None,
            p02b_validator=None,
            p02b_validator_sha256=None,
            p02b_binary=None,
            p02b_binary_sha256=None,
            p02b_max_age_seconds=21600,
        )
        with self.assertRaisesRegex(
            ContractError,
            r"^formal NebulaGraph run requires a complete P02B "
            r"result/validator/binary SHA-bound set$",
        ):
            consume_p02b(args, formal=True)

    def test_formal_p31_binding_covers_containers_inputs_and_store(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-nebulagraph-p31-") as raw:
            root = Path(raw)
            store = root / "store"
            store.mkdir()
            logs = root / "logs"
            logs.mkdir()
            binary = Path(sys.executable).resolve()
            binary_ref = {"path": str(binary), "sha256": sha256_file(binary)}
            dataset_ref = {"path": str(self.dataset), "sha256": sha256_file(self.dataset)}
            truth_ref = {"path": str(self.truth), "sha256": sha256_file(self.truth)}
            containers = ["cidr-nebula-graph", "cidr-nebula-meta", "cidr-nebula-storage"]
            roles = ("graphd", "metad", "storaged")
            snapshots = {}
            spec_roles = {}
            for index, (role, name) in enumerate(zip(roles, containers), start=1):
                role_store = store / role
                role_logs = logs / role
                role_store.mkdir()
                role_logs.mkdir()
                snapshots[role] = {
                    "role": role,
                    "name": name,
                    "logical_host": f"nebula-{role}",
                    "container_id": str(index) * 64,
                    "image_id": f"sha256:{str(index + 3) * 64}",
                    "config_image": f"nebula-{role}@sha256:{str(index + 6) * 64}",
                    "pid": 1000 + index,
                    "process_start_ticks": 200000 + index,
                    "started_at_utc": f"2026-01-01T00:00:0{index}Z",
                    "restart_count": 0,
                    "running": True,
                }
                spec_roles[role] = {
                    "mounts": [
                        {"source": str(role_store), "target": "/data"},
                        {"source": str(role_logs), "target": "/logs"},
                    ]
                }
            provenance = {
                "binary": binary_ref,
                "dataset": dataset_ref,
                "truth": truth_ref,
                "store": {"path": str(store), "sha256": "0" * 64},
                "container_names": containers,
                "container_runtime": [snapshots[role] for role in roles],
                "cluster_lifecycle": {
                    "preflight": {"receipt": {"spec": {"roles": spec_roles, "logs_root": str(logs)}}},
                    "start": {"receipt": {"containers": snapshots}},
                },
            }
            unique = {
                snapshots[role]["name"]: [{
                    "container_id": snapshots[role]["container_id"],
                    "pid": snapshots[role]["pid"],
                    "process_start_ticks": snapshots[role]["process_start_ticks"],
                    "started_at": snapshots[role]["started_at_utc"],
                    "restart_count": 0,
                }]
                for role in roles
            }
            p31 = {
                "collector": {"containers": containers, "extra_pids": []},
                "collector_result": {
                    "containers_seen": {
                        snapshots[role]["name"]: snapshots[role]["pid"] for role in roles
                    },
                    "container_identity_unique_set": unique,
                    "container_identity_history": {
                        name: [{**values[0], "before_sample_index": 0, "observed_at_utc": "2026-01-01T00:01:00Z"}]
                        for name, values in unique.items()
                    },
                    "ready": {"containers": {name: values[0] for name, values in unique.items()}},
                },
                "inputs": {
                    "binary": binary_ref,
                    "dataset": dataset_ref,
                    "truth": truth_ref,
                    "query_or_trace": truth_ref,
                },
                "disk_roots": [
                    {"role": "store", "label": "nebulagraph", "path": str(store)},
                    {"role": "temp", "label": "nebulagraph-logs", "path": str(logs)},
                ],
            }
            validate_nebulagraph_p31_binding(provenance, p31)
            p31["collector"] = {"containers": containers[:-1], "extra_pids": []}
            with self.assertRaisesRegex(ContractError, "container coverage"):
                validate_nebulagraph_p31_binding(provenance, p31)


if __name__ == "__main__":
    unittest.main()
