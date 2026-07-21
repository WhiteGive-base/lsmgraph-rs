#!/usr/bin/env python3
"""Fail-closed TuGraph adapter for the CIDR P10 typed-neighbor contract.

The adapter validates immutable request/store/runtime lineage, consumes the
formal P02B release gate, and invokes one native embedded TuGraph worker.  That
worker owns Galaxy/GraphDB for both warmup and measured phases, so no TuGraph
server or pre-existing service is started, stopped, or queried.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence


P10_DIR = Path(__file__).resolve().parents[2]
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
    phase_digest,
    read_truth,
    sha256_file,
)


WORKER_SCHEMA = "cidr-p10-tugraph-worker-v1"
STORE_SCHEMA = "cidr-p10-tugraph-store-v1"
RUNTIME_SCHEMA = "cidr-p10-tugraph-runtime-v1"
PROVENANCE_SCHEMA = "cidr-p10-tugraph-provenance-v1"
EXECUTION_MODEL = "native-embedded-single-worker-process-v1"
VERTEX_ID_CONTRACT = "internal-vid-equals-dense-id-v1"
FORMAL_QUERY_COUNT = 1700
FORMAL_TRUTH_SHA256 = "876ec4be45bb7c220ce595b8d8efd5c79d13db285569cc0d6ae3e8bc19a1c788"
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
LABEL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_strict_json(path: Path, context: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    except ContractError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"{context}: cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{context}: top-level JSON must be an object")
    return value


def exact_keys(value: object, keys: Iterable[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{context}: expected an object")
    expected = set(keys)
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing or unknown:
        raise ContractError(f"{context}: key drift missing={missing}, unknown={unknown}")
    return value


def exact_sha(value: object, context: str) -> str:
    if not isinstance(value, str) or not HEX64_RE.fullmatch(value):
        raise ContractError(f"{context}: expected lowercase SHA-256")
    return value


def integer(value: object, context: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{context}: expected an integer")
    if minimum is not None and value < minimum:
        raise ContractError(f"{context}: expected >= {minimum}")
    return value


def nonempty(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{context}: expected a non-empty string")
    return value


def canonical_path(value: object, context: str, *, file: bool = False, directory: bool = False) -> Path:
    raw = nonempty(value, context)
    if not Path(raw).is_absolute():
        raise ContractError(f"{context}: path must be absolute")
    path = Path(raw).resolve()
    if str(path) != raw:
        raise ContractError(f"{context}: path must be canonical: {raw}")
    if file and not path.is_file():
        raise ContractError(f"{context}: file does not exist: {path}")
    if directory and not path.is_dir():
        raise ContractError(f"{context}: directory does not exist: {path}")
    return path


def file_ref(value: object, context: str, *, executable: bool = False) -> tuple[Path, str]:
    item = exact_keys(value, ("path", "sha256"), context)
    path = canonical_path(item["path"], f"{context}.path", file=True)
    if executable and not os.access(path, os.X_OK):
        raise ContractError(f"{context}: file is not executable: {path}")
    expected = exact_sha(item["sha256"], f"{context}.sha256")
    actual = sha256_file(path)
    if actual != expected:
        raise ContractError(f"{context}: SHA-256 mismatch for {path}")
    return path, actual


def hashed_cli_file(path: Path | None, expected_sha: str | None, context: str, *, executable: bool = False) -> Path:
    if path is None or expected_sha is None:
        raise ContractError(f"{context}: path and SHA-256 are required")
    resolved = path.resolve()
    if not resolved.is_file():
        raise ContractError(f"{context}: file does not exist: {resolved}")
    if executable and not os.access(resolved, os.X_OK):
        raise ContractError(f"{context}: file is not executable: {resolved}")
    if sha256_file(resolved) != exact_sha(expected_sha, f"{context}.sha256"):
        raise ContractError(f"{context}: SHA-256 mismatch")
    return resolved


def validate_request(path: Path, store_label: str, expected_truth_sha: str) -> tuple[dict[str, Any], list[dict[str, int]], Path, Path, Path, str]:
    request = exact_keys(
        read_strict_json(path, "TuGraph adapter request"),
        (
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
        "TuGraph adapter request",
    )
    exact_values = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "system_id": "tugraph",
        "group": "embedded",
        "interface_scope": INTERFACE_SCOPE,
    }
    for key, expected in exact_values.items():
        if request[key] != expected:
            raise ContractError(f"request.{key}: {request[key]!r} != {expected!r}")
    if request["execution_mode"] not in ("fixture", "formal"):
        raise ContractError("request.execution_mode must be fixture or formal")
    if request["execution_mode"] == "formal" and expected_truth_sha != FORMAL_TRUTH_SHA256:
        raise ContractError("formal TuGraph P10 requires the frozen P01 SF10 truth SHA-256")
    nonempty(request["suite_id"], "request.suite_id")
    nonempty(request["run_id"], "request.run_id")
    nonempty(request["system_version"], "request.system_version")
    integer(request["repeat_index"], "request.repeat_index", 1)

    binary, _ = file_ref(request["binary"], "request.binary", executable=True)
    dataset_ref = exact_keys(request["dataset"], ("path", "sha256"), "request.dataset")
    dataset = canonical_path(dataset_ref["path"], "request.dataset.path")
    if not dataset.exists():
        raise ContractError(f"request.dataset.path does not exist: {dataset}")
    dataset_sha = exact_sha(dataset_ref["sha256"], "request.dataset.sha256")
    if dataset.is_file() and sha256_file(dataset) != dataset_sha:
        raise ContractError("request.dataset SHA-256 mismatch")

    truth_ref = exact_keys(
        request["truth"],
        ("path", "sha256", "query_count", "digest_algorithm"),
        "request.truth",
    )
    truth = canonical_path(truth_ref["path"], "request.truth.path", file=True)
    truth_sha = exact_sha(truth_ref["sha256"], "request.truth.sha256")
    if truth_sha != expected_truth_sha or sha256_file(truth) != truth_sha:
        raise ContractError("request truth does not match --expected-truth-sha256")
    if truth_ref["digest_algorithm"] != TRUTH_DIGEST_ALGORITHM:
        raise ContractError("request.truth.digest_algorithm drift")
    if integer(truth_ref["query_count"], "request.truth.query_count", 1) != FORMAL_QUERY_COUNT:
        raise ContractError("TuGraph P10 freezes exactly 1700 ordered truth rows")
    truth_rows = read_truth(truth, FORMAL_QUERY_COUNT)

    roots = request["store_roots"]
    if not isinstance(roots, list) or not roots:
        raise ContractError("request.store_roots must be a non-empty array")
    selected_path: Path | None = None
    selected_sha = ""
    labels: set[str] = set()
    for index, raw in enumerate(roots):
        context = f"request.store_roots[{index}]"
        item = exact_keys(raw, ("label", "path", "sha256"), context)
        label = nonempty(item["label"], f"{context}.label")
        if label in labels:
            raise ContractError(f"{context}: duplicate label")
        labels.add(label)
        root = canonical_path(item["path"], f"{context}.path", directory=True)
        lineage_sha = exact_sha(item["sha256"], f"{context}.sha256")
        if label == store_label:
            selected_path = root
            selected_sha = lineage_sha
    if selected_path is None:
        raise ContractError(f"request.store_roots has no {store_label!r} root")

    timing = exact_keys(
        request["timing"],
        (
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
        "request.timing",
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
            raise ContractError(f"request.timing.{key}: protocol drift")
    integer(timing["warmup_passes"], "request.timing.warmup_passes", 1)
    integer(timing["measured_passes"], "request.timing.measured_passes", 1)
    integer(timing["per_query_timeout_ms"], "request.timing.per_query_timeout_ms", 1)
    return request, truth_rows, binary, dataset, selected_path, selected_sha


def validate_store_manifest(
    path: Path,
    expected_sha: str,
    request: dict[str, Any],
    truth_rows: list[dict[str, int]],
    dataset: Path,
    store: Path,
    store_lineage_sha: str,
) -> tuple[dict[str, Any], list[tuple[int, str]]]:
    if sha256_file(path) != expected_sha:
        raise ContractError("TuGraph store manifest SHA-256 mismatch")
    value = exact_keys(
        read_strict_json(path, "TuGraph store manifest"),
        (
            "schema_version",
            "system_version",
            "interface_scope",
            "formal_eligible",
            "dataset",
            "truth",
            "database",
            "authentication",
            "vertex_id_contract",
            "edge_type_labels",
            "store_lineage",
            "import_provenance",
        ),
        "TuGraph store manifest",
    )
    if value["schema_version"] != STORE_SCHEMA or value["interface_scope"] != INTERFACE_SCOPE:
        raise ContractError("TuGraph store manifest schema/interface drift")
    if value["system_version"] != request["system_version"]:
        raise ContractError("TuGraph store/system version mismatch")
    if not isinstance(value["formal_eligible"], bool):
        raise ContractError("store manifest formal_eligible must be boolean")
    if request["execution_mode"] == "formal" and value["formal_eligible"] is not True:
        raise ContractError("formal TuGraph run requires a formal-eligible store")

    dataset_ref = exact_keys(value["dataset"], ("path", "sha256"), "store.dataset")
    if canonical_path(dataset_ref["path"], "store.dataset.path") != dataset:
        raise ContractError("store manifest references a different dataset")
    if exact_sha(dataset_ref["sha256"], "store.dataset.sha256") != request["dataset"]["sha256"]:
        raise ContractError("store/request dataset lineage differs")
    truth_ref = exact_keys(value["truth"], ("sha256", "query_count"), "store.truth")
    if exact_sha(truth_ref["sha256"], "store.truth.sha256") != request["truth"]["sha256"]:
        raise ContractError("store/request truth SHA-256 differs")
    if integer(truth_ref["query_count"], "store.truth.query_count", 1) != FORMAL_QUERY_COUNT:
        raise ContractError("store manifest does not freeze 1700 queries")

    database = exact_keys(
        value["database"],
        ("path", "graph", "read_only", "durable", "create_if_not_exist"),
        "store.database",
    )
    if canonical_path(database["path"], "store.database.path", directory=True) != store:
        raise ContractError("store manifest database path differs from request root")
    if not nonempty(database["graph"], "store.database.graph"):
        raise ContractError("store graph name is empty")
    if (database["read_only"], database["durable"], database["create_if_not_exist"]) != (
        True,
        False,
        False,
    ):
        raise ContractError("TuGraph embedded open mode must be read-only/non-durable/no-create")
    authentication = exact_keys(
        value["authentication"],
        ("user", "password_env", "password_sha256"),
        "store.authentication",
    )
    if not LABEL_RE.fullmatch(nonempty(authentication["user"], "store.authentication.user")):
        raise ContractError("TuGraph authentication user is malformed")
    if authentication["password_env"] != "CIDR_TUGRAPH_PASSWORD":
        raise ContractError("TuGraph authentication environment contract drift")
    exact_sha(authentication["password_sha256"], "store.authentication.password_sha256")

    vertex = exact_keys(
        value["vertex_id_contract"],
        ("kind", "contiguous_from_zero", "vertex_count"),
        "store.vertex_id_contract",
    )
    if vertex["kind"] != VERTEX_ID_CONTRACT or vertex["contiguous_from_zero"] is not True:
        raise ContractError("store does not guarantee internal VID == dense ID")
    integer(vertex["vertex_count"], "store.vertex_id_contract.vertex_count", 1)

    lineage = exact_keys(
        value["store_lineage"],
        ("hash_method", "sha256", "file_count", "total_bytes"),
        "store.store_lineage",
    )
    if nonempty(lineage["hash_method"], "store.store_lineage.hash_method") != "sha256-tree-v1(relative-path,size,file-sha256)":
        raise ContractError("store lineage hash method drift")
    if exact_sha(lineage["sha256"], "store.store_lineage.sha256") != store_lineage_sha:
        raise ContractError("store lineage differs from P10 request")
    integer(lineage["file_count"], "store.store_lineage.file_count", 1)
    integer(lineage["total_bytes"], "store.store_lineage.total_bytes", 1)

    provenance = exact_keys(
        value["import_provenance"],
        ("importer", "config", "source_revision"),
        "store.import_provenance",
    )
    file_ref(provenance["importer"], "store.import_provenance.importer", executable=True)
    file_ref(provenance["config"], "store.import_provenance.config")
    nonempty(provenance["source_revision"], "store.import_provenance.source_revision")

    raw_labels = value["edge_type_labels"]
    if not isinstance(raw_labels, list) or not raw_labels:
        raise ContractError("store.edge_type_labels must be a non-empty array")
    labels: list[tuple[int, str]] = []
    seen_types: set[int] = set()
    seen_names: set[str] = set()
    for index, raw in enumerate(raw_labels):
        item = exact_keys(raw, ("edge_type", "label"), f"store.edge_type_labels[{index}]")
        edge_type = integer(item["edge_type"], f"store.edge_type_labels[{index}].edge_type")
        label = nonempty(item["label"], f"store.edge_type_labels[{index}].label")
        if not LABEL_RE.fullmatch(label) or edge_type in seen_types or label in seen_names:
            raise ContractError("store edge label map contains an invalid or duplicate entry")
        seen_types.add(edge_type)
        seen_names.add(label)
        labels.append((edge_type, label))
    truth_types = {row["edge_type"] for row in truth_rows}
    if seen_types != truth_types:
        raise ContractError("store edge label map must exactly cover truth edge types")
    if [item[0] for item in labels] != sorted(seen_types):
        raise ContractError("store edge label map must be sorted by edge_type")
    return value, labels


def validate_runtime_manifest(
    path: Path,
    expected_sha: str,
    request: dict[str, Any],
    request_binary: Path,
) -> tuple[dict[str, Any], Path]:
    if sha256_file(path) != expected_sha:
        raise ContractError("TuGraph runtime manifest SHA-256 mismatch")
    value = exact_keys(
        read_strict_json(path, "TuGraph runtime manifest"),
        (
            "schema_version",
            "system_version",
            "execution_model",
            "worker",
            "liblgraph",
            "header_tree",
            "compat_header_tree",
            "boost_header_tree",
            "date_header_tree",
            "compiler",
            "build_image",
            "docker_client",
            "source",
            "build_flags",
            "runtime_image_digests",
            "runtime_container_names",
        ),
        "TuGraph runtime manifest",
    )
    if value["schema_version"] != RUNTIME_SCHEMA or value["execution_model"] != EXECUTION_MODEL:
        raise ContractError("TuGraph runtime schema/execution model drift")
    if value["system_version"] != request["system_version"]:
        raise ContractError("TuGraph runtime/system version mismatch")
    worker, _ = file_ref(value["worker"], "runtime.worker", executable=True)
    if worker != request_binary:
        raise ContractError("runtime worker differs from P10 request.binary")
    liblgraph, _ = file_ref(value["liblgraph"], "runtime.liblgraph")
    headers = exact_keys(
        value["header_tree"],
        ("path", "sha256", "hash_method", "file_count", "total_bytes"),
        "runtime.header_tree",
    )
    canonical_path(headers["path"], "runtime.header_tree.path", directory=True)
    exact_sha(headers["sha256"], "runtime.header_tree.sha256")
    nonempty(headers["hash_method"], "runtime.header_tree.hash_method")
    integer(headers["file_count"], "runtime.header_tree.file_count", 1)
    integer(headers["total_bytes"], "runtime.header_tree.total_bytes", 1)
    compat_headers = exact_keys(
        value["compat_header_tree"],
        ("path", "sha256", "hash_method", "file_count", "total_bytes", "scope"),
        "runtime.compat_header_tree",
    )
    canonical_path(
        compat_headers["path"], "runtime.compat_header_tree.path", directory=True
    )
    exact_sha(compat_headers["sha256"], "runtime.compat_header_tree.sha256")
    nonempty(compat_headers["hash_method"], "runtime.compat_header_tree.hash_method")
    integer(compat_headers["file_count"], "runtime.compat_header_tree.file_count", 1)
    integer(compat_headers["total_bytes"], "runtime.compat_header_tree.total_bytes", 1)
    if compat_headers["scope"] != "unused-spatial-wkb-includes-only-no-wkb-api-v1":
        raise ContractError("runtime compat-header scope drift")
    for name in ("boost_header_tree", "date_header_tree"):
        dependency = exact_keys(
            value[name],
            ("path", "sha256", "hash_method", "file_count", "total_bytes"),
            f"runtime.{name}",
        )
        canonical_path(dependency["path"], f"runtime.{name}.path", directory=True)
        exact_sha(dependency["sha256"], f"runtime.{name}.sha256")
        nonempty(dependency["hash_method"], f"runtime.{name}.hash_method")
        integer(dependency["file_count"], f"runtime.{name}.file_count", 1)
        integer(dependency["total_bytes"], f"runtime.{name}.total_bytes", 1)
    compiler = exact_keys(value["compiler"], ("path", "sha256", "version"), "runtime.compiler")
    nonempty(compiler["path"], "runtime.compiler.path")
    exact_sha(compiler["sha256"], "runtime.compiler.sha256")
    nonempty(compiler["version"], "runtime.compiler.version")
    build_image = exact_keys(
        value["build_image"],
        ("reference", "repo_digest", "image_id"),
        "runtime.build_image",
    )
    for key in ("reference", "repo_digest"):
        if "@sha256:" not in nonempty(build_image[key], f"runtime.build_image.{key}"):
            raise ContractError("TuGraph build image must be repo-digest pinned")
    if build_image["reference"] != build_image["repo_digest"]:
        raise ContractError("TuGraph build-image reference/repo digest differ")
    image_id = nonempty(build_image["image_id"], "runtime.build_image.image_id")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ContractError("TuGraph build-image ID is malformed")
    docker_client = exact_keys(
        value["docker_client"], ("path", "sha256", "version"), "runtime.docker_client"
    )
    file_ref(
        {"path": docker_client["path"], "sha256": docker_client["sha256"]},
        "runtime.docker_client",
        executable=True,
    )
    nonempty(docker_client["version"], "runtime.docker_client.version")
    file_ref(value["source"], "runtime.source")
    if not isinstance(value["build_flags"], list) or not all(
        isinstance(item, str) and item for item in value["build_flags"]
    ):
        raise ContractError("runtime.build_flags must be a non-empty string array")
    if not isinstance(value["runtime_image_digests"], list) or value["runtime_image_digests"]:
        raise ContractError("canonical TuGraph embedded path is native and must have no image digest")
    if not isinstance(value["runtime_container_names"], list) or value["runtime_container_names"]:
        raise ContractError("canonical TuGraph embedded path must have no container")
    return value, liblgraph


def validate_capabilities(binary: Path, formal: bool) -> dict[str, Any]:
    completed = subprocess.run(
        [str(binary), "--capabilities"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=False,
    )
    if completed.returncode != 0:
        raise ContractError(f"TuGraph capability probe failed: {completed.stderr.strip()}")
    try:
        value = json.loads(completed.stdout, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, ContractError) as exc:
        raise ContractError(f"TuGraph capability response is malformed: {exc}") from exc
    value = exact_keys(
        value,
        (
            "schema_version",
            "contract_version",
            "interface_scope",
            "execution_model",
            "formal_query_count",
            "fixture_only",
        ),
        "TuGraph worker capabilities",
    )
    exact = {
        "schema_version": WORKER_SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "interface_scope": INTERFACE_SCOPE,
        "execution_model": EXECUTION_MODEL,
        "formal_query_count": FORMAL_QUERY_COUNT,
    }
    for key, expected in exact.items():
        if value[key] != expected:
            raise ContractError(f"TuGraph capability {key} drift")
    if not isinstance(value["fixture_only"], bool):
        raise ContractError("TuGraph capability fixture_only must be boolean")
    if formal and value["fixture_only"]:
        raise ContractError("formal mode rejects fixture-only TuGraph worker")
    return value


def consume_p02b(args: argparse.Namespace, formal: bool) -> dict[str, Any] | None:
    supplied = (args.p02b_result, args.p02b_validator, args.p02b_validator_sha256)
    if not formal:
        if any(item is not None for item in supplied):
            raise ContractError("fixture TuGraph run must not claim a P02B formal release")
        return None
    if any(item is None for item in supplied):
        raise ContractError("formal TuGraph run requires P02B result/validator/SHA-256")
    validator = hashed_cli_file(
        args.p02b_validator,
        args.p02b_validator_sha256,
        "P02B validator",
        executable=True,
    )
    result = args.p02b_result.resolve()
    if not result.is_file():
        raise ContractError(f"P02B result does not exist: {result}")
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            str(validator),
            "--result",
            str(result),
            "--consumer",
            "P10",
            "--require-formal",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise ContractError(
            "P02B formal release rejected TuGraph P10: " + completed.stderr.strip()
        )
    return {
        "result": {"path": str(result), "sha256": sha256_file(result)},
        "validator": {"path": str(validator), "sha256": sha256_file(validator)},
        "consumer": "P10",
        "require_formal": True,
    }


def write_edge_map(path: Path, labels: list[tuple[int, str]]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        handle.write("edge_type\tlabel\n")
        for edge_type, label in labels:
            handle.write(f"{edge_type}\t{label}\n")
    os.replace(temporary, path)


def run_worker(
    binary: Path,
    request: dict[str, Any],
    store_manifest: dict[str, Any],
    truth_path: Path,
    edge_map: Path,
    output_dir: Path,
) -> None:
    timing = request["timing"]
    command = [
        str(binary),
        "--db-path",
        store_manifest["database"]["path"],
        "--graph",
        store_manifest["database"]["graph"],
        "--user",
        store_manifest["authentication"]["user"],
        "--truth-tsv",
        str(truth_path),
        "--edge-type-map",
        str(edge_map),
        "--output-dir",
        str(output_dir),
        "--system-version",
        request["system_version"],
        "--warmup-passes",
        str(timing["warmup_passes"]),
        "--measured-passes",
        str(timing["measured_passes"]),
        "--per-query-timeout-ms",
        str(timing["per_query_timeout_ms"]),
        "--expected-query-count",
        str(FORMAL_QUERY_COUNT),
        "--repeat-index",
        str(request["repeat_index"]),
    ]
    (output_dir / "tugraph-worker-command.json").write_text(
        json.dumps(command, indent=2) + "\n", encoding="utf-8"
    )
    with (output_dir / "tugraph-worker.stdout.log").open("wb") as stdout_handle, (
        output_dir / "tugraph-worker.stderr.log"
    ).open("wb") as stderr_handle:
        completed = subprocess.run(
            command,
            stdout=stdout_handle,
            stderr=stderr_handle,
            timeout=600,
            check=False,
        )
    if completed.returncode != 0:
        stderr_text = (output_dir / "tugraph-worker.stderr.log").read_text(
            encoding="utf-8", errors="replace"
        )[-4000:]
        raise ContractError(
            f"TuGraph worker exited {completed.returncode}: {stderr_text.strip()}"
        )


def read_observations(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if reader.fieldnames != OBSERVATION_COLUMNS:
                raise ContractError(f"TuGraph observations header drift: {reader.fieldnames!r}")
            return list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ContractError(f"cannot read TuGraph observations: {exc}") from exc


def validate_worker_outputs(
    output_dir: Path,
    request: dict[str, Any],
    truth_rows: list[dict[str, int]],
    binary: Path,
    liblgraph: Path,
    store: Path,
    user: str,
    expected_labels: list[tuple[int, str]],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    summary = exact_keys(
        read_strict_json(output_dir / "tugraph-worker-summary.json", "TuGraph worker summary"),
        (
            "schema_version",
            "contract_version",
            "interface_scope",
            "engine_api",
            "execution_model",
            "system_version",
            "pid",
            "ppid",
            "process_executable",
            "loaded_liblgraph",
            "database_path",
            "graph",
            "user",
            "read_only",
            "durable",
            "create_if_not_exist",
            "query_count",
            "resolved_edge_labels",
            "warmup",
            "measured",
        ),
        "TuGraph worker summary",
    )
    expected = {
        "schema_version": WORKER_SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "interface_scope": INTERFACE_SCOPE,
        "engine_api": "lgraph_api::Galaxy/OpenGraph/CreateReadTxn/GetOutEdgeIterator",
        "execution_model": EXECUTION_MODEL,
        "system_version": request["system_version"],
        "process_executable": str(binary),
        "loaded_liblgraph": str(liblgraph),
        "database_path": str(store),
        "user": user,
        "read_only": True,
        "durable": False,
        "create_if_not_exist": False,
        "query_count": FORMAL_QUERY_COUNT,
    }
    for key, value in expected.items():
        if summary[key] != value:
            raise ContractError(f"TuGraph worker summary.{key} drift")
    integer(summary["pid"], "worker.pid", 1)
    integer(summary["ppid"], "worker.ppid", 1)

    resolved = summary["resolved_edge_labels"]
    if not isinstance(resolved, list) or len(resolved) != len(expected_labels):
        raise ContractError("TuGraph worker resolved edge-label set changed")
    for index, ((edge_type, label), raw) in enumerate(zip(expected_labels, resolved)):
        item = exact_keys(raw, ("edge_type", "label", "label_id"), f"worker.labels[{index}]")
        if item["edge_type"] != edge_type or item["label"] != label:
            raise ContractError("TuGraph worker resolved a different edge label")
        integer(item["label_id"], f"worker.labels[{index}].label_id", 0)

    observations = read_observations(output_dir / "query-observations.tsv")
    phase_passes = {
        "warmup": request["timing"]["warmup_passes"],
        "measured": request["timing"]["measured_passes"],
    }
    if len(observations) != FORMAL_QUERY_COUNT * sum(phase_passes.values()):
        raise ContractError("TuGraph worker observation row count changed")
    position = 0
    timeout_ns = request["timing"]["per_query_timeout_ms"] * 1_000_000
    for phase in ("warmup", "measured"):
        for pass_index in range(phase_passes[phase]):
            for truth in truth_rows:
                row = observations[position]
                position += 1
                expected_row = {
                    "contract_version": CONTRACT_VERSION,
                    "system_id": "tugraph",
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
                for key, value in expected_row.items():
                    if row.get(key) != value:
                        raise ContractError(f"TuGraph observation {position - 1}.{key} drift")
                try:
                    latency = int(row["latency_ns"])
                except ValueError as exc:
                    raise ContractError("TuGraph observation latency is not an integer") from exc
                if latency <= 0:
                    raise ContractError("TuGraph observation latency must be positive")
                if row["status"] == "timeout":
                    if latency <= timeout_ns or any(
                        row[key] for key in ("actual_count", "actual_sum_hash", "actual_xor_hash")
                    ):
                        raise ContractError("TuGraph timeout row violates deadline/digest contract")
                elif row["status"] == "ok":
                    if latency > timeout_ns:
                        raise ContractError("TuGraph ok row exceeds per-query timeout")
                    for key in ("actual_count", "actual_sum_hash", "actual_xor_hash"):
                        try:
                            int(row[key])
                        except ValueError as exc:
                            raise ContractError(f"TuGraph observation {key} is not an integer") from exc
                else:
                    raise ContractError("TuGraph observation status must be ok or timeout")

    phase_keys = (
        "passes",
        "started_monotonic_ns",
        "ended_monotonic_ns",
        "elapsed_ns",
        "total_query_latency_ns",
        "timeout_queries",
        "mismatch_queries",
    )
    for phase, passes in phase_passes.items():
        item = exact_keys(summary[phase], phase_keys, f"worker.{phase}")
        if item["passes"] != passes:
            raise ContractError(f"worker.{phase}.passes drift")
        start = integer(item["started_monotonic_ns"], f"worker.{phase}.start", 0)
        end = integer(item["ended_monotonic_ns"], f"worker.{phase}.end", start + 1)
        elapsed = integer(item["elapsed_ns"], f"worker.{phase}.elapsed", 1)
        total = integer(item["total_query_latency_ns"], f"worker.{phase}.total", 1)
        if elapsed != end - start or elapsed < total:
            raise ContractError(f"worker.{phase} CLOCK_MONOTONIC boundary is inconsistent")
        rows = [row for row in observations if row["phase"] == phase]
        timeouts = sum(row["status"] == "timeout" for row in rows)
        mismatches = sum(
            row["status"] == "ok"
            and (
                row["actual_count"],
                row["actual_sum_hash"],
                row["actual_xor_hash"],
            )
            != (
                row["expected_count"],
                row["expected_sum_hash"],
                row["expected_xor_hash"],
            )
            for row in rows
        )
        if item["timeout_queries"] != timeouts or item["mismatch_queries"] != mismatches:
            raise ContractError(f"worker.{phase} counters differ from observations")
    if summary["warmup"]["ended_monotonic_ns"] > summary["measured"]["started_monotonic_ns"]:
        raise ContractError("TuGraph warmup/measured phases overlap")
    return observations, summary


def publish_contract_outputs(
    output_dir: Path,
    request: dict[str, Any],
    truth_rows: list[dict[str, int]],
    observations: list[dict[str, str]],
    worker: dict[str, Any],
) -> None:
    phases: dict[str, dict[str, Any]] = {}
    for phase in ("warmup", "measured"):
        passes = request["timing"][f"{phase}_passes"]
        rows = [row for row in observations if row["phase"] == phase]
        timeouts = sum(row["status"] == "timeout" for row in rows)
        completed = len(rows) - timeouts
        mismatches = sum(
            row["status"] == "ok"
            and (
                row["actual_count"],
                row["actual_sum_hash"],
                row["actual_xor_hash"],
            )
            != (
                row["expected_count"],
                row["expected_sum_hash"],
                row["expected_xor_hash"],
            )
            for row in rows
        )
        native = worker[phase]
        phases[phase] = {
            "passes": passes,
            "requested_queries": len(rows),
            "completed_queries": completed,
            "timeout_queries": timeouts,
            "mismatch_queries": mismatches,
            "started_monotonic_ns": native["started_monotonic_ns"],
            "ended_monotonic_ns": native["ended_monotonic_ns"],
            "elapsed_ns": native["elapsed_ns"],
            "expected_digest_sha256": phase_digest(phase, passes, truth_rows),
            "actual_digest_sha256": phase_digest(phase, passes, truth_rows, rows),
        }

    result = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "system_id": "tugraph",
        "group": "embedded",
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
    events = []
    for phase in ("warmup", "measured"):
        events.extend(
            (
                {
                    "contract_version": CONTRACT_VERSION,
                    "phase": phase,
                    "event": "start",
                    "monotonic_ns": phases[phase]["started_monotonic_ns"],
                },
                {
                    "contract_version": CONTRACT_VERSION,
                    "phase": phase,
                    "event": "end",
                    "monotonic_ns": phases[phase]["ended_monotonic_ns"],
                },
            )
        )
    temporary_events = output_dir / "phase-events.jsonl.tmp"
    temporary_events.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )
    os.replace(temporary_events, output_dir / "phase-events.jsonl")
    atomic_json(output_dir / "adapter-result.json", result)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store-label", default="tugraph")
    parser.add_argument("--store-manifest", required=True, type=Path)
    parser.add_argument("--store-manifest-sha256", required=True)
    parser.add_argument("--runtime-manifest", required=True, type=Path)
    parser.add_argument("--runtime-manifest-sha256", required=True)
    parser.add_argument("--expected-truth-sha256", required=True)
    parser.add_argument("--p02b-result", type=Path)
    parser.add_argument("--p02b-validator", type=Path)
    parser.add_argument("--p02b-validator-sha256")
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        expected_truth_sha = exact_sha(args.expected_truth_sha256, "--expected-truth-sha256")
        store_manifest_path = hashed_cli_file(
            args.store_manifest,
            args.store_manifest_sha256,
            "TuGraph store manifest",
        )
        runtime_manifest_path = hashed_cli_file(
            args.runtime_manifest,
            args.runtime_manifest_sha256,
            "TuGraph runtime manifest",
        )
        request, truth_rows, binary, dataset, store, store_lineage_sha = validate_request(
            args.request.resolve(), args.store_label, expected_truth_sha
        )
        formal = request["execution_mode"] == "formal"
        store_manifest, labels = validate_store_manifest(
            store_manifest_path,
            args.store_manifest_sha256,
            request,
            truth_rows,
            dataset,
            store,
            store_lineage_sha,
        )
        authentication = store_manifest["authentication"]
        password = os.environ.get(authentication["password_env"])
        if password is None or not password:
            raise ContractError(
                f"{authentication['password_env']} is required for TuGraph embedded login"
            )
        if hashlib.sha256(password.encode("utf-8")).hexdigest() != authentication[
            "password_sha256"
        ]:
            raise ContractError("TuGraph password environment value does not match store manifest")
        runtime_manifest, liblgraph = validate_runtime_manifest(
            runtime_manifest_path,
            args.runtime_manifest_sha256,
            request,
            binary,
        )
        capabilities = validate_capabilities(binary, formal)
        p02b = consume_p02b(args, formal)

        output_dir = args.output_dir.resolve()
        if not output_dir.is_dir():
            raise ContractError("--output-dir must already exist")
        if any(output_dir.iterdir()):
            raise ContractError("--output-dir must be empty")
        edge_map = output_dir / "tugraph-edge-type-map.tsv"
        write_edge_map(edge_map, labels)
        run_worker(
            binary,
            request,
            store_manifest,
            Path(request["truth"]["path"]),
            edge_map,
            output_dir,
        )
        observations, worker = validate_worker_outputs(
            output_dir,
            request,
            truth_rows,
            binary,
            liblgraph,
            store,
            authentication["user"],
            labels,
        )
        publish_contract_outputs(output_dir, request, truth_rows, observations, worker)
        provenance = {
            "schema_version": PROVENANCE_SCHEMA,
            "execution_mode": request["execution_mode"],
            "execution_model": EXECUTION_MODEL,
            "worker_pid": worker["pid"],
            "worker_ppid": worker["ppid"],
            "worker": runtime_manifest["worker"],
            "liblgraph": runtime_manifest["liblgraph"],
            "build_image": runtime_manifest["build_image"],
            "runtime_manifest": {
                "path": str(runtime_manifest_path),
                "sha256": sha256_file(runtime_manifest_path),
            },
            "store_manifest": {
                "path": str(store_manifest_path),
                "sha256": sha256_file(store_manifest_path),
            },
            "store_lineage_sha256": store_lineage_sha,
            "dataset": request["dataset"],
            "truth": request["truth"],
            "image_digests": [],
            "container_names": [],
            "p31_process_coverage": "root-adapter-plus-native-worker-child-v1",
            "p02b_release": p02b,
            "capabilities": capabilities,
            "edge_type_map": {
                "path": str(edge_map),
                "sha256": sha256_file(edge_map),
            },
        }
        atomic_json(output_dir / "tugraph-runtime-provenance.json", provenance)
        return 0
    except (ContractError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
