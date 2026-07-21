#!/usr/bin/env python3
"""Build a frozen NebulaGraph store manifest for P10."""

from __future__ import annotations

import argparse
import csv
import hashlib
import sys
from pathlib import Path

from nebula_adapter import STORE_SCHEMA, atomic_json, sha256_file, tree_identity


def file_ref(path: Path | None) -> dict[str, str] | None:
    if path is None:
        return None
    resolved = path.resolve()
    if not resolved.is_file():
        raise ValueError(f"file does not exist: {resolved}")
    return {"path": str(resolved), "sha256": sha256_file(resolved)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=("fixture", "formal"))
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--truth", required=True, type=Path)
    parser.add_argument("--space", required=True)
    parser.add_argument("--container-prefix", required=True)
    parser.add_argument("--graph-host", default="127.0.0.1")
    parser.add_argument("--graph-port", type=int, default=0)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password-env", default="CIDR_NEBULA_PASSWORD")
    parser.add_argument("--password-sha256", required=True)
    parser.add_argument("--import-source", type=Path)
    parser.add_argument("--import-result", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = args.data_root.resolve()
    dataset = args.dataset.resolve()
    truth = args.truth.resolve()
    if not root.is_dir() or not dataset.is_file() or not truth.is_file():
        parser.error("data root, dataset, and truth must exist")
    if args.mode == "formal" and args.graph_port <= 0:
        parser.error("formal mode requires --graph-port > 0")
    if args.mode == "formal" and (args.import_source is None or args.import_result is None):
        parser.error("formal mode requires import source and result lineage")
    if len(args.password_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in args.password_sha256):
        parser.error("--password-sha256 must be lowercase SHA-256")
    labels: dict[int, str] = {}
    with truth.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        rows = list(reader)
    for row in rows:
        edge_type = int(row["edge_type"])
        labels[edge_type] = f"E_P{edge_type}" if edge_type > 0 else f"E_N{abs(edge_type)}"
    prefix = args.container_prefix
    manifest = {
        "schema_version": STORE_SCHEMA,
        "system_version": "NebulaGraph 3.8.0",
        "formal_eligible": args.mode == "formal",
        "lineage_stage": "pre-service-start-frozen-copy-v1" if args.mode == "formal" else "empty-fixture-root-v1",
        "data_root": {"path": str(root), **tree_identity(root)},
        "dataset": {"path": str(dataset), "sha256": sha256_file(dataset)},
        "truth": {"path": str(truth), "sha256": sha256_file(truth), "query_count": len(rows)},
        "space": args.space,
        "graph_endpoint": {"host": args.graph_host, "port": args.graph_port},
        "authentication": {
            "user": args.user,
            "password_env": args.password_env,
            "password_sha256": args.password_sha256,
        },
        "edge_type_labels": [
            {"edge_type": edge_type, "label": labels[edge_type]} for edge_type in sorted(labels)
        ],
        "containers": {
            "metad": f"{prefix}-meta",
            "storaged": f"{prefix}-storage",
            "graphd": f"{prefix}-graph",
        },
        "network": f"{prefix}-net",
        "import_provenance": {
            "kind": "historical-sf10-store-copy-v1" if args.mode == "formal" else "tiny-fixture-import-v1",
            "source": file_ref(args.import_source),
            "result": file_ref(args.import_result),
        },
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(output, manifest)
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
