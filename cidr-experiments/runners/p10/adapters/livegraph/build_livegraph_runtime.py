#!/usr/bin/env python3
"""Build a frozen LiveGraph P10 worker and publish a fail-closed receipt.

The build runs from two clean Git trees and writes only to a new directory
outside both trees.  The receipt binds the current integration HEAD, the
LiveGraph source HEAD, every P10/P31 harness file consumed by the formal run,
the compiler, worker, liblivegraph, capability output, and resolved ldd graph.
It is build/correctness evidence only and never publishes a performance point.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CONFIG_SCHEMA = "p10-livegraph-runtime-build-config-v1"
RECEIPT_SCHEMA = "p10-livegraph-runtime-build-receipt-v1"
CAPABILITY_SCHEMA = "cidr-livegraph-p10-worker-v1"
PROCESS_LIFETIME = "fresh-import-and-query-process-lifetime-v1"
BUILD_POLICY = "clean-current-head-native-build-v1"
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

INTEGRATION_FILES = {
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


class BuildError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def atomic_text(path: Path, value: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def exact_object(value: object, keys: tuple[str, ...], context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BuildError(f"{context} must be an object")
    if set(value) != set(keys):
        raise BuildError(
            f"{context} keys differ: missing={sorted(set(keys) - set(value))}, "
            f"extra={sorted(set(value) - set(keys))}"
        )
    return value


def existing_file(path: Path, context: str, *, executable: bool = False) -> Path:
    result = path.resolve()
    if not result.is_file() or result.is_symlink():
        raise BuildError(f"{context} is not one regular file: {result}")
    if executable and not os.access(result, os.X_OK):
        raise BuildError(f"{context} is not executable: {result}")
    return result


def existing_directory(path: Path, context: str) -> Path:
    result = path.resolve()
    if not result.is_dir() or result.is_symlink():
        raise BuildError(f"{context} is not one real directory: {result}")
    return result


def artifact(path: Path) -> dict[str, Any]:
    value = existing_file(path, "artifact")
    return {
        "path": str(value),
        "size_bytes": value.stat().st_size,
        "sha256": sha256_file(value),
    }


def checked(
    argv: list[str],
    context: str,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BuildError(f"{context} could not execute: {exc}") from exc
    if completed.returncode != 0:
        raise BuildError(
            f"{context} failed ({completed.returncode})\nstdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    return completed


def git_state(root: Path, context: str) -> dict[str, Any]:
    root = existing_directory(root, context)
    top = checked(["git", "-C", str(root), "rev-parse", "--show-toplevel"], f"{context} root").stdout.strip()
    if Path(top).resolve() != root:
        raise BuildError(f"{context} is not a Git worktree root: {root}")
    head = checked(["git", "-C", str(root), "rev-parse", "HEAD"], f"{context} HEAD").stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise BuildError(f"{context} HEAD is malformed")
    status_lines = checked(
        ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=normal"],
        f"{context} status",
    ).stdout.splitlines()
    return {
        "root": str(root),
        "head": head,
        "dirty": bool(status_lines),
        "status_lines": status_lines,
    }


def require_clean_unchanged(before: dict[str, Any], after: dict[str, Any], context: str) -> None:
    if before["dirty"] or before["status_lines"]:
        raise BuildError(f"{context} was dirty before build: {before['status_lines']!r}")
    if after["dirty"] or after["status_lines"]:
        raise BuildError(f"{context} became dirty during build: {after['status_lines']!r}")
    if before["root"] != after["root"] or before["head"] != after["head"]:
        raise BuildError(f"{context} Git identity changed during build")


def load_config(path: Path) -> dict[str, Any]:
    config_path = existing_file(path, "build config")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BuildError(f"cannot read build config: {exc}") from exc
    config = exact_object(
        raw,
        ("schema_version", "integration_root", "livegraph_source_root", "output_dir", "compiler"),
        "build config",
    )
    if config["schema_version"] != CONFIG_SCHEMA:
        raise BuildError(f"build config schema must be {CONFIG_SCHEMA!r}")
    for key in ("integration_root", "livegraph_source_root", "output_dir", "compiler"):
        if not isinstance(config[key], str) or not config[key] or not Path(config[key]).is_absolute():
            raise BuildError(f"build config {key} must be one absolute path")
    return {**config, "path": str(config_path), "sha256": sha256_file(config_path)}


def parse_ldd(text: str, frozen_library: Path) -> list[dict[str, Any]]:
    dependencies: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("linux-vdso"):
            continue
        if "=> not found" in line:
            raise BuildError(f"ldd has an unresolved dependency: {line}")
        soname: str
        raw_path: str
        if "=>" in line:
            soname, remainder = [part.strip() for part in line.split("=>", 1)]
            raw_path = remainder.split(" (", 1)[0].strip()
        else:
            raw_path = line.split(" (", 1)[0].strip()
            soname = Path(raw_path).name
        path = existing_file(Path(raw_path), f"ldd dependency {soname}")
        dependencies.append({"soname": soname, **artifact(path)})
    if not dependencies:
        raise BuildError("ldd did not resolve any file dependencies")
    livegraph = [item for item in dependencies if item["soname"] == "liblivegraph.so"]
    if len(livegraph) != 1 or Path(livegraph[0]["path"]).resolve() != frozen_library.resolve():
        raise BuildError("ldd did not resolve exactly the frozen liblivegraph.so")
    return dependencies


def write_sha_manifest(root: Path) -> Path:
    excluded = {"SHA256SUMS", "DONE.build", "FAILED"}
    files = sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and path.name not in excluded and not path.is_symlink()
        ),
        key=lambda path: path.relative_to(root).as_posix().encode("utf-8"),
    )
    lines = [f"{sha256_file(path)}  {path.relative_to(root).as_posix()}" for path in files]
    target = root / "SHA256SUMS"
    atomic_text(target, "\n".join(lines) + "\n")
    return target


def terminal_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def publish_failed(output: Path, exc: BaseException) -> bool:
    """Publish FAILED only for this process's newly claimed output directory."""
    done = output / "DONE.build"
    failed = output / "FAILED"
    if terminal_exists(done) or terminal_exists(failed):
        return False
    atomic_json(
        failed,
        {
            "state": "FAILED",
            "failed_at_utc": utc_now(),
            "error_type": type(exc).__name__,
            "error": str(exc),
        },
    )
    return True


def claim_output(config: dict[str, Any]) -> tuple[Path, Path, Path]:
    """Atomically claim a safe, previously absent output root for one attempt."""
    integration_root = Path(config["integration_root"]).resolve()
    source_root = Path(config["livegraph_source_root"]).resolve()
    configured_output = Path(config["output_dir"])
    output = configured_output.resolve()
    if (
        configured_output.exists()
        or configured_output.is_symlink()
        or output.exists()
        or output.is_symlink()
    ):
        raise BuildError(f"output directory already exists: {output}")
    for root, label in ((integration_root, "integration"), (source_root, "LiveGraph source")):
        if output == root or root in output.parents or output in root.parents:
            raise BuildError(f"output directory overlaps the {label} worktree")
    try:
        output.mkdir(parents=True)
    except FileExistsError as exc:
        raise BuildError(f"output directory already exists: {output}") from exc
    return integration_root, source_root, output


def _run_claimed(
    config: dict[str, Any],
    integration_root: Path,
    source_root: Path,
    output: Path,
) -> dict[str, Any]:
    integration_root = existing_directory(integration_root, "integration root")
    source_root = existing_directory(source_root, "LiveGraph source root")

    integration_before = git_state(integration_root, "integration root")
    source_before = git_state(source_root, "LiveGraph source root")
    require_clean_unchanged(integration_before, integration_before, "integration root")
    require_clean_unchanged(source_before, source_before, "LiveGraph source root")

    integration_artifacts: dict[str, dict[str, Any]] = {}
    for name, relative in INTEGRATION_FILES.items():
        integration_artifacts[name] = artifact(integration_root / relative)
    worker_source = Path(integration_artifacts["worker_source"]["path"])
    source_bind = existing_directory(source_root / "bind", "LiveGraph bind headers")
    compiler = existing_file(Path(config["compiler"]), "compiler", executable=True)
    build_env = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
    compiler_version = checked(
        [str(compiler), "--version"], "compiler version", env=build_env, timeout=30
    ).stdout

    bin_dir = output / "bin"
    lib_dir = output / "lib"
    evidence_dir = output / "evidence"
    snapshot_dir = evidence_dir / "sources"
    source_build_dir = output / "source-build"
    for directory in (bin_dir, lib_dir, evidence_dir, snapshot_dir, source_build_dir):
        directory.mkdir()
    cmake_raw = shutil.which("cmake", path=build_env["PATH"])
    if cmake_raw is None:
        raise BuildError("cmake is required to rebuild liblivegraph from source HEAD")
    cmake = existing_file(Path(cmake_raw), "cmake", executable=True)
    configure_argv = [
        str(cmake), "-S", str(source_root), "-B", str(source_build_dir),
        "-DCMAKE_BUILD_TYPE=Release", "-DBUILD_TESTING=OFF",
        f"-DCMAKE_CXX_COMPILER={compiler}",
    ]
    configure_completed = checked(
        configure_argv, "configure LiveGraph source HEAD", cwd=output, env=build_env, timeout=300
    )
    source_build_argv = [
        str(cmake), "--build", str(source_build_dir), "--target", "livegraph", "--parallel", "1"
    ]
    source_build_completed = checked(
        source_build_argv, "rebuild LiveGraph source HEAD", cwd=output, env=build_env, timeout=900
    )
    source_library = existing_file(source_build_dir / "liblivegraph.so", "rebuilt source liblivegraph")
    source_build_command = evidence_dir / "source-build-command.json"
    atomic_json(
        source_build_command,
        {
            "configure_argv": configure_argv,
            "build_argv": source_build_argv,
            "cwd": str(output),
            "environment": build_env,
            "source_head": source_before["head"],
            "policy": "clean-source-head-out-of-tree-cmake-release-v1",
        },
    )
    atomic_text(evidence_dir / "source-configure.stdout.log", configure_completed.stdout)
    atomic_text(evidence_dir / "source-configure.stderr.log", configure_completed.stderr)
    atomic_text(evidence_dir / "source-build.stdout.log", source_build_completed.stdout)
    atomic_text(evidence_dir / "source-build.stderr.log", source_build_completed.stderr)
    frozen_library = lib_dir / "liblivegraph.so"
    shutil.copy2(source_library, frozen_library)
    frozen_library.chmod(stat.S_IMODE(frozen_library.stat().st_mode) | stat.S_IRUSR)
    worker = bin_dir / "livegraph_p10_driver"
    flags = ["-O2", "-std=c++17", "-Wall", "-Wextra", "-Wpedantic"]
    build_argv = [
        str(compiler),
        *flags,
        f"-I{source_bind}",
        str(worker_source),
        f"-L{source_library.parent}",
        "-llivegraph",
        "-ldl",
        "-lpthread",
        "-o",
        str(worker),
    ]
    build_started = utc_now()
    completed = checked(
        build_argv, "LiveGraph worker build", cwd=integration_root, env=build_env, timeout=300
    )
    build_completed = utc_now()
    if completed.stdout:
        atomic_text(evidence_dir / "build.stdout.log", completed.stdout)
    else:
        atomic_text(evidence_dir / "build.stdout.log", "")
    atomic_text(evidence_dir / "build.stderr.log", completed.stderr)
    worker.chmod(stat.S_IMODE(worker.stat().st_mode) | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    for name in (
        "worker_source",
        "external_driver_makefile",
        "adapter",
        "formal_system_template",
        "sf10_formal_wrapper",
        "p02b_validator",
        "p10_contract",
        "p10_suite",
    ):
        source = Path(integration_artifacts[name]["path"])
        shutil.copy2(source, snapshot_dir / f"{name}-{source.name}")

    build_command_path = evidence_dir / "build-command.json"
    atomic_json(
        build_command_path,
        {
            "argv": build_argv,
            "cwd": str(integration_root),
            "environment": build_env,
            "policy": BUILD_POLICY,
        },
    )
    runtime_env = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "LD_LIBRARY_PATH": str(lib_dir),
    }
    capability_completed = checked(
        [str(worker), "--capabilities"],
        "LiveGraph worker capability",
        env=runtime_env,
        timeout=30,
    )
    try:
        capability = json.loads(capability_completed.stdout)
    except json.JSONDecodeError as exc:
        raise BuildError(f"worker capability output is malformed: {exc}") from exc
    capability = exact_object(
        capability,
        ("schema_version", "process_lifetime", "runtime_library_path"),
        "worker capability",
    )
    if capability != {
        "schema_version": CAPABILITY_SCHEMA,
        "process_lifetime": PROCESS_LIFETIME,
        "runtime_library_path": str(frozen_library.resolve()),
    }:
        raise BuildError(f"worker capability differs from the frozen runtime: {capability!r}")
    capability_path = evidence_dir / "runtime-capabilities.json"
    atomic_json(capability_path, capability)

    ldd_tool_raw = shutil.which("ldd")
    if ldd_tool_raw is None:
        raise BuildError("ldd is required")
    ldd_tool = existing_file(Path(ldd_tool_raw), "ldd", executable=True)
    ldd_completed = checked([str(ldd_tool), str(worker)], "ldd", env=runtime_env, timeout=30)
    ldd_path = evidence_dir / "ldd.txt"
    atomic_text(ldd_path, ldd_completed.stdout)
    dependencies = parse_ldd(ldd_completed.stdout, frozen_library)

    integration_after = git_state(integration_root, "integration root")
    source_after = git_state(source_root, "LiveGraph source root")
    require_clean_unchanged(integration_before, integration_after, "integration root")
    require_clean_unchanged(source_before, source_after, "LiveGraph source root")
    if sha256_file(Path(config["path"])) != config["sha256"]:
        raise BuildError("build config changed during the build")

    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "state": "PASS",
        "scope": "build-and-runtime-correctness-only",
        "performance_eligible": False,
        "formal_performance_points": 0,
        "build_policy": BUILD_POLICY,
        "started_at_utc": build_started,
        "completed_at_utc": build_completed,
        "config": {
            "path": config["path"],
            "sha256": config["sha256"],
        },
        "integration": {
            "before": integration_before,
            "after": integration_after,
            "artifacts": integration_artifacts,
        },
        "livegraph_source": {
            "before": source_before,
            "after": source_after,
            "bind_headers": {"path": str(source_bind), "git_bound_by_head": True},
            "source_library": artifact(source_library),
            "rebuild": {
                "command": artifact(source_build_command),
                "cmake": artifact(cmake),
                "configure_stdout": artifact(evidence_dir / "source-configure.stdout.log"),
                "configure_stderr": artifact(evidence_dir / "source-configure.stderr.log"),
                "build_stdout": artifact(evidence_dir / "source-build.stdout.log"),
                "build_stderr": artifact(evidence_dir / "source-build.stderr.log"),
            },
        },
        "compiler": {
            **artifact(compiler),
            "version": compiler_version.splitlines(),
        },
        "build": {
            "command": artifact(build_command_path),
            "flags": flags,
            "stdout": artifact(evidence_dir / "build.stdout.log"),
            "stderr": artifact(evidence_dir / "build.stderr.log"),
        },
        "runtime": {
            "process_lifetime": PROCESS_LIFETIME,
            "binary": artifact(worker),
            "liblivegraph": artifact(frozen_library),
            "capabilities": artifact(capability_path),
            "runtime_library_path": str(frozen_library.resolve()),
            "ldd_tool": artifact(ldd_tool),
            "ldd_output": artifact(ldd_path),
            "dependencies": dependencies,
        },
    }
    receipt_path = output / "build-receipt.json"
    atomic_json(receipt_path, receipt)
    sha_manifest = write_sha_manifest(output)
    marker = {
        "state": "PASS",
        "scope": receipt["scope"],
        "performance_eligible": False,
        "formal_performance_points": 0,
        "receipt": str(receipt_path),
        "receipt_sha256": sha256_file(receipt_path),
        "sha256sums": str(sha_manifest),
        "sha256sums_sha256": sha256_file(sha_manifest),
    }
    done = output / "DONE.build"
    failed = output / "FAILED"
    if terminal_exists(done) or terminal_exists(failed):
        raise BuildError("terminal marker exists before DONE.build publication")
    atomic_json(done, marker)
    return {**receipt, "receipt": marker}


def run(config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    integration_root, source_root, output = claim_output(config)
    try:
        return _run_claimed(config, integration_root, source_root, output)
    except BaseException as exc:
        try:
            publish_failed(output, exc)
        except OSError:
            pass
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        receipt = run(args.config.resolve())
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0
    except (BuildError, OSError, subprocess.SubprocessError) as exc:
        print(f"build_livegraph_runtime: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
