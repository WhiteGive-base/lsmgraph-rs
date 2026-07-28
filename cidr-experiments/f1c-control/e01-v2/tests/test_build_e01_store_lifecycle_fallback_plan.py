#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "build_e01_store_lifecycle_fallback_plan.py"
)
SPEC = importlib.util.spec_from_file_location("fallback_plan", MODULE_PATH)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MOD)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


class FallbackLifecyclePlanTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, Path, Path]:
        immutable = root / "immutable"
        old_staging = immutable / "staging" / "budg-b64.tmp"
        old_staging.mkdir(parents=True)
        (old_staging / "partial").write_bytes(b"retained")
        source_budg = root / "source-budg"
        source_naive = root / "source-naive"
        parent = root / "parent.json"
        stores = []
        for variant, source in (
            ("budg-b64", source_budg),
            ("naive", source_naive),
        ):
            stores.append(
                {
                    "variant": variant,
                    "source_root": str(source),
                    "source_manifest": {
                        "path": str(root / f"{variant}.manifest.json"),
                        "sha256": "a" * 64,
                        "size_bytes": 1,
                    },
                    "expected_tree_sha256": "b" * 64,
                    "expected_file_count": 2,
                    "expected_total_bytes": 9,
                    "staging_root": str(
                        old_staging
                        if variant == "budg-b64"
                        else immutable / "staging" / "naive.tmp"
                    ),
                    "immutable_root": str(immutable / "stores" / variant),
                    "copy_policy": {
                        "preferred_method": "cp-reflink-always",
                        "full_copy_fallback_requires_explicit_enable": True,
                    },
                }
            )
        write_json(
            parent,
            {
                "schema_version": MOD.PARENT_SCHEMA,
                "state": "HOLD",
                "execution_state": "NOT_STARTED",
                "strict_serial": True,
                "max_live_mutable_clones": 1,
                "immutable_asset_root": str(immutable),
                "campaign_root": str(root / "campaign"),
                "stores": stores,
                "cells": [
                    {
                        "cell_key": "seml0:bridge-canary",
                        "variant": "budg-b64",
                        "clone_policy": {
                            "method": "cp-reflink-always",
                            "full_copy_fallback": False,
                        },
                    }
                ],
                "blockers": ["stores absent"],
                "large_content_read_now": False,
                "large_copy_performed_now": False,
                "source_store_modified_now": False,
                **MOD.FALSE_ELIGIBILITY,
            },
        )
        attempt = root / "attempt1"
        write_json(
            attempt / "PREFLIGHT.json",
            {
                "state": "PASS",
                "variant": "budg-b64",
                "expected": {"store_sha256": "b" * 64},
            },
        )
        write_json(
            attempt / "SOURCE-MANIFEST.json",
            {"store_sha256": "b" * 64},
        )
        (attempt / "COPY.stderr").write_bytes(b"Operation not supported\n")
        failure = attempt / "FAILED.json"
        write_json(
            failure,
            {
                "state": "FAILED_RETAINED",
                "variant": "budg-b64",
                "reason": "reflink copy failed rc=1",
                "source_modified": False,
                "source_root": str(source_budg),
                "staging_retained": True,
                "staging_root": str(old_staging),
            },
        )
        return parent, failure, old_staging

    def test_versions_both_rows_to_explicit_full_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent, failure, old_staging = self.fixture(Path(directory))
            value = MOD.build(
                parent_plan_path=parent,
                failure_receipt_path=failure,
                staging_generation="attempt2",
            )
            self.assertEqual(value["schema_version"], MOD.SCHEMA)
            self.assertEqual(
                value["copy_fallback"]["method"], MOD.FULL_COPY_METHOD
            )
            self.assertTrue(old_staging.is_dir())
            for row in value["stores"]:
                self.assertEqual(row["copy_method"], MOD.FULL_COPY_METHOD)
                self.assertTrue(row["full_copy_fallback"]["enabled"])
                self.assertNotEqual(Path(row["staging_root"]), old_staging)
                self.assertIn("staging-attempt2", row["staging_root"])
            self.assertEqual(
                value["cells"][0]["clone_policy"]["method"],
                MOD.FULL_COPY_METHOD,
            )
            self.assertTrue(
                value["cells"][0]["clone_policy"]["full_copy_fallback"]
            )

    def test_missing_failed_staging_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent, failure, old_staging = self.fixture(Path(directory))
            for path in old_staging.iterdir():
                path.unlink()
            old_staging.rmdir()
            with self.assertRaisesRegex(MOD.FallbackPlanError, "failed staging"):
                MOD.build(
                    parent_plan_path=parent,
                    failure_receipt_path=failure,
                    staging_generation="attempt2",
                )

    def test_bad_generation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent, failure, _ = self.fixture(Path(directory))
            with self.assertRaisesRegex(MOD.FallbackPlanError, "generation"):
                MOD.build(
                    parent_plan_path=parent,
                    failure_receipt_path=failure,
                    staging_generation="../attempt2",
                )


if __name__ == "__main__":
    unittest.main()
