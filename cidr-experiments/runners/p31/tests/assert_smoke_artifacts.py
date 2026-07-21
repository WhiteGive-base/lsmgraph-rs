#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--expect", required=True, choices=["pass", "failed"])
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    manifest = json.loads((run_dir / "run-manifest.json").read_text(encoding="utf-8"))
    validation = json.loads((run_dir / "validation.json").read_text(encoding="utf-8"))
    if args.expect == "failed":
        assert (run_dir / "FAILED").is_file(), "FAILED marker is missing"
        assert not (run_dir / "DONE").exists(), "failed run retained a DONE marker"
        assert manifest["state"] != "PASS"
        assert validation["state"] == "FAILED"
        assert validation["errors"], "failed validation has no reason"
        return 0

    assert (run_dir / "DONE").is_file(), "DONE marker is missing"
    assert not (run_dir / "FAILED").exists(), "successful run has FAILED marker"
    assert manifest["state"] == "PASS"
    assert validation["state"] == "PASS"
    done = json.loads((run_dir / "DONE").read_text(encoding="utf-8"))
    for filename, key in (
        ("run-manifest.json", "manifest_sha256"),
        ("validation.json", "validation_sha256"),
    ):
        digest = hashlib.sha256((run_dir / filename).read_bytes()).hexdigest()
        assert done[key] == digest, f"DONE hash mismatch for {filename}"
    resources = validation["resource_summary"]
    disks = validation["disk_summary"]
    assert resources["samples"] >= 3
    assert resources["peak_process_count"] >= 2
    assert resources["peak_pss_bytes"] > 0
    assert resources["process_write_bytes"] > 0
    assert validation["iostat_samples"] > 0
    assert disks["peak_store_total_bytes"] > 0
    assert disks["peak_temp_bytes"] > 0
    assert disks["roots"]["store:fixture-store"]["peak_payload_bytes"] > 0
    assert disks["roots"]["store:fixture-store"]["peak_sidecar_bytes"] > 0
    assert disks["roots"]["temp:fixture-temp"]["peak_temp_bytes"] > 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
