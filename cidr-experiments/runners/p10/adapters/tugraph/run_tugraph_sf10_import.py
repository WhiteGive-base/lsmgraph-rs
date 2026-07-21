#!/usr/bin/env python3
"""Validate and execute one frozen, correctness-only TuGraph SF10 import."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


SCHEMA = "cidr-p10-tugraph-sf10-import-config-v1"


class ImportError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def checked_ref(value: Any, context: str) -> Path:
    if not isinstance(value, dict) or not isinstance(value.get("path"), str) or not isinstance(
        value.get("sha256"), str
    ):
        raise ImportError(f"{context} file reference is malformed")
    path = Path(value["path"])
    if not path.is_absolute() or not path.is_file():
        raise ImportError(f"{context} path is not an absolute regular file")
    if sha256_file(path) != value["sha256"]:
        raise ImportError(f"{context} SHA-256 drift")
    if "size_bytes" in value and path.stat().st_size != value["size_bytes"]:
        raise ImportError(f"{context} size drift")
    return path


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ImportError(f"cannot read import config: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA:
        raise ImportError("import config schema drift")
    if value.get("performance_eligible") is not False or value.get("formal_performance_points") != 0:
        raise ImportError("SF10 rebuild must remain correctness-only")
    return value


def run(args: argparse.Namespace) -> None:
    config = load(args.config.resolve())
    dataset = checked_ref(config.get("dataset"), "dataset")
    edge_map = checked_ref(config.get("edge_type_map"), "edge-type map")
    importer_value = config.get("importer")
    importer = checked_ref(importer_value, "importer")
    checked_ref(importer_value.get("runtime_manifest"), "importer runtime manifest")
    database = config.get("database")
    policy = config.get("import_policy")
    if not isinstance(database, dict) or not isinstance(policy, dict):
        raise ImportError("database/import policy section is malformed")
    store = Path(database.get("path", ""))
    if not store.is_absolute() or store.exists() or not store.parent.is_dir():
        raise ImportError("store must remain an absolute nonexistent path with an existing parent")
    if database.get("fresh_path_required") is not True:
        raise ImportError("fresh-path policy drift")
    if database.get("credential_environment") != "CIDR_TUGRAPH_PASSWORD":
        raise ImportError("credential environment drift")
    password = os.environ.get("CIDR_TUGRAPH_PASSWORD")
    if not password:
        raise ImportError("CIDR_TUGRAPH_PASSWORD is required")
    if hashlib.sha256(password.encode("utf-8")).hexdigest() != database.get("password_sha256"):
        raise ImportError("CIDR_TUGRAPH_PASSWORD does not match the frozen password hash")
    expected_vertices = config["dataset"].get("expected_vertex_count")
    expected_edges = config["dataset"].get("expected_edge_count")
    batch_size = policy.get("batch_size")
    progress_every = policy.get("progress_every")
    if not all(isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in (
        expected_vertices,
        expected_edges,
        batch_size,
        progress_every,
    )):
        raise ImportError("count/batch policy is malformed")
    if policy.get("nice") != 19 or policy.get("ionice_class") != 3:
        raise ImportError("low-priority execution policy drift")
    result = args.result_json.resolve()
    if result.exists() or result == store:
        raise ImportError("result JSON must be a fresh path distinct from the store")
    result.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "nice",
        "-n",
        "19",
        "ionice",
        "-c",
        "3",
        str(importer),
        "--edges",
        str(dataset),
        "--db-dir",
        str(store),
        "--edge-type-map",
        str(edge_map),
        "--result-json",
        str(result),
        "--graph",
        str(database.get("graph")),
        "--user",
        str(database.get("user")),
        "--expected-vertices",
        str(expected_vertices),
        "--expected-edges",
        str(expected_edges),
        "--batch-size",
        str(batch_size),
        "--progress-every",
        str(progress_every),
    ]
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise ImportError(f"native importer failed with exit code {completed.returncode}")
    try:
        value = json.loads(result.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ImportError(f"cannot validate importer result: {exc}") from exc
    if (
        value.get("schema_version") != "cidr-p10-tugraph-sf10-import-result-v1"
        or value.get("status") != "PASS"
        or value.get("loaded_vertices") != expected_vertices
        or value.get("loaded_edges") != expected_edges
        or value.get("performance_eligible") is not False
        or value.get("formal_performance_points") != 0
    ):
        raise ImportError("importer result contract failed")
    if not store.is_dir():
        raise ImportError("importer returned PASS without creating the store")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--result-json", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    try:
        run(parse_args())
        return 0
    except (ImportError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
