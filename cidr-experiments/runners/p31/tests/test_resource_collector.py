#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

TEST_DIR = Path(__file__).resolve().parent
RUNNER_DIR = TEST_DIR.parent
sys.path.insert(0, str(RUNNER_DIR))

from resource_collector import (  # noqa: E402
    DeviceCounters,
    ProcCounters,
    ProcStat,
    ProcessAccumulator,
    classify_store_file,
    device_delta,
    parse_iostat_raw,
    parse_proc_stat_text,
    record_container_identity,
    resolve_container_identity,
    scan_disk_root,
)
from resource_schema import DISK_CATEGORIES, DISK_COLUMNS, RESOURCE_COLUMNS  # noqa: E402
from run_manifest import artifact_ref  # noqa: E402
from validate_resource_run import (  # noqa: E402
    pidstat_has_pid_sample,
    validate_container_tracking,
    validate_formal_provenance,
    validate_iostat_rows,
    validate_process_tracking,
)


class ResourceCollectorUnitTests(unittest.TestCase):
    def test_proc_stat_parser_accepts_spaces_and_parenthesis_in_comm(self) -> None:
        stat = parse_proc_stat_text(
            "123 (worker name) shard) R 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20"
        )
        self.assertEqual(stat.pid, 123)
        self.assertEqual(stat.ppid, 1)
        self.assertEqual(stat.pgrp, 2)
        self.assertEqual(stat.utime_ticks, 11)
        self.assertEqual(stat.stime_ticks, 12)
        self.assertEqual(stat.start_ticks, 19)

    def test_store_classification_is_mutually_exclusive(self) -> None:
        examples = {
            Path("levels/base.edge"): "payload",
            Path("metadata.idx"): "metadata",
            Path("catalog/schema.json"): "catalog",
            Path("query-signature.sidecar"): "sidecar",
            Path("MANIFEST-000001"): "manifest",
            Path("wal/0001.wal"): "wal",
            Path("tmp/pending.tmp"): "temp",
            Path("base_graph/KNOWS/meta.json"): "metadata",
            Path("budgeted-edge-candidates.tsv"): "sidecar",
            Path("snb_vertices.jsonl"): "payload",
            Path("notes.txt"): "other",
        }
        for path, expected in examples.items():
            with self.subTest(path=path):
                self.assertEqual(classify_store_file(path), expected)
        self.assertEqual(classify_store_file(Path("anything.bin"), "temp"), "temp")

    def test_scan_disk_root_category_sum_matches_total(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "levels").mkdir()
            (root / "levels" / "base.edge").write_bytes(b"p" * 13)
            (root / "metadata.idx").write_bytes(b"m" * 7)
            row = scan_disk_root("store", "fixture", root, 1.25, 0)
        self.assertEqual(row["scan_complete"], 1)
        self.assertEqual(row["scan_error_count"], 0)
        self.assertEqual(row["total_bytes"], 20)
        self.assertEqual(
            row["total_bytes"],
            sum(int(row[f"{category}_bytes"]) for category in DISK_CATEGORIES),
        )

    def test_iostat_12_2_parser(self) -> None:
        raw = """07/22/2026 12:30:33 AM
Device r/s rMB/s rrqm/s %rrqm r_await rareq-sz w/s wMB/s wrqm/s %wrqm w_await wareq-sz d/s dMB/s drqm/s %drqm d_await dareq-sz aqu-sz %util
nvme1n1 2.00 0.01 1.00 33.33 0.50 6.00 1192.00 12.39 1979.00 62.41 0.11 10.64 0.00 0.00 0.00 0.00 0.00 0.00 0.00 34.40
"""
        rows = parse_iostat_raw(raw, "nvme1n1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["timestamp_raw"], "07/22/2026 12:30:33 AM")
        self.assertEqual(rows[0]["read_iops"], 2.0)
        self.assertEqual(rows[0]["write_iops"], 1192.0)
        self.assertEqual(rows[0]["read_mib_s"], 0.01)
        self.assertEqual(rows[0]["write_mib_s"], 12.39)
        self.assertAlmostEqual(rows[0]["await_ms"], (0.5 * 2 + 0.11 * 1192) / 1194)
        self.assertEqual(rows[0]["util_pct"], 34.4)

    def test_iostat_c_locale_two_digit_year_timestamp(self) -> None:
        raw = """07/22/26 00:55:28
Device r/s rMB/s rrqm/s %rrqm r_await rareq-sz w/s wMB/s wrqm/s %wrqm w_await wareq-sz d/s dMB/s drqm/s %drqm d_await dareq-sz aqu-sz %util
nvme1n1 0.00 0.00 0.00 0.00 0.00 0.00 2.00 0.15 37.00 94.87 0.00 78.00 0.00 0.00 0.00 0.00 0.00 0.00 0.00 0.40
"""
        rows = parse_iostat_raw(raw, "nvme1n1")
        self.assertEqual(rows[0]["timestamp_raw"], "07/22/26 00:55:28")

    @staticmethod
    def iostat_validation_row(util_pct: float) -> dict[str, str]:
        return {
            "schema_version": "cidr-resource-v1",
            "sample_index": "0",
            "timestamp_raw": "07/23/26 23:54:15",
            "device": "nvme1n1",
            "read_iops": "26.0",
            "write_iops": "14098.0",
            "read_mib_s": "0.1",
            "write_mib_s": "3433.78",
            "read_await_ms": "1.65",
            "write_await_ms": "1.39",
            "await_ms": "1.3904786179552533",
            "aqu_sz": "9.36",
            "util_pct": str(util_pct),
        }

    def test_iostat_util_rounding_skew_is_preserved_and_warned(self) -> None:
        errors: list[str] = []
        warnings: list[str] = []
        validate_iostat_rows(
            [self.iostat_validation_row(100.8)],
            "nvme1n1",
            errors,
            warnings,
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("util_pct=100.8%", warnings[0])
        self.assertIn("raw value is preserved", warnings[0])

    def test_iostat_util_nominal_ceiling_has_no_warning(self) -> None:
        errors: list[str] = []
        warnings: list[str] = []
        validate_iostat_rows(
            [self.iostat_validation_row(100.0)],
            "nvme1n1",
            errors,
            warnings,
        )
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_iostat_util_rounding_skew_ceiling_is_inclusive(self) -> None:
        errors: list[str] = []
        warnings: list[str] = []
        validate_iostat_rows(
            [self.iostat_validation_row(101.0)],
            "nvme1n1",
            errors,
            warnings,
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("util_pct=101%", warnings[0])

    def test_iostat_util_above_rounding_skew_ceiling_fails_closed(self) -> None:
        errors: list[str] = []
        warnings: list[str] = []
        validate_iostat_rows(
            [self.iostat_validation_row(101.01)],
            "nvme1n1",
            errors,
            warnings,
        )
        self.assertEqual(warnings, [])
        self.assertEqual(
            errors,
            ["iostat-samples.tsv row 0: util_pct exceeds the 101.0% rounding/skew ceiling"],
        )

    def test_pidstat_root_sample_parser_handles_12_hour_time(self) -> None:
        raw = """# Time UID PID %usr Command
12:48:00 AM 1000 4321 25.00 fixture
"""
        self.assertTrue(pidstat_has_pid_sample(raw, 4321))
        self.assertFalse(pidstat_has_pid_sample(raw, 9999))

    def test_diskstats_delta(self) -> None:
        before = DeviceCounters(10, 100, 20, 30, 200, 40, 50)
        after = DeviceCounters(14, 140, 28, 32, 240, 44, 70)
        values = device_delta(before, after, 2.0)
        self.assertEqual(values["device_read_iops"], 2.0)
        self.assertEqual(values["device_write_iops"], 1.0)
        self.assertEqual(values["device_read_await_ms"], 2.0)
        self.assertEqual(values["device_write_await_ms"], 2.0)
        self.assertEqual(values["device_util_pct"], 1.0)

    def test_process_accumulator_excludes_preexisting_baseline(self) -> None:
        def proc(pid: int, start: int, user: int, written: int) -> ProcCounters:
            return ProcCounters(
                stat=ProcStat(pid=pid, ppid=0, pgrp=1, utime_ticks=user, stime_ticks=0, start_ticks=start),
                rss_bytes=4096,
                rss_readable=True,
                pss_bytes=2048,
                pss_readable=True,
                io_readable=True,
                read_bytes=0,
                write_bytes=written,
                cancelled_write_bytes=0,
                rchar=0,
                wchar=written,
            )

        snapshots = [
            {1: proc(1, 100, 10, 100), 2: proc(2, 50, 20, 1000)},
            {1: proc(1, 100, 15, 150), 2: proc(2, 50, 22, 1020)},
            {},
        ]
        accumulator = ProcessAccumulator(zero_baseline_start_ticks=100)
        with patch("resource_collector.read_proc_counters", side_effect=lambda pid: snapshots[0].get(pid)):
            first = accumulator.snapshot([1, 2], 1.0)
        snapshots.pop(0)
        with patch("resource_collector.read_proc_counters", side_effect=lambda pid: snapshots[0].get(pid)):
            second = accumulator.snapshot([1, 2], 2.0)
        snapshots.pop(0)
        with patch("resource_collector.read_proc_counters", side_effect=lambda pid: snapshots[0].get(pid)):
            final = accumulator.snapshot([], 3.0)
        self.assertEqual(first["process_write_bytes"], 100)
        self.assertEqual(second["process_write_bytes"], 170)
        self.assertEqual(final["process_write_bytes"], 170)
        self.assertEqual(final["process_count"], 0)

    def test_forced_external_pid_establishes_zero_baseline(self) -> None:
        proc = ProcCounters(
            stat=ProcStat(pid=42, ppid=0, pgrp=42, utime_ticks=50, stime_ticks=10, start_ticks=200),
            rss_bytes=4096,
            rss_readable=True,
            pss_bytes=2048,
            pss_readable=True,
            io_readable=True,
            read_bytes=100,
            write_bytes=200,
            cancelled_write_bytes=0,
            rchar=300,
            wchar=400,
        )
        after = ProcCounters(
            stat=ProcStat(pid=42, ppid=0, pgrp=42, utime_ticks=55, stime_ticks=12, start_ticks=200),
            rss_bytes=4096,
            rss_readable=True,
            pss_bytes=2048,
            pss_readable=True,
            io_readable=True,
            read_bytes=110,
            write_bytes=225,
            cancelled_write_bytes=0,
            rchar=330,
            wchar=440,
        )
        accumulator = ProcessAccumulator(zero_baseline_start_ticks=100)
        with patch("resource_collector.read_proc_counters", side_effect=[proc, after]):
            first = accumulator.snapshot([42], 1.0, zero_baseline_pids=[42])
            second = accumulator.snapshot([42], 2.0, zero_baseline_pids=[42])
        self.assertEqual(first["process_user_cpu_s"], 0)
        self.assertEqual(first["process_write_bytes"], 0)
        self.assertGreater(second["process_user_cpu_s"], 0)
        self.assertEqual(second["process_write_bytes"], 25)

    def test_docker_identity_resolution_and_unique_history(self) -> None:
        raw = json.dumps(
            [
                {
                    "Id": "a" * 64,
                    "RestartCount": 0,
                    "State": {
                        "Running": True,
                        "Pid": 4321,
                        "StartedAt": "2026-07-22T00:00:00.000000000Z",
                    },
                }
            ]
        )
        proc_stat = ProcStat(
            pid=4321, ppid=1, pgrp=4321, utime_ticks=0, stime_ticks=0, start_ticks=987654
        )
        with patch("resource_collector.shutil.which", return_value="/usr/bin/docker"), patch(
            "resource_collector.subprocess.check_output", return_value=raw
        ), patch(
            "resource_collector.read_proc_stat", return_value=proc_stat
        ):
            identity = resolve_container_identity("neo4j-formal")
        self.assertEqual(identity["pid"], 4321)
        self.assertEqual(identity["process_start_ticks"], 987654)
        history: dict[str, list[dict[str, object]]] = {}
        unique: dict[str, list[dict[str, object]]] = {}
        record_container_identity("neo4j-formal", identity, 0, history, unique)
        record_container_identity("neo4j-formal", identity, 1, history, unique)
        self.assertEqual(len(history["neo4j-formal"]), 2)
        self.assertEqual(unique["neo4j-formal"], [identity])

    @staticmethod
    def container_tracking_fixture() -> tuple[dict, dict, dict, list[dict[str, str]]]:
        identity = {
            "container_id": "b" * 64,
            "pid": 4321,
            "process_start_ticks": 987654,
            "started_at": "2026-07-22T00:00:00.000000000Z",
            "restart_count": 0,
        }
        ready = {
            "schema_version": "cidr-container-identity-v2",
            "state": "READY",
            "ready_at_utc": "2026-07-22T00:00:01.000Z",
            "root_pid": 100,
            "resource_sample_index": 0,
            "external_zero_baseline": True,
            "containers": {"neo4j-formal": identity},
            "extra_pids": [],
            "sample_pids": [100, 4321],
            "external_zero_baseline_pids": [4321],
        }
        collector = {
            "container_identity_schema_version": "cidr-container-identity-v2",
            "containers_seen": {"neo4j-formal": 4321},
            "container_identity_history": {
                "neo4j-formal": [
                    {
                        **identity,
                        "observed_at_utc": "2026-07-22T00:00:00.500Z",
                        "before_sample_index": 0,
                    }
                ]
            },
            "container_identity_unique_set": {"neo4j-formal": [identity]},
            "collector_ready": ready,
        }
        config = {"containers": ["neo4j-formal"], "extra_pids": []}
        resources = [{"sample_index": "0", "pids": "100,4321"}]
        return config, collector, ready, resources

    def test_container_tracking_accepts_one_sampled_stable_identity(self) -> None:
        config, collector, ready, resources = self.container_tracking_fixture()
        errors: list[str] = []
        validate_container_tracking(config, collector, ready, resources, 100, errors)
        self.assertEqual(errors, [])

    def test_container_tracking_rejects_unsampled_pid_and_identity_drift(self) -> None:
        config, collector, ready, resources = self.container_tracking_fixture()
        changed = {
            "container_id": "c" * 64,
            "pid": 9876,
            "process_start_ticks": 987655,
            "started_at": "2026-07-22T00:00:02.000000000Z",
            "restart_count": 1,
        }
        collector["container_identity_history"]["neo4j-formal"].append(
            {
                **changed,
                "observed_at_utc": "2026-07-22T00:00:02.500Z",
                "before_sample_index": 0,
            }
        )
        collector["container_identity_unique_set"]["neo4j-formal"].append(changed)
        collector["containers_seen"]["neo4j-formal"] = 9876
        errors: list[str] = []
        validate_container_tracking(config, collector, ready, resources, 100, errors)
        self.assertTrue(any("never sampled" in error for error in errors))
        self.assertTrue(any("exactly one stable identity" in error for error in errors))

    def test_container_tracking_rejects_non_integer_containers_seen_pid(self) -> None:
        config, collector, ready, resources = self.container_tracking_fixture()
        collector["containers_seen"]["neo4j-formal"] = "4321"
        errors: list[str] = []
        validate_container_tracking(config, collector, ready, resources, 100, errors)
        self.assertTrue(any("not a positive integer" in error for error in errors))

    def test_process_tracking_accepts_exact_sorted_sampled_identity(self) -> None:
        collector = {
            "process_identity_schema_version": "cidr-process-identity-v1",
            "process_identity_unique_set": [
                {
                    "pid": 4321,
                    "ppid": 100,
                    "pgrp": 4321,
                    "start_ticks": 987654,
                    "first_sample_index": 0,
                    "last_sample_index": 1,
                    "sample_count": 2,
                }
            ],
        }
        resources = [
            {"sample_index": "0", "pids": "100,4321"},
            {"sample_index": "1", "pids": "100,4321"},
        ]
        errors: list[str] = []
        validate_process_tracking(collector, resources, errors)
        self.assertEqual(errors, [])

    def test_process_tracking_rejects_identity_and_boundary_drift(self) -> None:
        base = {
            "pid": 4321,
            "ppid": 100,
            "pgrp": 4321,
            "start_ticks": 987654,
            "first_sample_index": 0,
            "last_sample_index": 1,
            "sample_count": 2,
        }
        resources = [
            {"sample_index": "0", "pids": "100,4321"},
            {"sample_index": "1", "pids": "100,4321"},
        ]
        for label, values in (
            ("start_ticks", [{**base, "start_ticks": 0}]),
            ("process_group", [{**base, "pgrp": 0}]),
            ("boundary", [{**base, "last_sample_index": 2}]),
            ("duplicate", [dict(base), dict(base)]),
        ):
            collector = {
                "process_identity_schema_version": "cidr-process-identity-v1",
                "process_identity_unique_set": values,
            }
            errors: list[str] = []
            validate_process_tracking(collector, resources, errors)
            with self.subTest(label=label):
                self.assertTrue(errors)
    def test_schemas_have_unique_columns(self) -> None:
        self.assertEqual(len(RESOURCE_COLUMNS), len(set(RESOURCE_COLUMNS)))
        self.assertEqual(len(DISK_COLUMNS), len(set(DISK_COLUMNS)))

    def test_explicit_file_hash_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "input.bin"
            path.write_bytes(b"fixture")
            with self.assertRaises(ValueError):
                artifact_ref(str(path), "0" * 64)

    def test_formal_provenance_accepts_complete_clean_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            binary = root / "bench"
            config = root / "config.toml"
            dataset = root / "dataset"
            binary.write_bytes(b"binary")
            config.write_text("fixture=true\n", encoding="utf-8")
            dataset.mkdir()
            manifest = {
                "performance_eligible_declared": True,
                "repo": {
                    "git_sha_command_ok": True,
                    "status_command_ok": True,
                    "git_sha": "1" * 40,
                    "dirty": False,
                },
                "host": {
                    "pidstat_version_command_ok": True,
                    "iostat_version_command_ok": True,
                },
                "storage_host": {
                    "device": {"exists": True},
                    "findmnt_command_ok": True,
                    "findmnt": "/dev/fixture ext4 rw",
                },
                "collector": {"require_aux_tools": True},
                "inputs": {
                    "binary": artifact_ref(str(binary)),
                    "dataset": artifact_ref(str(dataset), "2" * 64),
                    "config": artifact_ref(str(config)),
                    "truth": artifact_ref(None),
                    "query_or_trace": artifact_ref(None),
                },
            }
            errors: list[str] = []
            validate_formal_provenance(manifest, errors)
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
