#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

P10_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = P10_DIR.parents[2]
ADAPTER = P10_DIR / "adapters" / "seml0_adapter.py"
FIXTURE_BINARY = Path(__file__).resolve().parent / "fixture_seml0_binary.py"
P02B_DIR = P10_DIR.parent / "p02b"
P02B_PREPARE = P02B_DIR / "tests" / "prepare_fixture.py"
P02B_RUNNER = P02B_DIR / "run_sf10_sentinel.py"
P02B_VALIDATOR = P02B_DIR / "validate_sentinel_result.py"
P31_WRAPPER = P10_DIR.parent / "p31" / "run_with_resources.sh"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class SemL0AdapterTests(unittest.TestCase):
    maxDiff = None

    def invoke(
        self,
        command: list[str],
        *,
        expected: int = 0,
        timeout: int = 180,
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            env=environment,
            timeout=timeout,
        )
        self.assertEqual(
            completed.returncode,
            expected,
            f"command: {command!r}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        return completed

    def prepare(self, root: Path) -> list[str]:
        # Reuse the P02B fixture producer and the real sentinel runner.  This
        # intentionally creates the complete canonical result, provenance,
        # three P31 repeats, and FIXTURE-PASS marker consumed below.
        self.invoke(
            [
                sys.executable,
                "-B",
                str(P02B_PREPARE),
                "--root",
                str(root),
                "--repo-root",
                str(REPO_ROOT),
            ]
        )
        truth = root / "truth.tsv"
        id_map = root / "id-map"
        store = root / "store"
        plan = root / "query-plan.json"
        self.invoke(
            [
                str(FIXTURE_BINARY),
                "--io-backend",
                "blocking",
                "shared-truth-verify",
                "--data-dir",
                str(store),
                "--truth-tsv",
                str(truth),
                "--id-map-dir",
                str(id_map),
                "--expected-queries",
                "6",
                "--l0-layout",
                "schema",
                "--semantic-degree-hint",
                "--sample-plan-out",
                str(plan),
                "--output",
                str(root / "preflight.json"),
            ]
        )
        p02b_run = root / "p02b-run"
        self.invoke(
            [
                sys.executable,
                "-B",
                str(P02B_RUNNER),
                "--run-dir",
                str(p02b_run),
                "--clean-ready",
                str(root / "P03-CLEAN-WINDOW-MONITOR" / "raw" / "fixture-clean" / "READY"),
                "--repo-root",
                str(REPO_ROOT),
                "--binary",
                str(FIXTURE_BINARY),
                "--dataset-manifest",
                str(root / "dataset-manifest.json"),
                "--store",
                str(store),
                "--store-manifest",
                str(root / "store-manifest.json"),
                "--truth",
                str(truth),
                "--query-plan",
                str(plan),
                "--id-map-dir",
                str(id_map),
                "--config",
                str(root / "config.json"),
            ]
        )
        self.assertTrue((p02b_run / "FIXTURE-PASS").is_file())
        self.assertTrue((p02b_run / "provenance.json").is_file())
        self.assertFalse((p02b_run / "FAILED").exists())

        store_manifest = root / "store-manifest.json"
        id_manifest = id_map / "id-map-manifest.json"
        dataset_manifest = load_json(root / "dataset-manifest.json")
        store_lineage = load_json(store_manifest)
        request = root / "request.json"
        write_json(
            request,
            {
                "schema_version": "cidr-p10-adapter-request-v1",
                "contract_version": "cidr-typed-neighbor-adapter-v1",
                "suite_id": "fixture",
                "run_id": "fixture",
                "execution_mode": "fixture",
                "system_id": "seml0",
                "group": "embedded",
                "system_version": "fixture-seml0",
                "interface_scope": "typed-neighbor-dense-id-v1",
                "repeat_index": 1,
                "binary": {"path": str(FIXTURE_BINARY), "sha256": sha256(FIXTURE_BINARY)},
                "dataset": {
                    "path": str((root / "dataset").resolve()),
                    "sha256": dataset_manifest["dataset_sha256"],
                },
                "store_roots": [
                    {
                        "label": "store",
                        "path": str(store.resolve()),
                        "sha256": store_lineage["store_sha256"],
                    }
                ],
                "truth": {
                    "path": str(truth.resolve()),
                    "sha256": sha256(truth),
                    "query_count": 6,
                    "digest_algorithm": "mix64-dense-dst-count-sum-xor-v1",
                },
                "timing": {
                    "timing_boundary": "typed-neighbor-call-plus-result-materialization-and-digest-v1",
                    "clock": "CLOCK_MONOTONIC",
                    "cache_policy": "full-shared-trace-before-each-measured-repeat-v1",
                    "process_reuse_between_phases": True,
                    "warmup_passes": 1,
                    "measured_passes": 1,
                    "concurrency": 1,
                    "per_query_timeout_ms": 1000,
                    "sequence_digest_algorithm": "sha256-pass-query-count-sum-xor-v1",
                },
            },
        )
        p02b_result = p02b_run / "sentinel-result.json"
        output = root / "output"
        return [
            sys.executable,
            "-B",
            str(ADAPTER),
            "--mode",
            "fixture",
            "--variant",
            "schema",
            "--binary",
            str(FIXTURE_BINARY),
            "--binary-sha256",
            sha256(FIXTURE_BINARY),
            "--data-dir",
            str(store),
            "--store-manifest",
            str(store_manifest),
            "--store-manifest-sha256",
            sha256(store_manifest),
            "--store-tree-sha256",
            store_lineage["store_sha256"],
            "--sample-plan",
            str(plan),
            "--sample-plan-sha256",
            sha256(plan),
            "--truth",
            str(truth),
            "--truth-sha256",
            sha256(truth),
            "--id-map-dir",
            str(id_map),
            "--id-map-manifest-sha256",
            sha256(id_manifest),
            "--p02b-result",
            str(p02b_result),
            "--p02b-result-sha256",
            sha256(p02b_result),
            "--p02b-validator",
            str(P02B_VALIDATOR),
            "--p02b-validator-sha256",
            sha256(P02B_VALIDATOR),
            "--p31-wrapper",
            str(P31_WRAPPER),
            "--p31-wrapper-sha256",
            sha256(P31_WRAPPER),
            "--repo-root",
            str(REPO_ROOT),
            "--request",
            str(request),
            "--output-dir",
            str(output),
        ]

    def test_fixture_adapter_consumes_canonical_p02b_receipt_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-seml0-adapter-") as temporary:
            root = Path(temporary)
            command = self.prepare(root)
            self.invoke(command)
            output = root / "output"
            self.assertEqual((output / "fixture-invocations.txt").read_text(encoding="utf-8"), "1")
            self.assertTrue((output / "adapter-result.json").is_file())
            self.assertTrue((output / "query-observations.tsv").is_file())
            self.assertTrue((output / "phase-events.jsonl").is_file())
            provenance = load_json(output / "adapter-provenance.json")
            self.assertEqual(provenance["command"]["invocations"], 1)
            self.assertEqual(provenance["variant"], "schema")
            receipt = provenance["p02b"]["admission"]
            self.assertEqual(receipt["state"], "PASS")
            self.assertEqual(receipt["consumer"], "P10")
            self.assertEqual(receipt["sentinel_result_sha256"], provenance["p02b"]["result"]["sha256"])
            self.assertEqual(receipt["pass_marker_sha256"], provenance["p02b"]["pass_marker"]["sha256"])
            self.assertEqual(receipt["provenance_sha256"], provenance["p02b"]["provenance"]["sha256"])

    def test_canonical_p02b_lineage_tamper_fails_before_binary_launch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-seml0-lineage-") as temporary:
            root = Path(temporary)
            command = self.prepare(root)
            result_path = root / "p02b-run" / "sentinel-result.json"
            result = load_json(result_path)
            result["provenance"]["query_plan_sha256"] = "f" * 64
            write_json(result_path, result)
            marker_path = root / "p02b-run" / "FIXTURE-PASS"
            marker = load_json(marker_path)
            marker["result_sha256"] = sha256(result_path)
            write_json(marker_path, marker)
            command[command.index("--p02b-result-sha256") + 1] = sha256(result_path)
            completed = self.invoke(command, expected=2)
            self.assertIn("P02B admission failed", completed.stderr)
            self.assertFalse((root / "output" / "fixture-invocations.txt").exists())


if __name__ == "__main__":
    unittest.main()
