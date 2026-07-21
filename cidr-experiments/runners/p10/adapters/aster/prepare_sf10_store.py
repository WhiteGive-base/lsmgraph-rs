#!/usr/bin/env python3
"""Prepare, freeze, and correctness-check the canonical Aster SF10 store.

This runner is deliberately correctness-only.  It never admits a performance
point, never starts/stops a service, and never reuses an output directory.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

P10_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(P10_DIR))

from p10_contract import (  # noqa: E402
    CLOCK_NAME,
    CONTRACT_VERSION,
    FIXTURE_PROCESS_LIFETIME,
    INTERFACE_SCOPE,
    OBSERVATION_COLUMNS,
    REQUEST_SCHEMA_VERSION,
    SEQUENCE_DIGEST_ALGORITHM,
    TIMING_BOUNDARY,
    TRUTH_DIGEST_ALGORITHM,
)


SCHEMA_VERSION = "p10-aster-sf10-preparation-v1"
SOURCE_COMMIT = "6abb258e577c479325092a8ac0e7691fdfd154c2"
FINAL_WORKER_SHA256 = "12f848aa5aa7595d8626e67066280cc6dd023067b597d123fdc6caa067a6c1b8"
DENSE_SHA256 = "9727fed3d710a3f0897b1ee7379411a091e0568bef05902de8b1787efc39d258"
TRUTH_SHA256 = "876ec4be45bb7c220ce595b8d8efd5c79d13db285569cc0d6ae3e8bc19a1c788"
P01_MANIFEST_SHA256 = "fa4d731a656286da282d8413ccfe3f2dc8f5aa2cb8eed67d11c1407c12aa1d7b"
P01_FORMAL_PASS_SHA256 = "31ee8e85b4541648c7f11dc5901c482eb1e82719f6600d4b230e5f4b39433fe2"
P01_SHA256SUMS_SHA256 = "5d166715dab84e2f8bea878ccfc3ac1b9911c9120da4b3a8aa33b0703ae914b4"
P02B_DATASET_MANIFEST_SHA256 = "be11fd8a150af8ccd8cb88e1ad18c03b0f6e4f8279c8345be2d44e772977d51b"
P02B_SHA256SUMS_SHA256 = "9bb5270cb2cc9a4eefee4865c1929deb65d565eac9e92d69c4e026a54c0d3c35"
P02B_DATASET_TREE_SHA256 = "baa7c4b9701936253ebaeb4b3436c16403373af0b48c4276dacef780479952c5"
EXPECTED_QUERIES = 1700
EXPECTED_TOTAL_NEIGHBORS = 84104814
EXPECTED_VERTICES = 29987835
EXPECTED_EDGES = 355185382
EXPECTED_DENSE_BYTES = 6638607332
EXPECTED_P02B_FILES = 166
EXPECTED_P02B_BYTES = 9492415008
BLOCK_CACHE_BYTES = 256 * 1024 * 1024
QUERY_TIMEOUT_MS = 60000
FRESH_TIMEOUT_SECONDS = 5400
FREEZE_TIMEOUT_SECONDS = 1800
REOPEN_TIMEOUT_SECONDS = 1800

CANONICAL_REPO = Path("/data/WorkSpace/lsmgraph-cidr-integration")
CANONICAL_SOURCE = Path("/data/WorkSpace/lsmgraph-cidr-deps/Aster-clean")
CANONICAL_WORKER = Path(
    "/data/WorkSpace/results/P10-ASTER-BUILD/"
    "P10-ASTER-BUILD-20260721T200600Z-6abb258e/bin/aster_p10_worker"
)
CANONICAL_DENSE = Path(
    "/data/WorkSpace/lsmgraph-rs/baseline/external-baselines-20260624/"
    "livegraph/sf10-typed-neighbor/edges-dense.txt"
)
CANONICAL_TRUTH = Path(
    "/data/WorkSpace/lsmgraph-rs/baseline/external-baselines-20260626/"
    "3plus3-baselines/systems/neo4j/sf10-main/workload/truth-s50-seed42.tsv"
)
CANONICAL_P01_ROOT = Path(
    "/data/WorkSpace/lsmgraph-rs/cidr-experiments/artifacts/id-maps/"
    "P01-IDMAP-20260721T171325Z-187e851"
)
CANONICAL_P02B_ROOT = Path("/data/WorkSpace/results/P02B")
CANONICAL_OUTPUT_PARENT = Path("/data/WorkSpace/results/P10-ASTER-STORES")

P02B_REQUIRED_SUMS = {
    "sf10-dataset-manifest.json": P02B_DATASET_MANIFEST_SHA256,
    "sf10-naive-store-manifest.json": "56a57e1ba796fe71a7773e6bba29804b547c077956d40614c3a889694a08080f",
    "sf10-schema-store-manifest.json": "d38aaeb916685f3d2f189e55afd2a73de2426253f6045afaf541a303a987a001",
    "sf10-budg-b64-store-manifest.json": "b3f700573500649c4a7fd67f0560927cec0d0a1ca3910ca215638a115c3c90b5",
    "sf10-semantic-store-manifest.json": "f32b8a35a9e2116ceb9ee82755a94eb8765efe65512412bf7333634319fdc122",
    "sf10-shared-truth-plan.json": "4520c88eb594903eb6e3f282838e6cd885e205ee5b0b1790565112d5940f8ea3",
    "sf10-plan-preflight.json": "72348a0b4d200e868ffc18727d81f580b8f1ac2ac31fef889af749dbf291f074",
}

OUTPUT_NAME_RE = re.compile(r"P10-ASTER-SF10-FRESH-\d{8}T\d{6}Z-6abb258e")
HEX64_RE = re.compile(r"[0-9a-f]{64}")
_ACTIVE_PROCESS: Optional[subprocess.Popen[bytes]] = None


class PreparationError(RuntimeError):
    """Fail-closed preparation error."""


class PreparationInterrupted(PreparationError):
    """The wrapper received an interrupt while a stage was active."""


@dataclass(frozen=True)
class PreparationSpec:
    repo_root: Path
    source_root: Path
    source_commit: str
    binary: Path
    binary_sha256: str
    dataset: Path
    dataset_sha256: str
    dataset_size: int
    truth: Path
    truth_sha256: str
    expected_queries: int
    expected_total_neighbors: int
    expected_vertices: int
    expected_edges: int
    p01_manifest: Path
    p01_manifest_sha256: str
    p01_formal_pass: Path
    p01_formal_pass_sha256: str
    p01_sha256sums: Path
    p01_sha256sums_sha256: str
    p02b_dataset_manifest: Path
    p02b_dataset_manifest_sha256: str
    p02b_sha256sums: Path
    p02b_sha256sums_sha256: str
    p02b_required_sums: Mapping[str, str]
    p02b_dataset_tree_sha256: str
    p02b_dataset_files: int
    p02b_dataset_bytes: int


def canonical_spec() -> PreparationSpec:
    return PreparationSpec(
        repo_root=CANONICAL_REPO,
        source_root=CANONICAL_SOURCE,
        source_commit=SOURCE_COMMIT,
        binary=CANONICAL_WORKER,
        binary_sha256=FINAL_WORKER_SHA256,
        dataset=CANONICAL_DENSE,
        dataset_sha256=DENSE_SHA256,
        dataset_size=EXPECTED_DENSE_BYTES,
        truth=CANONICAL_TRUTH,
        truth_sha256=TRUTH_SHA256,
        expected_queries=EXPECTED_QUERIES,
        expected_total_neighbors=EXPECTED_TOTAL_NEIGHBORS,
        expected_vertices=EXPECTED_VERTICES,
        expected_edges=EXPECTED_EDGES,
        p01_manifest=CANONICAL_P01_ROOT / "id-map-manifest.json",
        p01_manifest_sha256=P01_MANIFEST_SHA256,
        p01_formal_pass=CANONICAL_P01_ROOT / "FORMAL-PASS",
        p01_formal_pass_sha256=P01_FORMAL_PASS_SHA256,
        p01_sha256sums=CANONICAL_P01_ROOT / "SHA256SUMS",
        p01_sha256sums_sha256=P01_SHA256SUMS_SHA256,
        p02b_dataset_manifest=CANONICAL_P02B_ROOT / "sf10-dataset-manifest.json",
        p02b_dataset_manifest_sha256=P02B_DATASET_MANIFEST_SHA256,
        p02b_sha256sums=CANONICAL_P02B_ROOT / "SHA256SUMS",
        p02b_sha256sums_sha256=P02B_SHA256SUMS_SHA256,
        p02b_required_sums=dict(P02B_REQUIRED_SUMS),
        p02b_dataset_tree_sha256=P02B_DATASET_TREE_SHA256,
        p02b_dataset_files=EXPECTED_P02B_FILES,
        p02b_dataset_bytes=EXPECTED_P02B_BYTES,
    )


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PreparationError(message)


def require_sha(value: str, context: str) -> str:
    require(isinstance(value, str) and HEX64_RE.fullmatch(value) is not None, f"{context}: invalid SHA-256")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_ref(path: Path, expected_sha: str, context: str) -> Dict[str, Any]:
    path = path.resolve()
    require(path.is_file() and not path.is_symlink(), f"{context}: missing regular file: {path}")
    expected = require_sha(expected_sha, f"{context} expected SHA-256")
    actual = sha256_file(path)
    require(actual == expected, f"{context}: SHA-256 mismatch ({actual} != {expected})")
    return {"path": str(path), "sha256": actual, "size_bytes": path.stat().st_size}


def read_json(path: Path, context: str) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreparationError(f"{context}: cannot read JSON: {exc}") from exc
    require(isinstance(value, dict), f"{context}: expected JSON object")
    return value


def write_json_exclusive(path: Path, value: object) -> None:
    require(not path.exists(), f"refusing to overwrite artifact: {path}")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    require(not temporary.exists(), f"temporary artifact already exists: {temporary}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_text_exclusive(path: Path, value: str) -> None:
    require(not path.exists(), f"refusing to overwrite artifact: {path}")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    require(not temporary.exists(), f"temporary artifact already exists: {temporary}")
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def git_state(root: Path, context: str) -> Dict[str, Any]:
    root = root.resolve()
    require(root.is_dir(), f"{context}: missing Git root: {root}")
    try:
        head = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=20,
        ).strip()
        status = subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PreparationError(f"{context}: cannot inspect Git state: {exc}") from exc
    require(re.fullmatch(r"[0-9a-f]{40}", head) is not None, f"{context}: invalid Git HEAD")
    require(status == "", f"{context}: worktree must be clean")
    return {
        "root": str(root),
        "head": head,
        "clean": True,
        "status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
    }


def parse_sha256s(path: Path, context: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise PreparationError(f"{context}: cannot read SHA256SUMS: {exc}") from exc
    require(bool(lines), f"{context}: SHA256SUMS is empty")
    for line_number, line in enumerate(lines, 1):
        fields = line.split(None, 1)
        require(len(fields) == 2, f"{context}: malformed line {line_number}")
        digest = require_sha(fields[0], f"{context} line {line_number}")
        name = fields[1].lstrip("*")
        candidate = Path(name)
        require(
            name and not candidate.is_absolute() and ".." not in candidate.parts,
            f"{context}: unsafe relative path at line {line_number}",
        )
        require(name not in values, f"{context}: duplicate entry {name}")
        values[name] = digest
    return values


def validate_truth(spec: PreparationSpec) -> Dict[str, Any]:
    truth_ref = file_ref(spec.truth, spec.truth_sha256, "canonical truth")
    expected_header = ["query_index", "edge_type", "src", "count", "sum_hash", "xor_hash"]
    total = 0
    try:
        with spec.truth.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            require(reader.fieldnames == expected_header, "canonical truth header mismatch")
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise PreparationError(f"canonical truth cannot be parsed: {exc}") from exc
    require(len(rows) == spec.expected_queries, "canonical truth query count mismatch")
    for index, row in enumerate(rows):
        try:
            require(int(row["query_index"]) == index, "canonical truth query order mismatch")
            int(row["edge_type"])
            require(int(row["src"]) >= 0, "canonical truth has negative source")
            count = int(row["count"])
            require(count >= 0, "canonical truth has negative count")
            int(row["sum_hash"])
            int(row["xor_hash"])
        except (TypeError, ValueError) as exc:
            raise PreparationError(f"canonical truth row {index} is malformed") from exc
        total += count
    require(total == spec.expected_total_neighbors, "canonical truth total-neighbor count mismatch")
    return {**truth_ref, "query_count": len(rows), "total_neighbors": total}


def validate_p01(spec: PreparationSpec) -> Dict[str, Any]:
    manifest_ref = file_ref(spec.p01_manifest, spec.p01_manifest_sha256, "P01 ID-map manifest")
    pass_ref = file_ref(spec.p01_formal_pass, spec.p01_formal_pass_sha256, "P01 FORMAL-PASS")
    sums_ref = file_ref(spec.p01_sha256sums, spec.p01_sha256sums_sha256, "P01 SHA256SUMS")
    sums = parse_sha256s(spec.p01_sha256sums, "P01 SHA256SUMS")
    require(sums.get("id-map-manifest.json") == spec.p01_manifest_sha256, "P01 manifest is not bound by SHA256SUMS")
    marker = spec.p01_formal_pass.read_text(encoding="utf-8").strip()
    require(marker == f"id-map-manifest.json sha256 {spec.p01_manifest_sha256}", "P01 FORMAL-PASS content mismatch")
    manifest = read_json(spec.p01_manifest, "P01 ID-map manifest")
    require(manifest.get("format") == "seml0-shared-id-map", "P01 format mismatch")
    require(manifest.get("status") == "PASS", "P01 status is not PASS")
    require(manifest.get("formal_pass") is True, "P01 formal_pass is not true")
    require(manifest.get("verification_complete") is True, "P01 verification is incomplete")
    require(manifest.get("vertex_count") == spec.expected_vertices, "P01 vertex count mismatch")
    recovery = manifest.get("recovery")
    require(isinstance(recovery, dict), "P01 recovery object is missing")
    require(recovery.get("expected_vertex_count") == spec.expected_vertices, "P01 expected vertex count mismatch")
    require(recovery.get("expected_edge_count") == spec.expected_edges, "P01 expected edge count mismatch")
    inputs = recovery.get("inputs")
    require(isinstance(inputs, dict) and isinstance(inputs.get("dense"), dict), "P01 dense lineage is missing")
    dense = inputs["dense"]
    require(Path(str(dense.get("path", ""))).resolve() == spec.dataset.resolve(), "P01 dense path mismatch")
    require(dense.get("sha256") == spec.dataset_sha256, "P01 dense SHA mismatch")
    require(dense.get("size_bytes") == spec.dataset_size, "P01 dense size mismatch")
    validation = recovery.get("validation")
    require(isinstance(validation, dict), "P01 validation object is missing")
    for key in (
        "complete_edge_count",
        "complete_vertex_count",
        "edge_type_lockstep",
        "endpoint_lockstep",
        "first_seen_dense_order",
        "input_sha256_during_lockstep",
        "input_sha256_preflight",
        "prefix_bijection",
    ):
        require(validation.get(key) == "PASS", f"P01 validation gate is not PASS: {key}")
    require(validation.get("verified_edge_rows") == spec.expected_edges, "P01 verified edge rows mismatch")
    require(validation.get("recovered_vertex_count") == spec.expected_vertices, "P01 recovered vertices mismatch")
    require(validation.get("reached_dense_eof") is True, "P01 dense EOF gate failed")
    return {"manifest": manifest_ref, "formal_pass": pass_ref, "sha256sums": sums_ref}


def validate_p02b(spec: PreparationSpec) -> Dict[str, Any]:
    manifest_ref = file_ref(
        spec.p02b_dataset_manifest,
        spec.p02b_dataset_manifest_sha256,
        "P02B dataset manifest",
    )
    sums_ref = file_ref(spec.p02b_sha256sums, spec.p02b_sha256sums_sha256, "P02B SHA256SUMS")
    sums = parse_sha256s(spec.p02b_sha256sums, "P02B SHA256SUMS")
    require(sums == dict(spec.p02b_required_sums), "P02B SHA256SUMS entry set/content mismatch")
    root = spec.p02b_sha256sums.parent.resolve()
    verified: Dict[str, Dict[str, Any]] = {}
    for name, expected in sorted(sums.items()):
        path = (root / name).resolve()
        require(path.parent == root, f"P02B checksum entry escapes root: {name}")
        verified[name] = file_ref(path, expected, f"P02B artifact {name}")
    manifest = read_json(spec.p02b_dataset_manifest, "P02B dataset manifest")
    require(manifest.get("schema_version") == "p02b-dataset-manifest-v1", "P02B dataset schema mismatch")
    require(manifest.get("dataset_sha256") == spec.p02b_dataset_tree_sha256, "P02B dataset tree SHA mismatch")
    require(manifest.get("file_count") == spec.p02b_dataset_files, "P02B dataset file count mismatch")
    require(manifest.get("total_bytes") == spec.p02b_dataset_bytes, "P02B dataset byte count mismatch")
    require(
        manifest.get("hash_method") == "sha256-tree-v1(relative-path,size,file-sha256)",
        "P02B dataset hash method mismatch",
    )
    return {"dataset_manifest": manifest_ref, "sha256sums": sums_ref, "verified": verified}


def validate_dataset(spec: PreparationSpec) -> Dict[str, Any]:
    dataset_ref = file_ref(spec.dataset, spec.dataset_sha256, "canonical dense dataset")
    require(dataset_ref["size_bytes"] == spec.dataset_size, "canonical dense dataset size mismatch")
    try:
        with spec.dataset.open("r", encoding="utf-8") as handle:
            header = handle.readline().rstrip("\n")
    except (OSError, UnicodeError) as exc:
        raise PreparationError(f"cannot read canonical dense header: {exc}") from exc
    require(header == str(spec.expected_vertices), "canonical dense vertex header mismatch")
    return {**dataset_ref, "declared_vertices": spec.expected_vertices, "expected_edges": spec.expected_edges}


def validate_priority(required: bool) -> Dict[str, Any]:
    nice_value = os.getpriority(os.PRIO_PROCESS, 0)
    ionice_raw = shutil.which("ionice")
    require(ionice_raw is not None, "ionice executable is missing")
    ionice = Path(ionice_raw).resolve()
    completed = subprocess.run(
        [str(ionice), "-p", str(os.getpid())],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=20,
    )
    require(completed.returncode == 0, f"cannot inspect ionice class: {completed.stderr.strip()}")
    observed = completed.stdout.strip().lower()
    if required:
        require(nice_value >= 10, "canonical preparation must run at nice >= 10")
        require("best-effort" in observed and "prio 7" in observed, "canonical preparation requires ionice best-effort/7")
    return {
        "nice": nice_value,
        "ionice": observed,
        "ionice_binary": {
            "path": str(ionice),
            "sha256": sha256_file(ionice),
            "size_bytes": ionice.stat().st_size,
        },
        "required": required,
    }


def worker_capabilities(binary: Path) -> Dict[str, Any]:
    completed = subprocess.run(
        [str(binary), "--capabilities"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    require(completed.returncode == 0, f"Aster worker capability probe failed: {completed.stderr.strip()}")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise PreparationError(f"Aster worker capability JSON is malformed: {exc}") from exc
    require(value.get("schema_version") == "p10-aster-rocksgraph-capabilities-v1", "Aster capability schema mismatch")
    require(value.get("store_capability") == "fresh-import-and-reopen-v1", "Aster store capability mismatch")
    require(value.get("typed_bridge") == "compact-first-occurrence-id-per-edge-type-src-v1", "Aster typed bridge mismatch")
    return value


def prepare_output_root(output_root: Path, required_parent: Optional[Path]) -> Path:
    require(output_root.is_absolute(), "--output-root must be absolute")
    resolved = output_root.resolve(strict=False)
    require(resolved == output_root, "--output-root must already be canonical (no symlink/.. aliases)")
    if required_parent is not None:
        parent = required_parent.resolve()
        require(parent.is_dir(), f"canonical output parent does not exist: {parent}")
        require(resolved.parent == parent, "--output-root must be a direct child of the canonical output parent")
        require(OUTPUT_NAME_RE.fullmatch(resolved.name) is not None, "--output-root name is outside the frozen convention")
    require(not resolved.exists(), "output root already exists; refusing to overwrite or resume")
    try:
        resolved.mkdir(mode=0o755)
    except FileExistsError as exc:
        raise PreparationError("output root appeared concurrently; refusing to continue") from exc
    return resolved


def request_document(
    spec: PreparationSpec,
    run_id: str,
    store: Path,
    store_sha256: str,
) -> Dict[str, Any]:
    if store_sha256:
        require_sha(store_sha256, "request store SHA-256")
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "suite_id": "p10-aster-sf10-store-preparation-correctness-only",
        "run_id": run_id,
        "execution_mode": "fixture",
        "system_id": "aster",
        "group": "embedded",
        "system_version": f"aster-rocksgraph@{spec.source_commit}",
        "interface_scope": INTERFACE_SCOPE,
        "repeat_index": 1,
        "process_lifetime": FIXTURE_PROCESS_LIFETIME,
        "binary": {"path": str(spec.binary.resolve()), "sha256": spec.binary_sha256},
        "dataset": {"path": str(spec.dataset.resolve()), "sha256": spec.dataset_sha256},
        "runtime_libraries": [],
        "store_roots": [{"label": "aster", "path": str(store.resolve()), "sha256": store_sha256}],
        "truth": {
            "path": str(spec.truth.resolve()),
            "sha256": spec.truth_sha256,
            "query_count": spec.expected_queries,
            "digest_algorithm": TRUTH_DIGEST_ALGORITHM,
        },
        "timing": {
            "timing_boundary": TIMING_BOUNDARY,
            "clock": CLOCK_NAME,
            "cache_policy": "full-shared-trace-before-each-measured-repeat-v1",
            "process_reuse_between_phases": True,
            "warmup_passes": 1,
            "measured_passes": 1,
            "concurrency": 1,
            "per_query_timeout_ms": QUERY_TIMEOUT_MS,
            "sequence_digest_algorithm": SEQUENCE_DIGEST_ALGORITHM,
        },
    }


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait(timeout=10)


def signal_handler(signum: int, _frame: object) -> None:
    global _ACTIVE_PROCESS
    if _ACTIVE_PROCESS is not None:
        try:
            os.killpg(_ACTIVE_PROCESS.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    raise PreparationInterrupted(f"received signal {signum}")


def run_stage(
    run_root: Path,
    stage: str,
    argv: Sequence[str],
    cwd: Path,
    timeout_seconds: int,
) -> Dict[str, Any]:
    global _ACTIVE_PROCESS
    command_path = run_root / f"{stage}.command.json"
    result_path = run_root / f"{stage}.result.json"
    stdout_path = run_root / f"{stage}.stdout.log"
    stderr_path = run_root / f"{stage}.stderr.log"
    command = [str(value) for value in argv]
    write_json_exclusive(
        command_path,
        {
            "stage": stage,
            "argv": command,
            "argv_sha256": hashlib.sha256(
                json.dumps(command, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "cwd": str(cwd.resolve()),
            "timeout_seconds": timeout_seconds,
            "started_at_utc": utc_now(),
        },
    )
    started = time.monotonic()
    result: Dict[str, Any]
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    with stdout_path.open("xb") as stdout_handle, stderr_path.open("xb") as stderr_handle:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
        )
        _ACTIVE_PROCESS = process
        try:
            try:
                returncode = process.wait(timeout=timeout_seconds)
                result = {"exit_code": returncode, "timed_out": False, "interrupted": False}
            except subprocess.TimeoutExpired:
                stop_process(process)
                result = {"exit_code": process.returncode, "timed_out": True, "interrupted": False}
        except BaseException:
            stop_process(process)
            if not result_path.exists():
                write_json_exclusive(
                    result_path,
                    {
                        "stage": stage,
                        "exit_code": process.returncode,
                        "timed_out": False,
                        "interrupted": True,
                        "elapsed_seconds": time.monotonic() - started,
                        "finished_at_utc": utc_now(),
                    },
                )
            raise
        finally:
            _ACTIVE_PROCESS = None
    result.update(
        {
            "stage": stage,
            "elapsed_seconds": time.monotonic() - started,
            "finished_at_utc": utc_now(),
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
        }
    )
    write_json_exclusive(result_path, result)
    require(not result["timed_out"], f"{stage} exceeded {timeout_seconds} seconds")
    require(result["exit_code"] == 0, f"{stage} exited {result['exit_code']}; evidence is preserved")
    return result


def validate_adapter_output(
    output_dir: Path,
    spec: PreparationSpec,
    lifecycle: str,
) -> Dict[str, Any]:
    result = read_json(output_dir / "adapter-result.json", f"{lifecycle} adapter result")
    require(result.get("system_id") == "aster", f"{lifecycle}: wrong system_id")
    require(result.get("process_lifetime") == FIXTURE_PROCESS_LIFETIME, f"{lifecycle}: wrong process lifetime")
    phase_summary: Dict[str, Any] = {}
    for phase in ("warmup", "measured"):
        value = result.get(phase)
        require(isinstance(value, dict), f"{lifecycle}: missing {phase} result")
        require(value.get("passes") == 1, f"{lifecycle}: {phase} pass count mismatch")
        require(value.get("requested_queries") == spec.expected_queries, f"{lifecycle}: {phase} requested count mismatch")
        require(value.get("completed_queries") == spec.expected_queries, f"{lifecycle}: {phase} completed count mismatch")
        require(value.get("timeout_queries") == 0, f"{lifecycle}: {phase} timeout detected")
        require(value.get("mismatch_queries") == 0, f"{lifecycle}: {phase} mismatch detected")

    observations = output_dir / "query-observations.tsv"
    try:
        with observations.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            require(reader.fieldnames == OBSERVATION_COLUMNS, f"{lifecycle}: observation header mismatch")
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise PreparationError(f"{lifecycle}: cannot parse observations: {exc}") from exc
    require(len(rows) == 2 * spec.expected_queries, f"{lifecycle}: observation row count mismatch")
    offset = 0
    for phase in ("warmup", "measured"):
        total_neighbors = 0
        for query_index in range(spec.expected_queries):
            row = rows[offset]
            offset += 1
            require(row["phase"] == phase, f"{lifecycle}: observation phase order mismatch")
            require(row["pass_index"] == "0", f"{lifecycle}: observation pass index mismatch")
            require(int(row["query_index"]) == query_index, f"{lifecycle}: observation query order mismatch")
            require(row["status"] == "ok", f"{lifecycle}: non-ok query status")
            for expected_name, actual_name in (
                ("expected_count", "actual_count"),
                ("expected_sum_hash", "actual_sum_hash"),
                ("expected_xor_hash", "actual_xor_hash"),
            ):
                require(row[expected_name] == row[actual_name], f"{lifecycle}: digest mismatch at query {query_index}")
            require(int(row["latency_ns"]) > 0, f"{lifecycle}: non-positive latency")
            total_neighbors += int(row["actual_count"])
        require(total_neighbors == spec.expected_total_neighbors, f"{lifecycle}: {phase} total-neighbor mismatch")
        phase_summary[phase] = {
            "queries": spec.expected_queries,
            "mismatches": 0,
            "timeouts": 0,
            "total_neighbors": total_neighbors,
        }

    provenance_path = output_dir / "adapter-provenance.json"
    provenance = read_json(provenance_path, f"{lifecycle} adapter provenance")
    require(provenance.get("mode") == "fixture", f"{lifecycle}: provenance mode is not fixture")
    require(provenance.get("lifecycle") == lifecycle, f"{lifecycle}: provenance lifecycle mismatch")
    binary = provenance.get("binary")
    dataset = provenance.get("dataset")
    store = provenance.get("store")
    require(isinstance(binary, dict) and binary.get("sha256") == spec.binary_sha256, f"{lifecycle}: binary lineage mismatch")
    require(isinstance(dataset, dict) and dataset.get("sha256") == spec.dataset_sha256, f"{lifecycle}: dataset lineage mismatch")
    require(isinstance(store, dict), f"{lifecycle}: store provenance is missing")
    post_store_sha = require_sha(str(store.get("post_store_sha256", "")), f"{lifecycle} post-store SHA")
    return {
        "adapter_result": file_ref(output_dir / "adapter-result.json", sha256_file(output_dir / "adapter-result.json"), f"{lifecycle} result"),
        "observations": file_ref(observations, sha256_file(observations), f"{lifecycle} observations"),
        "provenance": file_ref(provenance_path, sha256_file(provenance_path), f"{lifecycle} provenance"),
        "phases": phase_summary,
        "post_store_sha256": post_store_sha,
    }


def validate_store_manifest(path: Path, spec: PreparationSpec, store: Path) -> Dict[str, Any]:
    manifest = read_json(path, "Aster store manifest")
    require(manifest.get("schema_version") == "p10-aster-store-manifest-v1", "Aster store manifest schema mismatch")
    require(Path(str(manifest.get("store_path", ""))).resolve() == store.resolve(), "Aster store manifest path mismatch")
    store_sha = require_sha(str(manifest.get("store_sha256", "")), "Aster logical store SHA")
    source = manifest.get("source")
    binary = manifest.get("binary")
    dataset = manifest.get("dataset")
    require(isinstance(source, dict) and source.get("head") == spec.source_commit, "Aster manifest source mismatch")
    require(isinstance(binary, dict) and binary.get("sha256") == spec.binary_sha256, "Aster manifest binary mismatch")
    require(isinstance(dataset, dict) and dataset.get("sha256") == spec.dataset_sha256, "Aster manifest dataset mismatch")
    return {
        "artifact": {
            "path": str(path.resolve()),
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
        },
        "store_sha256": store_sha,
        "snapshot_tree_sha256": require_sha(str(manifest.get("snapshot_tree_sha256", "")), "Aster snapshot tree SHA"),
        "file_count": manifest.get("file_count"),
        "total_bytes": manifest.get("total_bytes"),
    }


def write_small_artifact_sums(run_root: Path, store: Path) -> Dict[str, Any]:
    artifacts: List[Path] = []
    for directory, dirnames, filenames in os.walk(run_root, followlinks=False):
        directory_path = Path(directory)
        dirnames[:] = [name for name in sorted(dirnames) if (directory_path / name).resolve() != store.resolve()]
        for name in sorted(filenames):
            path = directory_path / name
            require(path.is_file() and not path.is_symlink(), f"non-regular preparation artifact: {path}")
            if path.name in {"SHA256SUMS", "DONE.correctness-only", "FAILED.json"}:
                continue
            artifacts.append(path)
    artifacts.sort(key=lambda value: value.relative_to(run_root).as_posix().encode("utf-8"))
    lines = []
    total_bytes = 0
    for path in artifacts:
        relative = path.relative_to(run_root).as_posix()
        lines.append(f"{sha256_file(path)}  {relative}\n")
        total_bytes += path.stat().st_size
    sums_path = run_root / "SHA256SUMS"
    write_text_exclusive(sums_path, "".join(lines))
    return {
        "path": str(sums_path),
        "sha256": sha256_file(sums_path),
        "file_count": len(artifacts),
        "total_bytes": total_bytes,
    }


def execute_preparation(
    output_root: Path,
    spec: PreparationSpec,
    *,
    require_priority: bool,
    required_output_parent: Optional[Path],
) -> Dict[str, Any]:
    run_root: Optional[Path] = None
    previous_handlers: Dict[int, Any] = {}
    try:
        run_root = prepare_output_root(output_root, required_output_parent)
        write_json_exclusive(
            run_root / "STARTED.json",
            {
                "schema_version": SCHEMA_VERSION,
                "state": "STARTED",
                "started_at_utc": utc_now(),
                "performance_eligible": False,
                "formal_performance_points": 0,
                "output_root": str(run_root),
            },
        )
        for signal_number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous_handlers[signal_number] = signal.getsignal(signal_number)
            signal.signal(signal_number, signal_handler)

        priority = validate_priority(require_priority)
        require(spec.repo_root.resolve().is_dir(), "P10 repository is missing")
        require(spec.source_root.resolve().is_dir(), "Aster source root is missing")
        repo_pre = git_state(spec.repo_root, "P10 repository")
        source_pre = git_state(spec.source_root, "Aster source")
        require(source_pre["head"] == spec.source_commit, "Aster source commit mismatch")
        binary_ref = file_ref(spec.binary, spec.binary_sha256, "final Aster worker")
        require(os.access(spec.binary, os.X_OK), "final Aster worker is not executable")
        dataset = validate_dataset(spec)
        truth = validate_truth(spec)
        p01 = validate_p01(spec)
        p02b = validate_p02b(spec)
        capabilities = worker_capabilities(spec.binary)

        adapter = (spec.repo_root / "cidr-experiments/runners/p10/adapters/aster_adapter.py").resolve()
        manifest_builder = (
            spec.repo_root / "cidr-experiments/runners/p10/adapters/build_aster_store_manifest.py"
        ).resolve()
        adapter_ref = file_ref(adapter, sha256_file(adapter), "Aster adapter")
        builder_ref = file_ref(manifest_builder, sha256_file(manifest_builder), "Aster manifest builder")
        wrapper_ref = file_ref(Path(__file__).resolve(), sha256_file(Path(__file__).resolve()), "Aster preparation wrapper")
        preflight = {
            "schema_version": SCHEMA_VERSION,
            "state": "PASS",
            "performance_eligible": False,
            "formal_performance_points": 0,
            "priority": priority,
            "repo": repo_pre,
            "source": source_pre,
            "binary": binary_ref,
            "dataset": dataset,
            "truth": truth,
            "p01": p01,
            "p02b": p02b,
            "capabilities": capabilities,
            "adapter": adapter_ref,
            "manifest_builder": builder_ref,
            "wrapper": wrapper_ref,
            "service_actions": [],
        }
        write_json_exclusive(run_root / "preflight.json", preflight)

        store = run_root / "db"
        store.mkdir(mode=0o755)
        require(not any(store.iterdir()), "fresh Aster store is not empty")
        fresh_output = run_root / "fresh-output"
        fresh_request = request_document(spec, f"{run_root.name}-fresh", store, "")
        fresh_request_path = run_root / "fresh-request.json"
        write_json_exclusive(fresh_request_path, fresh_request)
        python = str(Path(sys.executable).resolve())
        common = [
            "--source-root",
            str(spec.source_root.resolve()),
            "--source-commit",
            spec.source_commit,
            "--repo-root",
            str(spec.repo_root.resolve()),
            "--binary",
            str(spec.binary.resolve()),
            "--binary-sha256",
            spec.binary_sha256,
            "--dataset",
            str(spec.dataset.resolve()),
            "--dataset-sha256",
            spec.dataset_sha256,
            "--store-label",
            "aster",
            "--block-cache-bytes",
            str(BLOCK_CACHE_BYTES),
        ]
        fresh_stage = run_stage(
            run_root,
            "01-fresh-import",
            [
                python,
                "-B",
                str(adapter),
                "--mode",
                "fixture",
                "--lifecycle",
                "fresh",
                *common,
                "--request",
                str(fresh_request_path),
                "--output-dir",
                str(fresh_output),
            ],
            spec.repo_root,
            FRESH_TIMEOUT_SECONDS,
        )
        fresh_validation = validate_adapter_output(fresh_output, spec, "fresh")

        store_manifest = run_root / "aster-store-manifest.json"
        freeze_stage = run_stage(
            run_root,
            "02-offline-freeze",
            [
                python,
                "-B",
                str(manifest_builder),
                "--store",
                str(store),
                "--output",
                str(store_manifest),
                "--source-root",
                str(spec.source_root.resolve()),
                "--source-commit",
                spec.source_commit,
                "--binary",
                str(spec.binary.resolve()),
                "--dataset",
                str(spec.dataset.resolve()),
            ],
            spec.repo_root,
            FREEZE_TIMEOUT_SECONDS,
        )
        frozen = validate_store_manifest(store_manifest, spec, store)
        require(
            frozen["store_sha256"] == fresh_validation["post_store_sha256"],
            "offline frozen logical store SHA differs from fresh-import store",
        )

        reopen_output = run_root / "reopen-output"
        reopen_request = request_document(
            spec,
            f"{run_root.name}-reopen",
            store,
            frozen["store_sha256"],
        )
        reopen_request_path = run_root / "reopen-request.json"
        write_json_exclusive(reopen_request_path, reopen_request)
        reopen_stage = run_stage(
            run_root,
            "03-frozen-reopen-correctness",
            [
                python,
                "-B",
                str(adapter),
                "--mode",
                "fixture",
                "--lifecycle",
                "reopen",
                *common,
                "--store-manifest",
                str(store_manifest),
                "--store-manifest-sha256",
                frozen["artifact"]["sha256"],
                "--request",
                str(reopen_request_path),
                "--output-dir",
                str(reopen_output),
            ],
            spec.repo_root,
            REOPEN_TIMEOUT_SECONDS,
        )
        reopen_validation = validate_adapter_output(reopen_output, spec, "reopen")
        require(
            reopen_validation["post_store_sha256"] == frozen["store_sha256"],
            "frozen store logical SHA changed during reopen correctness",
        )

        repo_post = git_state(spec.repo_root, "P10 repository after preparation")
        source_post = git_state(spec.source_root, "Aster source after preparation")
        require(repo_post == repo_pre, "P10 repository state changed during preparation")
        require(source_post == source_pre, "Aster source state changed during preparation")
        provenance_text = (
            f"schema_version={SCHEMA_VERSION}\n"
            "performance_eligible=false\n"
            "formal_performance_points=0\n"
            "purpose=aster-sf10-store-preparation-correctness-only\n"
            f"repo_head={repo_pre['head']}\n"
            f"source_commit={spec.source_commit}\n"
            f"binary_sha256={spec.binary_sha256}\n"
            f"dataset_sha256={spec.dataset_sha256}\n"
            f"truth_sha256={spec.truth_sha256}\n"
            f"p01_manifest_sha256={spec.p01_manifest_sha256}\n"
            f"p02b_sha256sums_sha256={spec.p02b_sha256sums_sha256}\n"
            f"logical_store_sha256={frozen['store_sha256']}\n"
            "expected_queries=1700\n"
            "mismatches=0\n"
            "total_neighbors=84104814\n"
        )
        write_text_exclusive(run_root / "provenance.env", provenance_text)
        summary = {
            "schema_version": SCHEMA_VERSION,
            "state": "PASS",
            "performance_eligible": False,
            "formal_performance_points": 0,
            "purpose": "aster-sf10-store-preparation-correctness-only",
            "output_root": str(run_root),
            "store": {
                "path": str(store),
                "logical_sha256": frozen["store_sha256"],
                "snapshot_tree_sha256": frozen["snapshot_tree_sha256"],
                "file_count": frozen["file_count"],
                "total_bytes": frozen["total_bytes"],
                "manifest": frozen["artifact"],
            },
            "acceptance": {
                "checked": spec.expected_queries,
                "mismatches": 0,
                "timeouts": 0,
                "total_neighbors": spec.expected_total_neighbors,
                "warmup_passes": 1,
                "measured_passes": 1,
            },
            "correctness": {"fresh": fresh_validation, "frozen_reopen": reopen_validation},
            "stages": {
                "fresh_import": fresh_stage,
                "offline_freeze": freeze_stage,
                "frozen_reopen_correctness": reopen_stage,
            },
            "lineage": preflight,
            "repo_post": repo_post,
            "source_post": source_post,
            "service_actions": [],
            "completed_at_utc": utc_now(),
        }
        summary_path = run_root / "preparation-summary.json"
        write_json_exclusive(summary_path, summary)
        sums = write_small_artifact_sums(run_root, store)
        done = {
            "schema_version": SCHEMA_VERSION,
            "state": "PASS",
            "classification": "correctness-only",
            "performance_eligible": False,
            "formal_performance_points": 0,
            "summary_sha256": sha256_file(summary_path),
            "sha256sums_sha256": sums["sha256"],
            "logical_store_sha256": frozen["store_sha256"],
            "checked": spec.expected_queries,
            "mismatches": 0,
            "total_neighbors": spec.expected_total_neighbors,
            "completed_at_utc": utc_now(),
        }
        write_json_exclusive(run_root / "DONE.correctness-only", done)
        return summary
    except BaseException as exc:
        if run_root is not None and run_root.exists() and not (run_root / "DONE.correctness-only").exists():
            failed = run_root / "FAILED.json"
            if not failed.exists():
                try:
                    write_json_exclusive(
                        failed,
                        {
                            "schema_version": SCHEMA_VERSION,
                            "state": "FAILED",
                            "performance_eligible": False,
                            "formal_performance_points": 0,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "failed_at_utc": utc_now(),
                            "evidence_preserved": True,
                        },
                    )
                except Exception:
                    pass
        raise
    finally:
        for signal_number, previous in previous_handlers.items():
            signal.signal(signal_number, previous)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--binary", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    spec = canonical_spec()
    try:
        require(args.repo_root.resolve() == spec.repo_root.resolve(), "--repo-root differs from canonical integration")
        require(args.source_root.resolve() == spec.source_root.resolve(), "--source-root differs from canonical Aster source")
        require(args.binary.resolve() == spec.binary.resolve(), "--binary differs from final canonical worker")
        summary = execute_preparation(
            args.output_root,
            spec,
            require_priority=True,
            required_output_parent=CANONICAL_OUTPUT_PARENT,
        )
        print(json.dumps({"state": "PASS", "output_root": summary["output_root"]}, sort_keys=True))
        return 0
    except PreparationInterrupted as exc:
        print(f"INTERRUPTED: {exc}", file=sys.stderr)
        return 130
    except (PreparationError, OSError, UnicodeError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
