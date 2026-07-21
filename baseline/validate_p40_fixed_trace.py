#!/usr/bin/env python3
"""Cross-arm and cross-repeat correctness gate for P40 fixed-trace runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_arm(path: Path) -> dict[str, Any]:
    queries: list[dict[str, Any]] = []
    starts: list[dict[str, Any]] = []
    done: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            event = record.get("event")
            if event == "start":
                starts.append(record)
            elif event == "query":
                queries.append(record)
            elif event == "done":
                done.append(record)
    if len(starts) != 1 or len(done) != 1:
        raise ValueError(f"{path}: expected one start and one done event")
    if not queries:
        raise ValueError(f"{path}: no query events")
    keys = [
        (
            int(record["seq"]),
            int(record["src"]),
            int(record["edge_type"]),
            int(record["result_count"]),
            str(record["result_digest"]),
        )
        for record in queries
    ]
    if len({key[0] for key in keys}) != len(keys):
        raise ValueError(f"{path}: duplicate query seq")
    if int(done[0].get("queries", -1)) != len(keys):
        raise ValueError(f"{path}: done query count does not match query events")
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "policy": starts[0].get("policy"),
        "trace_sequence_digest": starts[0].get("trace_sequence_digest"),
        "query_digest": done[0].get("query_digest"),
        "maintenance_actions": done[0].get("maintenance_actions"),
        "queries": keys,
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--trace-sha256", required=True)
    parser.add_argument("--repeats", type=int, required=True)
    parser.add_argument(
        "--arms",
        nargs="+",
        default=["none", "capacity-naive", "semantic-static", "semantic-feedback"],
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--git-head", required=True)
    parser.add_argument("--binary-sha256", required=True)
    parser.add_argument("--performance-eligible", choices=["true", "false"], required=True)
    parser.add_argument("--p31-collector", default="")
    args = parser.parse_args()

    actual_trace_sha = sha256_file(args.trace)
    if actual_trace_sha != args.trace_sha256:
        raise ValueError(
            f"trace SHA mismatch: actual={actual_trace_sha} expected={args.trace_sha256}"
        )

    loaded: dict[str, dict[str, Any]] = {}
    mismatches: list[dict[str, Any]] = []
    repeat_rows: list[dict[str, Any]] = []
    global_reference: list[tuple[int, int, int, int, str]] | None = None

    for repeat in range(1, args.repeats + 1):
        repeat_name = f"repeat-{repeat:02d}"
        arm_rows: dict[str, Any] = {}
        reference: list[tuple[int, int, int, int, str]] | None = None
        reference_arm = args.arms[0]
        for arm in args.arms:
            path = args.run_root / repeat_name / f"{arm}.jsonl"
            arm_data = load_arm(path)
            loaded[f"{repeat_name}/{arm}"] = arm_data
            arm_rows[arm] = {
                key: arm_data[key]
                for key in (
                    "path",
                    "sha256",
                    "policy",
                    "trace_sequence_digest",
                    "query_digest",
                    "maintenance_actions",
                )
            }
            queries = arm_data["queries"]
            if reference is None:
                reference = queries
                if global_reference is None:
                    global_reference = queries
            elif queries != reference:
                for index, (expected, actual) in enumerate(zip(reference, queries)):
                    if expected != actual:
                        mismatches.append(
                            {
                                "repeat": repeat,
                                "reference_arm": reference_arm,
                                "arm": arm,
                                "query_index": index,
                                "expected": expected,
                                "actual": actual,
                            }
                        )
                        if len(mismatches) >= 50:
                            break
                if len(queries) != len(reference) and len(mismatches) < 50:
                    mismatches.append(
                        {
                            "repeat": repeat,
                            "reference_arm": reference_arm,
                            "arm": arm,
                            "expected_query_count": len(reference),
                            "actual_query_count": len(queries),
                        }
                    )
            if global_reference is not None and queries != global_reference and len(mismatches) < 50:
                mismatches.append(
                    {
                        "repeat": repeat,
                        "arm": arm,
                        "kind": "cross-repeat-query-sequence-mismatch",
                    }
                )
        repeat_rows.append({"repeat": repeat, "arms": arm_rows})

    trace_digests = {row["trace_sequence_digest"] for row in loaded.values()}
    if len(trace_digests) != 1:
        mismatches.append({"kind": "trace-sequence-digest-mismatch", "values": sorted(trace_digests)})

    result = {
        "gate": "PASS" if not mismatches else "FAIL",
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "repeats": args.repeats,
        "arms": args.arms,
        "query_count": len(global_reference or []),
        "trace_sha256": actual_trace_sha,
        "repeat_results": repeat_rows,
        "performance_eligible": False,
    }
    write_json(args.output, result)

    manifest = {
        "schema_version": 1,
        "run_id": args.run_id,
        "git_head": args.git_head,
        "binary_sha256": args.binary_sha256,
        "trace_path": str(args.trace),
        "trace_sha256": actual_trace_sha,
        "repeats": args.repeats,
        "arms": args.arms,
        "correctness_gate": result["gate"],
        "mismatch_count": len(mismatches),
        "performance_eligible": args.performance_eligible == "true",
        "p31_collector": args.p31_collector or None,
        "host": platform.node(),
        "outputs": {
            key: {name: value for name, value in row.items() if name != "queries"}
            for key, row in sorted(loaded.items())
        },
    }
    write_json(args.manifest_output, manifest)
    print(json.dumps({"gate": result["gate"], "mismatches": len(mismatches)}, sort_keys=True))
    return 0 if not mismatches else 1


if __name__ == "__main__":
    raise SystemExit(main())
