#!/usr/bin/env python3
"""Build the external P20 correctness PASS consumed by measured profile runs.

This is phase 1 of the P20 protocol.  It consumes one current-schema,
digest-emitting correctness output for every canonical stage and never labels
those observations performance eligible.  ``run_single_profile.py`` is phase 2
and deliberately refuses to bootstrap this evidence from its measured run.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{16}$")
CANONICAL_STAGES = {
    "sf10": ["A0", "A1", "A2", "A3", "A4", "A5", "A6"],
    "sf30": ["A0", "A2", "A4", "A6"],
}


class GateError(ValueError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_sha(value, label):
    value = value.strip().lower()
    if not SHA256_RE.fullmatch(value):
        raise GateError("{} must be a SHA-256".format(label))
    return value


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GateError("duplicate JSON key: {}".format(key))
        result[key] = value
    return result


def load_json(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    except (OSError, json.JSONDecodeError, GateError) as exc:
        raise GateError("cannot read {}: {}".format(label, exc))
    if not isinstance(value, dict):
        raise GateError("{} must be a JSON object".format(label))
    return value


def verify_file(path, expected_sha, label):
    path = Path(path).resolve()
    if not path.is_file():
        raise GateError("{} must be a file".format(label))
    expected_sha = normalize_sha(expected_sha, label)
    actual = sha256_file(path)
    if actual != expected_sha:
        raise GateError("{} SHA-256 mismatch".format(label))
    return str(path), actual


def parse_observations(values, scale):
    observations = {}
    for raw in values:
        if "=" not in raw:
            raise GateError("observation must be STAGE=/absolute/output.json")
        stage, raw_path = raw.split("=", 1)
        if stage in observations:
            raise GateError("duplicate observation stage: {}".format(stage))
        path = Path(raw_path)
        if not path.is_absolute() or not path.is_file():
            raise GateError("observation must be an existing absolute file")
        observations[stage] = path.resolve()
    expected = CANONICAL_STAGES[scale]
    if list(observations) != expected:
        raise GateError("observations must be supplied once in canonical order: {}".format(expected))
    return observations


def validate_truth(truth, args, sample_sha):
    if (
        truth.get("schema_version") != 1
        or truth.get("digest_schema") != "storage-bench-result-digest-v1"
        or truth.get("scale") != args.scale
        or truth.get("workload") != args.workload
        or truth.get("sample_plan_sha256") != sample_sha
    ):
        raise GateError("truth schema/profile/sample-plan binding drift")
    expected_property_mode = "presence" if args.workload == "property-presence" else "none"
    if truth.get("property_predicate_mode") != expected_property_mode:
        raise GateError("truth property mode drift")
    if not isinstance(truth.get("property_id"), int) or isinstance(truth.get("property_id"), bool):
        raise GateError("truth property_id is invalid")
    entries = truth.get("entries")
    if not isinstance(entries, list) or not entries:
        raise GateError("truth entries are missing")
    return entries


def validate_output(raw, stage, truth_entries, truth, sample_plan_path):
    if (
        raw.get("query_control_stage") != stage
        or raw.get("sample_plan_version") != 1
        or raw.get("scan_requested") is not False
        or raw.get("emit_result_digests") is not True
        or raw.get("property_predicate_mode") != truth.get("property_predicate_mode")
        or raw.get("property_id") != truth.get("property_id")
    ):
        raise GateError("{} output schema/profile drift".format(stage))
    if Path(raw.get("sample_plan_in", sample_plan_path)).resolve() != Path(sample_plan_path).resolve():
        raise GateError("{} output sample-plan path drift".format(stage))
    benchmarks = raw.get("benchmarks")
    if not isinstance(benchmarks, list) or len(benchmarks) != len(truth_entries):
        raise GateError("{} truth/benchmark entry coverage drift".format(stage))
    checked = 0
    for entry_index, (expected, actual) in enumerate(zip(truth_entries, benchmarks)):
        if not isinstance(expected, dict) or not isinstance(actual, dict):
            raise GateError("{} entry is not an object".format(stage))
        for field in ("edge_type", "src_label", "dst_label"):
            if expected.get(field) != actual.get(field):
                raise GateError("{} entry {} identity drift".format(stage, entry_index))
        expected_entry_digest = expected.get("entry_result_digest")
        if (
            not DIGEST_RE.fullmatch(expected_entry_digest or "")
            or actual.get("entry_result_digest") != expected_entry_digest
        ):
            raise GateError("{} entry {} digest mismatch".format(stage, entry_index))
        expected_samples = expected.get("samples")
        actual_samples = actual.get("result_digests")
        if (
            not isinstance(expected_samples, list)
            or not expected_samples
            or not isinstance(actual_samples, list)
            or len(expected_samples) != len(actual_samples)
        ):
            raise GateError("{} entry {} sample coverage drift".format(stage, entry_index))
        for sample_index, (expected_sample, actual_sample) in enumerate(zip(expected_samples, actual_samples)):
            if not isinstance(expected_sample, dict) or not isinstance(actual_sample, dict):
                raise GateError("{} sample digest is not an object".format(stage))
            for field in ("src", "result_count", "result_digest"):
                if actual_sample.get(field) != expected_sample.get(field):
                    raise GateError(
                        "{} entry {} sample {} digest mismatch".format(stage, entry_index, sample_index)
                    )
            if not DIGEST_RE.fullmatch(expected_sample.get("result_digest", "")):
                raise GateError("truth sample digest is invalid")
            checked += 1
    return checked


def run(args):
    output = Path(args.output)
    if not output.is_absolute() or os.path.lexists(str(output)):
        raise GateError("output must be an absent absolute path")
    binary_path, binary_sha = verify_file(args.binary, args.binary_sha256, "binary")
    truth_path, truth_sha = verify_file(args.truth, args.truth_sha256, "truth")
    sample_path, sample_sha = verify_file(args.sample_plan, args.sample_plan_sha256, "sample plan")
    dataset_sha = normalize_sha(args.dataset_sha256, "dataset")
    dataset_path = Path(args.dataset).resolve()
    if not dataset_path.exists():
        raise GateError("dataset does not exist")
    if dataset_path.is_file() and sha256_file(dataset_path) != dataset_sha:
        raise GateError("dataset SHA-256 mismatch")
    truth = load_json(truth_path, "truth")
    truth_entries = validate_truth(truth, args, sample_sha)
    observations = parse_observations(args.observation, args.scale)
    observation_hashes = {}
    per_stage_checked = None
    for stage, path in observations.items():
        current_checked = validate_output(
            load_json(path, "{} correctness output".format(stage)),
            stage,
            truth_entries,
            truth,
            sample_path,
        )
        if per_stage_checked is None:
            per_stage_checked = current_checked
        elif current_checked != per_stage_checked:
            raise GateError("correctness output query counts differ across stages")
        observation_hashes[stage] = {"path": str(path), "sha256": sha256_file(path)}
    gate = {
        "schema_version": 1,
        "state": "PASS",
        "correctness_only": True,
        "performance_eligible": False,
        "generated_at_utc": utc_now(),
        "generator_sha256": sha256_file(Path(__file__).resolve()),
        "scale": args.scale,
        "workload": args.workload,
        "digest_schema": "storage-bench-result-digest-v1",
        "covered_stages": CANONICAL_STAGES[args.scale],
        "expected_queries_per_stage": per_stage_checked,
        "checked": per_stage_checked * len(observations),
        "mismatches": 0,
        "binary_path": binary_path,
        "binary_sha256": binary_sha,
        "dataset_path": str(dataset_path),
        "dataset_sha256": dataset_sha,
        "truth_path": truth_path,
        "truth_sha256": truth_sha,
        "sample_plan_path": sample_path,
        "sample_plan_sha256": sample_sha,
        "observations": observation_hashes,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(json.dumps(gate, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(temporary), str(output))
    print(str(output))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--scale", required=True, choices=("sf10", "sf30"))
    parser.add_argument(
        "--workload",
        required=True,
        choices=("typed-one-hop", "degree-stratified", "property-presence"),
    )
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--binary-sha256", required=True)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--dataset-sha256", required=True)
    parser.add_argument("--sample-plan", required=True, type=Path)
    parser.add_argument("--sample-plan-sha256", required=True)
    parser.add_argument("--truth", required=True, type=Path)
    parser.add_argument("--truth-sha256", required=True)
    parser.add_argument("--observation", action="append", default=[], metavar="STAGE=/ABS/OUTPUT.JSON")
    args = parser.parse_args()
    try:
        return run(args)
    except (GateError, OSError) as exc:
        print("FAIL: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
