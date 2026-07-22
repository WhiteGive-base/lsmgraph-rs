#!/usr/bin/env python3
"""Static/unit checks for the LiveGraph structured build receipt."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


P10_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = P10_DIR.parents[2]
BUILDER = P10_DIR / "adapters" / "livegraph" / "build_livegraph_runtime.py"
TEMPLATE = P10_DIR / "adapters" / "livegraph" / "build-config.template.json"

SPEC = importlib.util.spec_from_file_location("livegraph_builder", BUILDER)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class LiveGraphBuildReceiptTest(unittest.TestCase):
    def write_config(
        self,
        root: Path,
        *,
        integration: Path,
        source: Path,
        output: Path,
    ) -> Path:
        config = root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "schema_version": builder.CONFIG_SCHEMA,
                    "integration_root": str(integration.resolve()),
                    "livegraph_source_root": str(source.resolve()),
                    "output_dir": str(output.resolve()),
                    "compiler": str(Path(sys.executable).resolve()),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return config

    def assert_failed_only(self, output: Path) -> None:
        self.assertTrue(output.is_dir())
        self.assertFalse((output / "DONE.build").exists())
        failed = output / "FAILED"
        self.assertTrue(failed.is_file())
        self.assertEqual(json.loads(failed.read_text(encoding="utf-8"))["state"], "FAILED")

    def test_failed_reuse_never_deletes_or_relabels_existing_done_build(self) -> None:
        with tempfile.TemporaryDirectory(prefix="livegraph-build-existing-terminal-") as raw:
            root = Path(raw)
            output = root / "existing-output"
            output.mkdir()
            done = output / "DONE.build"
            done.write_text('{"state":"PASS","sentinel":"preserve"}\n', encoding="utf-8")
            before = done.read_bytes()
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "schema_version": builder.CONFIG_SCHEMA,
                        "integration_root": str(root / "missing-integration"),
                        "livegraph_source_root": str(root / "missing-source"),
                        "output_dir": str(output),
                        "compiler": "/usr/bin/g++",
                    }
                ) + "\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [sys.executable, "-B", str(BUILDER), "--config", str(config)],
                text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(done.read_bytes(), before)
            self.assertFalse((output / "FAILED").exists())

    def test_failed_reuse_never_overwrites_existing_failed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="livegraph-build-existing-failed-") as raw:
            root = Path(raw)
            output = root / "existing-output"
            output.mkdir()
            failed = output / "FAILED"
            failed.write_text('{"state":"FAILED","sentinel":"preserve"}\n', encoding="utf-8")
            before = failed.read_bytes()
            config = self.write_config(
                root,
                integration=root / "missing-integration",
                source=root / "missing-source",
                output=output,
            )
            with self.assertRaisesRegex(builder.BuildError, "output directory already exists"):
                builder.run(config)
            self.assertEqual(failed.read_bytes(), before)
            self.assertFalse((output / "DONE.build").exists())

    def test_failed_reuse_never_mutates_any_existing_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="livegraph-build-existing-output-") as raw:
            root = Path(raw)
            output = root / "existing-output"
            output.mkdir()
            sentinel = output / "unrelated-evidence.txt"
            sentinel.write_bytes(b"preserve exactly\n")
            before = {path.name: path.read_bytes() for path in output.iterdir()}
            config = self.write_config(
                root,
                integration=root / "missing-integration",
                source=root / "missing-source",
                output=output,
            )
            with self.assertRaisesRegex(builder.BuildError, "output directory already exists"):
                builder.run(config)
            after = {path.name: path.read_bytes() for path in output.iterdir()}
            self.assertEqual(after, before)
            self.assertFalse((output / "DONE.build").exists())
            self.assertFalse((output / "FAILED").exists())

    def test_new_safe_output_publishes_failed_for_early_root_errors(self) -> None:
        with tempfile.TemporaryDirectory(prefix="livegraph-build-early-failed-") as raw:
            root = Path(raw)
            cases = (
                ("integration", root / "missing-integration", root / "missing-source-a"),
                ("source", root / "present-integration", root / "missing-source-b"),
            )
            (root / "present-integration").mkdir()
            for label, integration, source in cases:
                with self.subTest(label=label):
                    case_root = root / label
                    case_root.mkdir()
                    output = root / f"output-{label}"
                    config = self.write_config(
                        case_root,
                        integration=integration,
                        source=source,
                        output=output,
                    )
                    with self.assertRaises(builder.BuildError):
                        builder.run(config)
                    self.assert_failed_only(output)

    def test_new_safe_output_publishes_failed_for_dirty_git_preflight(self) -> None:
        with tempfile.TemporaryDirectory(prefix="livegraph-build-git-failed-") as raw:
            root = Path(raw)
            integration = root / "integration"
            source = root / "source"
            integration.mkdir()
            source.mkdir()
            output = root / "output"
            config = self.write_config(
                root,
                integration=integration,
                source=source,
                output=output,
            )
            integration_state = {
                "root": str(integration.resolve()),
                "head": "1" * 40,
                "dirty": True,
                "status_lines": [" M tracked-file"],
            }
            source_state = {
                "root": str(source.resolve()),
                "head": "2" * 40,
                "dirty": False,
                "status_lines": [],
            }
            with mock.patch.object(
                builder,
                "git_state",
                side_effect=(integration_state, source_state),
            ):
                with self.assertRaisesRegex(builder.BuildError, "was dirty before build"):
                    builder.run(config)
            self.assert_failed_only(output)

    def test_build_receipt_binds_every_formal_livegraph_entrypoint(self) -> None:
        for name in (
            "adapter",
            "formal_system_template",
            "sf10_formal_wrapper",
            "p02b_validator",
            "p10_contract",
            "p10_suite",
            "p10_p31_bridge",
            "p31_wrapper",
            "p31_manifest",
            "p31_collector",
            "p31_validator",
        ):
            with self.subTest(name=name):
                self.assertIn(name, builder.INTEGRATION_FILES)

    def test_config_template_is_exact_and_explicit(self) -> None:
        value = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        self.assertEqual(
            set(value),
            {
                "schema_version",
                "integration_root",
                "livegraph_source_root",
                "output_dir",
                "compiler",
            },
        )
        self.assertEqual(value["schema_version"], builder.CONFIG_SCHEMA)
        self.assertTrue(value["integration_root"].startswith("__ABSOLUTE_"))
        self.assertTrue(value["livegraph_source_root"].startswith("__ABSOLUTE_"))
        self.assertTrue(value["output_dir"].startswith("__ABSOLUTE_"))
        self.assertEqual(value["compiler"], "/usr/bin/g++")

    def test_config_rejects_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory(prefix="livegraph-build-config-") as raw:
            path = Path(raw) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": builder.CONFIG_SCHEMA,
                        "integration_root": "/tmp/integration",
                        "livegraph_source_root": "/tmp/livegraph",
                        "output_dir": "/tmp/output",
                        "compiler": "/usr/bin/g++",
                        "unexpected": True,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(builder.BuildError):
                builder.load_config(path)

    def test_ldd_parser_rejects_unresolved_livegraph(self) -> None:
        with tempfile.TemporaryDirectory(prefix="livegraph-build-ldd-") as raw:
            frozen = Path(raw) / "liblivegraph.so"
            frozen.write_bytes(b"fixture")
            with self.assertRaises(builder.BuildError):
                builder.parse_ldd("liblivegraph.so => not found\n", frozen)

    @unittest.skipUnless(os.environ.get("LIVEGRAPH_SOURCE_ROOT"), "LIVEGRAPH_SOURCE_ROOT is not set")
    def test_real_clean_rebuild_publishes_consistent_receipt(self) -> None:
        source_root = Path(os.environ["LIVEGRAPH_SOURCE_ROOT"]).resolve()
        with tempfile.TemporaryDirectory(prefix="livegraph-build-receipt-") as raw:
            root = Path(raw)
            output = root / "runtime"
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "schema_version": builder.CONFIG_SCHEMA,
                        "integration_root": str(REPO_ROOT.resolve()),
                        "livegraph_source_root": str(source_root),
                        "output_dir": str(output),
                        "compiler": "/usr/bin/g++",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [sys.executable, str(BUILDER), "--config", str(config)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=300,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            receipt_path = output / "build-receipt.json"
            marker_path = output / "DONE.build"
            self.assertTrue(receipt_path.is_file())
            self.assertTrue(marker_path.is_file())
            self.assertFalse((output / "FAILED").exists())
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["schema_version"], builder.RECEIPT_SCHEMA)
            self.assertEqual(receipt["state"], "PASS")
            self.assertFalse(receipt["performance_eligible"])
            self.assertEqual(receipt["formal_performance_points"], 0)
            self.assertFalse(receipt["integration"]["before"]["dirty"])
            self.assertEqual(
                receipt["integration"]["before"]["head"],
                receipt["integration"]["after"]["head"],
            )
            self.assertEqual(
                receipt["livegraph_source"]["before"]["head"],
                receipt["livegraph_source"]["after"]["head"],
            )
            runtime = receipt["runtime"]
            self.assertEqual(runtime["process_lifetime"], builder.PROCESS_LIFETIME)
            self.assertEqual(
                Path(runtime["runtime_library_path"]).resolve(),
                (output / "lib/liblivegraph.so").resolve(),
            )
            self.assertEqual(len([item for item in runtime["dependencies"] if item["soname"] == "liblivegraph.so"]), 1)
            self.assertEqual(marker["receipt_sha256"], builder.sha256_file(receipt_path))
            for line in (output / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
                digest, relative = line.split("  ", 1)
                self.assertEqual(digest, builder.sha256_file(output / relative))


if __name__ == "__main__":
    unittest.main()
