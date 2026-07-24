#!/usr/bin/env python3
"""Normalize validated P20 run rows into the strict Figure 2 plotting schema.

The P20 aggregator deliberately emits measurement/provenance rows rather than
paper-plot COMMON metadata.  This adapter joins those rows back to their
content-addressed entry summaries and P31 manifests, derives only fields those
artifacts prove, and requires the remaining campaign facts in one frozen JSON
metadata document.
"""

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import aggregate_figure2 as aggregate  # noqa: E402


METADATA_SCHEMA = "p20-figure2-formal-metadata-v1"
P31_MANIFEST_SCHEMA = "cidr-run-manifest-v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
SCALE_RE = re.compile(r"^sf([1-9][0-9]*)$")
CACHE_STATES = {"cold", "warm", "fixed_budget"}
PROPERTY_COHORTS = {
    "mixed_positive",
    "zero",
    "balanced_mixed_zero",
    "not_applicable",
}

METADATA_TOP_LEVEL = {
    "schema_version",
    "run_input_sha256",
    "selection",
    "common",
    "property_cohort",
}
SELECTION_FIELDS = {
    "scale",
    "workload",
    "property_predicate_mode",
    "property_id",
}
EXPLICIT_COMMON_FIELDS = {
    "experiment_id",
    "system",
    "dataset_id",
    "vertex_count",
    "directed_edge_count",
    "property_count",
    "workload_id",
    "seed",
    "cache_state",
    "warmup_s",
}

SUMMARY_CONSTANTS = (
    "summary_schema_version",
    "run_id",
    "repeat_index",
    "scale",
    "stage",
    "mode",
    "workload",
    "property_predicate_mode",
    "property_id",
    "performance_eligible",
    "concurrency",
    "warmup_runs",
    "host_fingerprint_sha256",
    "git_sha",
    "feature_switches_json",
    "current_digest_pass",
    "current_digest_mismatches",
    "binary_sha256",
    "dataset_sha256",
    "sample_plan_sha256",
    "truth_sha256",
    "p31_manifest_sha256",
)
SUMMARY_REQUIRED = set(SUMMARY_CONSTANTS) | {
    "benchmark_entry_index",
    "elapsed_ms",
    "measured_operations",
    "candidate_segments_total",
    "body_read_segments_total",
    "logical_body_bytes_total",
    "get_neighbors_latency_histogram_json",
    "query_cpu_query_setup_ns",
    "query_cpu_metadata_admission_ns",
    "query_cpu_routing_index_ns",
    "query_cpu_body_decode_filter_ns",
    "query_cpu_mvcc_result_ns",
    "query_cpu_total_ns",
}

COMMON_COLUMNS = [
    "experiment_id",
    "run_id",
    "repeat_index",
    "timestamp_utc",
    "host_fingerprint",
    "git_sha",
    "binary_sha256",
    "system",
    "variant",
    "dataset_id",
    "scale_factor",
    "vertex_count",
    "directed_edge_count",
    "property_count",
    "input_sha256",
    "workload_id",
    "query_trace_sha256",
    "seed",
    "cache_state",
    "concurrency",
    "warmup_s",
    "measurement_s",
    "digest_pass",
    "mismatch_count",
    "unsupported_reason",
]
FIGURE2_COLUMNS = [
    "ablation_stage",
    "property_cohort",
    "feature_switches",
    "latency_p99_us",
    "candidate_segments_total",
    "body_read_segments_total",
    "measured_operations",
    "body_read_bytes_total",
    "device_read_bytes_total",
    "cpu_query_setup_ns",
    "cpu_admission_ns",
    "cpu_routing_ns",
    "cpu_body_decode_filter_ns",
    "cpu_mvcc_result_ns",
    "cpu_total_ns",
]
PROVENANCE_COLUMNS = [
    "normalization_schema_version",
    "normalization_metadata_sha256",
    "aggregate_run_input_sha256",
    "latency_summary_sha256",
    "cpu_phase_summary_sha256",
    "p31_manifest_sha256",
    "dataset_sha256",
    "sample_plan_sha256",
    "truth_sha256",
    "correctness_pass_sha256",
    "profiles_sha256",
    "source_scale",
    "source_workload",
    "source_property_predicate_mode",
    "source_property_id",
]
OUTPUT_COLUMNS = COMMON_COLUMNS + FIGURE2_COLUMNS + PROVENANCE_COLUMNS


class NormalizeError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise NormalizeError(message)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "duplicate JSON key: {}".format(key))
        value[key] = item
    return value


def load_json(path, label):
    path = Path(path).resolve()
    require(path.is_file(), "{} does not exist: {}".format(label, path))
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_float=Decimal,
            parse_constant=lambda token: (_ for _ in ()).throw(
                NormalizeError("non-standard JSON constant: {}".format(token))
            ),
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise NormalizeError("cannot parse {} JSON: {}".format(label, exc))
    require(isinstance(value, dict), "{} must be a JSON object".format(label))
    return path, value


def load_tsv(path, label, required_fields):
    path = Path(path).resolve()
    require(path.is_file(), "{} does not exist: {}".format(label, path))
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        header = reader.fieldnames or []
        require(header and all(header), "{} has an invalid/empty header".format(label))
        require(len(header) == len(set(header)), "{} has duplicate columns".format(label))
        require(
            set(required_fields) <= set(header),
            "{} is missing fields: {}".format(
                label, sorted(set(required_fields) - set(header))
            ),
        )
        rows = []
        for line_number, raw in enumerate(reader, start=2):
            require(None not in raw, "{} line {} has excess cells".format(label, line_number))
            row = {name: (value or "").strip() for name, value in raw.items()}
            row["__line__"] = str(line_number)
            rows.append(row)
    require(rows, "{} is empty".format(label))
    return path, header, rows


def integer(value, label, minimum=0):
    if isinstance(value, bool):
        raise NormalizeError("{} must be an integer".format(label))
    if isinstance(value, Decimal):
        require(value == value.to_integral_value(), "{} must be an integer".format(label))
    if isinstance(value, float):
        require(value.is_integer(), "{} must be an integer".format(label))
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise NormalizeError("{} must be an integer".format(label))
    if isinstance(value, str):
        require(str(parsed) == value, "{} must be a canonical integer".format(label))
    require(parsed >= minimum, "{} must be >= {}".format(label, minimum))
    return parsed


def decimal_value(value, label, positive=False):
    if isinstance(value, bool):
        raise NormalizeError("{} must be numeric".format(label))
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise NormalizeError("{} must be numeric".format(label))
    require(parsed.is_finite(), "{} must be finite".format(label))
    require(parsed > 0 if positive else parsed >= 0, "{} is out of range".format(label))
    return parsed


def decimal_text(value):
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def normalize_sha(value, label):
    require(isinstance(value, str), "{} must be a string".format(label))
    normalized = value.strip().lower()
    require(bool(SHA256_RE.fullmatch(normalized)), "{} must be 64 lowercase hex".format(label))
    return normalized


def validate_utc(value, label):
    require(isinstance(value, str) and value.strip(), "{} must be non-empty".format(label))
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise NormalizeError("{} must be ISO-8601".format(label))
    require(parsed.tzinfo is not None, "{} must carry a UTC offset".format(label))
    require(
        parsed.utcoffset() == timezone.utc.utcoffset(parsed),
        "{} must carry UTC/Z".format(label),
    )
    return value.strip()


def nonempty_string(value, label):
    require(isinstance(value, str) and value.strip(), "{} must be non-empty".format(label))
    return value.strip()


def load_metadata(path, run_input_sha256):
    path, value = load_json(path, "normalization metadata")
    require(
        set(value) == METADATA_TOP_LEVEL,
        "metadata top-level fields drift: expected {}, got {}".format(
            sorted(METADATA_TOP_LEVEL), sorted(value)
        ),
    )
    require(value["schema_version"] == METADATA_SCHEMA, "metadata schema version drift")
    expected_run_sha = normalize_sha(value["run_input_sha256"], "run_input_sha256")
    require(expected_run_sha == run_input_sha256, "metadata does not bind this aggregate run TSV")

    selection = value["selection"]
    require(isinstance(selection, dict), "metadata.selection must be an object")
    require(set(selection) == SELECTION_FIELDS, "metadata.selection fields drift")
    selection = {
        name: nonempty_string(selection[name], "selection.{}".format(name))
        for name in sorted(SELECTION_FIELDS)
    }
    require(selection["scale"] == "sf10", "formal A0--A6 plot normalization requires scale=sf10")

    common = value["common"]
    require(isinstance(common, dict), "metadata.common must be an object")
    require(set(common) == EXPLICIT_COMMON_FIELDS, "metadata.common fields drift")
    for name in ("experiment_id", "system", "dataset_id", "workload_id"):
        common[name] = nonempty_string(common[name], "common.{}".format(name))
    common["vertex_count"] = integer(common["vertex_count"], "common.vertex_count", 1)
    common["directed_edge_count"] = integer(
        common["directed_edge_count"], "common.directed_edge_count", 1
    )
    property_count = common["property_count"]
    if property_count is not None:
        property_count = integer(property_count, "common.property_count", 0)
    common["property_count"] = property_count
    common["seed"] = integer(common["seed"], "common.seed", 0)
    common["cache_state"] = nonempty_string(common["cache_state"], "common.cache_state")
    require(common["cache_state"] in CACHE_STATES, "common.cache_state is not canonical")
    common["warmup_s"] = decimal_value(common["warmup_s"], "common.warmup_s")

    cohort = nonempty_string(value["property_cohort"], "property_cohort")
    require(cohort in PROPERTY_COHORTS, "property_cohort is not canonical")
    if selection["workload"] == "property-presence":
        require(cohort != "not_applicable", "property workload requires an explicit property cohort")
        require(property_count is not None, "property workload requires common.property_count")
    else:
        require(cohort == "not_applicable", "non-property workload must use not_applicable cohort")

    return {
        "path": path,
        "sha256": sha256_file(path),
        "selection": selection,
        "common": common,
        "property_cohort": cohort,
    }


def load_run_input(path):
    path, _, rows = load_tsv(path, "aggregate run input", aggregate.RUN_COLUMNS)
    seen = set()
    for row in rows:
        require(row["figure2_schema_version"] == "1", "aggregate run schema version drift")
        key = tuple(
            row[name]
            for name in (
                "scale",
                "ablation_stage",
                "workload",
                "property_predicate_mode",
                "property_id",
                "repeat_index",
            )
        )
        require(key not in seen, "duplicate aggregate run key: {}".format(key))
        seen.add(key)
    return path, sha256_file(path), rows


def load_summary(path):
    path, _, rows = load_tsv(path, "P20 summary", SUMMARY_REQUIRED)
    first = rows[0]
    for row in rows[1:]:
        for field in SUMMARY_CONSTANTS:
            require(
                row[field] == first[field],
                "{} changes {} across entry rows".format(path, field),
            )
    indices = [integer(row["benchmark_entry_index"], "benchmark_entry_index") for row in rows]
    require(indices == list(range(len(rows))), "{} entry indices are missing/reordered".format(path))
    require(first["summary_schema_version"] == "2", "{} summary schema drift".format(path))
    for field in (
        "host_fingerprint_sha256",
        "binary_sha256",
        "dataset_sha256",
        "sample_plan_sha256",
        "truth_sha256",
        "p31_manifest_sha256",
    ):
        normalize_sha(first[field], "{}:{}".format(path, field))
    require(bool(GIT_SHA_RE.fullmatch(first["git_sha"])), "{} has invalid git_sha".format(path))
    return {
        "path": path,
        "sha256": sha256_file(path),
        "rows": rows,
        "first": first,
    }


def index_summaries(paths):
    by_sha = {}
    for path in paths:
        item = load_summary(path)
        require(item["sha256"] not in by_sha, "duplicate summary content/argument")
        by_sha[item["sha256"]] = item
    return by_sha


def load_manifest(path):
    path, value = load_json(path, "P31 manifest")
    require(value.get("schema_version") == P31_MANIFEST_SCHEMA, "P31 manifest schema drift")
    require(value.get("state") == "PASS", "P31 manifest is not PASS")
    validate_utc(value.get("started_at_utc"), "{} started_at_utc".format(path))
    return {
        "path": path,
        "sha256": sha256_file(path),
        "value": value,
    }


def index_manifests(paths):
    by_sha = {}
    for path in paths:
        item = load_manifest(path)
        require(item["sha256"] not in by_sha, "duplicate P31 manifest content/argument")
        by_sha[item["sha256"]] = item
    return by_sha


def sum_integer(rows, field):
    return sum(integer(row[field], field) for row in rows)


def sum_decimal(rows, field):
    return sum((decimal_value(row[field], field) for row in rows), Decimal(0))


def assert_equal(actual, expected, label):
    require(str(actual) == str(expected), "{} drift: {!r} != {!r}".format(label, actual, expected))


def validate_summary_identity(item, run_row, mode):
    first = item["first"]
    expected_run_id = run_row["latency_run_id"] if mode == "latency" else run_row["cpu_phase_run_id"]
    expected = {
        "run_id": expected_run_id,
        "repeat_index": run_row["repeat_index"],
        "scale": run_row["scale"],
        "stage": run_row["ablation_stage"],
        "mode": mode,
        "workload": run_row["workload"],
        "property_predicate_mode": run_row["property_predicate_mode"],
        "property_id": run_row["property_id"],
        "binary_sha256": run_row["binary_sha256"],
        "dataset_sha256": run_row["dataset_sha256"],
        "sample_plan_sha256": run_row["sample_plan_sha256"],
        "truth_sha256": run_row["truth_sha256"],
    }
    for field, value in expected.items():
        assert_equal(first[field], value, "{} summary {}".format(mode, field))
    expected_eligible = "true" if mode == "latency" else "false"
    assert_equal(first["performance_eligible"], expected_eligible, "{} eligibility".format(mode))
    assert_equal(first["warmup_runs"], "1", "{} warmup_runs".format(mode))
    require(first["current_digest_pass"] == "1", "{} summary current digest failed".format(mode))
    require(
        integer(first["current_digest_mismatches"], "current_digest_mismatches") == 0,
        "{} summary has current digest mismatches".format(mode),
    )
    operations = sum_integer(item["rows"], "measured_operations")
    assert_equal(operations, run_row["measured_operations"], "{} measured_operations".format(mode))

    if mode == "latency":
        totals = {
            "candidate_segments_total": sum_integer(item["rows"], "candidate_segments_total"),
            "body_read_segments_total": sum_integer(item["rows"], "body_read_segments_total"),
            "body_read_bytes_total": sum_integer(item["rows"], "logical_body_bytes_total"),
        }
        for field, value in totals.items():
            assert_equal(value, run_row[field], "latency {}".format(field))
        histogram = aggregate.merge_histograms(item["rows"])
        assert_equal(histogram["p99_us"], run_row["latency_p99_us"], "latency_p99_us")
        assert_equal(
            first["feature_switches_json"],
            run_row["feature_switches"],
            "latency feature_switches",
        )
    else:
        cpu_fields = {
            "cpu_query_setup_ns": "query_cpu_query_setup_ns",
            "cpu_admission_ns": "query_cpu_metadata_admission_ns",
            "cpu_routing_ns": "query_cpu_routing_index_ns",
            "cpu_body_decode_filter_ns": "query_cpu_body_decode_filter_ns",
            "cpu_mvcc_result_ns": "query_cpu_mvcc_result_ns",
            "cpu_total_ns": "query_cpu_total_ns",
        }
        for output_field, summary_field in cpu_fields.items():
            value = sum_integer(item["rows"], summary_field)
            assert_equal(value, run_row[output_field], "cpu-phase {}".format(output_field))
    return operations


def validate_manifest(item, latency_summary, run_row):
    manifest = item["value"]
    first = latency_summary["first"]
    assert_equal(manifest.get("run_id"), run_row["latency_run_id"], "P31 manifest run_id")
    require(
        manifest.get("performance_eligible_declared") is True,
        "latency P31 manifest is not performance eligible",
    )
    host = manifest.get("host")
    repo = manifest.get("repo")
    inputs = manifest.get("inputs")
    require(isinstance(host, dict) and isinstance(repo, dict), "P31 host/repo provenance missing")
    require(isinstance(inputs, dict), "P31 input provenance missing")
    assert_equal(
        host.get("fingerprint_sha256"),
        first["host_fingerprint_sha256"],
        "P31 host fingerprint",
    )
    assert_equal(repo.get("git_sha"), first["git_sha"], "P31 git_sha")
    for manifest_name, row_name in (
        ("binary", "binary_sha256"),
        ("dataset", "dataset_sha256"),
        ("truth", "truth_sha256"),
        ("query_or_trace", "sample_plan_sha256"),
    ):
        reference = inputs.get(manifest_name)
        require(isinstance(reference, dict), "P31 input {} missing".format(manifest_name))
        assert_equal(reference.get("sha256"), run_row[row_name], "P31 input {}".format(manifest_name))
    return validate_utc(
        manifest.get("started_at_utc"),
        "{} started_at_utc".format(item["path"]),
    )


def selection_key(row):
    return {
        "scale": row["scale"],
        "workload": row["workload"],
        "property_predicate_mode": row["property_predicate_mode"],
        "property_id": row["property_id"],
    }


def normalize_rows(run_rows, metadata, summaries, manifests, run_input_sha256):
    selected = [row for row in run_rows if selection_key(row) == metadata["selection"]]
    require(selected, "aggregate run input has no rows for frozen selection")

    stages = aggregate.CANONICAL_STAGES["sf10"]
    by_stage = {}
    for row in selected:
        stage = row["ablation_stage"]
        require(stage in stages, "unexpected SF10 stage: {}".format(stage))
        by_stage.setdefault(stage, []).append(row)
    require(set(by_stage) == set(stages), "selected rows lack the full A0--A6 staircase")
    for stage in stages:
        repeats = sorted(integer(row["repeat_index"], "repeat_index", 1) for row in by_stage[stage])
        require(repeats == [1, 2, 3], "{} repeat coverage is not exactly 1--3".format(stage))

    for field in (
        "binary_sha256",
        "dataset_sha256",
        "sample_plan_sha256",
        "truth_sha256",
        "profiles_sha256",
    ):
        values = {row[field] for row in selected}
        require(len(values) == 1, "selected campaign changes {}".format(field))

    scale_match = SCALE_RE.fullmatch(metadata["selection"]["scale"])
    require(scale_match is not None, "selection.scale is not canonical sfN")
    scale_factor = scale_match.group(1)
    common = metadata["common"]
    output = []
    seen_run_ids = set()
    observed_host = set()
    observed_git = set()
    observed_concurrency = set()

    for run_row in sorted(
        selected,
        key=lambda row: (
            stages.index(row["ablation_stage"]),
            integer(row["repeat_index"], "repeat_index", 1),
        ),
    ):
        latency_sha = normalize_sha(run_row["latency_summary_sha256"], "latency_summary_sha256")
        latency = summaries.get(latency_sha)
        require(latency is not None, "latency summary was not supplied: {}".format(latency_sha))
        validate_summary_identity(latency, run_row, "latency")

        stage = run_row["ablation_stage"]
        if stage == "A6":
            require(not run_row["cpu_phase_summary_sha256"], "A6 unexpectedly has a CPU summary")
            require(not run_row["cpu_phase_run_id"], "A6 unexpectedly has a CPU run ID")
            cpu_sha = ""
        else:
            cpu_sha = normalize_sha(
                run_row["cpu_phase_summary_sha256"], "cpu_phase_summary_sha256"
            )
            cpu = summaries.get(cpu_sha)
            require(cpu is not None, "CPU-phase summary was not supplied: {}".format(cpu_sha))
            validate_summary_identity(cpu, run_row, "cpu-phase")

        p31_sha = normalize_sha(latency["first"]["p31_manifest_sha256"], "p31_manifest_sha256")
        p31 = manifests.get(p31_sha)
        require(p31 is not None, "latency P31 manifest was not supplied: {}".format(p31_sha))
        timestamp = validate_manifest(p31, latency, run_row)

        measurement_ms = sum_decimal(latency["rows"], "elapsed_ms")
        require(measurement_ms > 0, "latency measured interval must be positive")
        measurement_s = measurement_ms / Decimal(1000)
        host = latency["first"]["host_fingerprint_sha256"]
        git_sha = latency["first"]["git_sha"]
        concurrency = integer(latency["first"]["concurrency"], "concurrency", 1)
        observed_host.add(host)
        observed_git.add(git_sha)
        observed_concurrency.add(concurrency)

        run_id = run_row["latency_run_id"]
        require(run_id not in seen_run_ids, "duplicate formal run_id: {}".format(run_id))
        seen_run_ids.add(run_id)
        row = {
            "experiment_id": common["experiment_id"],
            "run_id": run_id,
            "repeat_index": run_row["repeat_index"],
            "timestamp_utc": timestamp,
            "host_fingerprint": host,
            "git_sha": git_sha,
            "binary_sha256": run_row["binary_sha256"],
            "system": common["system"],
            "variant": stage,
            "dataset_id": common["dataset_id"],
            "scale_factor": scale_factor,
            "vertex_count": common["vertex_count"],
            "directed_edge_count": common["directed_edge_count"],
            "property_count": (
                "" if common["property_count"] is None else common["property_count"]
            ),
            "input_sha256": run_row["dataset_sha256"],
            "workload_id": common["workload_id"],
            "query_trace_sha256": run_row["sample_plan_sha256"],
            "seed": common["seed"],
            "cache_state": common["cache_state"],
            "concurrency": concurrency,
            "warmup_s": decimal_text(common["warmup_s"]),
            "measurement_s": decimal_text(measurement_s),
            "digest_pass": "true",
            "mismatch_count": "0",
            "unsupported_reason": "",
            "ablation_stage": stage,
            "property_cohort": metadata["property_cohort"],
            "feature_switches": run_row["feature_switches"],
            "latency_p99_us": run_row["latency_p99_us"],
            "candidate_segments_total": run_row["candidate_segments_total"],
            "body_read_segments_total": run_row["body_read_segments_total"],
            "measured_operations": run_row["measured_operations"],
            "body_read_bytes_total": run_row["body_read_bytes_total"],
            "device_read_bytes_total": run_row["device_read_bytes_total"],
            "cpu_query_setup_ns": run_row["cpu_query_setup_ns"],
            "cpu_admission_ns": run_row["cpu_admission_ns"],
            "cpu_routing_ns": run_row["cpu_routing_ns"],
            "cpu_body_decode_filter_ns": run_row["cpu_body_decode_filter_ns"],
            "cpu_mvcc_result_ns": run_row["cpu_mvcc_result_ns"],
            "cpu_total_ns": run_row["cpu_total_ns"],
            "normalization_schema_version": METADATA_SCHEMA,
            "normalization_metadata_sha256": metadata["sha256"],
            "aggregate_run_input_sha256": run_input_sha256,
            "latency_summary_sha256": latency_sha,
            "cpu_phase_summary_sha256": cpu_sha,
            "p31_manifest_sha256": p31_sha,
            "dataset_sha256": run_row["dataset_sha256"],
            "sample_plan_sha256": run_row["sample_plan_sha256"],
            "truth_sha256": run_row["truth_sha256"],
            "correctness_pass_sha256": run_row["correctness_pass_sha256"],
            "profiles_sha256": run_row["profiles_sha256"],
            "source_scale": run_row["scale"],
            "source_workload": run_row["workload"],
            "source_property_predicate_mode": run_row["property_predicate_mode"],
            "source_property_id": run_row["property_id"],
        }
        require(set(row) == set(OUTPUT_COLUMNS), "internal normalized row schema mismatch")
        output.append(row)

    require(len(observed_host) == 1, "selected campaign spans multiple host fingerprints")
    require(len(observed_git) == 1, "selected campaign spans multiple Git revisions")
    require(len(observed_concurrency) == 1, "selected campaign spans multiple concurrency values")
    require(len(output) == 21, "formal Figure 2 output must contain exactly 21 run-stage rows")
    return output


def write_tsv(path, rows):
    path = Path(path).resolve()
    require(not os.path.lexists(str(path)), "refusing to overwrite output: {}".format(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    require(not os.path.lexists(str(temporary)), "temporary output already exists: {}".format(temporary))
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=OUTPUT_COLUMNS,
                delimiter="\t",
                lineterminator="\n",
                extrasaction="raise",
            )
            writer.writeheader()
            writer.writerows(rows)
        os.replace(str(temporary), str(path))
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return path


def run(args):
    _, run_sha, run_rows = load_run_input(args.run_input)
    metadata = load_metadata(args.metadata, run_sha)
    summaries = index_summaries(args.summary)
    manifests = index_manifests(args.p31_manifest)
    rows = normalize_rows(run_rows, metadata, summaries, manifests, run_sha)
    output = write_tsv(args.output, rows)
    print(str(output))
    print(sha256_file(output))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-input", required=True, type=Path)
    parser.add_argument("--summary", required=True, action="append", type=Path)
    parser.add_argument("--p31-manifest", required=True, action="append", type=Path)
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main():
    args = build_parser().parse_args()
    try:
        return run(args)
    except (NormalizeError, OSError) as exc:
        print("FAIL: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
