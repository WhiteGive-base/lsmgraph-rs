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
import json
import os
import shutil
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    atomic_json,
    load_suite_manifest,
    read_p31_summary,
    sha256_file,
    validate_adapter_outputs,
    validate_adapter_p31_binding,
)


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
DEFAULT_P31 = REPO_ROOT / "cidr-experiments/runners/p31/run_with_resources.sh"
P31_ADAPTER_WRAPPER = SCRIPT_DIR / "run_adapter_with_p31.sh"

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


def verify_formal_preflight(run_root: Path, p31_wrapper: Path, clean_ready_file: Path | None) -> None:
    if p31_wrapper.resolve() != DEFAULT_P31.resolve():
        raise ContractError("formal mode forbids overriding the real P31 wrapper")
    if clean_ready_file is None:
        raise ContractError("formal mode requires --clean-ready-file")
    verify_clean_ready(clean_ready_file.resolve())
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
    if mode == "fixture":
        command.append("--allow-missing-aux-tools")
    timeout_binary = shutil.which("timeout")
    if timeout_binary is None:
        raise ContractError("GNU timeout is required to bound an adapter process")
    adapter_command = [
        system["adapter"]["path"],
        *system["adapter"]["args"],
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
) -> dict[str, Any]:
    repeat_dir = run_root / "systems" / system["id"] / f"repeat-{repeat_index:02d}"
    repeat_dir.mkdir(parents=True, exist_ok=False)
    adapter_output = repeat_dir / "adapter-output"
    adapter_output.mkdir()
    effective_system = materialize_fresh_repeat_roots(system, run_root, repeat_index)
    request = build_request(suite, effective_system, repeat_index, run_root.name, mode)
    request_path = repeat_dir / "adapter-request.json"
    atomic_json(request_path, request)
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
    )
    (repeat_dir / "orchestrator-command.json").write_text(
        json.dumps(command, indent=2) + "\n", encoding="utf-8"
    )
    with (repeat_dir / "p31-wrapper.stdout.log").open("wb") as stdout_handle, (
        repeat_dir / "p31-wrapper.stderr.log"
    ).open("wb") as stderr_handle:
        completed = subprocess.run(command, stdout=stdout_handle, stderr=stderr_handle, check=False)
    if completed.returncode != 0:
        raise ContractError(
            f"{system['id']} repeat {repeat_index}: P31/adapter exited {completed.returncode}; "
            f"see {repeat_dir / 'p31-wrapper.stderr.log'}"
        )
    p31 = read_p31_summary(repeat_dir / "p31", performance_eligible=mode == "formal")
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
    validated["p31"] = p31
    validated["request"] = {
        "path": str(request_path.resolve()),
        "sha256": sha256_file(request_path),
    }
    atomic_json(repeat_dir / "validated-result.json", validated)
    return validated


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


def run(args: argparse.Namespace) -> int:
    manifest_path = args.manifest.resolve()
    run_root = args.run_root.resolve()
    p31_wrapper = (args.p31_wrapper or DEFAULT_P31).resolve()
    if not run_root.is_absolute():
        raise ContractError("--run-root must be absolute")
    if run_root.exists() and any(run_root.iterdir()):
        raise ContractError(f"refusing non-empty run root: {run_root}")
    suite, truth_rows = load_suite_manifest(
        manifest_path, repo_root=REPO_ROOT, run_root=run_root, mode=args.mode
    )
    selected = select_systems(suite, args.system, args.group)
    for required_executable in (p31_wrapper, P31_ADAPTER_WRAPPER):
        if not required_executable.is_file() or not os.access(required_executable, os.X_OK):
            raise ContractError(f"required wrapper is missing or not executable: {required_executable}")
    if args.mode == "formal":
        verify_formal_preflight(run_root, p31_wrapper, args.clean_ready_file)
    elif args.clean_ready_file is not None:
        raise ContractError("--clean-ready-file is only valid in formal mode")

    run_root.mkdir(parents=True, exist_ok=True)
    running_path = run_root / "RUNNING"
    atomic_json(
        running_path,
        {
            "state": "RUNNING",
            "started_at_utc": utc_now(),
            "mode": args.mode,
            "selected_systems": [system["id"] for system in selected],
        },
    )
    resolved_manifest = run_root / "resolved-suite-manifest.json"
    atomic_json(resolved_manifest, suite)
    repeat_results: list[dict[str, Any]] = []
    try:
        for system in selected:
            for repeat_index in range(1, suite["protocol"]["repeats"] + 1):
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
                    )
                )
        repeat_rows = [flatten_repeat(result) for result in repeat_results]
        repeat_path = run_root / "repeat-results.tsv"
        write_tsv(repeat_path, REPEAT_COLUMNS, repeat_rows)
        system_rows = aggregate_systems(repeat_rows)
        system_path = run_root / "system-results.tsv"
        write_tsv(system_path, SYSTEM_COLUMNS, system_rows)
        complete_suite = {system["id"] for system in selected} == set(FROZEN_SYSTEM_GROUPS)
        summary = {
            "schema_version": "cidr-p10-suite-result-v1",
            "state": "PASS",
            "mode": args.mode,
            "performance_eligible": args.mode == "formal",
            "fixture_only": suite["fixture_only"],
            "suite_id": suite["suite_id"],
            "complete_frozen_suite": complete_suite,
            "selected_systems": [system["id"] for system in selected],
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
            "completed_at_utc": utc_now(),
        }
        summary_path = run_root / "suite-summary.json"
        atomic_json(summary_path, summary)
        marker_name = "DONE" if complete_suite else "PARTIAL-DONE"
        atomic_json(
            run_root / marker_name,
            {
                "state": "PASS",
                "complete_frozen_suite": complete_suite,
                "performance_eligible": args.mode == "formal",
                "summary_sha256": sha256_file(summary_path),
                "resolved_manifest_sha256": sha256_file(resolved_manifest),
            },
        )
        running_path.unlink()
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
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
            },
        )
        if running_path.exists():
            running_path.unlink()
        raise


def validate_only(args: argparse.Namespace) -> int:
    suite, truth_rows = load_suite_manifest(
        args.manifest.resolve(), repo_root=REPO_ROOT, run_root=args.run_root.resolve(), mode=args.mode
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
        command.add_argument("--run-root", required=True, type=Path)
        command.add_argument("--mode", choices=("fixture", "formal"), required=True)
        if action == "run":
            command.add_argument("--p31-wrapper", type=Path)
            command.add_argument("--clean-ready-file", type=Path)
            command.add_argument("--system", action="append", default=[])
            command.add_argument("--group", choices=("embedded", "client-server"))
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
