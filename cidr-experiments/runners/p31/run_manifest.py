#!/usr/bin/env python3
"""Create and update the immutable-shape manifest for one formal run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from resource_schema import MANIFEST_SCHEMA_VERSION, RESOURCE_SCHEMA_VERSION

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: object) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def command_output(command: list[str], cwd: Path | None = None) -> tuple[bool, str]:
    try:
        return (
            True,
            subprocess.check_output(
                command,
                cwd=str(cwd) if cwd else None,
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).strip(),
        )
    except (subprocess.SubprocessError, OSError):
        return False, ""


def normalize_sha(value: str | None) -> str:
    if not value:
        return ""
    lowered = value.strip().lower()
    if not SHA256_RE.fullmatch(lowered):
        raise ValueError(f"invalid SHA-256: {value!r}")
    return lowered


def artifact_ref(
    path_raw: str | None,
    explicit_sha: str | None = None,
    *,
    declare_file_sha: bool = False,
) -> dict[str, Any]:
    if not path_raw:
        return {"path": "", "kind": "unspecified", "exists": False, "size_bytes": None, "sha256": normalize_sha(explicit_sha)}
    path = Path(path_raw).resolve()
    exists = path.exists()
    if path.is_file():
        kind = "file"
        size: int | None = path.stat().st_size
        explicit_digest = normalize_sha(explicit_sha)
        if declare_file_sha:
            if not explicit_digest:
                raise ValueError(f"declared file SHA-256 is required: {path}")
            digest = explicit_digest
        else:
            actual_digest = sha256_file(path)
            if explicit_digest and explicit_digest != actual_digest:
                raise ValueError(f"explicit SHA-256 does not match file: {path}")
            digest = actual_digest
    elif path.is_dir():
        kind = "directory"
        size = None
        digest = normalize_sha(explicit_sha)
    else:
        kind = "missing"
        size = None
        digest = normalize_sha(explicit_sha)
    result = {"path": str(path), "kind": kind, "exists": exists, "size_bytes": size, "sha256": digest}
    if declare_file_sha:
        result["content_sha256_mode"] = "declared-no-read-v1"
    return result


def parse_named_input(raw: str) -> tuple[str, str, str]:
    try:
        label, remainder = raw.split("=", 1)
        path, digest = remainder.rsplit("=", 1)
    except ValueError as exc:
        raise ValueError(f"invalid LABEL=PATH=SHA256 input: {raw!r}") from exc
    if not LABEL_RE.fullmatch(label) or not path:
        raise ValueError(f"invalid named input label/path: {raw!r}")
    normalized = normalize_sha(digest)
    if not normalized:
        raise ValueError(f"named input requires SHA-256: {raw!r}")
    return label, path, normalized


def host_facts() -> dict[str, Any]:
    cpu_model = ""
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("model name"):
                cpu_model = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    mem_total = 0
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                mem_total = int(line.split()[1]) * 1024
                break
    except (OSError, ValueError, IndexError):
        pass
    pidstat_ok, pidstat_version = command_output(["pidstat", "-V"])
    iostat_ok, iostat_version = command_output(["iostat", "-V"])
    facts = {
        "hostname": platform.node(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "cpu_model": cpu_model,
        "logical_cpu_count": os.cpu_count(),
        "mem_total_bytes": mem_total,
        "pidstat_version_command_ok": pidstat_ok,
        "pidstat_version": pidstat_version,
        "iostat_version_command_ok": iostat_ok,
        "iostat_version": iostat_version,
    }
    facts["fingerprint_sha256"] = hashlib.sha256(
        json.dumps(facts, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return facts


def device_facts(device: str) -> dict[str, Any]:
    root = Path("/sys/class/block") / device

    def read(relative: str) -> str:
        try:
            return (root / relative).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    facts = {
        "device": device,
        "exists": root.exists(),
        "model": read("device/model"),
        "firmware_rev": read("device/firmware_rev"),
        "logical_block_size": read("queue/logical_block_size"),
        "physical_block_size": read("queue/physical_block_size"),
        "rotational": read("queue/rotational"),
        "scheduler": read("queue/scheduler"),
    }
    facts["fingerprint_sha256"] = hashlib.sha256(
        json.dumps(facts, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return facts


def parse_root_spec(raw: str, role: str) -> dict[str, str]:
    if "=" not in raw:
        raise ValueError(f"expected LABEL=PATH, got {raw!r}")
    label, path = raw.split("=", 1)
    if not LABEL_RE.fullmatch(label) or not path:
        raise ValueError(f"invalid LABEL=PATH: {raw!r}")
    return {"role": role, "label": label, "path": str(Path(path).resolve())}


def create_manifest(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "run-manifest.json"
    if manifest_path.exists():
        raise SystemExit(f"refusing to overwrite {manifest_path}")
    repo = args.repo_root.resolve()
    command_file = args.command_file.resolve()
    if args.interval <= 0 or args.disk_interval <= 0 or args.ready_timeout <= 0:
        raise ValueError("collector intervals must be positive")
    if args.min_samples < 2:
        raise ValueError("min_samples must be at least 2")
    if not args.device:
        raise ValueError("device must be non-empty")
    if not args.data_mount.resolve().is_dir():
        raise ValueError(f"data mount is not a directory: {args.data_mount.resolve()}")
    if len(args.container) != len(set(args.container)):
        raise ValueError("duplicate container name")
    if any(not name.strip() for name in args.container):
        raise ValueError("container names must be non-empty")
    if len(args.extra_pid) != len(set(args.extra_pid)) or any(pid <= 0 for pid in args.extra_pid):
        raise ValueError("extra PIDs must be unique positive integers")
    batch_values = (
        args.batch_lease,
        args.batch_gate_tool,
        args.batch_consumer,
        args.batch_anchor_binary,
    )
    if any(value is not None for value in batch_values) and not all(
        value is not None for value in batch_values
    ):
        raise ValueError("batch gate options must be supplied together")
    batch_gate = None
    if all(value is not None for value in batch_values):
        if args.batch_consumer not in {"P10", "P20"}:
            raise ValueError("batch consumer must be P10 or P20")
        lease_ref = artifact_ref(str(args.batch_lease.resolve()))
        tool_ref = artifact_ref(str(args.batch_gate_tool.resolve()))
        anchor_ref = artifact_ref(str(args.batch_anchor_binary.resolve()))
        for name, reference in (
            ("lease", lease_ref),
            ("gate tool", tool_ref),
            ("anchor binary", anchor_ref),
        ):
            if reference["kind"] != "file":
                raise ValueError(f"batch {name} is not a current file")
        batch_gate = {
            "protocol_version": "short-clean-window-v2",
            "consumer": args.batch_consumer,
            "lease": lease_ref,
            "gate_tool": tool_ref,
            "anchor_binary": anchor_ref,
            "integrity_guard_required": True,
        }
    named_specs = [parse_named_input(raw) for raw in args.input]
    named_labels = [label for label, _, _ in named_specs]
    reserved_inputs = {"binary", "dataset", "truth", "query_or_trace", "config"}
    if len(named_labels) != len(set(named_labels)):
        raise ValueError("duplicate named provenance input")
    if reserved_inputs.intersection(named_labels):
        raise ValueError("named provenance input collides with a reserved input")
    git_sha_ok, git_sha = command_output(["git", "rev-parse", "HEAD"], repo)
    git_status_ok, git_status = command_output(
        ["git", "status", "--porcelain=v1", "--untracked-files=normal"], repo
    )
    git_status_sha = hashlib.sha256((git_status + "\n").encode("utf-8")).hexdigest()
    findmnt_ok, findmnt = command_output(
        ["findmnt", "-no", "SOURCE,FSTYPE,OPTIONS", "--target", str(args.data_mount.resolve())]
    )
    stores = [parse_root_spec(raw, "store") for raw in args.store]
    stores.extend(parse_root_spec(raw, "temp") for raw in args.temp)
    if not any(root["role"] == "store" for root in stores):
        raise ValueError("at least one store root is required")
    identities = [(root["role"], root["label"]) for root in stores]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate disk-root role/label")
    for index, left in enumerate(stores):
        left_path = Path(left["path"])
        for right in stores[index + 1 :]:
            right_path = Path(right["path"])
            if left_path == right_path or left_path in right_path.parents or right_path in left_path.parents:
                raise ValueError(
                    f"overlapping disk roots are not allowed: {left_path} and {right_path}"
                )
    collector_path = args.collector.resolve()
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "resource_schema_version": RESOURCE_SCHEMA_VERSION,
        "run_id": args.run_id,
        "task_id": args.task_id,
        "state": "PREPARED",
        "performance_eligible_declared": args.performance_eligible == "true",
        "created_at_utc": utc_now(),
        "started_at_utc": None,
        "ended_at_utc": None,
        "root_pid": None,
        "command_exit_code": None,
        "collector_exit_code": None,
        "wrapper_signal": None,
        "repo": {
            "root": str(repo),
            "git_sha": git_sha,
            "git_sha_command_ok": git_sha_ok,
            "dirty": bool(git_status),
            "status_command_ok": git_status_ok,
            "status_sha256": git_status_sha,
            "status_lines": git_status.splitlines(),
        },
        "host": host_facts(),
        "storage_host": {
            "device": device_facts(args.device),
            "data_mount": str(args.data_mount.resolve()),
            "findmnt_command_ok": findmnt_ok,
            "findmnt": findmnt,
        },
        "command": {
            "path": str(command_file),
            "sha256": sha256_file(command_file),
        },
        "harness": {
            "wrapper": artifact_ref(str(args.wrapper.resolve())),
            "manifest_tool": artifact_ref(str(args.manifest_tool.resolve())),
            "collector": artifact_ref(str(collector_path)),
            "validator": artifact_ref(str(args.validator.resolve())),
        },
        "collector": {
            "path": str(collector_path),
            "sha256": sha256_file(collector_path),
            "device": args.device,
            "data_mount": str(args.data_mount.resolve()),
            "interval_s": args.interval,
            "disk_interval_s": args.disk_interval,
            "ready_timeout_s": args.ready_timeout,
            "min_samples": args.min_samples,
            "require_aux_tools": args.require_aux_tools == "true",
            "containers": args.container,
            "extra_pids": args.extra_pid,
        },
        "disk_roots": stores,
        "inputs": {
            "binary": artifact_ref(args.binary, args.binary_sha256),
            "dataset": artifact_ref(
                args.dataset,
                args.dataset_sha256,
                declare_file_sha=args.dataset_sha256_mode == "declared-no-read-v1",
            ),
            "truth": artifact_ref(args.truth, args.truth_sha256),
            "query_or_trace": artifact_ref(args.query_or_trace, args.query_or_trace_sha256),
            "config": artifact_ref(args.config, args.config_sha256),
            **{
                label: artifact_ref(path, digest)
                for label, path, digest in named_specs
            },
        },
        "execution_artifact": "execution.json",
        "artifacts": {},
        "summary": {},
        "validation": {"state": "NOT_RUN", "errors": [], "warnings": []},
    }
    if batch_gate is not None:
        manifest["batch_gate"] = batch_gate
    atomic_json(manifest_path, manifest)
    return 0


def record_execution(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    manifest_path = run_dir / "run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    execution = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "root_pid": args.root_pid,
        "started_at_utc": args.started_at_utc,
        "ended_at_utc": args.ended_at_utc,
        "command_exit_code": args.command_exit_code,
        "collector_exit_code": args.collector_exit_code,
        "wrapper_signal": args.wrapper_signal,
    }
    batch_execution_values = (
        args.command_release_at_utc,
        args.command_ended_at_utc,
        args.guard_exit_code,
    )
    if any(value is not None for value in batch_execution_values) and not all(
        value is not None for value in batch_execution_values
    ):
        raise ValueError("batch execution fields must be supplied together")
    if all(value is not None for value in batch_execution_values):
        execution.update(
            {
                "command_release_at_utc": args.command_release_at_utc,
                "command_ended_at_utc": args.command_ended_at_utc,
                "guard_exit_code": args.guard_exit_code,
            }
        )
    atomic_json(run_dir / "execution.json", execution)
    manifest.update(
        {
            "state": "VALIDATING" if args.command_exit_code == 0 and args.collector_exit_code == 0 else "FAILED_EXECUTION",
            "root_pid": args.root_pid,
            "started_at_utc": args.started_at_utc,
            "ended_at_utc": args.ended_at_utc,
            "command_exit_code": args.command_exit_code,
            "collector_exit_code": args.collector_exit_code,
            "wrapper_signal": args.wrapper_signal,
        }
    )
    if all(value is not None for value in batch_execution_values):
        manifest.update(
            {
                "command_release_at_utc": args.command_release_at_utc,
                "command_ended_at_utc": args.command_ended_at_utc,
                "guard_exit_code": args.guard_exit_code,
            }
        )
    atomic_json(manifest_path, manifest)
    return 0


def abort_manifest(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "run-manifest.json"
    if not manifest_path.exists():
        atomic_json(run_dir / "FAILED", {"state": "FAILED_WRAPPER", "reason": args.reason, "at_utc": utc_now()})
        return 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["state"] = "FAILED_WRAPPER"
    manifest["ended_at_utc"] = utc_now()
    manifest["validation"] = {"state": "FAILED", "errors": [args.reason], "warnings": []}
    atomic_json(manifest_path, manifest)
    atomic_json(run_dir / "FAILED", {"state": "FAILED_WRAPPER", "reason": args.reason, "at_utc": utc_now()})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="action", required=True)
    create = subs.add_parser("create")
    create.add_argument("--run-dir", required=True, type=Path)
    create.add_argument("--run-id", required=True)
    create.add_argument("--task-id", required=True)
    create.add_argument("--performance-eligible", choices=["true", "false"], required=True)
    create.add_argument("--repo-root", required=True, type=Path)
    create.add_argument("--command-file", required=True, type=Path)
    create.add_argument("--wrapper", required=True, type=Path)
    create.add_argument("--manifest-tool", required=True, type=Path)
    create.add_argument("--collector", required=True, type=Path)
    create.add_argument("--validator", required=True, type=Path)
    create.add_argument("--device", required=True)
    create.add_argument("--data-mount", required=True, type=Path)
    create.add_argument("--interval", required=True, type=float)
    create.add_argument("--disk-interval", required=True, type=float)
    create.add_argument("--ready-timeout", required=True, type=float)
    create.add_argument("--min-samples", required=True, type=int)
    create.add_argument("--require-aux-tools", choices=["true", "false"], required=True)
    create.add_argument("--store", action="append", default=[])
    create.add_argument("--temp", action="append", default=[])
    create.add_argument("--container", action="append", default=[])
    create.add_argument("--extra-pid", action="append", default=[], type=int)
    create.add_argument("--batch-lease", type=Path)
    create.add_argument("--batch-gate-tool", type=Path)
    create.add_argument("--batch-consumer")
    create.add_argument("--batch-anchor-binary", type=Path)
    for name in ("binary", "dataset", "truth", "query-or-trace", "config"):
        create.add_argument(f"--{name}")
        create.add_argument(f"--{name}-sha256")
    create.add_argument(
        "--dataset-sha256-mode",
        choices=("verify", "declared-no-read-v1"),
        default="verify",
    )
    create.add_argument("--input", action="append", default=[])
    create.set_defaults(handler=create_manifest)

    execution = subs.add_parser("execution")
    execution.add_argument("--run-dir", required=True, type=Path)
    execution.add_argument("--root-pid", required=True, type=int)
    execution.add_argument("--started-at-utc", required=True)
    execution.add_argument("--ended-at-utc", required=True)
    execution.add_argument("--command-exit-code", required=True, type=int)
    execution.add_argument("--collector-exit-code", required=True, type=int)
    execution.add_argument("--command-release-at-utc")
    execution.add_argument("--command-ended-at-utc")
    execution.add_argument("--guard-exit-code", type=int)
    execution.add_argument("--wrapper-signal", type=int)
    execution.set_defaults(handler=record_execution)

    abort = subs.add_parser("abort")
    abort.add_argument("--run-dir", required=True, type=Path)
    abort.add_argument("--reason", required=True)
    abort.set_defaults(handler=abort_manifest)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.handler(args))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"manifest error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
