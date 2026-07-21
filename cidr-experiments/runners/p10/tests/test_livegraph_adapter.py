#!/usr/bin/env python3
"""Real LiveGraph fixture smoke for the P10 adapter (never performance data)."""

from __future__ import annotations

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

    def request(self, store: Path, *, execution_mode: str = "fixture") -> dict:
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
            "binary": {"path": str(self.binary), "sha256": sha256_file(self.binary)},
            "dataset": {"path": str(self.dataset), "sha256": sha256_file(self.dataset)},
            "store_roots": [
                {
                    "label": "livegraph",
                    "path": str(store),
                    "sha256": "" if execution_mode == "fixture" else "0" * 64,
                }
            ],
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
            self.assertTrue((store / "livegraph-block").is_file())
            self.assertTrue((store / "livegraph-wal").is_file())

    def test_worker_declares_non_reopenable_store(self) -> None:
        completed = subprocess.run(
            [str(self.binary), "--capabilities"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        capability = json.loads(completed.stdout)
        self.assertEqual(capability["store_capability"], "import-only-process-lifetime-v1")

    def test_orchestrator_and_p31_bridge_execute_real_adapter(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-livegraph-orchestrated-") as raw:
            root = Path(raw)
            store = root / "store"
            store.mkdir()
            manifest = json.loads(self.fixture_manifest.read_text(encoding="utf-8"))
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
            livegraph["store_roots"] = [
                {"label": "livegraph", "path": str(store), "sha256": ""}
            ]
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
