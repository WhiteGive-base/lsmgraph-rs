#!/usr/bin/env python3

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "build_pristine_inventory.py"
SPEC = importlib.util.spec_from_file_location("inventory", str(SCRIPT))
INVENTORY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INVENTORY)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class PristineInventoryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.store = self.root / "store"
        (self.store / "nested").mkdir(parents=True)
        (self.store / "z.bin").write_bytes(b"z\x00")
        (self.store / "nested" / "a.bin").write_bytes(b"alpha\n")
        self.output = self.root / "manifest.json"
        self.dataset_sha = "1" * 64
        self.binary_sha = "2" * 64

    def tearDown(self):
        self.temporary.cleanup()

    def command(self, *extra):
        return [sys.executable, str(SCRIPT), "--store", str(self.store)] + list(extra)

    def build(self):
        return subprocess.run(
            self.command(
                "--output", str(self.output),
                "--scale", "sf10",
                "--dataset-sha256", self.dataset_sha,
                "--binary-sha256", self.binary_sha,
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def test_builds_exact_runner_compatible_schema_and_digest(self):
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(self.output.read_text(encoding="utf-8"))
        required = {
            "schema_version", "state", "inventory_schema", "store_path", "scale",
            "l0_layout", "dataset_sha256", "binary_sha256", "inventory_sha256", "files",
        }
        self.assertTrue(required.issubset(manifest))
        self.assertEqual([item["path"] for item in manifest["files"]], ["nested/a.bin", "z.bin"])
        canonical = json.dumps(
            manifest["files"], sort_keys=True, separators=(",", ":")
        ) + "\n"
        self.assertEqual(
            manifest["inventory_sha256"],
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(manifest["file_count"], 2)
        self.assertEqual(manifest["total_bytes"], 8)

    def test_verify_passes_then_detects_content_change(self):
        self.assertEqual(self.build().returncode, 0)
        passed = subprocess.run(
            self.command("--verify", str(self.output)),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(passed.returncode, 0, passed.stderr)
        (self.store / "z.bin").write_bytes(b"x\x00")
        failed = subprocess.run(
            self.command("--verify", str(self.output)),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(failed.returncode, 2)
        self.assertIn("differs from manifest", failed.stderr)

    def test_output_must_be_absent_and_outside_store(self):
        self.output.write_text("do not overwrite\n", encoding="utf-8")
        result = self.build()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "do not overwrite\n")
        inside = self.store / "manifest.json"
        result = subprocess.run(
            self.command(
                "--output", str(inside),
                "--scale", "sf10",
                "--dataset-sha256", self.dataset_sha,
                "--binary-sha256", self.binary_sha,
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("inside", result.stderr)

    def test_manifest_validator_rejects_unsorted_rows(self):
        self.assertEqual(self.build().returncode, 0)
        manifest = json.loads(self.output.read_text(encoding="utf-8"))
        manifest["files"].reverse()
        with self.assertRaises(INVENTORY.InventoryError):
            INVENTORY.validate_manifest(
                manifest, self.store, "sf10", self.dataset_sha, self.binary_sha
            )

    def test_rejects_symlink_store_root(self):
        linked = self.root / "store-link"
        try:
            linked.symlink_to(self.store, target_is_directory=True)
        except OSError as exc:
            self.skipTest("symlink creation unavailable: {}".format(exc))
        command = [
            sys.executable, str(SCRIPT), "--store", str(linked),
            "--output", str(self.output), "--scale", "sf10",
            "--dataset-sha256", self.dataset_sha,
            "--binary-sha256", self.binary_sha,
        ]
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("non-link", result.stderr)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
