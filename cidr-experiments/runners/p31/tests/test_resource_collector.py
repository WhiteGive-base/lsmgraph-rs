#!/usr/bin/env python3

from __future__ import annotations

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
    scan_disk_root,
)
from resource_schema import DISK_CATEGORIES, DISK_COLUMNS, RESOURCE_COLUMNS  # noqa: E402
from run_manifest import artifact_ref  # noqa: E402
from validate_resource_run import pidstat_has_pid_sample, validate_formal_provenance  # noqa: E402


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
