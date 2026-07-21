#!/usr/bin/env python3
"""Real LiveGraph fixture smoke for the P10 adapter (never performance data)."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

P10_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = P10_DIR.parents[2]
sys.path.insert(0, str(P10_DIR))

from p10_contract import (  # noqa: E402
    CLOCK_NAME,
    CONTRACT_VERSION,
    FRESH_IMPORT_PROCESS_LIFETIME,
    INTERFACE_SCOPE,
    REQUEST_SCHEMA_VERSION,
    SEQUENCE_DIGEST_ALGORITHM,
    TIMING_BOUNDARY,
    TRUTH_DIGEST_ALGORITHM,
    sha256_file,
    validate_adapter_outputs,
)


class LiveGraphAdapterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        raw_binary = os.environ.get("LIVEGRAPH_P10_BINARY")
        if not raw_binary:
            raise unittest.SkipTest("LIVEGRAPH_P10_BINARY is not set")
        cls.binary = Path(raw_binary).resolve()
        if not cls.binary.is_file():
            raise unittest.SkipTest(f"LiveGraph worker is missing: {cls.binary}")
        cls.adapter = P10_DIR / "adapters" / "livegraph_adapter.py"
        cls.runner = P10_DIR / "run_suite.py"
        cls.fixture_manifest = Path(__file__).resolve().parent / "fixture-suite.json"
        cls.fixture_p31 = Path(__file__).resolve().parent / "fixture_p31.sh"
        cls.dataset = REPO_ROOT / "baseline" / "shared-truth" / "fixtures" / "dense-edges.txt"
        cls.truth = REPO_ROOT / "baseline" / "shared-truth" / "fixtures" / "truth.tsv"
        capability = subprocess.run(
            [str(cls.binary), "--capabilities"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        cls.capability = json.loads(capability.stdout)
        cls.runtime_library = Path(cls.capability["runtime_library_path"]).resolve()

    def request(
        self,
        store: Path,
        *,
        execution_mode: str = "fixture",
        truth: Path | None = None,
        query_count: int = 2,
    ) -> dict:
        truth = truth or self.truth
        return {
            "schema_version": REQUEST_SCHEMA_VERSION,
            "contract_version": CONTRACT_VERSION,
            "suite_id": "p10-livegraph-real-fixture",
            "run_id": "fixture-r01",
            "execution_mode": execution_mode,
            "system_id": "livegraph",
            "group": "embedded",
            "system_version": "audited-livegraph-fixture",
            "interface_scope": INTERFACE_SCOPE,
            "repeat_index": 2,
            "process_lifetime": FRESH_IMPORT_PROCESS_LIFETIME,
            "binary": {"path": str(self.binary), "sha256": sha256_file(self.binary)},
            "dataset": {"path": str(self.dataset), "sha256": sha256_file(self.dataset)},
            "runtime_libraries": [
                {
                    "path": str(self.runtime_library),
                    "sha256": sha256_file(self.runtime_library),
                }
            ],
            "store_roots": [
                {
                    "label": "livegraph",
                    "path": str(store),
                    "sha256": "",
                }
            ],
            "truth": {
                "path": str(truth),
                "sha256": sha256_file(truth),
                "query_count": query_count,
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
                "per_query_timeout_ms": 1000,
                "sequence_digest_algorithm": SEQUENCE_DIGEST_ALGORITHM,
            },
        }

    def invoke(self, request: dict, root: Path) -> subprocess.CompletedProcess[str]:
        request_path = root / "request.json"
        output = root / "output"
        request_path.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
        return subprocess.run(
            [
                sys.executable,
                str(self.adapter),
                "--request",
                str(request_path),
                "--output-dir",
                str(output),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_real_livegraph_fixture_is_contract_valid(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-livegraph-fixture-") as raw:
            root = Path(raw)
            store = root / "store"
            store.mkdir()
            request = self.request(store)
            completed = self.invoke(request, root)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            truth_rows = []
            with self.truth.open("r", encoding="utf-8") as handle:
                header = next(handle)
                self.assertEqual(header, "query_index\tedge_type\tsrc\tcount\tsum_hash\txor_hash\n")
                for line in handle:
                    fields = [int(value) for value in line.rstrip("\n").split("\t")]
                    truth_rows.append(dict(zip(("query_index", "edge_type", "src", "count", "sum_hash", "xor_hash"), fields)))
            system = {
                "id": "livegraph",
                "display_name": "LiveGraph",
                "group": "embedded",
                "system_version": request["system_version"],
            }
            validated = validate_adapter_outputs(
                output_dir=root / "output",
                request=request,
                system=system,
                truth_rows=truth_rows,
                max_timeouts=0,
            )
            self.assertEqual(validated["query_count"], 2)
            self.assertEqual(validated["completed_queries"], 4)
            self.assertEqual(validated["mismatch_queries"], 0)
            self.assertGreater(validated["import_wall_s"], 0)
            self.assertGreater(validated["import_store_logical_bytes"], 0)
            self.assertTrue((store / "livegraph-block").is_file())
            self.assertTrue((store / "livegraph-wal").is_file())

    def test_worker_declares_fresh_import_and_runtime_library(self) -> None:
        self.assertEqual(
            self.capability["process_lifetime"], FRESH_IMPORT_PROCESS_LIFETIME
        )
        self.assertEqual(Path(self.capability["runtime_library_path"]).resolve(), self.runtime_library)

    def test_orchestrator_and_p31_bridge_execute_real_adapter(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-livegraph-orchestrated-") as raw:
            root = Path(raw)
            store = root / "store"
            store.mkdir()
            temp_base = root / "temp"
            temp_base.mkdir()
            manifest = json.loads(self.fixture_manifest.read_text(encoding="utf-8"))
            manifest["protocol"]["repeats"] = 2
            livegraph = next(system for system in manifest["systems"] if system["id"] == "livegraph")
            livegraph["adapter"] = {
                "path": str(self.adapter),
                "sha256": sha256_file(self.adapter),
                "args": [],
            }
            livegraph["binary"] = {
                "path": str(self.binary),
                "sha256": sha256_file(self.binary),
            }
            livegraph["process_lifetime"] = FRESH_IMPORT_PROCESS_LIFETIME
            livegraph["runtime_libraries"] = [
                {
                    "path": str(self.runtime_library),
                    "sha256": sha256_file(self.runtime_library),
                }
            ]
            livegraph["store_roots"] = [
                {"label": "livegraph", "path": str(store), "sha256": ""}
            ]
            livegraph["temp_roots"] = [{"label": "scratch", "path": str(temp_base)}]
            manifest_path = root / "suite.json"
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
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
                    "livegraph",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue((run_root / "PARTIAL-DONE").is_file())
            validated = json.loads(
                (
                    run_root
                    / "systems"
                    / "livegraph"
                    / "repeat-01"
                    / "validated-result.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(validated["completed_queries"], 2)
            self.assertEqual(validated["mismatch_queries"], 0)
            self.assertEqual(validated["process_lifetime"], FRESH_IMPORT_PROCESS_LIFETIME)
            self.assertGreater(validated["import_wall_s"], 0)
            with (run_root / "repeat-results.tsv").open(
                "r", encoding="utf-8", newline=""
            ) as handle:
                repeat_row = next(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual(
                repeat_row["process_lifetime"], FRESH_IMPORT_PROCESS_LIFETIME
            )
            self.assertGreater(float(repeat_row["import_wall_s"]), 0)
            self.assertGreater(int(repeat_row["import_store_logical_bytes"]), 0)
            with (run_root / "system-results.tsv").open(
                "r", encoding="utf-8", newline=""
            ) as handle:
                system_row = next(csv.DictReader(handle, delimiter="\t"))
            self.assertGreater(float(system_row["median_import_wall_s"]), 0)
            request = json.loads(
                (
                    run_root
                    / "systems"
                    / "livegraph"
                    / "repeat-01"
                    / "adapter-request.json"
                ).read_text(encoding="utf-8")
            )
            actual_store = Path(request["store_roots"][0]["path"])
            self.assertEqual(actual_store.parent, store)
            self.assertNotEqual(actual_store, store)
            self.assertTrue((actual_store / "livegraph-block").is_file())
            second_request = json.loads(
                (
                    run_root
                    / "systems"
                    / "livegraph"
                    / "repeat-02"
                    / "adapter-request.json"
                ).read_text(encoding="utf-8")
            )
            second_store = Path(second_request["store_roots"][0]["path"])
            self.assertNotEqual(second_store, actual_store)
            self.assertEqual(second_store.parent, store)
            self.assertEqual(len(list(temp_base.iterdir())), 2)
            p31_argv = (
                run_root / "systems" / "livegraph" / "repeat-01" / "p31" / "argv.txt"
            ).read_text(encoding="utf-8")
            for required in (
                "--binary-sha256",
                sha256_file(self.binary),
                "--dataset-sha256",
                sha256_file(self.dataset),
                "--truth-sha256",
                sha256_file(self.truth),
                "--config-sha256",
                "--store",
            ):
                self.assertIn(required, p31_argv)

    def test_formal_mode_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-livegraph-formal-negative-") as raw:
            root = Path(raw)
            store = root / "store"
            store.mkdir()
            request = self.request(store, execution_mode="formal")
            completed = self.invoke(request, root)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("formal LiveGraph P10", completed.stderr)
            self.assertFalse((root / "output" / "adapter-result.json").exists())

    def test_formal_contract_accepts_1700_query_synthetic_truth(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-livegraph-formal-contract-") as raw:
            root = Path(raw)
            store = root / "store"
            store.mkdir()
            source_rows = [line.split("\t") for line in self.truth.read_text(encoding="utf-8").splitlines()[1:]]
            formal_truth = root / "truth-1700.tsv"
            lines = ["query_index\tedge_type\tsrc\tcount\tsum_hash\txor_hash"]
            for query_index in range(1700):
                source = source_rows[query_index % len(source_rows)]
                lines.append("\t".join((str(query_index), *source[1:])))
            formal_truth.write_text("\n".join(lines) + "\n", encoding="utf-8")
            request = self.request(
                store,
                execution_mode="formal",
                truth=formal_truth,
                query_count=1700,
            )
            completed = self.invoke(request, root)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(
                (root / "output" / "adapter-result.json").read_text(encoding="utf-8")
            )
            self.assertEqual(result["process_lifetime"], FRESH_IMPORT_PROCESS_LIFETIME)
            self.assertEqual(result["measured"]["requested_queries"], 3400)
            self.assertEqual(result["measured"]["mismatch_queries"], 0)

    def test_runtime_library_sha_mismatch_fails_before_store_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-livegraph-lib-sha-negative-") as raw:
            root = Path(raw)
            store = root / "store"
            store.mkdir()
            request = self.request(store)
            request["runtime_libraries"][0]["sha256"] = "0" * 64
            completed = self.invoke(request, root)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("SHA-256 mismatch", completed.stderr)
            self.assertEqual(list(store.iterdir()), [])

    def test_binary_sha_mismatch_fails_before_store_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-livegraph-sha-negative-") as raw:
            root = Path(raw)
            store = root / "store"
            store.mkdir()
            request = self.request(store)
            request["binary"]["sha256"] = "0" * 64
            completed = self.invoke(request, root)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("SHA-256 mismatch", completed.stderr)
            self.assertEqual(list(store.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
