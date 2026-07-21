#!/usr/bin/env python3
"""Real Neo4j tiny fixture and fail-closed tests (never performance data)."""

from __future__ import annotations

import csv
import hashlib
import importlib.metadata
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from neo4j import GraphDatabase

P10_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = P10_DIR.parents[2]
sys.path.insert(0, str(P10_DIR))

from p10_contract import (  # noqa: E402
    CLOCK_NAME,
    CONTRACT_VERSION,
    EXTERNAL_PROCESS_LIFETIME,
    INTERFACE_SCOPE,
    REQUEST_SCHEMA_VERSION,
    SEQUENCE_DIGEST_ALGORITHM,
    TIMING_BOUNDARY,
    TRUTH_DIGEST_ALGORITHM,
    sha256_file,
    validate_adapter_outputs,
)

IMAGE = "neo4j:5.26.24"
MASK = (1 << 64) - 1
TREE_HASH_METHOD = "sha256-tree-v1(relative-path,size,file-sha256)"


def mix64(value: int) -> int:
    value = (value + 0x9E3779B97F4A7C15) & MASK
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & MASK
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & MASK
    return (value ^ (value >> 31)) & MASK


def digest(destinations: list[int]) -> tuple[int, int, int]:
    total = 0
    xor = 0
    for destination in destinations:
        total = (total + mix64(destination)) & MASK
        xor ^= mix64(destination ^ 0xD6E8FEB86659FD93)
    return len(destinations), total, xor


def tree_manifest(root: Path) -> dict[str, int | str]:
    files = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix().encode(),
    )
    result = hashlib.sha256()
    total = 0
    for path in files:
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        total += size
        result.update(f"file\0{relative}\0{size}\0{sha256_file(path)}\n".encode())
    return {"sha256": result.hexdigest(), "file_count": len(files), "total_bytes": total}


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class Neo4jAdapterTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        if os.environ.get("CIDR_SKIP_REAL_NEO4J_TESTS") == "1":
            raise unittest.SkipTest("real Neo4j fixture disabled by CIDR_SKIP_REAL_NEO4J_TESTS")
        if shutil.which("docker") is None:
            raise unittest.SkipTest("docker is unavailable")
        inspected = subprocess.run(
            ["docker", "image", "inspect", IMAGE],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if inspected.returncode != 0:
            raise unittest.SkipTest(f"pinned image is unavailable: {IMAGE}")
        image = json.loads(inspected.stdout)[0]
        repo_digests = image.get("RepoDigests") or []
        cls.image_digest = next(
            (item.split("@", 1)[1] for item in repo_digests if item.startswith("neo4j@sha256:")),
            None,
        )
        if cls.image_digest is None:
            raise unittest.SkipTest("pinned Neo4j image has no RepoDigest")

        cls.root = Path(tempfile.mkdtemp(prefix="p10-neo4j-real-fixture-"))
        cls.store = cls.root / "store"
        cls.import_dir = cls.root / "import"
        cls.dataset = cls.root / "dataset"
        cls.store.mkdir()
        cls.import_dir.mkdir()
        cls.dataset.mkdir()
        for path in (cls.store, cls.import_dir):
            path.chmod(0o777)
        (cls.dataset / "edges-dense.txt").write_text(
            "4\n0 1 1\n0 1 2\n2 -1 3\n",
            encoding="utf-8",
        )
        (cls.import_dir / "nodes.csv").write_text(
            "neo_id:ID(V),id:long\n0,0\n1,1\n2,2\n3,3\n",
            encoding="utf-8",
        )
        (cls.import_dir / "rels.csv").write_text(
            ":START_ID(V),:END_ID(V),:TYPE\n0,1,E_P1\n0,2,E_P1\n2,3,E_N1\n",
            encoding="utf-8",
        )
        cls.truth = cls.root / "truth.tsv"
        with cls.truth.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
            writer.writerow(["query_index", "edge_type", "src", "count", "sum_hash", "xor_hash"])
            writer.writerow([0, 1, 0, *digest([1, 2])])
            writer.writerow([1, -1, 2, *digest([3])])
        cls.formal_truth = cls.root / "formal-truth.tsv"
        with cls.formal_truth.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
            writer.writerow(["query_index", "edge_type", "src", "count", "sum_hash", "xor_hash"])
            row_digest = digest([1, 2])
            for query_index in range(1700):
                writer.writerow([query_index, 1, 0, *row_digest])

        dataset_tree = tree_manifest(cls.dataset)
        cls.dataset_manifest = cls.root / "dataset-manifest.json"
        write_json(
            cls.dataset_manifest,
            {
                "schema_version": "p02b-dataset-manifest-v1",
                "dataset_root": str(cls.dataset.resolve()),
                "dataset_sha256": dataset_tree["sha256"],
                "hash_method": TREE_HASH_METHOD,
                "file_count": dataset_tree["file_count"],
                "total_bytes": dataset_tree["total_bytes"],
            },
        )
        cls.import_container = f"cidr-p10-neo4j-import-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        imported = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--name",
                cls.import_container,
                "--network",
                "none",
                "--restart",
                "no",
                "-v",
                f"{cls.store.resolve()}:/data",
                "-v",
                f"{cls.import_dir.resolve()}:/import:ro",
                IMAGE,
                "neo4j-admin",
                "database",
                "import",
                "full",
                "--overwrite-destination=true",
                "--id-type=integer",
                "--nodes=V=/import/nodes.csv",
                "--relationships=/import/rels.csv",
                "--",
                "neo4j",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=180,
        )
        if imported.returncode != 0:
            cls._cleanup_resources()
            raise RuntimeError(f"Neo4j tiny import failed:\n{imported.stdout}")

        # Build the frozen :V(id) RANGE index before taking the offline
        # snapshot.  The later query container is read-only and may not mutate
        # schema state.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            index_port = listener.getsockname()[1]
        cls.index_container = f"cidr-p10-neo4j-index-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        indexed = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                cls.index_container,
                "--restart",
                "no",
                "-p",
                f"127.0.0.1:{index_port}:7687",
                "-v",
                f"{cls.store.resolve()}:/data",
                "-e",
                "NEO4J_AUTH=none",
                IMAGE,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if indexed.returncode != 0:
            cls._cleanup_resources()
            raise RuntimeError(f"Neo4j index preparation container failed: {indexed.stderr}")
        index_uri = f"bolt://127.0.0.1:{index_port}"
        deadline = time.monotonic() + 180
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            index_driver = None
            try:
                index_driver = GraphDatabase.driver(index_uri, auth=None)
                index_driver.verify_connectivity()
                with index_driver.session(database="neo4j") as session:
                    session.run("CREATE INDEX v_id IF NOT EXISTS FOR (v:V) ON (v.id)").consume()
                    session.run("CALL db.awaitIndexes()").consume()
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(1)
            finally:
                if index_driver is not None:
                    index_driver.close()
        else:
            cls._cleanup_resources()
            raise RuntimeError(f"Neo4j fixture index was not ready: {last_error}")
        stopped = subprocess.run(
            ["docker", "stop", "--time", "60", cls.index_container],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if stopped.returncode != 0:
            cls._cleanup_resources()
            raise RuntimeError(f"Neo4j index preparation did not stop cleanly: {stopped.stderr}")
        removed = subprocess.run(
            ["docker", "rm", cls.index_container],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if removed.returncode != 0:
            cls._cleanup_resources()
            raise RuntimeError(f"Neo4j index preparation container was not removed: {removed.stderr}")
        cls.index_container = None

        cls.freezer = P10_DIR / "adapters" / "freeze_neo4j_store.py"
        cls.store_manifest = cls.root / "store-manifest.json"
        frozen = subprocess.run(
            [
                sys.executable,
                "-B",
                str(cls.freezer),
                "--store-root",
                str(cls.store),
                "--dataset-manifest",
                str(cls.dataset_manifest),
                "--truth",
                str(cls.truth),
                "--neo4j-version",
                "5.26.24",
                "--image-digest",
                cls.image_digest,
                "--import-image-identity",
                "verified-repodigest",
                "--import-image-digest",
                cls.image_digest,
                "--sentinel",
                "databases/neo4j/neostore.nodestore.db",
                "--sentinel",
                "databases/neo4j/neostore.relationshipstore.db",
                "--output",
                str(cls.store_manifest),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if frozen.returncode != 0:
            cls._cleanup_resources()
            raise RuntimeError(f"Neo4j store freeze failed: {frozen.stderr}")
        cls.store_lineage = json.loads(cls.store_manifest.read_text(encoding="utf-8"))["store_sha256"]
        cls.formal_store_manifest = cls.root / "formal-store-manifest.json"
        formal_frozen = subprocess.run(
            [
                sys.executable,
                "-B",
                str(cls.freezer),
                "--store-root",
                str(cls.store),
                "--dataset-manifest",
                str(cls.dataset_manifest),
                "--truth",
                str(cls.formal_truth),
                "--neo4j-version",
                "5.26.24",
                "--image-digest",
                cls.image_digest,
                "--import-image-identity",
                "verified-repodigest",
                "--import-image-digest",
                cls.image_digest,
                "--sentinel",
                "databases/neo4j/neostore.nodestore.db",
                "--sentinel",
                "databases/neo4j/neostore.relationshipstore.db",
                "--output",
                str(cls.formal_store_manifest),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if formal_frozen.returncode != 0:
            cls._cleanup_resources()
            raise RuntimeError(f"Neo4j formal fixture store freeze failed: {formal_frozen.stderr}")

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            cls.port = listener.getsockname()[1]
        cls.container = f"cidr-p10-neo4j-fixture-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        started = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                cls.container,
                "--restart",
                "no",
                "-p",
                f"127.0.0.1:{cls.port}:7687",
                "-v",
                f"{cls.store.resolve()}:/data",
                "-e",
                "NEO4J_AUTH=none",
                "-e",
                "NEO4J_server_databases_default__to__read__only=true",
                IMAGE,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if started.returncode != 0:
            cls._cleanup_resources()
            raise RuntimeError(f"Neo4j tiny server failed to start: {started.stderr}")
        cls.uri = f"bolt://127.0.0.1:{cls.port}"
        deadline = time.monotonic() + 180
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            driver = None
            try:
                driver = GraphDatabase.driver(cls.uri, auth=None)
                driver.verify_connectivity()
                cls.server_agent = driver.get_server_info().agent
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(2)
            finally:
                if driver is not None:
                    driver.close()
        else:
            logs = subprocess.run(
                ["docker", "logs", cls.container],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            ).stdout
            cls._cleanup_resources()
            raise RuntimeError(f"Neo4j tiny server not ready: {last_error}\n{logs}")

        cls.adapter = P10_DIR / "adapters" / "neo4j_adapter.py"
        cls.runner = P10_DIR / "run_suite.py"
        cls.fixture_suite = Path(__file__).resolve().parent / "fixture-suite.json"
        cls.fixture_p31 = Path(__file__).resolve().parent / "fixture_p31.sh"
        cls.driver_version = importlib.metadata.version("neo4j")

    @classmethod
    def _cleanup_resources(cls) -> None:
        for attribute in ("container", "import_container", "index_container"):
            name = getattr(cls, attribute, None)
            if name:
                subprocess.run(
                    ["docker", "rm", "-f", name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
        root = getattr(cls, "root", None)
        if root and root.exists():
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "-u",
                    "root",
                    "-v",
                    f"{root.resolve()}:/target",
                    IMAGE,
                    "chown",
                    "-R",
                    f"{os.getuid()}:{os.getgid()}",
                    "/target",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            shutil.rmtree(root, ignore_errors=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._cleanup_resources()

    def request(
        self,
        truth: Path | None = None,
        query_count: int = 2,
        mode: str = "fixture",
    ) -> dict:
        truth = truth or self.truth
        dataset_manifest = json.loads(self.dataset_manifest.read_text(encoding="utf-8"))
        return {
            "schema_version": REQUEST_SCHEMA_VERSION,
            "contract_version": CONTRACT_VERSION,
            "suite_id": "p10-neo4j-real-fixture",
            "run_id": "fixture-r01",
            "execution_mode": mode,
            "system_id": "neo4j",
            "group": "client-server",
            "system_version": "neo4j-community-5.26.24",
            "interface_scope": INTERFACE_SCOPE,
            "repeat_index": 1,
            "process_lifetime": EXTERNAL_PROCESS_LIFETIME,
            "binary": {"path": sys.executable, "sha256": sha256_file(Path(sys.executable))},
            "dataset": {
                "path": str(self.dataset.resolve()),
                "sha256": dataset_manifest["dataset_sha256"],
            },
            "runtime_libraries": [],
            "store_roots": [
                {
                    "label": "neo4j-runtime",
                    "path": str(self.store.resolve()),
                    "sha256": self.store_lineage,
                }
            ],
            "truth": {
                "path": str(truth.resolve()),
                "sha256": sha256_file(truth),
                "query_count": query_count,
                "digest_algorithm": TRUTH_DIGEST_ALGORITHM,
            },
            "timing": {
                "timing_boundary": TIMING_BOUNDARY,
                "clock": CLOCK_NAME,
                "cache_policy": "full-shared-trace-before-each-measured-repeat-v1",
                "process_reuse_between_phases": True,
                "warmup_passes": 1,
                "measured_passes": 1,
                "concurrency": 1,
                "per_query_timeout_ms": 5000,
                "sequence_digest_algorithm": SEQUENCE_DIGEST_ALGORITHM,
            },
            "external_service": {
                "service_lifecycle": "external-prestarted",
                "containers": [self.container],
                "extra_pids": [],
                "image_digests": [self.image_digest],
            },
        }

    def adapter_command(
        self,
        request_path: Path,
        output: Path,
        store_manifest: Path | None = None,
        mode: str = "fixture",
        repo_root: Path | None = None,
    ) -> list[str]:
        store_manifest = store_manifest or self.store_manifest
        command = [
            sys.executable,
            "-B",
            str(self.adapter),
            "--mode",
            mode,
            "--uri",
            self.uri,
            "--expected-image-ref",
            IMAGE,
            "--expected-driver-version",
            self.driver_version,
            "--expected-server-agent",
            self.server_agent,
            "--dataset-manifest",
            str(self.dataset_manifest),
            "--dataset-manifest-sha256",
            sha256_file(self.dataset_manifest),
            "--store-manifest",
            str(store_manifest),
            "--store-manifest-sha256",
            sha256_file(store_manifest),
            "--request",
            str(request_path),
            "--output-dir",
            str(output),
        ]
        if repo_root is not None:
            command[-4:-4] = ["--repo-root", str(repo_root)]
        return command

    def invoke(self, root: Path, request: dict) -> subprocess.CompletedProcess[str]:
        request_path = root / "request.json"
        write_json(request_path, request)
        return subprocess.run(
            self.adapter_command(request_path, root / "output"),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )

    def test_real_bolt_fixture_is_contract_valid(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-neo4j-adapter-output-") as raw:
            root = Path(raw)
            request = self.request()
            completed = self.invoke(root, request)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            truth_rows = []
            with self.truth.open("r", encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle, delimiter="\t"):
                    truth_rows.append({key: int(value) for key, value in row.items()})
            validated = validate_adapter_outputs(
                output_dir=root / "output",
                request=request,
                system={
                    "id": "neo4j",
                    "display_name": "Neo4j Community",
                    "group": "client-server",
                    "system_version": request["system_version"],
                    "fixture_only": True,
                },
                truth_rows=truth_rows,
                max_timeouts=0,
            )
            self.assertEqual(validated["query_count"], 2)
            self.assertEqual(validated["completed_queries"], 2)
            provenance = json.loads(
                (root / "output" / "adapter-provenance.json").read_text(encoding="utf-8")
            )
            self.assertFalse(provenance["performance_eligible"])
            self.assertEqual(
                provenance["container_lifecycle"]["before"]["expected_repo_digest"],
                self.image_digest,
            )
            self.assertEqual(provenance["python_driver"]["version"], self.driver_version)

    def test_image_and_driver_tamper_fail_before_queries(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-neo4j-adapter-tamper-") as raw:
            root = Path(raw)
            request = self.request()
            request["external_service"]["image_digests"] = ["sha256:" + "0" * 64]
            completed = self.invoke(root, request)
            self.assertEqual(completed.returncode, 2)
            self.assertIn("image_digest mismatch", completed.stderr)
            self.assertFalse((root / "output" / "query-observations.tsv").exists())

            second = root / "driver"
            second.mkdir()
            request = self.request()
            request_path = second / "request.json"
            write_json(request_path, request)
            command = self.adapter_command(request_path, second / "output")
            position = command.index("--expected-driver-version") + 1
            command[position] = "0.0.0-invalid"
            completed = subprocess.run(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=60,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("Python driver", completed.stderr)
            self.assertFalse((second / "output" / "query-observations.tsv").exists())

    def test_formal_mode_requires_canonical_p02b_before_queries(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-neo4j-formal-gate-") as raw:
            root = Path(raw)
            clean_repo = root / "clean-repo"
            clean_repo.mkdir()
            subprocess.run(["git", "init", "-q", str(clean_repo)], check=True)
            subprocess.run(["git", "-C", str(clean_repo), "config", "user.email", "fixture@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(clean_repo), "config", "user.name", "fixture"], check=True)
            (clean_repo / "README").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(clean_repo), "add", "README"], check=True)
            subprocess.run(["git", "-C", str(clean_repo), "commit", "-q", "-m", "fixture"], check=True)
            request = self.request(self.formal_truth, 1700, mode="formal")
            request_path = root / "request.json"
            write_json(request_path, request)
            completed = subprocess.run(
                self.adapter_command(
                    request_path,
                    root / "output",
                    store_manifest=self.formal_store_manifest,
                    mode="formal",
                    repo_root=clean_repo,
                ),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=60,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("requires --p02b-result", completed.stderr)
            self.assertFalse((root / "output" / "query-observations.tsv").exists())

    def test_orchestrator_p31_bridge_executes_real_adapter(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-neo4j-orchestrated-") as raw:
            root = Path(raw)
            manifest = json.loads(self.fixture_suite.read_text(encoding="utf-8"))
            dataset_manifest = json.loads(self.dataset_manifest.read_text(encoding="utf-8"))
            manifest["dataset"] = {
                "path": str(self.dataset.resolve()),
                "sha256": dataset_manifest["dataset_sha256"],
            }
            manifest["truth"] = {
                "path": str(self.truth.resolve()),
                "sha256": sha256_file(self.truth),
                "query_count": 2,
                "digest_algorithm": TRUTH_DIGEST_ALGORITHM,
            }
            manifest["protocol"]["repeats"] = 1
            manifest["protocol"]["per_query_timeout_ms"] = 5000
            neo4j_system = next(system for system in manifest["systems"] if system["id"] == "neo4j")
            neo4j_system["fixture_only"] = False
            neo4j_system["process_lifetime"] = EXTERNAL_PROCESS_LIFETIME
            neo4j_system["adapter"] = {
                "path": str(self.adapter),
                "sha256": sha256_file(self.adapter),
                "args": [],
            }
            # adapter_command contains the Python interpreter and script prefix;
            # orchestrator adds --request/--output-dir itself.
            neo4j_system["adapter"]["args"] = [
                "--mode",
                "fixture",
                "--uri",
                self.uri,
                "--expected-image-ref",
                IMAGE,
                "--expected-driver-version",
                self.driver_version,
                "--expected-server-agent",
                self.server_agent,
                "--dataset-manifest",
                str(self.dataset_manifest),
                "--dataset-manifest-sha256",
                sha256_file(self.dataset_manifest),
                "--store-manifest",
                str(self.store_manifest),
                "--store-manifest-sha256",
                sha256_file(self.store_manifest),
            ]
            neo4j_system["binary"] = {
                "path": sys.executable,
                "sha256": sha256_file(Path(sys.executable)),
            }
            neo4j_system["runtime_libraries"] = []
            neo4j_system["store_roots"] = [
                {
                    "label": "neo4j-runtime",
                    "path": str(self.store.resolve()),
                    "sha256": self.store_lineage,
                }
            ]
            neo4j_system["containers"] = [self.container]
            neo4j_system["extra_pids"] = []
            neo4j_system["image_digests"] = [self.image_digest]
            manifest_path = root / "suite.json"
            write_json(manifest_path, manifest)
            run_root = root / "run"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(self.runner),
                    "run",
                    "--manifest",
                    str(manifest_path),
                    "--run-root",
                    str(run_root),
                    "--mode",
                    "fixture",
                    "--p31-wrapper",
                    str(self.fixture_p31),
                    "--system",
                    "neo4j",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=120,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue((run_root / "PARTIAL-DONE").is_file())
            request = json.loads(
                (
                    run_root
                    / "systems"
                    / "neo4j"
                    / "repeat-01"
                    / "adapter-request.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(request["external_service"]["containers"], [self.container])
            self.assertEqual(request["external_service"]["image_digests"], [self.image_digest])


if __name__ == "__main__":
    unittest.main()
