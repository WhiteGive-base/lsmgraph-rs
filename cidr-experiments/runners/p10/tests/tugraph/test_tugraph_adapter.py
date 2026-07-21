#!/usr/bin/env python3

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path


P10_DIR = Path(__file__).resolve().parents[2]
ADAPTER_DIR = P10_DIR / "adapters" / "tugraph"
ADAPTER = ADAPTER_DIR / "tugraph_adapter.py"
BUILD_WORKER = ADAPTER_DIR / "build_tugraph_worker.py"
MAKE_STORE = ADAPTER_DIR / "make_store_manifest.py"
EDGE_LABELS = ADAPTER_DIR / "sf10-edge-type-labels.tsv"
FIXTURE_BUILDER_SOURCE = Path(__file__).resolve().with_name("tugraph_fixture_builder.cpp")
P02B_LINEAGE = P10_DIR.parent / "p02b" / "build_lineage_manifest.py"
BUILD_IMAGE = (
    "tugraph/tugraph-runtime-ubuntu18.04@"
    "sha256:b492ccc83f866170b1fb43da5fd89e0ea43199a3144e9faee5b0c02b167bcee5"
)
sys.path.insert(0, str(P10_DIR))

from p10_contract import (  # noqa: E402
    CONTRACT_VERSION,
    INTERFACE_SCOPE,
    REQUEST_SCHEMA_VERSION,
    SEQUENCE_DIGEST_ALGORITHM,
    TIMING_BOUNDARY,
    CLOCK_NAME,
    TRUTH_DIGEST_ALGORITHM,
    sha256_file,
    validate_adapter_outputs,
)


def mix64(value: int) -> int:
    mask = (1 << 64) - 1
    value = (value + 0x9E3779B97F4A7C15) & mask
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & mask
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & mask
    return (value ^ (value >> 31)) & mask


def digest(destinations: list[int]) -> tuple[int, int, int]:
    mask = (1 << 64) - 1
    total = 0
    xor = 0
    for destination in destinations:
        total = (total + mix64(destination)) & mask
        xor ^= mix64(destination ^ 0xD6E8FEB86659FD93)
    return len(destinations), total, xor


def edge_types() -> list[int]:
    return [*range(-17, 0), *range(1, 18)]


def load_adapter_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("cidr_tugraph_adapter_for_test", ADAPTER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TuGraphAdapterTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.prefix = Path(
            os.environ.get("TUGRAPH_PREFIX", "/home/ydl/apps/tugraph-4.5.2/usr/local")
        )
        cls.gfortran = Path(
            os.environ.get(
                "TUGRAPH_GFORTRAN_LIBDIR",
                "/home/ydl/apps/libs-root/usr/lib/x86_64-linux-gnu",
            )
        )
        cls.boost = Path(os.environ.get("TUGRAPH_BOOST_INCLUDE", "/home/ydl/apps/boost_1_75_0"))
        cls.date = Path(
            os.environ.get(
                "TUGRAPH_DATE_INCLUDE",
                "/data/WorkSpace/tugraph_ldbc_snb/deps/date/include",
            )
        )
        required = [
            cls.prefix / "include",
            cls.prefix / "lib64/lgraph/liblgraph.so",
            cls.boost / "boost",
            cls.date,
            cls.gfortran,
        ]
        cls.docker = shutil.which("docker")
        if cls.docker is None or any(not path.exists() for path in required):
            raise unittest.SkipTest("native TuGraph 4.5.2 development runtime is unavailable")
        image = subprocess.run(
            [cls.docker, "image", "inspect", BUILD_IMAGE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if image.returncode != 0:
            raise unittest.SkipTest("digest-pinned local GCC 8 build image is unavailable")

    def run_checked(self, command: list[str], expected: int = 0) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["CIDR_TUGRAPH_PASSWORD"] = "73@TuGraph"
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            expected,
            f"command={command!r}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        return completed

    def prepare_fixture(self, root: Path) -> dict[str, Path | str | dict]:
        worker = root / "tugraph-p10-worker"
        runtime_manifest = root / "runtime-manifest.json"
        self.run_checked(
            [
                sys.executable,
                "-B",
                str(BUILD_WORKER),
                "--prefix",
                str(self.prefix),
                "--gfortran-libdir",
                str(self.gfortran),
                "--boost-include",
                str(self.boost),
                "--date-include",
                str(self.date),
                "--output",
                str(worker),
                "--runtime-manifest",
                str(runtime_manifest),
            ]
        )

        builder = root / "tugraph-fixture-builder"
        libdir = self.prefix / "lib64/lgraph"
        uid = os.getuid()
        gid = os.getgid()
        compile_flags = [
            "-O2",
            "-DNDEBUG",
            "-std=c++17",
            "-fopenmp",
            "-I",
            "/cidr-adapter/compat",
            "-I",
            str(self.prefix / "include"),
            "-I",
            str(self.boost),
            "-I",
            str(self.date),
            "-L",
            str(self.gfortran),
            f"-Wl,-rpath,{libdir}",
            f"-Wl,-rpath,{self.gfortran}",
            "-Wl,--disable-new-dtags",
            "-Wl,--allow-shlib-undefined",
            "-o",
            f"/fixture-output/{builder.name}",
            f"/fixture-src/{FIXTURE_BUILDER_SOURCE.name}",
            str(libdir / "liblgraph.so"),
            "-lrt",
        ]
        compile_command = [
            self.docker,
            "run",
            "--rm",
            "--pull=never",
            "--name",
            f"cidr-p10-tugraph-fixture-build-{os.getpid()}",
            "--network",
            "none",
            "--read-only",
            "--user",
            f"{uid}:{gid}",
            "--tmpfs",
            f"/tmp:rw,exec,nosuid,size=256m,uid={uid},gid={gid}",
            "-v",
            f"{ADAPTER_DIR}:/cidr-adapter:ro",
            "-v",
            f"{FIXTURE_BUILDER_SOURCE.parent}:/fixture-src:ro",
            "-v",
            f"{self.prefix}:{self.prefix}:ro",
            "-v",
            f"{self.boost}:{self.boost}:ro",
            "-v",
            f"{self.date}:{self.date}:ro",
            "-v",
            f"{root}:/fixture-output:rw",
            BUILD_IMAGE,
            "/usr/local/bin/g++",
            *compile_flags,
        ]
        self.run_checked(compile_command)
        store = root / "store"
        self.run_checked([str(builder), str(store)])

        dataset = root / "dense-edges.txt"
        with dataset.open("w", encoding="utf-8") as handle:
            handle.write("4\n")
            for edge_type in edge_types():
                handle.write(f"0 {edge_type} 1\n")
                handle.write(f"0 {edge_type} 2\n")
                handle.write(f"1 {edge_type} 3\n")

        truth = root / "truth.tsv"
        with truth.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
            writer.writerow(["query_index", "edge_type", "src", "count", "sum_hash", "xor_hash"])
            query_index = 0
            for edge_type in edge_types():
                for sample in range(50):
                    source = sample % 2
                    destinations = [1, 2] if source == 0 else [3]
                    count, total, xor = digest(destinations)
                    writer.writerow([query_index, edge_type, source, count, total, xor])
                    query_index += 1
        self.assertEqual(query_index, 1700)

        store_lineage = root / "store-lineage.json"
        self.run_checked(
            [
                sys.executable,
                "-B",
                str(P02B_LINEAGE),
                "--kind",
                "store",
                "--root",
                str(store),
                "--output",
                str(store_lineage),
            ]
        )
        store_lineage_value = json.loads(store_lineage.read_text(encoding="utf-8"))
        store_manifest = root / "store-manifest.json"
        self.run_checked(
            [
                sys.executable,
                "-B",
                str(MAKE_STORE),
                "--store",
                str(store),
                "--store-lineage",
                str(store_lineage),
                "--dataset",
                str(dataset),
                "--dataset-sha256",
                sha256_file(dataset),
                "--truth",
                str(truth),
                "--truth-sha256",
                sha256_file(truth),
                "--edge-labels",
                str(EDGE_LABELS),
                "--importer",
                str(builder),
                "--import-config",
                str(dataset),
                "--source-revision",
                "isolated-small-fixture-not-performance",
                "--vertex-count",
                "4",
                "--password-sha256",
                hashlib.sha256(b"73@TuGraph").hexdigest(),
                "--fixture-only",
                "--output",
                str(store_manifest),
            ]
        )
        request = {
            "schema_version": REQUEST_SCHEMA_VERSION,
            "contract_version": CONTRACT_VERSION,
            "suite_id": "p10-tugraph-native-fixture",
            "run_id": "fixture-run",
            "execution_mode": "fixture",
            "system_id": "tugraph",
            "group": "embedded",
            "system_version": "TuGraph-4.5.2-native-embedded",
            "interface_scope": INTERFACE_SCOPE,
            "repeat_index": 1,
            "binary": {"path": str(worker), "sha256": sha256_file(worker)},
            "dataset": {"path": str(dataset), "sha256": sha256_file(dataset)},
            "store_roots": [
                {
                    "label": "tugraph",
                    "path": str(store),
                    "sha256": store_lineage_value["store_sha256"],
                }
            ],
            "truth": {
                "path": str(truth),
                "sha256": sha256_file(truth),
                "query_count": 1700,
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
                "per_query_timeout_ms": 100,
                "sequence_digest_algorithm": SEQUENCE_DIGEST_ALGORITHM,
            },
        }
        request_path = root / "request.json"
        request_path.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
        return {
            "worker": worker,
            "runtime_manifest": runtime_manifest,
            "store_manifest": store_manifest,
            "request": request,
            "request_path": request_path,
            "truth": truth,
        }

    def test_native_embedded_fixture_and_p10_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-tugraph-") as temporary:
            root = Path(temporary).resolve()
            fixture = self.prepare_fixture(root)
            output = root / "adapter-output"
            output.mkdir()
            self.run_checked(
                [
                    sys.executable,
                    "-B",
                    str(ADAPTER),
                    "--store-manifest",
                    str(fixture["store_manifest"]),
                    "--store-manifest-sha256",
                    sha256_file(fixture["store_manifest"]),
                    "--runtime-manifest",
                    str(fixture["runtime_manifest"]),
                    "--runtime-manifest-sha256",
                    sha256_file(fixture["runtime_manifest"]),
                    "--expected-truth-sha256",
                    sha256_file(fixture["truth"]),
                    "--request",
                    str(fixture["request_path"]),
                    "--output-dir",
                    str(output),
                ]
            )
            truth_rows: list[dict[str, int]] = []
            with Path(fixture["truth"]).open("r", encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle, delimiter="\t"):
                    truth_rows.append({key: int(value) for key, value in row.items()})
            validated = validate_adapter_outputs(
                output_dir=output,
                request=fixture["request"],
                system={
                    "id": "tugraph",
                    "display_name": "TuGraph",
                    "group": "embedded",
                    "system_version": "TuGraph-4.5.2-native-embedded",
                },
                truth_rows=truth_rows,
                max_timeouts=0,
            )
            self.assertEqual(validated["completed_queries"], 1700)
            self.assertEqual(validated["mismatch_queries"], 0)
            self.assertEqual(validated["timeout_queries"], 0)
            self.assertGreater(validated["latency_p50_us"], 0)
            self.assertLessEqual(validated["latency_p50_us"], validated["latency_p95_us"])
            self.assertLessEqual(validated["latency_p95_us"], validated["latency_p99_us"])
            provenance = json.loads(
                (output / "tugraph-runtime-provenance.json").read_text(encoding="utf-8")
            )
            self.assertEqual(provenance["execution_model"], "native-embedded-single-worker-process-v1")
            self.assertEqual(provenance["image_digests"], [])
            self.assertEqual(provenance["container_names"], [])
            self.assertGreater(provenance["worker_pid"], 1)

    def test_formal_p02b_is_mandatory_and_fixture_cannot_claim_it(self) -> None:
        adapter = load_adapter_module()
        empty = types.SimpleNamespace(
            p02b_result=None,
            p02b_validator=None,
            p02b_validator_sha256=None,
        )
        with self.assertRaisesRegex(Exception, "requires P02B"):
            adapter.consume_p02b(empty, True)
        marker = Path(__file__).resolve()
        claimed = types.SimpleNamespace(
            p02b_result=marker,
            p02b_validator=marker,
            p02b_validator_sha256=hashlib.sha256(marker.read_bytes()).hexdigest(),
        )
        with self.assertRaisesRegex(Exception, "must not claim"):
            adapter.consume_p02b(claimed, False)


if __name__ == "__main__":
    unittest.main()
