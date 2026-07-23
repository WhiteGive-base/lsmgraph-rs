#!/usr/bin/env python3
"""Canonical downstream admission check for a P02B sentinel result."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from p02b_common import GateError, same_resolved_path, sha256_file


RESULT_SCHEMA = "p02b-sf10-sentinel-result-v1"
PROVENANCE_SCHEMA = "p02b-sentinel-provenance-v1"
CV_SCHEMA = "p02b-sentinel-cv-v1"
METRICS_SCHEMA = "p02b-sentinel-run-metrics-v1"
P31_MANIFEST_SCHEMA = "cidr-run-manifest-v1"
P31_RESOURCE_SCHEMA = "cidr-resource-v1"
CACHE_POLICY = "no-drop-caches;independent-process;in-process-warmup;os-cache-as-is"
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
HEX16_RE = re.compile(r"^[0-9a-f]{16}$")
GIT_HEAD_RE = re.compile(r"^[0-9a-f]{40,64}$")
CPUSET_RE = re.compile(r"^[0-9,-]+$")
CANONICAL_CONSUMERS = ["P10", "P20"]
RESULT_KEYS = {
    "schema_version",
    "state",
    "fixture_only",
    "performance_eligible",
    "formal_gate_eligible",
    "downstream_release_eligible",
    "consumers",
    "task_id",
    "scale",
    "run_id",
    "started_at_utc",
    "completed_at_utc",
    "clean_ready",
    "protocol",
    "provenance",
    "correctness",
    "stability",
    "repeats",
}
PROVENANCE_KEYS = {
    "schema_version",
    "created_at_utc",
    "repo",
    "fixture_mode",
    "files",
    "dataset",
    "store",
    "id_map",
    "query_plan_summary",
    "clean_ready_binding_sha256",
}
PROVENANCE_FILE_KEYS = {
    "binary",
    "truth",
    "query_plan",
    "config",
    "dataset_manifest",
    "store_manifest",
    "id_map_manifest",
    "p31_wrapper",
}
PROTOCOL_KEYS = {
    "independent_process_runs",
    "expected_queries",
    "warmup_runs",
    "measured_repeats",
    "minimum_measured_seconds_per_run",
    "cache_policy",
    "cpu",
    "io_backend",
    "l0_layout",
    "semantic_degree_hint",
    "force_signature",
    "p31",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise GateError(message)


def _unique_object(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GateError("duplicate JSON key: {}".format(key))
        result[key] = value
    return result


def read_json(path: Path, context: str) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GateError("cannot read {} {}: {}".format(context, path, exc)) from exc
    try:
        value = json.loads(text, object_pairs_hook=_unique_object)
    except GateError:
        raise
    except ValueError as exc:
        raise GateError("cannot parse {} {}: {}".format(context, path, exc)) from exc
    if not isinstance(value, dict):
        raise GateError("{} must contain one JSON object".format(context))
    return value


def require_exact_keys(value: Dict[str, Any], expected: Iterable[str], context: str) -> None:
    expected_set = set(expected)
    missing = sorted(expected_set - set(value))
    unknown = sorted(set(value) - expected_set)
    if missing or unknown:
        raise GateError(
            "{} key drift (missing={}, unknown={})".format(context, missing, unknown)
        )


def require_int(value: Any, context: str, minimum: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GateError("{} must be an integer".format(context))
    if minimum is not None and value < minimum:
        raise GateError("{} must be at least {}".format(context, minimum))
    return value


def require_number(value: Any, context: str, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GateError("{} must be numeric".format(context))
    result = float(value)
    if not math.isfinite(result) or result < 0 or (positive and result <= 0):
        raise GateError("{} has an invalid numeric value".format(context))
    return result


def require_hex64(value: Any, context: str) -> str:
    if not isinstance(value, str) or not HEX64_RE.fullmatch(value):
        raise GateError("{} must be a lowercase SHA-256".format(context))
    return value


def parse_timestamp(value: Any, context: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise GateError("{} timestamp is missing".format(context))
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise GateError("{} has an invalid timestamp".format(context)) from exc
    if parsed.tzinfo is None:
        raise GateError("{} timestamp must include a timezone".format(context))
    return parsed.astimezone(dt.timezone.utc)


def canonical_path(raw: Any, context: str, require_file: bool = False, require_dir: bool = False) -> Path:
    if not isinstance(raw, str) or not raw or not Path(raw).is_absolute():
        raise GateError("{} must be an absolute path".format(context))
    path = Path(raw).resolve()
    if str(path) != raw:
        raise GateError("{} must be a canonical resolved path".format(context))
    if require_file and not path.is_file():
        raise GateError("{} is not a current file".format(context))
    if require_dir and not path.is_dir():
        raise GateError("{} is not a current directory".format(context))
    return path


def validate_file_ref(
    reference: Any,
    context: str,
    expected_path: Optional[Path] = None,
) -> Path:
    if not isinstance(reference, dict):
        raise GateError("{} file reference is not an object".format(context))
    for key in ("path", "size_bytes", "sha256"):
        if key not in reference:
            raise GateError("{} file reference is missing {}".format(context, key))
    path = canonical_path(reference["path"], "{} path".format(context), require_file=True)
    if expected_path is not None and path != expected_path.resolve():
        raise GateError("{} points to the wrong file".format(context))
    size = require_int(reference["size_bytes"], "{} size_bytes".format(context), 0)
    digest = require_hex64(reference["sha256"], "{} sha256".format(context))
    if path.stat().st_size != size:
        raise GateError("{} size has changed".format(context))
    if sha256_file(path) != digest:
        raise GateError("{} content has changed".format(context))
    return path


def validate_hash_ref(
    reference: Any,
    context: str,
    expected_path: Optional[Path] = None,
) -> Path:
    if not isinstance(reference, dict):
        raise GateError("{} hash reference is not an object".format(context))
    path = canonical_path(reference.get("path"), "{} path".format(context), require_file=True)
    if expected_path is not None and path != expected_path.resolve():
        raise GateError("{} points to the wrong file".format(context))
    digest = require_hex64(reference.get("sha256"), "{} sha256".format(context))
    if sha256_file(path) != digest:
        raise GateError("{} content has changed".format(context))
    return path


def expand_cpuset(raw: Any, context: str) -> Set[int]:
    if not isinstance(raw, str) or not raw or not CPUSET_RE.fullmatch(raw):
        raise GateError("{} has invalid cpuset syntax".format(context))
    cpus: Set[int] = set()
    for token in raw.split(","):
        if not token:
            raise GateError("{} has an empty cpuset token".format(context))
        if "-" in token:
            if token.count("-") != 1:
                raise GateError("{} has an invalid range".format(context))
            left, right = (int(item) for item in token.split("-", 1))
            if left > right:
                raise GateError("{} has a descending range".format(context))
            cpus.update(range(left, right + 1))
        else:
            cpus.add(int(token))
    if not cpus:
        raise GateError("{} expands to no CPUs".format(context))
    return cpus


def validate_protocol(protocol: Any, fixture: bool) -> Dict[str, Any]:
    if not isinstance(protocol, dict):
        raise GateError("sentinel protocol is not an object")
    require_exact_keys(protocol, PROTOCOL_KEYS, "sentinel protocol")
    runs = require_int(protocol["independent_process_runs"], "protocol runs", 3)
    expected_queries = require_int(protocol["expected_queries"], "protocol expected_queries", 1)
    require_int(protocol["warmup_runs"], "protocol warmup_runs", 0)
    repeats = require_int(protocol["measured_repeats"], "protocol measured_repeats", 1)
    minimum_seconds = require_number(
        protocol["minimum_measured_seconds_per_run"],
        "protocol minimum_measured_seconds_per_run",
        positive=True,
    )
    if protocol["cache_policy"] != CACHE_POLICY:
        raise GateError("sentinel cache policy drift")
    if protocol["io_backend"] not in {"blocking", "uring"}:
        raise GateError("sentinel io_backend is invalid")
    if protocol["l0_layout"] not in {
        "schema",
        "naive",
        "kv-lsm",
        "edge-type-only",
        "semantic",
        "semantic-budgeted",
        "oracle",
    }:
        raise GateError("sentinel l0_layout is invalid")
    for key in ("semantic_degree_hint", "force_signature"):
        if not isinstance(protocol[key], bool):
            raise GateError("protocol {} must be boolean".format(key))

    cpu = protocol["cpu"]
    if not isinstance(cpu, dict):
        raise GateError("protocol cpu is not an object")
    require_exact_keys(cpu, {"housekeeping_cpuset", "formal_cpuset", "threads"}, "protocol cpu")
    housekeeping = expand_cpuset(cpu["housekeeping_cpuset"], "housekeeping cpuset")
    formal = expand_cpuset(cpu["formal_cpuset"], "formal cpuset")
    if housekeeping & formal:
        raise GateError("sentinel housekeeping/formal cpusets overlap")
    threads = require_int(cpu["threads"], "protocol threads", 1)
    if threads != len(formal):
        raise GateError("protocol threads differ from formal cpuset size")

    p31 = protocol["p31"]
    if not isinstance(p31, dict):
        raise GateError("protocol p31 is not an object")
    require_exact_keys(
        p31,
        {
            "device",
            "data_mount",
            "interval_seconds",
            "disk_interval_seconds",
            "min_samples",
            "require_aux_tools",
        },
        "protocol p31",
    )
    if not isinstance(p31["device"], str) or not p31["device"]:
        raise GateError("protocol p31.device is invalid")
    canonical_path(p31["data_mount"], "protocol p31.data_mount", require_dir=True)
    interval = require_number(p31["interval_seconds"], "protocol p31 interval", positive=True)
    disk_interval = require_number(
        p31["disk_interval_seconds"], "protocol p31 disk interval", positive=True
    )
    if disk_interval < interval:
        raise GateError("protocol p31 disk interval is too short")
    min_samples = require_int(p31["min_samples"], "protocol p31 min_samples", 2)
    if p31["require_aux_tools"] is not True:
        raise GateError("sentinel must require P31 auxiliary tools")
    if not fixture:
        if (
            runs != 3
            or expected_queries != 1700
            or minimum_seconds < 30.0
            or min_samples < 10
        ):
            raise GateError("formal sentinel protocol is not canonical")
    return {
        "independent_process_runs": runs,
        "expected_queries": expected_queries,
        "measured_repeats": repeats,
        "cpu": cpu,
        "p31": p31,
        "io_backend": protocol["io_backend"],
        "l0_layout": protocol["l0_layout"],
    }


def validate_repo(
    repo: Any,
    fixture: bool,
    expected_repo_root: Optional[Path],
    expected_repo_head: Optional[str],
) -> Tuple[str, str]:
    if not isinstance(repo, dict):
        raise GateError("sentinel provenance repo is not an object")
    require_exact_keys(repo, {"root", "head", "dirty", "status_lines"}, "provenance repo")
    root = canonical_path(repo["root"], "provenance repo root", require_dir=True)
    head = repo["head"]
    if not isinstance(head, str) or not GIT_HEAD_RE.fullmatch(head):
        raise GateError("provenance repo head is invalid")
    if not isinstance(repo["dirty"], bool):
        raise GateError("provenance repo dirty must be boolean")
    status_lines = repo["status_lines"]
    if not isinstance(status_lines, list) or not all(isinstance(item, str) for item in status_lines):
        raise GateError("provenance repo status_lines are invalid")
    if repo["dirty"] is not bool(status_lines):
        raise GateError("provenance repo dirty/status_lines disagree")
    if not fixture and repo["dirty"]:
        raise GateError("formal sentinel provenance reports a dirty repository")
    if expected_repo_root is not None and root != expected_repo_root.resolve():
        raise GateError("sentinel repo root differs from expected repo root")
    if expected_repo_head is not None:
        if not GIT_HEAD_RE.fullmatch(expected_repo_head):
            raise GateError("expected repo head is invalid")
        if head != expected_repo_head:
            raise GateError("sentinel repo head differs from expected repo head")
    return str(root), head


def validate_lineage(
    value: Any,
    kind: str,
    manifest_ref: Dict[str, Any],
    expected_result_sha: Any,
) -> None:
    context = "provenance {}".format(kind)
    if not isinstance(value, dict):
        raise GateError("{} is not an object".format(context))
    require_exact_keys(value, {"manifest", "root", "content_sha256", "hash_method"}, context)
    manifest_path = validate_file_ref(value["manifest"], "{} manifest".format(context))
    if (
        manifest_path != Path(manifest_ref["path"]).resolve()
        or value["manifest"]["size_bytes"] != manifest_ref["size_bytes"]
        or value["manifest"]["sha256"] != manifest_ref["sha256"]
    ):
        raise GateError("{} manifest reference drift".format(context))
    root = canonical_path(value["root"], "{} root".format(context), require_dir=True)
    content_sha = require_hex64(value["content_sha256"], "{} content sha256".format(context))
    if not isinstance(value["hash_method"], str) or not value["hash_method"]:
        raise GateError("{} hash method is invalid".format(context))
    manifest = read_json(manifest_path, "{} manifest".format(kind))
    root_key = "dataset_root" if kind == "dataset" else "store_path"
    digest_key = "{}_sha256".format(kind)
    if manifest.get("schema_version") != "p02b-{}-manifest-v1".format(kind):
        raise GateError("{} manifest schema drift".format(context))
    if not same_resolved_path(manifest.get(root_key), root):
        raise GateError("{} manifest root drift".format(context))
    if manifest.get(digest_key) != content_sha or manifest.get("hash_method") != value["hash_method"]:
        raise GateError("{} manifest content/hash-method drift".format(context))
    if expected_result_sha != content_sha:
        raise GateError("sentinel result {} digest drift".format(kind))


def validate_id_map(value: Any, files: Dict[str, Any]) -> None:
    if not isinstance(value, dict):
        raise GateError("provenance id_map is not an object")
    require_exact_keys(
        value,
        {
            "path",
            "manifest",
            "formal_pass",
            "checksums",
            "mapping_hash",
            "mapping_hash_algorithm",
            "dense_to_original_sha256",
            "original_to_dense_sha256",
        },
        "provenance id_map",
    )
    root = canonical_path(value["path"], "id_map path", require_dir=True)
    manifest_path = validate_file_ref(
        value["manifest"], "id_map manifest", root / "id-map-manifest.json"
    )
    formal_path = validate_file_ref(value["formal_pass"], "id_map FORMAL-PASS", root / "FORMAL-PASS")
    validate_file_ref(value["checksums"], "id_map SHA256SUMS", root / "SHA256SUMS")
    file_manifest = files["id_map_manifest"]
    if (
        manifest_path != Path(file_manifest["path"]).resolve()
        or value["manifest"]["size_bytes"] != file_manifest["size_bytes"]
        or value["manifest"]["sha256"] != file_manifest["sha256"]
    ):
        raise GateError("id_map manifest/file provenance drift")
    if value["mapping_hash_algorithm"] != "fnv1a64-le-dense-original-v1":
        raise GateError("id_map mapping algorithm drift")
    if not isinstance(value["mapping_hash"], str) or not HEX16_RE.fullmatch(value["mapping_hash"]):
        raise GateError("id_map mapping hash is invalid")
    dense_sha = require_hex64(value["dense_to_original_sha256"], "id_map dense SHA")
    original_sha = require_hex64(value["original_to_dense_sha256"], "id_map original SHA")
    manifest = read_json(manifest_path, "id_map manifest")
    if (
        manifest.get("format") != "seml0-shared-id-map"
        or manifest.get("format_version") != 1
        or manifest.get("status") != "PASS"
        or manifest.get("formal_pass") is not True
        or manifest.get("verification_complete") is not True
        or manifest.get("mapping_hash") != value["mapping_hash"]
        or manifest.get("mapping_hash_algorithm") != value["mapping_hash_algorithm"]
        or manifest.get("dense_to_original", {}).get("sha256") != dense_sha
        or manifest.get("original_to_dense", {}).get("sha256") != original_sha
    ):
        raise GateError("id_map manifest internal consistency failed")
    marker_tokens = formal_path.read_text(encoding="utf-8").strip().split()
    if marker_tokens != ["id-map-manifest.json", "sha256", sha256_file(manifest_path)]:
        raise GateError("id_map FORMAL-PASS does not bind its manifest")


def validate_clean_ready(run_dir: Path, value: Any, provenance: Dict[str, Any]) -> None:
    if not isinstance(value, dict):
        raise GateError("clean_ready is not an object")
    common_keys = {
        "schema_version",
        "state",
        "run_id",
        "ready_time",
        "age_seconds_at_binding",
        "required_consecutive_samples",
        "observed_consecutive_samples",
        "git_head",
        "host",
        "artifacts",
    }
    schema = value.get("schema_version")
    if schema == "p02b-clean-ready-binding-v1":
        require_exact_keys(value, common_keys, "clean_ready")
    elif schema == "p02b-clean-ready-binding-v2":
        require_exact_keys(
            value,
            common_keys | {"protocol_version", "source_v1_history_preserved", "timing"},
            "clean_ready v2",
        )
    else:
        raise GateError("clean_ready schema is unsupported")
    if value["state"] != "PASS":
        raise GateError("clean_ready schema/state drift")
    if not isinstance(value["run_id"], str) or not value["run_id"]:
        raise GateError("clean_ready run_id is invalid")
    parse_timestamp(value["ready_time"], "clean_ready ready_time")
    require_number(value["age_seconds_at_binding"], "clean_ready age")
    required = require_int(value["required_consecutive_samples"], "clean_ready required samples", 1)
    observed = require_int(value["observed_consecutive_samples"], "clean_ready observed samples", 1)
    if observed < required:
        raise GateError("clean_ready observed samples are insufficient")
    if schema == "p02b-clean-ready-binding-v2":
        if value["protocol_version"] != "short-clean-window-v2" or required < 5:
            raise GateError("clean_ready v2 protocol/sample count drift")
        if not isinstance(value["source_v1_history_preserved"], bool):
            raise GateError("clean_ready v2 history flag must be boolean")
        timing = value["timing"]
        if not isinstance(timing, dict):
            raise GateError("clean_ready v2 timing must be an object")
        require_exact_keys(
            timing,
            {
                "expected_interval_seconds",
                "gap_tolerance_seconds",
                "observed_gap_seconds",
                "maximum_observed_gap_seconds",
                "gap_check_pass",
            },
            "clean_ready v2 timing",
        )
        if timing["expected_interval_seconds"] != 60 or timing["gap_check_pass"] is not True:
            raise GateError("clean_ready v2 interval/gap classification drift")
        tolerance = require_number(timing["gap_tolerance_seconds"], "clean_ready v2 tolerance")
        if tolerance < 0 or tolerance > 15:
            raise GateError("clean_ready v2 gap tolerance drift")
        gaps = timing["observed_gap_seconds"]
        if not isinstance(gaps, list) or len(gaps) != required - 1:
            raise GateError("clean_ready v2 gap evidence count drift")
        observed_gaps = [
            require_number(gap, "clean_ready v2 observed gap", positive=True) for gap in gaps
        ]
        lower = 60.0 - tolerance
        upper = 60.0 + tolerance
        if any(gap < lower or gap > upper for gap in observed_gaps):
            raise GateError("clean_ready v2 observed gap is outside tolerance")
        recorded_maximum = require_number(
            timing["maximum_observed_gap_seconds"], "clean_ready v2 maximum gap", positive=True
        )
        if not math.isclose(recorded_maximum, max(observed_gaps), rel_tol=1e-9, abs_tol=1e-9):
            raise GateError("clean_ready v2 maximum gap summary drift")
    if not isinstance(value["git_head"], str) or not isinstance(value["host"], str) or not value["host"]:
        raise GateError("clean_ready git/host identity is invalid")
    artifacts = value["artifacts"]
    if not isinstance(artifacts, dict) or not artifacts:
        raise GateError("clean_ready artifacts are missing")
    if schema == "p02b-clean-ready-binding-v2" and set(artifacts) != {
        "READY",
        "COMPLETE",
        "classification.env",
        "STATE",
        "samples.tsv",
        "latest.tsv",
        "monitor_clean_window.sh",
    }:
        raise GateError("clean_ready v2 artifact set drift")
    for name, reference in artifacts.items():
        if not isinstance(name, str) or not name:
            raise GateError("clean_ready artifact name is invalid")
        validate_file_ref(reference, "clean_ready artifact {}".format(name))
    binding_path = run_dir / "clean-ready-binding.json"
    if sha256_file(binding_path) != require_hex64(
        provenance["clean_ready_binding_sha256"], "clean_ready binding SHA"
    ):
        raise GateError("clean_ready binding SHA drift")
    if read_json(binding_path, "clean_ready binding") != value:
        raise GateError("clean_ready result/binding content drift")


def validate_provenance(
    run_dir: Path,
    result: Dict[str, Any],
    fixture: bool,
    expected_repo_root: Optional[Path],
    expected_repo_head: Optional[str],
    expected_binary_sha256: Optional[str],
) -> Tuple[Path, str, str, str]:
    reference = result["provenance"]
    if not isinstance(reference, dict):
        raise GateError("sentinel provenance reference is not an object")
    require_exact_keys(
        reference,
        {
            "path",
            "sha256",
            "repo_head",
            "binary_sha256",
            "truth_sha256",
            "query_plan_sha256",
            "store_sha256",
            "dataset_sha256",
            "config_sha256",
        },
        "sentinel provenance reference",
    )
    provenance_path = canonical_path(
        reference["path"], "sentinel provenance path", require_file=True
    )
    expected_path = (run_dir / "provenance.json").resolve()
    if provenance_path != expected_path:
        raise GateError("sentinel provenance must be the adjacent provenance.json")
    provenance_sha = require_hex64(reference["sha256"], "sentinel provenance SHA")
    if sha256_file(provenance_path) != provenance_sha:
        raise GateError("sentinel provenance SHA does not match provenance.json")
    provenance = read_json(provenance_path, "sentinel provenance")
    require_exact_keys(provenance, PROVENANCE_KEYS, "sentinel provenance")
    if provenance["schema_version"] != PROVENANCE_SCHEMA:
        raise GateError("sentinel provenance schema drift")
    parse_timestamp(provenance["created_at_utc"], "provenance created_at")
    if provenance["fixture_mode"] is not fixture:
        raise GateError("sentinel result/provenance fixture classification drift")
    repo_root, repo_head = validate_repo(
        provenance["repo"], fixture, expected_repo_root, expected_repo_head
    )
    if reference["repo_head"] != repo_head:
        raise GateError("sentinel result/provenance repo head drift")

    files = provenance["files"]
    if not isinstance(files, dict):
        raise GateError("provenance files is not an object")
    require_exact_keys(files, PROVENANCE_FILE_KEYS, "provenance files")
    for name, file_reference in files.items():
        validate_file_ref(file_reference, "provenance file {}".format(name))
    binary_sha = files["binary"]["sha256"]
    if reference["binary_sha256"] != binary_sha:
        raise GateError("sentinel result/provenance binary SHA drift")
    if expected_binary_sha256 is not None:
        require_hex64(expected_binary_sha256, "expected binary SHA")
        if binary_sha != expected_binary_sha256:
            raise GateError("sentinel binary SHA differs from expected binary SHA")
    for result_key, file_key in (
        ("truth_sha256", "truth"),
        ("query_plan_sha256", "query_plan"),
        ("config_sha256", "config"),
    ):
        if reference[result_key] != files[file_key]["sha256"]:
            raise GateError("sentinel result/provenance {} drift".format(result_key))

    validate_lineage(
        provenance["dataset"], "dataset", files["dataset_manifest"], reference["dataset_sha256"]
    )
    validate_lineage(
        provenance["store"], "store", files["store_manifest"], reference["store_sha256"]
    )
    validate_id_map(provenance["id_map"], files)
    plan_summary = provenance["query_plan_summary"]
    if (
        not isinstance(plan_summary, dict)
        or plan_summary.get("version") != 1
        or plan_summary.get("source") != "shared-truth-tsv"
        or plan_summary.get("sha256") != files["query_plan"]["sha256"]
        or require_int(plan_summary.get("queries"), "query plan summary queries", 1)
        != result["protocol"]["expected_queries"]
    ):
        raise GateError("query plan provenance summary drift")
    return provenance_path, provenance_sha, repo_root, repo_head


def validate_correctness(
    run_dir: Path,
    correctness: Any,
    protocol_summary: Dict[str, Any],
    result: Dict[str, Any],
) -> None:
    if not isinstance(correctness, dict):
        raise GateError("sentinel correctness is not an object")
    require_exact_keys(
        correctness,
        {
            "state",
            "checked",
            "mismatches",
            "total_neighbors",
            "mapping_hash",
            "result",
            "regenerated_query_plan",
        },
        "sentinel correctness",
    )
    if correctness["state"] != "PASS":
        raise GateError("sentinel shared-truth correctness gate failed")
    if require_int(correctness["checked"], "correctness checked", 1) != protocol_summary["expected_queries"]:
        raise GateError("sentinel correctness coverage drift")
    if require_int(correctness["mismatches"], "correctness mismatches", 0) != 0:
        raise GateError("sentinel correctness mismatch gate failed")
    require_int(correctness["total_neighbors"], "correctness total_neighbors", 0)
    if not isinstance(correctness["mapping_hash"], str) or not HEX16_RE.fullmatch(
        correctness["mapping_hash"]
    ):
        raise GateError("sentinel correctness mapping_hash is invalid")
    validate_file_ref(
        correctness["result"], "correctness result", run_dir / "shared-truth-result.json"
    )
    generated = validate_file_ref(
        correctness["regenerated_query_plan"],
        "correctness regenerated query plan",
        run_dir / "regenerated-query-plan.json",
    )
    if sha256_file(generated) != result["provenance"]["query_plan_sha256"]:
        raise GateError("correctness regenerated query plan differs from provenance")


def validate_cv(stability: Any, protocol_summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(stability, dict):
        raise GateError("sentinel stability is not an object")
    require_exact_keys(
        stability,
        {
            "schema_version",
            "state",
            "method",
            "independent_process_runs",
            "query_count_per_run",
            "qps",
            "p99_us",
            "runs",
        },
        "sentinel stability",
    )
    if stability["schema_version"] != CV_SCHEMA or stability["state"] != "PASS":
        raise GateError("sentinel stability schema/state drift")
    if stability["method"] != "sample_standard_deviation_over_arithmetic_mean":
        raise GateError("sentinel stability method drift")
    expected_runs = protocol_summary["independent_process_runs"]
    if require_int(stability["independent_process_runs"], "stability run count", 3) != expected_runs:
        raise GateError("sentinel stability run coverage drift")
    expected_query_count = protocol_summary["expected_queries"] * protocol_summary["measured_repeats"]
    if require_int(stability["query_count_per_run"], "stability query count", 1) != expected_query_count:
        raise GateError("sentinel stability query count drift")
    run_records = stability["runs"]
    if not isinstance(run_records, list) or len(run_records) != expected_runs:
        raise GateError("sentinel stability run records are incomplete")

    for metric_name, hard_limit in (("qps", 0.07), ("p99_us", 0.05)):
        metric = stability[metric_name]
        if not isinstance(metric, dict):
            raise GateError("stability {} is not an object".format(metric_name))
        require_exact_keys(
            metric,
            {"values", "mean", "sample_stdev", "cv", "maximum_cv", "pass"},
            "stability {}".format(metric_name),
        )
        values = metric["values"]
        if not isinstance(values, list) or len(values) != expected_runs:
            raise GateError("stability {} values coverage drift".format(metric_name))
        numeric_values = [
            require_number(item, "stability {} value".format(metric_name), positive=True)
            for item in values
        ]
        expected_mean = statistics.mean(numeric_values)
        expected_stdev = statistics.stdev(numeric_values)
        expected_cv = expected_stdev / expected_mean
        for field, expected in (
            ("mean", expected_mean),
            ("sample_stdev", expected_stdev),
            ("cv", expected_cv),
        ):
            actual = require_number(metric[field], "stability {} {}".format(metric_name, field))
            if not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-15):
                raise GateError("stability {} {} was not recomputed correctly".format(metric_name, field))
        maximum = require_number(
            metric["maximum_cv"], "stability {} maximum_cv".format(metric_name), positive=True
        )
        if maximum > hard_limit or expected_cv > maximum or metric["pass"] is not True:
            raise GateError("sentinel {} CV sub-gate failed".format(metric_name))

    seen_indices: Set[int] = set()
    seen_ids: Set[str] = set()
    for index, record in enumerate(run_records, start=1):
        if not isinstance(record, dict):
            raise GateError("stability run record is not an object")
        require_exact_keys(
            record,
            {"path", "sha256", "run_index", "run_id", "query_count", "qps", "p99_us", "measured_seconds"},
            "stability run record",
        )
        metric_path = validate_hash_ref(record, "stability run {} metrics".format(index))
        run_index = require_int(record["run_index"], "stability run_index", 1)
        run_id = record["run_id"]
        if run_index != index or not isinstance(run_id, str) or not run_id:
            raise GateError("stability run identity drift")
        if run_index in seen_indices or run_id in seen_ids:
            raise GateError("duplicate stability run identity")
        seen_indices.add(run_index)
        seen_ids.add(run_id)
        if require_int(record["query_count"], "stability run query_count", 1) != expected_query_count:
            raise GateError("stability run query count drift")
        qps = require_number(record["qps"], "stability run qps", positive=True)
        p99 = require_number(record["p99_us"], "stability run p99", positive=True)
        require_number(record["measured_seconds"], "stability measured_seconds", positive=True)
        if qps != float(stability["qps"]["values"][index - 1]) or p99 != float(
            stability["p99_us"]["values"][index - 1]
        ):
            raise GateError("stability run/aggregate values drift")
        metric_document = read_json(metric_path, "stability run metrics")
        if (
            metric_document.get("schema_version") != METRICS_SCHEMA
            or metric_document.get("state") != "PASS"
            or metric_document.get("run_index") != run_index
            or metric_document.get("run_id") != run_id
            or metric_document.get("query_count") != record["query_count"]
            or float(metric_document.get("qps", -1)) != qps
            or float(metric_document.get("p99_us", -1)) != p99
        ):
            raise GateError("stability run metrics document drift")
    return run_records


def validate_repeats(
    result: Dict[str, Any],
    protocol_summary: Dict[str, Any],
    stability_runs: List[Dict[str, Any]],
    repo_head: str,
    fixture: bool,
) -> Dict[str, str]:
    repeats = result["repeats"]
    expected_runs = protocol_summary["independent_process_runs"]
    if not isinstance(repeats, list) or len(repeats) != expected_runs:
        raise GateError("sentinel repeat coverage drift")
    host: Optional[Dict[str, str]] = None
    for index, repeat in enumerate(repeats, start=1):
        if not isinstance(repeat, dict):
            raise GateError("sentinel repeat is not an object")
        require_exact_keys(repeat, {"run_index", "run_id", "metrics", "p31"}, "sentinel repeat")
        if require_int(repeat["run_index"], "repeat run_index", 1) != index:
            raise GateError("sentinel repeat index drift")
        run_id = repeat["run_id"]
        if not isinstance(run_id, str) or not run_id:
            raise GateError("sentinel repeat run_id is invalid")
        metrics_path = validate_file_ref(repeat["metrics"], "repeat metrics")
        stability_ref = stability_runs[index - 1]
        if (
            metrics_path != Path(stability_ref["path"]).resolve()
            or repeat["metrics"]["sha256"] != stability_ref["sha256"]
            or run_id != stability_ref["run_id"]
        ):
            raise GateError("sentinel repeat/stability metrics drift")

        p31 = repeat["p31"]
        if not isinstance(p31, dict):
            raise GateError("sentinel repeat P31 reference is not an object")
        require_exact_keys(p31, {"run_dir", "manifest", "validation", "done"}, "repeat P31")
        p31_root = canonical_path(p31["run_dir"], "repeat P31 run_dir", require_dir=True)
        manifest_path = validate_file_ref(
            p31["manifest"], "repeat P31 manifest", p31_root / "run-manifest.json"
        )
        validation_path = validate_file_ref(
            p31["validation"], "repeat P31 validation", p31_root / "validation.json"
        )
        done_path = validate_file_ref(p31["done"], "repeat P31 DONE", p31_root / "DONE")
        if (p31_root / "FAILED").exists():
            raise GateError("sentinel repeat P31 has a FAILED marker")
        manifest = read_json(manifest_path, "repeat P31 manifest")
        validation = read_json(validation_path, "repeat P31 validation")
        done = read_json(done_path, "repeat P31 DONE")
        if (
            manifest.get("schema_version") != P31_MANIFEST_SCHEMA
            or manifest.get("resource_schema_version") != P31_RESOURCE_SCHEMA
            or manifest.get("state") != "PASS"
            or manifest.get("run_id") != run_id
            or manifest.get("task_id") != "P02B-SF10-SENTINEL-r{}".format(index)
            or manifest.get("performance_eligible_declared") is not False
        ):
            raise GateError("sentinel repeat P31 manifest identity/state drift")
        manifest_repo = manifest.get("repo")
        if (
            not isinstance(manifest_repo, dict)
            or manifest_repo.get("git_sha") != repo_head
            or (not fixture and manifest_repo.get("dirty") is not False)
        ):
            raise GateError("sentinel repeat P31 repo provenance drift")
        current_host = manifest.get("host")
        if not isinstance(current_host, dict):
            raise GateError("sentinel repeat P31 host is missing")
        hostname = current_host.get("hostname")
        fingerprint = current_host.get("fingerprint_sha256")
        if not isinstance(hostname, str) or not hostname or not isinstance(fingerprint, str) or not HEX64_RE.fullmatch(fingerprint):
            raise GateError("sentinel repeat P31 host identity is invalid")
        host_summary = {"hostname": hostname, "fingerprint_sha256": fingerprint}
        if host is None:
            host = host_summary
        elif host != host_summary:
            raise GateError("sentinel repeats were collected on different hosts")
        if validation.get("state") != "PASS":
            raise GateError("sentinel repeat P31 validation is not PASS")
        if (
            done.get("state") != "PASS"
            or done.get("manifest_sha256") != sha256_file(manifest_path)
            or done.get("validation_sha256") != sha256_file(validation_path)
        ):
            raise GateError("sentinel repeat P31 DONE binding drift")
    if host is None:
        raise GateError("sentinel host evidence is missing")
    return host


def validate_result(
    result_path: Path,
    consumer: str,
    require_formal: bool,
    expected_repo_root: Optional[Path] = None,
    expected_repo_head: Optional[str] = None,
    expected_binary_sha256: Optional[str] = None,
    max_age_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    if consumer not in {"P10", "P20"}:
        raise GateError("unsupported sentinel consumer")
    if not isinstance(require_formal, bool):
        raise GateError("require_formal must be boolean")
    result_path = result_path.resolve()
    result = read_json(result_path, "sentinel result")
    require_exact_keys(result, RESULT_KEYS, "sentinel result")
    if result["schema_version"] != RESULT_SCHEMA or result["state"] != "PASS":
        raise GateError("sentinel result schema/state drift")
    if not isinstance(result["fixture_only"], bool):
        raise GateError("sentinel fixture_only must be boolean")
    fixture = result["fixture_only"]
    if result["performance_eligible"] is not False:
        raise GateError("sentinel gate must not claim paper-performance eligibility")
    if result["formal_gate_eligible"] is not (not fixture):
        raise GateError("sentinel formal eligibility/classification drift")
    if result["downstream_release_eligible"] is not (not fixture):
        raise GateError("sentinel downstream release/classification drift")
    if result["consumers"] != CANONICAL_CONSUMERS or consumer not in result["consumers"]:
        raise GateError("sentinel canonical consumer list drift")
    if require_formal and fixture:
        raise GateError("fixture sentinel cannot release a formal downstream run")
    if require_formal and (
        result["formal_gate_eligible"] is not True
        or result["downstream_release_eligible"] is not True
    ):
        raise GateError("sentinel has not released formal downstream experiments")
    if result["task_id"] != "P02B-SF10-SENTINEL" or result["scale"] != "sf10":
        raise GateError("sentinel task/scale identity drift")
    if not isinstance(result["run_id"], str) or not result["run_id"]:
        raise GateError("sentinel run_id is invalid")

    started = parse_timestamp(result["started_at_utc"], "sentinel started_at")
    completed = parse_timestamp(result["completed_at_utc"], "sentinel completed_at")
    if completed < started:
        raise GateError("sentinel completed before it started")
    now = dt.datetime.now(dt.timezone.utc)
    if completed > now:
        raise GateError("sentinel completion timestamp is in the future")
    if max_age_seconds is not None:
        maximum_age = require_number(max_age_seconds, "max_age_seconds", positive=True)
        if (now - completed).total_seconds() > maximum_age:
            raise GateError("sentinel result is older than max_age_seconds")

    run_dir = result_path.parent
    marker_name = "FIXTURE-PASS" if fixture else "PASS"
    opposite_marker = run_dir / ("PASS" if fixture else "FIXTURE-PASS")
    marker_path = run_dir / marker_name
    if opposite_marker.exists():
        raise GateError("sentinel run has an opposite-classification marker")
    if (run_dir / "FAILED").exists():
        raise GateError("sentinel run has a FAILED marker")
    marker = read_json(marker_path, "sentinel pass marker")
    require_exact_keys(marker, {"state", "fixture_only", "result", "result_sha256"}, "sentinel marker")
    if marker["state"] != "PASS" or marker["fixture_only"] is not fixture:
        raise GateError("sentinel marker classification is inconsistent")
    if not same_resolved_path(marker["result"], result_path):
        raise GateError("sentinel marker points to another result")
    result_sha = sha256_file(result_path)
    if marker["result_sha256"] != result_sha:
        raise GateError("sentinel marker does not bind sentinel-result.json")

    protocol_summary = validate_protocol(result["protocol"], fixture)
    provenance_path, provenance_sha, repo_root, repo_head = validate_provenance(
        run_dir,
        result,
        fixture,
        expected_repo_root.resolve() if expected_repo_root is not None else None,
        expected_repo_head,
        expected_binary_sha256,
    )
    validate_clean_ready(run_dir, result["clean_ready"], read_json(provenance_path, "sentinel provenance"))
    validate_correctness(run_dir, result["correctness"], protocol_summary, result)
    stability_runs = validate_cv(result["stability"], protocol_summary)
    host = validate_repeats(
        result, protocol_summary, stability_runs, repo_head, fixture
    )

    marker_sha = sha256_file(marker_path)
    binary_sha = result["provenance"]["binary_sha256"]
    return {
        "state": "PASS",
        "consumer": consumer,
        "formal_required": require_formal,
        "fixture_only": fixture,
        "scope": "host-global",
        "sentinel_result": str(result_path),
        "sentinel_result_sha256": result_sha,
        "pass_marker": str(marker_path.resolve()),
        "pass_marker_sha256": marker_sha,
        "provenance": str(provenance_path),
        "provenance_sha256": provenance_sha,
        "scale": result["scale"],
        "run_id": result["run_id"],
        "completed_at_utc": result["completed_at_utc"],
        "repo_root": repo_root,
        "repo_head": repo_head,
        "binary_sha256": binary_sha,
        "host": host,
        "protocol": {
            "cpu": protocol_summary["cpu"],
            "p31": protocol_summary["p31"],
            "io_backend": protocol_summary["io_backend"],
            "l0_layout": protocol_summary["l0_layout"],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--consumer", required=True, choices=("P10", "P20"))
    parser.add_argument("--require-formal", action="store_true")
    parser.add_argument("--expected-repo-root", type=Path)
    parser.add_argument("--expected-repo-head")
    parser.add_argument("--expected-binary-sha256")
    parser.add_argument("--max-age-seconds", type=float)
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                validate_result(
                    args.result,
                    args.consumer,
                    args.require_formal,
                    expected_repo_root=args.expected_repo_root,
                    expected_repo_head=args.expected_repo_head,
                    expected_binary_sha256=args.expected_binary_sha256,
                    max_age_seconds=args.max_age_seconds,
                ),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (GateError, OSError) as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
