#!/usr/bin/env python3
"""Fail-closed tests for the formal P10 consumer of one P31 run."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

P10_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(P10_DIR))

from p10_contract import ContractError, read_p31_summary, sha256_file  # noqa: E402


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def artifact(path: Path) -> dict[str, object]:
    return {
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def build_formal_run(run_dir: Path) -> None:
    run_dir.mkdir()
    container = "neo4j-cidr-r01"
    identity = {
        "container_id": "a" * 64,
        "pid": 4242,
        "process_start_ticks": 876543,
        "started_at": "2026-07-22T00:00:00Z",
        "restart_count": 0,
    }
    ready = {
        "schema_version": "cidr-container-identity-v2",
        "state": "READY",
        "containers": {container: identity},
    }
    history_item = {
        **identity,
        "before_sample_index": 0,
        "observed_at_utc": "2026-07-22T00:00:01Z",
    }
    status = {
        "schema_version": "cidr-resource-v1",
        "state": "DONE",
        "errors": [],
        "container_identity_schema_version": "cidr-container-identity-v2",
        "collector_ready": ready,
        "containers_seen": {container: 4242},
        "container_identity_history": {container: [history_item]},
        "container_identity_unique_set": {container: [identity]},
    }
    status_path = run_dir / "collector-status.json"
    ready_path = run_dir / "collector-ready.json"
    write_json(status_path, status)
    write_json(ready_path, ready)

    resources = {
        "peak_rss_bytes": 1,
        "peak_pss_bytes": 1,
        "process_user_cpu_s": 0.1,
        "process_sys_cpu_s": 0.2,
        "process_read_bytes": 2,
        "process_write_bytes": 3,
    }
    disk = {"peak_store_total_bytes": 4, "peak_temp_bytes": 5}
    validated_at = "2026-07-22T00:00:02Z"
    validation = {
        "schema_version": "cidr-run-manifest-v1",
        "state": "PASS",
        "validated_at_utc": validated_at,
        "errors": [],
        "warnings": [],
        "resource_summary": resources,
        "disk_summary": disk,
        "iostat_samples": 2,
    }
    collector_result = {
        "container_identity_schema_version": "cidr-container-identity-v2",
        "ready": ready,
        "containers_seen": status["containers_seen"],
        "container_identity_history": status["container_identity_history"],
        "container_identity_unique_set": status["container_identity_unique_set"],
        "status_artifact": artifact(status_path),
        "ready_artifact": artifact(ready_path),
    }
    manifest = {
        "schema_version": "cidr-run-manifest-v1",
        "state": "PASS",
        "performance_eligible_declared": True,
        "summary": {"resources": resources, "disk": disk},
        "validation": validation,
        "collector": {"containers": [container]},
        "collector_result": collector_result,
        "repo": {},
        "host": {},
        "harness": {},
        "inputs": {},
        "disk_roots": [],
    }
    validation_path = run_dir / "validation.json"
    manifest_path = run_dir / "run-manifest.json"
    write_json(validation_path, validation)
    write_json(manifest_path, manifest)
    write_json(
        run_dir / "DONE",
        {
            "state": "PASS",
            "validated_at_utc": validated_at,
            "manifest_sha256": sha256_file(manifest_path),
            "validation_sha256": sha256_file(validation_path),
        },
    )


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def refresh_done_manifest_sha(run_dir: Path) -> None:
    done_path = run_dir / "DONE"
    done = load_json(done_path)
    done["manifest_sha256"] = sha256_file(run_dir / "run-manifest.json")
    write_json(done_path, done)


class FormalP31ConsumerTests(unittest.TestCase):
    def with_run(self, mutation: Callable[[Path], None]) -> None:
        with tempfile.TemporaryDirectory(prefix="p31-consumer-") as raw:
            run_dir = Path(raw) / "run"
            build_formal_run(run_dir)
            mutation(run_dir)
            with self.assertRaises(ContractError):
                read_p31_summary(run_dir, performance_eligible=True)

    def test_accepts_and_returns_closed_manifest_and_validation_hashes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p31-consumer-") as raw:
            run_dir = Path(raw) / "run"
            build_formal_run(run_dir)
            summary = read_p31_summary(run_dir, performance_eligible=True)
            self.assertEqual(summary["manifest_sha256"], sha256_file(run_dir / "run-manifest.json"))
            self.assertEqual(summary["validation_sha256"], sha256_file(run_dir / "validation.json"))

    def test_rejects_any_tampered_closed_file(self) -> None:
        def append(name: str) -> Callable[[Path], None]:
            def mutate(run_dir: Path) -> None:
                with (run_dir / name).open("a", encoding="utf-8") as handle:
                    handle.write(" \n")

            return mutate

        for name in (
            "run-manifest.json",
            "validation.json",
            "collector-status.json",
            "collector-ready.json",
        ):
            with self.subTest(name=name):
                self.with_run(append(name))

    def test_rejects_done_hash_tamper(self) -> None:
        for field in ("manifest_sha256", "validation_sha256"):
            def mutate(run_dir: Path, field: str = field) -> None:
                done_path = run_dir / "DONE"
                done = load_json(done_path)
                done[field] = "0" * 64
                write_json(done_path, done)

            with self.subTest(field=field):
                self.with_run(mutate)

    def test_rejects_embedded_validation_tamper_even_when_done_is_resigned(self) -> None:
        def mutate(run_dir: Path) -> None:
            manifest_path = run_dir / "run-manifest.json"
            manifest = load_json(manifest_path)
            manifest["validation"]["iostat_samples"] = 99
            write_json(manifest_path, manifest)
            refresh_done_manifest_sha(run_dir)

        self.with_run(mutate)

    def test_rejects_collector_reference_path_size_or_sha_tamper(self) -> None:
        cases: tuple[tuple[str, object], ...] = (
            ("path", "/tmp/not-the-p31-status.json"),
            ("size_bytes", 999999),
            ("sha256", "0" * 64),
        )
        for field, value in cases:
            def mutate(run_dir: Path, field: str = field, value: object = value) -> None:
                manifest_path = run_dir / "run-manifest.json"
                manifest = load_json(manifest_path)
                manifest["collector_result"]["status_artifact"][field] = value
                write_json(manifest_path, manifest)
                refresh_done_manifest_sha(run_dir)

            with self.subTest(field=field):
                self.with_run(mutate)


if __name__ == "__main__":
    unittest.main()
