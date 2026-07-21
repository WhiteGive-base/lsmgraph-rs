#!/usr/bin/env python3
"""Validate one P20/P31 run and emit a fixed-schema entry-level TSV."""

from __future__ import print_function

import argparse
import csv
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{16}$")
P31_MANIFEST_SCHEMA = "cidr-run-manifest-v1"
P31_RESOURCE_SCHEMA = "cidr-resource-v1"
CANONICAL_STAGES = {
    "sf10": ["A0", "A1", "A2", "A3", "A4", "A5", "A6"],
    "sf30": ["A0", "A2", "A4", "A6"],
}
CPU_PHASE_FIELDS = [
    "query_cpu_query_setup_ns",
    "query_cpu_metadata_admission_ns",
    "query_cpu_routing_index_ns",
    "query_cpu_body_decode_filter_ns",
    "query_cpu_mvcc_result_ns",
]
SUMMARY_COLUMNS = [
    "summary_schema_version",
    "experiment_id",
    "task_id",
    "run_id",
    "repeat_index",
    "benchmark_entry_index",
    "scale",
    "stage",
    "mode",
    "workload",
    "property_predicate_mode",
    "property_id",
    "performance_eligible",
    "concurrency",
    "worker_threads",
    "cpuset",
    "stage_store",
    "ended_at_utc",
    "host_fingerprint_sha256",
    "git_sha",
    "edge_type",
    "src_label",
    "dst_label",
    "sampled_vertices",
    "measured_operations",
    "warmup_runs",
    "training_runs",
    "training_feedback_compactions",
    "measured_rounds",
    "elapsed_ms",
    "qps",
    "neighbor_edges",
    "get_neighbors_avg_us",
    "get_neighbors_p50_us",
    "get_neighbors_p90_us",
    "get_neighbors_p99_us",
    "get_neighbors_latency_histogram_json",
    "candidate_segments_total",
    "routed_segments_total",
    "body_read_segments_total",
    "logical_read_bytes_total",
    "logical_body_bytes_total",
    "query_cpu_query_setup_ns",
    "query_cpu_metadata_admission_ns",
    "query_cpu_routing_index_ns",
    "query_cpu_body_decode_filter_ns",
    "query_cpu_mvcc_result_ns",
    "query_cpu_phase_sum_ns",
    "query_cpu_total_ns",
    "query_cpu_clock_failures",
    "process_cpu_total_ns",
    "process_read_bytes_total",
    "process_write_bytes_total",
    "peak_rss_bytes",
    "peak_pss_bytes",
    "peak_store_total_bytes",
    "peak_device_read_mib_s",
    "peak_device_write_mib_s",
    "feature_switches_json",
    "pre_measurement_json",
    "cache_state_before_json",
    "cache_state_after_json",
    "correctness_gate_pass",
    "correctness_checked",
    "correctness_mismatches",
    "current_digest_pass",
    "current_digest_mismatches",
    "result_digest_count",
    "entry_result_digest",
    "binary_sha256",
    "dataset_sha256",
    "sample_plan_sha256",
    "truth_sha256",
    "correctness_pass_sha256",
    "p02b_sentinel_result_sha256",
    "p02b_pass_marker_sha256",
    "p02b_provenance_sha256",
    "p02b_validator_sha256",
    "p02b_admission_sha256",
    "profiles_sha256",
    "invocation_config_sha256",
    "resolved_profile_sha256",
    "command_sha256",
    "p20_runner_sha256",
    "p20_summarizer_sha256",
    "p31_wrapper_sha256",
    "pristine_store_sha256",
    "pristine_store_manifest_sha256",
    "stage_store_provenance_sha256",
    "stage_store_post_state_sha256",
    "stage_store_post_quick_inventory_sha256",
    "input_manifest_sha256",
    "raw_output_sha256",
    "p31_manifest_sha256",
    "p31_validation_sha256",
    "p31_done_sha256",
]


class SummaryError(ValueError):
    pass


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise SummaryError("duplicate JSON key: {}".format(key))
        value[key] = item
    return value


def load_json(path, label):
    try:
        value = json.loads(
            Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except (OSError, json.JSONDecodeError, SummaryError) as exc:
        raise SummaryError("cannot read {} JSON: {}".format(label, exc))
    if not isinstance(value, dict):
        raise SummaryError("{} must be a JSON object".format(label))
    return value


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def quick_inventory(root):
    root = Path(root)
    entries = []
    for directory, directory_names, file_names in os.walk(str(root), followlinks=False):
        directory_path = Path(directory)
        for name in list(directory_names):
            require(not (directory_path / name).is_symlink(), "stage store contains a symlink directory")
        for name in file_names:
            path = directory_path / name
            require(path.is_file() and not path.is_symlink(), "stage store contains a non-regular file")
            stat = path.stat()
            entries.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
    entries.sort(key=lambda value: value["path"])
    payload = json.dumps(entries, sort_keys=True, separators=(",", ":")) + "\n"
    return {
        "file_count": len(entries),
        "total_bytes": sum(item["size_bytes"] for item in entries),
        "quick_inventory_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "entries": entries,
    }


def compact_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def require(condition, message):
    if not condition:
        raise SummaryError(message)


def number(value, label, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SummaryError("{} must be numeric".format(label))
    result = float(value)
    if not math.isfinite(result) or result < 0 or (positive and result <= 0):
        raise SummaryError("{} has invalid numeric value".format(label))
    return value


def validate_latency_histogram(value, expected_operations):
    require(isinstance(value, dict), "get-neighbors latency histogram is missing")
    count = value.get("count")
    require(
        isinstance(count, int)
        and not isinstance(count, bool)
        and count == expected_operations,
        "latency histogram operation coverage drift",
    )
    for field in ("sum_us", "min_us", "max_us", "avg_us", "p50_us", "p90_us", "p99_us"):
        number(value.get(field), "latency histogram {}".format(field))
    buckets = value.get("buckets")
    require(isinstance(buckets, list) and buckets, "latency histogram buckets are missing")
    previous_bound = -1
    bucket_count = 0
    for bucket in buckets:
        require(isinstance(bucket, dict), "latency histogram bucket is invalid")
        upper = bucket.get("upper_bound_us")
        current_count = bucket.get("count")
        require(
            isinstance(upper, int)
            and not isinstance(upper, bool)
            and upper > previous_bound
            and isinstance(current_count, int)
            and not isinstance(current_count, bool)
            and current_count >= 0,
            "latency histogram bucket value is invalid",
        )
        previous_bound = upper
        bucket_count += current_count
    require(bucket_count == count, "latency histogram bucket coverage drift")
    return compact_json(value)


def read_input_manifest(path):
    rows = {}
    try:
        with Path(path).open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            require(
                reader.fieldnames == ["name", "path", "kind", "sha256", "verification"],
                "inputs.sha256.tsv schema drift",
            )
            for row in reader:
                name = row.get("name", "")
                require(name and name not in rows, "duplicate/empty input manifest name")
                require(bool(SHA256_RE.fullmatch(row.get("sha256", ""))), "invalid input SHA-256")
                rows[name] = row
    except OSError as exc:
        raise SummaryError("cannot read inputs.sha256.tsv: {}".format(exc))
    required = {
        "binary",
        "dataset",
        "sample_plan",
        "truth",
        "correctness_pass",
        "p02b_sentinel_result",
        "p02b_pass_marker",
        "p02b_provenance",
        "p02b_validator",
        "p02b_admission",
        "pristine_store",
        "pristine_store_manifest",
        "stage_store_provenance",
        "stage_store_post_state",
        "profiles",
        "invocation_config",
        "profile_validator",
        "p20_runner",
        "p20_summarizer",
        "p31_wrapper",
        "resolved_profile",
        "command",
    }
    missing = sorted(required - set(rows))
    require(not missing, "input manifest missing: {}".format(", ".join(missing)))
    for name, row in rows.items():
        if row["kind"] != "file":
            continue
        artifact = Path(row["path"])
        require(artifact.is_file(), "input file disappeared: {}".format(name))
        require(sha256_file(artifact) == row["sha256"], "input file changed: {}".format(name))
    return rows


def option_value(argv, name, default=None):
    for index, token in enumerate(argv):
        if token == name:
            require(index + 1 < len(argv), "missing value after {}".format(name))
            return argv[index + 1]
        if token.startswith(name + "="):
            return token.split("=", 1)[1]
    return default


def expected_feature_switches(resolved):
    features = resolved["stage"].get("features")
    require(isinstance(features, list) and len(features) == 6, "resolved feature list invalid")
    return {
        "exact_evidence_admission": features[0],
        "semantic_routing": features[1],
        "budgeted_degree_promotion": features[2],
        "feedback_priority": features[3],
        "semantic_compaction": features[4],
        "automatic_maintenance": resolved["mode"].get("resolved_automatic_maintenance"),
        "query_cpu_phase_instrumentation": resolved["mode"].get("query_cpu_phases"),
        "post_round_feedback_compact": False,
    }


def validate_p31(run_root, command, inputs, invocation):
    p31_root = Path(command.get("p31_run_root", ""))
    require(p31_root.resolve() == (run_root / "p31").resolve(), "P31 run-root drift")
    done_path = p31_root / "DONE"
    failed_path = p31_root / "FAILED"
    require(done_path.is_file(), "P31 DONE is missing")
    require(not failed_path.exists(), "P31 FAILED exists")
    manifest_path = p31_root / "run-manifest.json"
    validation_path = p31_root / "validation.json"
    manifest = load_json(manifest_path, "P31 manifest")
    validation = load_json(validation_path, "P31 validation")
    done = load_json(done_path, "P31 DONE")
    require(manifest.get("schema_version") == P31_MANIFEST_SCHEMA, "P31 manifest schema drift")
    require(
        manifest.get("resource_schema_version") == P31_RESOURCE_SCHEMA,
        "P31 resource schema drift",
    )
    require(validation.get("schema_version") == P31_MANIFEST_SCHEMA, "P31 validation schema drift")
    require(done.get("state") == "PASS", "P31 DONE state is not PASS")
    require(validation.get("state") == "PASS", "P31 validation state is not PASS")
    require(manifest.get("state") == "PASS", "P31 manifest state is not PASS")
    require(
        manifest.get("task_id") == command.get("task_id")
        and manifest.get("run_id") == command.get("run_id"),
        "P31 task/run identity drift",
    )
    p31_argv = command.get("p31_argv")
    require(isinstance(p31_argv, list), "recorded P31 argv is missing")
    expected_repo = option_value(p31_argv, "--repo-root")
    require(expected_repo is not None, "P31 repo-root option is missing")
    require(
        Path(manifest.get("repo", {}).get("root", "")).resolve()
        == Path(expected_repo).resolve(),
        "P31 repo-root identity drift",
    )
    expected_disk_roots = [
        {"role": "store", "label": "p20", "path": str(Path(command.get("stage_store", "")).resolve())}
    ]
    require(manifest.get("disk_roots") == expected_disk_roots, "P31 disk-root identity drift")
    manifest_sha = sha256_file(manifest_path)
    validation_sha = sha256_file(validation_path)
    require(done.get("manifest_sha256") == manifest_sha, "P31 DONE manifest hash mismatch")
    require(done.get("validation_sha256") == validation_sha, "P31 DONE validation hash mismatch")
    artifacts = manifest.get("artifacts")
    require(isinstance(artifacts, dict) and artifacts, "P31 artifact hash map is missing")
    for name, reference in artifacts.items():
        require(isinstance(reference, dict), "P31 artifact reference is invalid: {}".format(name))
        pure_name = PurePosixPath(name) if isinstance(name, str) else PurePosixPath("/")
        require(
            isinstance(name, str)
            and name
            and not pure_name.is_absolute()
            and ".." not in pure_name.parts,
            "P31 artifact path is unsafe",
        )
        artifact_path = p31_root / name
        require(artifact_path.is_file(), "P31 artifact disappeared: {}".format(name))
        require(
            bool(SHA256_RE.fullmatch(reference.get("sha256", ""))),
            "P31 artifact SHA-256 is invalid: {}".format(name),
        )
        require(
            sha256_file(artifact_path) == reference.get("sha256"),
            "P31 artifact hash mismatch: {}".format(name),
        )
    require(
        manifest.get("performance_eligible_declared") is invocation.get("performance_eligible"),
        "P31 performance eligibility drift",
    )
    require(
        manifest.get("collector", {}).get("require_aux_tools") is True,
        "paper-use P20 requires P31 auxiliary collectors",
    )
    expected_p31_inputs = {
        "binary": inputs["binary"]["sha256"],
        "dataset": inputs["dataset"]["sha256"],
        "truth": inputs["truth"]["sha256"],
        "query_or_trace": inputs["sample_plan"]["sha256"],
        "config": inputs["invocation_config"]["sha256"],
    }
    manifest_inputs = manifest.get("inputs", {})
    for name, expected_sha in expected_p31_inputs.items():
        require(
            manifest_inputs.get(name, {}).get("sha256") == expected_sha,
            "P31 input hash drift: {}".format(name),
        )
    resources = manifest.get("summary", {}).get("resources", {})
    disk = manifest.get("summary", {}).get("disk", {})
    required_resources = {
        "process_user_cpu_s",
        "process_sys_cpu_s",
        "process_read_bytes",
        "process_write_bytes",
        "peak_rss_bytes",
        "peak_pss_bytes",
        "peak_device_read_mib_s",
        "peak_device_write_mib_s",
    }
    require(required_resources <= set(resources), "P31 resource summary is incomplete")
    require("peak_store_total_bytes" in disk, "P31 disk summary is incomplete")
    return {
        "root": p31_root,
        "manifest": manifest,
        "resources": resources,
        "disk": disk,
        "manifest_sha": manifest_sha,
        "validation_sha": validation_sha,
        "done_sha": sha256_file(done_path),
    }


def validate_output(raw, resolved, invocation, command):
    stage = resolved["stage"]["id"]
    require(raw.get("data_dir") == command.get("stage_store"), "storage output data_dir drift")
    require(raw.get("query_control_stage") == stage, "storage output stage drift")
    require(raw.get("l0_layout") == "SemanticBudgeted", "storage output layout drift")
    require(raw.get("sample_plan_version") == 1, "storage output sample-plan version drift")
    require(raw.get("scan_requested") is False, "measured run generated/scanned a sample plan")
    require(raw.get("emit_result_digests") is True, "result digests were not emitted")
    require(raw.get("repeats") == 1, "single-profile run must have repeats=1")
    require(raw.get("warmup_runs") == invocation["runtime"]["warmup-runs"], "warmup drift")
    argv = command.get("storage_bench_argv")
    require(isinstance(argv, list) and argv, "recorded storage argv invalid")
    require(option_value(argv, "--sample-plan-in") is not None, "frozen sample plan was not used")
    require(option_value(argv, "--repeats") == "1", "recorded repeats drift")
    require("--auto-compact" not in argv, "measured --auto-compact is forbidden")
    require(raw.get("feature_switches") == expected_feature_switches(resolved), "feature-switch drift")
    expected_training_runs = int(option_value(argv, "--training-runs", "0"))
    expected_training_compactions = int(option_value(argv, "--training-feedback-compactions", "0"))
    require(raw.get("training_runs") == expected_training_runs, "training-runs drift")
    require(
        raw.get("training_feedback_compactions") == expected_training_compactions,
        "training compaction drift",
    )
    expected_workload = option_value(argv, "--workload-mode", "one-hop").replace("-", "_")
    require(raw.get("workload_mode") == expected_workload, "workload mode drift")
    expected_property = option_value(argv, "--property-predicate-mode", "none").replace("-", "_")
    require(raw.get("property_predicate_mode") == expected_property, "property mode drift")
    property_id_text = option_value(argv, "--property-id", "0")
    require(
        isinstance(property_id_text, str) and bool(re.fullmatch(r"0|[1-9][0-9]*", property_id_text)),
        "recorded property id is invalid",
    )
    expected_property_id = int(property_id_text)
    require(raw.get("property_id") == expected_property_id, "property id drift")
    require(
        invocation.get("property_predicate_mode") == expected_property
        and invocation.get("property_id") == expected_property_id,
        "invocation property identity drift",
    )
    require("cache_state_before" in raw and "cache_state_after" in raw, "cache state is missing")
    benchmarks = raw.get("benchmarks")
    require(isinstance(benchmarks, list) and benchmarks, "benchmarks must be non-empty")
    return benchmarks


def validate_current_digests(truth, benchmarks, invocation, inputs):
    require(truth.get("schema_version") == 1, "truth schema drift")
    require(
        truth.get("digest_schema") == "storage-bench-result-digest-v1",
        "truth digest schema drift",
    )
    require(
        truth.get("scale") == invocation.get("scale")
        and truth.get("workload") == invocation.get("workload"),
        "truth profile identity drift",
    )
    require(
        truth.get("sample_plan_sha256") == inputs["sample_plan"]["sha256"],
        "truth sample-plan binding drift",
    )
    require(
        truth.get("property_predicate_mode") == invocation.get("property_predicate_mode")
        and truth.get("property_id") == invocation.get("property_id"),
        "truth property identity drift",
    )
    expected_entries = truth.get("entries")
    require(
        isinstance(expected_entries, list)
        and expected_entries
        and len(expected_entries) == len(benchmarks),
        "truth/benchmark entry coverage drift",
    )
    for entry_index, (expected, actual) in enumerate(zip(expected_entries, benchmarks)):
        require(isinstance(expected, dict), "truth entry must be an object")
        for field in ("edge_type", "src_label", "dst_label"):
            require(
                expected.get(field) == actual.get(field),
                "truth entry {} {} identity drift".format(entry_index, field),
            )
        expected_entry_digest = expected.get("entry_result_digest")
        require(
            bool(DIGEST_RE.fullmatch(expected_entry_digest or "")),
            "truth entry digest is invalid",
        )
        require(
            actual.get("entry_result_digest") == expected_entry_digest,
            "current entry digest mismatches truth at entry {}".format(entry_index),
        )
        expected_samples = expected.get("samples")
        actual_samples = actual.get("result_digests")
        require(
            isinstance(expected_samples, list)
            and expected_samples
            and isinstance(actual_samples, list)
            and len(expected_samples) == len(actual_samples),
            "truth/current digest coverage drift at entry {}".format(entry_index),
        )
        for sample_index, (expected_sample, actual_sample) in enumerate(
            zip(expected_samples, actual_samples)
        ):
            require(
                isinstance(expected_sample, dict) and isinstance(actual_sample, dict),
                "truth/current sample digest must be an object",
            )
            require(
                isinstance(expected_sample.get("src"), int)
                and not isinstance(expected_sample.get("src"), bool)
                and isinstance(expected_sample.get("result_count"), int)
                and not isinstance(expected_sample.get("result_count"), bool)
                and expected_sample["result_count"] >= 0
                and bool(DIGEST_RE.fullmatch(expected_sample.get("result_digest", ""))),
                "truth sample digest is invalid",
            )
            require(
                actual_sample.get("src") == expected_sample["src"]
                and actual_sample.get("result_count") == expected_sample["result_count"]
                and actual_sample.get("result_digest") == expected_sample["result_digest"],
                "current sample digest mismatches truth at entry {}, sample {}".format(
                    entry_index, sample_index
                ),
            )


def cpuset_members(raw):
    require(isinstance(raw, str) and re.fullmatch(r"[0-9,-]+", raw), "invalid cpuset")
    result = set()
    for token in raw.split(","):
        if "-" in token:
            fields = token.split("-")
            require(len(fields) == 2, "invalid cpuset range")
            left, right = (int(value) for value in fields)
            require(left <= right, "descending cpuset range")
            values = range(left, right + 1)
        else:
            values = [int(token)]
        for value in values:
            require(value not in result, "duplicate CPU in cpuset")
            result.add(value)
    require(bool(result), "empty cpuset")
    return result


def validate_p02b_receipt(inputs, invocation, command, p31):
    repo_root = option_value(command["p31_argv"], "--repo-root")
    repo_head = p31["manifest"].get("repo", {}).get("git_sha")
    validator_argv = [
        sys.executable,
        inputs["p02b_validator"]["path"],
        "--result",
        inputs["p02b_sentinel_result"]["path"],
        "--consumer",
        "P20",
        "--require-formal",
        "--expected-repo-root",
        repo_root,
        "--expected-repo-head",
        repo_head,
        "--expected-binary-sha256",
        inputs["binary"]["sha256"],
        "--max-age-seconds",
        "21600",
    ]
    require(command.get("p02b_validator_argv") == validator_argv, "P02B validator argv drift")
    completed = subprocess.run(
        validator_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    require(
        completed.returncode == 0,
        "P02B sentinel validator failed: {}".format(completed.stderr.strip()),
    )
    try:
        receipt = json.loads(completed.stdout, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, SummaryError) as exc:
        raise SummaryError("P02B validator emitted invalid JSON: {}".format(exc))
    require(isinstance(receipt, dict), "P02B receipt must be an object")
    expected = {
        "state": "PASS",
        "consumer": "P20",
        "formal_required": True,
        "fixture_only": False,
        "scope": "host-global",
        "sentinel_result_sha256": inputs["p02b_sentinel_result"]["sha256"],
        "pass_marker_sha256": inputs["p02b_pass_marker"]["sha256"],
        "provenance_sha256": inputs["p02b_provenance"]["sha256"],
        "repo_head": repo_head,
        "binary_sha256": inputs["binary"]["sha256"],
        "scale": "sf10",
    }
    for name, value in expected.items():
        require(receipt.get(name) == value, "P02B receipt drift: {}".format(name))
    invocation_receipt = {
        "p02b_run_id": receipt.get("run_id"),
        "p02b_completed_at_utc": receipt.get("completed_at_utc"),
        "p02b_host_fingerprint_sha256": receipt.get("host", {}).get(
            "fingerprint_sha256"
        ),
    }
    for name, value in invocation_receipt.items():
        require(invocation.get(name) == value, "P02B invocation receipt drift: {}".format(name))
    for field, input_name in (
        ("sentinel_result", "p02b_sentinel_result"),
        ("pass_marker", "p02b_pass_marker"),
        ("provenance", "p02b_provenance"),
    ):
        require(
            Path(receipt.get(field, "")).resolve()
            == Path(inputs[input_name]["path"]).resolve(),
            "P02B receipt path drift: {}".format(field),
        )
    require(
        Path(receipt.get("repo_root", "")).resolve() == Path(repo_root).resolve(),
        "P02B receipt repo root drift",
    )
    sentinel_host = receipt.get("host")
    current_host = p31["manifest"].get("host")
    require(
        isinstance(sentinel_host, dict) and isinstance(current_host, dict),
        "P02B/current P31 host identity is missing",
    )
    require(
        sentinel_host.get("fingerprint_sha256")
        == current_host.get("fingerprint_sha256"),
        "P02B host-global admission fingerprint differs from current P31 host",
    )
    require(
        sentinel_host.get("hostname") == current_host.get("hostname"),
        "P02B host-global admission hostname differs from current P31 host",
    )
    return receipt


def validate_p02b_admission(admission, receipt, resolved, invocation, command, inputs):
    require(
        admission.get("schema_version") == "p20-p02b-admission-v1"
        and admission.get("state") == "PASS"
        and admission.get("scope") == "host-global",
        "P02B admission schema/state/scope drift",
    )
    policy = admission.get("policy")
    require(
        isinstance(policy, dict)
        and policy.get("consumer") == "P20"
        and policy.get("formal_pass_required") is True
        and policy.get("fixture_forbidden") is True
        and policy.get("maximum_age_seconds") == 21600
        and policy.get("same_host_fingerprint_required") is True
        and policy.get("release_is_scale_store_workload_independent") is True,
        "P02B admission policy drift",
    )
    p02b = admission.get("p02b")
    require(isinstance(p02b, dict), "P02B admission receipt is missing")
    require(p02b.get("receipt") == receipt, "P02B admission receipt drift")
    require(
        p02b.get("validator_sha256") == inputs["p02b_validator"]["sha256"]
        and p02b.get("validator_command") == command.get("p02b_validator_argv"),
        "P02B admission validator binding drift",
    )
    p20 = admission.get("p20")
    require(isinstance(p20, dict), "P02B admission P20 binding is missing")
    identity = {
        "task_id": invocation.get("task_id"),
        "run_id": invocation.get("run_id"),
        "repeat_index": invocation.get("repeat_index"),
        "scale": invocation.get("scale"),
        "stage": invocation.get("stage"),
        "mode": invocation.get("mode"),
        "workload": invocation.get("workload"),
        "property_id": invocation.get("property_id"),
        "repo_head": receipt.get("repo_head"),
    }
    for name, value in identity.items():
        require(p20.get(name) == value, "P02B admission P20 identity drift: {}".format(name))
    require(
        Path(p20.get("repo_root", "")).resolve()
        == Path(receipt.get("repo_root", "")).resolve(),
        "P02B admission repo root drift",
    )
    expected_inputs = {
        "binary": inputs["binary"]["sha256"],
        "dataset": inputs["dataset"]["sha256"],
        "sample_plan": inputs["sample_plan"]["sha256"],
        "truth": inputs["truth"]["sha256"],
    }
    require(p20.get("input_sha256") == expected_inputs, "P02B admission current input drift")
    expected_hashes = {
        "correctness_pass_sha256": inputs["correctness_pass"]["sha256"],
        "pristine_store_sha256": inputs["pristine_store"]["sha256"],
        "pristine_store_manifest_sha256": inputs["pristine_store_manifest"]["sha256"],
        "profiles_sha256": inputs["profiles"]["sha256"],
        "resolved_profile_sha256": inputs["resolved_profile"]["sha256"],
    }
    for name, value in expected_hashes.items():
        require(p20.get(name) == value, "P02B admission current hash drift: {}".format(name))
    isolation = p20.get("cpu_isolation")
    require(isinstance(isolation, dict) and isolation.get("disjoint") is True, "P20 CPU isolation missing")
    benchmark_cpuset = isolation.get("benchmark_cpuset")
    collector_cpuset = isolation.get("collector_cpuset")
    require(
        benchmark_cpuset == invocation.get("cpuset")
        and collector_cpuset == invocation.get("housekeeping_cpuset")
        and isolation.get("worker_threads") == invocation.get("worker_threads"),
        "P20 CPU isolation binding drift",
    )
    require(
        not (cpuset_members(benchmark_cpuset) & cpuset_members(collector_cpuset)),
        "P20 benchmark/collector cpusets overlap",
    )
    require(
        command["benchmark_argv"][:3] == ["taskset", "-c", benchmark_cpuset]
        and command["p31_argv"][:3] == ["taskset", "-c", collector_cpuset],
        "P20 taskset placement drift",
    )
    p31_policy = p20.get("p31")
    require(isinstance(p31_policy, dict), "P02B admission P31 policy missing")
    p31_argv = command["p31_argv"]
    require(
        p31_policy.get("wrapper_sha256") == inputs["p31_wrapper"]["sha256"]
        and p31_policy.get("device") == option_value(p31_argv, "--device")
        and p31_policy.get("data_mount") == option_value(p31_argv, "--data-mount")
        and str(p31_policy.get("interval_seconds")) == option_value(p31_argv, "--interval")
        and str(p31_policy.get("disk_interval_seconds")) == option_value(p31_argv, "--disk-interval")
        and str(p31_policy.get("min_samples")) == option_value(p31_argv, "--min-samples")
        and p31_policy.get("min_samples", 0) >= 10
        and p31_policy.get("require_aux_tools") is True
        and "--allow-missing-aux-tools" not in p31_argv,
        "P02B admission P31 policy drift",
    )
    require(
        resolved.get("scale") == p20.get("scale")
        and resolved.get("stage", {}).get("id") == p20.get("stage"),
        "P02B admission resolved profile drift",
    )


def build_rows(run_root):
    run_root = Path(run_root).resolve()
    require(run_root.is_dir(), "run root is not a directory")
    resolved = load_json(run_root / "resolved-profile.json", "resolved profile")
    invocation = load_json(run_root / "invocation-config.json", "invocation config")
    command = load_json(run_root / "command.json", "command")
    correctness = load_json(run_root / "correctness-pass.json", "correctness PASS")
    inputs_path = run_root / "inputs.sha256.tsv"
    inputs = read_input_manifest(inputs_path)
    truth = load_json(inputs["truth"]["path"], "truth")
    stage_provenance = load_json(
        inputs["stage_store_provenance"]["path"], "stage-store provenance"
    )
    stage_post_state = load_json(
        inputs["stage_store_post_state"]["path"], "stage-store post state"
    )
    p02b_admission = load_json(inputs["p02b_admission"]["path"], "P02B admission")
    require(
        sha256_file(run_root / "correctness-pass.json") == inputs["correctness_pass"]["sha256"],
        "copied correctness PASS hash mismatch",
    )
    require(invocation.get("schema_version") == 1, "invocation config schema drift")
    require(invocation.get("max_query_streams") == 1, "formal P20 must be single-stream")
    require(
        isinstance(invocation.get("repeat_index"), int)
        and not isinstance(invocation.get("repeat_index"), bool)
        and invocation["repeat_index"] > 0,
        "repeat index is invalid",
    )
    require(
        isinstance(invocation.get("worker_threads"), int)
        and not isinstance(invocation.get("worker_threads"), bool)
        and invocation["worker_threads"] > 0,
        "worker thread count is invalid",
    )
    require(command.get("invocation_kind") == "argv-no-shell", "command was not recorded as argv")
    require(
        command.get("task_id") == invocation.get("task_id")
        and command.get("run_id") == invocation.get("run_id")
        and command.get("repeat_index") == invocation.get("repeat_index"),
        "runner task/run/repeat identity drift",
    )
    require(
        command.get("worker_threads") == invocation.get("worker_threads")
        and command.get("frozen_environment") == invocation.get("frozen_environment"),
        "runner environment contract drift",
    )
    frozen_environment = invocation.get("frozen_environment")
    require(
        isinstance(frozen_environment, dict)
        and frozen_environment.get("RAYON_NUM_THREADS")
        == str(invocation.get("worker_threads"))
        and frozen_environment.get("TOKIO_WORKER_THREADS")
        == str(invocation.get("worker_threads"))
        and frozen_environment.get("SNB_SKIP_SEM_INDEX") == "absent"
        and frozen_environment.get("SNB_SKIP_ADJ_CACHE") == "absent",
        "frozen benchmark environment is invalid",
    )
    require(
        invocation.get("scale") == resolved.get("scale")
        and invocation.get("stage") == resolved.get("stage", {}).get("id")
        and invocation.get("mode") == resolved.get("mode", {}).get("name")
        and invocation.get("workload") == resolved.get("workload", {}).get("name"),
        "resolved/invocation profile identity drift",
    )
    require(stage_provenance.get("schema_version") == 1, "stage provenance schema drift")
    require(
        stage_provenance.get("stage_store") == command.get("stage_store")
        and stage_provenance.get("pristine_store") == command.get("pristine_store")
        and stage_provenance.get("pristine_store_sha256")
        == inputs["pristine_store"]["sha256"]
        and stage_provenance.get("pristine_store_manifest_sha256")
        == inputs["pristine_store_manifest"]["sha256"],
        "stage clone provenance drift",
    )
    clone = stage_provenance.get("clone")
    require(
        isinstance(clone, dict)
        and clone.get("content_verified") is True
        and isinstance(clone.get("stage_quick_inventory"), dict),
        "stage clone verification is incomplete",
    )
    require(
        stage_post_state.get("schema_version") == 1
        and stage_post_state.get("run_id") == invocation.get("run_id")
        and stage_post_state.get("repeat_index") == invocation.get("repeat_index")
        and stage_post_state.get("stage") == invocation.get("stage")
        and stage_post_state.get("workload") == invocation.get("workload")
        and stage_post_state.get("stage_store") == command.get("stage_store"),
        "stage post-state identity drift",
    )
    recorded_post_inventory = stage_post_state.get("quick_inventory")
    require(isinstance(recorded_post_inventory, dict), "stage post-state inventory is missing")
    require(
        bool(
            SHA256_RE.fullmatch(
                recorded_post_inventory.get("quick_inventory_sha256", "")
            )
        ),
        "stage post-state inventory hash is invalid",
    )
    require(
        quick_inventory(command.get("stage_store")) == recorded_post_inventory,
        "stage store changed after post-state capture",
    )
    p31_argv = command.get("p31_argv")
    benchmark_argv = command.get("benchmark_argv")
    require(isinstance(p31_argv, list) and isinstance(benchmark_argv, list), "command arrays missing")
    require("--" in p31_argv, "P31 argv separator is missing")
    require(p31_argv[p31_argv.index("--") + 1 :] == benchmark_argv, "P31 command tail drift")
    p31 = validate_p31(run_root, command, inputs, invocation)
    expected_p02b_hashes = {
        "p02b_sentinel_result_sha256": inputs["p02b_sentinel_result"]["sha256"],
        "p02b_pass_marker_sha256": inputs["p02b_pass_marker"]["sha256"],
        "p02b_provenance_sha256": inputs["p02b_provenance"]["sha256"],
        "p02b_validator_sha256": inputs["p02b_validator"]["sha256"],
        "p02b_admission_sha256": inputs["p02b_admission"]["sha256"],
    }
    for name, value in expected_p02b_hashes.items():
        require(invocation.get(name) == value, "P02B invocation hash drift: {}".format(name))
    p02b_receipt = validate_p02b_receipt(inputs, invocation, command, p31)
    validate_p02b_admission(
        p02b_admission, p02b_receipt, resolved, invocation, command, inputs
    )

    raw_path = p31["root"] / "command.stdout.log"
    raw = load_json(raw_path, "storage-bench stdout")
    benchmarks = validate_output(raw, resolved, invocation, command)
    validate_current_digests(truth, benchmarks, invocation, inputs)
    feature_json = compact_json(raw["feature_switches"])
    cache_before = compact_json(raw["cache_state_before"])
    cache_after = compact_json(raw["cache_state_after"])
    resources = p31["resources"]
    disk = p31["disk"]
    process_cpu_total_ns = int(
        round(
            (number(resources["process_user_cpu_s"], "P31 user CPU") + number(resources["process_sys_cpu_s"], "P31 sys CPU"))
            * 1_000_000_000
        )
    )
    common = {
        "summary_schema_version": 1,
        "experiment_id": resolved.get("experiment_id"),
        "task_id": command.get("task_id"),
        "run_id": command.get("run_id"),
        "repeat_index": invocation.get("repeat_index"),
        "scale": resolved.get("scale"),
        "stage": resolved.get("stage", {}).get("id"),
        "mode": resolved.get("mode", {}).get("name"),
        "workload": resolved.get("workload", {}).get("name"),
        "property_predicate_mode": invocation.get("property_predicate_mode"),
        "property_id": invocation.get("property_id"),
        "performance_eligible": str(bool(invocation.get("performance_eligible"))).lower(),
        "concurrency": 1,
        "worker_threads": invocation.get("worker_threads"),
        "cpuset": command.get("cpuset", ""),
        "stage_store": command.get("stage_store"),
        "ended_at_utc": p31["manifest"].get("ended_at_utc", ""),
        "host_fingerprint_sha256": p31["manifest"].get("host", {}).get("fingerprint_sha256", ""),
        "git_sha": p31["manifest"].get("repo", {}).get("git_sha", ""),
        "process_cpu_total_ns": process_cpu_total_ns,
        "process_read_bytes_total": int(number(resources["process_read_bytes"], "P31 process read bytes")),
        "process_write_bytes_total": int(number(resources["process_write_bytes"], "P31 process write bytes")),
        "peak_rss_bytes": int(number(resources["peak_rss_bytes"], "P31 RSS")),
        "peak_pss_bytes": int(number(resources["peak_pss_bytes"], "P31 PSS")),
        "peak_store_total_bytes": int(number(disk["peak_store_total_bytes"], "P31 store bytes")),
        "peak_device_read_mib_s": number(resources["peak_device_read_mib_s"], "P31 device read rate"),
        "peak_device_write_mib_s": number(resources["peak_device_write_mib_s"], "P31 device write rate"),
        "feature_switches_json": feature_json,
        "pre_measurement_json": compact_json(resolved.get("stage", {}).get("pre_measurement")),
        "cache_state_before_json": cache_before,
        "cache_state_after_json": cache_after,
        "correctness_gate_pass": 1,
        "correctness_checked": correctness.get("checked"),
        "correctness_mismatches": correctness.get("mismatches"),
        "current_digest_pass": 1,
        "current_digest_mismatches": 0,
        "binary_sha256": inputs["binary"]["sha256"],
        "dataset_sha256": inputs["dataset"]["sha256"],
        "sample_plan_sha256": inputs["sample_plan"]["sha256"],
        "truth_sha256": inputs["truth"]["sha256"],
        "correctness_pass_sha256": inputs["correctness_pass"]["sha256"],
        "p02b_sentinel_result_sha256": inputs["p02b_sentinel_result"]["sha256"],
        "p02b_pass_marker_sha256": inputs["p02b_pass_marker"]["sha256"],
        "p02b_provenance_sha256": inputs["p02b_provenance"]["sha256"],
        "p02b_validator_sha256": inputs["p02b_validator"]["sha256"],
        "p02b_admission_sha256": inputs["p02b_admission"]["sha256"],
        "profiles_sha256": inputs["profiles"]["sha256"],
        "invocation_config_sha256": inputs["invocation_config"]["sha256"],
        "resolved_profile_sha256": inputs["resolved_profile"]["sha256"],
        "command_sha256": inputs["command"]["sha256"],
        "p20_runner_sha256": inputs["p20_runner"]["sha256"],
        "p20_summarizer_sha256": inputs["p20_summarizer"]["sha256"],
        "p31_wrapper_sha256": inputs["p31_wrapper"]["sha256"],
        "pristine_store_sha256": inputs["pristine_store"]["sha256"],
        "pristine_store_manifest_sha256": inputs["pristine_store_manifest"]["sha256"],
        "stage_store_provenance_sha256": inputs["stage_store_provenance"]["sha256"],
        "stage_store_post_state_sha256": inputs["stage_store_post_state"]["sha256"],
        "stage_store_post_quick_inventory_sha256": recorded_post_inventory.get(
            "quick_inventory_sha256", ""
        ),
        "input_manifest_sha256": sha256_file(inputs_path),
        "raw_output_sha256": sha256_file(raw_path),
        "p31_manifest_sha256": p31["manifest_sha"],
        "p31_validation_sha256": p31["validation_sha"],
        "p31_done_sha256": p31["done_sha"],
    }
    require(correctness.get("state") == "PASS" and correctness.get("mismatches") == 0, "correctness gate drift")
    require(
        correctness.get("scale") == resolved.get("scale")
        and correctness.get("workload") == resolved.get("workload", {}).get("name")
        and correctness.get("covered_stages")
        == CANONICAL_STAGES.get(resolved.get("scale")),
        "correctness gate identity/coverage drift",
    )
    expected_queries_per_stage = correctness.get("expected_queries_per_stage")
    correctness_checked = correctness.get("checked")
    require(
        isinstance(expected_queries_per_stage, int)
        and not isinstance(expected_queries_per_stage, bool)
        and expected_queries_per_stage > 0
        and isinstance(correctness_checked, int)
        and not isinstance(correctness_checked, bool)
        and correctness_checked
        == expected_queries_per_stage * len(CANONICAL_STAGES[resolved.get("scale")]),
        "correctness gate checked count drift",
    )
    correctness_sha_fields = {
        "binary_sha256": inputs["binary"]["sha256"],
        "dataset_sha256": inputs["dataset"]["sha256"],
        "truth_sha256": inputs["truth"]["sha256"],
        "sample_plan_sha256": inputs["sample_plan"]["sha256"],
    }
    for name, expected_sha in correctness_sha_fields.items():
        actual = correctness.get(name)
        if name == "sample_plan_sha256" and actual is None:
            actual = correctness.get("query_or_trace_sha256")
        require(actual == expected_sha, "correctness gate SHA drift: {}".format(name))

    rows = []
    phase_sum_all = 0
    for entry_index, benchmark in enumerate(benchmarks):
        require(isinstance(benchmark, dict), "benchmark entry must be an object")
        require(benchmark.get("repeats") == 1, "benchmark repeats drift")
        sampled_vertices = number(benchmark.get("sampled_vertices"), "sampled_vertices", positive=True)
        rounds = benchmark.get("rounds")
        require(isinstance(rounds, list) and len(rounds) == 1, "benchmark must have one measured round")
        round_value = rounds[0]
        require(isinstance(round_value, dict), "measured round must be an object")
        require(round_value.get("kind") == "measured" and round_value.get("round") == 1, "measured round identity drift")
        summary = round_value.get("neighbor_summary")
        require(isinstance(summary, dict), "neighbor_summary is missing")
        operations = number(summary.get("get_neighbors_ops"), "measured operations", positive=True)
        neighbor_metrics = round_value.get("neighbor_metrics")
        require(isinstance(neighbor_metrics, dict), "neighbor_metrics is missing")
        storage_metrics = neighbor_metrics.get("storage")
        require(isinstance(storage_metrics, dict), "neighbor storage metrics are missing")
        latency_histogram_json = validate_latency_histogram(
            storage_metrics.get("get_neighbors_latency"), operations
        )
        elapsed_ms = number(round_value.get("get_neighbors_elapsed_ms"), "elapsed_ms", positive=True)
        require(operations == sampled_vertices, "measured operation/sample coverage drift")
        digests = benchmark.get("result_digests")
        require(isinstance(digests, list) and len(digests) == sampled_vertices, "result digest coverage drift")
        seen_srcs = set()
        for digest in digests:
            require(isinstance(digest, dict), "result digest must be an object")
            require(isinstance(digest.get("src"), int), "result digest src missing")
            require(digest["src"] not in seen_srcs, "duplicate result digest src")
            seen_srcs.add(digest["src"])
            number(digest.get("result_count"), "result_count")
            require(bool(DIGEST_RE.fullmatch(digest.get("result_digest", ""))), "invalid result digest")
        entry_digest = benchmark.get("entry_result_digest")
        require(bool(DIGEST_RE.fullmatch(entry_digest or "")), "entry result digest is missing/invalid")
        phase_values = {}
        for field in CPU_PHASE_FIELDS:
            phase_values[field] = int(number(round_value.get(field), field))
        clock_failures = int(number(round_value.get("query_cpu_clock_failures"), "query CPU clock failures"))
        require(clock_failures == 0, "query CPU clock failure")
        phase_sum = sum(phase_values.values())
        if resolved["mode"].get("query_cpu_phases"):
            require(phase_sum > 0, "CPU-phase mode emitted no phase CPU data")
            query_cpu_total_ns = int(
                number(round_value.get("query_cpu_total_ns"), "query_cpu_total_ns", positive=True)
            )
            require(
                query_cpu_total_ns >= phase_sum,
                "query CPU total is below summed mutually exclusive phases",
            )
        else:
            require(phase_sum == 0, "CPU phase data present while instrumentation is disabled")
            require(
                round_value.get("query_cpu_total_ns") is None,
                "query CPU total present while instrumentation is disabled",
            )
            query_cpu_total_ns = ""
        phase_sum_all += phase_sum
        row = dict(common)
        row.update(
            {
                "benchmark_entry_index": entry_index,
                "edge_type": "" if benchmark.get("edge_type") is None else benchmark.get("edge_type"),
                "src_label": "" if benchmark.get("src_label") is None else benchmark.get("src_label"),
                "dst_label": "" if benchmark.get("dst_label") is None else benchmark.get("dst_label"),
                "sampled_vertices": int(sampled_vertices),
                "measured_operations": int(operations),
                "warmup_runs": benchmark.get("warmup_runs"),
                "training_runs": raw.get("training_runs"),
                "training_feedback_compactions": raw.get("training_feedback_compactions"),
                "measured_rounds": 1,
                "elapsed_ms": elapsed_ms,
                "qps": float(operations) * 1000.0 / float(elapsed_ms),
                "neighbor_edges": int(number(round_value.get("neighbor_edges"), "neighbor_edges")),
                "get_neighbors_avg_us": number(summary.get("get_neighbors_avg_us"), "avg latency"),
                "get_neighbors_p50_us": number(summary.get("get_neighbors_p50_us"), "p50 latency"),
                "get_neighbors_p90_us": number(summary.get("get_neighbors_p90_us"), "p90 latency"),
                "get_neighbors_p99_us": number(summary.get("get_neighbors_p99_us"), "p99 latency"),
                "get_neighbors_latency_histogram_json": latency_histogram_json,
                "candidate_segments_total": int(number(summary.get("candidate_l0_segments"), "candidate segments")),
                "routed_segments_total": int(number(summary.get("routed_l0_segments"), "routed segments")),
                "body_read_segments_total": int(number(summary.get("body_reads"), "body reads")),
                "logical_read_bytes_total": int(number(summary.get("read_bytes"), "logical read bytes")),
                "logical_body_bytes_total": int(number(summary.get("body_bytes"), "logical body bytes")),
                "query_cpu_phase_sum_ns": phase_sum,
                "query_cpu_total_ns": query_cpu_total_ns,
                "query_cpu_clock_failures": clock_failures,
                "result_digest_count": len(digests),
                "entry_result_digest": entry_digest,
            }
        )
        row.update(phase_values)
        require(set(row) == set(SUMMARY_COLUMNS), "internal summary schema mismatch")
        rows.append(row)
    require(process_cpu_total_ns >= phase_sum_all, "P31 process CPU is below summed query phases")
    return rows


def write_summary(path, rows):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=SUMMARY_COLUMNS,
            delimiter="\t",
            lineterminator="\n",
            extrasaction="raise",
        )
        writer.writeheader()
        writer.writerows(rows)
    os.replace(str(tmp), str(path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True, type=Path)
    args = parser.parse_args()
    try:
        rows = build_rows(args.run_root)
        write_summary(args.run_root / "summary.tsv", rows)
        print(str(args.run_root / "summary.tsv"))
        return 0
    except (SummaryError, OSError) as exc:
        print("FAIL: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
