#!/usr/bin/env python3
"""Create the immutable TuGraph matched-store contract consumed by P10."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Sequence


STORE_SCHEMA = "cidr-p10-tugraph-store-v1"
INTERFACE_SCOPE = "typed-neighbor-dense-id-v1"
VERTEX_ID_CONTRACT = "internal-vid-equals-dense-id-v1"
TRUTH_COLUMNS = ["query_index", "edge_type", "src", "count", "sum_hash", "xor_hash"]
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
LABEL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ManifestError(RuntimeError):
    pass


def unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ManifestError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ManifestError(f"{path} must contain one JSON object")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha(value: str, context: str) -> str:
    if not HEX64_RE.fullmatch(value):
        raise ManifestError(f"{context} must be a lowercase SHA-256")
    return value


def file_ref(path: Path, executable: bool = False) -> dict[str, str]:
    path = path.resolve()
    if not path.is_file():
        raise ManifestError(f"missing file: {path}")
    if executable and not os.access(path, os.X_OK):
        raise ManifestError(f"file is not executable: {path}")
    return {"path": str(path), "sha256": sha256_file(path)}


def read_truth(path: Path) -> set[int]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if reader.fieldnames != TRUTH_COLUMNS:
                raise ManifestError("truth TSV header drift")
            edge_types: set[int] = set()
            count = 0
            for row in reader:
                if int(row["query_index"]) != count:
                    raise ManifestError("truth query_index must be contiguous")
                edge_types.add(int(row["edge_type"]))
                for key in ("src", "count", "sum_hash", "xor_hash"):
                    if int(row[key]) < 0:
                        raise ManifestError(f"truth {key} must be non-negative")
                count += 1
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        if isinstance(exc, ManifestError):
            raise
        raise ManifestError(f"cannot validate truth TSV: {exc}") from exc
    if count != 1700:
        raise ManifestError(f"TuGraph P10 requires 1700 truth rows, found {count}")
    return edge_types


def read_edge_map(path: Path, expected: set[int]) -> list[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if reader.fieldnames != ["edge_type", "label"]:
                raise ManifestError("edge-label TSV header drift")
            result: list[dict[str, Any]] = []
            seen_types: set[int] = set()
            seen_labels: set[str] = set()
            for row in reader:
                edge_type = int(row["edge_type"])
                label = row["label"]
                if (
                    not LABEL_RE.fullmatch(label)
                    or edge_type in seen_types
                    or label in seen_labels
                ):
                    raise ManifestError("edge-label TSV contains invalid or duplicate entry")
                seen_types.add(edge_type)
                seen_labels.add(label)
                result.append({"edge_type": edge_type, "label": label})
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        if isinstance(exc, ManifestError):
            raise
        raise ManifestError(f"cannot read edge-label TSV: {exc}") from exc
    if seen_types != expected or [row["edge_type"] for row in result] != sorted(expected):
        raise ManifestError("edge-label TSV must exactly cover sorted truth edge types")
    return result


def read_store_lineage(path: Path, store: Path) -> dict[str, Any]:
    value = read_json(path)
    expected_keys = {
        "schema_version",
        "store_root",
        "store_sha256",
        "hash_method",
        "file_count",
        "total_bytes",
    }
    if set(value) != expected_keys or value["schema_version"] != "p02b-store-manifest-v1":
        raise ManifestError("P02B store-lineage manifest schema/key drift")
    root = Path(value["store_root"]).resolve()
    if root != store or value["store_root"] != str(root):
        raise ManifestError("P02B store lineage describes a different root")
    if value["hash_method"] != "sha256-tree-v1(relative-path,size,file-sha256)":
        raise ManifestError("P02B store lineage hash method drift")
    sha(value["store_sha256"], "store_sha256")
    for key in ("file_count", "total_bytes"):
        if isinstance(value[key], bool) or not isinstance(value[key], int) or value[key] < 1:
            raise ManifestError(f"P02B store lineage {key} is invalid")
    return value


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def run(args: argparse.Namespace) -> None:
    store = args.store.resolve()
    dataset = args.dataset.resolve()
    truth = args.truth.resolve()
    if not store.is_dir() or not dataset.exists() or not truth.is_file():
        raise ManifestError("store/dataset/truth inputs must exist")
    lineage = read_store_lineage(args.store_lineage.resolve(), store)
    edge_types = read_truth(truth)
    labels = read_edge_map(args.edge_labels.resolve(), edge_types)
    truth_sha = sha256_file(truth)
    if truth_sha != sha(args.truth_sha256, "--truth-sha256"):
        raise ManifestError("truth SHA-256 differs from --truth-sha256")
    dataset_sha = sha(args.dataset_sha256, "--dataset-sha256")
    if dataset.is_file() and sha256_file(dataset) != dataset_sha:
        raise ManifestError("dataset file SHA-256 differs from --dataset-sha256")
    if args.vertex_count < 1:
        raise ManifestError("--vertex-count must be positive")
    if not LABEL_RE.fullmatch(args.user):
        raise ManifestError("--user is malformed")
    if args.password_env != "CIDR_TUGRAPH_PASSWORD":
        raise ManifestError("TuGraph P10 freezes CIDR_TUGRAPH_PASSWORD as credential source")
    value = {
        "schema_version": STORE_SCHEMA,
        "system_version": args.system_version,
        "interface_scope": INTERFACE_SCOPE,
        "formal_eligible": not args.fixture_only,
        "dataset": {"path": str(dataset), "sha256": dataset_sha},
        "truth": {"sha256": truth_sha, "query_count": 1700},
        "database": {
            "path": str(store),
            "graph": args.graph,
            "read_only": True,
            "durable": False,
            "create_if_not_exist": False,
        },
        "authentication": {
            "user": args.user,
            "password_env": args.password_env,
            "password_sha256": sha(args.password_sha256, "--password-sha256"),
        },
        "vertex_id_contract": {
            "kind": VERTEX_ID_CONTRACT,
            "contiguous_from_zero": True,
            "vertex_count": args.vertex_count,
        },
        "edge_type_labels": labels,
        "store_lineage": {
            "hash_method": lineage["hash_method"],
            "sha256": lineage["store_sha256"],
            "file_count": lineage["file_count"],
            "total_bytes": lineage["total_bytes"],
        },
        "import_provenance": {
            "importer": file_ref(args.importer, executable=True),
            "config": file_ref(args.import_config),
            "source_revision": args.source_revision,
        },
    }
    atomic_json(args.output.resolve(), value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--store-lineage", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--dataset-sha256", required=True)
    parser.add_argument("--truth", required=True, type=Path)
    parser.add_argument("--truth-sha256", required=True)
    parser.add_argument("--edge-labels", required=True, type=Path)
    parser.add_argument("--importer", required=True, type=Path)
    parser.add_argument("--import-config", required=True, type=Path)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--vertex-count", required=True, type=int)
    parser.add_argument("--graph", default="default")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password-env", default="CIDR_TUGRAPH_PASSWORD")
    parser.add_argument("--password-sha256", required=True)
    parser.add_argument("--system-version", default="TuGraph-4.5.2-native-embedded")
    parser.add_argument("--fixture-only", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    try:
        run(parse_args())
        return 0
    except (ManifestError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
