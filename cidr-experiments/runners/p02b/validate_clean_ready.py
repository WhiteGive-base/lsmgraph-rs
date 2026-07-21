#!/usr/bin/env python3
"""Validate and cryptographically bind one P03 clean-window READY artifact."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from p02b_common import (
    GateError,
    atomic_write_json,
    file_ref,
    parse_env_file,
    resolved_existing_file,
    sha256_file,
)


REQUIRED_SAMPLE_COLUMNS = {
    "timestamp",
    "sample",
    "metric_pass",
    "service_pass",
    "sample_pass",
    "streak",
    "reasons",
    "gate_mode",
}


def parse_timestamp(raw: str, context: str) -> dt.datetime:
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        value = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise GateError("{} has invalid ISO-8601 timestamp {!r}".format(context, raw)) from exc
    if value.tzinfo is None:
        raise GateError("{} timestamp must include a timezone".format(context))
    return value.astimezone(dt.timezone.utc)


def read_tsv(path: Path) -> List[Dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if reader.fieldnames is None:
                raise GateError("{} has no TSV header".format(path))
            if len(reader.fieldnames) != len(set(reader.fieldnames)):
                raise GateError("{} has duplicate TSV columns".format(path))
            missing = sorted(REQUIRED_SAMPLE_COLUMNS - set(reader.fieldnames))
            if missing:
                raise GateError("{} is missing columns: {}".format(path, ", ".join(missing)))
            rows = list(reader)
    except OSError as exc:
        raise GateError("cannot read {}: {}".format(path, exc)) from exc
    if not rows:
        raise GateError("{} has no sample rows".format(path))
    return rows


def require_int(mapping: Dict[str, str], key: str, context: str) -> int:
    try:
        value = int(mapping[key])
    except (KeyError, ValueError) as exc:
        raise GateError("{} has invalid integer {}".format(context, key)) from exc
    return value


def validate_clean_ready(
    ready_path: Path,
    max_age_seconds: float,
    minimum_consecutive_samples: int,
    now: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    if max_age_seconds <= 0:
        raise GateError("max_age_seconds must be positive")
    if minimum_consecutive_samples < 1:
        raise GateError("minimum_consecutive_samples must be positive")

    ready = resolved_existing_file(ready_path, "P03 READY")
    run_dir = ready.parent
    p03_dir = run_dir.parent.parent
    expected_files = {
        "classification": run_dir / "classification.env",
        "state": run_dir / "STATE",
        "samples": run_dir / "samples.tsv",
        "latest": run_dir / "latest.tsv",
        "complete": run_dir / "COMPLETE",
        "monitor_script": p03_dir / "monitor_clean_window.sh",
    }
    for name, path in expected_files.items():
        resolved_existing_file(path, "P03 {}".format(name))
    if (run_dir / "STOPPED").exists():
        raise GateError("P03 run has STOPPED marker: {}".format(run_dir / "STOPPED"))

    ready_env = parse_env_file(ready)
    classification = parse_env_file(expected_files["classification"])
    state = parse_env_file(expected_files["state"])
    complete = parse_env_file(expected_files["complete"])

    if ready_env.get("readiness_gate") != "PASS":
        raise GateError("P03 READY does not declare readiness_gate=PASS")
    if ready_env.get("performance_eligible") != "false":
        raise GateError("P03 READY must remain performance_eligible=false")
    if ready_env.get("gate_mode") != "seml0":
        raise GateError("P03 READY gate_mode must be seml0")
    if classification.get("purpose") != "clean_window_readiness_only":
        raise GateError("P03 classification purpose is not clean_window_readiness_only")
    if classification.get("performance_eligible") != "false":
        raise GateError("P03 classification must remain performance_eligible=false")
    if classification.get("gate_mode") != "seml0":
        raise GateError("P03 classification gate_mode must be seml0")

    run_id = ready_env.get("run_id", "")
    if not run_id or classification.get("run_id") != run_id or complete.get("run_id") != run_id:
        raise GateError("P03 run_id is absent or inconsistent across markers")
    if state.get("performance_eligible") != "false":
        raise GateError("P03 STATE must remain performance_eligible=false")
    if state.get("sample_pass") != "1" or state.get("reasons") != "none":
        raise GateError("P03 STATE is not a clean passing sample")

    script_digest = sha256_file(expected_files["monitor_script"])
    if classification.get("script_sha256") != script_digest:
        raise GateError("P03 monitor script SHA-256 differs from classification.env")

    declared_latest = ready_env.get("latest_sample", "")
    if not declared_latest or Path(declared_latest).resolve() != expected_files["latest"].resolve():
        raise GateError("P03 READY latest_sample does not bind the adjacent latest.tsv")

    try:
        configured_ready_samples = int(classification["ready_samples"])
        ready_passes = int(ready_env["consecutive_passes"])
        ready_sample_count = int(ready_env["samples"])
        state_streak = int(state["streak"])
        state_required = int(state["required_streak"])
    except (KeyError, ValueError) as exc:
        raise GateError("P03 markers contain invalid sample/streak fields") from exc
    required = max(minimum_consecutive_samples, configured_ready_samples, state_required)
    if ready_passes < required or state_streak < required:
        raise GateError(
            "P03 clean streak is too short: ready={} state={} required={}".format(
                ready_passes, state_streak, required
            )
        )

    rows = read_tsv(expected_files["samples"])
    latest_rows = read_tsv(expected_files["latest"])
    if len(latest_rows) != 1:
        raise GateError("P03 latest.tsv must contain exactly one row")
    latest = latest_rows[0]
    final = rows[-1]
    for key in latest:
        if latest.get(key) != final.get(key):
            raise GateError("P03 latest.tsv differs from final samples.tsv row at {}".format(key))
    if require_int(final, "sample", "P03 final sample") != ready_sample_count:
        raise GateError("P03 READY sample count differs from samples.tsv")
    if require_int(final, "streak", "P03 final sample") < required:
        raise GateError("P03 final samples.tsv streak is too short")
    if final.get("gate_mode") != "seml0":
        raise GateError("P03 final sample gate_mode is not seml0")

    passing_tail = rows[-required:]
    if len(passing_tail) != required:
        raise GateError("P03 samples.tsv does not retain the required passing tail")
    prior_sample = None
    for index, row in enumerate(passing_tail, start=1):
        if row.get("metric_pass") != "1" or row.get("service_pass") != "1":
            raise GateError("P03 passing tail row {} did not pass metric/service gates".format(index))
        if row.get("sample_pass") != "1" or row.get("reasons") != "none":
            raise GateError("P03 passing tail row {} is not clean".format(index))
        sample_number = require_int(row, "sample", "P03 passing tail")
        if prior_sample is not None and sample_number != prior_sample + 1:
            raise GateError("P03 passing tail sample numbers are not contiguous")
        prior_sample = sample_number

    ready_time = parse_timestamp(ready_env.get("ready_time", ""), "P03 READY")
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    age_seconds = (current.astimezone(dt.timezone.utc) - ready_time).total_seconds()
    if age_seconds < -60:
        raise GateError("P03 READY timestamp is in the future")
    if age_seconds > max_age_seconds:
        raise GateError(
            "P03 READY is stale: age {:.1f}s exceeds {:.1f}s".format(age_seconds, max_age_seconds)
        )

    return {
        "schema_version": "p02b-clean-ready-binding-v1",
        "state": "PASS",
        "run_id": run_id,
        "ready_time": ready_env["ready_time"],
        "age_seconds_at_binding": max(0.0, age_seconds),
        "required_consecutive_samples": required,
        "observed_consecutive_samples": ready_passes,
        "git_head": classification.get("git_head"),
        "host": classification.get("host"),
        "artifacts": {
            name: file_ref(path)
            for name, path in {
                "READY": ready,
                "COMPLETE": expected_files["complete"],
                "classification.env": expected_files["classification"],
                "STATE": expected_files["state"],
                "samples.tsv": expected_files["samples"],
                "latest.tsv": expected_files["latest"],
                "monitor_clean_window.sh": expected_files["monitor_script"],
            }.items()
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ready", required=True, type=Path)
    parser.add_argument("--max-age-seconds", required=True, type=float)
    parser.add_argument("--minimum-consecutive-samples", required=True, type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = validate_clean_ready(
            args.ready,
            args.max_age_seconds,
            args.minimum_consecutive_samples,
        )
        if args.output:
            atomic_write_json(args.output, result)
        else:
            import json

            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except GateError as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
