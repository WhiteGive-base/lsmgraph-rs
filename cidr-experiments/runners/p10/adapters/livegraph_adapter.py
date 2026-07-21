#!/usr/bin/env python3
"""Fail-closed LiveGraph adapter for the CIDR P10 typed-neighbor contract.

The adapter binds the P10 request's exact binary, dataset, truth, and store
lineage to a native LiveGraph worker.  The vendored LiveGraph revision is
currently import-only: its Graph constructor truncates block/WAL files and
does not reconstruct graph metadata.  Fixture mode can therefore import a
small dense graph and exercise the real query path, while formal mode refuses
to run until the worker advertises an explicitly reopenable query store.
"""

from __future__ import annotations

import argparse
import csv
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
)

WORKER_SCHEMA = "cidr-livegraph-p10-worker-v1"
IMPORT_ONLY_CAPABILITY = "import-only-process-lifetime-v1"
FORMAL_STORE_CAPABILITY = "reopenable-query-store-v1"
SHA256_CHARS = frozenset("0123456789abcdef")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store-label", default="livegraph")
    parser.add_argument("--block-name", default="livegraph-block")
    parser.add_argument("--wal-name", default="livegraph-wal")
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def exact_sha(value: object, context: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in SHA256_CHARS for char in value):
        raise ContractError(f"{context}: expected 64 lowercase hexadecimal characters")
    return value


def checked_file(ref: object, context: str, *, executable: bool = False) -> Path:
    item = require_keys(ref, required=("path", "sha256"), allowed=("path", "sha256"), context=context)
    path = Path(nonempty_string(item["path"], f"{context}.path")).resolve()
    if not path.is_file():
        raise ContractError(f"{context}: file does not exist: {path}")
    if executable and not os.access(path, os.X_OK):
        raise ContractError(f"{context}: file is not executable: {path}")
    expected = exact_sha(item["sha256"], f"{context}.sha256")
    actual = sha256_file(path)
    if actual != expected:
        raise ContractError(f"{context}: SHA-256 mismatch for {path}")
    return path


def validate_request(request_path: Path, store_label: str) -> tuple[dict[str, Any], list[dict[str, int]], Path, Path, Path]:
    request = require_keys(
        read_json(request_path, "LiveGraph adapter request"),
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
        context="LiveGraph adapter request",
    )
    exact_values = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "system_id": "livegraph",
        "group": "embedded",
        "interface_scope": INTERFACE_SCOPE,
    }
    for key, expected in exact_values.items():
        if request[key] != expected:
            raise ContractError(f"request.{key}: {request[key]!r} != {expected!r}")
    nonempty_string(request["suite_id"], "request.suite_id")
    nonempty_string(request["run_id"], "request.run_id")
    nonempty_string(request["system_version"], "request.system_version")
    integer(request["repeat_index"], "request.repeat_index", 1)
    if request["execution_mode"] not in ("fixture", "formal"):
        raise ContractError("request.execution_mode must be 'fixture' or 'formal'")

    binary = checked_file(request["binary"], "request.binary", executable=True)
    dataset = checked_file(request["dataset"], "request.dataset")
    truth_obj = require_keys(
        request["truth"],
        required=("path", "sha256", "query_count", "digest_algorithm"),
        allowed=("path", "sha256", "query_count", "digest_algorithm"),
        context="request.truth",
    )
    truth = checked_file(
        {"path": truth_obj["path"], "sha256": truth_obj["sha256"]}, "request.truth"
    )
    request["truth"]["path"] = str(truth)
    query_count = integer(truth_obj["query_count"], "request.truth.query_count", 1)
    if truth_obj["digest_algorithm"] != TRUTH_DIGEST_ALGORITHM:
        raise ContractError(f"request.truth.digest_algorithm must be {TRUTH_DIGEST_ALGORITHM!r}")
    truth_rows = read_truth(truth, query_count)

    stores = request["store_roots"]
    if not isinstance(stores, list) or not stores:
        raise ContractError("request.store_roots must be a non-empty array")
    selected: Path | None = None
    labels: set[str] = set()
    for index, raw in enumerate(stores):
        context = f"request.store_roots[{index}]"
        item = require_keys(
            raw,
            required=("label", "path", "sha256"),
            allowed=("label", "path", "sha256"),
            context=context,
        )
        label = nonempty_string(item["label"], f"{context}.label")
        if label in labels:
            raise ContractError(f"{context}.label is duplicated")
        labels.add(label)
        path = Path(nonempty_string(item["path"], f"{context}.path")).resolve()
        if not path.is_dir():
            raise ContractError(f"{context}.path is not an existing directory: {path}")
        lineage_sha = item["sha256"]
        if lineage_sha:
            exact_sha(lineage_sha, f"{context}.sha256")
        if request["execution_mode"] == "formal" and not lineage_sha:
            raise ContractError(f"{context}.sha256 is required in formal mode")
        if label == store_label:
            selected = path
    if selected is None:
        raise ContractError(f"request.store_roots has no label {store_label!r}")

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
    expected_timing = {
        "timing_boundary": TIMING_BOUNDARY,
        "clock": CLOCK_NAME,
        "cache_policy": "full-shared-trace-before-each-measured-repeat-v1",
        "process_reuse_between_phases": True,
        "concurrency": 1,
        "sequence_digest_algorithm": SEQUENCE_DIGEST_ALGORITHM,
    }
    for key, expected in expected_timing.items():
        if timing[key] != expected:
            raise ContractError(f"request.timing.{key}: {timing[key]!r} != {expected!r}")
    boolean(timing["process_reuse_between_phases"], "request.timing.process_reuse_between_phases")
    integer(timing["warmup_passes"], "request.timing.warmup_passes", 1)
    integer(timing["measured_passes"], "request.timing.measured_passes", 1)
    integer(timing["concurrency"], "request.timing.concurrency", 1)
    integer(timing["per_query_timeout_ms"], "request.timing.per_query_timeout_ms", 1)
    return request, truth_rows, binary, dataset, selected


def worker_capability(binary: Path) -> str:
    completed = subprocess.run(
        [str(binary), "--capabilities"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise ContractError(
            f"LiveGraph worker capability probe exited {completed.returncode}: {completed.stderr.strip()}"
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError(f"LiveGraph worker returned malformed capability JSON: {exc}") from exc
    value = require_keys(
        value,
        required=("schema_version", "store_capability"),
        allowed=("schema_version", "store_capability"),
        context="LiveGraph worker capabilities",
    )
    if value["schema_version"] != WORKER_SCHEMA:
        raise ContractError("LiveGraph worker capability schema is incompatible")
    return nonempty_string(value["store_capability"], "worker.store_capability")


def safe_store_file(root: Path, raw_name: str, context: str) -> Path:
    name = nonempty_string(raw_name, context)
    if Path(name).name != name or name in (".", ".."):
        raise ContractError(f"{context} must be one basename without directory traversal")
    path = (root / name).resolve()
    if path.parent != root:
        raise ContractError(f"{context} escapes the selected store root")
    return path


def load_observations(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if reader.fieldnames != OBSERVATION_COLUMNS:
                raise ContractError(f"LiveGraph observations header mismatch: {reader.fieldnames!r}")
            return list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ContractError(f"cannot read LiveGraph observations: {exc}") from exc


def validate_worker_output(
    output_dir: Path,
    request: dict[str, Any],
    truth_rows: list[dict[str, int]],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    summary = require_keys(
        read_json(output_dir / "livegraph-worker-summary.json", "LiveGraph worker summary"),
        required=(
            "schema_version",
            "store_capability",
            "vertex_count",
            "edge_count",
            "truth_query_count",
            "import_elapsed_ns",
            "warmup",
            "measured",
        ),
        allowed=(
            "schema_version",
            "store_capability",
            "vertex_count",
            "edge_count",
            "truth_query_count",
            "import_elapsed_ns",
            "warmup",
            "measured",
        ),
        context="LiveGraph worker summary",
    )
    if summary["schema_version"] != WORKER_SCHEMA or summary["store_capability"] != IMPORT_ONLY_CAPABILITY:
        raise ContractError("LiveGraph worker summary reports an unexpected capability")
    integer(summary["vertex_count"], "worker.vertex_count", 1)
    integer(summary["edge_count"], "worker.edge_count", 0)
    integer(summary["import_elapsed_ns"], "worker.import_elapsed_ns", 1)
    if summary["truth_query_count"] != len(truth_rows):
        raise ContractError("LiveGraph worker truth query count changed")
    observations = load_observations(output_dir / "query-observations.tsv")
    phase_passes = {
        "warmup": request["timing"]["warmup_passes"],
        "measured": request["timing"]["measured_passes"],
    }
    expected_total = len(truth_rows) * sum(phase_passes.values())
    if len(observations) != expected_total:
        raise ContractError(f"LiveGraph worker wrote {len(observations)} observations, expected {expected_total}")

    position = 0
    timeout_ns = request["timing"]["per_query_timeout_ms"] * 1_000_000
    for phase in ("warmup", "measured"):
        for pass_index in range(phase_passes[phase]):
            for truth in truth_rows:
                row = observations[position]
                position += 1
                expected = {
                    "contract_version": CONTRACT_VERSION,
                    "system_id": "livegraph",
                    "group": "embedded",
                    "repeat_index": str(request["repeat_index"]),
                    "phase": phase,
                    "pass_index": str(pass_index),
                    "query_index": str(truth["query_index"]),
                    "edge_type": str(truth["edge_type"]),
                    "src": str(truth["src"]),
                    "expected_count": str(truth["count"]),
                    "expected_sum_hash": str(truth["sum_hash"]),
                    "expected_xor_hash": str(truth["xor_hash"]),
                }
                for key, expected_value in expected.items():
                    if row.get(key) != expected_value:
                        raise ContractError(
                            f"LiveGraph observation {position - 1}.{key}: {row.get(key)!r} != {expected_value!r}"
                        )
                try:
                    latency_ns = int(row["latency_ns"])
                except ValueError as exc:
                    raise ContractError("LiveGraph observation latency is not an integer") from exc
                if latency_ns <= 0:
                    raise ContractError("LiveGraph observation latency must be positive")
                if row["status"] == "timeout":
                    if latency_ns < timeout_ns:
                        raise ContractError("LiveGraph timeout was reported below the deadline")
                    raise ContractError("LiveGraph timeout makes the repeat ineligible")
                if row["status"] != "ok" or latency_ns > timeout_ns:
                    raise ContractError("LiveGraph observation has an invalid status/deadline combination")
                actual = (row["actual_count"], row["actual_sum_hash"], row["actual_xor_hash"])
                expected_digest = (str(truth["count"]), str(truth["sum_hash"]), str(truth["xor_hash"]))
                if actual != expected_digest:
                    raise ContractError(f"LiveGraph truth mismatch at query_index={truth['query_index']}")

    for phase, passes in phase_passes.items():
        phase_summary = require_keys(
            summary[phase],
            required=(
                "passes",
                "started_monotonic_ns",
                "ended_monotonic_ns",
                "elapsed_ns",
                "total_query_latency_ns",
            ),
            allowed=(
                "passes",
                "started_monotonic_ns",
                "ended_monotonic_ns",
                "elapsed_ns",
                "total_query_latency_ns",
            ),
            context=f"worker.{phase}",
        )
        if phase_summary["passes"] != passes:
            raise ContractError(f"worker.{phase}.passes changed")
        start = integer(phase_summary["started_monotonic_ns"], f"worker.{phase}.start", 0)
        end = integer(phase_summary["ended_monotonic_ns"], f"worker.{phase}.end", start + 1)
        elapsed = integer(phase_summary["elapsed_ns"], f"worker.{phase}.elapsed", 1)
        total_query = integer(
            phase_summary["total_query_latency_ns"], f"worker.{phase}.total_query_latency", 1
        )
        if elapsed != end - start or elapsed < total_query:
            raise ContractError(f"worker.{phase} has inconsistent CLOCK_MONOTONIC boundaries")
    if summary["warmup"]["ended_monotonic_ns"] > summary["measured"]["started_monotonic_ns"]:
        raise ContractError("LiveGraph warmup and measured intervals overlap")
    return observations, summary


def publish_result(
    output_dir: Path,
    request: dict[str, Any],
    truth_rows: list[dict[str, int]],
    observations: list[dict[str, str]],
    worker_summary: dict[str, Any],
) -> None:
    phases: dict[str, dict[str, Any]] = {}
    for phase in ("warmup", "measured"):
        passes = request["timing"][f"{phase}_passes"]
        rows = [row for row in observations if row["phase"] == phase]
        summary = worker_summary[phase]
        phases[phase] = {
            "passes": passes,
            "requested_queries": passes * len(truth_rows),
            "completed_queries": len(rows),
            "timeout_queries": 0,
            "mismatch_queries": 0,
            "started_monotonic_ns": summary["started_monotonic_ns"],
            "ended_monotonic_ns": summary["ended_monotonic_ns"],
            "elapsed_ns": summary["elapsed_ns"],
            "expected_digest_sha256": phase_digest(phase, passes, truth_rows),
            "actual_digest_sha256": phase_digest(phase, passes, truth_rows, rows),
        }
        if phases[phase]["actual_digest_sha256"] != phases[phase]["expected_digest_sha256"]:
            raise ContractError(f"LiveGraph {phase} sequence digest mismatch")
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
        "concurrency": 1,
        "per_query_timeout_ms": request["timing"]["per_query_timeout_ms"],
        "warmup": phases["warmup"],
        "measured": phases["measured"],
    }
    atomic_json(output_dir / "adapter-result.json", result)


def main() -> int:
    args = parse_args()
    try:
        request, truth_rows, binary, dataset, store_root = validate_request(
            args.request.resolve(), args.store_label
        )
        block_path = safe_store_file(store_root, args.block_name, "--block-name")
        wal_path = safe_store_file(store_root, args.wal_name, "--wal-name")
        if block_path == wal_path:
            raise ContractError("LiveGraph block and WAL paths must differ")
        capability = worker_capability(binary)
        if request["execution_mode"] == "formal":
            if len(truth_rows) != 1700:
                raise ContractError("formal LiveGraph P10 requires the frozen 1,700-query truth")
            if capability != FORMAL_STORE_CAPABILITY:
                raise ContractError(
                    "formal LiveGraph P10 requires query-only reusable store capability "
                    f"{FORMAL_STORE_CAPABILITY!r}; worker reports {capability!r}"
                )
            store_mode = "reopen"
            if not block_path.is_file() or not wal_path.is_file():
                raise ContractError("formal LiveGraph P10 requires an existing compatible block/WAL store")
        else:
            if capability != IMPORT_ONLY_CAPABILITY:
                raise ContractError("fixture adapter expected the audited import-only LiveGraph worker")
            store_mode = "import"
            if block_path.exists() or wal_path.exists():
                raise ContractError("fixture import refuses to overwrite an existing block/WAL file")

        output_dir = args.output_dir.resolve()
        if output_dir == store_root or store_root in output_dir.parents or output_dir in store_root.parents:
            raise ContractError("LiveGraph adapter output and store roots must not overlap")
        output_dir.mkdir(parents=True, exist_ok=True)
        required_outputs = (
            "query-observations.tsv",
            "phase-events.jsonl",
            "adapter-result.json",
            "livegraph-worker-summary.json",
        )
        if any((output_dir / name).exists() for name in required_outputs):
            raise ContractError("LiveGraph output directory already contains adapter artifacts")
        command = [
            str(binary),
            "--store-mode",
            store_mode,
            "--truth-tsv",
            request["truth"]["path"],
            "--block-path",
            str(block_path),
            "--wal-path",
            str(wal_path),
            "--output-dir",
            str(output_dir),
            "--warmup-passes",
            str(request["timing"]["warmup_passes"]),
            "--measured-passes",
            str(request["timing"]["measured_passes"]),
            "--per-query-timeout-ms",
            str(request["timing"]["per_query_timeout_ms"]),
            "--expected-query-count",
            str(len(truth_rows)),
            "--repeat-index",
            str(request["repeat_index"]),
        ]
        if store_mode == "import":
            command.extend(("--edges", str(dataset)))
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        (output_dir / "livegraph-worker.stdout.log").write_bytes(completed.stdout)
        (output_dir / "livegraph-worker.stderr.log").write_bytes(completed.stderr)
        if completed.returncode != 0:
            raise ContractError(f"LiveGraph worker exited {completed.returncode}")
        observations, worker_summary = validate_worker_output(output_dir, request, truth_rows)
        publish_result(output_dir, request, truth_rows, observations, worker_summary)
        return 0
    except (ContractError, OSError, subprocess.SubprocessError) as exc:
        print(f"livegraph_adapter: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
