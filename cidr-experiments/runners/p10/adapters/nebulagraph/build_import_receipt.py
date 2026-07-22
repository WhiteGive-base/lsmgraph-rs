#!/usr/bin/env python3
"""Execute a pinned NebulaGraph importer and freeze wrapper-produced evidence."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

P10_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(P10_DIR))

from p10_contract import read_truth
from store_contract import (
    FORMAL_DATASET_SHA256,
    FORMAL_EDGE_COUNT,
    FORMAL_QUERY_COUNT,
    FORMAL_TRUTH_SHA256,
    FORMAL_VERTEX_COUNT,
    IMPORT_CORRECTNESS_SCHEMA,
    IMPORT_EXECUTION_SCHEMA,
    IMPORT_OBSERVATION_COLUMNS,
    IMPORT_RECEIPT_SCHEMA,
    IMPORTER_REPORT_SCHEMA,
    SYSTEM_VERSION,
    ContractError,
    assert_nonoverlapping,
    assert_offline,
    canonical_json_sha,
    current_git_state,
    exact_keys,
    exact_sha,
    file_ref,
    is_within,
    read_strict_json,
    require,
    stable_sha256_file,
    tree_identity,
    validate_file_ref,
    validate_images,
    write_json_exclusive,
)


OBSERVATION_COLUMNS = IMPORT_OBSERVATION_COLUMNS


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--docker", type=Path, default=Path("/usr/bin/docker"))
    parser.add_argument("--store-root", required=True, type=Path)
    parser.add_argument("--importer", required=True, type=Path)
    parser.add_argument("--importer-sha256", required=True)
    parser.add_argument("--argv-json", required=True, type=Path)
    parser.add_argument("--raw-dataset-manifest", required=True, type=Path)
    parser.add_argument("--dense-dataset", required=True, type=Path)
    parser.add_argument("--truth", required=True, type=Path)
    parser.add_argument("--runtime-manifest", required=True, type=Path)
    parser.add_argument("--runtime-manifest-sha256", required=True)
    parser.add_argument("--stdout", required=True, type=Path)
    parser.add_argument("--stderr", required=True, type=Path)
    parser.add_argument("--importer-report", required=True, type=Path)
    parser.add_argument("--correctness-observations", required=True, type=Path)
    parser.add_argument("--execution-result", required=True, type=Path)
    parser.add_argument("--correctness-result", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=86400)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def read_argv(path: Path) -> list[str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read importer argv JSON: {exc}") from exc
    require(
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and "\x00" not in item for item in value),
        "importer argv JSON must be a non-empty NUL-free string array",
    )
    return value


def exact_flag_path(argv: list[str], flag: str, expected: Path) -> None:
    positions = [index for index, value in enumerate(argv) if value == flag]
    require(len(positions) == 1 and positions[0] + 1 < len(argv), f"importer argv requires one {flag}")
    raw = argv[positions[0] + 1]
    require(not raw.startswith("--"), f"importer argv {flag} lacks a value")
    require(Path(raw).is_absolute(), f"importer argv {flag} must be absolute")
    require(Path(raw).resolve(strict=False) == expected.resolve(strict=False),
            f"importer argv {flag} differs from the wrapper-bound path")


def validate_canonical_argv(
    argv: list[str],
    *,
    importer: Path,
    raw_manifest: Path,
    dataset: Path,
    truth: Path,
    runtime: Path,
    store: Path,
    report: Path,
    observations: Path,
) -> None:
    require(Path(argv[0]).is_absolute() and Path(argv[0]).resolve() == importer,
            "importer argv[0] differs from pinned importer")
    expected = {
        "--raw-dataset-manifest": raw_manifest,
        "--dense-dataset": dataset,
        "--truth": truth,
        "--runtime-manifest": runtime,
        "--store-root": store,
        "--importer-report": report,
        "--correctness-observations": observations,
    }
    for flag, path in expected.items():
        exact_flag_path(argv, flag, path)
    bound_flags = {value for value in argv if value.startswith("--")}
    require(set(expected).issubset(bound_flags), "importer argv omits a canonical binding")
    require(not any(value.startswith(tuple(f"{flag}=" for flag in expected)) for value in argv),
            "importer argv must use separate canonical flag/value tokens")


def read_importer_report(
    path: Path,
    *,
    store: Path,
    raw_reference: dict[str, str],
    dataset_reference: dict[str, str],
    truth_reference: dict[str, str],
    runtime_reference: dict[str, str],
) -> tuple[dict[str, Any], dict[str, int]]:
    value = exact_keys(
        read_strict_json(path, "importer report"),
        (
            "schema_version", "store_root", "raw_dataset_manifest", "dense_dataset",
            "truth", "runtime_manifest", "loaded",
        ),
        "importer report",
    )
    require(value["schema_version"] == IMPORTER_REPORT_SCHEMA, "importer report schema drift")
    require(Path(value["store_root"]).resolve() == store, "importer report target store drift")
    for name, expected in (
        ("raw_dataset_manifest", raw_reference),
        ("dense_dataset", dataset_reference),
        ("truth", truth_reference),
        ("runtime_manifest", runtime_reference),
    ):
        observed = exact_keys(value[name], ("path", "sha256"), f"importer report {name}")
        require(
            Path(observed["path"]).resolve() == Path(expected["path"]).resolve()
            and observed["sha256"] == expected["sha256"],
            f"importer report {name} binding drift",
        )
    loaded = exact_keys(
        value["loaded"], ("vertex_count", "edge_count", "edge_type_count"), "importer report loaded"
    )
    expected_counts = {
        "vertex_count": FORMAL_VERTEX_COUNT,
        "edge_count": FORMAL_EDGE_COUNT,
        "edge_type_count": 34,
    }
    require(loaded == expected_counts, "importer report loaded-count closure drift")
    return value, loaded


def validate_correctness_observations(path: Path, truth_path: Path) -> dict[str, int]:
    truth_rows = read_truth(truth_path, FORMAL_QUERY_COUNT)
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            require(tuple(reader.fieldnames or ()) == OBSERVATION_COLUMNS,
                    "correctness observation header drift")
            observations = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ContractError(f"cannot read correctness observations: {exc}") from exc
    require(len(observations) == len(truth_rows), "correctness observation count drift")
    mismatches = 0
    timeouts = 0
    for index, (expected, observed) in enumerate(zip(truth_rows, observations)):
        require(set(observed) == set(OBSERVATION_COLUMNS),
                f"correctness observation {index}: column drift")
        expected_strings = {
            "query_index": str(expected["query_index"]),
            "edge_type": str(expected["edge_type"]),
            "src": str(expected["src"]),
            "expected_count": str(expected["count"]),
            "expected_sum_hash": str(expected["sum_hash"]),
            "expected_xor_hash": str(expected["xor_hash"]),
        }
        require(all(observed[key] == value for key, value in expected_strings.items()),
                f"correctness observation {index}: truth/order drift")
        status = observed["status"]
        require(status in ("ok", "timeout"), f"correctness observation {index}: invalid status")
        if status == "timeout":
            timeouts += 1
            continue
        actual = (
            observed["actual_count"], observed["actual_sum_hash"], observed["actual_xor_hash"]
        )
        expected_actual = (
            expected_strings["expected_count"],
            expected_strings["expected_sum_hash"],
            expected_strings["expected_xor_hash"],
        )
        mismatches += actual != expected_actual
    return {
        "query_count": len(observations),
        "mismatch_count": mismatches,
        "timeout_count": timeouts,
    }


def _prepare_output(path: Path, context: str) -> Path:
    require(path.is_absolute(), f"{context} must be absolute")
    resolved = path.resolve(strict=False)
    require(resolved.parent.is_dir() and not resolved.parent.is_symlink(),
            f"{context} parent must be a real directory")
    require(not resolved.exists() and not resolved.is_symlink(),
            f"refusing to overwrite {context}: {resolved}")
    return resolved


def run(args: argparse.Namespace) -> None:
    for name in (
        "repo_root", "store_root", "importer", "argv_json", "raw_dataset_manifest",
        "dense_dataset", "truth", "runtime_manifest", "stdout", "stderr",
        "importer_report", "correctness_observations", "execution_result",
        "correctness_result", "output",
    ):
        require(getattr(args, name).is_absolute(), f"--{name.replace('_', '-')} must be absolute")
    require(1 <= args.timeout_seconds <= 604800, "--timeout-seconds is out of bounds")
    store = args.store_root.resolve(strict=False)
    require(store.parent.is_dir() and not store.parent.is_symlink(),
            "import store parent must already be a real directory")
    require(not store.exists() and not store.is_symlink(),
            "strict import target must be absent before wrapper execution")
    outputs = {
        "stdout": _prepare_output(args.stdout, "import stdout"),
        "stderr": _prepare_output(args.stderr, "import stderr"),
        "report": _prepare_output(args.importer_report, "importer report"),
        "observations": _prepare_output(args.correctness_observations, "correctness observations"),
        "execution": _prepare_output(args.execution_result, "import execution result"),
        "correctness": _prepare_output(args.correctness_result, "import correctness result"),
        "receipt": _prepare_output(args.output, "import receipt"),
    }
    assert_nonoverlapping({"imported_store": store, **outputs})

    repo = current_git_state(args.repo_root)
    require(repo["clean"] is True, "strict import wrapper requires a clean Git worktree")
    require(not is_within(store, Path(repo["root"])),
            "strict import target store must be outside the Git worktree")
    require(all(not is_within(path, Path(repo["root"])) for path in outputs.values()),
            "strict import outputs must be outside the Git worktree")
    importer_reference = {
        "path": str(args.importer.resolve()),
        "sha256": exact_sha(args.importer_sha256, "importer SHA-256"),
    }
    importer = validate_file_ref(importer_reference, "importer", recompute=True)
    require(os.access(importer, os.X_OK), "pinned importer is not executable")
    require(importer == args.importer.resolve() and is_within(importer, Path(repo["root"])),
            "pinned importer must be inside the current repository")
    argv_source = file_ref(args.argv_json, "importer argv JSON")
    argv = read_argv(args.argv_json)

    raw_reference = file_ref(args.raw_dataset_manifest, "raw dataset manifest")
    raw_value = read_strict_json(Path(raw_reference["path"]), "raw dataset manifest")
    require(raw_value.get("schema_version") == "p02b-dataset-manifest-v1",
            "raw dataset manifest schema drift")
    dataset_reference = file_ref(args.dense_dataset, "canonical dense dataset")
    truth_reference = file_ref(args.truth, "canonical truth")
    require(dataset_reference["sha256"] == FORMAL_DATASET_SHA256,
            "dense dataset is not the canonical SF10 artifact")
    require(truth_reference["sha256"] == FORMAL_TRUTH_SHA256,
            "truth is not the canonical SF10 artifact")
    runtime_reference = {
        "path": str(args.runtime_manifest.resolve()),
        "sha256": exact_sha(args.runtime_manifest_sha256, "runtime manifest SHA-256"),
    }
    runtime_path = validate_file_ref(runtime_reference, "runtime manifest", recompute=True)
    runtime = exact_keys(
        read_strict_json(runtime_path, "runtime manifest"),
        ("schema_version", "system_version", "client", "docker", "images"),
        "runtime manifest",
    )
    require(runtime["schema_version"] == "cidr-p10-nebulagraph-runtime-v1",
            "runtime manifest schema drift")
    require(runtime["system_version"] == SYSTEM_VERSION, "runtime system version drift")
    images = validate_images(runtime["images"], "runtime.images")
    validate_canonical_argv(
        argv,
        importer=importer,
        raw_manifest=Path(raw_reference["path"]),
        dataset=Path(dataset_reference["path"]),
        truth=Path(truth_reference["path"]),
        runtime=runtime_path,
        store=store,
        report=outputs["report"],
        observations=outputs["observations"],
    )

    wrapper_reference = file_ref(Path(__file__), "strict import wrapper")
    started_at = utc_now()
    timed_out = False
    exit_code: int | None = None
    with outputs["stdout"].open("xb") as stdout_handle, outputs["stderr"].open("xb") as stderr_handle:
        try:
            completed = subprocess.run(
                argv,
                cwd=repo["root"],
                stdout=stdout_handle,
                stderr=stderr_handle,
                check=False,
                timeout=args.timeout_seconds,
            )
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
    completed_at = utc_now()
    importer_post_sha, _ = stable_sha256_file(importer, "importer post-execution")
    require(importer_post_sha == importer_reference["sha256"], "importer changed during execution")

    execution_common: dict[str, Any] = {
        "schema_version": IMPORT_EXECUTION_SCHEMA,
        "state": "PASS" if exit_code == 0 and not timed_out else "FAIL",
        "producer": wrapper_reference,
        "started_at_utc": started_at,
        "completed_at_utc": completed_at,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "cwd": repo["root"],
        "importer": importer_reference,
        "argv": argv,
        "argv_sha256": canonical_json_sha(argv),
        "argv_source": argv_source,
        "target_store": str(store),
        "inputs": {
            "raw_dataset_manifest": raw_reference,
            "dense_dataset": dataset_reference,
            "truth": truth_reference,
            "runtime_manifest": runtime_reference,
        },
        "stdout": file_ref(outputs["stdout"], "import stdout"),
        "stderr": file_ref(outputs["stderr"], "import stderr"),
        "report": None,
        "loaded": None,
        "images": images,
    }
    if exit_code != 0 or timed_out:
        write_json_exclusive(outputs["execution"], execution_common)
        raise ContractError(
            "pinned importer timed out" if timed_out else f"pinned importer exited {exit_code}"
        )
    try:
        require(store.is_dir() and not store.is_symlink(),
                "importer exited zero without creating the exact target store")
        require(outputs["report"].is_file() and not outputs["report"].is_symlink(),
                "importer exited zero without its wrapper-bound report")
        require(outputs["observations"].is_file() and not outputs["observations"].is_symlink(),
                "importer exited zero without wrapper-bound correctness observations")
        _report, loaded = read_importer_report(
            outputs["report"],
            store=store,
            raw_reference=raw_reference,
            dataset_reference=dataset_reference,
            truth_reference=truth_reference,
            runtime_reference=runtime_reference,
        )
        correctness_counts = validate_correctness_observations(
            outputs["observations"], Path(truth_reference["path"])
        )
        execution_common["report"] = file_ref(outputs["report"], "importer report")
        execution_common["loaded"] = loaded
    except (ContractError, OSError, ValueError):
        execution_common["state"] = "FAIL"
        write_json_exclusive(outputs["execution"], execution_common)
        raise
    write_json_exclusive(outputs["execution"], execution_common)
    correctness_value = {
        "schema_version": IMPORT_CORRECTNESS_SCHEMA,
        "state": "PASS" if correctness_counts["mismatch_count"] == 0 and correctness_counts["timeout_count"] == 0 else "FAIL",
        "producer": wrapper_reference,
        "importer": importer_reference,
        "argv_sha256": canonical_json_sha(argv),
        "target_store": str(store),
        **correctness_counts,
        "truth": truth_reference,
        "dataset": dataset_reference,
        "observations": file_ref(outputs["observations"], "correctness observations"),
    }
    write_json_exclusive(outputs["correctness"], correctness_value)
    require(correctness_value["state"] == "PASS",
            "import correctness observations contain mismatch or timeout")

    docker = args.docker.resolve()
    require(docker.is_file() and not docker.is_symlink() and os.access(docker, os.X_OK),
            "pinned Docker client is missing, symlinked, or non-executable")
    assert_offline((store,), docker=docker)
    imported_tree = tree_identity(store)
    assert_offline((store,), docker=docker)
    importer_post_sha, _ = stable_sha256_file(importer, "importer post-freeze")
    require(importer_post_sha == importer_reference["sha256"], "importer changed during freeze")
    require(current_git_state(args.repo_root) == repo,
            "Git HEAD/status changed during strict import execution")

    receipt = {
        "schema_version": IMPORT_RECEIPT_SCHEMA,
        "state": "PASS",
        "formal_eligible": True,
        "historical_tag_only": False,
        "system_version": SYSTEM_VERSION,
        "completed_at_utc": completed_at,
        "repo": repo,
        "wrapper": {**wrapper_reference, "policy": "execute-and-derive-evidence-v1"},
        "importer": {
            **importer_reference,
            "cwd": repo["root"],
            "argv": argv,
            "argv_sha256": canonical_json_sha(argv),
            "argv_source": argv_source,
        },
        "inputs": {
            "raw_dataset_manifest": raw_reference,
            "dense_dataset": dataset_reference,
            "truth": truth_reference,
            "runtime_manifest": runtime_reference,
        },
        "images": images,
        "execution": {
            "started_at_utc": started_at,
            "completed_at_utc": completed_at,
            "exit_code": 0,
            "timed_out": False,
            "stdout": execution_common["stdout"],
            "stderr": execution_common["stderr"],
            "report": execution_common["report"],
            "result": file_ref(outputs["execution"], "import execution result"),
        },
        "loaded": loaded,
        "correctness": {
            **correctness_counts,
            "observations": correctness_value["observations"],
            "result": file_ref(outputs["correctness"], "import correctness result"),
        },
        "store": {"path": str(store), **imported_tree},
    }
    write_json_exclusive(outputs["receipt"], receipt)
    print(outputs["receipt"])


def main() -> int:
    try:
        run(parse_args())
        return 0
    except (ContractError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"build_import_receipt: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
