#!/usr/bin/env python3
"""Static contract checks for the materialized Aster formal-system template."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

P10_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(P10_DIR))

from p10_contract import (  # noqa: E402
    FROZEN_SYSTEM_GROUPS,
    INTERFACE_SCOPE,
    PREBUILT_PROCESS_LIFETIME,
)


class AsterFormalTemplateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.path = P10_DIR / "adapters" / "aster" / "formal-system.template.json"
        cls.system = json.loads(cls.path.read_text(encoding="utf-8"))

    def test_declares_frozen_embedded_lifecycle(self) -> None:
        expected_keys = {
            "id",
            "display_name",
            "group",
            "interface_scope",
            "system_version",
            "fixture_only",
            "service_lifecycle",
            "process_lifetime",
            "adapter",
            "binary",
            "runtime_libraries",
            "store_roots",
            "temp_roots",
            "containers",
            "extra_pids",
            "image_digests",
        }
        self.assertEqual(set(self.system), expected_keys)
        self.assertEqual(self.system["id"], "aster")
        self.assertEqual(self.system["group"], FROZEN_SYSTEM_GROUPS["aster"])
        self.assertEqual(self.system["interface_scope"], INTERFACE_SCOPE)
        self.assertFalse(self.system["fixture_only"])
        self.assertEqual(self.system["service_lifecycle"], "in-process")
        self.assertEqual(self.system["process_lifetime"], PREBUILT_PROCESS_LIFETIME)
        self.assertEqual(self.system["runtime_libraries"], [])
        self.assertEqual(self.system["temp_roots"], [])
        self.assertEqual(self.system["containers"], [])
        self.assertEqual(self.system["extra_pids"], [])
        self.assertEqual(self.system["image_digests"], [])

    def test_freezes_formal_adapter_inputs_and_p31_coverage(self) -> None:
        adapter = self.system["adapter"]
        self.assertEqual(
            adapter["path"],
            "__ABSOLUTE_REPO__/cidr-experiments/runners/p10/adapters/aster_adapter.py",
        )
        arguments = adapter["args"]
        self.assertEqual(len(arguments) % 2, 0)
        flags = arguments[::2]
        self.assertTrue(all(flag.startswith("--") for flag in flags))
        self.assertEqual(len(flags), len(set(flags)))
        values = dict(zip(flags, arguments[1::2]))
        expected_flags = {
            "--mode",
            "--lifecycle",
            "--source-root",
            "--source-commit",
            "--repo-root",
            "--binary",
            "--binary-sha256",
            "--dataset",
            "--dataset-sha256",
            "--store-label",
            "--store-manifest",
            "--store-manifest-sha256",
            "--p02b-result",
            "--p02b-result-sha256",
            "--p02b-validator",
            "--p02b-validator-sha256",
            "--p02b-max-age-seconds",
            "--p31-wrapper",
            "--p31-wrapper-sha256",
            "--block-cache-bytes",
        }
        self.assertEqual(set(values), expected_flags)
        self.assertEqual(values["--mode"], "formal")
        self.assertEqual(values["--lifecycle"], "reopen")
        self.assertEqual(values["--source-commit"], "6abb258e577c479325092a8ac0e7691fdfd154c2")
        self.assertEqual(
            self.system["system_version"],
            f"aster-rocksgraph@{values['--source-commit']}",
        )
        self.assertEqual(values["--binary"], self.system["binary"]["path"])
        self.assertEqual(values["--binary-sha256"], self.system["binary"]["sha256"])
        self.assertEqual(values["--dataset-sha256"], "9727fed3d710a3f0897b1ee7379411a091e0568bef05902de8b1787efc39d258")
        self.assertEqual(values["--store-label"], "aster")
        self.assertEqual(values["--p02b-max-age-seconds"], "3600")
        self.assertEqual(values["--block-cache-bytes"], "268435456")
        self.assertTrue(values["--store-manifest"].endswith("/aster-store-manifest.json"))
        self.assertTrue(values["--p02b-result"].endswith("/sentinel-result.json"))
        self.assertTrue(values["--p02b-validator"].endswith("/validate_sentinel_result.py"))
        self.assertTrue(values["--p31-wrapper"].endswith("/run_with_resources.sh"))

        self.assertEqual(
            self.system["store_roots"],
            [
                {
                    "label": "aster",
                    "path": "__ABSOLUTE_ASTER_SF10_STORE__",
                    "sha256": "__ASTER_LOGICAL_STORE_SHA256__",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
