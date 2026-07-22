#!/usr/bin/env python3
"""Run the fail-closed P02B SF10 stability sentinel with P31 sidecars."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from calculate_cv import calculate_cv
from extract_run_metrics import extract_run_metrics, validate_query_plan
from p02b_common import (
    GateError,
    atomic_write_json,
    atomic_write_text,
    ensure_new_directory,
    expand_cpuset,
    file_ref,
    read_json,
    reject_unknown_keys,
    require_hex64,
    require_keys,
    resolved_existing_dir,
    resolved_existing_file,
    same_resolved_path,
    sha256_file,
)
from validate_clean_ready import validate_clean_ready


CONFIG_SCHEMA = "p02b-sf10-sentinel-config-v1"
RESULT_SCHEMA = "p02b-sf10-sentinel-result-v1"
CACHE_POLICY = "no-drop-caches;independent-process;in-process-warmup;os-cache-as-is"
CLEAN_PROTOCOL_V2 = "short-clean-window-v2"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def validate_config(path: Path) -> Dict[str, Any]:
    config = read_json(path)
    allowed = {
        "schema_version",
        "task_id",
        "scale",
        "fixture_mode",
        "independent_runs",
        "expected_queries",
        "warmup_runs",
        "measured_repeats",
        "minimum_measured_seconds_per_run",
        "correctness_timeout_seconds",
        "repeat_timeout_seconds",
        "semantic_degree_hint",
        "force_signature",
        "l0_layout",
        "io_backend",
        "cache_policy",
        "cpu",
        "p31",
        "thresholds",
        "clean_ready",
    }
    require_keys(config, allowed, "sentinel config")
    reject_unknown_keys(config, allowed, "sentinel config")
    if config.get("schema_version") != CONFIG_SCHEMA:
        raise GateError("sentinel config has wrong schema_version")
    if config.get("task_id") != "P02B-SF10-SENTINEL" or config.get("scale") != "sf10":
        raise GateError("sentinel config must freeze task_id=P02B-SF10-SENTINEL and scale=sf10")
    if not isinstance(config.get("fixture_mode"), bool):
        raise GateError("fixture_mode must be boolean")
    fixture = bool(config["fixture_mode"])

    integer_fields = (
        "independent_runs",
        "expected_queries",
        "warmup_runs",
        "measured_repeats",
        "correctness_timeout_seconds",
        "repeat_timeout_seconds",
    )
    for key in integer_fields:
        if isinstance(config.get(key), bool) or not isinstance(config.get(key), int):
            raise GateError("{} must be an integer".format(key))
    if config["independent_runs"] < 3:
        raise GateError("independent_runs must be at least 3")
    if config["expected_queries"] < 1 or config["warmup_runs"] < 0 or config["measured_repeats"] < 1:
        raise GateError("query/warmup/repeat counts are invalid")
    if config["correctness_timeout_seconds"] < 1 or config["repeat_timeout_seconds"] < 1:
        raise GateError("timeouts must be positive")
    minimum_seconds = config.get("minimum_measured_seconds_per_run")
    if isinstance(minimum_seconds, bool) or not isinstance(minimum_seconds, (int, float)):
        raise GateError("minimum_measured_seconds_per_run must be numeric")
    if float(minimum_seconds) <= 0:
        raise GateError("minimum_measured_seconds_per_run must be positive")
    for key in ("semantic_degree_hint", "force_signature"):
        if not isinstance(config.get(key), bool):
            raise GateError("{} must be boolean".format(key))
    if config.get("l0_layout") not in {
        "schema",
        "naive",
        "kv-lsm",
        "edge-type-only",
        "semantic",
        "semantic-budgeted",
        "oracle",
    }:
        raise GateError("l0_layout is not a supported frozen layout")
    if config.get("io_backend") not in {"blocking", "uring"}:
        raise GateError("io_backend must be blocking or uring")
    if config.get("cache_policy") != CACHE_POLICY:
        raise GateError("cache_policy must be exactly {!r}".format(CACHE_POLICY))

    cpu = config.get("cpu")
    if not isinstance(cpu, dict):
        raise GateError("cpu must be an object")
    reject_unknown_keys(cpu, {"housekeeping_cpuset", "formal_cpuset", "threads"}, "cpu")
    require_keys(cpu, {"housekeeping_cpuset", "formal_cpuset", "threads"}, "cpu")
    housekeeping = expand_cpuset(cpu["housekeeping_cpuset"], "housekeeping_cpuset")
    formal = expand_cpuset(cpu["formal_cpuset"], "formal_cpuset")
    if housekeeping & formal:
        raise GateError("housekeeping and formal cpusets overlap")
    if isinstance(cpu.get("threads"), bool) or not isinstance(cpu.get("threads"), int):
        raise GateError("cpu.threads must be an integer")
    if cpu["threads"] != len(formal):
        raise GateError("cpu.threads must equal the formal cpuset CPU count")

    p31 = config.get("p31")
    if not isinstance(p31, dict):
        raise GateError("p31 must be an object")
    p31_keys = {
        "device",
        "data_mount",
        "interval_seconds",
        "disk_interval_seconds",
        "min_samples",
        "require_aux_tools",
    }
    require_keys(p31, p31_keys, "p31")
    reject_unknown_keys(p31, p31_keys, "p31")
    if not isinstance(p31["device"], str) or not p31["device"]:
        raise GateError("p31.device must be non-empty")
    if not isinstance(p31["data_mount"], str) or not Path(p31["data_mount"]).is_absolute():
        raise GateError("p31.data_mount must be absolute")
    for key in ("interval_seconds", "disk_interval_seconds"):
        if isinstance(p31[key], bool) or not isinstance(p31[key], (int, float)) or float(p31[key]) <= 0:
            raise GateError("p31.{} must be positive".format(key))
    if float(p31["disk_interval_seconds"]) < float(p31["interval_seconds"]):
        raise GateError("p31 disk interval cannot be shorter than process interval")
    if isinstance(p31["min_samples"], bool) or not isinstance(p31["min_samples"], int):
        raise GateError("p31.min_samples must be an integer")
    if p31["min_samples"] < 2:
        raise GateError("p31.min_samples must be at least 2")
    if p31["require_aux_tools"] is not True:
        raise GateError("P02B requires P31 pidstat/iostat auxiliary tools")

    thresholds = config.get("thresholds")
    if not isinstance(thresholds, dict):
        raise GateError("thresholds must be an object")
    require_keys(thresholds, {"qps_cv_max", "p99_cv_max"}, "thresholds")
    reject_unknown_keys(thresholds, {"qps_cv_max", "p99_cv_max"}, "thresholds")
    qps_limit = float(thresholds["qps_cv_max"])
    p99_limit = float(thresholds["p99_cv_max"])
    if not 0 < qps_limit <= 0.03 or not 0 < p99_limit <= 0.05:
        raise GateError("CV thresholds may not exceed QPS=3% and P99=5%")

    clean = config.get("clean_ready")
    if not isinstance(clean, dict):
        raise GateError("clean_ready must be an object")
    clean_protocol = clean.get("protocol_version")
    if clean_protocol is None:
        clean_keys = {"max_age_seconds", "minimum_consecutive_samples"}
    elif clean_protocol == CLEAN_PROTOCOL_V2:
        clean_keys = {
            "protocol_version",
            "max_age_seconds",
            "minimum_consecutive_samples",
            "sample_interval_seconds",
        }
    else:
        raise GateError("clean_ready.protocol_version is unsupported")
    require_keys(clean, clean_keys, "clean_ready")
    reject_unknown_keys(clean, clean_keys, "clean_ready")
    if isinstance(clean["max_age_seconds"], bool) or not isinstance(clean["max_age_seconds"], (int, float)):
        raise GateError("clean_ready.max_age_seconds must be numeric")
    if isinstance(clean["minimum_consecutive_samples"], bool) or not isinstance(
        clean["minimum_consecutive_samples"], int
    ):
        raise GateError("clean_ready.minimum_consecutive_samples must be an integer")
    if clean["max_age_seconds"] <= 0 or clean["minimum_consecutive_samples"] < 1:
        raise GateError("clean_ready limits are invalid")
    if clean_protocol == CLEAN_PROTOCOL_V2:
        interval = clean.get("sample_interval_seconds")
        if isinstance(interval, bool) or not isinstance(interval, int):
            raise GateError("clean_ready.sample_interval_seconds must be an integer")
        if clean["minimum_consecutive_samples"] != 5 or interval != 60:
            raise GateError("short-clean-window-v2 is frozen at exactly 5 x 60 seconds")
        if clean["max_age_seconds"] > 300:
            raise GateError("short-clean-window-v2 READY age may not exceed 300 seconds")

    if not fixture:
        if config["independent_runs"] != 3 or config["expected_queries"] != 1700:
            raise GateError("formal P02B must use exactly 3 runs and 1700 queries")
        if clean_protocol is None and (
            clean["minimum_consecutive_samples"] < 10 or clean["max_age_seconds"] > 300
        ):
            raise GateError("formal legacy P02B requires >=10 clean samples and READY age <=300s")
    return config


def validate_lineage_manifest(
    path: Path,
    schema: str,
    root_key: str,
    digest_key: str,
    expected_root: Optional[Path] = None,
) -> Dict[str, Any]:
    value = read_json(path)
    required = {"schema_version", root_key, digest_key, "hash_method"}
    require_keys(value, required, "lineage manifest {}".format(path))
    if value.get("schema_version") != schema:
        raise GateError("{} has wrong schema_version".format(path))
    root_raw = value.get(root_key)
    if not isinstance(root_raw, str) or not Path(root_raw).is_absolute():
        raise GateError("{} {} must be an absolute path".format(path, root_key))
    root = Path(root_raw).resolve()
    if not root.is_dir():
        raise GateError("{} references missing directory {}".format(path, root))
    if expected_root is not None and root != expected_root.resolve():
        raise GateError("{} does not describe the selected directory".format(path))
    digest = require_hex64(value.get(digest_key), "{} {}".format(path, digest_key))
    if not isinstance(value.get("hash_method"), str) or not value["hash_method"]:
        raise GateError("{} hash_method must be non-empty".format(path))
    return {
        "manifest": file_ref(path),
        "root": str(root),
        "content_sha256": digest,
        "hash_method": value["hash_method"],
    }


def validate_truth_tsv(path: Path, expected_queries: int) -> Dict[str, Any]:
    expected_header = ["query_index", "edge_type", "src", "count", "sum_hash", "xor_hash"]
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle, delimiter="\t")
            header = next(reader, None)
            if header != expected_header:
                raise GateError("truth TSV has the wrong header")
            count = 0
            for row in reader:
                if len(row) != len(expected_header):
                    raise GateError("truth TSV row {} has wrong column count".format(count + 2))
                if int(row[0]) != count:
                    raise GateError("truth TSV query_index is not contiguous")
                int(row[1])
                for token in row[2:]:
                    if int(token) < 0:
                        raise GateError("truth TSV digest/id/count values must be non-negative")
                count += 1
    except (OSError, ValueError) as exc:
        if isinstance(exc, GateError):
            raise
        raise GateError("cannot validate truth TSV {}: {}".format(path, exc)) from exc
    if count != expected_queries:
        raise GateError("truth TSV has {} rows, expected {}".format(count, expected_queries))
    result = file_ref(path)
    result["queries"] = count
    return result


def validate_id_map(path: Path) -> Dict[str, Any]:
    manifest_path = path / "id-map-manifest.json"
    marker_path = path / "FORMAL-PASS"
    checksum_path = path / "SHA256SUMS"
    manifest = read_json(manifest_path)
    if manifest.get("format") != "seml0-shared-id-map" or manifest.get("format_version") != 1:
        raise GateError("ID map manifest has wrong format")
    if manifest.get("status") != "PASS" or manifest.get("formal_pass") is not True:
        raise GateError("ID map manifest is not formal PASS")
    if manifest.get("verification_complete") is not True:
        raise GateError("ID map verification is incomplete")
    if manifest.get("mapping_hash_algorithm") != "fnv1a64-le-dense-original-v1":
        raise GateError("ID map uses an unsupported mapping hash algorithm")
    if not isinstance(manifest.get("mapping_hash"), str) or not re.fullmatch(
        r"[0-9a-f]{16}", manifest["mapping_hash"]
    ):
        raise GateError("ID map mapping_hash must be 16 lowercase hex digits")
    require_hex64(
        manifest.get("dense_to_original", {}).get("sha256"), "ID map dense_to_original SHA"
    )
    require_hex64(
        manifest.get("original_to_dense", {}).get("sha256"), "ID map original_to_dense SHA"
    )
    if not marker_path.is_file() or not checksum_path.is_file():
        raise GateError("ID map is missing FORMAL-PASS or SHA256SUMS")
    marker_text = marker_path.read_text(encoding="utf-8").strip().split()
    if len(marker_text) != 3 or marker_text[0] != "id-map-manifest.json" or marker_text[1] != "sha256":
        raise GateError("ID map FORMAL-PASS marker has invalid format")
    if marker_text[2] != sha256_file(manifest_path):
        raise GateError("ID map FORMAL-PASS marker does not bind the manifest")
    return {
        "path": str(path.resolve()),
        "manifest": file_ref(manifest_path),
        "formal_pass": file_ref(marker_path),
        "checksums": file_ref(checksum_path),
        "mapping_hash": manifest.get("mapping_hash"),
        "mapping_hash_algorithm": manifest.get("mapping_hash_algorithm"),
        "dense_to_original_sha256": manifest["dense_to_original"]["sha256"],
        "original_to_dense_sha256": manifest["original_to_dense"]["sha256"],
    }


def git_facts(repo: Path) -> Dict[str, Any]:
    def output(args: Sequence[str]) -> str:
        result = subprocess.run(
            list(args), cwd=str(repo), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        if result.returncode != 0:
            raise GateError("git command failed: {}".format(" ".join(args)))
        return result.stdout.strip()

    head = output(["git", "rev-parse", "HEAD"])
    status = output(["git", "status", "--porcelain=v1", "--untracked-files=normal"])
    return {"root": str(repo), "head": head, "dirty": bool(status), "status_lines": status.splitlines()}


def run_command(command: Sequence[str], stdout_path: Path, stderr_path: Path, timeout: int) -> None:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
        process = subprocess.Popen(
            list(command),
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
        )
        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise GateError("command timed out after {}s: {}".format(timeout, command[0])) from exc
    if return_code != 0:
        raise GateError(
            "command exited {}: {}; see {} and {}".format(
                return_code, command[0], stdout_path, stderr_path
            )
        )


def validate_truth_result(
    path: Path,
    expected_queries: int,
    store: Path,
    truth: Path,
    id_map: Path,
    generated_plan: Path,
    expected_plan_sha256: str,
    expected_mapping_hash: str,
) -> Dict[str, Any]:
    result = read_json(path)
    if result.get("consumer") != "seml0-shared-truth-v1":
        raise GateError("shared-truth verification has wrong consumer")
    if result.get("correctness_only") is not True or result.get("performance_eligible") is not False:
        raise GateError("shared-truth verification has unsafe classification")
    if result.get("truth_rows") != expected_queries:
        raise GateError("shared-truth verification has wrong query count")
    for raw, expected, name in (
        (result.get("data_dir"), store, "store"),
        (result.get("truth_tsv"), truth, "truth"),
        (result.get("id_map_dir"), id_map, "id map"),
        (result.get("sample_plan_out"), generated_plan, "generated query plan"),
    ):
        if not same_resolved_path(raw, expected):
            raise GateError("shared-truth verification reported wrong {} path".format(name))
    verification = result.get("verification")
    if not isinstance(verification, dict):
        raise GateError("shared-truth verification payload is missing")
    if verification.get("status") != "PASS":
        raise GateError("shared-truth verification is not PASS")
    if verification.get("checked") != expected_queries or verification.get("mismatches") != 0:
        raise GateError("shared-truth verification count/digest gate failed")
    if verification.get("sample_mismatches") not in ([], None):
        raise GateError("shared-truth verification retained mismatch details")
    if verification.get("mapping_hash") != expected_mapping_hash:
        raise GateError("shared-truth verification used a different ID mapping")
    generated_sha = sha256_file(generated_plan)
    if generated_sha != expected_plan_sha256:
        raise GateError("regenerated shared-truth plan differs from frozen query plan")
    return {
        "state": "PASS",
        "checked": expected_queries,
        "mismatches": 0,
        "total_neighbors": verification.get("total_neighbors"),
        "mapping_hash": verification.get("mapping_hash"),
        "result": file_ref(path),
        "regenerated_query_plan": file_ref(generated_plan),
    }


def validate_p31_run(
    run_dir: Path,
    expected_task_id: str,
    expected_repo_head: str,
    expected_store: Path,
    expected_inputs: Dict[str, Tuple[Path, str]],
    fixture_mode: bool,
) -> Dict[str, Any]:
    manifest_path = run_dir / "run-manifest.json"
    done_path = run_dir / "DONE"
    manifest = read_json(manifest_path)
    done = read_json(done_path)
    if manifest.get("state") != "PASS" or manifest.get("task_id") != expected_task_id:
        raise GateError("P31 manifest did not finish PASS for {}".format(expected_task_id))
    if manifest.get("performance_eligible_declared") is not False:
        raise GateError("P02B sentinel P31 run must remain performance_eligible=false")
    repo = manifest.get("repo", {})
    if repo.get("git_sha") != expected_repo_head:
        raise GateError("P31 repo HEAD differs from frozen sentinel HEAD")
    if not fixture_mode and repo.get("dirty") is not False:
        raise GateError("formal P02B P31 manifest reports a dirty repository")
    collector = manifest.get("collector", {})
    if collector.get("require_aux_tools") is not True:
        raise GateError("P31 run did not require pidstat/iostat")
    validation = manifest.get("validation", {})
    if validation.get("state") != "PASS" or validation.get("errors"):
        raise GateError("P31 validation is not clean PASS")
    roots = manifest.get("disk_roots", [])
    matching_store = [
        root
        for root in roots
        if isinstance(root, dict)
        and root.get("role") == "store"
        and Path(str(root.get("path", ""))).resolve() == expected_store.resolve()
    ]
    if len(matching_store) != 1:
        raise GateError("P31 manifest does not bind exactly one selected store")
    inputs = manifest.get("inputs", {})
    for name, (path, digest) in expected_inputs.items():
        reference = inputs.get(name, {})
        if not same_resolved_path(reference.get("path"), path) or reference.get("sha256") != digest:
            raise GateError("P31 {} provenance differs from frozen input".format(name))
    if done.get("state") != "PASS":
        raise GateError("P31 DONE marker is not PASS")
    if done.get("manifest_sha256") != sha256_file(manifest_path):
        raise GateError("P31 DONE marker does not bind run-manifest.json")
    validation_path = run_dir / "validation.json"
    if done.get("validation_sha256") != sha256_file(validation_path):
        raise GateError("P31 DONE marker does not bind validation.json")
    return {
        "run_dir": str(run_dir.resolve()),
        "manifest": file_ref(manifest_path),
        "validation": file_ref(validation_path),
        "done": file_ref(done_path),
    }


def assert_inputs_unchanged(provenance: Dict[str, Any]) -> None:
    for name, reference in provenance["files"].items():
        path = Path(reference["path"])
        if sha256_file(path) != reference["sha256"]:
            raise GateError("frozen input changed during sentinel: {}".format(name))


def canonical_batch_gate(repo: Path, selected: Optional[Path]) -> Path:
    expected = (repo / "cidr-experiments/runners/batch_gate_v2.py").resolve()
    gate = resolved_existing_file(selected or expected, "batch gate v2")
    if gate != expected:
        raise GateError("batch gate tool must be the canonical repository copy: {}".format(expected))
    return gate


def validate_v2_clean_ready(
    ready: Path,
    config: Dict[str, Any],
    repo: Path,
    git: Dict[str, Any],
    gate: Path,
    output: Path,
) -> Dict[str, Any]:
    command = [
        sys.executable,
        str(gate),
        "validate-p03",
        "--ready",
        str(ready),
        "--expected-repo-head",
        str(git["head"]),
        "--expected-hostname",
        socket.gethostname(),
        "--max-age-seconds",
        str(config["clean_ready"]["max_age_seconds"]),
        "--output",
        str(output),
    ]
    completed = subprocess.run(
        command,
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise GateError(
            "batch gate rejected short P03 READY: {}".format(completed.stderr.strip())
        )
    value = read_json(output)
    required_samples = value.get("required_consecutive_samples")
    if (
        value.get("schema_version") != "p02b-clean-ready-binding-v2"
        or value.get("state") != "PASS"
        or value.get("protocol_version") != CLEAN_PROTOCOL_V2
        or isinstance(required_samples, bool)
        or not isinstance(required_samples, int)
        or required_samples < config["clean_ready"]["minimum_consecutive_samples"]
        or value.get("git_head") != git["head"]
        or value.get("host") != socket.gethostname()
    ):
        raise GateError("short P03 binding identity/protocol drift")
    timing = value.get("timing")
    if (
        not isinstance(timing, dict)
        or timing.get("expected_interval_seconds")
        != config["clean_ready"]["sample_interval_seconds"]
        or timing.get("gap_check_pass") is not True
    ):
        raise GateError("short P03 binding lacks frozen gap evidence")
    return value


def assert_current_formal_identity(
    repo: Path,
    binary: Path,
    expected_git: Dict[str, Any],
    expected_binary: Dict[str, Any],
    expected_hostname: str,
) -> None:
    current_git = git_facts(repo)
    if (
        current_git.get("root") != expected_git.get("root")
        or current_git.get("head") != expected_git.get("head")
        or current_git.get("dirty") is not False
    ):
        raise GateError("repository HEAD/clean identity changed before correctness")
    if socket.gethostname() != expected_hostname:
        raise GateError("host identity changed before correctness")
    if file_ref(binary) != expected_binary:
        raise GateError("benchmark binary identity changed before correctness")


def run(args: argparse.Namespace) -> Dict[str, Any]:
    started_at = utc_now()
    run_dir = ensure_new_directory(args.run_dir)
    repo = resolved_existing_dir(args.repo_root, "repo root")
    binary = resolved_existing_file(args.binary, "benchmark binary", executable=True)
    store = resolved_existing_dir(args.store, "SF10 store")
    truth = resolved_existing_file(args.truth, "shared truth TSV")
    query_plan = resolved_existing_file(args.query_plan, "shared query plan")
    id_map = resolved_existing_dir(args.id_map_dir, "ID map directory")
    config_path = resolved_existing_file(args.config, "sentinel config")
    dataset_manifest_path = resolved_existing_file(args.dataset_manifest, "dataset manifest")
    store_manifest_path = resolved_existing_file(args.store_manifest, "store manifest")
    p31_wrapper = resolved_existing_file(
        args.p31_wrapper or repo / "cidr-experiments/runners/p31/run_with_resources.sh",
        "P31 wrapper",
        executable=True,
    )
    config = validate_config(config_path)
    fixture_mode = bool(config["fixture_mode"])
    clean_protocol = config["clean_ready"].get("protocol_version")
    batch_gate = None  # type: Optional[Path]
    if clean_protocol == CLEAN_PROTOCOL_V2:
        batch_gate = canonical_batch_gate(repo, args.batch_gate_tool)
        if fixture_mode:
            if args.batch_lease_output is not None:
                raise GateError("fixture P02B may not issue a formal batch lease")
        else:
            if args.batch_lease_output is None or not args.batch_lease_output.is_absolute():
                raise GateError("formal short-clean-window-v2 requires absolute --batch-lease-output")
    elif args.batch_lease_output is not None or args.batch_gate_tool is not None:
        raise GateError("batch gate/lease options require short-clean-window-v2 config")
    if not fixture_mode:
        allowed_root = (repo / "cidr-experiments/runs/P02B-SF10-SENTINEL/raw").resolve()
        if allowed_root not in run_dir.parents:
            raise GateError("formal P02B run_dir must be below {}".format(allowed_root))

    git = git_facts(repo)
    if not fixture_mode and git["dirty"]:
        raise GateError("formal P02B requires a clean Git worktree")
    clean_binding_path = run_dir / "clean-ready-binding.json"
    if clean_protocol == CLEAN_PROTOCOL_V2:
        if batch_gate is None:
            raise GateError("short-clean-window-v2 gate resolution failed")
        clean_binding = validate_v2_clean_ready(
            args.clean_ready,
            config,
            repo,
            git,
            batch_gate,
            clean_binding_path,
        )
    else:
        clean_binding = validate_clean_ready(
            args.clean_ready,
            float(config["clean_ready"]["max_age_seconds"]),
            int(config["clean_ready"]["minimum_consecutive_samples"]),
        )
        atomic_write_json(clean_binding_path, clean_binding)

    dataset_lineage = validate_lineage_manifest(
        dataset_manifest_path,
        "p02b-dataset-manifest-v1",
        "dataset_root",
        "dataset_sha256",
    )
    store_lineage = validate_lineage_manifest(
        store_manifest_path,
        "p02b-store-manifest-v1",
        "store_path",
        "store_sha256",
        expected_root=store,
    )
    truth_ref = validate_truth_tsv(truth, int(config["expected_queries"]))
    plan_summary = validate_query_plan(query_plan, int(config["expected_queries"]))
    if plan_summary.get("semantic_degree_hint") != config["semantic_degree_hint"]:
        raise GateError("query-plan semantic_degree_hint differs from config")
    if plan_summary.get("force_signature") != config["force_signature"]:
        raise GateError("query-plan force_signature differs from config")
    id_map_ref = validate_id_map(id_map)

    provenance = {
        "schema_version": "p02b-sentinel-provenance-v1",
        "created_at_utc": started_at,
        "repo": git,
        "fixture_mode": fixture_mode,
        "files": {
            "binary": file_ref(binary),
            "truth": truth_ref,
            "query_plan": file_ref(query_plan),
            "config": file_ref(config_path),
            "dataset_manifest": file_ref(dataset_manifest_path),
            "store_manifest": file_ref(store_manifest_path),
            "id_map_manifest": id_map_ref["manifest"],
            "p31_wrapper": file_ref(p31_wrapper),
        },
        "dataset": dataset_lineage,
        "store": store_lineage,
        "id_map": id_map_ref,
        "query_plan_summary": plan_summary,
        "clean_ready_binding_sha256": sha256_file(run_dir / "clean-ready-binding.json"),
    }
    atomic_write_json(run_dir / "provenance.json", provenance)

    if not fixture_mode:
        assert_current_formal_identity(
            repo,
            binary,
            git,
            provenance["files"]["binary"],
            socket.gethostname(),
        )

    generated_plan = run_dir / "regenerated-query-plan.json"
    truth_result_path = run_dir / "shared-truth-result.json"
    correctness_command: List[str] = [
        "taskset",
        "-c",
        config["cpu"]["formal_cpuset"],
        "env",
        "RAYON_NUM_THREADS={}".format(config["cpu"]["threads"]),
        str(binary),
        "--io-backend",
        config["io_backend"],
        "shared-truth-verify",
        "--data-dir",
        str(store),
        "--truth-tsv",
        str(truth),
        "--id-map-dir",
        str(id_map),
        "--expected-queries",
        str(config["expected_queries"]),
        "--l0-layout",
        config["l0_layout"],
        "--sample-plan-out",
        str(generated_plan),
        "--output",
        str(truth_result_path),
    ]
    if config["semantic_degree_hint"]:
        correctness_command.append("--semantic-degree-hint")
    if config["force_signature"]:
        correctness_command.append("--force-signature")
    atomic_write_text(
        run_dir / "correctness-command.json",
        json.dumps(correctness_command, ensure_ascii=False, indent=2) + "\n",
    )
    run_command(
        correctness_command,
        run_dir / "correctness.stdout.log",
        run_dir / "correctness.stderr.log",
        int(config["correctness_timeout_seconds"]),
    )
    correctness = validate_truth_result(
        truth_result_path,
        int(config["expected_queries"]),
        store,
        truth,
        id_map,
        generated_plan,
        provenance["files"]["query_plan"]["sha256"],
        id_map_ref["mapping_hash"],
    )

    repeat_records: List[Dict[str, Any]] = []
    metric_paths: List[Path] = []
    p31_expected_inputs = {
        "binary": (binary, provenance["files"]["binary"]["sha256"]),
        "dataset": (
            dataset_manifest_path,
            provenance["files"]["dataset_manifest"]["sha256"],
        ),
        "truth": (truth, provenance["files"]["truth"]["sha256"]),
        "query_or_trace": (query_plan, provenance["files"]["query_plan"]["sha256"]),
        "config": (config_path, provenance["files"]["config"]["sha256"]),
    }
    for run_index in range(1, int(config["independent_runs"]) + 1):
        run_id = "{}-r{}".format(run_dir.name, run_index)
        p31_dir = run_dir / "repeats" / "r{}".format(run_index) / "p31"
        benchmark_command: List[str] = [
            "taskset",
            "-c",
            config["cpu"]["formal_cpuset"],
            "env",
            "RAYON_NUM_THREADS={}".format(config["cpu"]["threads"]),
            "P02B_SENTINEL_RUN_INDEX={}".format(run_index),
            str(binary),
            "--io-backend",
            config["io_backend"],
            "storage-bench",
            "--data-dir",
            str(store),
            "--sample-plan-in",
            str(query_plan),
            "--warmup-runs",
            str(config["warmup_runs"]),
            "--repeats",
            str(config["measured_repeats"]),
            "--l0-layout",
            config["l0_layout"],
        ]
        if config["semantic_degree_hint"]:
            benchmark_command.append("--semantic-degree-hint")
        if config["force_signature"]:
            benchmark_command.append("--force-signature")

        p31_command: List[str] = [
            "taskset",
            "-c",
            config["cpu"]["housekeeping_cpuset"],
            str(p31_wrapper),
            "--run-dir",
            str(p31_dir),
            "--run-id",
            run_id,
            "--task-id",
            "P02B-SF10-SENTINEL-r{}".format(run_index),
            "--performance-eligible",
            "false",
            "--repo-root",
            str(repo),
            "--device",
            config["p31"]["device"],
            "--data-mount",
            config["p31"]["data_mount"],
            "--interval",
            str(config["p31"]["interval_seconds"]),
            "--disk-interval",
            str(config["p31"]["disk_interval_seconds"]),
            "--min-samples",
            str(config["p31"]["min_samples"]),
            "--store",
            "seml0={}".format(store),
            "--binary",
            str(binary),
            "--binary-sha256",
            provenance["files"]["binary"]["sha256"],
            "--dataset",
            str(dataset_manifest_path),
            "--dataset-sha256",
            provenance["files"]["dataset_manifest"]["sha256"],
            "--truth",
            str(truth),
            "--truth-sha256",
            provenance["files"]["truth"]["sha256"],
            "--query-or-trace",
            str(query_plan),
            "--query-or-trace-sha256",
            provenance["files"]["query_plan"]["sha256"],
            "--config",
            str(config_path),
            "--config-sha256",
            provenance["files"]["config"]["sha256"],
            "--",
        ] + benchmark_command
        wrapper_stdout = run_dir / "repeats" / "r{}".format(run_index) / "p31-wrapper.stdout.log"
        wrapper_stderr = run_dir / "repeats" / "r{}".format(run_index) / "p31-wrapper.stderr.log"
        run_command(
            p31_command,
            wrapper_stdout,
            wrapper_stderr,
            int(config["repeat_timeout_seconds"]),
        )
        p31_ref = validate_p31_run(
            p31_dir,
            "P02B-SF10-SENTINEL-r{}".format(run_index),
            git["head"],
            store,
            p31_expected_inputs,
            fixture_mode,
        )
        metric_path = run_dir / "repeats" / "r{}".format(run_index) / "run-metrics.json"
        metrics = extract_run_metrics(
            p31_dir / "command.stdout.log",
            query_plan,
            run_index,
            run_id,
            int(config["expected_queries"]),
            int(config["warmup_runs"]),
            int(config["measured_repeats"]),
            float(config["minimum_measured_seconds_per_run"]),
        )
        atomic_write_json(metric_path, metrics)
        metric_paths.append(metric_path)
        repeat_records.append({"run_index": run_index, "run_id": run_id, "metrics": file_ref(metric_path), "p31": p31_ref})

    cv = calculate_cv(
        metric_paths,
        int(config["independent_runs"]),
        float(config["thresholds"]["qps_cv_max"]),
        float(config["thresholds"]["p99_cv_max"]),
    )
    atomic_write_json(run_dir / "cv-result.json", cv)
    if cv["state"] != "PASS":
        raise GateError(
            "sentinel stability HOLD: qps_cv={:.6f}, p99_cv={:.6f}".format(
                cv["qps"]["cv"], cv["p99_us"]["cv"]
            )
        )
    assert_inputs_unchanged(provenance)

    result = {
        "schema_version": RESULT_SCHEMA,
        "state": "PASS",
        "fixture_only": fixture_mode,
        "performance_eligible": False,
        "formal_gate_eligible": not fixture_mode,
        "downstream_release_eligible": not fixture_mode,
        "consumers": ["P10", "P20"],
        "task_id": config["task_id"],
        "scale": config["scale"],
        "run_id": run_dir.name,
        "started_at_utc": started_at,
        "completed_at_utc": utc_now(),
        "clean_ready": clean_binding,
        "protocol": {
            "independent_process_runs": config["independent_runs"],
            "expected_queries": config["expected_queries"],
            "warmup_runs": config["warmup_runs"],
            "measured_repeats": config["measured_repeats"],
            "minimum_measured_seconds_per_run": config["minimum_measured_seconds_per_run"],
            "cache_policy": config["cache_policy"],
            "cpu": config["cpu"],
            "io_backend": config["io_backend"],
            "l0_layout": config["l0_layout"],
            "semantic_degree_hint": config["semantic_degree_hint"],
            "force_signature": config["force_signature"],
            "p31": config["p31"],
        },
        "provenance": {
            "path": str((run_dir / "provenance.json").resolve()),
            "sha256": sha256_file(run_dir / "provenance.json"),
            "repo_head": git["head"],
            "binary_sha256": provenance["files"]["binary"]["sha256"],
            "truth_sha256": provenance["files"]["truth"]["sha256"],
            "query_plan_sha256": provenance["files"]["query_plan"]["sha256"],
            "store_sha256": store_lineage["content_sha256"],
            "dataset_sha256": dataset_lineage["content_sha256"],
            "config_sha256": provenance["files"]["config"]["sha256"],
        },
        "correctness": correctness,
        "stability": cv,
        "repeats": repeat_records,
    }
    result_path = run_dir / "sentinel-result.json"
    atomic_write_json(result_path, result)
    marker_name = "FIXTURE-PASS" if fixture_mode else "PASS"
    atomic_write_json(
        run_dir / marker_name,
        {
            "state": "PASS",
            "fixture_only": fixture_mode,
            "result": str(result_path.resolve()),
            "result_sha256": sha256_file(result_path),
        },
    )
    return result


def issue_batch_lease(args: argparse.Namespace, result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    clean = result.get("clean_ready")
    if not isinstance(clean, dict) or clean.get("protocol_version") != CLEAN_PROTOCOL_V2:
        return None
    if result.get("fixture_only") is True:
        return None
    if args.batch_lease_output is None:
        raise GateError("short-clean-window-v2 formal PASS omitted --batch-lease-output")
    repo = resolved_existing_dir(args.repo_root, "repo root")
    gate = canonical_batch_gate(repo, args.batch_gate_tool)
    validator = resolved_existing_file(
        repo / "cidr-experiments/runners/p02b/validate_sentinel_result.py",
        "canonical P02B validator",
    )
    result_path = resolved_existing_file(args.run_dir / "sentinel-result.json", "P02B result")
    command = [
        sys.executable,
        str(gate),
        "issue-lease",
        "--p02b-result",
        str(result_path),
        "--p02b-validator",
        str(validator),
        "--repo-root",
        str(repo),
        "--binary",
        str(resolved_existing_file(args.binary, "benchmark binary", executable=True)),
        "--output",
        str(args.batch_lease_output),
    ]
    completed = subprocess.run(
        command,
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise GateError("batch lease issuance failed: {}".format(completed.stderr.strip()))
    lease_path = resolved_existing_file(args.batch_lease_output, "issued batch lease")
    lease_marker = resolved_existing_file(
        lease_path.with_name(lease_path.name + ".PASS.json"), "batch lease marker"
    )
    try:
        lease = json.loads(completed.stdout)
    except ValueError as exc:
        raise GateError("batch gate emitted invalid lease JSON") from exc
    if not isinstance(lease, dict) or lease.get("state") != "PASS":
        raise GateError("batch gate did not emit a PASS lease")
    receipt = {
        "schema_version": "p02b-batch-lease-issuance-v2",
        "state": "PASS",
        "issued_at_utc": utc_now(),
        "lease": file_ref(lease_path),
        "lease_marker": file_ref(lease_marker),
    }
    atomic_write_json(args.run_dir / "BATCH-LEASE-ISSUED.json", receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--clean-ready", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--dataset-manifest", required=True, type=Path)
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--store-manifest", required=True, type=Path)
    parser.add_argument("--truth", required=True, type=Path)
    parser.add_argument("--query-plan", required=True, type=Path)
    parser.add_argument("--id-map-dir", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--p31-wrapper", type=Path)
    parser.add_argument("--batch-gate-tool", type=Path)
    parser.add_argument("--batch-lease-output", type=Path)
    args = parser.parse_args()
    try:
        result = run(args)
    except (GateError, OSError, ValueError, subprocess.SubprocessError) as exc:
        try:
            if args.run_dir.exists() and args.run_dir.is_dir():
                for stale in (args.run_dir / "PASS", args.run_dir / "FIXTURE-PASS"):
                    if stale.exists():
                        stale.unlink()
                atomic_write_json(
                    args.run_dir / "FAILED",
                    {"state": "FAILED", "failed_at_utc": utc_now(), "error": str(exc)},
                )
        except OSError:
            pass
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2
    try:
        lease_receipt = issue_batch_lease(args, result)
    except (GateError, OSError, ValueError, subprocess.SubprocessError) as exc:
        try:
            atomic_write_json(
                args.run_dir / "BATCH-LEASE-FAILED.json",
                {
                    "schema_version": "p02b-batch-lease-issuance-v2",
                    "state": "FAILED",
                    "failed_at_utc": utc_now(),
                    "p02b_pass_preserved": True,
                    "error": str(exc),
                },
            )
        except OSError:
            pass
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 3
    output = dict(result)
    if lease_receipt is not None:
        output["batch_lease_issuance"] = lease_receipt
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
