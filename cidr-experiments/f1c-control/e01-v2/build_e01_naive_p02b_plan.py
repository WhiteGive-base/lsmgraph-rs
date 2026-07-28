#!/usr/bin/env python3
"""Freeze the naive/no-hint P02B plan with exact 1700-query equivalence."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


SCHEMA = "cidr-e01-naive-p02b-plan-equivalence-v1"
SOURCE_PLAN_SHA256 = "4520c88eb594903eb6e3f282838e6cd885e205ee5b0b1790565112d5940f8ea3"
FALSE_ELIGIBILITY = {
    "formal_eligible": False,
    "performance_eligible": False,
    "paper_claim_eligible": False,
}


class PlanError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PlanError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PlanError(f"invalid source plan: {exc}") from exc
    require(type(value) is dict, "source plan object required")
    return value


def sequence(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries = value.get("entries")
    require(type(entries) is list and entries, "entries array required")
    rows: list[dict[str, Any]] = []
    query_index = 0
    for entry_index, entry in enumerate(entries):
        require(type(entry) is dict, f"entry {entry_index}: object required")
        edge_type = entry.get("edge_type")
        require(type(edge_type) is int, f"entry {entry_index}: edge_type required")
        samples = entry.get("samples")
        require(type(samples) is list, f"entry {entry_index}: samples required")
        for sample_index, sample in enumerate(samples):
            require(type(sample) is dict, f"entry {entry_index} sample {sample_index}: object required")
            require(type(sample.get("src")) is int, "sample src required")
            require(type(sample.get("degree")) is int, "sample degree required")
            rows.append(
                {
                    "query_index": query_index,
                    "entry_index": entry_index,
                    "sample_index": sample_index,
                    "edge_type": edge_type,
                    "src": sample["src"],
                    "degree": sample["degree"],
                }
            )
            query_index += 1
    return rows


def sequence_sha(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def build(source_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    source_path = source_path.resolve()
    require(sha256_file(source_path) == SOURCE_PLAN_SHA256, "source plan SHA drift")
    source = load(source_path)
    require(source.get("semantic_degree_hint") is True, "source plan must be hint=true")
    rows = sequence(source)
    require(len(rows) == 1700, "source plan must contain exactly 1700 queries")
    target = copy.deepcopy(source)
    target["semantic_degree_hint"] = False
    target_rows = sequence(target)
    require(rows == target_rows, "query sequence changed")
    restored = copy.deepcopy(target)
    restored["semantic_degree_hint"] = True
    require(restored == source, "fields other than semantic_degree_hint changed")
    digest = sequence_sha(rows)
    receipt = {
        "schema_version": SCHEMA,
        "state": "PASS",
        "source_plan": {
            "path": str(source_path),
            "sha256": sha256_file(source_path),
            "size_bytes": source_path.stat().st_size,
        },
        "source_semantic_degree_hint": True,
        "target_semantic_degree_hint": False,
        "query_count": 1700,
        "entry_count": len(source["entries"]),
        "sequence_fields": ["query_index", "entry_index", "sample_index", "edge_type", "src", "degree"],
        "source_sequence_sha256": digest,
        "target_sequence_sha256": digest,
        "exact_sequence_equal": True,
        "only_changed_field": "semantic_degree_hint",
        "timing_generated": False,
        **FALSE_ELIGIBILITY,
    }
    return target, receipt


def atomic(path: Path, payload: bytes) -> None:
    require(not path.exists(), f"refusing to overwrite {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-plan", type=Path, required=True)
    parser.add_argument("--output-plan", type=Path, required=True)
    parser.add_argument("--output-receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan, receipt = build(args.source_plan)
        plan_payload = (json.dumps(plan, indent=2, sort_keys=True) + "\n").encode()
        receipt["target_plan"] = {
            "path": str(args.output_plan.resolve()),
            "sha256": hashlib.sha256(plan_payload).hexdigest(),
            "size_bytes": len(plan_payload),
        }
        atomic(args.output_plan, plan_payload)
        atomic(args.output_receipt, (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode())
        print(json.dumps({"state": "PASS", "query_count": 1700, "sequence_sha256": receipt["source_sequence_sha256"]}, sort_keys=True))
        return 0
    except (PlanError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
