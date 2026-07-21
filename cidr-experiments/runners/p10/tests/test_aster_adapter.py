#!/usr/bin/env python3
"""Real Aster/RocksGraph tiny correctness fixtures for the P10 adapter."""

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
    ContractError,
    read_truth,
    sha256_file,
    validate_adapter_p31_binding,
    validate_adapter_outputs,
)


class AsterAdapterTest(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        binary = os.environ.get("ASTER_P10_BINARY")
        source = os.environ.get("ASTER_SOURCE_ROOT")
        if not binary or not source:
            raise unittest.SkipTest("ASTER_P10_BINARY and ASTER_SOURCE_ROOT are not set")
        cls.binary = Path(binary).resolve()
        cls.source = Path(source).resolve()
        if not cls.binary.is_file() or not cls.source.is_dir():
            raise unittest.SkipTest("Aster worker/source path is missing")
        cls.source_commit = subprocess.check_output(
            ["git", "-C", str(cls.source), "rev-parse", "HEAD"], text=True
        ).strip()
        cls.adapter = P10_DIR / "adapters" / "aster_adapter.py"
        cls.manifest_builder = P10_DIR / "adapters" / "build_aster_store_manifest.py"
        cls.runner = P10_DIR / "run_suite.py"
        cls.fixture_manifest = Path(__file__).resolve().parent / "fixture-suite.json"
        cls.fixture_p31 = Path(__file__).resolve().parent / "fixture_p31.sh"
        cls.dataset = REPO_ROOT / "baseline" / "shared-truth" / "fixtures" / "dense-edges.txt"
        cls.truth = REPO_ROOT / "baseline" / "shared-truth" / "fixtures" / "truth.tsv"

    def request(self, store: Path, *, mode: str, store_sha256: str = "") -> dict:
        return {
            "schema_version": REQUEST_SCHEMA_VERSION,
            "contract_version": CONTRACT_VERSION,
            "suite_id": "p10-aster-real-tiny-fixture",
            "run_id": "aster-tiny-r01",
            "execution_mode": mode,
            "system_id": "aster",
            "group": "embedded",
            "system_version": f"aster-rocksgraph@{self.source_commit}",
            "interface_scope": INTERFACE_SCOPE,
            "repeat_index": 1,
            "binary": {"path": str(self.binary), "sha256": sha256_file(self.binary)},
            "dataset": {"path": str(self.dataset), "sha256": sha256_file(self.dataset)},
            "store_roots": [{"label": "aster", "path": str(store), "sha256": store_sha256}],
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

    def invoke(
        self,
        root: Path,
        request: dict,
        *,
        lifecycle: str,
        output_name: str,
        store_manifest: Path | None = None,
        binary_sha256: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        request_path = root / f"request-{output_name}.json"
        request_path.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
        command = [
            sys.executable,
            "-B",
            str(self.adapter),
            "--mode",
            request["execution_mode"],
            "--lifecycle",
            lifecycle,
            "--source-root",
            str(self.source),
            "--source-commit",
            self.source_commit,
            "--repo-root",
            str(REPO_ROOT),
            "--binary",
            str(self.binary),
            "--binary-sha256",
            binary_sha256 or sha256_file(self.binary),
            "--dataset",
            str(self.dataset),
            "--dataset-sha256",
            sha256_file(self.dataset),
            "--block-cache-bytes",
            str(8 * 1024 * 1024),
            "--request",
            str(request_path),
            "--output-dir",
            str(root / output_name),
        ]
        if store_manifest is not None:
            command.extend(("--store-manifest", str(store_manifest), "--store-manifest-sha256", sha256_file(store_manifest)))
        return subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=180,
        )

    def assert_valid(self, root: Path, request: dict, output_name: str) -> dict:
        result = validate_adapter_outputs(
            output_dir=root / output_name,
            request=request,
            system={
                "id": "aster",
                "display_name": "Aster",
                "group": "embedded",
                "system_version": request["system_version"],
                "fixture_only": True,
            },
            truth_rows=read_truth(self.truth, 2),
            max_timeouts=0,
        )
        self.assertEqual(result["completed_queries"], 4)
        self.assertEqual(result["mismatch_queries"], 0)
        self.assertEqual(result["timeout_queries"], 0)
        return result

    def test_real_rocksgraph_fresh_then_frozen_reopen(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-aster-real-") as raw:
            root = Path(raw)
            store = root / "store"
            store.mkdir()
            fresh_request = self.request(store, mode="fixture")
            fresh = self.invoke(root, fresh_request, lifecycle="fresh", output_name="fresh-output")
            self.assertEqual(fresh.returncode, 0, fresh.stderr)
            self.assert_valid(root, fresh_request, "fresh-output")
            self.assertTrue((store / "p10-aster-key-map.tsv").is_file())
            self.assertTrue((store / "p10-aster-store.json").is_file())

            manifest = root / "store-manifest.json"
            built = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(self.manifest_builder),
                    "--store",
                    str(store),
                    "--output",
                    str(manifest),
                    "--source-root",
                    str(self.source),
                    "--source-commit",
                    self.source_commit,
                    "--binary",
                    str(self.binary),
                    "--dataset",
                    str(self.dataset),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=180,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            store_sha = json.loads(manifest.read_text(encoding="utf-8"))["store_sha256"]
            reopen_request = self.request(store, mode="fixture", store_sha256=store_sha)
            reopened = self.invoke(
                root,
                reopen_request,
                lifecycle="reopen",
                output_name="reopen-output",
                store_manifest=manifest,
            )
            self.assertEqual(reopened.returncode, 0, reopened.stderr)
            result = self.assert_valid(root, reopen_request, "reopen-output")
            provenance = result["adapter_provenance"]
            self.assertEqual(provenance["lifecycle"], "reopen")
            self.assertEqual(provenance["store"]["store_sha256"], store_sha)
            self.assertEqual(provenance["store"]["post_store_sha256"], store_sha)

    def test_binary_sha_mismatch_fails_before_fresh_store_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-aster-sha-") as raw:
            root = Path(raw)
            store = root / "store"
            store.mkdir()
            request = self.request(store, mode="fixture")
            request["binary"]["sha256"] = "0" * 64
            completed = self.invoke(
                root,
                request,
                lifecycle="fresh",
                output_name="output",
                binary_sha256="0" * 64,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("SHA-256 mismatch", completed.stderr)
            self.assertEqual(list(store.iterdir()), [])

    def test_orchestrator_wraps_real_aster_adapter_with_p31(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-aster-orchestrated-") as raw:
            root = Path(raw)
            store = root / "store"
            store.mkdir()
            manifest = json.loads(self.fixture_manifest.read_text(encoding="utf-8"))
            aster = next(system for system in manifest["systems"] if system["id"] == "aster")
            aster["system_version"] = f"aster-rocksgraph@{self.source_commit}"
            aster["adapter"] = {
                "path": str(self.adapter),
                "sha256": sha256_file(self.adapter),
                "args": [
                    "--mode",
                    "fixture",
                    "--lifecycle",
                    "fresh",
                    "--source-root",
                    str(self.source),
                    "--source-commit",
                    self.source_commit,
                    "--repo-root",
                    str(REPO_ROOT),
                    "--binary",
                    str(self.binary),
                    "--binary-sha256",
                    sha256_file(self.binary),
                    "--dataset",
                    str(self.dataset),
                    "--dataset-sha256",
                    sha256_file(self.dataset),
                    "--block-cache-bytes",
                    str(8 * 1024 * 1024),
                ],
            }
            aster["binary"] = {"path": str(self.binary), "sha256": sha256_file(self.binary)}
            aster["store_roots"] = [{"label": "aster", "path": str(store), "sha256": ""}]
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
                    "aster",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=180,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue((run_root / "PARTIAL-DONE").is_file())
            validated = json.loads(
                (run_root / "systems" / "aster" / "repeat-01" / "validated-result.json").read_text(encoding="utf-8")
            )
            self.assertEqual(validated["completed_queries"], 2)
            self.assertEqual(validated["mismatch_queries"], 0)
            self.assertEqual(validated["adapter_provenance"]["command"]["invocations"], 1)
            p31_argv = (run_root / "systems" / "aster" / "repeat-01" / "p31" / "argv.txt").read_text(encoding="utf-8")
            for required in (sha256_file(self.binary), sha256_file(self.dataset), sha256_file(self.truth), "--store"):
                self.assertIn(required, p31_argv)

    def test_formal_mode_requires_p02b_p31_and_frozen_store_before_launch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-aster-formal-negative-") as raw:
            root = Path(raw)
            store = root / "store"
            store.mkdir()
            request = self.request(store, mode="formal", store_sha256="0" * 64)
            completed = self.invoke(root, request, lifecycle="reopen", output_name="output")
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("missing required argv", completed.stderr)
            self.assertFalse((root / "output" / "adapter-result.json").exists())

    def test_formal_p31_cross_binding_rejects_binary_drift(self) -> None:
        store = self.dataset.parent
        wrapper = self.fixture_p31.resolve()
        provenance = {
            "binary": {"path": str(self.binary), "sha256": sha256_file(self.binary)},
            "truth": {"path": str(self.truth), "sha256": sha256_file(self.truth)},
            "p31_wrapper": {"path": str(wrapper), "sha256": sha256_file(wrapper)},
            "repo": {"head": "a" * 40},
            "store": {"path": str(store)},
        }
        p31 = {
            "harness": {"wrapper": dict(provenance["p31_wrapper"])},
            "inputs": {
                "binary": dict(provenance["binary"]),
                "truth": dict(provenance["truth"]),
                "query_or_trace": dict(provenance["truth"]),
            },
            "repo": {"git_sha": "a" * 40, "dirty": False},
            "disk_roots": [{"role": "store", "label": "aster", "path": str(store)}],
        }
        validate_adapter_p31_binding(provenance, p31, system_id="aster")
        p31["inputs"]["binary"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ContractError, "binary SHA-256"):
            validate_adapter_p31_binding(provenance, p31, system_id="aster")


if __name__ == "__main__":
    unittest.main()
