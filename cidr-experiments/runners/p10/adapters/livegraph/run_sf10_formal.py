#!/usr/bin/env python3
"""Correctness-gated formal LiveGraph SF10 launcher.

This wrapper admits exactly one canonical LiveGraph P10 run: the frozen SF10
dense edge stream and 1,700-query truth, three independent fresh-import
processes, and one warmup plus one measured pass per process.  Before delegating
to the shared P10/P31 orchestrator it validates the current-host P02B release
and the clean-current-HEAD LiveGraph build receipt.  Preflight evidence alone
never publishes a performance point.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


HERE = Path(__file__).resolve().parent
P10_DIR = HERE.parents[1]
REPO_ROOT = P10_DIR.parents[2]
P31_DIR = P10_DIR.parent / "p31"
RUN_SUITE = P10_DIR / "run_suite.py"
FORMAL_TEMPLATE = HERE / "formal-system.template.json"

sys.path.insert(0, str(P10_DIR))

from p10_contract import (  # noqa: E402
    CLOCK_NAME,
    ContractError,
    FRESH_IMPORT_PROCESS_LIFETIME,
    INTERFACE_SCOPE,
    SEQUENCE_DIGEST_ALGORITHM,
    TIMING_BOUNDARY,
    TRUTH_DIGEST_ALGORITHM,
    exclusive_json,
    load_suite_manifest,
    read_p31_summary,
    validate_livegraph_dataset_terminal_identity,
    validate_livegraph_p31_binding,
    validate_livegraph_post_p31_store_seal,
)


SCHEMA = "p10-livegraph-sf10-formal-wrapper-v1"
BUILD_SCHEMA = "p10-livegraph-runtime-build-receipt-v1"
PROCESS_LIFETIME = FRESH_IMPORT_PROCESS_LIFETIME
BUILD_SCOPE = "build-and-runtime-correctness-only"
CANONICAL_DENSE_SHA256 = "9727fed3d710a3f0897b1ee7379411a091e0568bef05902de8b1787efc39d258"
CANONICAL_DENSE_BYTES = 6_638_607_332
CANONICAL_TRUTH_SHA256 = "876ec4be45bb7c220ce595b8d8efd5c79d13db285569cc0d6ae3e8bc19a1c788"
CANONICAL_TRUTH_QUERIES = 1_700
P02B_MAX_AGE_SECONDS = 21_600
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_HEAD_RE = re.compile(r"^[0-9a-f]{40}$")

ADAPTER_ARG_KEYS = {
    "--store-label",
    "--block-name",
    "--wal-name",
    "--temp-base",
    "--temp-label",
    "--build-receipt",
    "--build-receipt-sha256",
    "--p02b-result",
    "--p02b-result-sha256",
    "--p02b-validator",
    "--p02b-validator-sha256",
    "--p02b-max-age-seconds",
}

INTEGRATION_ARTIFACTS = {
    "builder": "cidr-experiments/runners/p10/adapters/livegraph/build_livegraph_runtime.py",
    "build_config_template": "cidr-experiments/runners/p10/adapters/livegraph/build-config.template.json",
    "worker_source": "baseline/external-drivers/livegraph_p10_driver.cpp",
    "external_driver_makefile": "baseline/external-drivers/Makefile",
    "adapter": "cidr-experiments/runners/p10/adapters/livegraph_adapter.py",
    "formal_system_template": "cidr-experiments/runners/p10/adapters/livegraph/formal-system.template.json",
    "sf10_formal_wrapper": "cidr-experiments/runners/p10/adapters/livegraph/run_sf10_formal.py",
    "p02b_validator": "cidr-experiments/runners/p02b/validate_sentinel_result.py",
    "p10_contract": "cidr-experiments/runners/p10/p10_contract.py",
    "p10_suite": "cidr-experiments/runners/p10/run_suite.py",
    "p10_p31_bridge": "cidr-experiments/runners/p10/run_adapter_with_p31.sh",
    "p31_wrapper": "cidr-experiments/runners/p31/run_with_resources.sh",
    "p31_manifest": "cidr-experiments/runners/p31/run_manifest.py",
    "p31_collector": "cidr-experiments/runners/p31/resource_collector.py",
    "p31_validator": "cidr-experiments/runners/p31/validate_resource_run.py",
}


class ReadinessError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ReadinessError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def read_object(path: Path, context: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReadinessError(f"cannot read {context} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReadinessError(f"{context} must be one JSON object")
    return value


def exact_object(value: object, keys: set[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReadinessError(f"{context} must be an object")
    if set(value) != keys:
        raise ReadinessError(
            f"{context} key drift: missing={sorted(keys - set(value))}, "
            f"extra={sorted(set(value) - keys)}"
        )
    return value


def exact_sha(value: object, context: str) -> str:
    if not isinstance(value, str) or not HEX64_RE.fullmatch(value):
        raise ReadinessError(f"{context} must be a lowercase SHA-256")
    return value


def checked_file(
    path: Path, expected_sha: str, context: str, *, executable: bool = False
) -> dict[str, Any]:
    if path.is_symlink():
        raise ReadinessError(f"{context} must not be a symbolic link: {path}")
    resolved = path.resolve()
    if not resolved.is_file():
        raise ReadinessError(f"{context} is not one regular file: {resolved}")
    if executable and not os.access(resolved, os.X_OK):
        raise ReadinessError(f"{context} is not executable: {resolved}")
    expected_sha = exact_sha(expected_sha, f"{context} SHA-256")
    actual = sha256_file(resolved)
    if actual != expected_sha:
        raise ReadinessError(f"{context} SHA-256 mismatch")
    return {"path": str(resolved), "size_bytes": resolved.stat().st_size, "sha256": actual}


def validate_file_ref(reference: object, context: str) -> dict[str, Any]:
    value = exact_object(reference, {"path", "size_bytes", "sha256"}, context)
    if (
        not isinstance(value["path"], str)
        or not Path(value["path"]).is_absolute()
        or str(Path(value["path"]).resolve()) != value["path"]
        or type(value["size_bytes"]) is not int
        or value["size_bytes"] < 0
    ):
        raise ReadinessError(f"{context} path/size is not canonical")
    path = Path(value["path"])
    current = checked_file(path, value["sha256"], context)
    if current["size_bytes"] != value["size_bytes"]:
        raise ReadinessError(f"{context} size changed")
    return current


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def validate_sha_manifest(root: Path, manifest: Path) -> dict[str, Any]:
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ReadinessError(f"cannot read LiveGraph SHA256SUMS: {exc}") from exc
    if not lines:
        raise ReadinessError("LiveGraph SHA256SUMS is empty")
    observed: set[str] = set()
    for line in lines:
        if "  " not in line:
            raise ReadinessError("LiveGraph SHA256SUMS has a malformed row")
        digest, relative = line.split("  ", 1)
        exact_sha(digest, "LiveGraph SHA256SUMS digest")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts or relative in observed:
            raise ReadinessError("LiveGraph SHA256SUMS has an unsafe or duplicate path")
        unresolved = root / relative_path
        if unresolved.is_symlink():
            raise ReadinessError("LiveGraph SHA256SUMS must not cover symbolic links")
        target = unresolved.resolve()
        if root.resolve() not in target.parents:
            raise ReadinessError("LiveGraph SHA256SUMS entry escapes the build root")
        checked_file(target, digest, f"LiveGraph build artifact {relative}")
        observed.add(relative)
    expected = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.name not in {"SHA256SUMS", "DONE.build", "FAILED"}
    }
    if observed != expected:
        raise ReadinessError(
            f"LiveGraph SHA256SUMS coverage drift: missing={sorted(expected - observed)}, "
            f"extra={sorted(observed - expected)}"
        )
    return file_ref(manifest)


def parse_adapter_args(raw: object) -> dict[str, str]:
    if not isinstance(raw, list) or not raw or not all(isinstance(item, str) for item in raw):
        raise ReadinessError("LiveGraph adapter args must be a non-empty string array")
    if len(raw) % 2:
        raise ReadinessError("LiveGraph adapter args must be exact option/value pairs")
    result: dict[str, str] = {}
    for index in range(0, len(raw), 2):
        key, value = raw[index : index + 2]
        if key in result:
            raise ReadinessError(f"duplicate LiveGraph adapter option: {key}")
        result[key] = value
    if set(result) != ADAPTER_ARG_KEYS:
        raise ReadinessError(
            f"LiveGraph adapter option drift: missing={sorted(ADAPTER_ARG_KEYS - set(result))}, "
            f"extra={sorted(set(result) - ADAPTER_ARG_KEYS)}"
        )
    if result["--store-label"] != "livegraph":
        raise ReadinessError("formal LiveGraph store label must be livegraph")
    if result["--block-name"] != "livegraph-block" or result["--wal-name"] != "livegraph-wal":
        raise ReadinessError("formal LiveGraph block/WAL names drifted")
    if result["--temp-label"] != "scratch" or not PurePosixPath(result["--temp-base"]).is_absolute():
        raise ReadinessError("formal LiveGraph temp base/label drifted")
    try:
        max_age = int(result["--p02b-max-age-seconds"])
    except ValueError as exc:
        raise ReadinessError("P02B max age must be an integer") from exc
    if max_age != P02B_MAX_AGE_SECONDS:
        raise ReadinessError(f"P02B max age must be exactly {P02B_MAX_AGE_SECONDS} seconds")
    return result


def git_state(root: Path) -> dict[str, Any]:
    def output(argv: list[str]) -> str:
        try:
            return subprocess.check_output(argv, text=True, stderr=subprocess.STDOUT, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReadinessError(f"Git provenance command failed: {argv!r}: {exc}") from exc

    top = Path(output(["git", "-C", str(root), "rev-parse", "--show-toplevel"]).strip()).resolve()
    if top != root.resolve():
        raise ReadinessError("current integration root is not a Git worktree root")
    head = output(["git", "-C", str(root), "rev-parse", "HEAD"]).strip()
    if not GIT_HEAD_RE.fullmatch(head):
        raise ReadinessError("current integration HEAD is malformed")
    status = output(
        ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=normal"]
    ).splitlines()
    return {"root": str(top), "head": head, "dirty": bool(status), "status_lines": status}


def validate_git_receipt(value: object, context: str) -> dict[str, Any]:
    state = exact_object(value, {"root", "head", "dirty", "status_lines"}, context)
    if (
        not isinstance(state["root"], str)
        or not GIT_HEAD_RE.fullmatch(str(state["head"]))
        or state["dirty"] is not False
        or state["status_lines"] != []
    ):
        raise ReadinessError(f"{context} is not a clean frozen Git identity")
    return state


def validate_protocol(suite: dict[str, Any]) -> None:
    protocol = suite.get("protocol")
    if not isinstance(protocol, dict):
        raise ReadinessError("suite protocol is missing")
    expected = {
        "group_policy": "report-separately-no-cross-group-speedups",
        "interface_scope": INTERFACE_SCOPE,
        "timing_boundary": TIMING_BOUNDARY,
        "clock": CLOCK_NAME,
        "cache_policy": "full-shared-trace-before-each-measured-repeat-v1",
        "process_reuse_between_phases": True,
        "warmup_passes": 1,
        "measured_passes": 1,
        "repeats": 3,
        "concurrency": 1,
        "max_timeouts": 0,
    }
    for key, wanted in expected.items():
        if protocol.get(key) != wanted:
            raise ReadinessError(f"formal LiveGraph protocol {key} must be {wanted!r}")
    timeout_ms = protocol.get("per_query_timeout_ms")
    adapter_timeout_s = protocol.get("adapter_process_timeout_s")
    if type(timeout_ms) is not int or timeout_ms <= 0:
        raise ReadinessError("per-query timeout must be a positive integer")
    if type(adapter_timeout_s) is not int or adapter_timeout_s < 3_600:
        raise ReadinessError("adapter process timeout must reserve at least one hour per SF10 repeat")


def validate_build_receipt(
    receipt_path: Path,
    receipt_sha: str,
    system: dict[str, Any],
) -> dict[str, Any]:
    receipt_ref = checked_file(receipt_path, receipt_sha, "LiveGraph build receipt")
    receipt = exact_object(
        read_object(receipt_path, "LiveGraph build receipt"),
        {
            "schema_version",
            "state",
            "scope",
            "performance_eligible",
            "formal_performance_points",
            "build_policy",
            "started_at_utc",
            "completed_at_utc",
            "config",
            "integration",
            "livegraph_source",
            "compiler",
            "build",
            "runtime",
        },
        "LiveGraph build receipt",
    )
    if (
        receipt["schema_version"] != BUILD_SCHEMA
        or receipt["state"] != "PASS"
        or receipt["scope"] != BUILD_SCOPE
        or receipt["performance_eligible"] is not False
        or receipt["formal_performance_points"] != 0
        or receipt["build_policy"] != "clean-current-head-native-build-v1"
    ):
        raise ReadinessError("LiveGraph build receipt classification/state drift")

    marker_path = receipt_path.resolve().parent / "DONE.build"
    if (receipt_path.resolve().parent / "FAILED").exists():
        raise ReadinessError("LiveGraph build directory contains both DONE.build and FAILED")
    if not marker_path.is_file() or marker_path.is_symlink():
        raise ReadinessError("LiveGraph DONE.build is not one regular file")
    marker = exact_object(
        read_object(marker_path, "LiveGraph build marker"),
        {
            "state",
            "scope",
            "performance_eligible",
            "formal_performance_points",
            "receipt",
            "receipt_sha256",
            "sha256sums",
            "sha256sums_sha256",
        },
        "LiveGraph build marker",
    )
    if (
        marker["state"] != "PASS"
        or marker["scope"] != BUILD_SCOPE
        or marker["performance_eligible"] is not False
        or marker["formal_performance_points"] != 0
        or Path(str(marker["receipt"])).resolve() != receipt_path.resolve()
        or marker["receipt_sha256"] != receipt_ref["sha256"]
    ):
        raise ReadinessError("LiveGraph DONE.build does not bind the build receipt")
    sha_manifest = checked_file(
        Path(str(marker["sha256sums"])),
        str(marker["sha256sums_sha256"]),
        "LiveGraph build SHA256SUMS",
    )
    if Path(sha_manifest["path"]).parent != receipt_path.resolve().parent:
        raise ReadinessError("LiveGraph SHA256SUMS is outside the build receipt directory")
    validate_sha_manifest(receipt_path.resolve().parent, Path(sha_manifest["path"]))

    integration = exact_object(receipt["integration"], {"before", "after", "artifacts"}, "build integration")
    before = validate_git_receipt(integration["before"], "build integration.before")
    after = validate_git_receipt(integration["after"], "build integration.after")
    if before != after:
        raise ReadinessError("integration Git identity changed during the LiveGraph build")
    current = git_state(REPO_ROOT)
    if current["dirty"] or current["status_lines"] or current != after:
        raise ReadinessError("current clean integration Git identity differs from the build receipt")

    artifacts = exact_object(
        integration["artifacts"], set(INTEGRATION_ARTIFACTS), "build integration artifacts"
    )
    validated_artifacts: dict[str, dict[str, Any]] = {}
    for name, relative in INTEGRATION_ARTIFACTS.items():
        reference = validate_file_ref(artifacts[name], f"build artifact {name}")
        if Path(reference["path"]).resolve() != (REPO_ROOT / relative).resolve():
            raise ReadinessError(f"build artifact {name} points outside its canonical path")
        validated_artifacts[name] = reference

    source = exact_object(
        receipt["livegraph_source"],
        {"before", "after", "bind_headers", "source_library", "rebuild"},
        "LiveGraph source receipt",
    )
    source_before = validate_git_receipt(source["before"], "LiveGraph source.before")
    source_after = validate_git_receipt(source["after"], "LiveGraph source.after")
    if source_before != source_after:
        raise ReadinessError("LiveGraph source Git identity changed during build")
    if git_state(Path(source_after["root"])) != source_after:
        raise ReadinessError("current clean LiveGraph source Git identity differs from the build receipt")
    source_library = validate_file_ref(source["source_library"], "LiveGraph source library")
    rebuild = exact_object(
        source["rebuild"],
        {"command", "cmake", "configure_stdout", "configure_stderr", "build_stdout", "build_stderr"},
        "LiveGraph source rebuild",
    )
    for name in rebuild:
        validate_file_ref(rebuild[name], f"LiveGraph source rebuild {name}")
    rebuild_command = exact_object(
        read_object(Path(rebuild["command"]["path"]), "LiveGraph source rebuild command"),
        {"configure_argv", "build_argv", "cwd", "environment", "source_head", "policy"},
        "LiveGraph source rebuild command",
    )
    if (
        rebuild_command["source_head"] != source_after["head"]
        or rebuild_command["policy"] != "clean-source-head-out-of-tree-cmake-release-v1"
        or rebuild_command["environment"] != {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
        or not isinstance(rebuild_command["configure_argv"], list)
        or not isinstance(rebuild_command["build_argv"], list)
        or str(Path(source_after["root"])) not in rebuild_command["configure_argv"]
        or "livegraph" not in rebuild_command["build_argv"]
    ):
        raise ReadinessError("LiveGraph source rebuild is not bound to the clean source HEAD")
    bind_headers = exact_object(
        source["bind_headers"], {"path", "git_bound_by_head"}, "LiveGraph bind headers"
    )
    if (
        bind_headers["git_bound_by_head"] is not True
        or Path(str(bind_headers["path"])).resolve() != Path(source_after["root"]) / "bind"
        or not Path(str(bind_headers["path"])).resolve().is_dir()
    ):
        raise ReadinessError("LiveGraph bind headers are not bound by the frozen source HEAD")

    config = exact_object(receipt["config"], {"path", "sha256"}, "LiveGraph build config")
    checked_file(Path(str(config["path"])), str(config["sha256"]), "LiveGraph build config")
    compiler = exact_object(
        receipt["compiler"], {"path", "size_bytes", "sha256", "version"}, "LiveGraph compiler"
    )
    validate_file_ref(
        {key: compiler[key] for key in ("path", "size_bytes", "sha256")},
        "LiveGraph compiler",
    )
    if not isinstance(compiler["version"], list) or not all(
        isinstance(line, str) for line in compiler["version"]
    ):
        raise ReadinessError("LiveGraph compiler version is malformed")
    build = exact_object(receipt["build"], {"command", "flags", "stdout", "stderr"}, "LiveGraph build")
    if build["flags"] != ["-O2", "-std=c++17", "-Wall", "-Wextra", "-Wpedantic"]:
        raise ReadinessError("LiveGraph build flags drifted")
    for name in ("command", "stdout", "stderr"):
        validate_file_ref(build[name], f"LiveGraph build {name}")
    worker_command = exact_object(
        read_object(Path(build["command"]["path"]), "LiveGraph worker build command"),
        {"argv", "cwd", "environment", "policy"},
        "LiveGraph worker build command",
    )
    if (
        worker_command["environment"] != {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
        or worker_command["policy"] != "clean-current-head-native-build-v1"
        or worker_command["cwd"] != current["root"]
        or not isinstance(worker_command["argv"], list)
    ):
        raise ReadinessError("LiveGraph worker compiler environment/identity is not frozen")

    runtime = exact_object(
        receipt["runtime"],
        {
            "process_lifetime",
            "binary",
            "liblivegraph",
            "capabilities",
            "runtime_library_path",
            "ldd_tool",
            "ldd_output",
            "dependencies",
        },
        "LiveGraph build runtime",
    )
    if runtime["process_lifetime"] != PROCESS_LIFETIME:
        raise ReadinessError("LiveGraph build process lifetime drift")
    binary = validate_file_ref(runtime["binary"], "frozen LiveGraph worker")
    library = validate_file_ref(runtime["liblivegraph"], "frozen liblivegraph")
    if source_library["sha256"] != library["sha256"] or source_library["size_bytes"] != library["size_bytes"]:
        raise ReadinessError("frozen liblivegraph differs from the source-HEAD rebuilt library")
    if Path(str(runtime["runtime_library_path"])).resolve() != Path(library["path"]):
        raise ReadinessError("build receipt runtime library path drift")
    if system["binary"] != {"path": binary["path"], "sha256": binary["sha256"]}:
        raise ReadinessError("formal system binary differs from the build receipt")
    if system["runtime_libraries"] != [{"path": library["path"], "sha256": library["sha256"]}]:
        raise ReadinessError("formal system liblivegraph differs from the build receipt")
    capability_ref = validate_file_ref(runtime["capabilities"], "LiveGraph capability receipt")
    validate_file_ref(runtime["ldd_tool"], "LiveGraph ldd tool")
    validate_file_ref(runtime["ldd_output"], "LiveGraph ldd output")
    capability = exact_object(
        read_object(Path(capability_ref["path"]), "LiveGraph capabilities"),
        {"schema_version", "process_lifetime", "runtime_library_path"},
        "LiveGraph capabilities",
    )
    if (
        capability["schema_version"] != "cidr-livegraph-p10-worker-v1"
        or capability["process_lifetime"] != PROCESS_LIFETIME
        or Path(str(capability["runtime_library_path"])).resolve() != Path(library["path"])
    ):
        raise ReadinessError("LiveGraph capability receipt differs from the frozen runtime")
    dependencies = runtime["dependencies"]
    if not isinstance(dependencies, list):
        raise ReadinessError("LiveGraph dependency graph is not an array")
    livegraph_dependencies = []
    for index, dependency in enumerate(dependencies):
        value = exact_object(
            dependency,
            {"soname", "path", "size_bytes", "sha256"},
            f"LiveGraph dependency {index}",
        )
        reference = validate_file_ref(
            {key: value[key] for key in ("path", "size_bytes", "sha256")},
            f"LiveGraph dependency {index}",
        )
        if value["soname"] == "liblivegraph.so":
            livegraph_dependencies.append(reference)
    if livegraph_dependencies != [library]:
        raise ReadinessError("ldd graph does not bind exactly the frozen liblivegraph")
    return {
        "receipt": receipt_ref,
        "marker": {
            "path": str(marker_path.resolve()),
            "sha256": sha256_file(marker_path),
            "size_bytes": marker_path.stat().st_size,
        },
        "integration": current,
        "livegraph_source": source_after,
        "source_library": source_library,
        "binary": binary,
        "liblivegraph": library,
        "artifacts": validated_artifacts,
    }


def current_p31_host() -> dict[str, str]:
    module_path = P31_DIR / "run_manifest.py"
    spec = importlib.util.spec_from_file_location("livegraph_sf10_p31_manifest", module_path)
    if spec is None or spec.loader is None:
        raise ReadinessError("cannot load the canonical P31 host-fingerprint implementation")
    sys.path.insert(0, str(P31_DIR))
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    facts = module.host_facts()
    if (
        not isinstance(facts, dict)
        or not isinstance(facts.get("hostname"), str)
        or not HEX64_RE.fullmatch(str(facts.get("fingerprint_sha256", "")))
    ):
        raise ReadinessError("canonical P31 host facts are malformed")
    return {"hostname": facts["hostname"], "fingerprint_sha256": facts["fingerprint_sha256"]}


def validate_p02b(
    adapter_args: dict[str, str],
    *,
    expected_repo_root: Path,
    expected_repo_head: str,
    expected_binary_sha256: str,
) -> dict[str, Any]:
    result = checked_file(
        Path(adapter_args["--p02b-result"]),
        adapter_args["--p02b-result-sha256"],
        "P02B sentinel result",
    )
    validator = checked_file(
        Path(adapter_args["--p02b-validator"]),
        adapter_args["--p02b-validator-sha256"],
        "P02B validator",
    )
    command = [
        sys.executable,
        validator["path"],
        "--result",
        result["path"],
        "--consumer",
        "P10",
        "--require-formal",
        "--max-age-seconds",
        str(P02B_MAX_AGE_SECONDS),
        "--expected-repo-root",
        str(expected_repo_root.resolve()),
        "--expected-repo-head",
        expected_repo_head,
        "--expected-binary-sha256",
        expected_binary_sha256,
    ]
    env = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        completed = subprocess.run(
            command,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReadinessError(f"P02B admission could not execute: {exc}") from exc
    if completed.returncode != 0:
        raise ReadinessError(f"P02B admission failed: {completed.stderr.strip()}")
    try:
        admission = json.loads(completed.stdout, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, ReadinessError) as exc:
        raise ReadinessError(f"P02B admission returned malformed JSON: {exc}") from exc
    admission = exact_object(
        admission,
        {
            "state",
            "consumer",
            "formal_required",
            "fixture_only",
            "scope",
            "sentinel_result",
            "sentinel_result_sha256",
            "pass_marker",
            "pass_marker_sha256",
            "provenance",
            "provenance_sha256",
            "scale",
            "run_id",
            "completed_at_utc",
            "repo_root",
            "repo_head",
            "binary_sha256",
            "host",
            "protocol",
        },
        "P02B admission",
    )
    if (
        admission["state"] != "PASS"
        or admission["consumer"] != "P10"
        or admission["formal_required"] is not True
        or admission["fixture_only"] is not False
        or admission["scope"] != "host-global"
        or admission["scale"] != "sf10"
        or Path(str(admission["sentinel_result"])).resolve() != Path(result["path"])
        or admission["sentinel_result_sha256"] != result["sha256"]
        or Path(str(admission["repo_root"])).resolve() != expected_repo_root.resolve()
        or admission["repo_head"] != expected_repo_head
        or admission["binary_sha256"] != expected_binary_sha256
        or admission["host"] != current_p31_host()
    ):
        raise ReadinessError("P02B admission identity/classification/host drift")
    checked_file(
        Path(str(admission["pass_marker"])),
        str(admission["pass_marker_sha256"]),
        "P02B PASS marker",
    )
    checked_file(
        Path(str(admission["provenance"])),
        str(admission["provenance_sha256"]),
        "P02B provenance",
    )
    return {"result": result, "validator": validator, "admission": admission}


def validate_canonical_suite(
    manifest: Path,
    run_root: Path,
    *,
    manifest_base_dir: Path | None = None,
) -> tuple[dict[str, Any], list[dict[str, int]], dict[str, Any], dict[str, str]]:
    try:
        suite, truth_rows = load_suite_manifest(
            manifest.resolve(),
            repo_root=REPO_ROOT,
            run_root=run_root.resolve(),
            mode="formal",
            manifest_base_dir=manifest_base_dir,
        )
    except Exception as exc:  # shared contract errors are part of this fail-closed gate
        raise ReadinessError(f"formal suite manifest is invalid: {exc}") from exc
    if suite.get("fixture_only") is not False:
        raise ReadinessError("formal LiveGraph wrapper rejects fixture suites")
    validate_protocol(suite)
    dataset = suite["dataset"]
    dataset_path = Path(dataset["path"]).resolve()
    if dataset["sha256"] != CANONICAL_DENSE_SHA256 or dataset_path.stat().st_size != CANONICAL_DENSE_BYTES:
        raise ReadinessError("suite dataset is not the canonical SF10 dense edge stream")
    truth = suite["truth"]
    if (
        truth["sha256"] != CANONICAL_TRUTH_SHA256
        or truth["query_count"] != CANONICAL_TRUTH_QUERIES
        or truth["digest_algorithm"] != TRUTH_DIGEST_ALGORITHM
        or len(truth_rows) != CANONICAL_TRUTH_QUERIES
    ):
        raise ReadinessError("suite truth is not the canonical 1,700-query SF10 truth")
    systems = [system for system in suite["systems"] if system["id"] == "livegraph"]
    if len(systems) != 1:
        raise ReadinessError("formal suite must contain exactly one LiveGraph system")
    system = systems[0]
    if (
        system["group"] != "embedded"
        or system["interface_scope"] != INTERFACE_SCOPE
        or system["fixture_only"] is not False
        or system["service_lifecycle"] != "in-process"
        or system["process_lifetime"] != PROCESS_LIFETIME
        or system["containers"]
        or system["extra_pids"]
        or system["image_digests"]
        or len(system["store_roots"]) != 1
        or system["store_roots"][0]["label"] != "livegraph"
        or system["store_roots"][0]["sha256"] != ""
        or len(system["temp_roots"]) != 1
        or system["temp_roots"][0]["label"] != "scratch"
    ):
        raise ReadinessError("formal LiveGraph system lifecycle/root contract drift")
    if Path(system["adapter"]["path"]).resolve() != (P10_DIR / "adapters/livegraph_adapter.py").resolve():
        raise ReadinessError("formal suite points to a non-canonical LiveGraph adapter")
    adapter_args = parse_adapter_args(system["adapter"]["args"])
    temp_base = Path(adapter_args["--temp-base"]).resolve()
    declared_temp = Path(system["temp_roots"][0]["path"]).resolve()
    if temp_base != declared_temp:
        raise ReadinessError("formal LiveGraph --temp-base differs from system.temp_roots[0]")
    for other, label in (
        (run_root.resolve(), "run/output root"),
        (REPO_ROOT.resolve(), "integration root"),
        (Path(system["store_roots"][0]["path"]).resolve(), "store base"),
    ):
        if temp_base == other or temp_base in other.parents or other in temp_base.parents:
            raise ReadinessError(f"formal LiveGraph temp base overlaps {label}")
    if Path(adapter_args["--p02b-validator"]).resolve() != (
        REPO_ROOT / "cidr-experiments/runners/p02b/validate_sentinel_result.py"
    ).resolve():
        raise ReadinessError("formal suite points to a non-canonical P02B validator")
    return suite, truth_rows, system, adapter_args


def file_ref(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ReadinessError(f"formal evidence is not one regular non-symlink file: {path}")
    resolved = path.resolve()
    return {
        "path": str(resolved),
        "size_bytes": resolved.stat().st_size,
        "sha256": sha256_file(resolved),
    }


def validate_stage_journal(
    rows: list[dict[str, Any]],
    provenance: dict[str, Any],
    start_receipt: dict[str, Any],
    exit_receipt: dict[str, Any],
) -> None:
    expected_stages = [
        "preflight-passed",
        "worker-started",
        "worker-exited",
        "worker-output-validated",
        "adapter-result-published",
        "adapter-complete",
    ]
    base_keys = {"schema_version", "sequence", "stage", "at_utc", "monotonic_ns"}
    extras = {
        "preflight-passed": {"execution_mode", "store_root", "temp_root"},
        "worker-started": {"pid", "proc_start_ticks"},
        "worker-exited": {"pid", "returncode"},
        "worker-output-validated": set(),
        "adapter-result-published": set(),
        "adapter-complete": set(),
    }
    if len(rows) != len(expected_stages):
        raise ReadinessError("formal LiveGraph stage count drift")
    previous_monotonic = -1
    for expected_sequence, (row, expected_stage) in enumerate(
        zip(rows, expected_stages), start=1
    ):
        if not isinstance(row, dict) or set(row) != base_keys | extras[expected_stage]:
            raise ReadinessError(f"formal LiveGraph {expected_stage} stage keys drifted")
        sequence = row["sequence"]
        monotonic_ns = row["monotonic_ns"]
        if (
            row["schema_version"] != "p10-livegraph-adapter-stage-v1"
            or row["stage"] != expected_stage
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence != expected_sequence
            or isinstance(monotonic_ns, bool)
            or not isinstance(monotonic_ns, int)
            or monotonic_ns <= previous_monotonic
        ):
            raise ReadinessError(f"formal LiveGraph {expected_stage} stage identity drifted")
        at_utc = row["at_utc"]
        try:
            parsed_at = datetime.fromisoformat(
                at_utc[:-1] + "+00:00" if isinstance(at_utc, str) and at_utc.endswith("Z") else ""
            )
        except ValueError as exc:
            raise ReadinessError(f"formal LiveGraph {expected_stage} timestamp is malformed") from exc
        if not isinstance(at_utc, str) or not at_utc.endswith("Z") or parsed_at.utcoffset() != timezone.utc.utcoffset(None):
            raise ReadinessError(f"formal LiveGraph {expected_stage} timestamp is not UTC")
        previous_monotonic = monotonic_ns

    store = provenance.get("store")
    temp = provenance.get("temp")
    preflight, started, exited = rows[:3]
    if (
        not isinstance(store, dict)
        or not isinstance(temp, dict)
        or preflight["execution_mode"] != provenance.get("execution_mode")
        or preflight["store_root"] != store.get("root")
        or preflight["temp_root"] != temp.get("root")
    ):
        raise ReadinessError("formal LiveGraph preflight stage/provenance binding drift")
    if (
        isinstance(started["pid"], bool)
        or not isinstance(started["pid"], int)
        or isinstance(started["proc_start_ticks"], bool)
        or not isinstance(started["proc_start_ticks"], int)
        or started["pid"] <= 0
        or started["proc_start_ticks"] <= 0
        or isinstance(exited["pid"], bool)
        or not isinstance(exited["pid"], int)
        or isinstance(exited["returncode"], bool)
        or not isinstance(exited["returncode"], int)
    ):
        raise ReadinessError("formal LiveGraph worker stage numeric identity drift")
    if (
        started["pid"] != start_receipt.get("pid")
        or started["proc_start_ticks"] != start_receipt.get("proc_start_ticks")
    ):
        raise ReadinessError("formal LiveGraph worker-started stage/receipt binding drift")
    if (
        exited["pid"] != exit_receipt.get("pid")
        or exited["returncode"] != exit_receipt.get("returncode")
    ):
        raise ReadinessError("formal LiveGraph worker-exited stage/receipt binding drift")


def snapshot_manifest(source: Path, destination: Path) -> dict[str, dict[str, Any]]:
    """Freeze the exact input bytes while retaining the source directory for token resolution."""

    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise ReadinessError(f"cannot snapshot formal suite manifest: {exc}") from exc
    digest = hashlib.sha256(payload).hexdigest()
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    descriptor = os.open(destination, flags, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("short write while snapshotting formal suite manifest")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    destination.chmod(0o444)
    return {
        "source_at_snapshot": {
            "path": str(source.resolve()),
            "size_bytes": len(payload),
            "sha256": digest,
        },
        "snapshot": file_ref(destination),
    }


def validate_completed_run(
    run_root: Path,
    expected_manifest: dict[str, Any],
) -> dict[str, Any]:
    manifest_snapshot_path = Path(str(expected_manifest.get("path", "")))
    if file_ref(manifest_snapshot_path) != expected_manifest:
        raise ReadinessError("formal manifest snapshot changed after preflight")
    summary_path = run_root / "suite-summary.json"
    summary = read_object(summary_path, "LiveGraph suite summary")
    if (
        summary.get("state") != "PASS"
        or summary.get("mode") != "formal"
        or summary.get("performance_eligible") is not True
        or summary.get("fixture_only") is not False
        or summary.get("complete_frozen_suite") is not False
        or summary.get("selected_systems") != ["livegraph"]
        or summary.get("system_count") != 1
        or summary.get("repeat_count") != 3
    ):
        raise ReadinessError("completed LiveGraph suite summary identity/classification drift")
    validate_protocol(summary)
    resolved_manifest_path = run_root / "resolved-suite-manifest.json"
    resolved_manifest = read_object(
        resolved_manifest_path, "LiveGraph resolved suite manifest"
    )
    resolved_sha = sha256_file(resolved_manifest_path)
    source_manifest = resolved_manifest.get("source_manifest")
    if (
        not isinstance(source_manifest, dict)
        or Path(str(source_manifest.get("path", ""))).resolve()
        != Path(str(expected_manifest.get("path", ""))).resolve()
        or source_manifest.get("sha256") != expected_manifest.get("sha256")
        or summary.get("resolved_manifest_sha256") != resolved_sha
        or summary.get("suite_id") != resolved_manifest.get("suite_id")
        or summary.get("protocol") != resolved_manifest.get("protocol")
        or summary.get("truth") != resolved_manifest.get("truth")
    ):
        raise ReadinessError("completed LiveGraph run is not bound to the preflight manifest snapshot")
    marker_path = run_root / "PARTIAL-DONE"
    if (
        (run_root / "DONE").exists()
        or (run_root / "FAILED").exists()
        or (run_root / "RUNNING").exists()
    ):
        raise ReadinessError("single-system LiveGraph run has an invalid terminal marker")
    marker = exact_object(
        read_object(marker_path, "LiveGraph PARTIAL-DONE"),
        {
            "state", "complete_frozen_suite", "performance_eligible",
            "summary_sha256", "resolved_manifest_sha256",
        },
        "LiveGraph PARTIAL-DONE",
    )
    if (
        marker.get("state") != "PASS"
        or marker.get("complete_frozen_suite") is not False
        or marker.get("performance_eligible") is not True
        or marker.get("summary_sha256") != sha256_file(summary_path)
        or marker.get("resolved_manifest_sha256") != resolved_sha
    ):
        raise ReadinessError("LiveGraph PARTIAL-DONE does not bind the formal summary")

    repeat_results = run_root / "repeat-results.tsv"
    system_results = run_root / "system-results.tsv"
    expected_summary_refs = {
        "repeat_results": {
            "path": str(repeat_results.resolve()),
            "sha256": sha256_file(repeat_results),
        },
        "system_results": {
            "path": str(system_results.resolve()),
            "sha256": sha256_file(system_results),
        },
    }
    for label, expected in expected_summary_refs.items():
        if summary.get(label) != expected:
            raise ReadinessError(f"LiveGraph summary {label} reference drift")
    try:
        with repeat_results.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ReadinessError(f"cannot read LiveGraph repeat results: {exc}") from exc
    if len(rows) != 3:
        raise ReadinessError("formal LiveGraph run must contain exactly three repeat rows")
    declared_evidence = summary.get("repeat_evidence")
    if not isinstance(declared_evidence, list) or len(declared_evidence) != 3:
        raise ReadinessError("formal LiveGraph summary lacks exactly three repeat evidence chains")
    p31: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        if (
            row.get("system_id") != "livegraph"
            or row.get("repeat_index") != str(index)
            or row.get("process_lifetime") != PROCESS_LIFETIME
            or row.get("warmup_passes") != "1"
            or row.get("measured_passes") != "1"
            or row.get("query_count") != str(CANONICAL_TRUTH_QUERIES)
            or row.get("timeout_queries") != "0"
            or row.get("mismatch_queries") != "0"
        ):
            raise ReadinessError(f"formal LiveGraph repeat {index} coverage/correctness drift")
        p31_root = Path(str(row.get("p31_run_dir", ""))).resolve()
        expected_root = (run_root / f"systems/livegraph/repeat-{index:02d}/p31").resolve()
        if p31_root != expected_root:
            raise ReadinessError(f"formal LiveGraph repeat {index} points to another P31 run")
        matching_evidence = [
            item for item in declared_evidence
            if isinstance(item, dict)
            and item.get("system_id") == "livegraph"
            and item.get("repeat_index") == index
        ]
        if len(matching_evidence) != 1:
            raise ReadinessError(f"formal LiveGraph repeat {index} evidence identity is missing/ambiguous")
        declared = matching_evidence[0]
        evidence_files = {
            "validated_result": expected_root.parent / "validated-result.json",
            "adapter_provenance": expected_root.parent / "adapter-output/adapter-provenance.json",
            "adapter_stages": expected_root.parent / "adapter-output/adapter-stage-events.jsonl",
            "worker_start": expected_root.parent / "adapter-output/worker-start.json",
            "worker_exit": expected_root.parent / "adapter-output/worker-exit.json",
            "worker_stdout": expected_root.parent / "adapter-output/livegraph-worker.stdout.log",
            "worker_stderr": expected_root.parent / "adapter-output/livegraph-worker.stderr.log",
            "post_p31_store_seal": expected_root.parent / "livegraph-post-p31-store-seal.json",
        }
        normalized_evidence: dict[str, Any] = {}
        if set(declared) != {"system_id", "repeat_index", *evidence_files}:
            raise ReadinessError(f"formal LiveGraph repeat {index} evidence keys drifted")
        for label, path in evidence_files.items():
            current = file_ref(path)
            if declared.get(label) != current:
                raise ReadinessError(f"formal LiveGraph repeat {index} {label} hash/path drift")
            normalized_evidence[label] = current
        validated_result = read_object(
            evidence_files["validated_result"], f"LiveGraph repeat {index} validated result"
        )
        adapter_provenance = read_object(
            evidence_files["adapter_provenance"], f"LiveGraph repeat {index} adapter provenance"
        )
        if (
            validated_result.get("schema_version") != "cidr-p10-validated-repeat-v1"
            or validated_result.get("system_id") != "livegraph"
            or validated_result.get("repeat_index") != index
            or validated_result.get("adapter_provenance") != adapter_provenance
        ):
            raise ReadinessError(f"formal LiveGraph repeat {index} validated/provenance content drift")
        try:
            stage_rows = [
                json.loads(line, object_pairs_hook=unique_object)
                for line in evidence_files["adapter_stages"].read_text(encoding="utf-8").splitlines()
                if line
            ]
        except (OSError, UnicodeError, json.JSONDecodeError, ReadinessError) as exc:
            raise ReadinessError(f"formal LiveGraph repeat {index} stage journal is malformed: {exc}") from exc
        start_receipt = read_object(
            evidence_files["worker_start"], f"LiveGraph repeat {index} worker start"
        )
        exit_receipt = read_object(
            evidence_files["worker_exit"], f"LiveGraph repeat {index} worker exit"
        )
        validate_stage_journal(stage_rows, adapter_provenance, start_receipt, exit_receipt)
        lifecycle = adapter_provenance.get("worker_lifecycle")
        if (
            not isinstance(lifecycle, dict)
            or lifecycle.get("identity") != exit_receipt
            or start_receipt.get("pid") != exit_receipt.get("pid")
            or start_receipt.get("proc_start_ticks") != exit_receipt.get("proc_start_ticks")
            or exit_receipt.get("returncode") != 0
            or exit_receipt.get("same_process_alive_after_wait") is not False
            or exit_receipt.get("process_group_id") != exit_receipt.get("pid")
            or exit_receipt.get("process_group_members_after_wait") != []
        ):
            raise ReadinessError(f"formal LiveGraph repeat {index} worker lifecycle receipt drift")
        try:
            p31_summary = read_p31_summary(expected_root, performance_eligible=True)
            validate_livegraph_dataset_terminal_identity(adapter_provenance)
            validate_livegraph_p31_binding(adapter_provenance, p31_summary)
            sealed = validate_livegraph_post_p31_store_seal(
                adapter_provenance,
                p31_summary,
                declared["post_p31_store_seal"],
            )
        except ContractError as exc:
            raise ReadinessError(
                f"formal LiveGraph repeat {index} post-P31 store seal is invalid: {exc}"
            ) from exc
        if validated_result.get("livegraph_post_p31_store_seal") != sealed:
            raise ReadinessError(
                f"formal LiveGraph repeat {index} validated/store-seal content drift"
            )
        p31.append(
            {
                "run_dir": str(p31_root),
                "manifest": file_ref(p31_root / "run-manifest.json"),
                "validation": file_ref(p31_root / "validation.json"),
                "done": file_ref(p31_root / "DONE"),
                "evidence": normalized_evidence,
            }
        )
    return {
        "summary": file_ref(summary_path),
        "marker": file_ref(marker_path),
        "resolved_manifest": file_ref(resolved_manifest_path),
        "repeat_results": file_ref(repeat_results),
        "system_results": file_ref(system_results),
        "p31": p31,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--clean-ready-file", type=Path)
    parser.add_argument("--preflight-only", action="store_true")
    return parser.parse_args()


def resolve_output_roots(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    manifest = args.manifest.resolve()
    run_root = args.run_root.resolve()
    evidence = args.evidence_dir.resolve()
    if not run_root.is_absolute() or not evidence.is_absolute():
        raise ReadinessError("run/evidence roots must be absolute")
    for left, right in ((run_root, evidence), (REPO_ROOT.resolve(), evidence)):
        if left == right or left in right.parents or right in left.parents:
            raise ReadinessError("run, evidence, and Git roots must not overlap")
    return manifest, run_root, evidence


def claim_evidence_directory(evidence: Path) -> None:
    """Atomically select the sole process allowed to publish formal evidence."""

    if evidence.exists() or evidence.is_symlink():
        raise ReadinessError("evidence directory must not already exist")
    try:
        evidence.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ReadinessError("evidence directory was claimed concurrently") from exc
    if evidence.is_symlink() or not evidence.is_dir():
        raise ReadinessError("claimed evidence path is not one real directory")


def run(
    args: argparse.Namespace,
    *,
    claimed_roots: tuple[Path, Path, Path] | None = None,
) -> dict[str, Any]:
    if claimed_roots is None:
        claimed_roots = resolve_output_roots(args)
        claim_evidence_directory(claimed_roots[2])
    manifest, run_root, evidence = claimed_roots
    if evidence.is_symlink() or not evidence.is_dir() or any(evidence.iterdir()):
        raise ReadinessError("formal evidence ownership was not preserved before preflight")
    if not manifest.is_file() or manifest.is_symlink():
        raise ReadinessError("--manifest must be one regular file")
    if not args.preflight_only:
        if args.clean_ready_file is None or not args.clean_ready_file.resolve().is_file():
            raise ReadinessError("formal execution requires --clean-ready-file")
        if run_root.exists() and any(run_root.iterdir()):
            raise ReadinessError("formal run root must be absent or empty")

    manifest_snapshot = snapshot_manifest(
        manifest, evidence / "input-suite-manifest.json"
    )
    suite, _, system, adapter_args = validate_canonical_suite(
        Path(manifest_snapshot["snapshot"]["path"]),
        run_root,
        manifest_base_dir=manifest.parent,
    )
    build = validate_build_receipt(
        Path(adapter_args["--build-receipt"]),
        adapter_args["--build-receipt-sha256"],
        system,
    )
    p02b = validate_p02b(
        adapter_args,
        expected_repo_root=Path(build["integration"]["root"]),
        expected_repo_head=build["integration"]["head"],
        expected_binary_sha256=system["binary"]["sha256"],
    )
    preflight = {
        "schema_version": SCHEMA,
        "state": "PASS",
        "scope": "canonical-sf10-formal-preflight-with-manifest-snapshot-v2",
        "performance_eligible": False,
        "formal_performance_points": 0,
        "created_at_utc": utc_now(),
        "manifest": manifest_snapshot["source_at_snapshot"],
        "manifest_snapshot": manifest_snapshot["snapshot"],
        "manifest_base_dir": str(manifest.parent.resolve()),
        "protocol": suite["protocol"],
        "dataset": {**suite["dataset"], "size_bytes": Path(suite["dataset"]["path"]).stat().st_size},
        "truth": {**suite["truth"], "size_bytes": Path(suite["truth"]["path"]).stat().st_size},
        "build": build,
        "p02b": p02b,
        "run_root": str(run_root),
    }
    preflight_path = evidence / "preflight-receipt.json"
    atomic_json(preflight_path, preflight)
    if args.preflight_only:
        marker = {
            "state": "PASS",
            "scope": preflight["scope"],
            "performance_eligible": False,
            "formal_performance_points": 0,
            "preflight_receipt_sha256": sha256_file(preflight_path),
        }
        atomic_json(evidence / "DONE.preflight", marker)
        return {**preflight, "terminal_marker": marker}

    command = [
        sys.executable,
        "-B",
        str(RUN_SUITE),
        "run",
        "--manifest",
        str(manifest_snapshot["snapshot"]["path"]),
        "--manifest-base-dir",
        str(manifest.parent.resolve()),
        "--run-root",
        str(run_root),
        "--mode",
        "formal",
        "--clean-ready-file",
        str(args.clean_ready_file.resolve()),
        "--system",
        "livegraph",
    ]
    atomic_json(evidence / "orchestrator-command.json", {"argv": command, "cwd": str(REPO_ROOT)})
    with (evidence / "orchestrator.stdout.log").open("wb") as stdout_handle, (
        evidence / "orchestrator.stderr.log"
    ).open("wb") as stderr_handle:
        completed = subprocess.run(
            command,
            cwd=str(REPO_ROOT),
            env={
                "PATH": "/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            stdout=stdout_handle,
            stderr=stderr_handle,
            check=False,
        )
    if completed.returncode != 0:
        raise ReadinessError(f"P10/P31 LiveGraph run exited {completed.returncode}")
    outputs = validate_completed_run(run_root, manifest_snapshot["snapshot"])
    result = {
        "schema_version": SCHEMA,
        "state": "PASS",
        "scope": "canonical-sf10-formal-livegraph-run-with-post-p31-store-seal-v2",
        "performance_eligible": True,
        "formal_performance_points": 3,
        "completed_at_utc": utc_now(),
        "preflight": file_ref(preflight_path),
        "outputs": outputs,
    }
    result_path = evidence / "formal-run-receipt.json"
    atomic_json(result_path, result)
    marker = {
        "state": "PASS",
        "scope": result["scope"],
        "performance_eligible": True,
        "formal_performance_points": 3,
        "result_sha256": sha256_file(result_path),
    }
    atomic_json(evidence / "DONE.formal", marker)
    return {**result, "terminal_marker": marker}


def publish_failure_if_owned(evidence: Path, owned: bool, exc: BaseException) -> None:
    """Only the process that created the evidence directory may write FAILED."""

    if (
        not owned
        or not evidence.is_dir()
        or (evidence / "DONE.formal").exists()
        or (evidence / "DONE.preflight").exists()
    ):
        return
    exclusive_json(
        evidence / "FAILED",
        {
            "state": "FAILED",
            "failed_at_utc": utc_now(),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "performance_eligible": False,
            "formal_performance_points": 0,
        },
        "formal failure marker",
    )


def main() -> int:
    args = parse_args()
    evidence = args.evidence_dir.resolve()
    evidence_claimed = False
    try:
        roots = resolve_output_roots(args)
        claim_evidence_directory(roots[2])
        evidence_claimed = True
        result = run(args, claimed_roots=roots)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (ReadinessError, OSError, subprocess.SubprocessError) as exc:
        try:
            publish_failure_if_owned(evidence, evidence_claimed, exc)
        except (ContractError, OSError) as publish_exc:
            print(
                f"run_sf10_formal: cannot publish owned FAILED marker: {publish_exc}",
                file=sys.stderr,
            )
        print(f"run_sf10_formal: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
