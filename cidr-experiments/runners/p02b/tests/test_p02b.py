#!/usr/bin/env python3

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from build_lineage_manifest import build_tree_manifest
from calculate_cv import calculate_cv
from p02b_common import GateError, sha256_file
from validate_clean_ready import validate_clean_ready


class CvTests(unittest.TestCase):
    def make_metric(self, root: Path, index: int, qps: float, p99: float) -> Path:
        path = root / "r{}-metrics.json".format(index)
        path.write_text(
            json.dumps(
                {
                    "schema_version": "p02b-sentinel-run-metrics-v1",
                    "state": "PASS",
                    "run_index": index,
                    "run_id": "fixture-r{}".format(index),
                    "query_count": 60,
                    "qps": qps,
                    "p99_us": p99,
                    "measured_seconds": 1.0,
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_cv_passes_with_stable_independent_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [
                self.make_metric(root, 1, 100.0, 1000.0),
                self.make_metric(root, 2, 101.0, 1005.0),
                self.make_metric(root, 3, 99.5, 995.0),
            ]
            result = calculate_cv(paths, 3, 0.03, 0.05)
            self.assertEqual(result["state"], "PASS")
            self.assertTrue(result["qps"]["pass"])
            self.assertTrue(result["p99_us"]["pass"])

    def test_cv_holds_when_qps_is_unstable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [
                self.make_metric(root, 1, 100.0, 1000.0),
                self.make_metric(root, 2, 120.0, 1000.0),
                self.make_metric(root, 3, 80.0, 1000.0),
            ]
            result = calculate_cv(paths, 3, 0.03, 0.05)
            self.assertEqual(result["state"], "HOLD")
            self.assertFalse(result["qps"]["pass"])

    def test_cv_rejects_duplicate_run_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [
                self.make_metric(root, 1, 100.0, 1000.0),
                self.make_metric(root, 2, 100.0, 1000.0),
                self.make_metric(root, 3, 100.0, 1000.0),
            ]
            value = json.loads(paths[2].read_text(encoding="utf-8"))
            value["run_id"] = "fixture-r2"
            paths[2].write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(GateError):
                calculate_cv(paths, 3, 0.03, 0.05)


class LineageManifestTests(unittest.TestCase):
    def test_tree_digest_is_deterministic_and_path_sensitive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "store"
            root.mkdir()
            (root / "a").write_text("one\n", encoding="utf-8")
            (root / "b").write_text("two\n", encoding="utf-8")
            first = build_tree_manifest(root, "store")
            second = build_tree_manifest(root, "store")
            self.assertEqual(first["store_sha256"], second["store_sha256"])
            (root / "b").write_text("changed\n", encoding="utf-8")
            third = build_tree_manifest(root, "store")
            self.assertNotEqual(first["store_sha256"], third["store_sha256"])


class CleanReadyTests(unittest.TestCase):
    def make_ready(self, root: Path, stopped: bool = False) -> Path:
        p03 = root / "P03-CLEAN-WINDOW-MONITOR"
        run_dir = p03 / "raw" / "fixture-clean"
        run_dir.mkdir(parents=True)
        monitor = p03 / "monitor_clean_window.sh"
        monitor.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        classification = {
            "performance_eligible": "false",
            "purpose": "clean_window_readiness_only",
            "gate_mode": "seml0",
            "run_id": "fixture-clean",
            "script_sha256": sha256_file(monitor),
            "git_head": "0" * 40,
            "host": "fixture",
            "ready_samples": "3",
        }
        (run_dir / "classification.env").write_text(
            "".join("{}={}\n".format(key, value) for key, value in classification.items()),
            encoding="utf-8",
        )
        header = (
            "timestamp\tsample\tmetric_pass\tservice_pass\tsample_pass\tstreak\t"
            "reasons\tgate_mode\n"
        )
        rows = "".join(
            "{}\t{}\t1\t1\t1\t{}\tnone\tseml0\n".format(now, index, index)
            for index in range(1, 4)
        )
        (run_dir / "samples.tsv").write_text(header + rows, encoding="utf-8")
        (run_dir / "latest.tsv").write_text(header + rows.splitlines(True)[-1], encoding="utf-8")
        (run_dir / "STATE").write_text(
            "performance_eligible=false\nsample_pass=1\nstreak=3\nrequired_streak=3\nreasons=none\n",
            encoding="utf-8",
        )
        (run_dir / "COMPLETE").write_text("run_id=fixture-clean\n", encoding="utf-8")
        (run_dir / "READY").write_text(
            "performance_eligible=false\n"
            "readiness_gate=PASS\n"
            "gate_mode=seml0\n"
            "run_id=fixture-clean\n"
            "ready_time={}\n"
            "samples=3\n"
            "consecutive_passes=3\n"
            "latest_sample={}\n".format(now, (run_dir / "latest.tsv").resolve()),
            encoding="utf-8",
        )
        if stopped:
            (run_dir / "STOPPED").write_text("state=stopped\n", encoding="utf-8")
        return run_dir / "READY"

    def test_ready_binding_passes_and_hashes_monitor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ready = self.make_ready(Path(temporary))
            result = validate_clean_ready(ready, 300, 3)
            self.assertEqual(result["state"], "PASS")
            self.assertEqual(result["observed_consecutive_samples"], 3)
            self.assertIn("monitor_clean_window.sh", result["artifacts"])

    def test_ready_binding_rejects_stopped_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ready = self.make_ready(Path(temporary), stopped=True)
            with self.assertRaises(GateError):
                validate_clean_ready(ready, 300, 3)

    def test_ready_binding_rejects_tampered_samples(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ready = self.make_ready(Path(temporary))
            samples = ready.parent / "samples.tsv"
            text = samples.read_text(encoding="utf-8").replace("\t3\t1\t1\t1\t3\t", "\t3\t1\t1\t0\t0\t")
            samples.write_text(text, encoding="utf-8")
            with self.assertRaises(GateError):
                validate_clean_ready(ready, 300, 3)


if __name__ == "__main__":
    unittest.main()
