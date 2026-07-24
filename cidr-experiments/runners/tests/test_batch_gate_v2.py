#!/usr/bin/env python3
"""Offline fail-closed tests for the short clean-window/batch lease protocol."""

from __future__ import print_function

import ast
import argparse
import csv
import datetime as dt
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "batch_gate_v2.py"
SPEC = importlib.util.spec_from_file_location("batch_gate_v2", str(MODULE_PATH))
gate = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(gate)

HEAD = "a" * 40
HOST = {"hostname": "formal-host", "fingerprint_sha256": "b" * 64}
BASE = dt.datetime(2026, 7, 22, 0, 0, tzinfo=dt.timezone.utc)


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_env(path, values):
    path.write_text("".join("{}={}\n".format(key, value) for key, value in values), encoding="utf-8")


def make_p03(root, count=5, gap_seconds=60):
    p03 = root / "P03-CLEAN-WINDOW-MONITOR"
    run = p03 / "raw" / "P03-fixture"
    run.mkdir(parents=True)
    script = p03 / "monitor_clean_window.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    rows = []
    for index in range(count):
        rows.append(
            {
                "timestamp": iso(BASE + dt.timedelta(seconds=index * gap_seconds)),
                "sample": str(index + 1),
                "metric_pass": "1",
                "service_pass": "1",
                "sample_pass": "1",
                "streak": str(index + 1),
                "reasons": "none",
                "gate_mode": "seml0",
            }
        )
    columns = [
        "timestamp",
        "sample",
        "metric_pass",
        "service_pass",
        "sample_pass",
        "streak",
        "reasons",
        "gate_mode",
    ]
    for path, selected in ((run / "samples.tsv", rows), (run / "latest.tsv", rows[-1:])):
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, delimiter="\t", lineterminator="\n")
            writer.writeheader()
            writer.writerows(selected)
    ready_time = BASE + dt.timedelta(seconds=(count - 1) * gap_seconds + 1)
    write_env(
        run / "classification.env",
        [
            ("performance_eligible", "false"),
            ("purpose", "clean_window_readiness_only"),
            ("gate_mode", "seml0"),
            ("run_id", "P03-fixture"),
            ("script_sha256", gate.sha256_file(script)),
            ("git_head", HEAD),
            ("host", HOST["hostname"]),
            ("sample_interval_seconds", "60"),
            ("ready_samples", str(count)),
        ],
    )
    write_env(
        run / "STATE",
        [
            ("performance_eligible", "false"),
            ("sample_pass", "1"),
            ("streak", str(count)),
            ("required_streak", str(count)),
            ("reasons", "none"),
        ],
    )
    write_env(
        run / "READY",
        [
            ("performance_eligible", "false"),
            ("readiness_gate", "PASS"),
            ("gate_mode", "seml0"),
            ("run_id", "P03-fixture"),
            ("ready_time", iso(ready_time)),
            ("samples", str(count)),
            ("consecutive_passes", str(count)),
            ("latest_sample", str((run / "latest.tsv").resolve())),
        ],
    )
    write_env(run / "COMPLETE", [("run_id", "P03-fixture")])
    return run / "READY", ready_time


class LeaseFixture(object):
    def __init__(self, root, clean_schema="p02b-clean-ready-binding-v1", clean_samples=15):
        self.root = root
        self.repo = root / "repo"
        self.repo.mkdir()
        self.binary = root / "lsmgraph"
        self.binary.write_bytes(b"formal-binary")
        try:
            self.binary.chmod(0o755)
        except OSError:
            pass
        self.validator = root / "validate_sentinel_result.py"
        self.validator.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        self.extractor = root / "extract_run_metrics.py"
        self.extractor.write_text("# fixture extractor\n", encoding="utf-8")
        self.stability_calculator = root / "calculate_stability.py"
        self.stability_calculator.write_text(
            "# fixture stability calculator\n", encoding="utf-8"
        )
        self.stability = {
            "schema_version": "p02b-sentinel-stability-v2",
            "state": "PASS",
            "method": "quantization-aware-tail-v1",
        }
        self.stability_result = root / "stability-result.json"
        write_json(self.stability_result, self.stability)
        unsigned_contract = {
            "schema_version": gate.P02B_GATE_CONTRACT_SCHEMA,
            "method": gate.P02B_GATE_METHOD,
            "quantile": {"numerator": 99, "denominator": 100},
            "tail_bounds_us": {"lower": 150000, "upper": 250000},
            "sigma_multiplier": 3,
            "qps_cv_max": 0.07,
            "mean_storage_latency_cv_max": 0.07,
            "require_zero_overflow": True,
            "stability_result": gate.file_ref(self.stability_result),
            "tools": {
                "extract_run_metrics": gate.file_ref(self.extractor),
                "calculate_stability": gate.file_ref(self.stability_calculator),
                "validate_sentinel_result": gate.file_ref(self.validator),
            },
        }
        self.gate_contract = dict(unsigned_contract)
        self.gate_contract["contract_sha256"] = gate.canonical_sha256(
            unsigned_contract
        )
        clean = {
            "schema_version": clean_schema,
            "required_consecutive_samples": clean_samples,
            "observed_consecutive_samples": clean_samples,
            "git_head": HEAD,
            "host": HOST["hostname"],
        }
        if clean_schema == gate.P03_BINDING_SCHEMA:
            clean.update(
                {
                    "source_v1_history_preserved": False,
                    "timing": {
                        "gap_check_pass": True,
                        "expected_interval_seconds": 60,
                    },
                }
            )
        self.result = root / "sentinel-result.json"
        write_json(
            self.result,
            {
                "schema_version": gate.P02B_RESULT_SCHEMA,
                "state": "PASS",
                "fixture_only": False,
                "formal_gate_eligible": True,
                "downstream_release_eligible": True,
                "clean_ready": clean,
                "gate_contract": self.gate_contract,
                "stability": self.stability,
            },
        )
        binary_sha = gate.sha256_file(self.binary)
        result_sha = gate.sha256_file(self.result)
        self.provenance = root / "provenance.json"
        write_json(
            self.provenance,
            {
                "schema_version": "p02b-sentinel-provenance-v1",
                "fixture_mode": False,
                "repo": {
                    "root": str(self.repo.resolve()),
                    "head": HEAD,
                    "dirty": False,
                    "status_lines": [],
                },
                "files": {"binary": gate.file_ref(self.binary)},
            },
        )
        self.pass_marker = root / "PASS"
        write_json(
            self.pass_marker,
            {
                "state": "PASS",
                "fixture_only": False,
                "result": str(self.result.resolve()),
                "result_sha256": result_sha,
            },
        )
        base = {
            "state": "PASS",
            "formal_required": True,
            "fixture_only": False,
            "scope": "host-global",
            "sentinel_result": str(self.result.resolve()),
            "sentinel_result_sha256": result_sha,
            "repo_root": str(self.repo.resolve()),
            "repo_head": HEAD,
            "binary_sha256": binary_sha,
            "host": dict(HOST),
            "scale": "sf10",
            "run_id": "p02b-fixture",
            "completed_at_utc": iso(BASE),
            "protocol": {"fixture": True},
            "gate_contract": self.gate_contract,
            "gate_contract_sha256": self.gate_contract["contract_sha256"],
            "pass_marker": str(self.pass_marker.resolve()),
            "pass_marker_sha256": gate.sha256_file(self.pass_marker),
            "provenance": str(self.provenance.resolve()),
            "provenance_sha256": gate.sha256_file(self.provenance),
        }
        self.admissions = {}
        for consumer in ("P10", "P20"):
            receipt = dict(base)
            receipt["consumer"] = consumer
            self.admissions[consumer] = receipt
        self.repo_facts = {
            "root": str(self.repo.resolve()),
            "head": HEAD,
            "dirty": False,
            "status_lines": [],
        }
        self.lease = root / "batch-lease.json"

    def refresh_result_bindings(self):
        result_sha = gate.sha256_file(self.result)
        marker = json.loads(self.pass_marker.read_text(encoding="utf-8"))
        marker["result_sha256"] = result_sha
        write_json(self.pass_marker, marker)
        marker_sha = gate.sha256_file(self.pass_marker)
        for receipt in self.admissions.values():
            receipt["sentinel_result_sha256"] = result_sha
            receipt["pass_marker_sha256"] = marker_sha

    def refresh_lease_marker(self):
        marker_path = self.lease.with_name(self.lease.name + ".PASS.json")
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["lease_sha256"] = gate.sha256_file(self.lease)
        write_json(marker_path, marker)

    def issue(self, now=BASE):
        return gate.issue_lease_from_admissions(
            self.admissions,
            self.result,
            self.validator,
            self.repo,
            self.binary,
            self.lease,
            now=now,
            current_repo=dict(self.repo_facts),
            current_host=dict(HOST),
        )

    def validate(self, now=None, repo_facts=None, host=None):
        return gate.validate_lease(
            self.lease,
            "P20",
            self.repo,
            self.binary,
            now=now or BASE + dt.timedelta(hours=1),
            current_repo=repo_facts or dict(self.repo_facts),
            current_host=host or dict(HOST),
        )


def write_guard_fixture(fixture, rows):
    guard_dir = fixture.root / "guard"
    guard_dir.mkdir()
    samples = guard_dir / "integrity-samples.tsv"
    with samples.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=gate.GUARD_COLUMNS, delimiter="\t", lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)
    status = {
        "schema_version": gate.GUARD_SCHEMA,
        "state": "PASS",
        "started_at_utc": rows[0]["timestamp_utc"],
        "ended_at_utc": rows[-1]["timestamp_utc"],
        "sample_count": len(rows),
        "interval_seconds": 1.0,
        "maximum_gap_seconds": 3.0,
        "maximum_observed_gap_seconds": max(float(row["gap_s"]) for row in rows),
        "errors": [],
        "lease": gate.file_ref(fixture.lease),
        "samples": gate.file_ref(samples),
        "repo_head": HEAD,
        "binary_sha256": gate.sha256_file(fixture.binary),
    }
    write_json(guard_dir / "status.json", status)
    write_json(
        guard_dir / "READY.json",
        {
            "schema_version": gate.GUARD_SCHEMA,
            "state": "READY",
            "ready_at_utc": rows[0]["timestamp_utc"],
            "sample_index": 0,
            "lease_sha256": gate.sha256_file(fixture.lease),
            "repo_head": HEAD,
        },
    )
    return guard_dir


def clean_guard_rows():
    rows = []
    for index in range(2):
        rows.append(
            {
                "timestamp_utc": iso(BASE + dt.timedelta(seconds=index + 1)),
                "sample_index": str(index),
                "monotonic_s": "{:.6f}".format(float(index)),
                "gap_s": "0.000000" if index == 0 else "1.000000",
                "lease_remaining_s": str(gate.LEASE_DURATION_SECONDS - index - 1),
                "repo_head": HEAD,
                "repo_dirty": "0",
                "binary_size_bytes": "13",
                "binary_mtime_ns": "1",
                "contamination_count": "0",
                "contamination_pids": "",
                "sample_pass": "1",
                "reasons": "none",
            }
        )
    return rows


class P03V2Tests(unittest.TestCase):
    def test_accepts_exactly_five_one_minute_samples(self):
        with tempfile.TemporaryDirectory(prefix="p03-v2-") as raw:
            ready, ready_time = make_p03(Path(raw), count=5)
            result = gate.validate_p03_ready_v2(
                ready,
                HEAD,
                HOST["hostname"],
                now=ready_time + dt.timedelta(seconds=10),
            )
            self.assertEqual(result["schema_version"], gate.P03_BINDING_SCHEMA)
            self.assertEqual(result["required_consecutive_samples"], 5)
            self.assertTrue(result["timing"]["gap_check_pass"])

    def test_rejects_unmonitored_gap(self):
        with tempfile.TemporaryDirectory(prefix="p03-v2-gap-") as raw:
            ready, ready_time = make_p03(Path(raw), count=5, gap_seconds=80)
            with self.assertRaisesRegex(gate.GateError, "sampling gap"):
                gate.validate_p03_ready_v2(
                    ready,
                    HEAD,
                    HOST["hostname"],
                    now=ready_time + dt.timedelta(seconds=1),
                )

    def test_preserves_and_accepts_original_fifteen_sample_evidence(self):
        with tempfile.TemporaryDirectory(prefix="p03-v1-history-") as raw:
            ready, ready_time = make_p03(Path(raw), count=15)
            result = gate.validate_p03_ready_v2(
                ready,
                HEAD,
                HOST["hostname"],
                now=ready_time + dt.timedelta(seconds=1),
            )
            self.assertTrue(result["source_v1_history_preserved"])
            self.assertEqual(result["required_consecutive_samples"], 15)

    def test_rejects_head_or_host_drift(self):
        with tempfile.TemporaryDirectory(prefix="p03-v2-id-") as raw:
            ready, ready_time = make_p03(Path(raw), count=5)
            with self.assertRaisesRegex(gate.GateError, "HEAD differs"):
                gate.validate_p03_ready_v2(
                    ready,
                    "c" * 40,
                    HOST["hostname"],
                    now=ready_time,
                )


class LeaseTests(unittest.TestCase):
    def test_issue_and_validate_24_hour_lease_from_legacy_15_sample_gate(self):
        with tempfile.TemporaryDirectory(prefix="lease-v2-") as raw:
            fixture = LeaseFixture(Path(raw))
            lease = fixture.issue()
            self.assertEqual(lease["duration_seconds"], 86400)
            self.assertEqual(lease["p02b"]["clean_window"]["mode"], "legacy-15-sample-v1")
            self.assertEqual(lease["schema_version"], gate.LEASE_SCHEMA)
            self.assertEqual(lease["p02b"]["gate_contract"], fixture.gate_contract)
            self.assertEqual(
                lease["p02b"]["gate_contract_sha256"],
                fixture.gate_contract["contract_sha256"],
            )
            admission = fixture.validate()
            self.assertEqual(admission["state"], "PASS")
            self.assertEqual(
                admission["gate_contract_sha256"],
                fixture.gate_contract["contract_sha256"],
            )

    def test_issue_accepts_gap_checked_short_v2_gate(self):
        with tempfile.TemporaryDirectory(prefix="lease-short-v2-") as raw:
            fixture = LeaseFixture(Path(raw), gate.P03_BINDING_SCHEMA, 5)
            lease = fixture.issue()
            self.assertEqual(lease["p02b"]["clean_window"]["mode"], "short-5x60-v2")

    def test_rejects_short_window_disguised_as_v1(self):
        with tempfile.TemporaryDirectory(prefix="lease-short-v1-") as raw:
            fixture = LeaseFixture(Path(raw), "p02b-clean-ready-binding-v1", 5)
            with self.assertRaisesRegex(gate.GateError, "may not use the v1"):
                fixture.issue()

    def test_expiry_head_host_and_binary_changes_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="lease-drift-") as raw:
            fixture = LeaseFixture(Path(raw))
            fixture.issue()
            with self.assertRaisesRegex(gate.GateError, "expired"):
                fixture.validate(now=BASE + dt.timedelta(seconds=86400))
            changed_repo = dict(fixture.repo_facts)
            changed_repo["head"] = "c" * 40
            with self.assertRaisesRegex(gate.GateError, "HEAD/clean"):
                fixture.validate(repo_facts=changed_repo)
            with self.assertRaisesRegex(gate.GateError, "host identity"):
                fixture.validate(host={"hostname": "other", "fingerprint_sha256": "d" * 64})
            fixture.binary.write_bytes(b"changed-binary")
            with self.assertRaisesRegex(gate.GateError, "size changed"):
                fixture.validate()

    def test_p02b_admission_must_bind_host_head_and_binary(self):
        with tempfile.TemporaryDirectory(prefix="lease-bind-") as raw:
            fixture = LeaseFixture(Path(raw))
            fixture.admissions["P10"]["repo_head"] = "c" * 40
            with self.assertRaisesRegex(gate.GateError, "admission drift"):
                fixture.issue()

    def test_p02b_clean_window_must_bind_leased_head_and_host(self):
        for field, value, message in (
            ("git_head", "c" * 40, "clean_ready HEAD"),
            ("host", "other-host", "clean_ready host"),
        ):
            with self.subTest(field=field):
                with tempfile.TemporaryDirectory(prefix="lease-clean-bind-") as raw:
                    fixture = LeaseFixture(Path(raw))
                    result = json.loads(fixture.result.read_text(encoding="utf-8"))
                    result["clean_ready"][field] = value
                    write_json(fixture.result, result)
                    new_sha = gate.sha256_file(fixture.result)
                    marker = json.loads(fixture.pass_marker.read_text(encoding="utf-8"))
                    marker["result_sha256"] = new_sha
                    write_json(fixture.pass_marker, marker)
                    marker_sha = gate.sha256_file(fixture.pass_marker)
                    for receipt in fixture.admissions.values():
                        receipt["sentinel_result_sha256"] = new_sha
                        receipt["pass_marker_sha256"] = marker_sha
                    with self.assertRaisesRegex(gate.GateError, message):
                        fixture.issue()

    def test_pass_marker_tamper_invalidates_an_issued_lease(self):
        with tempfile.TemporaryDirectory(prefix="lease-marker-tamper-") as raw:
            fixture = LeaseFixture(Path(raw))
            fixture.issue()
            marker = json.loads(fixture.pass_marker.read_text(encoding="utf-8"))
            marker["result_sha256"] = "0" * 64
            write_json(fixture.pass_marker, marker)
            with self.assertRaisesRegex(gate.GateError, "pass_marker SHA-256 changed"):
                fixture.validate()

    def test_provenance_tamper_invalidates_an_issued_lease(self):
        with tempfile.TemporaryDirectory(prefix="lease-provenance-tamper-") as raw:
            fixture = LeaseFixture(Path(raw))
            fixture.issue()
            fixture.provenance.write_text(
                fixture.provenance.read_text(encoding="utf-8") + " ", encoding="utf-8"
            )
            with self.assertRaisesRegex(gate.GateError, "provenance SHA-256 changed"):
                fixture.validate()

    def test_legacy_result_v1_cannot_issue_a_new_lease(self):
        with tempfile.TemporaryDirectory(prefix="lease-result-v1-") as raw:
            fixture = LeaseFixture(Path(raw))
            result = json.loads(fixture.result.read_text(encoding="utf-8"))
            result["schema_version"] = "p02b-sf10-sentinel-result-v1"
            write_json(fixture.result, result)
            fixture.refresh_result_bindings()
            with self.assertRaisesRegex(gate.GateError, "result schema drift"):
                fixture.issue()

    def test_receipt_gate_contract_digest_tamper_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="lease-contract-digest-") as raw:
            fixture = LeaseFixture(Path(raw))
            fixture.admissions["P10"]["gate_contract_sha256"] = "0" * 64
            with self.assertRaisesRegex(
                gate.GateError, "receipt gate_contract SHA-256 drift"
            ):
                fixture.issue()

    def test_consumer_gate_contract_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="lease-contract-consumer-") as raw:
            fixture = LeaseFixture(Path(raw))
            changed = dict(fixture.gate_contract)
            changed["qps_cv_max"] = 0.06
            unsigned = dict(changed)
            unsigned.pop("contract_sha256")
            changed["contract_sha256"] = gate.canonical_sha256(unsigned)
            fixture.admissions["P20"]["gate_contract"] = changed
            fixture.admissions["P20"]["gate_contract_sha256"] = changed[
                "contract_sha256"
            ]
            with self.assertRaisesRegex(gate.GateError, "gate_contract"):
                fixture.issue()

    def test_rehashed_method_id_alias_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="lease-contract-method-id-") as raw:
            fixture = LeaseFixture(Path(raw))
            changed = dict(fixture.gate_contract)
            changed["method_id"] = changed.pop("method")
            unsigned = dict(changed)
            unsigned.pop("contract_sha256")
            changed["contract_sha256"] = gate.canonical_sha256(unsigned)
            fixture.admissions["P10"]["gate_contract"] = changed
            fixture.admissions["P10"]["gate_contract_sha256"] = changed[
                "contract_sha256"
            ]
            with self.assertRaisesRegex(gate.GateError, "gate_contract keys drift"):
                fixture.issue()

    def test_lease_gate_contract_tamper_fails_after_marker_reseal(self):
        with tempfile.TemporaryDirectory(prefix="lease-contract-lease-") as raw:
            fixture = LeaseFixture(Path(raw))
            fixture.issue()
            lease = json.loads(fixture.lease.read_text(encoding="utf-8"))
            lease["p02b"]["gate_contract_sha256"] = "0" * 64
            write_json(fixture.lease, lease)
            fixture.refresh_lease_marker()
            with self.assertRaisesRegex(gate.GateError, "gate_contract SHA-256 drift"):
                fixture.validate()

    def test_stability_artifact_tamper_invalidates_issued_lease(self):
        with tempfile.TemporaryDirectory(prefix="lease-stability-tamper-") as raw:
            fixture = LeaseFixture(Path(raw))
            fixture.issue()
            fixture.stability_result.write_text(
                fixture.stability_result.read_text(encoding="utf-8") + " ",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(gate.GateError, "stability result size changed"):
                fixture.validate()

    def test_gate_tool_tamper_invalidates_issued_lease(self):
        with tempfile.TemporaryDirectory(prefix="lease-tool-tamper-") as raw:
            fixture = LeaseFixture(Path(raw))
            fixture.issue()
            fixture.stability_calculator.write_text(
                fixture.stability_calculator.read_text(encoding="utf-8") + " ",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                gate.GateError, "calculate_stability size changed"
            ):
                fixture.validate()


class GuardEvidenceTests(unittest.TestCase):
    def test_clean_continuous_repeat_is_accepted(self):
        with tempfile.TemporaryDirectory(prefix="guard-pass-") as raw:
            fixture = LeaseFixture(Path(raw))
            fixture.issue()
            rows = clean_guard_rows()
            identity = json.loads(fixture.lease.read_text(encoding="utf-8"))["identity"]["binary"]
            for row in rows:
                row["binary_size_bytes"] = str(identity["size_bytes"])
                row["binary_mtime_ns"] = str(identity["mtime_ns"])
            guard_dir = write_guard_fixture(fixture, rows)
            result = gate.validate_guard_evidence(guard_dir, fixture.lease, HEAD)
            self.assertEqual(result["state"], "PASS")
            self.assertEqual(result["sample_count"], 2)

    def test_gap_contamination_and_head_change_fail_closed(self):
        mutations = [
            ("gap", lambda rows: rows[1].update({"monotonic_s": "4.0", "gap_s": "4.0"})),
            (
                "contamination",
                lambda rows: rows[1].update(
                    {
                        "contamination_count": "1",
                        "contamination_pids": "123",
                        "sample_pass": "0",
                        "reasons": "external_contamination",
                    }
                ),
            ),
            ("HEAD", lambda rows: rows[1].update({"repo_head": "c" * 40})),
        ]
        for label, mutate in mutations:
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory(prefix="guard-fail-") as raw:
                    fixture = LeaseFixture(Path(raw))
                    fixture.issue()
                    rows = clean_guard_rows()
                    identity = json.loads(fixture.lease.read_text(encoding="utf-8"))["identity"]["binary"]
                    for row in rows:
                        row["binary_size_bytes"] = str(identity["size_bytes"])
                        row["binary_mtime_ns"] = str(identity["mtime_ns"])
                    mutate(rows)
                    guard_dir = write_guard_fixture(fixture, rows)
                    with self.assertRaises(gate.GateError):
                        gate.validate_guard_evidence(guard_dir, fixture.lease, HEAD)

    def test_binary_stat_tamper_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="guard-binary-") as raw:
            fixture = LeaseFixture(Path(raw))
            fixture.issue()
            rows = clean_guard_rows()
            identity = json.loads(fixture.lease.read_text(encoding="utf-8"))["identity"]["binary"]
            for row in rows:
                row["binary_size_bytes"] = str(identity["size_bytes"])
                row["binary_mtime_ns"] = str(identity["mtime_ns"])
            rows[1]["binary_size_bytes"] = str(identity["size_bytes"] + 1)
            guard_dir = write_guard_fixture(fixture, rows)
            with self.assertRaises(gate.GateError):
                gate.validate_guard_evidence(guard_dir, fixture.lease, HEAD)


class GuardRuntimeBoundaryTests(unittest.TestCase):
    def test_stop_created_during_scan_gets_a_post_stop_final_sample(self):
        with tempfile.TemporaryDirectory(prefix="guard-stop-boundary-") as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            binary = root / "lsmgraph"
            binary.write_bytes(b"formal-binary")
            binary_stat = binary.stat()
            stop_file = root / "COLLECTOR_STOP"
            lease_path = root / "batch-lease.json"
            expected_binary = {
                "path": str(binary.resolve()),
                "size_bytes": binary_stat.st_size,
                "mtime_ns": binary_stat.st_mtime_ns,
                "sha256": gate.sha256_file(binary),
            }
            write_json(
                lease_path,
                {
                    "expires_at_utc": iso(
                        dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
                    ),
                    "identity": {"binary": expected_binary},
                },
            )
            output_dir = root / "guard"
            args = argparse.Namespace(
                output_dir=output_dir,
                lease=lease_path,
                consumer="P20",
                repo_root=repo,
                binary=binary,
                root_pid=os.getpid(),
                allowed_pid=[],
                allowed_container=[],
                max_gap_seconds=3.0,
                terminate_pgid_on_failure=False,
                stop_file=stop_file,
                interval_seconds=0.001,
            )
            calls = []
            stop_created_at = []

            def fake_git_facts(_repo):
                calls.append(len(calls) + 1)
                if len(calls) == 2:
                    stop_created_at.append(dt.datetime.now(dt.timezone.utc))
                    stop_file.write_text("stop\n", encoding="utf-8")
                return {
                    "root": str(repo.resolve()),
                    "head": HEAD,
                    "dirty": False,
                    "status_lines": [],
                }

            admission = {
                "repo_head": HEAD,
                "lease_sha256": gate.sha256_file(lease_path),
            }
            with mock.patch.object(gate, "validate_lease", return_value=admission), mock.patch.object(
                gate, "git_facts", side_effect=fake_git_facts
            ), mock.patch.object(gate, "scan_known_interference", return_value=[]):
                self.assertEqual(gate.run_guard(args), 0)

            status = json.loads((output_dir / "status.json").read_text(encoding="utf-8"))
            with (output_dir / "integrity-samples.tsv").open(
                encoding="utf-8", newline=""
            ) as handle:
                rows = list(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual(len(rows), 3)
            self.assertEqual(status["sample_count"], 3)
            self.assertGreaterEqual(
                gate.parse_timestamp(rows[-1]["timestamp_utc"], "final sample"),
                stop_created_at[0],
            )


class CompatibilityTests(unittest.TestCase):
    def test_source_parses_as_python_38(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        ast.parse(source, filename=str(MODULE_PATH), feature_version=(3, 8))


if __name__ == "__main__":
    unittest.main(verbosity=2)
