#!/usr/bin/env python3

from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


P10_DIR = Path(__file__).resolve().parents[1]
RUNNER = P10_DIR / "run_suite.py"
MANIFEST = Path(__file__).resolve().parent / "fixture-suite.json"
FIXTURE_P31 = Path(__file__).resolve().parent / "fixture_p31.sh"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class P10OrchestratorTests(unittest.TestCase):
    maxDiff = None

    def run_cli(self, *args: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, "-B", str(RUNNER), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            expected,
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        return completed

    def test_static_manifest_validation_covers_frozen_six(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-static-") as temporary:
            run_root = Path(temporary) / "not-created"
            completed = self.run_cli(
                "validate",
                "--manifest",
                str(MANIFEST),
                "--run-root",
                str(run_root),
                "--mode",
                "fixture",
            )
            value = json.loads(completed.stdout)
            self.assertEqual(value["state"], "VALID")
            self.assertEqual(
                value["systems"],
                ["seml0", "livegraph", "aster", "tugraph", "neo4j", "nebulagraph"],
            )
            self.assertEqual(value["query_count"], 2)
            self.assertFalse(run_root.exists())

    def test_documented_json_schemas_accept_fixture_contract(self) -> None:
        try:
            import jsonschema
        except ImportError:
            self.skipTest("optional jsonschema package is unavailable; executable standard-library validator still runs")
        schemas = P10_DIR / "schemas"
        suite_schema = load_json(schemas / "suite-manifest.schema.json")
        jsonschema.Draft202012Validator.check_schema(suite_schema)
        jsonschema.Draft202012Validator(suite_schema).validate(load_json(MANIFEST))

    def test_full_six_system_fixture_run(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-suite-") as temporary:
            run_root = Path(temporary) / "run"
            self.run_cli(
                "run",
                "--manifest",
                str(MANIFEST),
                "--run-root",
                str(run_root),
                "--mode",
                "fixture",
                "--p31-wrapper",
                str(FIXTURE_P31),
            )
            self.assertTrue((run_root / "DONE").is_file())
            self.assertFalse((run_root / "FAILED").exists())
            self.assertFalse((run_root / "PARTIAL-DONE").exists())
            summary = json.loads((run_root / "suite-summary.json").read_text(encoding="utf-8"))
            self.assertTrue(summary["complete_frozen_suite"])
            self.assertFalse(summary["performance_eligible"])
            self.assertFalse(summary["cross_group_speedups_allowed"])
            self.assertEqual(summary["groups"]["embedded"], ["seml0", "livegraph", "aster", "tugraph"])
            self.assertEqual(summary["groups"]["client-server"], ["neo4j", "nebulagraph"])

            with (run_root / "repeat-results.tsv").open("r", encoding="utf-8", newline="") as handle:
                repeat_rows = list(csv.DictReader(handle, delimiter="\t"))
            with (run_root / "system-results.tsv").open("r", encoding="utf-8", newline="") as handle:
                system_rows = list(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual(len(repeat_rows), 6)
            self.assertEqual(len(system_rows), 6)
            for row in repeat_rows:
                self.assertEqual(row["completed_queries"], "2")
                self.assertEqual(row["timeout_queries"], "0")
                self.assertEqual(row["mismatch_queries"], "0")
                self.assertEqual(row["expected_digest_sha256"], row["actual_digest_sha256"])
                self.assertGreater(float(row["latency_p95_us"]), 0)
                self.assertGreater(float(row["latency_p99_us"]), 0)
                self.assertEqual(row["peak_rss_bytes"], "1048576")

            for system_id in ("neo4j", "nebulagraph"):
                argv = (run_root / "systems" / system_id / "repeat-01" / "p31" / "argv.txt").read_text(
                    encoding="utf-8"
                )
                self.assertIn("--container\n", argv)
            observations = run_root / "systems" / "seml0" / "repeat-01" / "adapter-output" / "query-observations.tsv"
            self.assertEqual(len(observations.read_text(encoding="utf-8").splitlines()), 5)
            try:
                import jsonschema
            except ImportError:
                return
            schemas = P10_DIR / "schemas"
            request = load_json(run_root / "systems" / "seml0" / "repeat-01" / "adapter-request.json")
            adapter_result = load_json(
                run_root / "systems" / "seml0" / "repeat-01" / "adapter-output" / "adapter-result.json"
            )
            with observations.open("r", encoding="utf-8", newline="") as handle:
                first_observation = next(csv.DictReader(handle, delimiter="\t"))
            for schema_name, instance in (
                ("adapter-request.schema.json", request),
                ("adapter-result.schema.json", adapter_result),
                ("observation-row.schema.json", first_observation),
            ):
                schema = load_json(schemas / schema_name)
                jsonschema.Draft202012Validator.check_schema(schema)
                jsonschema.Draft202012Validator(schema).validate(instance)

    def test_group_scope_cannot_publish_full_done(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-partial-") as temporary:
            run_root = Path(temporary) / "run"
            self.run_cli(
                "run",
                "--manifest",
                str(MANIFEST),
                "--run-root",
                str(run_root),
                "--mode",
                "fixture",
                "--p31-wrapper",
                str(FIXTURE_P31),
                "--group",
                "embedded",
            )
            self.assertTrue((run_root / "PARTIAL-DONE").is_file())
            self.assertFalse((run_root / "DONE").exists())
            summary = json.loads((run_root / "suite-summary.json").read_text(encoding="utf-8"))
            self.assertFalse(summary["complete_frozen_suite"])
            self.assertEqual(summary["system_count"], 4)

    def altered_manifest(self, directory: Path, profile: str) -> Path:
        value = json.loads(MANIFEST.read_text(encoding="utf-8"))
        value["systems"][0]["adapter"]["args"][1] = profile
        path = directory / f"fixture-{profile}.json"
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        return path

    def test_mismatch_timeout_and_order_are_fail_closed(self) -> None:
        for profile in ("mismatch", "timeout", "bad-order"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory(prefix=f"p10-{profile}-") as temporary:
                temporary_path = Path(temporary)
                manifest = self.altered_manifest(temporary_path, profile)
                run_root = temporary_path / "run"
                self.run_cli(
                    "run",
                    "--manifest",
                    str(manifest),
                    "--run-root",
                    str(run_root),
                    "--mode",
                    "fixture",
                    "--p31-wrapper",
                    str(FIXTURE_P31),
                    expected=2,
                )
                self.assertTrue((run_root / "FAILED").is_file())
                self.assertFalse((run_root / "DONE").exists())
                self.assertFalse((run_root / "PARTIAL-DONE").exists())
                failure = json.loads((run_root / "FAILED").read_text(encoding="utf-8"))
                self.assertEqual(failure["state"], "FAILED")

    def test_formal_mode_rejects_fixture_manifest_before_run(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-formal-reject-") as temporary:
            run_root = Path(temporary) / "run"
            completed = self.run_cli(
                "validate",
                "--manifest",
                str(MANIFEST),
                "--run-root",
                str(run_root),
                "--mode",
                "formal",
                expected=2,
            )
            self.assertIn("rejects fixture_only", completed.stderr)
            self.assertFalse(run_root.exists())

    def test_formal_mode_rejects_mislabeled_fixture_adapter(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-formal-mislabeled-") as temporary:
            temporary_path = Path(temporary)
            value = load_json(MANIFEST)
            value["fixture_only"] = False
            for key in ("dataset", "truth"):
                source = Path(str(value[key]["path"]).replace("${REPO_ROOT}", str(P10_DIR.parents[2])))
                value[key]["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
            adapter_path = P10_DIR / "tests" / "fixture_adapter.py"
            adapter_sha = hashlib.sha256(adapter_path.read_bytes()).hexdigest()
            for system in value["systems"]:
                system["fixture_only"] = False
                system["system_version"] = system["system_version"].replace("fixture-", "test-mislabeled-")
                system["adapter"]["sha256"] = adapter_sha
                system["binary"]["sha256"] = adapter_sha
            manifest = temporary_path / "mislabeled.json"
            manifest.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
            completed = self.run_cli(
                "validate",
                "--manifest",
                str(manifest),
                "--run-root",
                str(temporary_path / "run"),
                "--mode",
                "formal",
                expected=2,
            )
            self.assertIn("rejects test/fixture adapter paths", completed.stderr)

    def test_unknown_manifest_field_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p10-unknown-") as temporary:
            temporary_path = Path(temporary)
            value = json.loads(MANIFEST.read_text(encoding="utf-8"))
            value["protocol"]["unfrozen_knob"] = 1
            manifest = temporary_path / "unknown.json"
            manifest.write_text(json.dumps(value) + "\n", encoding="utf-8")
            completed = self.run_cli(
                "validate",
                "--manifest",
                str(manifest),
                "--run-root",
                str(temporary_path / "run"),
                "--mode",
                "fixture",
                expected=2,
            )
            self.assertIn("unknown keys", completed.stderr)


if __name__ == "__main__":
    unittest.main()
