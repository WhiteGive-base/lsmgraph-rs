#!/usr/bin/env python3

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


TEST_DIR = Path(__file__).resolve().parent
RUNNER_DIR = TEST_DIR.parent
sys.path.insert(0, str(RUNNER_DIR))

from resource_schema import (  # noqa: E402
    CONTAINER_IDENTITY_SCHEMA_VERSION,
    MANIFEST_SCHEMA_VERSION,
    RESOURCE_SCHEMA_VERSION,
    TERMINAL_CENSOR_POLICY,
)
from validate_resource_run import (  # noqa: E402
    PROCESS_IDENTITY_SCHEMA_VERSION,
    TERMINAL_CENSOR_RECEIPT_SCHEMA_VERSION,
    TERMINAL_CENSOR_UNREADABLE_FIELDS,
    TERMINAL_CENSOR_WARNING,
    build_terminal_censor_quality,
    classify_terminal_censor_policy,
    validate_resource_rows,
)


ROOT_PID = 100
HELPER_PID = 200


def resource_row(
    index: int,
    timestamp_utc: str,
    monotonic_s: float,
    pids: str,
    root_alive: int,
    user_cpu_s: float,
    sys_cpu_s: float,
    rss_bytes: int,
    pss_bytes: int,
    unreadable: int,
    io_total: int,
) -> dict[str, str]:
    process_count = 0 if not pids else len(pids.split(","))
    return {
        "schema_version": RESOURCE_SCHEMA_VERSION,
        "timestamp_utc": timestamp_utc,
        "monotonic_s": str(monotonic_s),
        "sample_index": str(index),
        "root_pid": str(ROOT_PID),
        "root_alive": str(root_alive),
        "process_count": str(process_count),
        "pids": pids,
        "process_user_cpu_s": str(user_cpu_s),
        "process_sys_cpu_s": str(sys_cpu_s),
        "process_cpu_pct": "25.0" if process_count else "0.0",
        "process_rss_bytes": str(rss_bytes),
        "process_rss_unreadable": str(unreadable),
        "process_pss_bytes": str(pss_bytes),
        "process_pss_unreadable": str(unreadable),
        "process_io_unreadable": str(unreadable),
        "process_read_bytes": str(io_total),
        "process_write_bytes": str(io_total * 2),
        "process_cancelled_write_bytes": "0",
        "process_rchar": str(io_total * 3),
        "process_wchar": str(io_total * 4),
        "process_read_mib_s": "0.0",
        "process_write_mib_s": "0.0",
        "host_load1": "0.1",
        "host_load5": "0.1",
        "host_load15": "0.1",
        "host_mem_total_bytes": "1000000",
        "host_mem_available_bytes": "900000",
        "host_swap_total_bytes": "0",
        "host_swap_free_bytes": "0",
        "device": "nvme1n1",
        "device_read_iops": "0.0",
        "device_write_iops": "0.0",
        "device_read_mib_s": "0.0",
        "device_write_mib_s": "0.0",
        "device_read_await_ms": "0.0",
        "device_write_await_ms": "0.0",
        "device_await_ms": "0.0",
        "device_util_pct": "0.0",
        "data_mount": "/data",
        "data_free_bytes": "800000",
    }


def policy_fixture(with_helper: bool = True) -> tuple[
    dict, dict, dict, dict, dict, list[dict[str, str]]
]:
    first_pids = f"{ROOT_PID},{HELPER_PID}" if with_helper else str(ROOT_PID)
    rows = [
        resource_row(
            0,
            "2026-07-24T00:00:00.000Z",
            0.0,
            first_pids,
            1,
            1.0,
            0.5,
            4096,
            2048,
            0,
            10,
        ),
        resource_row(
            1,
            "2026-07-24T00:00:00.250Z",
            0.25,
            str(ROOT_PID),
            1,
            2.0,
            0.8,
            8192,
            4096,
            0,
            20,
        ),
        resource_row(
            2,
            "2026-07-24T00:00:00.500Z",
            0.5,
            str(ROOT_PID),
            1,
            2.1,
            0.9,
            0,
            0,
            1,
            20,
        ),
        resource_row(
            3,
            "2026-07-24T00:00:00.750Z",
            0.75,
            "",
            0,
            2.1,
            0.9,
            0,
            0,
            0,
            20,
        ),
    ]
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "resource_schema_version": RESOURCE_SCHEMA_VERSION,
        "performance_eligible_declared": True,
        "terminal_censor_policy": dict(TERMINAL_CENSOR_POLICY),
        "root_pid": ROOT_PID,
        "collector": {
            "interval_s": 0.25,
            "containers": [],
            "extra_pids": [],
        },
    }
    execution = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "root_pid": ROOT_PID,
        "command_exit_code": 0,
        "collector_exit_code": 0,
        "guard_exit_code": 0,
        "wrapper_signal": None,
        "command_release_at_utc": "2026-07-24T00:00:00.100Z",
        "command_ended_at_utc": "2026-07-24T00:00:00.600Z",
    }
    identities = [
        {
            "pid": ROOT_PID,
            "ppid": 50,
            "pgrp": ROOT_PID,
            "start_ticks": 1000,
            "first_sample_index": 0,
            "last_sample_index": 2,
            "sample_count": 3,
        }
    ]
    if with_helper:
        identities.append(
            {
                "pid": HELPER_PID,
                "ppid": ROOT_PID,
                "pgrp": ROOT_PID,
                "start_ticks": 1001,
                "first_sample_index": 0,
                "last_sample_index": 0,
                "sample_count": 1,
            }
        )
    identities.sort(key=lambda value: (value["pid"], value["start_ticks"]))
    ready = {
        "schema_version": CONTAINER_IDENTITY_SCHEMA_VERSION,
        "state": "READY",
        "root_pid": ROOT_PID,
        "resource_sample_index": 0,
        "external_zero_baseline": True,
        "containers": {},
        "extra_pids": [],
        "sample_pids": [ROOT_PID, *([HELPER_PID] if with_helper else [])],
        "external_zero_baseline_pids": [],
    }
    collector = {
        "schema_version": RESOURCE_SCHEMA_VERSION,
        "state": "DONE",
        "errors": [],
        "root_pid": ROOT_PID,
        "root_seen_alive": True,
        "resource_samples": len(rows),
        "interval_s": 0.25,
        "containers": [],
        "extra_pids": [],
        "process_identity_schema_version": PROCESS_IDENTITY_SCHEMA_VERSION,
        "process_identity_unique_set": identities,
        "collector_ready": ready,
    }
    guard_status = {
        "schema_version": "cidr-p31-integrity-guard-v2",
        "state": "PASS",
        "errors": [],
    }
    return manifest, execution, collector, ready, guard_status, rows


class TerminalCensorPolicyTests(unittest.TestCase):
    def classify(self, fixture: tuple) -> tuple[dict | None, str]:
        return classify_terminal_censor_policy(*fixture)

    def test_accepts_exact_root_only_penultimate_shape_with_sample_zero_helper(self) -> None:
        receipt, reason = self.classify(policy_fixture(with_helper=True))
        self.assertEqual(reason, "accepted exact root-only penultimate terminal censor")
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual(
            receipt["schema_version"], TERMINAL_CENSOR_RECEIPT_SCHEMA_VERSION
        )
        self.assertEqual(receipt["sample_index"], 2)
        self.assertEqual(receipt["root_pid"], ROOT_PID)
        self.assertEqual(receipt["root_start_ticks"], 1000)
        self.assertEqual(
            receipt["censored_fields"],
            list(TERMINAL_CENSOR_UNREADABLE_FIELDS),
        )
        self.assertTrue(receipt["process_cpu_tail_lower_bound"])
        self.assertTrue(receipt["process_io_tail_lower_bound"])

    def test_accepts_exact_shape_without_helper(self) -> None:
        receipt, reason = self.classify(policy_fixture(with_helper=False))
        self.assertIsNotNone(receipt, reason)

    def test_missing_policy_retains_three_strict_unreadable_errors(self) -> None:
        manifest, execution, collector, ready, guard_status, rows = policy_fixture()
        manifest.pop("terminal_censor_policy")
        receipt, reason = classify_terminal_censor_policy(
            manifest, execution, collector, ready, guard_status, rows
        )
        self.assertIsNone(receipt)
        self.assertIn("not explicitly enabled", reason)
        errors: list[str] = []
        warnings: list[str] = []
        validate_resource_rows(rows, 2, True, errors, warnings, receipt)
        self.assertEqual(
            errors,
            [
                "one or more RSS samples were unreadable; aggregate is a lower bound",
                "one or more PSS samples were unreadable; aggregate is a lower bound",
                "one or more process I/O samples were unreadable; aggregate is a lower bound",
            ],
        )
        self.assertEqual(warnings, [])

    def test_exact_receipt_replaces_only_generic_unreadable_errors_with_warning(self) -> None:
        fixture = policy_fixture()
        receipt, reason = self.classify(fixture)
        self.assertIsNotNone(receipt, reason)
        errors: list[str] = []
        warnings: list[str] = []
        validate_resource_rows(fixture[-1], 2, True, errors, warnings, receipt)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [TERMINAL_CENSOR_WARNING])
        quality = build_terminal_censor_quality(fixture[0], receipt, reason)
        self.assertEqual(
            quality["process_cpu_quality"],
            "observed-through-terminal-stat-lower-bound",
        )
        self.assertEqual(quality["process_io_quality"], "tail-lower-bound")
        self.assertEqual(quality["peak_rss_pss_quality"], "max-readable-samples")
        self.assertTrue(quality["terminal_censored"])
        self.assertEqual(quality["terminal_censored_sample_index"], 2)

    def test_forged_receipt_cannot_suppress_strict_errors(self) -> None:
        rows = policy_fixture()[-1]
        errors: list[str] = []
        warnings: list[str] = []
        validate_resource_rows(rows, 2, True, errors, warnings, {"sample_index": 2})
        self.assertEqual(len(errors), 3)
        self.assertEqual(warnings, [])

    def test_rejects_every_material_shape_or_provenance_drift(self) -> None:
        mutations = {
            "wrong-policy": lambda value: value[0]["terminal_censor_policy"].update(
                {"unexpected": True}
            ),
            "not-performance": lambda value: value[0].update(
                {"performance_eligible_declared": False}
            ),
            "container-present": lambda value: value[0]["collector"].update(
                {"containers": ["db"]}
            ),
            "command-failed": lambda value: value[1].update(
                {"command_exit_code": 1}
            ),
            "guard-failed": lambda value: value[4].update({"state": "FAILED"}),
            "two-unreadable-rows": lambda value: value[5][1].update(
                {
                    "process_rss_unreadable": "1",
                    "process_pss_unreadable": "1",
                    "process_io_unreadable": "1",
                }
            ),
            "bad-not-root-only": lambda value: value[5][2].update(
                {"pids": f"{ROOT_PID},300", "process_count": "2"}
            ),
            "final-not-empty": lambda value: value[5][3].update(
                {"pids": str(ROOT_PID), "process_count": "1", "root_alive": "1"}
            ),
            "helper-persists": lambda value: value[5][1].update(
                {"pids": f"{ROOT_PID},{HELPER_PID}", "process_count": "2"}
            ),
            "root-identity-short": lambda value: value[2][
                "process_identity_unique_set"
            ][0].update({"last_sample_index": 1}),
            "io-changes-at-censor": lambda value: value[5][2].update(
                {"process_read_bytes": "21"}
            ),
            "cpu-changes-after-censor": lambda value: value[5][3].update(
                {"process_user_cpu_s": "2.2"}
            ),
            "end-too-late": lambda value: (
                value[1].update(
                    {"command_ended_at_utc": "2026-07-24T00:00:00.800Z"}
                ),
                value[5][3].update(
                    {"timestamp_utc": "2026-07-24T00:00:00.900Z"}
                ),
            ),
            "sample-gap": lambda value: value[5][3].update(
                {"monotonic_s": "1.5"}
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                fixture = list(copy.deepcopy(policy_fixture()))
                mutate(fixture)
                receipt, reason = classify_terminal_censor_policy(*fixture)
                self.assertIsNone(receipt, reason)
                self.assertNotEqual(reason, "")


if __name__ == "__main__":
    unittest.main()
