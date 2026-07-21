#!/usr/bin/env python3
"""Freeze one correctness-only TuGraph SF10 import configuration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path


SCHEMA = "cidr-p10-tugraph-sf10-import-config-v1"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


class ConfigError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_ref(path: Path) -> dict[str, object]:
    path = path.resolve()
    if not path.is_file():
        raise ConfigError(f"missing regular file: {path}")
    return {"path": str(path), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def run(args: argparse.Namespace) -> None:
    dataset = file_ref(args.dataset)
    if dataset["sha256"] != args.dataset_sha256:
        raise ConfigError("dataset SHA-256 differs from --dataset-sha256")
    edge_map = file_ref(args.edge_type_map)
    importer = file_ref(args.importer)
    runtime_manifest = file_ref(args.runtime_manifest)
    try:
        runtime = json.loads(args.runtime_manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read runtime manifest: {exc}") from exc
    if runtime.get("schema_version") != "cidr-p10-tugraph-sf10-importer-runtime-v1":
        raise ConfigError("importer runtime manifest schema drift")
    if runtime.get("importer", {}).get("sha256") != importer["sha256"]:
        raise ConfigError("runtime manifest describes a different importer")
    if not args.store.is_absolute():
        raise ConfigError("store must be supplied as an absolute path")
    store = args.store.resolve()
    if store.exists():
        raise ConfigError("store must be an absolute, nonexistent path")
    if not store.parent.is_dir():
        raise ConfigError("store parent must already exist")
    if args.expected_vertices < 1 or args.expected_edges < 1:
        raise ConfigError("expected counts must be positive")
    if args.batch_size < 1 or args.progress_every < 1:
        raise ConfigError("batch/progress intervals must be positive")
    if not HEX64.fullmatch(args.dataset_sha256):
        raise ConfigError("dataset SHA-256 is malformed")
    if not GIT_SHA.fullmatch(args.source_revision):
        raise ConfigError("source revision must be a full lowercase Git SHA")
    password = os.environ.get("CIDR_TUGRAPH_PASSWORD")
    if not password:
        raise ConfigError("CIDR_TUGRAPH_PASSWORD is required")
    password_sha256 = hashlib.sha256(password.encode("utf-8")).hexdigest()
    value = {
        "schema_version": SCHEMA,
        "system_version": "TuGraph-4.5.2-native-embedded",
        "performance_eligible": False,
        "formal_performance_points": 0,
        "dataset": {
            **dataset,
            "expected_vertex_count": args.expected_vertices,
            "expected_edge_count": args.expected_edges,
        },
        "edge_type_map": edge_map,
        "database": {
            "path": str(store),
            "graph": args.graph,
            "user": args.user,
            "credential_environment": "CIDR_TUGRAPH_PASSWORD",
            "password_sha256": password_sha256,
            "fresh_path_required": True,
        },
        "importer": {
            **importer,
            "runtime_manifest": runtime_manifest,
        },
        "import_policy": {
            "batch_size": args.batch_size,
            "progress_every": args.progress_every,
            "nice": 19,
            "ionice_class": 3,
            "timing_scope": "engineering-log-only-not-paper-performance",
        },
        "source_revision": args.source_revision,
    }
    atomic_json(args.output.resolve(), value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--dataset-sha256", required=True)
    parser.add_argument("--edge-type-map", required=True, type=Path)
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--importer", required=True, type=Path)
    parser.add_argument("--runtime-manifest", required=True, type=Path)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--expected-vertices", required=True, type=int)
    parser.add_argument("--expected-edges", required=True, type=int)
    parser.add_argument("--batch-size", type=int, default=100000)
    parser.add_argument("--progress-every", type=int, default=5000000)
    parser.add_argument("--graph", default="default")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    try:
        run(parse_args())
        return 0
    except (ConfigError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
