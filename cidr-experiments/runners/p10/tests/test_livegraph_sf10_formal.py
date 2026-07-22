#!/usr/bin/env python3
"""Static/unit checks for the correctness-gated LiveGraph SF10 launcher."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


P10_DIR = Path(__file__).resolve().parents[1]
LIVEGRAPH_DIR = P10_DIR / "adapters/livegraph"
WRAPPER = LIVEGRAPH_DIR / "run_sf10_formal.py"
TEMPLATE = LIVEGRAPH_DIR / "formal-system.template.json"

SPEC = importlib.util.spec_from_file_location("livegraph_sf10_formal", WRAPPER)
assert SPEC is not None and SPEC.loader is not None
formal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(formal)


def canonical_protocol() -> dict:
    return {
        "group_policy": "report-separately-no-cross-group-speedups",
        "interface_scope": formal.INTERFACE_SCOPE,
        "timing_boundary": formal.TIMING_BOUNDARY,
        "clock": formal.CLOCK_NAME,
        "cache_policy": "full-shared-trace-before-each-measured-repeat-v1",
        "process_reuse_between_phases": True,
        "warmup_passes": 1,
        "measured_passes": 1,
        "repeats": 3,
        "per_query_timeout_ms": 60_000,
        "adapter_process_timeout_s": 7_200,
        "concurrency": 1,
        "max_timeouts": 0,
    }


class LiveGraphSf10FormalTest(unittest.TestCase):
    @staticmethod
    def stage_fixture() -> tuple[list[dict], dict, dict, dict]:
        base = {
            "schema_version": "p10-livegraph-adapter-stage-v1",
            "at_utc": "2026-07-22T00:00:00.000Z",
        }
        rows = [
            {
                **base, "sequence": 1, "stage": "preflight-passed", "monotonic_ns": 1,
                "execution_mode": "formal", "store_root": "/store", "temp_root": "/temp",
            },
            {
                **base, "sequence": 2, "stage": "worker-started", "monotonic_ns": 2,
                "pid": 123, "proc_start_ticks": 456,
            },
            {
                **base, "sequence": 3, "stage": "worker-exited", "monotonic_ns": 3,
                "pid": 123, "returncode": 0,
            },
            {**base, "sequence": 4, "stage": "worker-output-validated", "monotonic_ns": 4},
            {**base, "sequence": 5, "stage": "adapter-result-published", "monotonic_ns": 5},
            {**base, "sequence": 6, "stage": "adapter-complete", "monotonic_ns": 6},
        ]
        provenance = {
            "execution_mode": "formal",
            "store": {"root": "/store"},
            "temp": {"root": "/temp"},
        }
        start = {"pid": 123, "proc_start_ticks": 456}
        exit_receipt = {"pid": 123, "returncode": 0}
        return rows, provenance, start, exit_receipt

    def test_stage_journal_has_exact_schema_and_receipt_bindings(self) -> None:
        rows, provenance, start, exit_receipt = self.stage_fixture()
        formal.validate_stage_journal(rows, provenance, start, exit_receipt)
        mutations = (
            lambda value: value[0].__setitem__("schema_version", "drifted"),
            lambda value: value[3].__setitem__("extra", True),
            lambda value: value[1].__setitem__("sequence", True),
            lambda value: value[2].__setitem__("monotonic_ns", 2),
            lambda value: value[1].__setitem__("pid", True),
            lambda value: value[1].__setitem__("pid", 999),
        )
        for mutate in mutations:
            drifted = json.loads(json.dumps(rows))
            mutate(drifted)
            with self.subTest(mutation=mutate), self.assertRaises(formal.ReadinessError):
                formal.validate_stage_journal(drifted, provenance, start, exit_receipt)

    def test_evidence_claim_loser_never_publishes_failed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="livegraph-evidence-claim-") as raw:
            evidence = Path(raw) / "evidence"
            barrier = threading.Barrier(2)

            def attempt() -> bool:
                barrier.wait(timeout=5)
                try:
                    formal.claim_evidence_directory(evidence)
                except formal.ReadinessError:
                    return False
                return True

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(lambda _: attempt(), range(2)))
            self.assertEqual(sum(outcomes), 1)
            self.assertEqual(list(evidence.iterdir()), [])
            formal.publish_failure_if_owned(
                evidence, False, formal.ReadinessError("concurrent loser")
            )
            self.assertFalse((evidence / "FAILED").exists())
            formal.publish_failure_if_owned(
                evidence, True, formal.ReadinessError("owner failure")
            )
            failure = json.loads((evidence / "FAILED").read_text(encoding="utf-8"))
            self.assertEqual(failure["state"], "FAILED")
            self.assertEqual(failure["error"], "owner failure")

    def test_manifest_snapshot_preserves_exact_bytes_and_source_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="livegraph-manifest-snapshot-") as raw:
            root = Path(raw)
            source = root / "suite.json"
            destination = root / "evidence" / "input-suite-manifest.json"
            destination.parent.mkdir()
            original = b'{"path":"${MANIFEST_DIR}/dense.bin"}\n'
            source.write_bytes(original)
            snapshot = formal.snapshot_manifest(source, destination)
            self.assertEqual(destination.read_bytes(), original)
            self.assertEqual(snapshot["snapshot"], formal.file_ref(destination))
            self.assertEqual(snapshot["source_at_snapshot"]["path"], str(source.resolve()))
            self.assertEqual(
                snapshot["source_at_snapshot"]["sha256"], formal.sha256_file(destination)
            )
            source.write_bytes(b'{"changed":true}\n')
            self.assertEqual(destination.read_bytes(), original)

    def test_early_preflight_failure_publishes_exclusive_failed_marker(self) -> None:
        with tempfile.TemporaryDirectory(prefix="livegraph-early-failed-") as raw:
            root = Path(raw)
            evidence = root / "evidence"
            completed = subprocess.run(
                [
                    sys.executable, "-B", str(WRAPPER),
                    "--manifest", str(root / "missing.json"),
                    "--run-root", str(root / "run"),
                    "--evidence-dir", str(evidence),
                    "--preflight-only",
                ],
                text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertTrue((evidence / "FAILED").is_file())
            self.assertFalse((evidence / "DONE.preflight").exists())
            self.assertFalse((evidence / "DONE.formal").exists())

    def test_preexisting_evidence_is_never_mutated_by_nonowner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="livegraph-evidence-nonowner-") as raw:
            root = Path(raw)
            evidence = root / "evidence"
            evidence.mkdir()
            sentinel = evidence / "owner-sentinel"
            sentinel.write_bytes(b"preserve owner evidence\n")
            completed = subprocess.run(
                [
                    sys.executable, "-B", str(WRAPPER),
                    "--manifest", str(root / "missing.json"),
                    "--run-root", str(root / "run"),
                    "--evidence-dir", str(evidence),
                    "--preflight-only",
                ],
                text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(sentinel.read_bytes(), b"preserve owner evidence\n")
            self.assertEqual({path.name for path in evidence.iterdir()}, {"owner-sentinel"})
            self.assertFalse((evidence / "FAILED").exists())

    def test_p02b_admission_is_bound_to_repo_head_and_binary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="livegraph-p02b-identity-") as raw:
            root = Path(raw)
            result = root / "sentinel-result.json"
            validator = root / "validator.py"
            marker = root / "PASS"
            provenance = root / "provenance.json"
            for path, payload in (
                (result, b"result"), (validator, b"validator"),
                (marker, b"pass"), (provenance, b"provenance"),
            ):
                path.write_bytes(payload)
            expected_head = "1" * 40
            expected_binary = "2" * 64
            host = {"hostname": "host", "fingerprint_sha256": "3" * 64}
            admission = {
                "state": "PASS", "consumer": "P10", "formal_required": True,
                "fixture_only": False, "scope": "host-global",
                "sentinel_result": str(result), "sentinel_result_sha256": formal.sha256_file(result),
                "pass_marker": str(marker), "pass_marker_sha256": formal.sha256_file(marker),
                "provenance": str(provenance), "provenance_sha256": formal.sha256_file(provenance),
                "scale": "sf10", "run_id": "sentinel", "completed_at_utc": "2026-07-22T00:00:00Z",
                "repo_root": str(root), "repo_head": expected_head,
                "binary_sha256": expected_binary, "host": host, "protocol": {},
            }
            adapter_args = {
                "--p02b-result": str(result),
                "--p02b-result-sha256": formal.sha256_file(result),
                "--p02b-validator": str(validator),
                "--p02b-validator-sha256": formal.sha256_file(validator),
            }

            def invoke(payload: dict) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess([], 0, json.dumps(payload), "")

            with mock.patch.object(formal, "current_p31_host", return_value=host), mock.patch.object(
                formal.subprocess, "run", side_effect=lambda command, **kwargs: invoke(admission)
            ) as called:
                formal.validate_p02b(
                    adapter_args,
                    expected_repo_root=root,
                    expected_repo_head=expected_head,
                    expected_binary_sha256=expected_binary,
                )
                command = called.call_args.args[0]
                self.assertEqual(command[command.index("--expected-repo-head") + 1], expected_head)
                self.assertEqual(command[command.index("--expected-binary-sha256") + 1], expected_binary)

            drifted = {**admission, "repo_head": "4" * 40}
            with mock.patch.object(formal, "current_p31_host", return_value=host), mock.patch.object(
                formal.subprocess, "run", return_value=invoke(drifted)
            ), self.assertRaises(formal.ReadinessError):
                formal.validate_p02b(
                    adapter_args,
                    expected_repo_root=root,
                    expected_repo_head=expected_head,
                    expected_binary_sha256=expected_binary,
                )

    def test_formal_template_freezes_fresh_lifecycle_and_receipt_gates(self) -> None:
        value = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        self.assertEqual(value["id"], "livegraph")
        self.assertFalse(value["fixture_only"])
        self.assertEqual(value["group"], "embedded")
        self.assertEqual(value["service_lifecycle"], "in-process")
        self.assertEqual(value["process_lifetime"], formal.PROCESS_LIFETIME)
        self.assertEqual(len(value["runtime_libraries"]), 1)
        self.assertEqual(value["runtime_libraries"][0]["path"], "/ABS/P10-LIVEGRAPH-BUILD/lib/liblivegraph.so")
        self.assertEqual(value["store_roots"][0]["sha256"], "")
        self.assertEqual(value["temp_roots"][0]["label"], "scratch")
        args = formal.parse_adapter_args(value["adapter"]["args"])
        self.assertIn("--build-receipt", args)
        self.assertIn("--p02b-result", args)
        self.assertEqual(args["--p02b-max-age-seconds"], str(formal.P02B_MAX_AGE_SECONDS))

    def test_protocol_requires_exactly_three_independent_one_plus_one_repeats(self) -> None:
        protocol = canonical_protocol()
        formal.validate_protocol({"protocol": protocol})
        for key, invalid in (("repeats", 2), ("warmup_passes", 2), ("measured_passes", 2)):
            drifted = dict(protocol)
            drifted[key] = invalid
            with self.subTest(key=key), self.assertRaises(formal.ReadinessError):
                formal.validate_protocol({"protocol": drifted})

    def test_adapter_args_reject_duplicates_unknowns_and_age_drift(self) -> None:
        template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        args = template["adapter"]["args"]
        with self.assertRaises(formal.ReadinessError):
            formal.parse_adapter_args([*args, "--p02b-result", "/another/result.json"])
        with self.assertRaises(formal.ReadinessError):
            formal.parse_adapter_args([*args, "--unknown", "value"])
        drifted = list(args)
        drifted[drifted.index("--p02b-max-age-seconds") + 1] = "99999"
        with self.assertRaises(formal.ReadinessError):
            formal.parse_adapter_args(drifted)

    def test_duplicate_json_keys_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="livegraph-formal-json-") as raw:
            path = Path(raw) / "duplicate.json"
            path.write_text('{"state":"PASS","state":"FAILED"}\n', encoding="utf-8")
            with self.assertRaises(formal.ReadinessError):
                formal.read_object(path, "duplicate fixture")

    def test_build_sha_manifest_requires_complete_coverage(self) -> None:
        with tempfile.TemporaryDirectory(prefix="livegraph-formal-sums-") as raw:
            root = Path(raw)
            payload = root / "payload.bin"
            payload.write_bytes(b"bound")
            manifest = root / "SHA256SUMS"
            manifest.write_text(
                f"{formal.sha256_file(payload)}  payload.bin\n", encoding="utf-8"
            )
            formal.validate_sha_manifest(root, manifest)
            (root / "unlisted.bin").write_bytes(b"unbound")
            with self.assertRaises(formal.ReadinessError):
                formal.validate_sha_manifest(root, manifest)


if __name__ == "__main__":
    unittest.main()
