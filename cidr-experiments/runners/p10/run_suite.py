#!/usr/bin/env python3
"""Fail-closed P10/P11 cross-system orchestrator.

One adapter process executes the warmup and measured phases for each repeat.
The process is wrapped by P31, then its typed-neighbor observations are checked
against the frozen truth before any suite-level DONE marker is published.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import json
import os
import secrets
import shutil
import socket
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import fcntl
except ImportError:  # pragma: no cover - formal execution is Linux-only
    fcntl = None  # type: ignore[assignment]

from p10_contract import (
    CLOCK_NAME,
    CONTRACT_VERSION,
    FRESH_IMPORT_PROCESS_LIFETIME,
    FROZEN_SYSTEM_GROUPS,
    GROUP_POLICY,
    INTERFACE_SCOPE,
    REQUEST_SCHEMA_VERSION,
    SEQUENCE_DIGEST_ALGORITHM,
    TIMING_BOUNDARY,
    ContractError,
    audit_neo4j_runtime_store,
    atomic_json,
    claim_empty_directory,
    load_suite_manifest,
    publish_livegraph_post_p31_store_seal,
    read_json,
    read_p31_summary,
    sha256_file,
    validate_adapter_outputs,
    validate_adapter_p31_binding,
    validate_livegraph_p31_binding,
    validate_neo4j_p31_binding,
    validate_neo4j_store_audit,
    validate_neo4j_store_audit_pair,
    validate_nebulagraph_p31_binding,
)
from adapters.launch_neo4j_runtime import (
    IMAGE_REPO_DIGEST as NEO4J_IMAGE_REPO_DIGEST,
    current_host as neo4j_current_host,
    validate_launch_receipt,
    validate_launch_receipts_independent,
    validate_stop_receipt,
)


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
DEFAULT_P31 = REPO_ROOT / "cidr-experiments/runners/p31/run_with_resources.sh"
DEFAULT_BATCH_GATE = REPO_ROOT / "cidr-experiments/runners/batch_gate_v2.py"
P31_ADAPTER_WRAPPER = SCRIPT_DIR / "run_adapter_with_p31.sh"
NEO4J_LIFECYCLE = SCRIPT_DIR / "adapters" / "launch_neo4j_runtime.py"
NEBULAGRAPH_LIFECYCLE = (
    SCRIPT_DIR / "adapters" / "nebulagraph" / "formal_cluster.py"
)
P02B_RESULT_SCHEMA = "p02b-sf10-sentinel-result-v2"
P02B_GATE_CONTRACT_SCHEMA = "p02b-gate-contract-v1"
P02B_GATE_METHOD = "quantization-aware-tail-v1"
P02B_GATE_CONTRACT_KEYS = {
    "schema_version",
    "method",
    "quantile",
    "tail_bounds_us",
    "sigma_multiplier",
    "qps_cv_max",
    "mean_storage_latency_cv_max",
    "require_zero_overflow",
    "stability_result",
    "tools",
    "contract_sha256",
}
P02B_GATE_TOOL_FILENAMES = {
    "extract_run_metrics": "extract_run_metrics.py",
    "calculate_stability": "calculate_stability.py",
    "validate_sentinel_result": "validate_sentinel_result.py",
}
BATCH_LEASE_RECEIPT_KEYS = {
    "schema_version",
    "state",
    "consumer",
    "lease",
    "lease_sha256",
    "issued_at_utc",
    "expires_at_utc",
    "remaining_seconds",
    "host",
    "repo_head",
    "binary_sha256",
    "gate_contract",
    "gate_contract_sha256",
}


def _nebulagraph_cluster_module() -> Any:
    """Load the standalone controller only for an actual formal NebulaGraph repeat."""

    module_dir = str(NEBULAGRAPH_LIFECYCLE.parent)
    if module_dir not in sys.path:
        sys.path.insert(0, module_dir)
    return importlib.import_module("formal_cluster")

REPEAT_COLUMNS = [
    "system_id",
    "display_name",
    "group",
    "system_version",
    "process_lifetime",
    "repeat_index",
    "query_count",
    "warmup_passes",
    "measured_passes",
    "warmup_s",
    "measurement_s",
    "import_wall_s",
    "import_user_cpu_s",
    "import_system_cpu_s",
    "import_store_logical_bytes",
    "import_store_allocated_bytes",
    "completed_queries",
    "timeout_queries",
    "mismatch_queries",
    "qps",
    "latency_p50_us",
    "latency_p95_us",
    "latency_p99_us",
    "expected_digest_sha256",
    "actual_digest_sha256",
    "peak_rss_bytes",
    "peak_pss_bytes",
    "process_user_cpu_s",
    "process_sys_cpu_s",
    "process_read_bytes",
    "process_write_bytes",
    "peak_store_total_bytes",
    "peak_temp_bytes",
    "p31_run_dir",
]

SYSTEM_COLUMNS = [
    "system_id",
    "display_name",
    "group",
    "system_version",
    "process_lifetime",
    "repeats",
    "query_count",
    "timeout_queries_total",
    "mismatch_queries_total",
    "median_warmup_s",
    "median_measurement_s",
    "median_import_wall_s",
    "median_import_cpu_s",
    "max_import_store_logical_bytes",
    "max_import_store_allocated_bytes",
    "median_qps",
    "median_latency_p50_us",
    "median_latency_p95_us",
    "median_latency_p99_us",
    "max_peak_rss_bytes",
    "max_peak_pss_bytes",
    "median_process_cpu_s",
    "max_peak_store_total_bytes",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def write_tsv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})
    os.replace(temporary, path)


def verify_clean_ready(path: Path) -> None:
    if not path.is_file():
        raise ContractError(f"formal mode requires an existing clean-ready file: {path}")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ContractError(f"cannot read clean-ready file: {exc}") from exc
    if "readiness_gate=PASS" not in lines:
        raise ContractError("clean-ready file lacks an exact readiness_gate=PASS line")


def artifact_ref(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    return {
        "path": str(resolved),
        "size_bytes": resolved.stat().st_size,
        "sha256": sha256_file(resolved),
    }


def _canonical_json_sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _require_sha256(value: object, context: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ContractError(f"{context} must be a lowercase SHA-256")
    return value


def _validate_current_file_ref(reference: object, context: str) -> Path:
    if not isinstance(reference, dict) or set(reference) != {
        "path",
        "size_bytes",
        "sha256",
    }:
        raise ContractError(f"{context} file reference drift")
    raw_path = reference.get("path")
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
        raise ContractError(f"{context} path must be absolute")
    path = Path(raw_path).resolve()
    if not path.is_file():
        raise ContractError(f"{context} is not a current file: {path}")
    if (
        isinstance(reference.get("size_bytes"), bool)
        or not isinstance(reference.get("size_bytes"), int)
        or reference["size_bytes"] != path.stat().st_size
    ):
        raise ContractError(f"{context} size drift")
    if sha256_file(path) != _require_sha256(reference.get("sha256"), f"{context} SHA"):
        raise ContractError(f"{context} SHA-256 drift")
    return path


def _validate_p02b_gate_contract_binding(
    receipt: dict[str, Any], lease_path: Path
) -> dict[str, Any]:
    contract = receipt.get("gate_contract")
    if not isinstance(contract, dict) or set(contract) != P02B_GATE_CONTRACT_KEYS:
        raise ContractError("batch lease P02B gate contract keys drift")
    if (
        contract.get("schema_version") != P02B_GATE_CONTRACT_SCHEMA
        or contract.get("method") != P02B_GATE_METHOD
        or contract.get("quantile") != {"numerator": 99, "denominator": 100}
        or contract.get("tail_bounds_us") != {"lower": 150000, "upper": 250000}
        or contract.get("sigma_multiplier") != 3
        or contract.get("qps_cv_max") != 0.07
        or contract.get("mean_storage_latency_cv_max") != 0.07
        or contract.get("require_zero_overflow") is not True
    ):
        raise ContractError("batch lease P02B gate contract policy drift")
    unsigned_contract = dict(contract)
    embedded_digest = _require_sha256(
        unsigned_contract.pop("contract_sha256"),
        "batch lease P02B gate contract digest",
    )
    if _canonical_json_sha256(unsigned_contract) != embedded_digest:
        raise ContractError("batch lease P02B gate contract canonical digest drift")
    if (
        _require_sha256(
            receipt.get("gate_contract_sha256"),
            "batch lease receipt P02B gate contract digest",
        )
        != embedded_digest
    ):
        raise ContractError("batch lease receipt P02B gate contract digest drift")

    lease = read_json(lease_path.resolve(), "batch lease")
    if lease.get("schema_version") != "cidr-batch-lease-v2":
        raise ContractError("batch lease schema drift")
    p02b = lease.get("p02b")
    if not isinstance(p02b, dict):
        raise ContractError("batch lease lacks a P02B binding")
    if (
        p02b.get("gate_contract") != contract
        or p02b.get("gate_contract_sha256") != embedded_digest
    ):
        raise ContractError("batch lease/receipt P02B gate contract binding drift")

    result_path = _validate_current_file_ref(
        p02b.get("result"), "batch lease P02B result"
    )
    validator_path = _validate_current_file_ref(
        p02b.get("validator"), "batch lease P02B validator"
    )
    stability_path = _validate_current_file_ref(
        contract.get("stability_result"), "batch lease P02B stability result"
    )
    if stability_path != (result_path.parent / "stability-result.json").resolve():
        raise ContractError("batch lease P02B stability-result path drift")

    tools = contract.get("tools")
    if not isinstance(tools, dict) or set(tools) != set(P02B_GATE_TOOL_FILENAMES):
        raise ContractError("batch lease P02B gate tools drift")
    for name, filename in P02B_GATE_TOOL_FILENAMES.items():
        tool_path = _validate_current_file_ref(
            tools.get(name), f"batch lease P02B gate tool {name}"
        )
        if tool_path != (validator_path.parent / filename).resolve():
            raise ContractError(f"batch lease P02B gate tool {name} path drift")

    result = read_json(result_path, "batch lease P02B result")
    if (
        result.get("schema_version") != P02B_RESULT_SCHEMA
        or result.get("state") != "PASS"
        or result.get("fixture_only") is not False
        or result.get("formal_gate_eligible") is not True
        or result.get("downstream_release_eligible") is not True
        or result.get("gate_contract") != contract
    ):
        raise ContractError("batch lease does not bind a formal P02B result-v2 PASS")
    stability = read_json(stability_path, "batch lease P02B stability result")
    if (
        stability.get("schema_version") != "p02b-sentinel-stability-v2"
        or stability.get("state") != "PASS"
        or stability.get("method") != P02B_GATE_METHOD
        or result.get("stability") != stability
    ):
        raise ContractError("batch lease P02B stability evidence drift")
    return contract


def batch_anchor_from_lease(lease_path: Path) -> Path:
    lease = read_json(lease_path.resolve(), "batch lease")
    raw = lease.get("identity", {}).get("binary", {}).get("path")
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        raise ContractError("batch lease omits an absolute anchor binary path")
    binary = Path(raw).resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ContractError("batch lease anchor binary is missing or not executable")
    return binary


def validate_batch_lease(
    lease_path: Path, gate_tool: Path, anchor_binary: Path
) -> tuple[dict[str, Any], list[str]]:
    command = [
        sys.executable,
        "-B",
        str(gate_tool),
        "validate-lease",
        "--lease",
        str(lease_path),
        "--consumer",
        "P10",
        "--repo-root",
        str(REPO_ROOT),
        "--binary",
        str(anchor_binary),
    ]
    completed = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False
    )
    if completed.returncode != 0:
        raise ContractError(
            "batch lease validation failed: {}".format(completed.stderr.strip())
        )
    try:
        receipt = json.loads(completed.stdout)
    except ValueError as exc:
        raise ContractError("batch gate emitted invalid JSON") from exc
    if (
        not isinstance(receipt, dict)
        or set(receipt) != BATCH_LEASE_RECEIPT_KEYS
        or receipt.get("schema_version") != "cidr-batch-lease-admission-v2"
        or receipt.get("state") != "PASS"
        or receipt.get("consumer") != "P10"
        or Path(str(receipt.get("lease", ""))).resolve() != lease_path.resolve()
        or receipt.get("lease_sha256") != sha256_file(lease_path)
        or receipt.get("binary_sha256") != sha256_file(anchor_binary)
    ):
        raise ContractError("batch lease admission receipt drift")
    host = receipt.get("host")
    fingerprint = host.get("fingerprint_sha256") if isinstance(host, dict) else None
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint)
        or not isinstance(receipt.get("repo_head"), str)
        or len(receipt["repo_head"]) not in (40, 64)
        or not isinstance(receipt.get("issued_at_utc"), str)
        or not isinstance(receipt.get("expires_at_utc"), str)
        or isinstance(receipt.get("remaining_seconds"), bool)
        or not isinstance(receipt.get("remaining_seconds"), (int, float))
        or receipt["remaining_seconds"] <= 0
    ):
        raise ContractError("batch lease admission identity/lifetime drift")
    _validate_p02b_gate_contract_binding(receipt, lease_path)
    return receipt, command


def verify_formal_preflight(
    run_root: Path,
    p31_wrapper: Path,
    *,
    batch_lease: Path | None,
    batch_gate_tool: Path | None,
    legacy_v1_clean_ready: Path | None,
    compatibility_clean_ready: Path | None,
) -> dict[str, Any]:
    if p31_wrapper.resolve() != DEFAULT_P31.resolve():
        raise ContractError("formal mode forbids overriding the real P31 wrapper")
    legacy_candidates = [
        path
        for path in (legacy_v1_clean_ready, compatibility_clean_ready)
        if path is not None
    ]
    if len(legacy_candidates) > 1:
        raise ContractError("legacy clean-ready aliases are mutually exclusive")
    batch_count = int(batch_lease is not None) + int(batch_gate_tool is not None)
    if batch_count == 1:
        raise ContractError("--batch-lease and --batch-gate-tool are required together")
    if batch_count and legacy_candidates:
        raise ContractError("formal mode forbids mixing v2 lease and legacy v1 admission")
    if not batch_count and not legacy_candidates:
        raise ContractError(
            "formal mode requires a v2 batch lease or explicit legacy v1 clean-ready"
        )
    if run_root == REPO_ROOT or REPO_ROOT in run_root.parents:
        raise ContractError("formal run-root must be outside the Git worktree so P31 sees a clean repository")
    try:
        status = subprocess.check_output(
            ["git", "-C", str(REPO_ROOT), "status", "--porcelain=v1", "--untracked-files=normal"],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContractError(f"formal mode could not audit Git status: {exc}") from exc
    if status:
        raise ContractError("formal mode requires a clean Git worktree")
    if batch_count:
        assert batch_lease is not None
        assert batch_gate_tool is not None
        lease_path = batch_lease.resolve()
        gate_tool = batch_gate_tool.resolve()
        if gate_tool != DEFAULT_BATCH_GATE.resolve():
            raise ContractError("formal v2 mode forbids overriding the canonical batch gate tool")
        if not lease_path.is_file() or not gate_tool.is_file():
            raise ContractError("formal v2 batch lease/gate tool is missing")
        anchor_binary = batch_anchor_from_lease(lease_path)
        receipt, command = validate_batch_lease(lease_path, gate_tool, anchor_binary)
        return {
            "protocol": "batch-lease-v2",
            "lease": lease_path,
            "gate_tool": gate_tool,
            "anchor_binary": anchor_binary,
            "suite_receipt": receipt,
            "suite_validator_argv": command,
        }
    legacy_path = legacy_candidates[0].resolve()
    verify_clean_ready(legacy_path)
    return {"protocol": "legacy-clean-ready-v1", "clean_ready": legacy_path}


def select_systems(suite: dict[str, Any], ids: list[str], group: str | None) -> list[dict[str, Any]]:
    if ids and group:
        raise ContractError("--system and --group are mutually exclusive")
    known = {system["id"] for system in suite["systems"]}
    if ids:
        if len(ids) != len(set(ids)):
            raise ContractError("duplicate --system selection")
        unknown = sorted(set(ids) - known)
        if unknown:
            raise ContractError(f"unknown --system values: {unknown}")
        selected = [system for system in suite["systems"] if system["id"] in ids]
    elif group:
        selected = [system for system in suite["systems"] if system["group"] == group]
    else:
        selected = list(suite["systems"])
    if not selected:
        raise ContractError("system selection is empty")
    return selected


def select_repeat_indices(suite: dict[str, Any], requested: list[int]) -> list[int]:
    total = suite["protocol"]["repeats"]
    if not requested:
        return list(range(1, total + 1))
    if len(requested) != len(set(requested)):
        raise ContractError("duplicate --repeat-index selection")
    invalid = sorted(index for index in requested if index < 1 or index > total)
    if invalid:
        raise ContractError(f"--repeat-index is outside the frozen 1..{total} range: {invalid}")
    return sorted(requested)


def build_request(
    suite: dict[str, Any],
    system: dict[str, Any],
    repeat_index: int,
    run_id: str,
    mode: str,
) -> dict[str, Any]:
    protocol = suite["protocol"]
    request = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "suite_id": suite["suite_id"],
        "run_id": run_id,
        "execution_mode": mode,
        "system_id": system["id"],
        "group": system["group"],
        "system_version": system["system_version"],
        "interface_scope": INTERFACE_SCOPE,
        "repeat_index": repeat_index,
        "process_lifetime": system["process_lifetime"],
        "binary": {
            "path": system["binary"]["path"],
            "sha256": system["binary"]["sha256"],
        },
        "dataset": {
            "path": suite["dataset"]["path"],
            "sha256": suite["dataset"]["sha256"],
        },
        "runtime_libraries": [dict(library) for library in system["runtime_libraries"]],
        "store_roots": [dict(root) for root in system["store_roots"]],
        "truth": {
            "path": suite["truth"]["path"],
            "sha256": suite["truth"]["sha256"],
            "query_count": suite["truth"]["query_count"],
            "digest_algorithm": suite["truth"]["digest_algorithm"],
        },
        "timing": {
            "timing_boundary": TIMING_BOUNDARY,
            "clock": CLOCK_NAME,
            "cache_policy": suite["protocol"]["cache_policy"],
            "process_reuse_between_phases": True,
            "warmup_passes": protocol["warmup_passes"],
            "measured_passes": protocol["measured_passes"],
            "concurrency": protocol["concurrency"],
            "per_query_timeout_ms": protocol["per_query_timeout_ms"],
            "sequence_digest_algorithm": SEQUENCE_DIGEST_ALGORITHM,
        },
    }
    if system["group"] == "client-server":
        request["external_service"] = {
            "service_lifecycle": system["service_lifecycle"],
            "containers": list(system["containers"]),
            "extra_pids": list(system["extra_pids"]),
            "image_digests": list(system["image_digests"]),
        }
        lifecycle = system.get("active_nebulagraph_lifecycle")
        if lifecycle is not None:
            if system["id"] != "nebulagraph" or mode != "formal":
                raise ContractError(
                    "NebulaGraph lifecycle receipt paths are restricted to formal NebulaGraph"
                )
            request["external_service"]["orchestrated_lifecycle"] = {
                "controller": dict(lifecycle["controller"]),
                "receipts": {
                    name: str(Path(path).resolve())
                    for name, path in lifecycle["receipt_paths"].items()
                },
                "logs_root": str(Path(lifecycle["logs_root"]).resolve()),
            }
    return request


def p31_command(
    *,
    suite: dict[str, Any],
    system: dict[str, Any],
    repeat_index: int,
    repeat_dir: Path,
    request_path: Path,
    adapter_output: Path,
    resolved_manifest: Path,
    p31_wrapper: Path,
    mode: str,
    extra_adapter_args: list[str] | None = None,
    named_inputs: list[str] | None = None,
    admission: dict[str, Any] | None = None,
) -> list[str]:
    performance_eligible = mode == "formal"
    p31_dir = repeat_dir / "p31"
    resources = suite["resources"]
    command = [
        str(P31_ADAPTER_WRAPPER),
        "--p31-wrapper",
        str(p31_wrapper),
        "--run-dir",
        str(p31_dir),
        "--run-id",
        f"{system['id']}-r{repeat_index:02d}",
        "--task-id",
        f"P10-P11-{system['id']}-r{repeat_index:02d}",
        "--performance-eligible",
        str(performance_eligible).lower(),
        "--repo-root",
        str(REPO_ROOT),
        "--device",
        resources["device"],
        "--data-mount",
        resources["data_mount"],
        "--interval",
        str(resources["interval_s"]),
        "--disk-interval",
        str(resources["disk_interval_s"]),
        "--min-samples",
        str(resources["min_samples"]),
    ]
    for root in system["store_roots"]:
        command.extend(("--store", f"{root['label']}={root['path']}"))
    for root in system["temp_roots"]:
        command.extend(("--temp", f"{root['label']}={root['path']}"))
    for container in system["containers"]:
        command.extend(("--container", container))
    for pid in system["extra_pids"]:
        command.extend(("--extra-pid", str(pid)))
    for value in named_inputs or []:
        command.extend(("--input", value))
    if admission is not None and admission.get("protocol") == "batch-lease-v2":
        command.extend(
            (
                "--batch-lease",
                str(admission["lease"]),
                "--batch-gate-tool",
                str(admission["gate_tool"]),
                "--batch-consumer",
                "P10",
                "--batch-anchor-binary",
                str(admission["anchor_binary"]),
            )
        )
    command.extend(
        (
            "--binary",
            system["binary"]["path"],
            "--binary-sha256",
            system["binary"]["sha256"],
            "--dataset",
            suite["dataset"]["path"],
            "--dataset-sha256",
            suite["dataset"]["sha256"],
            "--truth",
            suite["truth"]["path"],
            "--truth-sha256",
            suite["truth"]["sha256"],
            "--query-or-trace",
            suite["truth"]["path"],
            "--query-or-trace-sha256",
            suite["truth"]["sha256"],
            "--config",
            str(resolved_manifest),
            "--config-sha256",
            sha256_file(resolved_manifest),
        )
    )
    if mode == "formal" and system["id"] == "livegraph":
        command.extend(("--dataset-sha256-mode", "declared-no-read-v1"))
    if mode == "fixture":
        command.append("--allow-missing-aux-tools")
    timeout_binary = shutil.which("timeout")
    if timeout_binary is None:
        raise ContractError("GNU timeout is required to bound an adapter process")
    adapter_command = [
        system["adapter"]["path"],
        *system["adapter"]["args"],
        *(extra_adapter_args or []),
        "--request",
        str(request_path),
        "--output-dir",
        str(adapter_output),
    ]
    command.extend(
        (
            "--",
            timeout_binary,
            "--signal=TERM",
            "--kill-after=5s",
            f"{suite['protocol']['adapter_process_timeout_s']}s",
            *adapter_command,
        )
    )
    return command


def exact_adapter_arg(system: dict[str, Any], flag: str) -> str:
    """Return one flag value from a static adapter argv, rejecting ambiguity."""

    args = system.get("adapter", {}).get("args", [])
    if not isinstance(args, list):
        raise ContractError(f"{system.get('id', 'system')} adapter args are malformed")
    positions = [index for index, value in enumerate(args) if value == flag]
    if len(positions) != 1 or positions[0] + 1 >= len(args):
        raise ContractError(f"{system.get('id', 'system')} adapter requires exactly one {flag}")
    value = args[positions[0] + 1]
    if not isinstance(value, str) or not value or value.startswith("--"):
        raise ContractError(f"{system.get('id', 'system')} adapter {flag} value is invalid")
    return value


def livegraph_p31_inputs(system: dict[str, Any]) -> list[str]:
    """Revalidate and bind every small formal gate artifact into P31."""

    from adapters.livegraph import run_sf10_formal as formal_gate

    adapter_args = {
        flag: exact_adapter_arg(system, flag)
        for flag in (
            "--build-receipt", "--build-receipt-sha256", "--p02b-result",
            "--p02b-result-sha256", "--p02b-validator", "--p02b-validator-sha256",
            "--p02b-max-age-seconds",
        )
    }
    build = formal_gate.validate_build_receipt(
        Path(adapter_args["--build-receipt"]),
        adapter_args["--build-receipt-sha256"],
        system,
    )
    p02b = formal_gate.validate_p02b(
        adapter_args,
        expected_repo_root=Path(build["integration"]["root"]),
        expected_repo_head=build["integration"]["head"],
        expected_binary_sha256=system["binary"]["sha256"],
    )
    admission = p02b["admission"]
    references = {
        "livegraph_build_receipt": build["receipt"],
        "livegraph_build_marker": build["marker"],
        "livegraph_source_library": build["source_library"],
        "livegraph_runtime_library": build["liblivegraph"],
        "livegraph_p02b_result": p02b["result"],
        "livegraph_p02b_validator": p02b["validator"],
        "livegraph_p02b_pass_marker": {
            "path": admission["pass_marker"], "sha256": admission["pass_marker_sha256"]
        },
        "livegraph_p02b_provenance": {
            "path": admission["provenance"], "sha256": admission["provenance_sha256"]
        },
    }
    values: list[str] = []
    for label, ref in references.items():
        path = Path(str(ref.get("path", ""))).resolve()
        digest = str(ref.get("sha256", ""))
        if not path.is_file() or len(digest) != 64:
            raise ContractError(f"formal LiveGraph P31 input {label} is malformed")
        values.append(f"{label}={path}={digest}")
    return values


def seal_livegraph_dataset(request: dict[str, Any], repeat_dir: Path) -> tuple[Path, str]:
    """Publish the post-hash inode identity consumed inside P31 without rehashing SF10."""

    dataset = Path(str(request["dataset"]["path"])).resolve()
    if not dataset.is_file() or dataset.is_symlink():
        raise ContractError("formal LiveGraph dataset is not one regular file")
    stat = dataset.stat()
    receipt = {
        "schema_version": "p10-livegraph-dataset-seal-v1",
        "state": "PASS",
        "dataset": {
            "path": str(dataset),
            "sha256": request["dataset"]["sha256"],
            "identity": {
                "device": stat.st_dev,
                "inode": stat.st_ino,
                "size_bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "ctime_ns": stat.st_ctime_ns,
            },
        },
    }
    path = repeat_dir / "livegraph-dataset-seal.json"
    atomic_json(path, receipt)
    return path, sha256_file(path)


def materialize_fresh_repeat_roots(
    system: dict[str, Any], run_root: Path, repeat_index: int
) -> dict[str, Any]:
    """Create isolated store/temp directories for one fresh-import repeat."""

    if system["process_lifetime"] != FRESH_IMPORT_PROCESS_LIFETIME:
        return system
    effective = dict(system)
    run_token = hashlib.sha256(str(run_root.resolve()).encode()).hexdigest()[:12]
    planned: list[tuple[str, dict[str, str], Path]] = []
    for role in ("store", "temp"):
        roots: list[dict[str, str]] = []
        for root in system[f"{role}_roots"]:
            base = Path(root["path"]).resolve()
            if not base.is_dir():
                raise ContractError(f"fresh {role} base root is not an existing directory: {base}")
            instance = base / (
                f"{system['id']}-{role}-{root['label']}-{run_token}-r{repeat_index:02d}"
            )
            if instance.parent != base:
                raise ContractError(f"fresh {role} root escaped its declared base: {instance}")
            if instance.exists():
                raise ContractError(f"fresh {role} root already exists: {instance}")
            normalized = dict(root)
            normalized["path"] = str(instance)
            roots.append(normalized)
            planned.append((role, normalized, instance))
        effective[f"{role}_roots"] = roots
    paths = [path for _, _, path in planned]
    if len(paths) != len(set(paths)):
        raise ContractError("fresh store/temp roots resolve to duplicate paths")
    for left_index, left in enumerate(paths):
        for right in paths[left_index + 1 :]:
            if left in right.parents or right in left.parents:
                raise ContractError("fresh store/temp roots must not overlap")
    for _, _, path in planned:
        path.mkdir()
    return effective


def materialize_external_repeat_binding(
    system: dict[str, Any], repeat_index: int
) -> dict[str, Any]:
    """Select one pre-registered, independent Neo4j service for this repeat."""

    bindings = system.get("repeat_bindings", [])
    if not bindings:
        return system
    if system.get("id") != "neo4j":
        raise ContractError("external repeat bindings are reserved for Neo4j")
    matches = [binding for binding in bindings if binding.get("repeat_index") == repeat_index]
    if len(matches) != 1:
        raise ContractError(f"Neo4j repeat {repeat_index} lacks one exact repeat binding")
    binding = matches[0]
    effective = dict(system)
    effective["store_roots"] = [dict(binding["store_root"])]
    effective["temp_roots"] = [dict(binding["logs_root"])]
    effective["containers"] = [binding["container"]]
    effective_adapter = dict(system["adapter"])
    args = list(effective_adapter["args"])

    def replace(flag: str, value: str) -> None:
        positions = [index for index, item in enumerate(args) if item == flag]
        if len(positions) != 1 or positions[0] + 1 >= len(args):
            raise ContractError(f"Neo4j adapter args require exactly one {flag}")
        args[positions[0] + 1] = value

    replacements = {
        "--uri": binding["uri"],
        "--logs-root": binding["logs_root"]["path"],
        "--store-manifest": binding["store_manifest"]["path"],
        "--store-manifest-sha256": binding["store_manifest"]["sha256"],
        "--import-receipt": binding["import_receipt"]["path"],
        "--import-receipt-sha256": binding["import_receipt"]["sha256"],
    }
    for flag, value in replacements.items():
        replace(flag, value)
    effective_adapter["args"] = args
    effective["adapter"] = effective_adapter
    effective["active_repeat_binding"] = binding
    return effective


def materialize_nebulagraph_repeat_binding(
    system: dict[str, Any], repeat_index: int
) -> dict[str, Any]:
    """Select one independently cloned NebulaGraph store and Docker namespace."""

    if system.get("id") != "nebulagraph":
        return system
    bindings = system.get("repeat_bindings", [])
    if not bindings:
        return system
    matches = [binding for binding in bindings if binding.get("repeat_index") == repeat_index]
    if len(matches) != 1:
        raise ContractError(f"NebulaGraph repeat {repeat_index} lacks one exact repeat binding")
    binding = matches[0]
    effective = dict(system)
    effective["store_roots"] = [dict(binding["store_root"])]
    effective["temp_roots"] = [dict(binding["logs_root"])]
    effective["containers"] = [
        binding["containers"][role] for role in ("graphd", "metad", "storaged")
    ]
    effective_adapter = dict(system["adapter"])
    args = list(effective_adapter["args"])

    def replace(flag: str, value: str) -> None:
        positions = [index for index, item in enumerate(args) if item == flag]
        if len(positions) != 1 or positions[0] + 1 >= len(args):
            raise ContractError(f"NebulaGraph adapter args require exactly one {flag}")
        args[positions[0] + 1] = value

    replace("--store-manifest", binding["store_manifest"]["path"])
    replace("--store-manifest-sha256", binding["store_manifest"]["sha256"])
    effective_adapter["args"] = args
    effective["adapter"] = effective_adapter
    effective["active_nebulagraph_repeat_binding"] = binding
    return effective


def materialize_nebulagraph_lifecycle(
    system: dict[str, Any], repeat_dir: Path, mode: str
) -> dict[str, Any]:
    """Bind one formal NebulaGraph repeat to orchestrator-owned receipt paths."""

    if mode != "formal" or system.get("id") != "nebulagraph":
        return system
    controller = Path(exact_adapter_arg(system, "--cluster-controller")).resolve()
    controller_sha = exact_adapter_arg(system, "--cluster-controller-sha256")
    if controller != NEBULAGRAPH_LIFECYCLE.resolve() or not controller.is_file():
        raise ContractError(
            f"formal NebulaGraph requires the repository lifecycle controller: {NEBULAGRAPH_LIFECYCLE}"
        )
    if sha256_file(controller) != controller_sha:
        raise ContractError(
            "formal NebulaGraph lifecycle controller differs from the suite-pinned SHA-256"
        )
    logs = system.get("temp_roots")
    if not isinstance(logs, list) or len(logs) != 1:
        raise ContractError("formal NebulaGraph requires exactly one P31-visible logs root")
    logs_root = Path(str(logs[0].get("path", ""))).resolve()
    if logs[0].get("label") != "nebulagraph-logs" or not logs_root.is_dir():
        raise ContractError(
            "formal NebulaGraph requires an existing nebulagraph-logs temp root"
        )
    receipt_root = repeat_dir / "nebulagraph-lifecycle"
    receipt_root.mkdir()
    paths = {
        "store_lock": receipt_root / "store-admission.lock",
        "sealed_admission": receipt_root / "sealed-admission.json",
        "preflight": receipt_root / "preflight-receipt.json",
        "partial_start": receipt_root / "partial-start-receipt.json",
        "start_cleanup": receipt_root / "start-cleanup-receipt.json",
        "start": receipt_root / "start-receipt.json",
        "live_gate": receipt_root / "start-receipt.json",
        "stop": receipt_root / "stop-receipt.json",
    }
    effective = dict(system)
    effective["active_nebulagraph_lifecycle"] = {
        "controller": {"path": str(controller), "sha256": controller_sha},
        "receipt_root": str(receipt_root.resolve()),
        "receipt_paths": {name: str(path.resolve()) for name, path in paths.items()},
        "logs_root": str(logs_root),
    }
    return effective


def _run_nebulagraph_lifecycle_command(
    command: list[str], *, repeat_dir: Path, phase: str, timeout_s: int,
    require_success: bool = True,
) -> int:
    """Run one controller phase outside P31 and retain bounded command evidence."""

    atomic_json(repeat_dir / f"nebulagraph-{phase}-command.json", command)
    with (repeat_dir / f"nebulagraph-{phase}.stdout.log").open("wb") as stdout_handle, (
        repeat_dir / f"nebulagraph-{phase}.stderr.log"
    ).open("wb") as stderr_handle:
        try:
            completed = subprocess.run(
                command,
                stdout=stdout_handle,
                stderr=stderr_handle,
                check=False,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise ContractError(
                f"NebulaGraph {phase} exceeded its {timeout_s}s hard deadline"
            ) from exc
    if require_success and completed.returncode != 0:
        raise ContractError(
            f"NebulaGraph {phase} exited {completed.returncode}; "
            f"see {repeat_dir / f'nebulagraph-{phase}.stderr.log'}"
        )
    return completed.returncode


def _nebulagraph_common_lifecycle_args(
    *, system: dict[str, Any], request_path: Path
) -> list[str]:
    """Build the exact immutable admission argv shared by preflight phases."""

    return [
        "--request", str(request_path.resolve()),
        "--request-sha256", sha256_file(request_path),
        "--runtime-manifest", exact_adapter_arg(system, "--runtime-manifest"),
        "--runtime-manifest-sha256", exact_adapter_arg(system, "--runtime-manifest-sha256"),
        "--store-manifest", exact_adapter_arg(system, "--store-manifest"),
        "--store-manifest-sha256", exact_adapter_arg(system, "--store-manifest-sha256"),
        "--repo-root", exact_adapter_arg(system, "--repo-root"),
        "--p02b-result", exact_adapter_arg(system, "--p02b-result"),
        "--p02b-result-sha256", exact_adapter_arg(system, "--p02b-result-sha256"),
        "--p02b-validator", exact_adapter_arg(system, "--p02b-validator"),
        "--p02b-validator-sha256", exact_adapter_arg(system, "--p02b-validator-sha256"),
        "--p02b-binary", exact_adapter_arg(system, "--p02b-binary"),
        "--p02b-binary-sha256", exact_adapter_arg(system, "--p02b-binary-sha256"),
        "--p02b-max-age-seconds", exact_adapter_arg(system, "--p02b-max-age-seconds"),
    ]


def _current_process_start_ticks() -> int:
    try:
        raw = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="utf-8")
        fields = raw.rsplit(")", 1)[1].split()
        value = int(fields[19])
    except (OSError, UnicodeError, ValueError, IndexError) as exc:
        raise ContractError(f"cannot capture orchestrator process identity: {exc}") from exc
    if value <= 0:
        raise ContractError("orchestrator process start ticks are invalid")
    return value


def acquire_nebulagraph_store_lock(lock_path: Path) -> dict[str, Any]:
    """Create and retain one non-inheritable lock for the whole formal repeat."""

    if fcntl is None:
        raise ContractError("formal NebulaGraph store locking requires POSIX flock")
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ContractError(f"cannot create exclusive NebulaGraph store lock: {exc}") from exc
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        owner = {
            "schema_version": "cidr-p10-nebulagraph-store-lock-v1",
            "pid": os.getpid(),
            "process_start_ticks": _current_process_start_ticks(),
            "hostname": socket.gethostname(),
            "created_at_utc": utc_now(),
        }
        payload = (json.dumps(owner, sort_keys=True, separators=(",", ":")) + "\n").encode()
        written = os.write(descriptor, payload)
        if written != len(payload):
            raise ContractError("short write while publishing NebulaGraph store lock owner")
        os.fsync(descriptor)
        return {"fd": descriptor, "path": lock_path.resolve(), "owner": owner}
    except BaseException:
        os.close(descriptor)
        try:
            lock_path.unlink()
        except OSError:
            pass
        raise


def release_nebulagraph_store_lock(lifecycle: dict[str, Any]) -> None:
    lock = lifecycle.get("_store_lock")
    if not isinstance(lock, dict):
        return
    descriptor = lock.get("fd")
    if not isinstance(descriptor, int):
        raise ContractError("NebulaGraph lifecycle lock descriptor is malformed")
    if fcntl is None:
        raise ContractError("formal NebulaGraph store unlocking requires POSIX flock")
    fcntl.flock(descriptor, fcntl.LOCK_UN)
    os.close(descriptor)
    lifecycle.pop("_store_lock", None)


def launch_nebulagraph_repeat(
    *, system: dict[str, Any], request_path: Path, repeat_dir: Path
) -> dict[str, Any]:
    """Preflight and launch one formal cluster, then validate its full identity."""

    lifecycle = system.get("active_nebulagraph_lifecycle")
    if not isinstance(lifecycle, dict):
        raise ContractError("formal NebulaGraph repeat lacks orchestrated lifecycle paths")
    controller = Path(lifecycle["controller"]["path"]).resolve()
    paths = {name: Path(path) for name, path in lifecycle["receipt_paths"].items()}
    lock = acquire_nebulagraph_store_lock(paths["store_lock"])
    pending = {**lifecycle, "_store_lock": lock, "_start_attempted": False}
    try:
        seal_command = [
            system["binary"]["path"],
            str(controller),
            "seal",
            *_nebulagraph_common_lifecycle_args(system=system, request_path=request_path),
            "--lock-file", str(lock["path"]),
            "--lock-owner-pid", str(lock["owner"]["pid"]),
            "--lock-owner-start-ticks", str(lock["owner"]["process_start_ticks"]),
            "--output", str(paths["sealed_admission"]),
        ]
        _run_nebulagraph_lifecycle_command(
            seal_command, repeat_dir=repeat_dir, phase="seal", timeout_s=7200
        )
        sealed_sha = sha256_file(paths["sealed_admission"])
        cluster = _nebulagraph_cluster_module()
        sealed_path, sealed = cluster.validate_sealed_admission_receipt(
            paths["sealed_admission"],
            sealed_sha,
            request_path=request_path,
            request_sha256=sha256_file(request_path),
            max_age_seconds=900,
        )
        pending.update(
            {
                "sealed_admission_path": sealed_path,
                "sealed_admission_sha256": sealed_sha,
                "sealed_admission": sealed,
            }
        )
        preflight_command = [
            system["binary"]["path"],
            str(controller),
            "preflight",
            *_nebulagraph_common_lifecycle_args(system=system, request_path=request_path),
            "--logs-root", lifecycle["logs_root"],
            "--query-timeout-ms", "5000",
            "--output", str(paths["preflight"]),
        ]
        _run_nebulagraph_lifecycle_command(
            preflight_command, repeat_dir=repeat_dir, phase="preflight", timeout_s=900
        )
        preflight_sha = sha256_file(paths["preflight"])
        preflight_path, preflight, _docker = cluster.validate_preflight_receipt(
            paths["preflight"], preflight_sha, max_age_seconds=600
        )
        pending.update(
            {
                "preflight_path": preflight_path,
                "preflight_sha256": preflight_sha,
                "preflight": preflight,
            }
        )
        start_command = [
            system["binary"]["path"],
            str(controller),
            "start",
            "--preflight", str(preflight_path),
            "--preflight-sha256", preflight_sha,
            "--max-preflight-age-seconds", "600",
            "--startup-timeout-seconds", "300",
            "--partial-output", str(paths["partial_start"]),
            "--cleanup-output", str(paths["start_cleanup"]),
            "--cleanup-stop-timeout-seconds", "30",
            "--output", str(paths["start"]),
        ]
        pending["_start_attempted"] = True
        _run_nebulagraph_lifecycle_command(
            start_command, repeat_dir=repeat_dir, phase="start", timeout_s=900
        )
        start_sha = sha256_file(paths["start"])
        start_path, start = cluster.validate_start_receipt(
            paths["start"],
            start_sha,
            preflight_path=preflight_path,
            preflight_sha256=preflight_sha,
            preflight_value=preflight,
        )
        pending.update(
            {
                "start_path": start_path,
                "start_sha256": start_sha,
                "start": start,
            }
        )
        return pending
    except BaseException as failure:
        try:
            recovery = recover_nebulagraph_failure(
                system=system,
                repeat_dir=repeat_dir,
                lifecycle=pending,
                primary_failure=failure,
            )
            if hasattr(failure, "add_note"):
                failure.add_note(
                    f"NebulaGraph launch recovery state: {recovery.get('state', 'UNKNOWN')}"
                )
        except BaseException as recovery_error:
            if hasattr(failure, "add_note"):
                failure.add_note(
                    "NebulaGraph launch recovery also failed: "
                    f"{type(recovery_error).__name__}: {recovery_error}"
                )
        finally:
            release_nebulagraph_store_lock(pending)
        raise


def stop_nebulagraph_repeat(
    *, system: dict[str, Any], repeat_dir: Path, lifecycle: dict[str, Any]
) -> dict[str, Any]:
    """Stop one launch-bound cluster by exact IDs and validate teardown evidence."""

    controller = Path(lifecycle["controller"]["path"]).resolve()
    if (
        controller != NEBULAGRAPH_LIFECYCLE.resolve()
        or sha256_file(controller) != lifecycle["controller"]["sha256"]
    ):
        raise ContractError("NebulaGraph stop controller differs from its launch binding")
    stop_path = Path(lifecycle["receipt_paths"]["stop"])
    command = [
        system["binary"]["path"],
        str(controller),
        "stop",
        "--preflight", str(lifecycle["preflight_path"]),
        "--preflight-sha256", lifecycle["preflight_sha256"],
        "--start-receipt", str(lifecycle["start_path"]),
        "--start-receipt-sha256", lifecycle["start_sha256"],
        "--stop-timeout-seconds", "30",
        "--output", str(stop_path),
    ]
    _run_nebulagraph_lifecycle_command(
        command, repeat_dir=repeat_dir, phase="stop", timeout_s=180
    )
    stop_sha = sha256_file(stop_path)
    cluster = _nebulagraph_cluster_module()
    validated_path, stop = cluster.validate_stop_receipt(
        stop_path,
        stop_sha,
        preflight_path=Path(lifecycle["preflight_path"]),
        preflight_sha256=lifecycle["preflight_sha256"],
        start_path=Path(lifecycle["start_path"]),
        start_sha256=lifecycle["start_sha256"],
        preflight_value=lifecycle["preflight"],
        start_value=lifecycle["start"],
    )
    return {
        **lifecycle,
        "stop_path": validated_path,
        "stop_sha256": stop_sha,
        "stop": stop,
    }


def recover_nebulagraph_failure(
    *,
    system: dict[str, Any],
    repeat_dir: Path,
    lifecycle: dict[str, Any],
    primary_failure: BaseException,
) -> dict[str, Any]:
    """Publish terminal exact-ID cleanup evidence while the store lock is still held."""

    status_path = repeat_dir / "nebulagraph-failure-recovery.json"
    controller = Path(lifecycle["controller"]["path"]).resolve()
    if (
        controller != NEBULAGRAPH_LIFECYCLE.resolve()
        or sha256_file(controller) != lifecycle["controller"]["sha256"]
    ):
        raise ContractError("NebulaGraph recovery controller differs from its launch binding")
    paths = {name: Path(path) for name, path in lifecycle["receipt_paths"].items()}
    cluster = _nebulagraph_cluster_module()
    failure_value = {
        "type": type(primary_failure).__name__,
        "message": (str(primary_failure) or "unspecified failure")[:2000],
    }

    def publish(
        state: str,
        disposition: str,
        *,
        receipt_path: Path | None = None,
        receipt_state: str | None = None,
        detail: str | None = None,
    ) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": "cidr-p10-nebulagraph-failure-recovery-v1",
            "state": state,
            "created_at_utc": utc_now(),
            "primary_failure": failure_value,
            "disposition": disposition,
            "name_lookup_used": False,
            "store_lock_held": "_store_lock" in lifecycle,
            "detail": detail,
            "recovery_receipt": None,
        }
        if receipt_path is not None:
            value["recovery_receipt"] = {
                "path": str(receipt_path.resolve()),
                "sha256": sha256_file(receipt_path),
                "state": receipt_state,
            }
        atomic_json(status_path, value)
        return value

    preflight_path = lifecycle.get("preflight_path")
    preflight_sha = lifecycle.get("preflight_sha256")
    preflight = lifecycle.get("preflight")
    if not isinstance(preflight_path, Path) or not isinstance(preflight_sha, str) or not isinstance(preflight, dict):
        if lifecycle.get("_start_attempted") is True:
            return publish(
                "BLOCKED",
                "START_ATTEMPT_WITHOUT_VALIDATED_PREFLIGHT",
                detail="resource identities cannot be recovered without a validated preflight receipt",
            )
        return publish("PASS", "NO_DOCKER_MUTATION_ATTEMPTED")

    start_path = paths["start"]
    cleanup_path = paths["start_cleanup"]
    if start_path.is_file() and not start_path.is_symlink():
        start_sha = sha256_file(start_path)
        validated_start_path, start = cluster.validate_start_receipt(
            start_path,
            start_sha,
            preflight_path=preflight_path,
            preflight_sha256=preflight_sha,
            preflight_value=preflight,
        )
        if cleanup_path.exists() or cleanup_path.is_symlink():
            raise ContractError("start-bound recovery output already exists")
        command = [
            system["binary"]["path"],
            str(controller),
            "recover",
            "--preflight", str(preflight_path),
            "--preflight-sha256", preflight_sha,
            "--start-receipt", str(validated_start_path),
            "--start-receipt-sha256", start_sha,
            "--stop-timeout-seconds", "30",
            "--output", str(cleanup_path),
        ]
        _run_nebulagraph_lifecycle_command(
            command,
            repeat_dir=repeat_dir,
            phase="failure-recover-start",
            timeout_s=420,
            require_success=False,
        )
        if not cleanup_path.is_file() or cleanup_path.is_symlink():
            return publish(
                "BLOCKED", "START_BOUND_CLEANUP_MISSING",
                detail="recovery controller did not publish an exact-ID receipt",
            )
        cleanup_sha = sha256_file(cleanup_path)
        _validated_cleanup_path, cleanup = cluster.validate_start_cleanup_receipt(
            cleanup_path,
            cleanup_sha,
            preflight_path=preflight_path,
            preflight_sha256=preflight_sha,
            start_path=validated_start_path,
            start_sha256=start_sha,
            preflight_value=preflight,
            start_value=start,
        )
        return publish(
            cleanup["state"],
            "START_BOUND_EXACT_ID_CLEANUP",
            receipt_path=cleanup_path,
            receipt_state=cleanup["state"],
            detail=cleanup.get("blocked_reason"),
        )

    partial_path = paths["partial_start"]
    if partial_path.is_file() and not partial_path.is_symlink():
        partial_sha = sha256_file(partial_path)
        validated_partial_path, partial = cluster.validate_partial_start_receipt(
            partial_path,
            partial_sha,
            preflight_path=preflight_path,
            preflight_sha256=preflight_sha,
            preflight_value=preflight,
        )
        if not cleanup_path.exists() and not cleanup_path.is_symlink():
            command = [
                system["binary"]["path"],
                str(controller),
                "cleanup",
                "--preflight", str(preflight_path),
                "--preflight-sha256", preflight_sha,
                "--partial-start", str(validated_partial_path),
                "--partial-start-sha256", partial_sha,
                "--max-preflight-age-seconds", "604800",
                "--stop-timeout-seconds", "30",
                "--output", str(cleanup_path),
            ]
            _run_nebulagraph_lifecycle_command(
                command,
                repeat_dir=repeat_dir,
                phase="failure-recover-partial",
                timeout_s=420,
                require_success=False,
            )
        if not cleanup_path.is_file() or cleanup_path.is_symlink():
            return publish(
                "BLOCKED", "PARTIAL_CLEANUP_MISSING",
                detail="partial start has no exact-ID cleanup receipt",
            )
        cleanup_sha = sha256_file(cleanup_path)
        _validated_cleanup_path, cleanup = cluster.validate_cleanup_receipt(
            cleanup_path,
            cleanup_sha,
            preflight_path=preflight_path,
            preflight_sha256=preflight_sha,
            partial_path=validated_partial_path,
            partial_sha256=partial_sha,
            preflight_value=preflight,
            partial_value=partial,
        )
        return publish(
            cleanup["state"],
            "PARTIAL_START_EXACT_ID_CLEANUP",
            receipt_path=cleanup_path,
            receipt_state=cleanup["state"],
            detail=cleanup.get("blocked_reason"),
        )

    if lifecycle.get("_start_attempted") is True:
        return publish(
            "BLOCKED",
            "START_ATTEMPT_WITHOUT_RESOURCE_IDS",
            detail=(
                "start was attempted but neither a start receipt nor a partial-start receipt exists; "
                "name-based deletion is forbidden"
            ),
        )
    return publish("PASS", "NO_DOCKER_MUTATION_ATTEMPTED")


def _run_lifecycle_command(command: list[str], *, repeat_dir: Path, phase: str) -> None:
    """Run one small Neo4j lifecycle command and preserve its exact invocation/logs."""

    atomic_json(repeat_dir / f"neo4j-{phase}-command.json", command)
    with (repeat_dir / f"neo4j-{phase}.stdout.log").open("wb") as stdout_handle, (
        repeat_dir / f"neo4j-{phase}.stderr.log"
    ).open("wb") as stderr_handle:
        completed = subprocess.run(
            command,
            stdout=stdout_handle,
            stderr=stderr_handle,
            check=False,
        )
    if completed.returncode != 0:
        raise ContractError(
            f"Neo4j {phase} exited {completed.returncode}; "
            f"see {repeat_dir / f'neo4j-{phase}.stderr.log'}"
        )


def launch_neo4j_repeat(
    *,
    system: dict[str, Any],
    repeat_index: int,
    repeat_dir: Path,
    run_id: str,
    store_preflight: Path,
) -> dict[str, Any]:
    """Launch one preregistered repeat and validate the resulting receipt."""

    binding = system.get("active_repeat_binding")
    if not isinstance(binding, dict) or binding.get("repeat_index") != repeat_index:
        raise ContractError("formal Neo4j launch lacks its active repeat binding")
    launcher = binding.get("launcher")
    if not isinstance(launcher, dict):
        raise ContractError("formal Neo4j repeat lacks its pinned lifecycle helper")
    launcher_path = Path(str(launcher.get("path", ""))).resolve()
    if launcher_path != NEO4J_LIFECYCLE.resolve() or not launcher_path.is_file():
        raise ContractError(f"Neo4j lifecycle helper is missing: {NEO4J_LIFECYCLE}")
    if sha256_file(launcher_path) != launcher.get("sha256"):
        raise ContractError("Neo4j lifecycle helper differs from the suite-pinned SHA-256")
    repeat_root = Path(binding["repeat_root"]).resolve()
    receipt_root = repeat_root / "p10-lifecycle" / run_id
    receipt_root.mkdir(parents=True, exist_ok=False)
    launch_path = receipt_root / "launch-receipt.json"
    parsed_uri = urlparse(binding["uri"])
    if parsed_uri.port is None:
        raise ContractError("formal Neo4j repeat URI lacks a Bolt port")
    image_digests = system.get("image_digests")
    if image_digests != [NEO4J_IMAGE_REPO_DIGEST]:
        raise ContractError("formal Neo4j launch lacks the frozen image RepoDigest")
    command = [
        system["binary"]["path"],
        str(launcher_path),
        "launch",
        "--repeat-index", str(repeat_index),
        "--clone-id", binding["clone_id"],
        "--repeat-root", str(repeat_root),
        "--source-store-root", binding["source_store_root"],
        "--store-root", binding["store_root"]["path"],
        "--logs-root", binding["logs_root"]["path"],
        "--store-manifest", binding["store_manifest"]["path"],
        "--store-manifest-sha256", binding["store_manifest"]["sha256"],
        "--store-preflight", str(store_preflight.resolve()),
        "--store-preflight-sha256", sha256_file(store_preflight),
        "--container-name", binding["container"],
        "--bolt-port", str(parsed_uri.port),
        "--image-digest", image_digests[0],
        "--output", str(launch_path),
    ]
    _run_lifecycle_command(command, repeat_dir=repeat_dir, phase="launch")
    launch = validate_launch_receipt(
        read_json(launch_path, "Neo4j runtime launch receipt"),
        verify_artifacts=True,
        expected_host=neo4j_current_host(),
    )
    return {
        "receipt_root": receipt_root,
        "launch_path": launch_path,
        "launch_sha256": sha256_file(launch_path),
        "launch": launch,
    }


def stop_neo4j_repeat(
    *,
    system: dict[str, Any],
    repeat_dir: Path,
    lifecycle: dict[str, Any],
) -> dict[str, Any]:
    """Gracefully stop/remove one launched repeat and validate exact-ID evidence."""

    stop_path = Path(lifecycle["receipt_root"]) / "stop-receipt.json"
    binding = system.get("active_repeat_binding")
    launcher_path = Path(str(binding.get("launcher", {}).get("path", ""))).resolve() if isinstance(binding, dict) else Path()
    if launcher_path != NEO4J_LIFECYCLE.resolve() or sha256_file(launcher_path) != binding["launcher"]["sha256"]:
        raise ContractError("Neo4j stop helper differs from the suite-pinned launcher")
    command = [
        system["binary"]["path"],
        str(launcher_path),
        "stop",
        "--launch-receipt", str(lifecycle["launch_path"]),
        "--launch-receipt-sha256", lifecycle["launch_sha256"],
        "--output", str(stop_path),
    ]
    _run_lifecycle_command(command, repeat_dir=repeat_dir, phase="stop")
    stop = validate_stop_receipt(
        read_json(stop_path, "Neo4j runtime stop receipt"),
        verify_artifacts=True,
        expected_host=neo4j_current_host(),
        launch_document=lifecycle["launch"],
    )
    return {
        **lifecycle,
        "stop_path": stop_path,
        "stop_sha256": sha256_file(stop_path),
        "stop": stop,
    }


def _failure_cleanup_command(
    command: list[str],
    *,
    repeat_dir: Path,
    phase: str,
    timeout_s: int = 120,
) -> subprocess.CompletedProcess[str]:
    """Run one exact-target cleanup command while retaining its full evidence."""

    atomic_json(repeat_dir / f"neo4j-failure-{phase}-command.json", command)
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_s,
    )
    (repeat_dir / f"neo4j-failure-{phase}.stdout.log").write_text(
        completed.stdout, encoding="utf-8"
    )
    (repeat_dir / f"neo4j-failure-{phase}.stderr.log").write_text(
        completed.stderr, encoding="utf-8"
    )
    if completed.returncode != 0:
        raise ContractError(
            f"Neo4j failure cleanup phase {phase} exited {completed.returncode}; "
            f"see {repeat_dir / f'neo4j-failure-{phase}.stderr.log'}"
        )
    return completed


def _inspect_failure_cleanup_target(
    *,
    container_name: str,
    expected_container_id: str,
    repeat_dir: Path,
    phase: str,
) -> dict[str, Any]:
    """Inspect the launch-bound full ID and verify its recorded name."""

    completed = _failure_cleanup_command(
        ["docker", "container", "inspect", expected_container_id],
        repeat_dir=repeat_dir,
        phase=phase,
    )
    try:
        values = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError("Neo4j failure cleanup inspect is not JSON") from exc
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
        raise ContractError("Neo4j failure cleanup inspect must return one container")
    inspect = values[0]
    if inspect.get("Id") != expected_container_id:
        raise ContractError("Neo4j failure cleanup target ID differs from the launch receipt")
    if inspect.get("Name") != f"/{container_name}":
        raise ContractError("Neo4j failure cleanup target name differs from the launch receipt")
    state = inspect.get("State")
    restart_count = inspect.get("RestartCount")
    if not isinstance(state, dict) or not isinstance(state.get("Running"), bool):
        raise ContractError("Neo4j failure cleanup inspect lacks an exact running state")
    if not isinstance(state.get("Pid"), int) or isinstance(state.get("Pid"), bool):
        raise ContractError("Neo4j failure cleanup inspect lacks an exact PID")
    if not isinstance(state.get("ExitCode"), int) or isinstance(state.get("ExitCode"), bool):
        raise ContractError("Neo4j failure cleanup inspect lacks an exact exit code")
    if not isinstance(restart_count, int) or isinstance(restart_count, bool):
        raise ContractError("Neo4j failure cleanup inspect lacks an exact restart count")
    return {
        "container_name": container_name,
        "container_id": expected_container_id,
        "running": state["Running"],
        "pid": state["Pid"],
        "exit_code": state["ExitCode"],
        "restart_count": restart_count,
    }


def cleanup_failed_neo4j_repeat(
    *,
    system: dict[str, Any],
    repeat_dir: Path,
    lifecycle: dict[str, Any],
    failure: BaseException,
) -> dict[str, Any]:
    """Gracefully stop and remove only the failed repeat's launch-bound container."""

    binding = system.get("active_repeat_binding")
    launch = lifecycle.get("launch")
    if not isinstance(binding, dict) or not isinstance(launch, dict):
        raise ContractError("Neo4j failure cleanup lacks its launch-bound repeat identity")
    runtime_contract = launch.get("runtime_contract")
    running = launch.get("docker", {}).get("running")
    running_config = running.get("config") if isinstance(running, dict) else None
    if not isinstance(runtime_contract, dict) or not isinstance(running_config, dict):
        raise ContractError("Neo4j failure cleanup launch receipt lacks container identity")
    container_name = runtime_contract.get("container_name")
    container_id = running_config.get("container_id")
    if (
        not isinstance(container_name, str)
        or container_name != binding.get("container")
        or not isinstance(container_id, str)
        or len(container_id) != 64
    ):
        raise ContractError("Neo4j failure cleanup binding differs from the launch receipt")
    intent = {
        "schema_version": "cidr-p10-neo4j-failure-cleanup-intent-v1",
        "failure": {"type": type(failure).__name__, "message": str(failure)},
        "target": {"container_name": container_name, "container_id": container_id},
        "launch_receipt": {
            "path": str(Path(lifecycle["launch_path"]).resolve()),
            "sha256": lifecycle["launch_sha256"],
        },
        "policy": "exact-launch-identity-graceful-stop-then-remove-v1",
    }
    atomic_json(repeat_dir / "neo4j-failure-cleanup-intent.json", intent)
    validated_stop = lifecycle.get("stop")
    if (
        isinstance(validated_stop, dict)
        and validated_stop.get("outcome")
        == {
            "completed": True,
            "container_running": False,
            "container_absent": True,
            "exit_code": 0,
            "restart_count": 0,
        }
    ):
        receipt = {
            "schema_version": "cidr-p10-neo4j-failure-cleanup-v1",
            "failure": intent["failure"],
            "target": intent["target"],
            "before": None,
            "after_graceful_stop": validated_stop["post_stop_inspect"]["runtime"],
            "validated_stop_receipt": {
                "path": str(Path(lifecycle["stop_path"]).resolve()),
                "sha256": lifecycle["stop_sha256"],
            },
            "failure_graceful_stop": validated_stop["stop"],
            "remove": validated_stop["remove"],
            "absence_probe": validated_stop["absence_probe"],
            "outcome": {
                "gracefully_stopped": True,
                "container_absent": True,
                "non_target_containers_touched": False,
            },
        }
        receipt_path = repeat_dir / "neo4j-failure-cleanup.json"
        atomic_json(receipt_path, receipt)
        return {**lifecycle, "failure_cleanup_path": receipt_path, "failure_cleanup": receipt}
    before = _inspect_failure_cleanup_target(
        container_name=container_name,
        expected_container_id=container_id,
        repeat_dir=repeat_dir,
        phase="cleanup-inspect-before",
    )
    failure_stop: dict[str, Any] | None = None
    after_stop = before
    if before["running"]:
        # Mutate by the immutable full ID, never by a potentially reused name.
        stop_command = ["docker", "container", "stop", "--time", "60", container_id]
        stopped = _failure_cleanup_command(
            stop_command,
            repeat_dir=repeat_dir,
            phase="cleanup-graceful-stop",
            timeout_s=90,
        )
        if stopped.stdout.strip() != container_id:
            raise ContractError("Neo4j failure cleanup stop did not return its exact full ID")
        failure_stop = {"command": stop_command, "stdout": stopped.stdout.strip()}
        after_stop = _inspect_failure_cleanup_target(
            container_name=container_name,
            expected_container_id=container_id,
            repeat_dir=repeat_dir,
            phase="cleanup-inspect-after-stop",
        )
    if (
        after_stop["running"]
        or after_stop["pid"] != 0
        or after_stop["exit_code"] != 0
        or after_stop["restart_count"] != 0
    ):
        raise ContractError("Neo4j failed repeat is not cleanly stopped; refusing removal")
    # Removal is also ID-bound so a concurrent same-name container is never touched.
    remove_command = ["docker", "container", "rm", container_id]
    removed = _failure_cleanup_command(
        remove_command,
        repeat_dir=repeat_dir,
        phase="cleanup-remove",
    )
    if removed.stdout.strip() != container_id:
        raise ContractError("Neo4j failure cleanup removal did not return its exact full ID")
    absence_command = [
        "docker", "container", "ls", "--all",
        "--filter", f"name=^/{container_name}$", "--format", "{{.ID}}",
    ]
    absent = _failure_cleanup_command(
        absence_command,
        repeat_dir=repeat_dir,
        phase="cleanup-prove-absent",
    )
    if absent.stdout.strip():
        raise ContractError("Neo4j failed repeat container still exists after removal")
    receipt = {
        "schema_version": "cidr-p10-neo4j-failure-cleanup-v1",
        "failure": intent["failure"],
        "target": intent["target"],
        "before": before,
        "after_graceful_stop": after_stop,
        "validated_stop_receipt": None if "stop" not in lifecycle else {
            "path": str(Path(lifecycle["stop_path"]).resolve()),
            "sha256": lifecycle["stop_sha256"],
        },
        "failure_graceful_stop": failure_stop,
        "remove": {"command": remove_command, "stdout": removed.stdout.strip()},
        "absence_probe": {"command": absence_command, "stdout": absent.stdout},
        "outcome": {
            "gracefully_stopped": True,
            "container_absent": True,
            "non_target_containers_touched": False,
        },
    }
    receipt_path = repeat_dir / "neo4j-failure-cleanup.json"
    atomic_json(receipt_path, receipt)
    return {**lifecycle, "failure_cleanup_path": receipt_path, "failure_cleanup": receipt}


def preserve_failed_neo4j_repeat(
    *,
    system: dict[str, Any],
    repeat_dir: Path,
    lifecycle: dict[str, Any] | None,
    failure: BaseException,
) -> None:
    """Retain the primary error while making a best-effort, evidenced cleanup."""

    atomic_json(
        repeat_dir / "neo4j-failure-context.json",
        {
            "schema_version": "cidr-p10-neo4j-failure-context-v1",
            "failure": {"type": type(failure).__name__, "message": str(failure)},
            "launch_completed": lifecycle is not None,
        },
    )
    if lifecycle is None:
        return
    try:
        cleanup_failed_neo4j_repeat(
            system=system,
            repeat_dir=repeat_dir,
            lifecycle=lifecycle,
            failure=failure,
        )
    except BaseException as cleanup_error:  # never replace the experiment's primary failure
        atomic_json(
            repeat_dir / "neo4j-failure-cleanup-error.json",
            {
                "schema_version": "cidr-p10-neo4j-failure-cleanup-error-v1",
                "primary_failure": {"type": type(failure).__name__, "message": str(failure)},
                "cleanup_failure": {
                    "type": type(cleanup_error).__name__,
                    "message": str(cleanup_error),
                },
            },
        )
        if hasattr(failure, "add_note"):
            failure.add_note(
                f"Neo4j exact-target cleanup also failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )


def _execute_repeat_impl(
    *,
    suite: dict[str, Any],
    system: dict[str, Any],
    truth_rows: list[dict[str, int]],
    repeat_index: int,
    run_root: Path,
    resolved_manifest: Path,
    p31_wrapper: Path,
    mode: str,
    admission: dict[str, Any] | None = None,
) -> dict[str, Any]:
    repeat_admission: dict[str, Any] | None = None
    if admission is not None and admission.get("protocol") == "batch-lease-v2":
        receipt, validator_argv = validate_batch_lease(
            admission["lease"], admission["gate_tool"], admission["anchor_binary"]
        )
        repeat_admission = {
            "schema_version": "cidr-p10-repeat-batch-admission-v2",
            "state": "PASS",
            "protocol": "batch-lease-v2",
            "consumer": "P10",
            "receipt": receipt,
            "validator_argv": validator_argv,
            "lease": artifact_ref(admission["lease"]),
            "gate_tool": artifact_ref(admission["gate_tool"]),
            "anchor_binary": artifact_ref(admission["anchor_binary"]),
        }
    repeat_dir = run_root / "systems" / system["id"] / f"repeat-{repeat_index:02d}"
    repeat_dir.mkdir(parents=True, exist_ok=False)
    repeat_admission_path: Path | None = None
    if repeat_admission is not None:
        repeat_admission_path = repeat_dir / "batch-lease-admission.json"
        atomic_json(repeat_admission_path, repeat_admission)
    adapter_output = repeat_dir / "adapter-output"
    adapter_output.mkdir()
    effective_system = materialize_fresh_repeat_roots(system, run_root, repeat_index)
    effective_system = materialize_external_repeat_binding(effective_system, repeat_index)
    effective_system = materialize_nebulagraph_repeat_binding(
        effective_system, repeat_index
    )
    effective_system = materialize_nebulagraph_lifecycle(
        effective_system, repeat_dir, mode
    )
    request = build_request(suite, effective_system, repeat_index, run_root.name, mode)
    request_path = repeat_dir / "adapter-request.json"
    atomic_json(request_path, request)
    neo4j_audits: dict[str, Any] | None = None
    neo4j_lifecycle: dict[str, Any] | None = None
    nebulagraph_lifecycle: dict[str, Any] | None = None
    dynamic_adapter_args: list[str] = []
    named_inputs: list[str] = []
    if mode == "formal" and system["id"] == "livegraph":
        named_inputs = livegraph_p31_inputs(effective_system)
        dataset_seal, dataset_seal_sha = seal_livegraph_dataset(request, repeat_dir)
        named_inputs.append(f"livegraph_dataset_seal={dataset_seal}={dataset_seal_sha}")
        dynamic_adapter_args.extend(
            ["--dataset-seal", str(dataset_seal), "--dataset-seal-sha256", dataset_seal_sha]
        )
    if mode == "formal" and system["id"] == "neo4j":
        matching_stores = [
            root for root in effective_system["store_roots"]
            if root.get("label") == "neo4j-runtime"
        ]
        if len(matching_stores) != 1:
            raise ContractError("formal Neo4j requires exactly one neo4j-runtime store root")
        store_root = Path(matching_stores[0]["path"]).resolve()
        manifest_path = Path(exact_adapter_arg(effective_system, "--store-manifest")).resolve()
        manifest_sha = exact_adapter_arg(effective_system, "--store-manifest-sha256")
        pre_path = repeat_dir / "neo4j-store-audit-pre.json"
        pre = audit_neo4j_runtime_store(
            stage="pre",
            store_root=store_root,
            store_manifest_path=manifest_path,
            store_manifest_sha256=manifest_sha,
            request_path=request_path,
            output_path=pre_path,
        )
        request_ref = {
            "path": str(request_path.resolve()),
            "sha256": sha256_file(request_path),
            "size_bytes": request_path.stat().st_size,
        }
        manifest_ref = {
            "path": str(manifest_path),
            "sha256": manifest_sha,
            "size_bytes": manifest_path.stat().st_size,
        }
        pre = validate_neo4j_store_audit(
            pre,
            stage="pre",
            request_ref=request_ref,
            store_root=store_root,
            store_manifest_ref=manifest_ref,
        )
        neo4j_audits = {
            "store_root": store_root,
            "store_manifest": manifest_ref,
            "request": request_ref,
            "pre_path": pre_path,
            "pre": pre,
        }
        dynamic_adapter_args = [
            "--store-preflight",
            str(pre_path),
            "--store-preflight-sha256",
            sha256_file(pre_path),
        ]
        neo4j_lifecycle = launch_neo4j_repeat(
            system=effective_system,
            repeat_index=repeat_index,
            repeat_dir=repeat_dir,
            run_id=run_root.name,
            store_preflight=pre_path,
        )
        dynamic_adapter_args.extend(
            [
                "--launch-receipt",
                str(neo4j_lifecycle["launch_path"]),
                "--launch-receipt-sha256",
                neo4j_lifecycle["launch_sha256"],
            ]
        )
    elif mode == "formal" and system["id"] == "nebulagraph":
        nebulagraph_lifecycle = launch_nebulagraph_repeat(
            system=effective_system,
            request_path=request_path,
            repeat_dir=repeat_dir,
        )
        dynamic_adapter_args = [
            "--sealed-admission",
            str(nebulagraph_lifecycle["sealed_admission_path"]),
            "--sealed-admission-sha256",
            nebulagraph_lifecycle["sealed_admission_sha256"],
            "--cluster-preflight",
            str(nebulagraph_lifecycle["preflight_path"]),
            "--cluster-preflight-sha256",
            nebulagraph_lifecycle["preflight_sha256"],
            "--cluster-start-receipt",
            str(nebulagraph_lifecycle["start_path"]),
            "--cluster-start-receipt-sha256",
            nebulagraph_lifecycle["start_sha256"],
        ]
    completed: subprocess.CompletedProcess[Any] | None = None
    p31_error: BaseException | None = None
    teardown_error: BaseException | None = None
    try:
        command = p31_command(
            suite=suite,
            system=effective_system,
            repeat_index=repeat_index,
            repeat_dir=repeat_dir,
            request_path=request_path,
            adapter_output=adapter_output,
            resolved_manifest=resolved_manifest,
            p31_wrapper=p31_wrapper,
            mode=mode,
            extra_adapter_args=dynamic_adapter_args,
            named_inputs=named_inputs,
            admission=admission,
        )
        (repeat_dir / "orchestrator-command.json").write_text(
            json.dumps(command, indent=2) + "\n", encoding="utf-8"
        )
        with (repeat_dir / "p31-wrapper.stdout.log").open("wb") as stdout_handle, (
            repeat_dir / "p31-wrapper.stderr.log"
        ).open("wb") as stderr_handle:
            completed = subprocess.run(command, stdout=stdout_handle, stderr=stderr_handle, check=False)
        if completed.returncode != 0:
            p31_error = ContractError(
                f"{system['id']} repeat {repeat_index}: P31/adapter exited "
                f"{completed.returncode}; see {repeat_dir / 'p31-wrapper.stderr.log'}"
            )
    except BaseException as exc:
        p31_error = exc
    finally:
        if neo4j_lifecycle is not None:
            neo4j_lifecycle = stop_neo4j_repeat(
                system=effective_system,
                repeat_dir=repeat_dir,
                lifecycle=neo4j_lifecycle,
            )
        if nebulagraph_lifecycle is not None:
            try:
                nebulagraph_lifecycle = stop_nebulagraph_repeat(
                    system=effective_system,
                    repeat_dir=repeat_dir,
                    lifecycle=nebulagraph_lifecycle,
                )
            except BaseException as stop_failure:
                try:
                    recovery = recover_nebulagraph_failure(
                        system=effective_system,
                        repeat_dir=repeat_dir,
                        lifecycle=nebulagraph_lifecycle,
                        primary_failure=stop_failure,
                    )
                    teardown_error = ContractError(
                        "NebulaGraph graceful stop failed; exact-ID recovery state="
                        f"{recovery.get('state', 'UNKNOWN')}: {stop_failure}"
                    )
                except BaseException as recovery_failure:
                    teardown_error = ContractError(
                        "NebulaGraph graceful stop and exact-ID recovery both failed: "
                        f"stop={stop_failure}; recovery={recovery_failure}"
                    )
            finally:
                release_nebulagraph_store_lock(nebulagraph_lifecycle)
    if neo4j_audits is not None:
        post_path = repeat_dir / "neo4j-store-audit-post-stop.json"
        post = audit_neo4j_runtime_store(
            stage="post-stop",
            store_root=neo4j_audits["store_root"],
            store_manifest_path=Path(neo4j_audits["store_manifest"]["path"]),
            store_manifest_sha256=neo4j_audits["store_manifest"]["sha256"],
            request_path=request_path,
            output_path=post_path,
            stop_receipt_path=Path(neo4j_lifecycle["stop_path"]) if neo4j_lifecycle is not None else None,
            stop_receipt_sha256=neo4j_lifecycle["stop_sha256"] if neo4j_lifecycle is not None else None,
        )
        stop_ref = None if neo4j_lifecycle is None else {
            "path": str(Path(neo4j_lifecycle["stop_path"]).resolve()),
            "sha256": neo4j_lifecycle["stop_sha256"],
            "size_bytes": Path(neo4j_lifecycle["stop_path"]).stat().st_size,
        }
        post = validate_neo4j_store_audit(
            post,
            stage="post-stop",
            request_ref=neo4j_audits["request"],
            store_root=neo4j_audits["store_root"],
            store_manifest_ref=neo4j_audits["store_manifest"],
            stop_receipt_ref=stop_ref,
        )
        validate_neo4j_store_audit_pair(neo4j_audits["pre"], post)
        neo4j_audits.update({"post_stop_path": post_path, "post_stop": post})
    if p31_error is not None:
        if teardown_error is not None and hasattr(p31_error, "add_note"):
            p31_error.add_note(
                f"NebulaGraph teardown also failed: {type(teardown_error).__name__}: {teardown_error}"
            )
        if isinstance(p31_error, ContractError):
            raise p31_error
        if isinstance(p31_error, (KeyboardInterrupt, SystemExit)):
            raise p31_error
        raise ContractError(
            f"{system['id']} repeat {repeat_index}: cannot run P31/adapter: {p31_error}"
        ) from p31_error
    if teardown_error is not None:
        raise teardown_error
    if completed is None:
        raise ContractError(f"{system['id']} repeat {repeat_index}: P31/adapter did not start")
    p31 = read_p31_summary(repeat_dir / "p31", performance_eligible=mode == "formal")
    if repeat_admission is not None:
        integrity = p31.get("integrity_guard")
        if (
            not isinstance(integrity, dict)
            or integrity.get("state") != "PASS"
            or integrity.get("consumer") != "P10"
            or integrity.get("lease_sha256")
            != repeat_admission["receipt"]["lease_sha256"]
        ):
            raise ContractError(
                f"{system['id']} repeat {repeat_index}: P31 integrity guard binding drift"
            )
    validated = validate_adapter_outputs(
        output_dir=adapter_output,
        request=request,
        system=effective_system,
        truth_rows=truth_rows,
        max_timeouts=suite["protocol"]["max_timeouts"],
    )
    if mode == "formal" and system["id"] in {"seml0", "aster"}:
        validate_adapter_p31_binding(
            validated.get("adapter_provenance"),
            p31,
            system_id=system["id"],
        )
    elif mode == "formal" and system["id"] == "neo4j":
        validate_neo4j_p31_binding(validated.get("adapter_provenance"), p31)
    elif mode == "formal" and system["id"] == "nebulagraph":
        validate_nebulagraph_p31_binding(validated.get("adapter_provenance"), p31)
    elif mode == "formal" and system["id"] == "livegraph":
        validate_livegraph_p31_binding(validated.get("adapter_provenance"), p31)
        provenance_path = adapter_output / "adapter-provenance.json"
        validated["livegraph_post_p31_store_seal"] = publish_livegraph_post_p31_store_seal(
            provenance_path,
            validated["adapter_provenance"],
            p31,
            repeat_dir / "livegraph-post-p31-store-seal.json",
        )
    validated["p31"] = p31
    if neo4j_audits is not None:
        validated["neo4j_store_audit"] = {
            "pre": {
                "path": str(neo4j_audits["pre_path"].resolve()),
                "sha256": sha256_file(neo4j_audits["pre_path"]),
            },
            "post_stop": {
                "path": str(neo4j_audits["post_stop_path"].resolve()),
                "sha256": sha256_file(neo4j_audits["post_stop_path"]),
            },
            "immutable_unchanged": True,
        }
    if neo4j_lifecycle is not None:
        validated["neo4j_lifecycle"] = {
            "launch": {
                "path": str(Path(neo4j_lifecycle["launch_path"]).resolve()),
                "sha256": neo4j_lifecycle["launch_sha256"],
            },
            "stop": {
                "path": str(Path(neo4j_lifecycle["stop_path"]).resolve()),
                "sha256": neo4j_lifecycle["stop_sha256"],
            },
            "graceful_stop": True,
        }
    if nebulagraph_lifecycle is not None:
        validated["nebulagraph_lifecycle"] = {
            "controller": dict(nebulagraph_lifecycle["controller"]),
            "sealed_admission": {
                "path": str(Path(nebulagraph_lifecycle["sealed_admission_path"]).resolve()),
                "sha256": nebulagraph_lifecycle["sealed_admission_sha256"],
            },
            "preflight": {
                "path": str(Path(nebulagraph_lifecycle["preflight_path"]).resolve()),
                "sha256": nebulagraph_lifecycle["preflight_sha256"],
            },
            "start": {
                "path": str(Path(nebulagraph_lifecycle["start_path"]).resolve()),
                "sha256": nebulagraph_lifecycle["start_sha256"],
            },
            "live_gate": {
                "path": str(Path(nebulagraph_lifecycle["start_path"]).resolve()),
                "sha256": nebulagraph_lifecycle["start_sha256"],
                "selector": "live_gate",
            },
            "stop": {
                "path": str(Path(nebulagraph_lifecycle["stop_path"]).resolve()),
                "sha256": nebulagraph_lifecycle["stop_sha256"],
            },
            "graceful_stop": True,
        }
    validated["request"] = {
        "path": str(request_path.resolve()),
        "sha256": sha256_file(request_path),
    }
    if repeat_admission is not None and repeat_admission_path is not None:
        validated["batch_admission"] = {
            "protocol": "batch-lease-v2",
            "artifact": artifact_ref(repeat_admission_path),
            "receipt": repeat_admission["receipt"],
            "integrity_guard": p31["integrity_guard"],
        }
    atomic_json(repeat_dir / "validated-result.json", validated)
    return validated


def _recover_neo4j_lifecycle(
    *,
    system: dict[str, Any],
    repeat_index: int,
    run_id: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Recover the published launch identity after any orchestration exception."""

    effective_system = materialize_external_repeat_binding(system, repeat_index)
    binding = effective_system.get("active_repeat_binding")
    if not isinstance(binding, dict):
        raise ContractError("cannot recover failed Neo4j repeat binding")
    receipt_root = Path(binding["repeat_root"]).resolve() / "p10-lifecycle" / run_id
    launch_path = receipt_root / "launch-receipt.json"
    if not launch_path.is_file():
        return effective_system, None
    launch = validate_launch_receipt(
        read_json(launch_path, "Neo4j runtime launch receipt"),
        verify_artifacts=True,
        expected_host=neo4j_current_host(),
    )
    lifecycle: dict[str, Any] = {
        "receipt_root": receipt_root,
        "launch_path": launch_path,
        "launch_sha256": sha256_file(launch_path),
        "launch": launch,
    }
    stop_path = receipt_root / "stop-receipt.json"
    if stop_path.is_file():
        try:
            stop = validate_stop_receipt(
                read_json(stop_path, "Neo4j runtime stop receipt"),
                verify_artifacts=True,
                expected_host=neo4j_current_host(),
                launch_document=launch,
            )
        except (ContractError, OSError, ValueError):
            # The launch identity remains sufficient for exact-target cleanup.
            pass
        else:
            lifecycle.update(
                {
                    "stop_path": stop_path,
                    "stop_sha256": sha256_file(stop_path),
                    "stop": stop,
                }
            )
    return effective_system, lifecycle


def execute_repeat(
    *,
    suite: dict[str, Any],
    system: dict[str, Any],
    truth_rows: list[dict[str, int]],
    repeat_index: int,
    run_root: Path,
    resolved_manifest: Path,
    p31_wrapper: Path,
    mode: str,
    admission: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute one repeat and fail-closed cleanup every launched Neo4j failure."""

    try:
        return _execute_repeat_impl(
            suite=suite,
            system=system,
            truth_rows=truth_rows,
            repeat_index=repeat_index,
            run_root=run_root,
            resolved_manifest=resolved_manifest,
            p31_wrapper=p31_wrapper,
            mode=mode,
            admission=admission,
        )
    except BaseException as failure:
        if mode == "formal" and system.get("id") == "neo4j":
            repeat_dir = run_root / "systems" / "neo4j" / f"repeat-{repeat_index:02d}"
            repeat_dir.mkdir(parents=True, exist_ok=True)
            try:
                effective_system, lifecycle = _recover_neo4j_lifecycle(
                    system=system,
                    repeat_index=repeat_index,
                    run_id=run_root.name,
                )
            except BaseException as recovery_error:
                atomic_json(
                    repeat_dir / "neo4j-failure-recovery-error.json",
                    {
                        "schema_version": "cidr-p10-neo4j-failure-recovery-error-v1",
                        "primary_failure": {
                            "type": type(failure).__name__,
                            "message": str(failure),
                        },
                        "recovery_failure": {
                            "type": type(recovery_error).__name__,
                            "message": str(recovery_error),
                        },
                    },
                )
                if hasattr(failure, "add_note"):
                    failure.add_note(
                        f"Neo4j lifecycle recovery also failed: "
                        f"{type(recovery_error).__name__}: {recovery_error}"
                    )
            else:
                preserve_failed_neo4j_repeat(
                    system=effective_system,
                    repeat_dir=repeat_dir,
                    lifecycle=lifecycle,
                    failure=failure,
                )
        raise


def flatten_repeat(result: dict[str, Any]) -> dict[str, Any]:
    resources = result["p31"]["resources"]
    disk = result["p31"]["disk"]
    return {
        **{column: result.get(column, "") for column in REPEAT_COLUMNS},
        "peak_rss_bytes": resources.get("peak_rss_bytes", ""),
        "peak_pss_bytes": resources.get("peak_pss_bytes", ""),
        "process_user_cpu_s": resources.get("process_user_cpu_s", ""),
        "process_sys_cpu_s": resources.get("process_sys_cpu_s", ""),
        "process_read_bytes": resources.get("process_read_bytes", ""),
        "process_write_bytes": resources.get("process_write_bytes", ""),
        "peak_store_total_bytes": disk.get("peak_store_total_bytes", ""),
        "peak_temp_bytes": disk.get("peak_temp_bytes", ""),
        "p31_run_dir": result["p31"]["run_dir"],
    }


def numeric(rows: list[dict[str, Any]], key: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        value = row.get(key, "")
        if value == "":
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError) as exc:
            raise ContractError(f"cannot aggregate non-numeric {key}={value!r}") from exc
    return values


def aggregate_systems(repeat_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for system_id in FROZEN_SYSTEM_GROUPS:
        rows = [row for row in repeat_rows if row["system_id"] == system_id]
        if not rows:
            continue
        cpu_totals = [
            float(row.get("process_user_cpu_s") or 0) + float(row.get("process_sys_cpu_s") or 0)
            for row in rows
        ]
        import_wall = numeric(rows, "import_wall_s")
        import_cpu = [
            float(row["import_user_cpu_s"]) + float(row["import_system_cpu_s"])
            for row in rows
            if row.get("import_user_cpu_s", "") != ""
            and row.get("import_system_cpu_s", "") != ""
        ]
        output.append(
            {
                "system_id": system_id,
                "display_name": rows[0]["display_name"],
                "group": rows[0]["group"],
                "system_version": rows[0]["system_version"],
                "process_lifetime": rows[0]["process_lifetime"],
                "repeats": len(rows),
                "query_count": rows[0]["query_count"],
                "timeout_queries_total": sum(int(row["timeout_queries"]) for row in rows),
                "mismatch_queries_total": sum(int(row["mismatch_queries"]) for row in rows),
                "median_warmup_s": statistics.median(numeric(rows, "warmup_s")),
                "median_measurement_s": statistics.median(numeric(rows, "measurement_s")),
                "median_import_wall_s": statistics.median(import_wall) if import_wall else "",
                "median_import_cpu_s": statistics.median(import_cpu) if import_cpu else "",
                "max_import_store_logical_bytes": max(
                    numeric(rows, "import_store_logical_bytes"), default=""
                ),
                "max_import_store_allocated_bytes": max(
                    numeric(rows, "import_store_allocated_bytes"), default=""
                ),
                "median_qps": statistics.median(numeric(rows, "qps")),
                "median_latency_p50_us": statistics.median(numeric(rows, "latency_p50_us")),
                "median_latency_p95_us": statistics.median(numeric(rows, "latency_p95_us")),
                "median_latency_p99_us": statistics.median(numeric(rows, "latency_p99_us")),
                "max_peak_rss_bytes": max(numeric(rows, "peak_rss_bytes"), default=""),
                "max_peak_pss_bytes": max(numeric(rows, "peak_pss_bytes"), default=""),
                "median_process_cpu_s": statistics.median(cpu_totals),
                "max_peak_store_total_bytes": max(numeric(rows, "peak_store_total_bytes"), default=""),
            }
        )
    return output


def validate_neo4j_driver_cross_repeat(results: list[dict[str, Any]], expected_repeats: int) -> None:
    neo4j_results = [result for result in results if result.get("system_id") == "neo4j"]
    if not neo4j_results:
        return
    if len(neo4j_results) != expected_repeats:
        raise ContractError("formal Neo4j driver binding lacks one result per repeat")
    bindings: list[tuple[object, ...]] = []
    launch_receipts: list[dict[str, Any]] = []
    for result in neo4j_results:
        provenance = result.get("adapter_provenance")
        driver = provenance.get("python_driver") if isinstance(provenance, dict) else None
        if not isinstance(driver, dict):
            raise ContractError("formal Neo4j result lacks Python driver provenance")
        bindings.append(
            (
                driver.get("version"),
                driver.get("expected_package_tree_sha256"),
                driver.get("package_tree_sha256"),
                driver.get("file_count"),
                driver.get("total_bytes"),
            )
        )
        lifecycle = provenance.get("container_lifecycle")
        before = lifecycle.get("before") if isinstance(lifecycle, dict) else None
        store = provenance.get("store")
        lineage = store.get("lineage") if isinstance(store, dict) else None
        reference = store.get("reference") if isinstance(store, dict) else None
        if not isinstance(before, dict) or not isinstance(lineage, dict) or not isinstance(reference, dict):
            raise ContractError("formal Neo4j result lacks repeat service/store identity")
        launch = provenance.get("launch")
        launch_receipt = launch.get("receipt") if isinstance(launch, dict) else None
        if not isinstance(launch_receipt, dict):
            raise ContractError("formal Neo4j result lacks its validated launch receipt")
        launch_receipts.append(launch_receipt)
    if len(set(bindings)) != 1:
        raise ContractError("Neo4j Python driver tree differs across repeats")
    validate_launch_receipts_independent(launch_receipts)


def validate_nebulagraph_cross_repeat(
    results: list[dict[str, Any]], expected_repeats: int, selected_indices: list[int]
) -> None:
    """Bind full formal NebulaGraph output to three independent clone/lifecycle identities."""

    nebula_results = [result for result in results if result.get("system_id") == "nebulagraph"]
    if not nebula_results:
        return
    if len(nebula_results) != len(selected_indices):
        raise ContractError("formal NebulaGraph lacks one result per selected repeat")
    specs: list[dict[str, Any]] = []
    sealed_values: list[dict[str, Any]] = []
    start_values: list[dict[str, Any]] = []
    stop_refs: list[tuple[str, str]] = []
    for result in nebula_results:
        provenance = result.get("adapter_provenance")
        lifecycle = provenance.get("cluster_lifecycle") if isinstance(provenance, dict) else None
        preflight = lifecycle.get("preflight") if isinstance(lifecycle, dict) else None
        start = lifecycle.get("start") if isinstance(lifecycle, dict) else None
        preflight_receipt = preflight.get("receipt") if isinstance(preflight, dict) else None
        start_receipt = start.get("receipt") if isinstance(start, dict) else None
        sealed = provenance.get("sealed_admission") if isinstance(provenance, dict) else None
        sealed_receipt = sealed.get("receipt") if isinstance(sealed, dict) else None
        result_lifecycle = result.get("nebulagraph_lifecycle")
        stop = result_lifecycle.get("stop") if isinstance(result_lifecycle, dict) else None
        if (
            not isinstance(preflight_receipt, dict)
            or not isinstance(start_receipt, dict)
            or not isinstance(sealed_receipt, dict)
            or not isinstance(stop, dict)
            or not isinstance(stop.get("path"), str)
            or not isinstance(stop.get("sha256"), str)
        ):
            raise ContractError("formal NebulaGraph cross-repeat lineage is incomplete")
        spec = preflight_receipt.get("spec")
        if not isinstance(spec, dict):
            raise ContractError("formal NebulaGraph preflight lacks its launch spec")
        specs.append(spec)
        sealed_values.append(sealed_receipt)
        start_values.append(start_receipt)
        stop_refs.append((str(Path(stop["path"]).resolve()), stop["sha256"]))
    observed_indices = sorted(spec.get("repeat_index") for spec in specs)
    if observed_indices != selected_indices:
        raise ContractError("NebulaGraph result repeat indices differ from the selected repeats")
    if selected_indices != list(range(1, expected_repeats + 1)):
        return
    cluster = _nebulagraph_cluster_module()
    cluster.validate_repeat_isolation(specs)
    run_ids = {spec.get("run_id") for spec in specs}
    if len(run_ids) != 1:
        raise ContractError("NebulaGraph repeat launch specs do not share one run ID")
    clone_receipts: list[str] = []
    clone_targets: list[str] = []
    clone_sources: set[tuple[str, str]] = set()
    store_shas: set[str] = set()
    dense_refs: set[tuple[str, str]] = set()
    for expected_repeat, sealed in zip(
        sorted(selected_indices), sorted(sealed_values, key=lambda value: value["validated"]["repeat_index"])
    ):
        lineage = sealed.get("lineage")
        validated = sealed.get("validated")
        artifacts = sealed.get("artifacts")
        if not isinstance(lineage, dict) or not isinstance(validated, dict) or not isinstance(artifacts, dict):
            raise ContractError("NebulaGraph sealed admission lineage is malformed")
        if (
            validated.get("repeat_index") != expected_repeat
            or lineage.get("clone_repeat_index") != expected_repeat
            or lineage.get("clone_run_id") not in run_ids
        ):
            raise ContractError("NebulaGraph sealed clone run/repeat lineage drift")
        clone_ref = lineage.get("clone_receipt")
        target = lineage.get("clone_target")
        source = lineage.get("clone_source")
        store = sealed.get("store")
        tree = store.get("tree") if isinstance(store, dict) else None
        dense = artifacts.get("dataset")
        if not all(isinstance(value, dict) for value in (clone_ref, target, source, tree, dense)):
            raise ContractError("NebulaGraph sealed clone/tree/dense lineage is incomplete")
        clone_receipts.append(str(Path(str(clone_ref.get("path", ""))).resolve()))
        clone_targets.append(str(Path(str(target.get("path", ""))).resolve()))
        clone_sources.add((str(Path(str(source.get("path", ""))).resolve()), str(source.get("sha256"))))
        store_shas.add(str(tree.get("sha256")))
        dense_refs.add((str(Path(str(dense.get("path", ""))).resolve()), str(dense.get("sha256"))))
    if (
        len(set(clone_receipts)) != expected_repeats
        or len(set(clone_targets)) != expected_repeats
        or len(clone_sources) != 1
        or len(store_shas) != 1
        or len(dense_refs) != 1
        or len(stop_refs) != expected_repeats
        or len(set(stop_refs)) != expected_repeats
    ):
        raise ContractError("NebulaGraph three-repeat clone/dense/stop lineage is not independent")
    container_ids = [
        start["containers"][role]["container_id"]
        for start in start_values
        for role in ("metad", "storaged", "graphd")
    ]
    network_ids = [start["network"]["network_id"] for start in start_values]
    if len(container_ids) != len(set(container_ids)) or len(network_ids) != len(set(network_ids)):
        raise ContractError("NebulaGraph three-repeat Docker IDs are reused")


def run(args: argparse.Namespace) -> int:
    manifest_path = args.manifest.resolve()
    run_root = args.run_root.resolve()
    p31_wrapper = (args.p31_wrapper or DEFAULT_P31).resolve()
    if not run_root.is_absolute():
        raise ContractError("--run-root must be absolute")
    if run_root.exists() and any(run_root.iterdir()):
        raise ContractError(f"refusing non-empty run root: {run_root}")
    suite, truth_rows = load_suite_manifest(
        manifest_path,
        repo_root=REPO_ROOT,
        run_root=run_root,
        mode=args.mode,
        manifest_base_dir=getattr(args, "manifest_base_dir", None),
    )
    selected = select_systems(suite, args.system, args.group)
    repeat_indices = select_repeat_indices(suite, args.repeat_index)
    for required_executable in (p31_wrapper, P31_ADAPTER_WRAPPER):
        if not required_executable.is_file() or not os.access(required_executable, os.X_OK):
            raise ContractError(f"required wrapper is missing or not executable: {required_executable}")
    admission: dict[str, Any] | None = None
    if args.mode == "formal":
        admission = verify_formal_preflight(
            run_root,
            p31_wrapper,
            batch_lease=args.batch_lease,
            batch_gate_tool=args.batch_gate_tool,
            legacy_v1_clean_ready=args.legacy_v1_clean_ready,
            compatibility_clean_ready=args.clean_ready_file,
        )
    elif any(
        value is not None
        for value in (
            args.clean_ready_file,
            args.legacy_v1_clean_ready,
            args.batch_lease,
            args.batch_gate_tool,
        )
    ):
        raise ContractError("admission options are only valid in formal mode")

    claim_empty_directory(
        run_root,
        claim_name="RUN-CLAIM.json",
        claim={
            "schema_version": "cidr-p10-run-claim-v1",
            "state": "CLAIMED",
            "claim_id": secrets.token_hex(16),
            "claimed_at_utc": utc_now(),
            "owner_pid": os.getpid(),
            "mode": args.mode,
            "manifest_path": str(manifest_path),
            "selected_systems": [system["id"] for system in selected],
            "selected_repeat_indices": repeat_indices,
        },
        context="P10 run root",
    )
    running_path = run_root / "RUNNING"
    resolved_manifest = run_root / "resolved-suite-manifest.json"
    repeat_results: list[dict[str, Any]] = []
    suite_admission_path: Path | None = None
    repeat_integrity_path: Path | None = None
    try:
        atomic_json(
            running_path,
            {
                "state": "RUNNING",
                "started_at_utc": utc_now(),
                "mode": args.mode,
                "selected_systems": [system["id"] for system in selected],
                "selected_repeat_indices": repeat_indices,
            },
        )
        atomic_json(resolved_manifest, suite)
        if admission is not None and admission.get("protocol") == "batch-lease-v2":
            suite_admission_path = run_root / "batch-lease-admission.json"
            atomic_json(
                suite_admission_path,
                {
                    "schema_version": "cidr-p10-suite-batch-admission-v2",
                    "state": "PASS",
                    "protocol": "batch-lease-v2",
                    "consumer": "P10",
                    "receipt": admission["suite_receipt"],
                    "validator_argv": admission["suite_validator_argv"],
                    "lease": artifact_ref(admission["lease"]),
                    "gate_tool": artifact_ref(admission["gate_tool"]),
                    "anchor_binary": artifact_ref(admission["anchor_binary"]),
                },
            )
        for system in selected:
            for repeat_index in repeat_indices:
                repeat_results.append(
                    execute_repeat(
                        suite=suite,
                        system=system,
                        truth_rows=truth_rows,
                        repeat_index=repeat_index,
                        run_root=run_root,
                        resolved_manifest=resolved_manifest,
                        p31_wrapper=p31_wrapper,
                        mode=args.mode,
                        admission=admission,
                    )
                )
        if args.mode == "formal":
            validate_neo4j_driver_cross_repeat(repeat_results, len(repeat_indices))
            validate_nebulagraph_cross_repeat(
                repeat_results, suite["protocol"]["repeats"], repeat_indices
            )
        repeat_rows = [flatten_repeat(result) for result in repeat_results]
        repeat_path = run_root / "repeat-results.tsv"
        write_tsv(repeat_path, REPEAT_COLUMNS, repeat_rows)
        system_rows = aggregate_systems(repeat_rows)
        system_path = run_root / "system-results.tsv"
        write_tsv(system_path, SYSTEM_COLUMNS, system_rows)
        if admission is not None and admission.get("protocol") == "batch-lease-v2":
            repeat_integrity_path = run_root / "repeat-integrity.json"
            atomic_json(
                repeat_integrity_path,
                {
                    "schema_version": "cidr-p10-repeat-integrity-index-v2",
                    "state": "PASS",
                    "lease_sha256": admission["suite_receipt"]["lease_sha256"],
                    "repeats": [
                        {
                            "system_id": result["system_id"],
                            "repeat_index": result["repeat_index"],
                            "admission_sha256": result["batch_admission"]["artifact"]["sha256"],
                            "guard_status_sha256": result["batch_admission"]["integrity_guard"]
                            ["evidence"]["status"]["sha256"],
                            "guard_samples_sha256": result["batch_admission"]["integrity_guard"]
                            ["evidence"]["samples"]["sha256"],
                            "guard_ready_sha256": result["batch_admission"]["integrity_guard"]
                            ["evidence"]["ready"]["sha256"],
                            "command_release_sha256": result["batch_admission"]
                            ["integrity_guard"]["evidence"]["release"]["sha256"],
                            "p31_validation_sha256": result["p31"]["validation_sha256"],
                            "p31_done_sha256": result["p31"]["done_sha256"],
                        }
                        for result in repeat_results
                    ],
                },
            )
        repeat_evidence: list[dict[str, Any]] = []
        if args.mode == "formal":
            for result in repeat_results:
                if result.get("system_id") != "livegraph":
                    continue
                repeat_index = int(result["repeat_index"])
                repeat_dir = run_root / "systems" / "livegraph" / f"repeat-{repeat_index:02d}"

                def evidence_ref(path: Path) -> dict[str, Any]:
                    if not path.is_file() or path.is_symlink():
                        raise ContractError(f"LiveGraph repeat evidence is missing: {path}")
                    return {
                        "path": str(path.resolve()),
                        "size_bytes": path.stat().st_size,
                        "sha256": sha256_file(path),
                    }

                output = repeat_dir / "adapter-output"
                repeat_evidence.append(
                    {
                        "system_id": "livegraph",
                        "repeat_index": repeat_index,
                        "validated_result": evidence_ref(repeat_dir / "validated-result.json"),
                        "adapter_provenance": evidence_ref(output / "adapter-provenance.json"),
                        "adapter_stages": evidence_ref(output / "adapter-stage-events.jsonl"),
                        "worker_start": evidence_ref(output / "worker-start.json"),
                        "worker_exit": evidence_ref(output / "worker-exit.json"),
                        "worker_stdout": evidence_ref(output / "livegraph-worker.stdout.log"),
                        "worker_stderr": evidence_ref(output / "livegraph-worker.stderr.log"),
                        "post_p31_store_seal": evidence_ref(
                            repeat_dir / "livegraph-post-p31-store-seal.json"
                        ),
                    }
                )
        complete_suite = (
            {system["id"] for system in selected} == set(FROZEN_SYSTEM_GROUPS)
            and repeat_indices == list(range(1, suite["protocol"]["repeats"] + 1))
        )
        summary = {
            "schema_version": "cidr-p10-suite-result-v1",
            "state": "PASS",
            "mode": args.mode,
            "performance_eligible": args.mode == "formal",
            "fixture_only": suite["fixture_only"],
            "suite_id": suite["suite_id"],
            "complete_frozen_suite": complete_suite,
            "selected_systems": [system["id"] for system in selected],
            "selected_repeat_indices": repeat_indices,
            "system_count": len(selected),
            "repeat_count": len(repeat_results),
            "group_policy": GROUP_POLICY,
            "cross_group_speedups_allowed": False,
            "groups": {
                "embedded": [system["id"] for system in selected if system["group"] == "embedded"],
                "client-server": [system["id"] for system in selected if system["group"] == "client-server"],
            },
            "truth": suite["truth"],
            "protocol": suite["protocol"],
            "resolved_manifest_sha256": sha256_file(resolved_manifest),
            "repeat_results": {"path": str(repeat_path), "sha256": sha256_file(repeat_path)},
            "system_results": {"path": str(system_path), "sha256": sha256_file(system_path)},
            "repeat_evidence": repeat_evidence,
            "completed_at_utc": utc_now(),
        }
        if admission is not None:
            summary["admission_protocol"] = admission["protocol"]
        if suite_admission_path is not None and repeat_integrity_path is not None:
            summary["batch_lease"] = artifact_ref(admission["lease"])
            summary["suite_batch_admission"] = artifact_ref(suite_admission_path)
            summary["repeat_integrity"] = artifact_ref(repeat_integrity_path)
        summary_path = run_root / "suite-summary.json"
        atomic_json(summary_path, summary)
        marker_name = "DONE" if complete_suite else "PARTIAL-DONE"
        marker = {
            "state": "PASS",
            "complete_frozen_suite": complete_suite,
            "performance_eligible": args.mode == "formal",
            "summary_sha256": sha256_file(summary_path),
            "resolved_manifest_sha256": sha256_file(resolved_manifest),
        }
        if admission is not None:
            marker["admission_protocol"] = admission["protocol"]
        if suite_admission_path is not None and repeat_integrity_path is not None:
            marker["batch_lease_sha256"] = sha256_file(admission["lease"])
            marker["suite_batch_admission_sha256"] = sha256_file(suite_admission_path)
            marker["repeat_integrity_sha256"] = sha256_file(repeat_integrity_path)
        atomic_json(run_root / marker_name, marker)
        running_path.unlink()
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    except BaseException as exc:
        done = run_root / "DONE"
        partial = run_root / "PARTIAL-DONE"
        for marker in (done, partial):
            if marker.exists():
                marker.unlink()
        atomic_json(
            run_root / "FAILED",
            {
                "state": "FAILED",
                "failed_at_utc": utc_now(),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "validated_repeats": len(repeat_results),
                "selected_repeat_indices": repeat_indices,
            },
        )
        if running_path.exists():
            running_path.unlink()
        raise


def validate_only(args: argparse.Namespace) -> int:
    suite, truth_rows = load_suite_manifest(
        args.manifest.resolve(),
        repo_root=REPO_ROOT,
        run_root=args.run_root.resolve(),
        mode=args.mode,
        manifest_base_dir=getattr(args, "manifest_base_dir", None),
    )
    output = {
        "state": "VALID",
        "mode": args.mode,
        "suite_id": suite["suite_id"],
        "systems": [system["id"] for system in suite["systems"]],
        "groups": {system_id: group for system_id, group in FROZEN_SYSTEM_GROUPS.items()},
        "query_count": len(truth_rows),
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("validate", "run"):
        command = subparsers.add_parser(action)
        command.add_argument("--manifest", required=True, type=Path)
        command.add_argument("--manifest-base-dir", type=Path)
        command.add_argument("--run-root", required=True, type=Path)
        command.add_argument("--mode", choices=("fixture", "formal"), required=True)
        if action == "run":
            command.add_argument("--p31-wrapper", type=Path)
            command.add_argument("--clean-ready-file", type=Path)
            command.add_argument("--legacy-v1-clean-ready", type=Path)
            command.add_argument("--batch-lease", type=Path)
            command.add_argument("--batch-gate-tool", type=Path)
            command.add_argument("--system", action="append", default=[])
            command.add_argument("--group", choices=("embedded", "client-server"))
            command.add_argument("--repeat-index", action="append", type=int, default=[])
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return validate_only(args) if args.action == "validate" else run(args)
    except ContractError as exc:
        print(f"P10/P11 contract failure: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - never turn an unexpected failure into PASS
        print(f"P10/P11 fatal failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
