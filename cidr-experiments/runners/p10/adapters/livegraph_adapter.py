#!/usr/bin/env python3
"""Fail-closed LiveGraph adapter for the CIDR P10 typed-neighbor contract.

The adapter binds the P10 request's exact binary, runtime library, dataset,
truth, and fresh store roots to a native LiveGraph worker.  Every independent
repeat is explicitly import -> warmup -> measured in one process.  P31 wraps
that whole lifecycle, while query latency/QPS are derived only from measured
typed-neighbor observations and import cost is reported separately.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

P10_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(P10_DIR))
LIVEGRAPH_GATE_DIR = Path(__file__).resolve().parent / "livegraph"
sys.path.insert(0, str(LIVEGRAPH_GATE_DIR))

from p10_contract import (  # noqa: E402
    CLOCK_NAME,
    CONTRACT_VERSION,
    FRESH_IMPORT_PROCESS_LIFETIME,
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
    nearest_rank,
    phase_digest,
    read_json,
    read_truth,
    require_keys,
    sha256_file,
)

import run_sf10_formal as formal_gate  # noqa: E402

WORKER_SCHEMA = "cidr-livegraph-p10-worker-v1"
FRESH_IMPORT_CAPABILITY = FRESH_IMPORT_PROCESS_LIFETIME
PROCESS_LIFETIME = FRESH_IMPORT_PROCESS_LIFETIME
SHA256_CHARS = frozenset("0123456789abcdef")
PROVENANCE_SCHEMA = "p10-livegraph-adapter-provenance-v2"
FIXTURE_PROVENANCE_SCHEMA = "p10-livegraph-adapter-provenance-v1"
STAGE_SCHEMA = "p10-livegraph-adapter-stage-v1"
PID_SCHEMA = "p10-livegraph-worker-pid-v2"
FORMAL_GATE_OPTIONS = (
    "build_receipt",
    "build_receipt_sha256",
    "p02b_result",
    "p02b_result_sha256",
    "p02b_validator",
    "p02b_validator_sha256",
    "p02b_max_age_seconds",
    "temp_base",
    "temp_label",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store-label", default="livegraph")
    parser.add_argument("--block-name", default="livegraph-block")
    parser.add_argument("--wal-name", default="livegraph-wal")
    parser.add_argument("--temp-base", type=Path)
    parser.add_argument("--temp-label")
    parser.add_argument("--build-receipt", type=Path)
    parser.add_argument("--build-receipt-sha256")
    parser.add_argument("--p02b-result", type=Path)
    parser.add_argument("--p02b-result-sha256")
    parser.add_argument("--p02b-validator", type=Path)
    parser.add_argument("--p02b-validator-sha256")
    parser.add_argument("--p02b-max-age-seconds", type=int)
    parser.add_argument("--dataset-seal", type=Path)
    parser.add_argument("--dataset-seal-sha256")
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


def validate_request(
    request_path: Path, store_label: str
) -> tuple[dict[str, Any], list[dict[str, int]], Path, Path, Path, list[Path]]:
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
            "process_lifetime",
            "binary",
            "dataset",
            "runtime_libraries",
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
            "process_lifetime",
            "binary",
            "dataset",
            "runtime_libraries",
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
        "process_lifetime": FRESH_IMPORT_PROCESS_LIFETIME,
    }
    for key, expected in exact_values.items():
        if request[key] != expected:
            raise ContractError(f"request.{key}: {request[key]!r} != {expected!r}")
    nonempty_string(request["suite_id"], "request.suite_id")
    nonempty_string(request["run_id"], "request.run_id")
    nonempty_string(request["system_version"], "request.system_version")
    repeat_index = integer(request["repeat_index"], "request.repeat_index", 1)
    if request["execution_mode"] not in ("fixture", "formal"):
        raise ContractError("request.execution_mode must be 'fixture' or 'formal'")

    binary = checked_file(request["binary"], "request.binary", executable=True)
    if request["execution_mode"] == "formal":
        dataset_ref = require_keys(
            request["dataset"], required=("path", "sha256"), allowed=("path", "sha256"),
            context="request.dataset",
        )
        dataset = Path(nonempty_string(dataset_ref["path"], "request.dataset.path")).resolve()
        exact_sha(dataset_ref["sha256"], "request.dataset.sha256")
        if not dataset.is_file() or dataset.is_symlink():
            raise ContractError("request.dataset is not one regular file")
    else:
        dataset = checked_file(request["dataset"], "request.dataset")
    raw_runtime_libraries = request["runtime_libraries"]
    if not isinstance(raw_runtime_libraries, list) or len(raw_runtime_libraries) != 1:
        raise ContractError("request.runtime_libraries must bind exactly one liblivegraph.so")
    runtime_libraries: list[Path] = []
    normalized_libraries: list[dict[str, str]] = []
    for index, library in enumerate(raw_runtime_libraries):
        path = checked_file(library, f"request.runtime_libraries[{index}]")
        runtime_libraries.append(path)
        normalized_libraries.append({"path": str(path), "sha256": sha256_file(path)})
    if len(runtime_libraries) != len(set(runtime_libraries)):
        raise ContractError("request.runtime_libraries contains duplicate paths")
    if runtime_libraries[0].name != "liblivegraph.so":
        raise ContractError("request.runtime_libraries must name liblivegraph.so")
    request["runtime_libraries"] = normalized_libraries
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
        raw_path = Path(nonempty_string(item["path"], f"{context}.path"))
        if raw_path.is_symlink():
            raise ContractError(f"{context}.path must not be a symbolic link")
        path = raw_path.resolve()
        if not path.is_dir():
            raise ContractError(f"{context}.path is not an existing directory: {path}")
        lineage_sha = item["sha256"]
        if lineage_sha:
            exact_sha(lineage_sha, f"{context}.sha256")
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
    if request["execution_mode"] == "formal":
        if repeat_index > 3:
            raise ContractError("formal LiveGraph repeat_index must be in 1..3")
        if timing["warmup_passes"] != 1 or timing["measured_passes"] != 1:
            raise ContractError("formal LiveGraph requires exactly one warmup and one measured pass")
        if request["dataset"]["sha256"] != formal_gate.CANONICAL_DENSE_SHA256:
            raise ContractError("formal LiveGraph P10 requires the canonical SF10 dense dataset")
        if dataset.stat().st_size != formal_gate.CANONICAL_DENSE_BYTES:
            raise ContractError("formal LiveGraph P10 dense dataset size drift")
        if (
            request["truth"]["sha256"] != formal_gate.CANONICAL_TRUTH_SHA256
            or query_count != formal_gate.CANONICAL_TRUTH_QUERIES
        ):
            raise ContractError("formal LiveGraph P10 requires the canonical 1,700-query truth")
    return request, truth_rows, binary, dataset, selected, runtime_libraries


def worker_capability(binary: Path, environment: dict[str, str]) -> dict[str, str]:
    completed = subprocess.run(
        [str(binary), "--capabilities"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
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
        required=("schema_version", "process_lifetime", "runtime_library_path"),
        allowed=("schema_version", "process_lifetime", "runtime_library_path"),
        context="LiveGraph worker capabilities",
    )
    if value["schema_version"] != WORKER_SCHEMA:
        raise ContractError("LiveGraph worker capability schema is incompatible")
    return {
        "process_lifetime": nonempty_string(
            value["process_lifetime"], "worker.process_lifetime"
        ),
        "runtime_library_path": str(
            Path(nonempty_string(value["runtime_library_path"], "worker.runtime_library_path")).resolve()
        ),
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def file_artifact(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file() or path.is_symlink():
        raise ContractError(f"adapter artifact is not one regular file: {path}")
    return {
        "path": str(resolved),
        "size_bytes": resolved.stat().st_size,
        "sha256": sha256_file(resolved),
    }


def file_identity(path: Path, context: str) -> dict[str, int]:
    if path.is_symlink():
        raise ContractError(f"{context} must not be a symbolic link")
    try:
        value = path.stat()
    except OSError as exc:
        raise ContractError(f"cannot stat {context}: {exc}") from exc
    if not path.is_file():
        raise ContractError(f"{context} is not one regular file")
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "size_bytes": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
    }


def bound_input_artifact(path: Path, expected_sha: str, identity: dict[str, int]) -> dict[str, Any]:
    current = file_identity(path, "bound LiveGraph input")
    if current != identity:
        raise ContractError("bound LiveGraph input identity changed during the worker lifecycle")
    return {
        "path": str(path.resolve()),
        "size_bytes": current["size_bytes"],
        "sha256": exact_sha(expected_sha, "bound LiveGraph input SHA-256"),
        "identity_before_and_after": current,
    }


def require_empty_real_directory(path: Path, context: str) -> Path:
    if path.is_symlink():
        raise ContractError(f"{context} must not be a symbolic link")
    resolved = path.resolve()
    if not resolved.is_dir():
        raise ContractError(f"{context} is not an existing directory: {resolved}")
    try:
        entries: list[str] = []
        for entry in resolved.iterdir():
            entries.append(entry.name)
            if len(entries) == 8:
                break
    except OSError as exc:
        raise ContractError(f"cannot inspect {context}: {exc}") from exc
    if entries:
        raise ContractError(f"{context} must be wholly empty: {entries!r}")
    return resolved


def prepare_output_directory(path: Path, store_root: Path) -> Path:
    if path.is_symlink():
        raise ContractError("LiveGraph adapter output must not be a symbolic link")
    output = path.resolve()
    if output == store_root or store_root in output.parents or output in store_root.parents:
        raise ContractError("LiveGraph adapter output and store roots must not overlap")
    if output.exists():
        return require_empty_real_directory(output, "LiveGraph adapter output directory")
    output.mkdir(parents=True)
    return output


def resolve_temp_root(
    args: argparse.Namespace,
    request: dict[str, Any],
    store_root: Path,
    output_dir: Path,
) -> Path:
    if request["execution_mode"] != "formal":
        if args.temp_base is not None or args.temp_label is not None:
            raise ContractError("fixture LiveGraph runs must not claim a formal materialized temp root")
        temp = output_dir / "tmp"
        temp.mkdir()
        return require_empty_real_directory(temp, "fixture LiveGraph TMPDIR")
    if args.temp_base is None or not args.temp_label:
        raise ContractError("formal LiveGraph requires --temp-base and --temp-label")
    if args.temp_base.is_symlink():
        raise ContractError("formal LiveGraph temp base must not be a symbolic link")
    base = args.temp_base.resolve()
    if not base.is_dir():
        raise ContractError("formal LiveGraph temp base is not an existing directory")
    if Path(args.temp_label).name != args.temp_label or args.temp_label in (".", ".."):
        raise ContractError("formal LiveGraph temp label is invalid")
    repeat_index = request["repeat_index"]
    prefix = f"livegraph-store-{args.store_label}-"
    suffix = f"-r{repeat_index:02d}"
    name = store_root.name
    if not name.startswith(prefix) or not name.endswith(suffix):
        raise ContractError("formal LiveGraph store root does not follow the orchestrator materialization contract")
    token = name[len(prefix) : -len(suffix)]
    if not re.fullmatch(r"[0-9a-f]{12}", token):
        raise ContractError("formal LiveGraph materialized root token is malformed")
    temp = base / f"livegraph-temp-{args.temp_label}-{token}{suffix}"
    if temp.parent != base:
        raise ContractError("formal LiveGraph TMPDIR escaped its declared base")
    temp = require_empty_real_directory(temp, "formal LiveGraph TMPDIR")
    if temp == store_root or temp in store_root.parents or store_root in temp.parents:
        raise ContractError("formal LiveGraph store and TMPDIR roots overlap")
    return temp


def sanitized_worker_environment(
    runtime_libraries: list[Path], temp_root: Path
) -> tuple[dict[str, str], dict[str, Any]]:
    if "LD_PRELOAD" in os.environ:
        raise ContractError("LiveGraph adapter rejects inherited LD_PRELOAD")
    if len(runtime_libraries) != 1 or runtime_libraries[0].name != "liblivegraph.so":
        raise ContractError("LiveGraph worker environment requires one frozen liblivegraph.so")
    library_parent = runtime_libraries[0].resolve().parent
    discarded = sorted(key for key in os.environ if key.startswith("LD_"))
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "LD_LIBRARY_PATH": str(library_parent),
        "TMPDIR": str(temp_root),
        "TMP": str(temp_root),
        "TEMP": str(temp_root),
    }
    audit = {
        "policy": "minimal-fixed-env-unique-frozen-lib-parent-v1",
        "inherited_ld_preload_present": False,
        "discarded_loader_variables": discarded,
        "injected": environment,
    }
    return environment, audit


def formal_receipts(
    args: argparse.Namespace, request: dict[str, Any]
) -> dict[str, Any] | None:
    receipt_names = FORMAL_GATE_OPTIONS[:-2] + ("dataset_seal", "dataset_seal_sha256")
    supplied = {name: getattr(args, name, None) for name in receipt_names}
    if request["execution_mode"] != "formal":
        if any(value is not None for value in supplied.values()):
            raise ContractError("fixture LiveGraph runs must not claim formal build/P02B receipts")
        return None
    missing = sorted(name for name, value in supplied.items() if value is None)
    if missing:
        raise ContractError(f"formal LiveGraph receipt options are incomplete: {missing}")
    if args.p02b_max_age_seconds != formal_gate.P02B_MAX_AGE_SECONDS:
        raise ContractError(
            f"formal LiveGraph P02B max age must be {formal_gate.P02B_MAX_AGE_SECONDS} seconds"
        )
    system = {
        "binary": dict(request["binary"]),
        "runtime_libraries": [dict(item) for item in request["runtime_libraries"]],
    }
    build = formal_gate.validate_build_receipt(
        args.build_receipt.resolve(), args.build_receipt_sha256, system
    )
    admission_args = {
        "--p02b-result": str(args.p02b_result.resolve()),
        "--p02b-result-sha256": args.p02b_result_sha256,
        "--p02b-validator": str(args.p02b_validator.resolve()),
        "--p02b-validator-sha256": args.p02b_validator_sha256,
        "--p02b-max-age-seconds": str(args.p02b_max_age_seconds),
    }
    p02b = formal_gate.validate_p02b(
        admission_args,
        expected_repo_root=Path(build["integration"]["root"]),
        expected_repo_head=build["integration"]["head"],
        expected_binary_sha256=request["binary"]["sha256"],
    )
    seal_path = args.dataset_seal.resolve()
    if not seal_path.is_file() or seal_path.is_symlink():
        raise ContractError("formal LiveGraph dataset seal is not one regular file")
    seal_sha = exact_sha(args.dataset_seal_sha256, "formal dataset seal SHA-256")
    if sha256_file(seal_path) != seal_sha:
        raise ContractError("formal LiveGraph dataset seal SHA-256 mismatch")
    seal_keys = ("schema_version", "state", "dataset")
    seal = require_keys(
        read_json(seal_path, "formal LiveGraph dataset seal"),
        required=seal_keys, allowed=seal_keys, context="formal LiveGraph dataset seal",
    )
    dataset_binding = require_keys(
        seal["dataset"],
        required=("path", "sha256", "identity"),
        allowed=("path", "sha256", "identity"),
        context="formal LiveGraph sealed dataset",
    )
    identity = require_keys(
        dataset_binding["identity"],
        required=("device", "inode", "size_bytes", "mtime_ns", "ctime_ns"),
        allowed=("device", "inode", "size_bytes", "mtime_ns", "ctime_ns"),
        context="formal LiveGraph sealed dataset identity",
    )
    dataset_path = Path(str(request["dataset"]["path"])).resolve()
    if (
        seal["schema_version"] != "p10-livegraph-dataset-seal-v1"
        or seal["state"] != "PASS"
        or Path(str(dataset_binding["path"])).resolve() != dataset_path
        or dataset_binding["sha256"] != request["dataset"]["sha256"]
        or identity != file_identity(dataset_path, "formal LiveGraph sealed dataset")
    ):
        raise ContractError("formal LiveGraph dataset seal differs from request/current inode")
    return {
        "build": build,
        "p02b": p02b,
        "dataset_seal": {
            "path": str(seal_path), "sha256": seal_sha,
            "size_bytes": seal_path.stat().st_size, "content": seal,
        },
    }


class StageJournal:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.sequence = 0

    def event(self, stage: str, **values: Any) -> None:
        self.sequence += 1
        row = {
            "schema_version": STAGE_SCHEMA,
            "sequence": self.sequence,
            "stage": stage,
            "at_utc": utc_now(),
            "monotonic_ns": time.monotonic_ns(),
            **values,
        }
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        try:
            payload = (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("short write to LiveGraph stage journal")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def process_start_ticks(pid: int) -> int:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ContractError(f"cannot read LiveGraph worker PID identity: {exc}") from exc
    close = text.rfind(")")
    fields = text[close + 2 :].split() if close >= 0 else []
    if len(fields) <= 19:
        raise ContractError("LiveGraph worker /proc stat is malformed")
    try:
        return int(fields[19])
    except ValueError as exc:
        raise ContractError("LiveGraph worker start ticks are malformed") from exc


def same_process_alive(pid: int, start_ticks: int) -> bool:
    try:
        return process_start_ticks(pid) == start_ticks
    except FileNotFoundError:
        return False
    except ContractError:
        # An existing but unreadable/malformed proc identity is potentially live.
        return True


def process_group_members(pgrp: int) -> set[int]:
    members: set[int] = set()
    try:
        entries = list(Path("/proc").iterdir())
    except OSError as exc:
        raise ContractError(f"cannot enumerate /proc for LiveGraph worker cleanup: {exc}") from exc
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / "stat").read_text(encoding="utf-8")
            close = text.rfind(")")
            fields = text[close + 2 :].split() if close >= 0 else []
            if len(fields) <= 2:
                raise ValueError("malformed /proc stat")
            if int(fields[2]) == pgrp:
                members.add(int(entry.name))
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, ValueError) as exc:
            raise ContractError(
                f"cannot authenticate process-group membership for PID {entry.name}: {exc}"
            ) from exc
    return members


def terminate_worker_group(process: subprocess.Popen[Any]) -> None:
    """Best-effort TERM/KILL of the whole detached PG, always reaping its leader."""

    pgid = process.pid
    def group_may_be_live() -> bool:
        try:
            return bool(process_group_members(pgid))
        except ContractError:
            return True

    if group_may_be_live():
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 2.0
    while group_may_be_live() and time.monotonic() < deadline:
        time.sleep(0.02)
    if group_may_be_live():
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
    deadline = time.monotonic() + 2.0
    while group_may_be_live() and time.monotonic() < deadline:
        time.sleep(0.02)
    if group_may_be_live():
        raise ContractError("LiveGraph worker process group could not be fully reaped")


def run_worker(
    command: list[str],
    environment: dict[str, str],
    temp_root: Path,
    output_dir: Path,
    journal: StageJournal,
) -> tuple[int, dict[str, Any]]:
    stdout_path = output_dir / "livegraph-worker.stdout.log"
    stderr_path = output_dir / "livegraph-worker.stderr.log"
    started_at = utc_now()
    started_monotonic = time.monotonic_ns()
    process: subprocess.Popen[Any] | None = None
    previous: dict[int, Any] = {}
    interrupted_signal: int | None = None
    interrupt_deadline: float | None = None
    start_ticks: int | None = None
    process_group_leaked = False
    start_path = output_dir / "worker-start.json"

    def forward(signum: int, _frame: object) -> None:
        nonlocal interrupt_deadline, interrupted_signal
        interrupted_signal = signum
        interrupt_deadline = time.monotonic() + 4
        # Install this before Popen: once a detached child can exist, no signal
        # window may retain the default action and strand that child.
        if process is not None:
            try:
                os.killpg(process.pid, signum)
            except OSError:
                pass
        try:
            journal.event(
                "adapter-signal",
                signal=signum,
                worker_pid=None if process is None else process.pid,
            )
        except OSError:
            pass

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, forward)
    try:
        with stdout_path.open("xb") as stdout_handle, stderr_path.open("xb") as stderr_handle:
            if interrupted_signal is not None:
                raise ContractError(
                    f"LiveGraph adapter received signal {interrupted_signal} before worker launch"
                )
            process = subprocess.Popen(
                command,
                cwd=str(temp_root),
                env=environment,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,
            )
            if interrupted_signal is not None:
                try:
                    os.killpg(process.pid, interrupted_signal)
                except OSError:
                    pass
            start_ticks = process_start_ticks(process.pid)
            start_receipt = {
                "schema_version": PID_SCHEMA,
                "state": "RUNNING",
                "pid": process.pid,
                "proc_start_ticks": start_ticks,
                "process_group_id": process.pid,
                "started_at_utc": started_at,
                "started_monotonic_ns": started_monotonic,
                "argv": command,
                "cwd": str(temp_root),
            }
            atomic_json(start_path, start_receipt)
            journal.event("worker-started", pid=process.pid, proc_start_ticks=start_ticks)

            while True:
                try:
                    returncode = process.wait(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    if interrupt_deadline is not None and time.monotonic() >= interrupt_deadline:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        returncode = process.wait(timeout=2)
                        break
            process_group_leaked = bool(process_group_members(process.pid))
    finally:
        try:
            if process is not None:
                needs_cleanup = process.poll() is None
                if not needs_cleanup:
                    try:
                        needs_cleanup = bool(process_group_members(process.pid))
                    except ContractError:
                        needs_cleanup = True
                if needs_cleanup:
                    terminate_worker_group(process)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)

    assert process is not None and start_ticks is not None
    if process_group_leaked:
        raise ContractError("LiveGraph worker left descendants in its detached process group")

    ended_at = utc_now()
    ended_monotonic = time.monotonic_ns()
    if same_process_alive(process.pid, start_ticks) or process_group_members(process.pid):
        terminate_worker_group(process)
        raise ContractError("LiveGraph worker process group remained alive after wait/reap")
    exit_receipt = {
        "schema_version": PID_SCHEMA,
        "state": "EXITED",
        "pid": process.pid,
        "proc_start_ticks": start_ticks,
        "process_group_id": process.pid,
        "started_at_utc": started_at,
        "ended_at_utc": ended_at,
        "started_monotonic_ns": started_monotonic,
        "ended_monotonic_ns": ended_monotonic,
        "returncode": returncode,
        "same_process_alive_after_wait": False,
        "process_group_members_after_wait": [],
    }
    exit_path = output_dir / "worker-exit.json"
    atomic_json(exit_path, exit_receipt)
    journal.event("worker-exited", pid=process.pid, returncode=returncode)
    if interrupted_signal is not None:
        raise ContractError(f"LiveGraph adapter received signal {interrupted_signal}")
    return returncode, {
        "start": file_artifact(start_path),
        "exit": file_artifact(exit_path),
        "stdout": file_artifact(stdout_path),
        "stderr": file_artifact(stderr_path),
        "identity": exit_receipt,
    }


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
            "process_lifetime",
            "runtime_library_path",
            "vertex_count",
            "edge_count",
            "truth_query_count",
            "setup",
            "warmup",
            "measured",
        ),
        allowed=(
            "schema_version",
            "process_lifetime",
            "runtime_library_path",
            "vertex_count",
            "edge_count",
            "truth_query_count",
            "setup",
            "warmup",
            "measured",
        ),
        context="LiveGraph worker summary",
    )
    if summary["schema_version"] != WORKER_SCHEMA:
        raise ContractError("LiveGraph worker summary has an incompatible schema")
    if summary["process_lifetime"] != FRESH_IMPORT_PROCESS_LIFETIME:
        raise ContractError("LiveGraph worker did not report the frozen fresh-import lifecycle")
    runtime_path = str(
        Path(nonempty_string(summary["runtime_library_path"], "worker.runtime_library_path")).resolve()
    )
    if runtime_path not in [library["path"] for library in request["runtime_libraries"]]:
        raise ContractError("LiveGraph worker loaded an unbound runtime library")
    integer(summary["vertex_count"], "worker.vertex_count", 1)
    integer(summary["edge_count"], "worker.edge_count", 0)
    if summary["truth_query_count"] != len(truth_rows):
        raise ContractError("LiveGraph worker truth query count changed")
    setup_keys = (
        "started_monotonic_ns",
        "ended_monotonic_ns",
        "wall_ns",
        "user_cpu_ns",
        "system_cpu_ns",
        "store_logical_bytes",
        "store_allocated_bytes",
        "block_logical_bytes",
        "block_allocated_bytes",
        "wal_logical_bytes",
        "wal_allocated_bytes",
    )
    setup = require_keys(
        summary["setup"], required=setup_keys, allowed=setup_keys, context="worker.setup"
    )
    setup_start = integer(setup["started_monotonic_ns"], "worker.setup.start", 0)
    setup_end = integer(setup["ended_monotonic_ns"], "worker.setup.end", setup_start + 1)
    setup_wall = integer(setup["wall_ns"], "worker.setup.wall", 1)
    if setup_wall != setup_end - setup_start:
        raise ContractError("worker.setup wall differs from its CLOCK_MONOTONIC boundary")
    integer(setup["user_cpu_ns"], "worker.setup.user_cpu_ns", 0)
    integer(setup["system_cpu_ns"], "worker.setup.system_cpu_ns", 0)
    store_logical = integer(setup["store_logical_bytes"], "worker.setup.store_logical_bytes", 1)
    store_allocated = integer(
        setup["store_allocated_bytes"], "worker.setup.store_allocated_bytes", 0
    )
    block_logical = integer(setup["block_logical_bytes"], "worker.setup.block_logical_bytes", 1)
    block_allocated = integer(
        setup["block_allocated_bytes"], "worker.setup.block_allocated_bytes", 0
    )
    wal_logical = integer(setup["wal_logical_bytes"], "worker.setup.wal_logical_bytes", 1)
    wal_allocated = integer(setup["wal_allocated_bytes"], "worker.setup.wal_allocated_bytes", 0)
    if store_logical != block_logical + wal_logical:
        raise ContractError("worker.setup store logical bytes do not equal block + WAL")
    if store_allocated != block_allocated + wal_allocated:
        raise ContractError("worker.setup store allocated bytes do not equal block + WAL")
    observations = load_observations(output_dir / "query-observations.tsv")
    phase_passes = {
        "warmup": request["timing"]["warmup_passes"],
        "measured": request["timing"]["measured_passes"],
    }
    expected_total = len(truth_rows) * sum(phase_passes.values())
    if len(observations) != expected_total:
        raise ContractError(f"LiveGraph worker wrote {len(observations)} observations, expected {expected_total}")

    position = 0
    observed_latencies: dict[str, list[int]] = {"warmup": [], "measured": []}
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
                observed_latencies[phase].append(latency_ns)
                actual = (row["actual_count"], row["actual_sum_hash"], row["actual_xor_hash"])
                expected_digest = (str(truth["count"]), str(truth["sum_hash"]), str(truth["xor_hash"]))
                if actual != expected_digest:
                    raise ContractError(f"LiveGraph truth mismatch at query_index={truth['query_index']}")

    for phase, passes in phase_passes.items():
        phase_keys = (
            "passes",
            "started_monotonic_ns",
            "ended_monotonic_ns",
            "elapsed_ns",
            "total_query_latency_ns",
            "completed_queries",
            "timeout_queries",
            "mismatch_queries",
            "qps",
            "latency_p50_ns",
            "latency_p95_ns",
            "latency_p99_ns",
        )
        phase_summary = require_keys(
            summary[phase],
            required=phase_keys,
            allowed=phase_keys,
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
        latencies = observed_latencies[phase]
        expected_queries = passes * len(truth_rows)
        exact_phase = {
            "completed_queries": expected_queries,
            "timeout_queries": 0,
            "mismatch_queries": 0,
            "latency_p50_ns": nearest_rank(latencies, 0.50),
            "latency_p95_ns": nearest_rank(latencies, 0.95),
            "latency_p99_ns": nearest_rank(latencies, 0.99),
        }
        for key, expected in exact_phase.items():
            if phase_summary.get(key) != expected:
                raise ContractError(f"worker.{phase}.{key}: {phase_summary.get(key)!r} != {expected!r}")
        try:
            qps = float(phase_summary["qps"])
        except (TypeError, ValueError) as exc:
            raise ContractError(f"worker.{phase}.qps is not numeric") from exc
        expected_qps = expected_queries / (elapsed / 1_000_000_000)
        if not math.isfinite(qps) or not math.isclose(qps, expected_qps, rel_tol=1e-9):
            raise ContractError(f"worker.{phase}.qps differs from the query-only phase boundary")
    if setup_end > summary["warmup"]["started_monotonic_ns"]:
        raise ContractError("LiveGraph fresh import overlaps warmup")
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
        "process_lifetime": request["process_lifetime"],
        "truth_sha256": request["truth"]["sha256"],
        "sequence_digest_algorithm": SEQUENCE_DIGEST_ALGORITHM,
        "timing_boundary": TIMING_BOUNDARY,
        "clock": CLOCK_NAME,
        "concurrency": 1,
        "per_query_timeout_ms": request["timing"]["per_query_timeout_ms"],
        "warmup": phases["warmup"],
        "measured": phases["measured"],
        "setup": {
            "policy": FRESH_IMPORT_PROCESS_LIFETIME,
            "kind": "fresh-import",
            "started_monotonic_ns": worker_summary["setup"]["started_monotonic_ns"],
            "ended_monotonic_ns": worker_summary["setup"]["ended_monotonic_ns"],
            "wall_ns": worker_summary["setup"]["wall_ns"],
            "user_cpu_ns": worker_summary["setup"]["user_cpu_ns"],
            "system_cpu_ns": worker_summary["setup"]["system_cpu_ns"],
            "store_logical_bytes": worker_summary["setup"]["store_logical_bytes"],
            "store_allocated_bytes": worker_summary["setup"]["store_allocated_bytes"],
            "binary_sha256": request["binary"]["sha256"],
            "dataset_sha256": request["dataset"]["sha256"],
            "truth_sha256": request["truth"]["sha256"],
            "runtime_libraries": request["runtime_libraries"],
        },
    }
    atomic_json(output_dir / "adapter-result.json", result)


def validate_final_store(store_root: Path, block_path: Path, wal_path: Path) -> None:
    try:
        entries = list(store_root.iterdir())
    except OSError as exc:
        raise ContractError(f"cannot inspect final LiveGraph store: {exc}") from exc
    if {entry.name for entry in entries} != {block_path.name, wal_path.name}:
        raise ContractError("LiveGraph worker wrote files outside the frozen block/WAL store contract")
    for path in (block_path, wal_path):
        if path.is_symlink() or not path.is_file() or path.resolve().parent != store_root:
            raise ContractError("LiveGraph final block/WAL is not one regular in-root file")


def node_identity(value: os.stat_result, *, kind: str) -> dict[str, Any]:
    if kind == "regular-file":
        valid_kind = stat.S_ISREG(value.st_mode)
    elif kind == "directory":
        valid_kind = stat.S_ISDIR(value.st_mode)
    else:
        raise ContractError(f"unknown LiveGraph node identity kind: {kind}")
    if not valid_kind:
        raise ContractError(f"LiveGraph store node is not a {kind}")
    return {
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
        "kind": kind,
        "mode_bits": stat.S_IMODE(value.st_mode),
        "uid": int(value.st_uid),
        "gid": int(value.st_gid),
        "nlink": int(value.st_nlink),
        "size_bytes": int(value.st_size),
        "mtime_ns": int(value.st_mtime_ns),
        "ctime_ns": int(value.st_ctime_ns),
    }


def freeze_formal_store(
    store_root: Path, block_path: Path, wal_path: Path
) -> dict[str, Any]:
    """Make worker outputs read-only and record terminal stat identity without payload I/O."""

    validate_final_store(store_root, block_path, wal_path)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(store_root, directory_flags)
    file_fds: dict[str, int] = {}
    try:
        for role, path in (("block", block_path), ("wal", wal_path)):
            descriptor = os.open(path.name, file_flags, dir_fd=directory_fd)
            file_fds[role] = descriptor
            value = os.fstat(descriptor)
            if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
                raise ContractError(f"LiveGraph {role} must be one unlinked regular file")
            if value.st_uid != os.geteuid() or value.st_gid != os.getegid():
                raise ContractError(f"LiveGraph {role} owner differs from the adapter")
            os.fchmod(descriptor, 0o444)
        os.fchmod(directory_fd, 0o555)
        if sorted(os.listdir(directory_fd)) != sorted((block_path.name, wal_path.name)):
            raise ContractError("LiveGraph store entries changed while publishing terminal identity")
        root_identity = node_identity(os.fstat(directory_fd), kind="directory")
        if (
            root_identity["mode_bits"] != 0o555
            or root_identity["uid"] != os.geteuid()
            or root_identity["gid"] != os.getegid()
        ):
            raise ContractError("LiveGraph formal store root mode/owner identity drift")
        files: dict[str, Any] = {}
        for role, path in (("block", block_path), ("wal", wal_path)):
            identity = node_identity(os.fstat(file_fds[role]), kind="regular-file")
            if identity["mode_bits"] != 0o444 or identity["nlink"] != 1:
                raise ContractError(f"LiveGraph {role} terminal identity is not read-only/single-link")
            files[role] = {
                "role": role,
                "path": str(path.resolve()),
                "content_sha256_mode": "deferred-post-p31-full-sha256-v1",
                "identity_after_worker_exit": identity,
            }
        return {
            "root": str(store_root.resolve()),
            "initial_state": "wholly-empty",
            "final_entries": [block_path.name, wal_path.name],
            "terminal_policy": "worker-exited-posix-read-only-stat-only-v1",
            "read_only_policy": "root-0555-files-0444-v1",
            "root_identity_after_worker_exit": root_identity,
            **files,
        }
    finally:
        for descriptor in file_fds.values():
            os.close(descriptor)
        os.close(directory_fd)


def publish_provenance(
    *,
    args: argparse.Namespace,
    output_dir: Path,
    request_path: Path,
    request: dict[str, Any],
    binary: Path,
    dataset: Path,
    runtime_libraries: list[Path],
    store_root: Path,
    temp_root: Path,
    block_path: Path,
    wal_path: Path,
    command: list[str],
    environment_audit: dict[str, Any],
    lifecycle: dict[str, Any],
    receipts: dict[str, Any] | None,
    input_identities: dict[str, dict[str, int]],
    formal_store: dict[str, Any] | None,
) -> None:
    if receipts is not None and not isinstance(formal_store, dict):
        raise ContractError("formal LiveGraph provenance requires terminal store identity")
    if receipts is None and formal_store is not None:
        raise ContractError("fixture LiveGraph provenance cannot claim a formal terminal store")
    build_binding = None
    p02b_binding = None
    if receipts is not None:
        build = receipts["build"]
        build_binding = {
            "receipt": build["receipt"],
            "marker": build["marker"],
            "integration": build["integration"],
            "livegraph_source": build["livegraph_source"],
            "source_library": build["source_library"],
            "binary": build["binary"],
            "liblivegraph": build["liblivegraph"],
        }
        p02b_binding = receipts["p02b"]
    artifacts = {
        "request": file_artifact(request_path),
        "adapter": file_artifact(Path(__file__)),
        "binary": bound_input_artifact(
            binary, request["binary"]["sha256"], input_identities["binary"]
        ),
        "dataset": bound_input_artifact(
            dataset, request["dataset"]["sha256"], input_identities["dataset"]
        ),
        "truth": bound_input_artifact(
            Path(request["truth"]["path"]),
            request["truth"]["sha256"],
            input_identities["truth"],
        ),
        "runtime_library": bound_input_artifact(
            runtime_libraries[0], request["runtime_libraries"][0]["sha256"],
            input_identities["runtime_library"],
        ),
        "runtime_capability": file_artifact(output_dir / "runtime-capability.json"),
        "observations": file_artifact(output_dir / "query-observations.tsv"),
        "phase_events": file_artifact(output_dir / "phase-events.jsonl"),
        "worker_summary": file_artifact(output_dir / "livegraph-worker-summary.json"),
        "adapter_result": file_artifact(output_dir / "adapter-result.json"),
    }
    provenance = {
        "schema_version": PROVENANCE_SCHEMA if receipts is not None else FIXTURE_PROVENANCE_SCHEMA,
        "execution_mode": request["execution_mode"],
        "system_id": "livegraph",
        "suite_id": request["suite_id"],
        "run_id": request["run_id"],
        "repeat_index": request["repeat_index"],
        "process_lifetime": PROCESS_LIFETIME,
        "completed_at_utc": utc_now(),
        "artifacts": artifacts,
        "store": formal_store if receipts is not None else {
            "root": str(store_root),
            "initial_state": "wholly-empty",
            "final_entries": [block_path.name, wal_path.name],
            "block": file_artifact(block_path),
            "wal": file_artifact(wal_path),
        },
        "temp": {
            "root": str(temp_root),
            "environment_variable": "TMPDIR",
            "actual_value": environment_audit["injected"]["TMPDIR"],
        },
        "environment": environment_audit,
        "command": {"argv": command, "cwd": str(temp_root)},
        "worker_lifecycle": lifecycle,
        "formal_build": build_binding,
        "p02b_admission": p02b_binding,
        "dataset_seal": None if receipts is None else receipts["dataset_seal"],
        "formal_gate_options": {
            "build_receipt": str(args.build_receipt.resolve()) if args.build_receipt else None,
            "p02b_result": str(args.p02b_result.resolve()) if args.p02b_result else None,
            "p02b_max_age_seconds": args.p02b_max_age_seconds,
            "dataset_seal": str(args.dataset_seal.resolve()) if args.dataset_seal else None,
            "temp_base": str(args.temp_base.resolve()) if args.temp_base else None,
            "temp_label": args.temp_label,
        },
    }
    atomic_json(output_dir / "adapter-provenance.json", provenance)


def main() -> int:
    args = parse_args()
    output_dir: Path | None = None
    journal: StageJournal | None = None
    try:
        request_path = args.request.resolve()
        request, truth_rows, binary, dataset, store_root, runtime_libraries = validate_request(
            request_path, args.store_label
        )
        input_identities = {
            "binary": file_identity(binary, "LiveGraph worker binary"),
            "dataset": file_identity(dataset, "LiveGraph dataset"),
            "truth": file_identity(Path(request["truth"]["path"]), "LiveGraph truth"),
            "runtime_library": file_identity(
                runtime_libraries[0], "LiveGraph runtime library"
            ),
        }
        block_path = safe_store_file(store_root, args.block_name, "--block-name")
        wal_path = safe_store_file(store_root, args.wal_name, "--wal-name")
        if block_path == wal_path:
            raise ContractError("LiveGraph block and WAL paths must differ")
        require_empty_real_directory(store_root, "LiveGraph fresh store root")
        receipts = formal_receipts(args, request)
        output_dir = prepare_output_directory(args.output_dir, store_root)
        temp_root = resolve_temp_root(args, request, store_root, output_dir)
        environment, environment_audit = sanitized_worker_environment(runtime_libraries, temp_root)
        capability = worker_capability(binary, environment)
        if capability["process_lifetime"] != FRESH_IMPORT_CAPABILITY:
            raise ContractError("LiveGraph worker did not declare the frozen fresh-import lifecycle")
        if Path(capability["runtime_library_path"]) != runtime_libraries[0]:
            raise ContractError("LiveGraph worker loaded a runtime library outside the frozen request")
        atomic_json(output_dir / "runtime-capability.json", capability)
        journal = StageJournal(output_dir / "adapter-stage-events.jsonl")
        journal.event(
            "preflight-passed",
            execution_mode=request["execution_mode"],
            store_root=str(store_root),
            temp_root=str(temp_root),
        )
        command = [
            str(binary),
            "--store-mode",
            "fresh-import",
            "--edges",
            str(dataset),
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
        returncode, lifecycle = run_worker(
            command, environment, temp_root, output_dir, journal
        )
        if returncode != 0:
            raise ContractError(f"LiveGraph worker exited {returncode}")
        validate_final_store(store_root, block_path, wal_path)
        observations, worker_summary = validate_worker_output(output_dir, request, truth_rows)
        journal.event("worker-output-validated")
        publish_result(output_dir, request, truth_rows, observations, worker_summary)
        journal.event("adapter-result-published")
        formal_store = None
        if receipts is not None:
            formal_store = freeze_formal_store(store_root, block_path, wal_path)
        publish_provenance(
            args=args,
            output_dir=output_dir,
            request_path=request_path,
            request=request,
            binary=binary,
            dataset=dataset,
            runtime_libraries=runtime_libraries,
            store_root=store_root,
            temp_root=temp_root,
            block_path=block_path,
            wal_path=wal_path,
            command=command,
            environment_audit=environment_audit,
            lifecycle=lifecycle,
            receipts=receipts,
            input_identities=input_identities,
            formal_store=formal_store,
        )
        journal.event("adapter-complete")
        return 0
    except (ContractError, OSError, subprocess.SubprocessError) as exc:
        if journal is not None:
            try:
                journal.event("adapter-failed", error_type=type(exc).__name__, error=str(exc))
            except OSError:
                pass
        if output_dir is not None and output_dir.is_dir():
            failure = output_dir / "adapter-failure.json"
            if not failure.exists():
                try:
                    atomic_json(
                        failure,
                        {
                            "state": "FAILED",
                            "failed_at_utc": utc_now(),
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "performance_eligible": False,
                        },
                    )
                except OSError:
                    pass
        print(f"livegraph_adapter: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
