#!/usr/bin/env python3
"""Run one canonical P20 profile through the P31 resource wrapper.

The runner is deliberately fail-closed: it resolves the canonical profile in a
separate process, verifies frozen inputs and correctness evidence, clones a new
stage store, and invokes P31 with argv lists (never a shell command string).
"""

from __future__ import print_function

import argparse
import csv
import hashlib
import json
import os
import re
import signal
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


HERE = Path(__file__).resolve().parent
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
ALLOWED_RUNTIME_OVERRIDES = {"warmup-runs", "repeats"}
FORBIDDEN_ENVIRONMENT = {"SNB_SKIP_SEM_INDEX", "SNB_SKIP_ADJ_CACHE"}
CANONICAL_STAGES = {
    "sf10": ["A0", "A1", "A2", "A3", "A4", "A5", "A6"],
    "sf30": ["A0", "A2", "A4", "A6"],
}
CLEAN_WINDOW_MAX_AGE_SECONDS = 3600


class RunnerError(ValueError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def atomic_json(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_sha(value, label):
    lowered = value.strip().lower()
    if not SHA256_RE.fullmatch(lowered):
        raise RunnerError("{} must be a 64-character SHA-256".format(label))
    return lowered


def require_absolute(path, label):
    path = Path(path)
    if not path.is_absolute():
        raise RunnerError("{} must be absolute: {}".format(label, path))
    return path.resolve()


def artifact_row(name, path, expected_sha, require_file=False, require_dir=False):
    path = require_absolute(path, name)
    expected_sha = normalize_sha(expected_sha, name + " SHA-256")
    if not path.exists():
        raise RunnerError("{} does not exist: {}".format(name, path))
    if require_file and not path.is_file():
        raise RunnerError("{} must be a file: {}".format(name, path))
    if require_dir and not path.is_dir():
        raise RunnerError("{} must be a directory: {}".format(name, path))
    if path.is_file():
        actual = sha256_file(path)
        if actual != expected_sha:
            raise RunnerError("{} SHA-256 mismatch: expected {}, got {}".format(name, expected_sha, actual))
        verification = "computed-file"
        kind = "file"
    elif path.is_dir():
        # P31 treats directory hashes as externally frozen dataset/store IDs.
        # Record that distinction instead of pretending a byte hash was made.
        verification = "declared-directory-id"
        kind = "directory"
    else:
        raise RunnerError("{} has unsupported file type: {}".format(name, path))
    return {
        "name": name,
        "path": str(path),
        "kind": kind,
        "sha256": expected_sha,
        "verification": verification,
    }


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise RunnerError("duplicate JSON key: {}".format(key))
        value[key] = item
    return value


def read_json(path, label):
    try:
        value = json.loads(
            Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except (OSError, json.JSONDecodeError, RunnerError) as exc:
        raise RunnerError("cannot read {} JSON: {}".format(label, exc))
    if not isinstance(value, dict):
        raise RunnerError("{} must be a JSON object".format(label))
    return value


def validate_sample_plan(path):
    plan = read_json(path, "sample plan")
    if plan.get("version") != 1:
        raise RunnerError("sample plan version must be exactly 1")
    entries = plan.get("entries")
    if not isinstance(entries, list) or not entries:
        raise RunnerError("sample plan must contain at least one entry")
    expected_queries = 0
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get("samples"), list) or not entry["samples"]:
            raise RunnerError("sample plan entry {} has no samples".format(index))
        expected_queries += len(entry["samples"])
    return expected_queries


def validate_truth(path, scale, workload, sample_plan_sha256, property_id):
    truth = read_json(path, "truth")
    if truth.get("schema_version") != 1:
        raise RunnerError("truth schema_version must be exactly 1")
    if truth.get("digest_schema") != "storage-bench-result-digest-v1":
        raise RunnerError("truth has unknown digest_schema")
    if truth.get("scale") != scale or truth.get("workload") != workload:
        raise RunnerError("truth scale/workload does not match this profile")
    if truth.get("sample_plan_sha256") != sample_plan_sha256:
        raise RunnerError("truth does not bind the frozen sample plan")
    expected_property_mode = "presence" if workload == "property-presence" else "none"
    if truth.get("property_predicate_mode") != expected_property_mode:
        raise RunnerError("truth property_predicate_mode does not match this workload")
    if truth.get("property_id") != property_id:
        raise RunnerError("truth property_id does not match this workload")
    entries = truth.get("entries")
    if not isinstance(entries, list) or not entries:
        raise RunnerError("truth must contain non-empty entries")
    return truth


def validate_correctness_evidence(path, expected, scale, stage, workload, expected_queries):
    evidence = read_json(path, "correctness PASS evidence")
    required = {
        "state",
        "correctness_only",
        "performance_eligible",
        "scale",
        "workload",
        "checked",
        "mismatches",
        "schema_version",
        "digest_schema",
        "covered_stages",
        "expected_queries_per_stage",
        "binary_sha256",
        "dataset_sha256",
        "truth_sha256",
    }
    missing = sorted(required - set(evidence))
    if missing:
        raise RunnerError("correctness PASS evidence missing keys: {}".format(", ".join(missing)))
    sample_sha = evidence.get("sample_plan_sha256", evidence.get("query_or_trace_sha256"))
    if sample_sha is None:
        raise RunnerError("correctness PASS evidence must bind sample_plan_sha256")
    if evidence.get("state") != "PASS":
        raise RunnerError("correctness evidence state must be PASS")
    if evidence.get("schema_version") != 1:
        raise RunnerError("correctness evidence schema_version must be exactly 1")
    if evidence.get("digest_schema") != "storage-bench-result-digest-v1":
        raise RunnerError("correctness evidence has unknown digest_schema")
    if evidence.get("correctness_only") is not True or evidence.get("performance_eligible") is not False:
        raise RunnerError("correctness evidence must be correctness_only=true and performance_eligible=false")
    if evidence.get("scale") != scale or evidence.get("workload") != workload:
        raise RunnerError("correctness evidence scale/workload does not match this profile")
    covered_stages = evidence.get("covered_stages")
    allowed_stages = CANONICAL_STAGES[scale]
    if covered_stages != allowed_stages or stage not in covered_stages:
        raise RunnerError("correctness evidence must cover the canonical stage list")
    if evidence.get("expected_queries_per_stage") != expected_queries:
        raise RunnerError("correctness evidence sample-plan coverage is incomplete")
    checked = evidence.get("checked")
    mismatches = evidence.get("mismatches")
    expected_checked = expected_queries * len(covered_stages)
    if not isinstance(checked, int) or isinstance(checked, bool) or checked != expected_checked:
        raise RunnerError(
            "correctness evidence checked must equal full coverage {}".format(expected_checked)
        )
    if not isinstance(mismatches, int) or isinstance(mismatches, bool) or mismatches != 0:
        raise RunnerError("correctness evidence mismatches must be exactly 0")
    bindings = {
        "binary": evidence.get("binary_sha256"),
        "dataset": evidence.get("dataset_sha256"),
        "truth": evidence.get("truth_sha256"),
        "sample_plan": sample_sha,
    }
    for name, actual in bindings.items():
        if not isinstance(actual, str) or actual.lower() != expected[name]:
            raise RunnerError("correctness evidence {} SHA-256 does not match".format(name))
    return evidence


def validate_clean_window_sentinel(path, expected, scale, pristine_store_sha256):
    sentinel = read_json(path, "clean-window sentinel")
    required = {
        "schema_version",
        "state",
        "purpose",
        "performance_eligible",
        "gate_mode",
        "scale",
        "host",
        "completed_at_utc",
        "observations",
        "qps_cv",
        "p99_cv",
        "monitor_ready_sha256",
        "binary_sha256",
        "dataset_sha256",
        "sample_plan_sha256",
        "truth_sha256",
        "pristine_store_sha256",
    }
    missing = sorted(required - set(sentinel))
    if missing:
        raise RunnerError("clean-window sentinel missing: {}".format(", ".join(missing)))
    if (
        sentinel.get("schema_version") != 1
        or sentinel.get("state") != "PASS"
        or sentinel.get("purpose") != "p20-clean-window-sentinel"
        or sentinel.get("performance_eligible") is not False
        or sentinel.get("gate_mode") != "seml0"
    ):
        raise RunnerError("clean-window sentinel schema/state drift")
    if sentinel.get("scale") != scale or sentinel.get("host") != socket.gethostname():
        raise RunnerError("clean-window sentinel scale/host drift")
    observations = sentinel.get("observations")
    if not isinstance(observations, int) or isinstance(observations, bool) or observations < 3:
        raise RunnerError("clean-window sentinel requires at least three observations")
    for name, limit in (("qps_cv", 0.03), ("p99_cv", 0.05)):
        value = sentinel.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value > limit:
            raise RunnerError("clean-window sentinel {} exceeds {:.2f}".format(name, limit))
    normalize_sha(sentinel.get("monitor_ready_sha256", ""), "monitor READY SHA-256")
    expected_bindings = {
        "binary_sha256": expected["binary"],
        "dataset_sha256": expected["dataset"],
        "sample_plan_sha256": expected["sample_plan"],
        "truth_sha256": expected["truth"],
        "pristine_store_sha256": pristine_store_sha256,
    }
    for name, expected_sha in expected_bindings.items():
        if sentinel.get(name) != expected_sha:
            raise RunnerError("clean-window sentinel binding drift: {}".format(name))
    completed = sentinel.get("completed_at_utc")
    if not isinstance(completed, str) or not completed:
        raise RunnerError("clean-window sentinel completion time is missing")
    try:
        completed_time = datetime.fromisoformat(completed.replace("Z", "+00:00"))
    except ValueError:
        raise RunnerError("clean-window sentinel completion time is invalid")
    if completed_time.tzinfo is None:
        raise RunnerError("clean-window sentinel completion time must include a timezone")
    age_seconds = (datetime.now(timezone.utc) - completed_time.astimezone(timezone.utc)).total_seconds()
    if age_seconds < -300 or age_seconds > CLEAN_WINDOW_MAX_AGE_SECONDS:
        raise RunnerError("clean-window sentinel is stale or from the future")
    return sentinel


def resolve_profile(args):
    command = [
        sys.executable,
        str(args.profile_validator),
        "--profiles",
        str(args.profiles),
        "--resolve",
        "--scale",
        args.scale,
        "--stage",
        args.stage,
        "--mode",
        args.mode,
        "--workload",
        args.workload,
    ]
    for binding in args.bind:
        command.extend(["--bind", binding])
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RunnerError("profile resolver failed: {}".format(result.stderr.strip()))
    try:
        resolved = json.loads(result.stdout, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, RunnerError) as exc:
        raise RunnerError("profile resolver emitted invalid JSON: {}".format(exc))
    if not isinstance(resolved, dict):
        raise RunnerError("resolved profile must be a JSON object")
    if resolved.get("scale") != args.scale:
        raise RunnerError("resolved scale drift")
    if resolved.get("stage", {}).get("id") != args.stage:
        raise RunnerError("resolved stage drift")
    if resolved.get("mode", {}).get("name") != args.mode:
        raise RunnerError("resolved mode drift")
    if resolved.get("workload", {}).get("name") != args.workload:
        raise RunnerError("resolved workload drift")
    storage_args = resolved.get("storage_bench_args")
    if not isinstance(storage_args, list) or not storage_args or storage_args[0] != "storage-bench":
        raise RunnerError("resolved profile has invalid storage_bench_args")
    if not all(isinstance(item, str) for item in storage_args):
        raise RunnerError("resolved storage_bench_args must contain only strings")
    if resolved.get("mode", {}).get("max_query_streams") != 1:
        raise RunnerError("P20 profile is not single-stream")
    if resolved.get("fixed_inputs", {}).get("io_backend") != "blocking":
        raise RunnerError("P20 io_backend must remain blocking")
    for token in storage_args:
        if token == "--auto-compact" or token.startswith("--auto-compact="):
            raise RunnerError("measured --auto-compact is forbidden")
    return resolved


def validate_bindings(workload, bindings):
    parsed = {}
    for raw in bindings:
        if "=" not in raw:
            raise RunnerError("binding must be NAME=VALUE: {!r}".format(raw))
        name, value = raw.split("=", 1)
        if name in parsed:
            raise RunnerError("duplicate binding: {}".format(name))
        parsed[name] = value
    if workload == "property-presence":
        if set(parsed) != {"PROPERTY_ID"}:
            raise RunnerError("property-presence requires exactly PROPERTY_ID")
        try:
            property_id = int(parsed["PROPERTY_ID"], 10)
        except ValueError:
            raise RunnerError("PROPERTY_ID must be a decimal u32")
        if str(property_id) != parsed["PROPERTY_ID"] or not (0 <= property_id <= 0xFFFFFFFF):
            raise RunnerError("PROPERTY_ID must be a canonical decimal u32")
        return property_id
    elif parsed:
        raise RunnerError("runtime bindings are forbidden for this workload")
    return 0


def parse_runtime_overrides(values):
    settings = {"warmup-runs": 0, "repeats": 1}
    seen = set()
    for raw in values:
        if "=" not in raw:
            raise RunnerError("runtime override must be NAME=VALUE: {!r}".format(raw))
        name, value = raw.split("=", 1)
        name = name.strip().lstrip("-")
        if name == "auto-compact":
            raise RunnerError("measured --auto-compact is forbidden")
        if name not in ALLOWED_RUNTIME_OVERRIDES:
            raise RunnerError("unknown runtime override: {}".format(name))
        if name in seen:
            raise RunnerError("duplicate runtime override: {}".format(name))
        seen.add(name)
        try:
            parsed = int(value)
        except ValueError:
            raise RunnerError("runtime override {} must be an integer".format(name))
        if parsed < 0:
            raise RunnerError("runtime override {} cannot be negative".format(name))
        settings[name] = parsed
    if settings["repeats"] != 1:
        raise RunnerError("single-profile independent run requires repeats=1")
    return settings


def parse_cpuset(raw):
    if raw is None:
        return None, 0
    if not re.fullmatch(r"[0-9,-]+", raw):
        raise RunnerError("invalid taskset cpuset syntax")
    cpus = []
    for part in raw.split(","):
        if not part:
            raise RunnerError("invalid empty cpuset component")
        if "-" in part:
            fields = part.split("-")
            if len(fields) != 2:
                raise RunnerError("invalid cpuset range: {}".format(part))
            start, end = (int(value) for value in fields)
            if start > end:
                raise RunnerError("descending cpuset range: {}".format(part))
            cpus.extend(range(start, end + 1))
        else:
            cpus.append(int(part))
    if not cpus or len(cpus) != len(set(cpus)):
        raise RunnerError("cpuset must be non-empty and contain no duplicate CPUs")
    return raw, len(cpus)


def paths_overlap(left, right):
    left = Path(left).resolve()
    right = Path(right).resolve()
    return left == right or left in right.parents or right in left.parents


def canonical_inventory_sha(files):
    normalized = [
        {
            "path": item["path"],
            "size_bytes": item["size_bytes"],
            "sha256": item["sha256"],
        }
        for item in sorted(files, key=lambda value: value["path"])
    ]
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":")) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def quick_inventory(root):
    root = Path(root)
    entries = []
    for directory, directory_names, file_names in os.walk(str(root), followlinks=False):
        directory_path = Path(directory)
        for name in list(directory_names):
            path = directory_path / name
            if path.is_symlink():
                raise RunnerError("store inventory forbids symlink directory: {}".format(path))
        for name in file_names:
            path = directory_path / name
            if path.is_symlink() or not path.is_file():
                raise RunnerError("store inventory requires regular files: {}".format(path))
            stat = path.stat()
            entries.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
            )
    entries.sort(key=lambda value: value["path"])
    payload = json.dumps(entries, sort_keys=True, separators=(",", ":")) + "\n"
    return {
        "file_count": len(entries),
        "total_bytes": sum(item["size_bytes"] for item in entries),
        "quick_inventory_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "entries": entries,
    }


def validate_store_manifest(path, source, expected_sha, scale, dataset_sha, binary_sha):
    manifest = read_json(path, "pristine store manifest")
    required = {
        "schema_version",
        "state",
        "inventory_schema",
        "store_path",
        "scale",
        "l0_layout",
        "dataset_sha256",
        "binary_sha256",
        "inventory_sha256",
        "files",
    }
    missing = sorted(required - set(manifest))
    if missing:
        raise RunnerError("pristine store manifest missing: {}".format(", ".join(missing)))
    if (
        manifest.get("schema_version") != 1
        or manifest.get("state") != "FROZEN"
        or manifest.get("inventory_schema") != "p20-store-inventory-v1"
    ):
        raise RunnerError("pristine store manifest schema/state drift")
    if Path(manifest.get("store_path", "")).resolve() != Path(source).resolve():
        raise RunnerError("pristine store manifest path drift")
    if manifest.get("scale") != scale or manifest.get("l0_layout") != "semantic-budgeted":
        raise RunnerError("pristine store manifest scale/layout drift")
    if manifest.get("dataset_sha256") != dataset_sha or manifest.get("binary_sha256") != binary_sha:
        raise RunnerError("pristine store manifest input SHA drift")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise RunnerError("pristine store manifest files must be non-empty")
    seen = set()
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "size_bytes", "sha256"}:
            raise RunnerError("invalid pristine store inventory entry")
        relative = item.get("path")
        pure = PurePosixPath(relative) if isinstance(relative, str) else PurePosixPath("/")
        if pure.is_absolute() or ".." in pure.parts or "." in pure.parts or not relative:
            raise RunnerError("unsafe pristine store inventory path")
        if relative in seen:
            raise RunnerError("duplicate pristine store inventory path")
        seen.add(relative)
        if not isinstance(item.get("size_bytes"), int) or isinstance(item.get("size_bytes"), bool) or item["size_bytes"] < 0:
            raise RunnerError("invalid pristine store inventory size")
        normalize_sha(item.get("sha256", ""), "store file SHA-256")
    inventory_sha = canonical_inventory_sha(files)
    if inventory_sha != expected_sha or manifest.get("inventory_sha256") != expected_sha:
        raise RunnerError("pristine store inventory SHA-256 drift")
    actual = quick_inventory(source)
    expected_sizes = {item["path"]: item["size_bytes"] for item in files}
    actual_sizes = {item["path"]: item["size_bytes"] for item in actual["entries"]}
    if actual_sizes != expected_sizes:
        raise RunnerError("pristine store file-set/size differs from frozen manifest")
    verify_store_content(source, files)
    return manifest, actual


def verify_store_content(root, files):
    root = Path(root)
    for item in files:
        path = root / PurePosixPath(item["path"])
        if not path.is_file() or path.is_symlink():
            raise RunnerError("clone inventory file is missing/non-regular: {}".format(path))
        if path.stat().st_size != item["size_bytes"] or sha256_file(path) != item["sha256"]:
            raise RunnerError("clone content differs from frozen inventory: {}".format(path))


def clone_pristine_store(source, destination, store_manifest, source_before):
    source = Path(source)
    destination = Path(destination)
    if os.path.lexists(str(destination)):
        raise RunnerError("stage store must be new and absent: {}".format(destination))
    if source.is_symlink() or not source.is_dir() or not any(source.iterdir()):
        raise RunnerError("pristine store must be a non-empty directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir()
    command = ["cp", "-a", "--reflink=auto", str(source) + "/.", str(destination)]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RunnerError("stage-store clone failed; partial destination retained: {}".format(result.stderr.strip()))
    if not any(destination.iterdir()):
        raise RunnerError("stage-store clone produced an empty destination")
    source_after = quick_inventory(source)
    if source_before != source_after:
        raise RunnerError("pristine store changed while cloning")
    stage_inventory = quick_inventory(destination)
    if {
        item["path"]: item["size_bytes"] for item in stage_inventory["entries"]
    } != {item["path"]: item["size_bytes"] for item in source_before["entries"]}:
        raise RunnerError("stage clone file-set/size differs from pristine source")
    verify_store_content(destination, store_manifest["files"])
    return {
        "clone_argv": command,
        "source_quick_inventory": source_after,
        "stage_quick_inventory": stage_inventory,
        "content_verified": True,
    }


def write_input_tsv(path, rows):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["name", "path", "kind", "sha256", "verification"],
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
    os.replace(str(tmp), str(path))


def run_with_signal_forwarding(argv, cwd, env):
    """Run one child and forward outer HUP/INT/TERM without invoking a shell."""
    child = subprocess.Popen(argv, cwd=str(cwd), env=env)
    previous = {}

    def forward(signum, _frame):
        if child.poll() is None:
            child.send_signal(signum)

    try:
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, forward)
        return child.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def write_p20_failure(output_root, message, p31_returncode=None):
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    atomic_json(
        output_root / "P20-FAILED.json",
        {
            "schema_version": 1,
            "state": "FAILED",
            "failed_at_utc": utc_now(),
            "reason": message,
            "p31_returncode": p31_returncode,
        },
    )


def run(args):
    output_root = require_absolute(args.output_root, "output root")
    stage_store = require_absolute(args.stage_store, "stage store")
    pristine_store = require_absolute(args.pristine_store, "pristine store")
    repo_root = require_absolute(args.repo_root, "repo root")
    profiles = require_absolute(args.profiles, "profiles")
    profile_validator = require_absolute(args.profile_validator, "profile validator")
    summarizer = require_absolute(args.summarizer, "summarizer")
    p31_wrapper = require_absolute(args.p31_wrapper, "P31 wrapper")
    data_mount = require_absolute(args.data_mount, "data mount")
    if args.mode == "correctness":
        raise RunnerError(
            "mode=correctness is not runnable here; this measured runner consumes an external correctness PASS"
        )
    inherited_forbidden = sorted(name for name in FORBIDDEN_ENVIRONMENT if name in os.environ)
    if inherited_forbidden:
        raise RunnerError(
            "forbidden benchmark environment variable(s): {}".format(
                ", ".join(inherited_forbidden)
            )
        )
    if os.path.lexists(str(output_root)):
        raise RunnerError("output root must not already exist (empty or non-empty): {}".format(output_root))
    if os.path.lexists(str(stage_store)):
        raise RunnerError("stage store must not already exist: {}".format(stage_store))
    for path, label in ((repo_root, "repo root"), (data_mount, "data mount")):
        if not path.is_dir():
            raise RunnerError("{} must be an existing directory: {}".format(label, path))
    for path, label in (
        (profiles, "profiles"),
        (profile_validator, "profile validator"),
        (summarizer, "summarizer"),
        (p31_wrapper, "P31 wrapper"),
    ):
        if not path.is_file():
            raise RunnerError("{} must be an existing file: {}".format(label, path))
    if (
        paths_overlap(output_root, stage_store)
        or paths_overlap(pristine_store, stage_store)
        or paths_overlap(output_root, pristine_store)
    ):
        raise RunnerError("output, pristine store, and stage store must be non-overlapping")
    if not ID_RE.fullmatch(args.task_id):
        raise RunnerError("invalid task id")
    run_id = args.run_id or output_root.name
    if not ID_RE.fullmatch(run_id):
        raise RunnerError("invalid run id")
    cpuset, cpuset_count = parse_cpuset(args.cpuset)
    if cpuset is not None and shutil.which("taskset") is None:
        raise RunnerError("--cpuset requested but taskset is unavailable")
    if cpuset is not None and args.worker_threads > cpuset_count:
        raise RunnerError("worker threads cannot exceed the pinned CPU count")
    runtime = parse_runtime_overrides(args.runtime_override)
    property_id = validate_bindings(args.workload, args.bind)

    # Resolve paths back into args only after absolute-path validation.
    args.profiles = profiles
    args.profile_validator = profile_validator
    resolved = resolve_profile(args)
    resolved_perf = resolved.get("mode", {}).get("performance_eligible")
    if not isinstance(resolved_perf, bool):
        raise RunnerError("resolved mode omitted boolean performance_eligible")
    performance_text = "true" if resolved_perf else "false"
    if args.performance_eligible is not None and args.performance_eligible != performance_text:
        raise RunnerError(
            "performance_eligible={} disagrees with resolved mode {} ({})".format(
                args.performance_eligible, args.mode, resolved_perf
            )
        )
    if resolved_perf and args.allow_missing_aux_tools:
        raise RunnerError("formal performance mode cannot allow missing P31 auxiliary tools")
    if resolved_perf and cpuset is None:
        raise RunnerError("formal performance mode requires an explicit --cpuset")
    sentinel_args_present = args.clean_window_sentinel is not None or args.clean_window_sentinel_sha256 is not None
    if sentinel_args_present and not (
        args.clean_window_sentinel is not None
        and args.clean_window_sentinel_sha256 is not None
    ):
        raise RunnerError("clean-window sentinel path and SHA-256 must be supplied together")
    if resolved_perf and not sentinel_args_present:
        raise RunnerError("formal performance mode requires a clean-window sentinel")

    frozen_environment = os.environ.copy()
    for name in FORBIDDEN_ENVIRONMENT:
        frozen_environment.pop(name, None)
    frozen_environment["RAYON_NUM_THREADS"] = str(args.worker_threads)
    frozen_environment["TOKIO_WORKER_THREADS"] = str(args.worker_threads)
    recorded_environment = {
        "RAYON_NUM_THREADS": frozen_environment["RAYON_NUM_THREADS"],
        "TOKIO_WORKER_THREADS": frozen_environment["TOKIO_WORKER_THREADS"],
        "SNB_SKIP_SEM_INDEX": "absent",
        "SNB_SKIP_ADJ_CACHE": "absent",
    }

    binary_row = artifact_row("binary", args.binary, args.binary_sha256, require_file=True)
    if not os.access(binary_row["path"], os.X_OK):
        raise RunnerError("binary is not executable: {}".format(binary_row["path"]))
    dataset_row = artifact_row("dataset", args.dataset, args.dataset_sha256)
    sample_row = artifact_row("sample_plan", args.sample_plan, args.sample_plan_sha256, require_file=True)
    truth_row = artifact_row("truth", args.truth, args.truth_sha256, require_file=True)
    correctness_row = artifact_row(
        "correctness_pass", args.correctness_pass, args.correctness_pass_sha256, require_file=True
    )
    pristine_row = artifact_row(
        "pristine_store", pristine_store, args.pristine_store_sha256, require_dir=True
    )
    pristine_row["verification"] = "verified-via-pristine-store-manifest"
    pristine_manifest_row = artifact_row(
        "pristine_store_manifest",
        args.pristine_store_manifest,
        args.pristine_store_manifest_sha256,
        require_file=True,
    )
    profiles_sha = sha256_file(profiles)
    validator_sha = sha256_file(profile_validator)
    summarizer_sha = sha256_file(summarizer)
    runner_sha = sha256_file(Path(__file__).resolve())
    p31_sha = sha256_file(p31_wrapper)
    expected_queries = validate_sample_plan(sample_row["path"])
    validate_truth(
        truth_row["path"], args.scale, args.workload, sample_row["sha256"], property_id
    )
    store_manifest, source_before = validate_store_manifest(
        pristine_manifest_row["path"],
        pristine_store,
        pristine_row["sha256"],
        args.scale,
        dataset_row["sha256"],
        binary_row["sha256"],
    )
    expected = {
        "binary": binary_row["sha256"],
        "dataset": dataset_row["sha256"],
        "sample_plan": sample_row["sha256"],
        "truth": truth_row["sha256"],
    }
    clean_window_row = None
    if sentinel_args_present:
        clean_window_row = artifact_row(
            "clean_window_sentinel",
            args.clean_window_sentinel,
            args.clean_window_sentinel_sha256,
            require_file=True,
        )
        validate_clean_window_sentinel(
            clean_window_row["path"], expected, args.scale, pristine_row["sha256"]
        )
    validate_correctness_evidence(
        correctness_row["path"], expected, args.scale, args.stage, args.workload, expected_queries
    )

    output_root.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output_root.parent / ("." + output_root.name + ".p20.lock")
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise RunnerError("another P20 runner holds lock: {}".format(lock_path))
    os.close(lock_fd)
    p31_returncode = None
    try:
        clone_result = clone_pristine_store(
            pristine_store, stage_store, store_manifest, source_before
        )
        output_root.mkdir()
        p31_run_root = output_root / "p31"
        atomic_json(output_root / "resolved-profile.json", resolved)
        atomic_json(
            output_root / "stage-store-provenance.json",
            {
                "schema_version": 1,
                "created_at_utc": utc_now(),
                "store_isolation": resolved["fixed_inputs"]["store_isolation"],
                "pristine_store": str(pristine_store),
                "pristine_store_sha256": pristine_row["sha256"],
                "pristine_store_manifest": pristine_manifest_row["path"],
                "pristine_store_manifest_sha256": pristine_manifest_row["sha256"],
                "stage_store": str(stage_store),
                "clone": clone_result,
                "exclusive_claim": {
                    "run_id": run_id,
                    "repeat_index": args.repeat_index,
                    "stage": args.stage,
                    "workload": args.workload,
                },
            },
        )
        stage_provenance_row = {
            "name": "stage_store_provenance",
            "path": str(output_root / "stage-store-provenance.json"),
            "kind": "file",
            "sha256": sha256_file(output_root / "stage-store-provenance.json"),
            "verification": "computed-file",
        }
        shutil.copy2(correctness_row["path"], str(output_root / "correctness-pass.json"))
        invocation_config = {
            "schema_version": 1,
            "experiment_id": resolved["experiment_id"],
            "task_id": args.task_id,
            "run_id": run_id,
            "scale": args.scale,
            "stage": args.stage,
            "mode": args.mode,
            "workload": args.workload,
            "bindings": list(args.bind),
            "property_predicate_mode": (
                "presence" if args.workload == "property-presence" else "none"
            ),
            "property_id": property_id,
            "repeat_index": args.repeat_index,
            "performance_eligible": resolved_perf,
            "max_query_streams": 1,
            "worker_threads": args.worker_threads,
            "runtime": runtime,
            "csr_metadata_cache_entries": args.csr_metadata_cache_entries,
            "cpuset": cpuset or "",
            "frozen_environment": recorded_environment,
            "input_sha256": expected,
            "correctness_pass_sha256": correctness_row["sha256"],
            "clean_window_sentinel_sha256": (
                clean_window_row["sha256"] if clean_window_row is not None else ""
            ),
            "pristine_store_sha256": pristine_row["sha256"],
            "pristine_store_manifest_sha256": pristine_manifest_row["sha256"],
            "stage_store_provenance_sha256": stage_provenance_row["sha256"],
            "profiles_sha256": profiles_sha,
            "resolved_profile_sha256": sha256_file(output_root / "resolved-profile.json"),
        }
        atomic_json(output_root / "invocation-config.json", invocation_config)
        invocation_config_sha = sha256_file(output_root / "invocation-config.json")
        storage_argv = [
            binary_row["path"],
            "--io-backend",
            "blocking",
            "--csr-metadata-cache-entries",
            str(args.csr_metadata_cache_entries),
        ] + list(resolved["storage_bench_args"])
        storage_argv.extend(
            [
                "--data-dir",
                str(stage_store),
                "--sample-plan-in",
                sample_row["path"],
                "--warmup-runs",
                str(runtime["warmup-runs"]),
                "--repeats",
                "1",
            ]
        )
        for token in storage_argv:
            if token == "--auto-compact" or token.startswith("--auto-compact="):
                raise RunnerError("measured --auto-compact is forbidden")
        benchmark_argv = list(storage_argv)
        if cpuset is not None:
            benchmark_argv = ["taskset", "-c", cpuset] + benchmark_argv

        p31_argv = [
            str(p31_wrapper),
            "--run-dir",
            str(p31_run_root),
            "--run-id",
            run_id,
            "--task-id",
            args.task_id,
            "--performance-eligible",
            performance_text,
            "--repo-root",
            str(repo_root),
            "--device",
            args.device,
            "--data-mount",
            str(data_mount),
            "--interval",
            str(args.interval),
            "--disk-interval",
            str(args.disk_interval),
            "--min-samples",
            str(args.min_samples),
            "--store",
            "p20={}".format(stage_store),
            "--binary",
            binary_row["path"],
            "--binary-sha256",
            binary_row["sha256"],
            "--dataset",
            dataset_row["path"],
            "--dataset-sha256",
            dataset_row["sha256"],
            "--truth",
            truth_row["path"],
            "--truth-sha256",
            truth_row["sha256"],
            "--query-or-trace",
            sample_row["path"],
            "--query-or-trace-sha256",
            sample_row["sha256"],
            "--config",
            str(output_root / "invocation-config.json"),
            "--config-sha256",
            invocation_config_sha,
        ]
        if args.allow_missing_aux_tools:
            p31_argv.append("--allow-missing-aux-tools")
        p31_argv.extend(["--"] + benchmark_argv)
        command_doc = {
            "schema_version": 1,
            "invocation_kind": "argv-no-shell",
            "task_id": args.task_id,
            "run_id": run_id,
            "repeat_index": args.repeat_index,
            "cpuset": cpuset or "",
            "worker_threads": args.worker_threads,
            "frozen_environment": recorded_environment,
            "runtime_overrides": runtime,
            "storage_bench_argv": storage_argv,
            "benchmark_argv": benchmark_argv,
            "p31_argv": p31_argv,
            "p31_run_root": str(p31_run_root),
            "stage_store": str(stage_store),
            "pristine_store": str(pristine_store),
        }
        atomic_json(output_root / "command.json", command_doc)
        rows = [
            binary_row,
            dataset_row,
            sample_row,
            truth_row,
            correctness_row,
            pristine_row,
            pristine_manifest_row,
            stage_provenance_row,
            {"name": "profiles", "path": str(profiles), "kind": "file", "sha256": profiles_sha, "verification": "computed-file"},
            {"name": "invocation_config", "path": str(output_root / "invocation-config.json"), "kind": "file", "sha256": invocation_config_sha, "verification": "computed-file"},
            {"name": "profile_validator", "path": str(profile_validator), "kind": "file", "sha256": validator_sha, "verification": "computed-file"},
            {"name": "p20_runner", "path": str(Path(__file__).resolve()), "kind": "file", "sha256": runner_sha, "verification": "computed-file"},
            {"name": "p20_summarizer", "path": str(summarizer), "kind": "file", "sha256": summarizer_sha, "verification": "computed-file"},
            {"name": "p31_wrapper", "path": str(p31_wrapper), "kind": "file", "sha256": p31_sha, "verification": "computed-file"},
            {"name": "resolved_profile", "path": str(output_root / "resolved-profile.json"), "kind": "file", "sha256": sha256_file(output_root / "resolved-profile.json"), "verification": "computed-file"},
            {"name": "command", "path": str(output_root / "command.json"), "kind": "file", "sha256": sha256_file(output_root / "command.json"), "verification": "computed-file"},
        ]
        if clean_window_row is not None:
            rows.append(clean_window_row)
        write_input_tsv(output_root / "inputs.sha256.tsv", rows)

        p31_returncode = run_with_signal_forwarding(
            p31_argv, cwd=repo_root, env=frozen_environment
        )

        done = p31_run_root / "DONE"
        failed = p31_run_root / "FAILED"
        if p31_returncode != 0 or not done.is_file() or failed.exists():
            message = "P31 failed marker gate: rc={}, DONE={}, FAILED={}".format(
                p31_returncode, done.is_file(), failed.exists()
            )
            write_p20_failure(output_root, message, p31_returncode)
            raise RunnerError(message)

        post_state = {
            "schema_version": 1,
            "captured_at_utc": utc_now(),
            "run_id": run_id,
            "repeat_index": args.repeat_index,
            "stage": args.stage,
            "workload": args.workload,
            "stage_store": str(stage_store),
            "quick_inventory": quick_inventory(stage_store),
        }
        atomic_json(output_root / "stage-store-post-state.json", post_state)
        post_state_row = {
            "name": "stage_store_post_state",
            "path": str(output_root / "stage-store-post-state.json"),
            "kind": "file",
            "sha256": sha256_file(output_root / "stage-store-post-state.json"),
            "verification": "computed-file",
        }
        rows.append(post_state_row)
        write_input_tsv(output_root / "inputs.sha256.tsv", rows)

        summary_result = subprocess.run(
            [sys.executable, str(summarizer), "--run-root", str(output_root)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if summary_result.returncode != 0:
            message = "P20 summary validation failed: {}".format(summary_result.stderr.strip())
            write_p20_failure(output_root, message, p31_returncode)
            raise RunnerError(message)
        pass_doc = {
            "schema_version": 1,
            "state": "PASS",
            "validated_at_utc": utc_now(),
            "p31_done_sha256": sha256_file(done),
            "p31_manifest_sha256": sha256_file(p31_run_root / "run-manifest.json"),
            "stage_store_provenance_sha256": stage_provenance_row["sha256"],
            "stage_store_post_state_sha256": post_state_row["sha256"],
            "summary_sha256": sha256_file(output_root / "summary.tsv"),
        }
        atomic_json(output_root / "P20-PASS.json", pass_doc)
        print(str(output_root / "summary.tsv"))
        return 0
    except (RunnerError, OSError) as exc:
        if output_root.exists():
            write_p20_failure(output_root, str(exc), p31_returncode)
        raise
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--stage-store", required=True, type=Path)
    parser.add_argument("--pristine-store", required=True, type=Path)
    parser.add_argument("--pristine-store-sha256", required=True)
    parser.add_argument("--pristine-store-manifest", required=True, type=Path)
    parser.add_argument("--pristine-store-manifest-sha256", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--repeat-index", required=True, type=int)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--p31-wrapper", required=True, type=Path)
    parser.add_argument("--profiles", type=Path, default=HERE / "profiles.json")
    parser.add_argument("--profile-validator", type=Path, default=HERE / "validate_profiles.py")
    parser.add_argument("--summarizer", type=Path, default=HERE / "summarize_profile.py")
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--binary-sha256", required=True)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--dataset-sha256", required=True)
    parser.add_argument("--sample-plan", required=True, type=Path)
    parser.add_argument("--sample-plan-sha256", required=True)
    parser.add_argument("--truth", required=True, type=Path)
    parser.add_argument("--truth-sha256", required=True)
    parser.add_argument("--correctness-pass", required=True, type=Path)
    parser.add_argument("--correctness-pass-sha256", required=True)
    parser.add_argument("--clean-window-sentinel", type=Path)
    parser.add_argument("--clean-window-sentinel-sha256")
    parser.add_argument("--scale", required=True, choices=("sf10", "sf30"))
    parser.add_argument("--stage", required=True, choices=tuple("A{}".format(i) for i in range(7)))
    parser.add_argument(
        "--mode",
        required=True,
        choices=("latency", "cpu-phase", "closed-loop-resource", "correctness"),
    )
    parser.add_argument(
        "--workload",
        required=True,
        choices=("typed-one-hop", "degree-stratified", "property-presence"),
    )
    parser.add_argument("--bind", action="append", default=[])
    parser.add_argument(
        "--performance-eligible",
        choices=("true", "false"),
        help="optional assertion; the canonical resolved mode remains authoritative",
    )
    parser.add_argument("--csr-metadata-cache-entries", type=int, default=4096)
    parser.add_argument("--worker-threads", type=int, default=1)
    parser.add_argument("--cpuset")
    parser.add_argument("--runtime-override", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--device", default="nvme1n1")
    parser.add_argument("--data-mount", required=True, type=Path)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--disk-interval", type=float, default=15.0)
    parser.add_argument("--min-samples", type=int, default=2)
    parser.add_argument("--allow-missing-aux-tools", action="store_true")
    return parser


def main():
    args = build_parser().parse_args()
    if (
        args.interval <= 0
        or args.disk_interval <= 0
        or args.min_samples < 2
        or args.csr_metadata_cache_entries <= 0
        or args.worker_threads <= 0
        or args.repeat_index <= 0
    ):
        print("FAIL: invalid positive numeric runner option", file=sys.stderr)
        return 2
    try:
        return run(args)
    except (RunnerError, OSError) as exc:
        print("FAIL: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
