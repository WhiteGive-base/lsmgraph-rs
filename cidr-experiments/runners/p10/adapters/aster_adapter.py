#!/usr/bin/env python3
"""Fail-closed Aster/RocksGraph adapter for the CIDR P10 contract."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

P10_DIR = Path(__file__).resolve().parents[1]
ADAPTER_DIR = Path(__file__).resolve().parent
P31_DIR = P10_DIR.parent / "p31"
sys.path.insert(0, str(P10_DIR))
sys.path.insert(0, str(ADAPTER_DIR))
sys.path.insert(0, str(P31_DIR))

from build_aster_store_manifest import (  # noqa: E402
    KEY_MAP_NAME,
    SCHEMA_VERSION as STORE_MANIFEST_SCHEMA,
    STORE_HASH_METHOD,
    STORE_METADATA_NAME,
    TREE_HASH_METHOD,
    logical_store_sha256,
    stable_tree_manifest,
)
from p10_contract import (  # noqa: E402
    CLOCK_NAME,
    CONTRACT_VERSION,
    INTERFACE_SCOPE,
    OBSERVATION_COLUMNS,
    REQUEST_SCHEMA_VERSION,
    RESULT_SCHEMA_VERSION,
    SEQUENCE_DIGEST_ALGORITHM,
    TIMING_BOUNDARY,
    TRUTH_DIGEST_ALGORITHM,
    ContractError,
    atomic_json,
    boolean,
    integer,
    nonempty_string,
    phase_digest,
    read_json,
    read_truth,
    require_keys,
    sha256_file,
    validate_adapter_outputs,
)
from run_manifest import host_facts  # noqa: E402

PROVENANCE_SCHEMA = "p10-aster-adapter-provenance-v1"
WORKER_SCHEMA = "p10-aster-rocksgraph-worker-v1"
STORE_METADATA_SCHEMA = "p10-aster-rocksgraph-store-v1"
RAW_COLUMNS = [
    "phase",
    "pass_index",
    "query_index",
    "edge_type",
    "src",
    "expected_count",
    "actual_count",
    "expected_sum_hash",
    "actual_sum_hash",
    "expected_xor_hash",
    "actual_xor_hash",
    "status",
    "latency_ns",
]
SHA256_CHARS = frozenset("0123456789abcdef")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def exact_sha(value: object, context: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in SHA256_CHARS for char in value):
        raise ContractError(f"{context}: expected 64 lowercase hexadecimal characters")
    return value


def resolve_file(path: Path, expected_sha: str, context: str, *, executable: bool = False) -> dict[str, Any]:
    path = path.resolve()
    require(path.is_file(), f"{context}: missing file: {path}")
    if executable:
        require(os.access(path, os.X_OK), f"{context}: file is not executable: {path}")
    expected = exact_sha(expected_sha, f"{context} SHA-256")
    actual = sha256_file(path)
    require(actual == expected, f"{context}: SHA-256 mismatch")
    return {"path": str(path), "sha256": actual, "size_bytes": path.stat().st_size}


def artifact_ref(path: Path) -> dict[str, Any]:
    path = path.resolve()
    require(path.is_file(), f"artifact is missing: {path}")
    return {"path": str(path), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}


def same_path(value: object, expected: Path, context: str) -> None:
    require(isinstance(value, str), f"{context}: expected absolute path string")
    require(Path(value).resolve() == expected.resolve(), f"{context}: path mismatch")


def git_state(root: Path, context: str) -> dict[str, Any]:
    root = root.resolve()
    require(root.is_dir(), f"{context}: Git root does not exist: {root}")
    try:
        head = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True, stderr=subprocess.STDOUT, timeout=20
        ).strip()
        status = subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=normal"],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContractError(f"{context}: cannot audit Git state: {exc}") from exc
    require(len(head) == 40 and all(char in "0123456789abcdef" for char in head), f"{context}: invalid Git HEAD")
    return {
        "root": str(root),
        "head": head,
        "clean": status == "",
        "status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
    }


def command_digest(argv: list[str]) -> str:
    return hashlib.sha256(json.dumps(argv, separators=(",", ":")).encode("utf-8")).hexdigest()


def validate_request(args: argparse.Namespace) -> tuple[dict[str, Any], list[dict[str, int]], Path]:
    request = require_keys(
        read_json(args.request, "Aster adapter request"),
        required=(
            "schema_version",
            "contract_version",
            "suite_id",
            "run_id",
            "execution_mode",
            "system_id",
            "group",
            "system_version",
            "interface_scope",
            "repeat_index",
            "binary",
            "dataset",
            "store_roots",
            "truth",
            "timing",
        ),
        allowed=(
            "schema_version",
            "contract_version",
            "suite_id",
            "run_id",
            "execution_mode",
            "system_id",
            "group",
            "system_version",
            "interface_scope",
            "repeat_index",
            "binary",
            "dataset",
            "store_roots",
            "truth",
            "timing",
        ),
        context="Aster adapter request",
    )
    expected = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "execution_mode": args.mode,
        "system_id": "aster",
        "group": "embedded",
        "interface_scope": INTERFACE_SCOPE,
    }
    for key, value in expected.items():
        require(request[key] == value, f"request.{key}: {request[key]!r} != {value!r}")
    nonempty_string(request["suite_id"], "request.suite_id")
    nonempty_string(request["run_id"], "request.run_id")
    nonempty_string(request["system_version"], "request.system_version")
    integer(request["repeat_index"], "request.repeat_index", 1)

    binary_obj = require_keys(
        request["binary"], required=("path", "sha256"), allowed=("path", "sha256"), context="request.binary"
    )
    same_path(binary_obj["path"], args.binary, "request.binary.path")
    require(binary_obj["sha256"] == args.binary_sha256, "request.binary.sha256 differs from adapter argv")
    dataset_obj = require_keys(
        request["dataset"], required=("path", "sha256"), allowed=("path", "sha256"), context="request.dataset"
    )
    same_path(dataset_obj["path"], args.dataset, "request.dataset.path")
    require(dataset_obj["sha256"] == args.dataset_sha256, "request.dataset.sha256 differs from adapter argv")

    truth_obj = require_keys(
        request["truth"],
        required=("path", "sha256", "query_count", "digest_algorithm"),
        allowed=("path", "sha256", "query_count", "digest_algorithm"),
        context="request.truth",
    )
    truth_path = Path(nonempty_string(truth_obj["path"], "request.truth.path")).resolve()
    truth_ref = resolve_file(truth_path, exact_sha(truth_obj["sha256"], "request.truth.sha256"), "truth TSV")
    require(truth_obj["digest_algorithm"] == TRUTH_DIGEST_ALGORITHM, "request.truth.digest_algorithm mismatch")
    query_count = integer(truth_obj["query_count"], "request.truth.query_count", 1)
    truth_rows = read_truth(truth_path, query_count)

    stores = request["store_roots"]
    require(isinstance(stores, list) and stores, "request.store_roots must be a non-empty array")
    selected: Path | None = None
    labels: set[str] = set()
    for index, raw in enumerate(stores):
        context = f"request.store_roots[{index}]"
        item = require_keys(raw, required=("label", "path", "sha256"), allowed=("label", "path", "sha256"), context=context)
        label = nonempty_string(item["label"], f"{context}.label")
        require(label not in labels, f"{context}.label is duplicated")
        labels.add(label)
        path = Path(nonempty_string(item["path"], f"{context}.path")).resolve()
        require(path.is_dir(), f"{context}.path is not an existing directory: {path}")
        if args.mode == "formal" or args.lifecycle == "reopen":
            exact_sha(item["sha256"], f"{context}.sha256")
        elif item["sha256"]:
            exact_sha(item["sha256"], f"{context}.sha256")
        if label == args.store_label:
            selected = path
    require(selected is not None, f"request.store_roots has no label {args.store_label!r}")

    timing = require_keys(
        request["timing"],
        required=(
            "timing_boundary",
            "clock",
            "cache_policy",
            "process_reuse_between_phases",
            "warmup_passes",
            "measured_passes",
            "concurrency",
            "per_query_timeout_ms",
            "sequence_digest_algorithm",
        ),
        allowed=(
            "timing_boundary",
            "clock",
            "cache_policy",
            "process_reuse_between_phases",
            "warmup_passes",
            "measured_passes",
            "concurrency",
            "per_query_timeout_ms",
            "sequence_digest_algorithm",
        ),
        context="request.timing",
    )
    exact_timing = {
        "timing_boundary": TIMING_BOUNDARY,
        "clock": CLOCK_NAME,
        "cache_policy": "full-shared-trace-before-each-measured-repeat-v1",
        "process_reuse_between_phases": True,
        "concurrency": 1,
        "sequence_digest_algorithm": SEQUENCE_DIGEST_ALGORITHM,
    }
    for key, value in exact_timing.items():
        require(timing[key] == value, f"request.timing.{key}: {timing[key]!r} != {value!r}")
    boolean(timing["process_reuse_between_phases"], "request.timing.process_reuse_between_phases")
    integer(timing["warmup_passes"], "request.timing.warmup_passes", 1)
    integer(timing["measured_passes"], "request.timing.measured_passes", 1)
    integer(timing["concurrency"], "request.timing.concurrency", 1)
    integer(timing["per_query_timeout_ms"], "request.timing.per_query_timeout_ms", 1)
    request["truth"]["path"] = str(truth_path)
    request["truth"]["sha256"] = truth_ref["sha256"]
    return request, truth_rows, selected


def require_formal_argv(args: argparse.Namespace) -> None:
    if args.mode != "formal":
        return
    required = {
        "--store-manifest": args.store_manifest,
        "--store-manifest-sha256": args.store_manifest_sha256,
        "--p02b-result": args.p02b_result,
        "--p02b-result-sha256": args.p02b_result_sha256,
        "--p02b-validator": args.p02b_validator,
        "--p02b-validator-sha256": args.p02b_validator_sha256,
        "--p31-wrapper": args.p31_wrapper,
        "--p31-wrapper-sha256": args.p31_wrapper_sha256,
    }
    missing = [key for key, value in required.items() if value in (None, "")]
    require(not missing, f"formal Aster adapter is missing required argv: {missing}")
    require(args.lifecycle == "reopen", "formal Aster adapter requires lifecycle=reopen")


def validate_store(
    args: argparse.Namespace,
    request: dict[str, Any],
    store: Path,
    source: dict[str, Any],
    binary: dict[str, Any],
    dataset: dict[str, Any],
) -> dict[str, Any]:
    request_root = next(root for root in request["store_roots"] if root["label"] == args.store_label)
    if args.lifecycle == "fresh":
        require(args.mode == "fixture", "fresh Aster lifecycle is correctness-fixture only")
        require(not any(store.iterdir()), "fresh Aster lifecycle refuses a non-empty store")
        require(not request_root["sha256"], "fresh fixture store must not claim a frozen SHA-256")
        return {"path": str(store), "tree_sha256": "", "manifest": None, "pre_tree": None}

    require(args.store_manifest is not None and args.store_manifest_sha256, "reopen lifecycle requires a store manifest")
    manifest_ref = resolve_file(args.store_manifest, args.store_manifest_sha256, "Aster store manifest")
    manifest = require_keys(
        read_json(args.store_manifest, "Aster store manifest"),
        required=(
            "schema_version",
            "created_at_utc",
            "store_path",
            "store_sha256",
            "hash_method",
            "file_count",
            "total_bytes",
            "excluded_runtime_files",
            "files",
            "snapshot_tree_sha256",
            "snapshot_tree_hash_method",
            "source",
            "binary",
            "dataset",
            "key_map",
            "store_metadata",
        ),
        allowed=(
            "schema_version",
            "created_at_utc",
            "store_path",
            "store_sha256",
            "hash_method",
            "file_count",
            "total_bytes",
            "excluded_runtime_files",
            "files",
            "snapshot_tree_sha256",
            "snapshot_tree_hash_method",
            "source",
            "binary",
            "dataset",
            "key_map",
            "store_metadata",
        ),
        context="Aster store manifest",
    )
    require(manifest["schema_version"] == STORE_MANIFEST_SCHEMA, "Aster store manifest schema mismatch")
    same_path(manifest["store_path"], store, "Aster store manifest.store_path")
    store_sha = exact_sha(manifest["store_sha256"], "Aster store manifest.store_sha256")
    require(manifest["hash_method"] == STORE_HASH_METHOD, "Aster store manifest hash method mismatch")
    exact_sha(manifest["snapshot_tree_sha256"], "Aster store manifest.snapshot_tree_sha256")
    require(manifest["snapshot_tree_hash_method"] == TREE_HASH_METHOD, "Aster snapshot tree hash method mismatch")
    require(request_root["sha256"] == store_sha, "request store SHA-256 differs from Aster manifest")
    for name, expected in (("source", source), ("binary", binary), ("dataset", dataset)):
        observed = manifest[name]
        require(isinstance(observed, dict), f"Aster store manifest.{name} is not an object")
        if name == "source":
            same_path(observed.get("root"), Path(expected["root"]), "Aster store manifest.source.root")
            require(observed.get("head") == expected["head"], "Aster store manifest source commit mismatch")
        else:
            same_path(observed.get("path"), Path(expected["path"]), f"Aster store manifest.{name}.path")
            require(observed.get("sha256") == expected["sha256"], f"Aster store manifest {name} SHA-256 mismatch")
    key_map = manifest["key_map"]
    metadata = manifest["store_metadata"]
    require(isinstance(key_map, dict) and isinstance(metadata, dict), "Aster store manifest auxiliary refs are invalid")
    same_path(key_map.get("path"), store / KEY_MAP_NAME, "Aster store key_map.path")
    same_path(metadata.get("path"), store / STORE_METADATA_NAME, "Aster store metadata.path")
    key_map_ref = resolve_file(store / KEY_MAP_NAME, str(key_map.get("sha256", "")), "Aster key map")
    metadata_ref = resolve_file(store / STORE_METADATA_NAME, str(metadata.get("sha256", "")), "Aster store metadata")
    metadata_value = read_json(store / STORE_METADATA_NAME, "Aster store metadata")
    require(metadata_value.get("schema_version") == STORE_METADATA_SCHEMA, "Aster store metadata schema mismatch")
    require(metadata_value.get("source_commit") == source["head"], "Aster store metadata source commit mismatch")
    require(metadata_value.get("dataset_sha256") == dataset["sha256"], "Aster store metadata dataset SHA mismatch")
    require(metadata_value.get("builder_binary_sha256") == binary["sha256"], "Aster store metadata binary SHA mismatch")
    require(metadata_value.get("key_map") == KEY_MAP_NAME, "Aster store metadata key-map name mismatch")
    pre_tree = stable_tree_manifest(store)
    logical_sha = logical_store_sha256(Path(binary["path"]), store)
    require(logical_sha == store_sha, "current Aster logical query store differs from frozen SHA-256")
    return {
        "path": str(store),
        "store_sha256": store_sha,
        "hash_method": STORE_HASH_METHOD,
        "manifest": manifest_ref,
        "key_map": key_map_ref,
        "metadata": metadata_ref,
        "pre_tree": {key: pre_tree[key] for key in ("sha256", "file_count", "total_bytes", "hash_method", "excluded_runtime_files")},
    }


def validate_p02b(args: argparse.Namespace, formal: bool) -> dict[str, Any] | None:
    provided = any(
        value not in (None, "")
        for value in (args.p02b_result, args.p02b_result_sha256, args.p02b_validator, args.p02b_validator_sha256)
    )
    if not provided:
        require(not formal, "formal Aster adapter requires a P02B admission")
        return None
    require(
        all(value not in (None, "") for value in (args.p02b_result, args.p02b_result_sha256, args.p02b_validator, args.p02b_validator_sha256)),
        "P02B argv must be supplied as a complete result/validator SHA-bound set",
    )
    result = resolve_file(args.p02b_result, args.p02b_result_sha256, "P02B result")
    validator = resolve_file(args.p02b_validator, args.p02b_validator_sha256, "P02B validator", executable=True)
    command = [
        validator["path"],
        "--result",
        result["path"],
        "--consumer",
        "P10",
        "--max-age-seconds",
        str(args.p02b_max_age_seconds),
    ]
    if formal:
        command.append("--require-formal")
    completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=120)
    require(completed.returncode == 0, f"P02B admission failed: {completed.stderr.strip()}")
    try:
        admission = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError(f"P02B validator emitted invalid JSON: {exc}") from exc
    require(isinstance(admission, dict) and admission.get("state") == "PASS", "P02B validator returned non-PASS")
    require(admission.get("consumer") == "P10" and admission.get("scope") == "host-global", "P02B receipt scope mismatch")
    require(admission.get("formal_required") is formal, "P02B formal classification mismatch")
    require(admission.get("sentinel_result_sha256") == result["sha256"], "P02B receipt result SHA mismatch")
    host = admission.get("host")
    current_host = host_facts()
    require(
        isinstance(host, dict)
        and host.get("hostname") == platform.node()
        and host.get("fingerprint_sha256") == current_host["fingerprint_sha256"],
        "P02B receipt is from another host or host fingerprint has drifted",
    )
    marker = resolve_file(Path(str(admission.get("pass_marker", ""))), str(admission.get("pass_marker_sha256", "")), "P02B PASS marker")
    provenance = resolve_file(Path(str(admission.get("provenance", ""))), str(admission.get("provenance_sha256", "")), "P02B provenance")
    return {"result": result, "validator": validator, "pass_marker": marker, "provenance": provenance, "admission": admission}


def read_raw_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            require(reader.fieldnames == RAW_COLUMNS, f"Aster raw observation header mismatch: {reader.fieldnames!r}")
            return list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ContractError(f"cannot read Aster raw observations: {exc}") from exc


def atomic_text(path: Path, value: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def convert_outputs(
    output_dir: Path,
    request: dict[str, Any],
    truth_rows: list[dict[str, int]],
    args: argparse.Namespace,
    binary: dict[str, Any],
    source: dict[str, Any],
) -> None:
    summary = require_keys(
        read_json(output_dir / "aster-worker-summary.json", "Aster worker summary"),
        required=(
            "schema_version",
            "lifecycle",
            "query_count",
            "source_commit",
            "dataset_sha256",
            "binary_sha256",
            "key_count",
            "warmup",
            "measured",
        ),
        allowed=(
            "schema_version",
            "lifecycle",
            "query_count",
            "source_commit",
            "dataset_sha256",
            "binary_sha256",
            "key_count",
            "warmup",
            "measured",
        ),
        context="Aster worker summary",
    )
    require(summary["schema_version"] == WORKER_SCHEMA, "Aster worker schema mismatch")
    require(summary["lifecycle"] == args.lifecycle, "Aster worker lifecycle mismatch")
    require(summary["query_count"] == len(truth_rows), "Aster worker query count mismatch")
    require(summary["source_commit"] == source["head"], "Aster worker source commit mismatch")
    require(summary["dataset_sha256"] == request["dataset"]["sha256"], "Aster worker dataset SHA mismatch")
    require(summary["binary_sha256"] == binary["sha256"], "Aster worker binary SHA mismatch")
    integer(summary["key_count"], "Aster worker key_count", 1)

    raw_rows = read_raw_rows(output_dir / "aster-raw-observations.tsv")
    rows = [
        {
            "contract_version": CONTRACT_VERSION,
            "system_id": "aster",
            "group": "embedded",
            "repeat_index": str(request["repeat_index"]),
            **row,
        }
        for row in raw_rows
    ]
    temporary = output_dir / "query-observations.tsv.tmp"
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OBSERVATION_COLUMNS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, output_dir / "query-observations.tsv")

    events: list[dict[str, Any]] = []
    try:
        for line_number, line in enumerate((output_dir / "aster-raw-phase-events.jsonl").read_text(encoding="utf-8").splitlines(), 1):
            value = require_keys(
                json.loads(line),
                required=("phase", "event", "monotonic_ns"),
                allowed=("phase", "event", "monotonic_ns"),
                context=f"Aster raw event line {line_number}",
            )
            events.append({"contract_version": CONTRACT_VERSION, **value})
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read Aster raw events: {exc}") from exc
    atomic_text(output_dir / "phase-events.jsonl", "".join(json.dumps(event, sort_keys=True) + "\n" for event in events))

    phase_result: dict[str, Any] = {}
    phase_keys = (
        "passes",
        "requested_queries",
        "completed_queries",
        "timeout_queries",
        "mismatch_queries",
        "started_monotonic_ns",
        "ended_monotonic_ns",
        "elapsed_ns",
    )
    for phase, pass_key in (("warmup", "warmup_passes"), ("measured", "measured_passes")):
        raw_phase = require_keys(summary[phase], required=phase_keys, allowed=phase_keys, context=f"Aster worker summary.{phase}")
        passes = request["timing"][pass_key]
        require(raw_phase["passes"] == passes, f"Aster worker {phase} pass count mismatch")
        phase_rows = [row for row in rows if row["phase"] == phase]
        phase_result[phase] = {
            **raw_phase,
            "expected_digest_sha256": phase_digest(phase, passes, truth_rows),
            "actual_digest_sha256": phase_digest(phase, passes, truth_rows, phase_rows),
        }
    result = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "system_id": "aster",
        "group": "embedded",
        "system_version": request["system_version"],
        "interface_scope": INTERFACE_SCOPE,
        "repeat_index": request["repeat_index"],
        "truth_sha256": request["truth"]["sha256"],
        "sequence_digest_algorithm": SEQUENCE_DIGEST_ALGORITHM,
        "timing_boundary": TIMING_BOUNDARY,
        "clock": CLOCK_NAME,
        "concurrency": 1,
        "per_query_timeout_ms": request["timing"]["per_query_timeout_ms"],
        "warmup": phase_result["warmup"],
        "measured": phase_result["measured"],
    }
    atomic_json(output_dir / "adapter-result.json", result)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("fixture", "formal"))
    parser.add_argument("--lifecycle", required=True, choices=("fresh", "reopen"))
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--binary-sha256", required=True)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--dataset-sha256", required=True)
    parser.add_argument("--store-label", default="aster")
    parser.add_argument("--store-manifest", type=Path)
    parser.add_argument("--store-manifest-sha256")
    parser.add_argument("--p02b-result", type=Path)
    parser.add_argument("--p02b-result-sha256")
    parser.add_argument("--p02b-validator", type=Path)
    parser.add_argument("--p02b-validator-sha256")
    parser.add_argument("--p02b-max-age-seconds", type=int, default=3600)
    parser.add_argument("--p31-wrapper", type=Path)
    parser.add_argument("--p31-wrapper-sha256")
    parser.add_argument("--block-cache-bytes", type=int, default=256 * 1024 * 1024)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def run(args: argparse.Namespace) -> None:
    require(args.block_cache_bytes > 0, "--block-cache-bytes must be positive")
    require(args.p02b_max_age_seconds > 0, "--p02b-max-age-seconds must be positive")
    args.source_root = args.source_root.resolve()
    args.repo_root = args.repo_root.resolve()
    args.binary = args.binary.resolve()
    args.dataset = args.dataset.resolve()
    args.request = args.request.resolve()
    args.output_dir = args.output_dir.resolve()
    if args.store_manifest is not None:
        args.store_manifest = args.store_manifest.resolve()
    if args.p02b_result is not None:
        args.p02b_result = args.p02b_result.resolve()
    if args.p02b_validator is not None:
        args.p02b_validator = args.p02b_validator.resolve()
    if args.p31_wrapper is not None:
        args.p31_wrapper = args.p31_wrapper.resolve()
    require_formal_argv(args)
    request_ref = artifact_ref(args.request)
    request, truth_rows, store_path = validate_request(args)
    if args.mode == "formal":
        require(len(truth_rows) == 1700, "formal Aster adapter requires the frozen 1700-query truth")

    binary = resolve_file(args.binary, args.binary_sha256, "Aster RocksGraph worker", executable=True)
    dataset = resolve_file(args.dataset, args.dataset_sha256, "dense dataset")
    source = git_state(args.source_root, "Aster source")
    expected_source = args.source_commit.lower()
    require(len(expected_source) == 40 and all(char in "0123456789abcdef" for char in expected_source), "--source-commit must be a full lowercase Git SHA")
    require(source["head"] == expected_source, "Aster source HEAD differs from frozen --source-commit")
    repo = git_state(args.repo_root, "P10 harness repository")
    if args.mode == "formal":
        require(source["clean"], "formal Aster adapter requires a clean Aster source tree")
        require(repo["clean"], "formal Aster adapter requires a clean P10 harness worktree")
        require(request["system_version"] == f"aster-rocksgraph@{source['head']}", "formal request.system_version must freeze the exact Aster source commit")
        for path, context in (
            (args.binary, "binary"),
            (args.dataset, "dataset"),
            (Path(request["truth"]["path"]), "truth"),
            (store_path, "store"),
            (args.store_manifest, "store manifest"),
            (args.p02b_result, "P02B result"),
            (args.p02b_validator, "P02B validator"),
            (args.p31_wrapper, "P31 wrapper"),
        ):
            require(path is not None and not any("fixture" in part.lower() or part.lower() == "tests" for part in path.resolve().parts), f"formal Aster {context} rejects fixture/test path")

    p02b = validate_p02b(args, args.mode == "formal")
    p31 = (
        resolve_file(args.p31_wrapper, args.p31_wrapper_sha256, "P31 wrapper", executable=True)
        if args.p31_wrapper is not None
        else None
    )
    store = validate_store(args, request, store_path, source, binary, dataset)
    require(
        args.output_dir != store_path
        and store_path not in args.output_dir.parents
        and args.output_dir not in store_path.parents,
        "adapter output must not overlap the Aster store",
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    protected_names = (
        "adapter-result.json",
        "query-observations.tsv",
        "phase-events.jsonl",
        "adapter-provenance.json",
        "aster-raw-observations.tsv",
        "aster-raw-phase-events.jsonl",
        "aster-worker-summary.json",
    )
    for name in protected_names:
        require(not (args.output_dir / name).exists(), f"adapter refuses to overwrite {name}")

    command = [
        binary["path"],
        "--lifecycle",
        args.lifecycle,
        "--dataset",
        dataset["path"],
        "--truth",
        request["truth"]["path"],
        "--store",
        str(store_path),
        "--output-dir",
        str(args.output_dir),
        "--source-commit",
        source["head"],
        "--dataset-sha256",
        dataset["sha256"],
        "--binary-sha256",
        binary["sha256"],
        "--warmup-passes",
        str(request["timing"]["warmup_passes"]),
        "--measured-passes",
        str(request["timing"]["measured_passes"]),
        "--timeout-ms",
        str(request["timing"]["per_query_timeout_ms"]),
        "--block-cache-bytes",
        str(args.block_cache_bytes),
    ]
    with (args.output_dir / "aster.stdout.log").open("wb") as stdout_handle, (
        args.output_dir / "aster.stderr.log"
    ).open("wb") as stderr_handle:
        completed = subprocess.run(command, cwd=args.repo_root, stdout=stdout_handle, stderr=stderr_handle, check=False)
    require(completed.returncode == 0, f"Aster RocksGraph worker exited {completed.returncode}; see aster.stderr.log")
    convert_outputs(args.output_dir, request, truth_rows, args, binary, source)

    post_tree = stable_tree_manifest(store_path)
    post_store_sha = logical_store_sha256(Path(binary["path"]), store_path)
    if args.lifecycle == "reopen":
        require(post_store_sha == store["store_sha256"], "Aster logical query store changed during reopen lifecycle")
    else:
        store["store_sha256"] = post_store_sha
        store["hash_method"] = STORE_HASH_METHOD
    store["post_store_sha256"] = post_store_sha
    store["post_tree"] = {
        key: post_tree[key]
        for key in ("sha256", "file_count", "total_bytes", "hash_method", "excluded_runtime_files")
    }
    provenance = {
        "schema_version": PROVENANCE_SCHEMA,
        "mode": args.mode,
        "lifecycle": args.lifecycle,
        "request": request_ref,
        "repo": repo,
        "source": source,
        "worker_source": artifact_ref(ADAPTER_DIR / "aster_p10_worker.cc"),
        "binary": binary,
        "dataset": dataset,
        "store": store,
        "truth": artifact_ref(Path(request["truth"]["path"])),
        "p02b": p02b,
        "p31_wrapper": p31,
        "command": {
            "argv": command,
            "argv_sha256": command_digest(command),
            "invocations": 1,
            "exit_code": completed.returncode,
        },
        "raw_artifacts": {
            name: artifact_ref(args.output_dir / name)
            for name in ("aster-worker-summary.json", "aster-raw-observations.tsv", "aster-raw-phase-events.jsonl")
        },
    }
    atomic_json(args.output_dir / "adapter-provenance.json", provenance)
    system = {
        "id": "aster",
        "display_name": "Aster",
        "group": "embedded",
        "system_version": request["system_version"],
        "fixture_only": args.mode == "fixture",
    }
    validate_adapter_outputs(
        output_dir=args.output_dir,
        request=request,
        system=system,
        truth_rows=truth_rows,
        max_timeouts=0,
    )


def main() -> int:
    args = parse_args()
    try:
        run(args)
        return 0
    except (ContractError, OSError, UnicodeError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
