#!/usr/bin/env python3
"""Downstream P10/P20 admission check for a P02B sentinel result."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

from p02b_common import GateError, read_json, same_resolved_path, sha256_file


def validate_result(result_path: Path, consumer: str, require_formal: bool) -> Dict[str, Any]:
    result_path = result_path.resolve()
    result = read_json(result_path)
    if result.get("schema_version") != "p02b-sf10-sentinel-result-v1":
        raise GateError("sentinel result has wrong schema_version")
    if result.get("state") != "PASS":
        raise GateError("sentinel result is not PASS")
    if result.get("performance_eligible") is not False:
        raise GateError("sentinel gate must not claim paper-performance eligibility")
    if consumer not in {"P10", "P20"} or consumer not in result.get("consumers", []):
        raise GateError("sentinel result does not release consumer {}".format(consumer))
    fixture = result.get("fixture_only") is True
    if require_formal:
        if fixture:
            raise GateError("fixture sentinel cannot release a formal downstream run")
        if result.get("formal_gate_eligible") is not True:
            raise GateError("sentinel is not formal-gate eligible")
        if result.get("downstream_release_eligible") is not True:
            raise GateError("sentinel has not released downstream experiments")

    correctness = result.get("correctness", {})
    if correctness.get("state") != "PASS" or correctness.get("mismatches") != 0:
        raise GateError("sentinel shared-truth correctness gate failed")
    stability = result.get("stability", {})
    if stability.get("state") != "PASS":
        raise GateError("sentinel stability gate failed")
    if not stability.get("qps", {}).get("pass") or not stability.get("p99_us", {}).get("pass"):
        raise GateError("sentinel CV sub-gate failed")

    marker_name = "FIXTURE-PASS" if fixture else "PASS"
    marker_path = result_path.parent / marker_name
    marker = read_json(marker_path)
    if marker.get("state") != "PASS" or marker.get("fixture_only") is not fixture:
        raise GateError("sentinel marker classification is inconsistent")
    if not same_resolved_path(marker.get("result"), result_path):
        raise GateError("sentinel marker points to another result")
    if marker.get("result_sha256") != sha256_file(result_path):
        raise GateError("sentinel marker does not bind sentinel-result.json")
    if (result_path.parent / "FAILED").exists():
        raise GateError("sentinel run has a FAILED marker")
    return {
        "state": "PASS",
        "consumer": consumer,
        "formal_required": require_formal,
        "fixture_only": fixture,
        "sentinel_result": str(result_path),
        "sentinel_result_sha256": sha256_file(result_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--consumer", required=True, choices=("P10", "P20"))
    parser.add_argument("--require-formal", action="store_true")
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                validate_result(args.result, args.consumer, args.require_formal),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except GateError as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
