#!/usr/bin/env python3
"""Strict SemL0 adapter for the P10/P11 typed-neighbor contract.

The adapter validates all frozen lineage before launching exactly one
``lsmgraph storage-bench`` process.  That process owns both the whole-trace
warmup and measured phases, preserving Engine-local cache/controller state.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

P10_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(P10_DIR))

from p10_contract import (  # noqa: E402
    CLOCK_NAME,
    CONTRACT_VERSION,
    INTERFACE_SCOPE,
    OBSERVATION_COLUMNS,
    RESULT_SCHEMA_VERSION,
    SEQUENCE_DIGEST_ALGORITHM,
    TIMING_BOUNDARY,
    ContractError,
    atomic_json,
    phase_digest,
    read_json,
    read_truth,
    sha256_file,
    validate_adapter_outputs,
)

RAW_SCHEMA_VERSION = "p10-seml0-storage-bench-raw-v1"
PROVENANCE_SCHEMA_VERSION = "p10-seml0-adapter-provenance-v1"
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
VARIANTS = {
    "naive": ("naive", False),
    "schema": ("schema", False),
    "b64": ("semantic-budgeted", True),
    "budg-b64": ("semantic-budgeted", True),
    "semantic": ("semantic", True),
}
TREE_HASH_METHOD = "sha256-tree-v1(relative-path,size,file-sha256)"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def normalize_sha(value: str, label: str) -> str:
    value = value.lower()
    require(len(value) == 64 and all(char in "0123456789abcdef" for char in value), f"{label}: invalid SHA-256")
    return value


def resolve_file(path: Path, expected_sha: str, label: str, executable: bool = False) -> dict[str, Any]:
    path = path.resolve()
    require(path.is_file(), f"{label}: missing file: {path}")
    if executable:
        require(os.access(path, os.X_OK), f"{label}: file is not executable: {path}")
    expected_sha = normalize_sha(expected_sha, f"{label} SHA-256")
    actual_sha = sha256_file(path)
    require(actual_sha == expected_sha, f"{label}: SHA-256 mismatch")
    return {"path": str(path), "sha256": actual_sha, "size_bytes": path.stat().st_size}


def reject_fixtureish(path: Path, label: str) -> None:
    parts = [part.lower() for part in path.resolve().parts]
    require(not any("fixture" in part or part == "tests" for part in parts), f"formal {label} rejects fixture/test path")


def same_path(left: object, right: Path, label: str) -> None:
    require(isinstance(left, str), f"{label}: expected path string")
    require(Path(left).resolve() == right.resolve(), f"{label}: path mismatch")


def read_object(path: Path, label: str) -> dict[str, Any]:
    return read_json(path, label)


def command_digest(argv: list[str]) -> str:
    encoded = json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def artifact_ref(path: Path) -> dict[str, Any]:
    path = path.resolve()
    return {"path": str(path), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}


def git_state(repo_root: Path) -> dict[str, Any]:
    try:
        head = subprocess.check_output(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True, stderr=subprocess.STDOUT, timeout=20
        ).strip()
        status = subprocess.check_output(
            ["git", "-C", str(repo_root), "status", "--porcelain=v1", "--untracked-files=normal"],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContractError(f"cannot audit Git repository: {exc}") from exc
    require(len(head) == 40, "Git HEAD is not a full commit SHA")
    return {"root": str(repo_root), "head": head, "clean": status == "", "status_sha256": hashlib.sha256(status.encode()).hexdigest()}


def tree_manifest(root: Path) -> dict[str, Any]:
    root = root.resolve()
    require(root.is_dir(), f"store directory does not exist: {root}")
    files: list[Path] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in sorted(dirnames):
            require(not (directory_path / name).is_symlink(), "store tree rejects symlink directory")
        for name in sorted(filenames):
            path = directory_path / name
            require(not path.is_symlink() and path.is_file(), f"store tree found non-regular file: {path}")
            files.append(path)
    files.sort(key=lambda path: path.relative_to(root).as_posix().encode("utf-8"))
    require(bool(files), "store tree must not be empty")
    digest = hashlib.sha256()
    total_bytes = 0
    for path in files:
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        total_bytes += size
        digest.update(f"file\0{relative}\0{size}\0{sha256_file(path)}\n".encode("utf-8"))
    return {"sha256": digest.hexdigest(), "file_count": len(files), "total_bytes": total_bytes, "hash_method": TREE_HASH_METHOD}


def validate_id_map(directory: Path, manifest_expected_sha: str) -> dict[str, Any]:
    directory = directory.resolve()
    require(directory.is_dir(), f"ID-map directory does not exist: {directory}")
    manifest_path = directory / "id-map-manifest.json"
    manifest_ref = resolve_file(manifest_path, manifest_expected_sha, "ID-map manifest")
    manifest = read_object(manifest_path, "ID-map manifest")
    require(manifest.get("format") == "seml0-shared-id-map", "ID-map manifest has wrong format")
    require(manifest.get("format_version") == 1, "ID-map manifest has wrong format_version")
    require(manifest.get("mapping_hash_algorithm") == "fnv1a64-le-dense-original-v1", "ID-map mapping hash algorithm mismatch")
    files: dict[str, Any] = {}
    for key in ("dense_to_original", "original_to_dense"):
        value = manifest.get(key)
        require(isinstance(value, dict), f"ID-map manifest lacks {key}")
        name = value.get("path")
        require(isinstance(name, str) and Path(name).name == name, f"ID-map {key} path must be a basename")
        files[key] = resolve_file(directory / name, str(value.get("sha256", "")), f"ID-map {key}")
    return {"directory": str(directory), "manifest": manifest_ref, "files": files, "mapping_hash": manifest.get("mapping_hash")}


def validate_plan(path: Path, expected_sha: str, expected_queries: int) -> dict[str, Any]:
    ref = resolve_file(path, expected_sha, "shared sample plan")
    plan = read_object(path, "shared sample plan")
    require(plan.get("version") == 1 and plan.get("source") == "shared-truth-tsv", "sample plan is not a shared-truth v1 plan")
    require(plan.get("src_label") is None and plan.get("dst_label") is None, "sample plan contains label filters")
    entries = plan.get("entries")
    require(isinstance(entries, list), "sample plan entries must be an array")
    count = 0
    for index, entry in enumerate(entries):
        require(isinstance(entry, dict), f"sample plan entry {index} must be an object")
        require(type(entry.get("edge_type")) is int, f"sample plan entry {index} lacks typed edge_type")
        require(entry.get("src_label") is None and entry.get("dst_label") is None, "sample plan entry contains label filters")
        samples = entry.get("samples")
        require(isinstance(samples, list), f"sample plan entry {index} samples must be an array")
        count += len(samples)
    require(count == expected_queries, f"sample plan has {count} queries, expected {expected_queries}")
    return {**ref, "query_count": count}


def validate_store(args: argparse.Namespace, formal: bool, p02b: dict[str, Any]) -> dict[str, Any]:
    store = args.data_dir.resolve()
    require(store.is_dir(), f"SemL0 store does not exist: {store}")
    manifest_ref = resolve_file(args.store_manifest, args.store_manifest_sha256, "store manifest")
    manifest = read_object(args.store_manifest, "store manifest")
    require(manifest.get("schema_version") == "p02b-store-manifest-v1", "store manifest schema mismatch")
    same_path(manifest.get("store_path"), store, "store manifest.store_path")
    frozen_sha = normalize_sha(args.store_tree_sha256, "store tree SHA-256")
    require(manifest.get("store_sha256") == frozen_sha, "store manifest digest differs from --store-tree-sha256")
    provenance = p02b.get("provenance", {})
    require(provenance.get("store_sha256") == frozen_sha, "P02B store digest differs from current P10 store")
    current: dict[str, Any] | None = None
    if formal:
        require(manifest.get("hash_method") == TREE_HASH_METHOD, "formal store manifest has wrong tree hash method")
        current = tree_manifest(store)
        require(current["sha256"] == frozen_sha, "current store tree differs from frozen SHA-256")
        if "file_count" in manifest:
            require(manifest["file_count"] == current["file_count"], "store manifest file_count mismatch")
        if "total_bytes" in manifest:
            require(manifest["total_bytes"] == current["total_bytes"], "store manifest total_bytes mismatch")
    return {"path": str(store), "tree_sha256": frozen_sha, "manifest": manifest_ref, "current_tree": current}


def validate_p02b(
    args: argparse.Namespace,
    formal: bool,
    git: dict[str, Any],
    binary: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    result_ref = resolve_file(args.p02b_result, args.p02b_result_sha256, "P02B result")
    validator_ref = resolve_file(args.p02b_validator, args.p02b_validator_sha256, "P02B validator", executable=True)
    command = [
        str(args.p02b_validator.resolve()),
        "--result",
        str(args.p02b_result.resolve()),
        "--consumer",
        "P10",
        "--expected-repo-root",
        str(args.repo_root),
        "--expected-repo-head",
        git["head"],
        "--expected-binary-sha256",
        binary["sha256"],
    ]
    if formal:
        command.append("--require-formal")
    completed = subprocess.run(command, text=True, capture_output=True, check=False, timeout=60)
    require(completed.returncode == 0, f"P02B admission failed: {completed.stderr.strip()}")
    try:
        admission = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError(f"P02B validator emitted invalid JSON: {exc}") from exc
    require(admission.get("state") == "PASS" and admission.get("consumer") == "P10", "P02B validator returned a non-PASS admission")
    require(admission.get("formal_required") is formal, "P02B validator receipt formal mode mismatch")
    require(type(admission.get("fixture_only")) is bool, "P02B validator receipt classification is invalid")
    if formal:
        require(admission["fixture_only"] is False, "formal P02B validator receipt is fixture-only")
    require(admission.get("scale") == "sf10", "P02B validator receipt scale mismatch")
    same_path(admission.get("sentinel_result"), args.p02b_result, "P02B validator receipt result")
    require(
        admission.get("sentinel_result_sha256") == result_ref["sha256"],
        "P02B validator receipt result SHA-256 mismatch",
    )
    same_path(admission.get("repo_root"), args.repo_root, "P02B validator receipt repo root")
    require(admission.get("repo_head") == git["head"], "P02B validator receipt repo HEAD mismatch")
    require(admission.get("binary_sha256") == binary["sha256"], "P02B validator receipt binary mismatch")
    marker_path = Path(str(admission.get("pass_marker", "")))
    provenance_path = Path(str(admission.get("provenance", "")))
    marker_ref = resolve_file(
        marker_path,
        str(admission.get("pass_marker_sha256", "")),
        "P02B PASS marker receipt",
    )
    provenance_ref = resolve_file(
        provenance_path,
        str(admission.get("provenance_sha256", "")),
        "P02B provenance receipt",
    )
    result = read_object(args.p02b_result, "P02B result")
    require(sha256_file(args.p02b_result.resolve()) == result_ref["sha256"], "P02B result changed after validation")
    require(result.get("run_id") == admission.get("run_id"), "P02B result/receipt run_id mismatch")
    require(result.get("completed_at_utc") == admission.get("completed_at_utc"), "P02B result/receipt completion mismatch")
    if formal:
        require(result.get("scale") == "sf10", "formal P10 requires an SF10 P02B sentinel")
        require(result.get("formal_gate_eligible") is True and result.get("downstream_release_eligible") is True, "P02B did not release formal P10")
        require(result.get("protocol", {}).get("expected_queries") == 1700, "formal P02B query count must be 1700")
    return result, {
        "result": result_ref,
        "pass_marker": marker_ref,
        "provenance": provenance_ref,
        "validator": validator_ref,
        "admission": admission,
    }


def bind_p02b_provenance_files(
    p02b_binding: dict[str, Any],
    expected: dict[str, dict[str, Any]],
) -> None:
    provenance_path = Path(p02b_binding["provenance"]["path"])
    provenance = read_object(provenance_path, "P02B canonical provenance")
    require(
        sha256_file(provenance_path) == p02b_binding["provenance"]["sha256"],
        "P02B provenance changed after validation",
    )
    files = provenance.get("files")
    require(isinstance(files, dict), "P02B provenance lacks canonical files")
    for key, reference in expected.items():
        observed = files.get(key)
        require(isinstance(observed, dict), f"P02B provenance lacks files.{key}")
        same_path(observed.get("path"), Path(reference["path"]), f"P02B provenance files.{key}")
        require(observed.get("sha256") == reference["sha256"], f"P02B provenance files.{key} SHA-256 mismatch")
        require(observed.get("size_bytes") == reference["size_bytes"], f"P02B provenance files.{key} size mismatch")


def read_raw_observations(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            require(reader.fieldnames == RAW_COLUMNS, f"SemL0 raw observation header mismatch: {reader.fieldnames!r}")
            return list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ContractError(f"cannot read SemL0 raw observations: {exc}") from exc


def atomic_text(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def convert_outputs(args: argparse.Namespace, request: dict[str, Any], truth_rows: list[dict[str, int]], raw_dir: Path) -> None:
    raw_result_path = raw_dir / "p10-raw-result.json"
    raw_observations_path = raw_dir / "p10-raw-observations.tsv"
    raw_events_path = raw_dir / "p10-raw-phase-events.jsonl"
    raw_result = read_object(raw_result_path, "SemL0 raw result")
    require(raw_result.get("schema_version") == RAW_SCHEMA_VERSION, "SemL0 raw result schema mismatch")
    require(raw_result.get("clock") == CLOCK_NAME, "SemL0 raw clock mismatch")
    require(raw_result.get("timing_boundary") == TIMING_BOUNDARY, "SemL0 raw timing boundary mismatch")
    require(raw_result.get("query_count") == len(truth_rows), "SemL0 raw query count mismatch")
    require(raw_result.get("per_query_timeout_ms") == request["timing"]["per_query_timeout_ms"], "SemL0 raw timeout mismatch")
    raw_rows = read_raw_observations(raw_observations_path)
    rows: list[dict[str, str]] = []
    for raw in raw_rows:
        rows.append(
            {
                "contract_version": CONTRACT_VERSION,
                "system_id": request["system_id"],
                "group": request["group"],
                "repeat_index": str(request["repeat_index"]),
                **raw,
            }
        )
    with (args.output_dir / "query-observations.tsv.tmp").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OBSERVATION_COLUMNS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(args.output_dir / "query-observations.tsv.tmp", args.output_dir / "query-observations.tsv")

    raw_events: list[dict[str, Any]] = []
    for line in raw_events_path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        raw_events.append({"contract_version": CONTRACT_VERSION, **value})
    atomic_text(
        args.output_dir / "phase-events.jsonl",
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in raw_events),
    )
    phase_rows = {phase: [row for row in rows if row["phase"] == phase] for phase in ("warmup", "measured")}
    phase_result: dict[str, Any] = {}
    for phase, pass_key in (("warmup", "warmup_passes"), ("measured", "measured_passes")):
        raw_summary = raw_result.get(phase)
        require(isinstance(raw_summary, dict), f"SemL0 raw result lacks {phase} summary")
        passes = request["timing"][pass_key]
        require(raw_summary.get("passes") == passes, f"SemL0 raw {phase} pass count mismatch")
        phase_result[phase] = {
            **raw_summary,
            "expected_digest_sha256": phase_digest(phase, passes, truth_rows),
            "actual_digest_sha256": phase_digest(phase, passes, truth_rows, phase_rows[phase]),
        }
    result = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "system_id": request["system_id"],
        "group": request["group"],
        "system_version": request["system_version"],
        "interface_scope": INTERFACE_SCOPE,
        "repeat_index": request["repeat_index"],
        "truth_sha256": request["truth"]["sha256"],
        "sequence_digest_algorithm": SEQUENCE_DIGEST_ALGORITHM,
        "timing_boundary": TIMING_BOUNDARY,
        "clock": CLOCK_NAME,
        "concurrency": request["timing"]["concurrency"],
        "per_query_timeout_ms": request["timing"]["per_query_timeout_ms"],
        "warmup": phase_result["warmup"],
        "measured": phase_result["measured"],
    }
    atomic_json(args.output_dir / "adapter-result.json", result)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("fixture", "formal"))
    parser.add_argument("--variant", required=True, choices=tuple(VARIANTS))
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--binary-sha256", required=True)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--store-manifest", required=True, type=Path)
    parser.add_argument("--store-manifest-sha256", required=True)
    parser.add_argument("--store-tree-sha256", required=True)
    parser.add_argument("--sample-plan", required=True, type=Path)
    parser.add_argument("--sample-plan-sha256", required=True)
    parser.add_argument("--truth", required=True, type=Path)
    parser.add_argument("--truth-sha256", required=True)
    parser.add_argument("--id-map-dir", required=True, type=Path)
    parser.add_argument("--id-map-manifest-sha256", required=True)
    parser.add_argument("--p02b-result", required=True, type=Path)
    parser.add_argument("--p02b-result-sha256", required=True)
    parser.add_argument("--p02b-validator", required=True, type=Path)
    parser.add_argument("--p02b-validator-sha256", required=True)
    parser.add_argument("--p31-wrapper", required=True, type=Path)
    parser.add_argument("--p31-wrapper-sha256", required=True)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--io-backend", choices=("blocking", "io-uring"), default="blocking")
    parser.add_argument("--csr-metadata-cache-entries", type=int, default=4096)
    parser.add_argument("--query-control-stage", default="a6")
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def run(args: argparse.Namespace) -> None:
    formal = args.mode == "formal"
    args.repo_root = args.repo_root.resolve()
    args.output_dir = args.output_dir.resolve()
    require(args.repo_root.is_dir(), "repo root does not exist")
    require(args.csr_metadata_cache_entries > 0, "CSR metadata cache entries must be positive")
    request_ref = artifact_ref(args.request)
    request = read_object(args.request, "P10 adapter request")
    require(request.get("schema_version") == "cidr-p10-adapter-request-v1", "request schema mismatch")
    require(request.get("contract_version") == CONTRACT_VERSION, "request contract mismatch")
    require(request.get("system_id") == "seml0" and request.get("group") == "embedded", "SemL0 adapter received another system")
    require(request.get("interface_scope") == INTERFACE_SCOPE, "request interface scope mismatch")
    timing = request.get("timing", {})
    require(timing.get("timing_boundary") == TIMING_BOUNDARY and timing.get("clock") == CLOCK_NAME, "request timing contract mismatch")
    require(timing.get("process_reuse_between_phases") is True and timing.get("concurrency") == 1, "SemL0 adapter requires one reused process and concurrency=1")
    require(type(timing.get("warmup_passes")) is int and timing["warmup_passes"] > 0, "warmup passes must be positive")
    require(type(timing.get("measured_passes")) is int and timing["measured_passes"] > 0, "measured passes must be positive")
    require(type(timing.get("per_query_timeout_ms")) is int and timing["per_query_timeout_ms"] > 0, "query timeout must be positive")
    if formal:
        require(request.get("truth", {}).get("query_count") == 1700, "formal SemL0 adapter requires 1700 queries")

    binary = resolve_file(args.binary, args.binary_sha256, "SemL0 binary", executable=True)
    truth = resolve_file(args.truth, args.truth_sha256, "truth TSV")
    same_path(request.get("truth", {}).get("path"), args.truth, "request truth")
    require(request["truth"].get("sha256") == truth["sha256"], "request truth SHA-256 mismatch")
    truth_rows = read_truth(args.truth.resolve(), request["truth"]["query_count"])
    plan = validate_plan(args.sample_plan.resolve(), args.sample_plan_sha256, len(truth_rows))
    id_map = validate_id_map(args.id_map_dir, args.id_map_manifest_sha256)
    p31 = resolve_file(args.p31_wrapper, args.p31_wrapper_sha256, "P31 wrapper", executable=True)
    git = git_state(args.repo_root)
    if formal:
        require(git["clean"], "formal SemL0 adapter requires a clean Git worktree")
    p02b_result, p02b = validate_p02b(args, formal, git, binary)
    provenance = p02b_result.get("provenance", {})
    require(provenance.get("binary_sha256") == binary["sha256"], "P02B binary differs from P10 binary")
    require(provenance.get("truth_sha256") == truth["sha256"], "P02B truth differs from P10 truth")
    require(provenance.get("query_plan_sha256") == plan["sha256"], "P02B query plan differs from P10 sample plan")
    if formal:
        require(provenance.get("repo_head") == git["head"], "P02B repo HEAD differs from current P10 repo")
        for path, label in (
            (args.binary, "binary"),
            (args.truth, "truth"),
            (args.sample_plan, "sample plan"),
            (args.id_map_dir, "ID map"),
            (args.data_dir, "store"),
            (args.store_manifest, "store manifest"),
            (args.p02b_result, "P02B result"),
            (args.p02b_validator, "P02B validator"),
            (args.p31_wrapper, "P31 wrapper"),
        ):
            reject_fixtureish(path, label)
    store = validate_store(args, formal, p02b_result)
    bind_p02b_provenance_files(
        p02b,
        {
            "binary": binary,
            "truth": truth,
            "query_plan": plan,
            "store_manifest": store["manifest"],
            "id_map_manifest": id_map["manifest"],
            "p31_wrapper": p31,
        },
    )

    for protected, label in (
        (args.data_dir.resolve(), "store"),
        (args.id_map_dir.resolve(), "ID map"),
    ):
        require(
            args.output_dir != protected
            and protected not in args.output_dir.parents
            and args.output_dir not in protected.parents,
            f"adapter output must not overlap the immutable {label}",
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name in ("adapter-result.json", "query-observations.tsv", "phase-events.jsonl", "adapter-provenance.json"):
        require(not (args.output_dir / name).exists(), f"adapter refuses to overwrite {name}")
    raw_dir = args.output_dir / "seml0-raw"
    require(not raw_dir.exists(), "adapter refuses to overwrite seml0-raw")
    layout, degree_hint = VARIANTS[args.variant]
    command = [
        str(args.binary.resolve()),
        "--io-backend",
        args.io_backend,
        "--csr-metadata-cache-entries",
        str(args.csr_metadata_cache_entries),
        "storage-bench",
        "--data-dir",
        str(args.data_dir.resolve()),
        "--warmup-runs",
        str(timing["warmup_passes"]),
        "--repeats",
        str(timing["measured_passes"]),
        "--sample-plan-in",
        str(args.sample_plan.resolve()),
        "--p10-raw-output-dir",
        str(raw_dir),
        "--p10-truth-tsv",
        str(args.truth.resolve()),
        "--p10-id-map-dir",
        str(args.id_map_dir.resolve()),
        "--p10-per-query-timeout-ms",
        str(timing["per_query_timeout_ms"]),
        "--l0-layout",
        layout,
        "--query-control-stage",
        args.query_control_stage,
    ]
    if degree_hint:
        command.append("--semantic-degree-hint")
    environment = os.environ.copy()
    environment["RUST_BACKTRACE"] = "1"
    with (args.output_dir / "seml0.stdout.log").open("wb") as stdout_handle, (
        args.output_dir / "seml0.stderr.log"
    ).open("wb") as stderr_handle:
        completed = subprocess.run(
            command,
            cwd=args.repo_root,
            env=environment,
            stdout=stdout_handle,
            stderr=stderr_handle,
            check=False,
        )
    require(completed.returncode == 0, f"SemL0 storage-bench exited {completed.returncode}; see seml0.stderr.log")
    convert_outputs(args, request, truth_rows, raw_dir)
    adapter_provenance = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "mode": args.mode,
        "variant": args.variant,
        "request": request_ref,
        "repo": git,
        "binary": binary,
        "store": store,
        "truth": truth,
        "sample_plan": plan,
        "id_map": id_map,
        "p02b": p02b,
        "p31_wrapper": p31,
        "command": {"argv": command, "argv_sha256": command_digest(command), "invocations": 1, "exit_code": completed.returncode},
        "raw_artifacts": {
            name: artifact_ref(raw_dir / name)
            for name in ("p10-raw-result.json", "p10-raw-observations.tsv", "p10-raw-phase-events.jsonl")
        },
    }
    atomic_json(args.output_dir / "adapter-provenance.json", adapter_provenance)
    system = {
        "id": "seml0",
        "group": "embedded",
        "system_version": request["system_version"],
        "display_name": "SemL0",
        "fixture_only": not formal,
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
