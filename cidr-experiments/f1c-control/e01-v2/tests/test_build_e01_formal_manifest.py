#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any


MODULE_PATH = Path(__file__).resolve().parents[1] / "build_e01_formal_manifest.py"
SPEC = importlib.util.spec_from_file_location("build_e01_formal_manifest", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


FALSE = {
    "formal_eligible": False,
    "performance_eligible": False,
    "paper_claim_eligible": False,
}


def write_json(path: Path, value: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


class E01FormalManifestBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="e01-builder-")
        self.root = Path(self.temp.name)
        self.host = "a" * 64
        self.git = "b" * 40
        self.input_sha = "c" * 64
        self.trace_sha = "d" * 64
        self.protocol = {
            "dataset_id": "LDBC-SNB-SF10",
            "scale_factor": 10,
            "vertex_count": 1000000,
            "directed_edge_count": 10000000,
            "property_count": 0,
            "query_count": 1700,
            "input_sha256": self.input_sha,
            "workload_id": "typed-neighbor-s50-v2",
            "query_trace_sha256": self.trace_sha,
            "seed": 42,
            "cache_state": "warm",
            "concurrency": 1,
            "interface_scope": "typed-neighbor-dense-id-v1",
            "host_fingerprint": self.host,
            "harness_git_sha": self.git,
        }
        self.protocol_sha = builder.canonical_sha256(self.protocol)
        self.campaign = "E01-FORMAL-TEST"
        common = {
            "state": "PASS",
            "campaign_id": self.campaign,
            "protocol_sha256": self.protocol_sha,
            "host_fingerprint": self.host,
            "git_sha": self.git,
            "fresh": True,
            **FALSE,
        }
        self.p03_path = self.root / "receipts/p03.json"
        p03_sha = write_json(
            self.p03_path,
            {"schema_version": "cidr-e01-p03-clean-ready-v1", **common},
        )
        self.gate_path = self.root / "receipts/p02b.json"
        gate_sha = write_json(
            self.gate_path,
            {
                "schema_version": "cidr-e01-p02b-gate-v1",
                "p03_receipt_sha256": p03_sha,
                **common,
            },
        )
        self.lease_path = self.root / "receipts/lease.json"
        lease_sha = write_json(
            self.lease_path,
            {
                "schema_version": "cidr-e01-formal-lease-v1",
                "p02b_gate_sha256": gate_sha,
                "exclusive_single_host": True,
                "serial_formal_timing": True,
                **common,
            },
        )
        self.marker_path = self.root / "receipts/lease.PASS.json"
        write_json(
            self.marker_path,
            {
                "schema_version": "cidr-e01-formal-lease-marker-v1",
                "state": "PASS",
                "campaign_id": self.campaign,
                "lease_sha256": lease_sha,
                **FALSE,
            },
        )
        self.dataset_path = self.root / "seals/dataset.json"
        self.trace_path = self.root / "seals/trace.json"
        self._write_seal(self.dataset_path, "dataset", self.protocol["dataset_id"], self.input_sha)
        self._write_seal(self.trace_path, "query_trace", self.protocol["workload_id"], self.trace_sha)
        self.systems = []
        for index, (key, name) in enumerate(builder.SYSTEMS):
            binary_sha = f"{100 + index:064x}"
            store_sha = f"{200 + index:064x}"
            binary_path = self.root / f"seals/{key}-binary.json"
            store_path = self.root / f"seals/{key}-store.json"
            self._write_seal(binary_path, "binary", key, binary_sha)
            self._write_seal(store_path, "store", key, store_sha)
            self.systems.append(
                {
                    "system_key": key,
                    "display_name": name,
                    "variant": "naive" if key == "seml0-naive" else "formal-default",
                    "binary_sha256": binary_sha,
                    "store_sha256": store_sha,
                    "binary_seal": str(binary_path),
                    "store_seal": str(store_path),
                }
            )
        self.spec = {
            "schema_version": "cidr-e01-formal-launch-spec-v1",
            "state": "READY",
            "campaign_id": self.campaign,
            "created_at_utc": "2026-07-27T00:00:00Z",
            "classification": dict(FALSE),
            "protocol": self.protocol,
            "repeat_indices": [1, 2, 3],
            "serial_formal_timing": True,
            "legacy_conditional_evidence_allowed": False,
            "admission": {
                "p03_clean_ready": str(self.p03_path),
                "p02b_gate": str(self.gate_path),
                "batch_lease": str(self.lease_path),
                "lease_marker": str(self.marker_path),
            },
            "dataset_seal": str(self.dataset_path),
            "trace_seal": str(self.trace_path),
            "systems": self.systems,
        }
        self.spec_path = self.root / "campaign-spec.json"
        self.output = self.root / "E01-launch-manifest.json"
        write_json(self.spec_path, self.spec)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_seal(self, path: Path, kind: str, asset_id: str, content_sha: str) -> None:
        write_json(
            path,
            {
                "schema_version": "cidr-e01-asset-seal-v1",
                "state": "SEALED",
                "campaign_id": self.campaign,
                "asset_kind": kind,
                "asset_id": asset_id,
                "protocol_sha256": self.protocol_sha,
                "content_sha256": content_sha,
                "immutable": True,
                "fresh_for_campaign": True,
                **FALSE,
            },
        )

    def rewrite_spec(self) -> None:
        write_json(self.spec_path, self.spec)

    def build(self) -> dict[str, Any]:
        return builder.build_manifest(self.spec_path, self.output)

    def test_valid_manifest_freezes_21_runs_in_fixed_order(self) -> None:
        manifest = self.build()
        expected = [
            f"{key}:r{repeat}"
            for key, _ in builder.SYSTEMS
            for repeat in builder.REPEATS
        ]
        self.assertEqual([row["run_key"] for row in manifest["runs"]], expected)
        self.assertEqual(manifest["run_count"], 21)
        self.assertEqual(manifest["state"], "PASS")
        self.assertTrue(manifest["serial_formal_timing"])
        self.assertFalse(manifest["formal_eligible"])
        self.assertFalse(manifest["performance_eligible"])
        self.assertFalse(manifest["paper_claim_eligible"])

    def test_conditional_eligibility_promotion_is_rejected(self) -> None:
        self.spec["classification"]["formal_eligible"] = True
        self.rewrite_spec()
        with self.assertRaises(builder.BuildError):
            self.build()

    def test_system_order_drift_is_rejected(self) -> None:
        self.spec["systems"][0], self.spec["systems"][1] = (
            self.spec["systems"][1],
            self.spec["systems"][0],
        )
        self.rewrite_spec()
        with self.assertRaises(builder.BuildError):
            self.build()

    def test_repeat_drift_is_rejected(self) -> None:
        self.spec["repeat_indices"] = [1, 2]
        self.rewrite_spec()
        with self.assertRaises(builder.BuildError):
            self.build()

    def test_nonfresh_p03_is_rejected(self) -> None:
        value = json.loads(self.p03_path.read_text())
        value["fresh"] = False
        write_json(self.p03_path, value)
        with self.assertRaises(builder.BuildError):
            self.build()

    def test_p02b_to_p03_lineage_drift_is_rejected(self) -> None:
        value = json.loads(self.gate_path.read_text())
        value["p03_receipt_sha256"] = "9" * 64
        write_json(self.gate_path, value)
        with self.assertRaises(builder.BuildError):
            self.build()

    def test_lease_to_gate_lineage_drift_is_rejected(self) -> None:
        value = json.loads(self.lease_path.read_text())
        value["p02b_gate_sha256"] = "9" * 64
        write_json(self.lease_path, value)
        with self.assertRaises(builder.BuildError):
            self.build()

    def test_lease_marker_drift_is_rejected(self) -> None:
        value = json.loads(self.marker_path.read_text())
        value["lease_sha256"] = "9" * 64
        write_json(self.marker_path, value)
        with self.assertRaises(builder.BuildError):
            self.build()

    def test_unsealed_store_is_rejected(self) -> None:
        path = Path(self.systems[0]["store_seal"])
        value = json.loads(path.read_text())
        value["immutable"] = False
        write_json(path, value)
        with self.assertRaises(builder.BuildError):
            self.build()

    def test_binary_sha_drift_is_rejected(self) -> None:
        self.spec["systems"][0]["binary_sha256"] = "9" * 64
        self.rewrite_spec()
        with self.assertRaises(builder.BuildError):
            self.build()

    def test_store_sha_drift_is_rejected(self) -> None:
        self.spec["systems"][0]["store_sha256"] = "9" * 64
        self.rewrite_spec()
        with self.assertRaises(builder.BuildError):
            self.build()

    def test_existing_output_is_not_overwritten(self) -> None:
        self.output.write_text("preserve\n")
        with self.assertRaises(FileExistsError):
            self.build()
        self.assertEqual(self.output.read_text(), "preserve\n")

    def test_receipt_symlink_is_rejected(self) -> None:
        link = self.root / "receipts/p03-link.json"
        try:
            link.symlink_to(self.p03_path)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable")
        self.spec["admission"]["p03_clean_ready"] = str(link)
        self.rewrite_spec()
        with self.assertRaises(builder.BuildError):
            self.build()


if __name__ == "__main__":
    unittest.main()
