#!/usr/bin/env python3
"""Tiny P31-shaped fixture wrapper; it never runs a real benchmark."""

import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main():
    argv = sys.argv[1:]
    if "--" not in argv:
        print("fixture P31 requires --", file=sys.stderr)
        return 64
    split = argv.index("--")
    options = argv[:split]
    command = argv[split + 1 :]
    values = {}
    flags = set()
    index = 0
    while index < len(options):
        key = options[index]
        if key == "--allow-missing-aux-tools":
            flags.add(key)
            index += 1
            continue
        if index + 1 >= len(options):
            print("fixture P31 missing value", file=sys.stderr)
            return 64
        values.setdefault(key, []).append(options[index + 1])
        index += 2
    run_root = Path(values["--run-dir"][-1])
    if run_root.exists():
        print("fixture P31 refuses existing run root", file=sys.stderr)
        return 73
    run_root.mkdir(parents=True)
    atomic_json(run_root / "p31-argv.json", {"argv": argv, "flags": sorted(flags)})
    (run_root / "command.txt").write_text(json.dumps(command) + "\n", encoding="utf-8")
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    (run_root / "command.stdout.log").write_text(result.stdout, encoding="utf-8")
    (run_root / "command.stderr.log").write_text(result.stderr, encoding="utf-8")
    if result.returncode != 0:
        atomic_json(run_root / "FAILED", {"state": "FAILED", "command_exit_code": result.returncode})
        return result.returncode
    input_map = {
        "binary": ("--binary", "--binary-sha256"),
        "dataset": ("--dataset", "--dataset-sha256"),
        "truth": ("--truth", "--truth-sha256"),
        "query_or_trace": ("--query-or-trace", "--query-or-trace-sha256"),
        "config": ("--config", "--config-sha256"),
    }
    inputs = {}
    for name, (path_key, sha_key) in input_map.items():
        inputs[name] = {
            "path": values[path_key][-1],
            "sha256": values[sha_key][-1],
            "exists": True,
        }
    fixture_artifacts = [
        "resource-samples.tsv",
        "disk-samples.tsv",
        "iostat-samples.tsv",
        "pidstat.raw",
        "iostat.raw",
        "collector-status.json",
        "collector.stdout.log",
        "collector.stderr.log",
        "execution.json",
    ]
    for name in fixture_artifacts:
        (run_root / name).write_text("fixture\n", encoding="utf-8")
    artifact_names = fixture_artifacts + [
        "command.stdout.log",
        "command.stderr.log",
        "command.txt",
    ]
    artifacts = {
        name: {"size_bytes": (run_root / name).stat().st_size, "sha256": sha256(run_root / name)}
        for name in artifact_names
    }
    stores = []
    for raw_store in values.get("--store", []):
        label, store_path = raw_store.split("=", 1)
        stores.append(
            {"role": "store", "label": label, "path": str(Path(store_path).resolve())}
        )
    git_sha = subprocess.check_output(
        ["git", "-C", values["--repo-root"][-1], "rev-parse", "HEAD"], text=True
    ).strip()
    manifest = {
        "schema_version": "cidr-run-manifest-v1",
        "resource_schema_version": "cidr-resource-v1",
        "state": "PASS",
        "task_id": values["--task-id"][-1],
        "run_id": values["--run-id"][-1],
        "ended_at_utc": now(),
        "performance_eligible_declared": values["--performance-eligible"][-1] == "true",
        "repo": {"root": values["--repo-root"][-1], "git_sha": git_sha, "dirty": False},
        "host": {"hostname": platform.node(), "fingerprint_sha256": "f" * 64},
        "collector": {"require_aux_tools": True},
        "disk_roots": stores,
        "inputs": inputs,
        "artifacts": artifacts,
        "summary": {
            "resources": {
                "process_user_cpu_s": 0.01,
                "process_sys_cpu_s": 0.002,
                "process_read_bytes": 4096,
                "process_write_bytes": 2048,
                "peak_rss_bytes": 1048576,
                "peak_pss_bytes": 786432,
                "peak_device_read_mib_s": 1.25,
                "peak_device_write_mib_s": 0.5,
            },
            "disk": {"peak_store_total_bytes": 8192},
        },
    }
    validation = {
        "schema_version": "cidr-run-manifest-v1",
        "state": "PASS",
        "errors": [],
        "warnings": [],
    }
    atomic_json(run_root / "run-manifest.json", manifest)
    atomic_json(run_root / "validation.json", validation)
    atomic_json(
        run_root / "DONE",
        {
            "state": "PASS",
            "manifest_sha256": sha256(run_root / "run-manifest.json"),
            "validation_sha256": sha256(run_root / "validation.json"),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
