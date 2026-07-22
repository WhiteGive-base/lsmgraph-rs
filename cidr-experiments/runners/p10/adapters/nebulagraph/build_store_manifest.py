#!/usr/bin/env python3
"""Freeze an offline NebulaGraph tree into a strict P10 store-v2 manifest."""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
from pathlib import Path

from store_contract import (
    FORMAL_QUERY_COUNT,
    LOGICAL_HOSTS,
    STORE_SCHEMA,
    SYMLINK_POLICY,
    SYSTEM_VERSION,
    ContractError,
    assert_nonoverlapping,
    assert_offline,
    current_git_state,
    exact_keys,
    exact_sha,
    file_ref,
    is_within,
    paths_overlap,
    read_strict_json,
    require,
    tree_identity,
    validate_clone_receipt,
    validate_file_ref,
    validate_identity_names,
    validate_images,
    validate_import_receipt,
    write_json_exclusive,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=("fixture", "formal"))
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--truth", required=True, type=Path)
    parser.add_argument("--space", required=True)
    parser.add_argument("--container-prefix", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--repeat-index", type=int)
    parser.add_argument("--graph-host", default="127.0.0.1")
    parser.add_argument("--graph-port", type=int, default=0)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password-env", default="CIDR_NEBULA_PASSWORD")
    parser.add_argument("--password-sha256", required=True)
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--docker", type=Path, default=Path("/usr/bin/docker"))
    parser.add_argument("--runtime-manifest", type=Path)
    parser.add_argument("--runtime-manifest-sha256")
    parser.add_argument("--raw-dataset-manifest", type=Path)
    parser.add_argument("--raw-dataset-manifest-sha256")
    parser.add_argument("--import-receipt", type=Path)
    parser.add_argument("--import-receipt-sha256")
    parser.add_argument("--clone-receipt", type=Path)
    parser.add_argument("--clone-receipt-sha256")
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def read_labels(truth: Path) -> tuple[dict[int, str], int]:
    try:
        with truth.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            require(
                reader.fieldnames == ["query_index", "edge_type", "src", "count", "sum_hash", "xor_hash"],
                "truth TSV header drift",
            )
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ContractError(f"cannot read truth TSV: {exc}") from exc
    labels: dict[int, str] = {}
    for index, row in enumerate(rows):
        try:
            require(int(row["query_index"]) == index, "truth query indexes must be dense and ordered")
            edge_type = int(row["edge_type"])
        except (TypeError, ValueError) as exc:
            raise ContractError(f"truth row {index} has invalid integer fields") from exc
        labels[edge_type] = f"E_P{edge_type}" if edge_type > 0 else f"E_N{abs(edge_type)}"
    require(bool(rows), "truth TSV is empty")
    return labels, len(rows)


def runtime_images(args: argparse.Namespace) -> tuple[dict[str, str], list[dict[str, str]]]:
    require(args.runtime_manifest is not None and args.runtime_manifest_sha256 is not None,
            "formal freezer requires runtime manifest path and SHA-256")
    reference = {
        "path": str(args.runtime_manifest),
        "sha256": exact_sha(args.runtime_manifest_sha256, "runtime manifest SHA-256"),
    }
    path = validate_file_ref(reference, "runtime manifest", recompute=True)
    value = exact_keys(
        read_strict_json(path, "NebulaGraph runtime manifest"),
        ("schema_version", "system_version", "client", "docker", "images"),
        "NebulaGraph runtime manifest",
    )
    require(value["schema_version"] == "cidr-p10-nebulagraph-runtime-v1",
            "runtime manifest schema drift")
    require(value["system_version"] == SYSTEM_VERSION, "runtime system version drift")
    return reference, validate_images(value["images"], "runtime.images")


def run(args: argparse.Namespace) -> None:
    formal = args.mode == "formal"
    require(args.data_root.is_absolute(), "--data-root must be absolute")
    require(args.output.is_absolute(), "--output must be absolute")
    require(args.data_root.is_dir() and not args.data_root.is_symlink(),
            "data root must be a real directory")
    root = args.data_root.resolve()
    output = args.output.resolve(strict=False)
    require(output.parent.is_dir() and not output.parent.is_symlink(),
            "manifest parent must already be a real directory")
    require(not output.exists() and not output.is_symlink(), f"refusing to overwrite output: {output}")
    assert_nonoverlapping({"data_root": root, "manifest": output})

    dataset_reference = file_ref(args.dataset, "NebulaGraph dense dataset")
    truth_reference = file_ref(args.truth, "NebulaGraph truth")
    dataset = Path(dataset_reference["path"])
    truth = Path(truth_reference["path"])
    labels, query_count = read_labels(truth)
    require(
        isinstance(args.password_sha256, str)
        and len(args.password_sha256) == 64
        and all(character in "0123456789abcdef" for character in args.password_sha256),
        "--password-sha256 must be lowercase SHA-256",
    )
    require(bool(args.space) and args.space.replace("_", "a").isalnum(), "--space is not a safe identifier")
    require(bool(args.container_prefix), "--container-prefix must be non-empty")
    require(args.graph_port >= 0 and args.graph_port <= 65535, "graph port is out of range")

    prefix = args.container_prefix
    containers = {
        "metad": f"{prefix}-meta",
        "storaged": f"{prefix}-storage",
        "graphd": f"{prefix}-graph",
    }
    logical_hosts = (
        dict(LOGICAL_HOSTS)
        if formal
        else {
            "metad": f"{prefix}-meta-host",
            "storaged": f"{prefix}-storage-host",
            "graphd": f"{prefix}-graph-host",
        }
    )
    network = f"{prefix}-net"
    validate_identity_names(containers, logical_hosts, network, formal=formal)

    repo: dict[str, object] | None = None
    runtime_reference: dict[str, str] | None = None
    import_reference: dict[str, str] | None = None
    clone_reference: dict[str, str] | None = None
    offline_checks = 0

    if formal:
        require(args.repo_root is not None, "formal freezer requires --repo-root")
        require(args.run_id is not None and bool(args.run_id), "formal freezer requires --run-id")
        require(args.repeat_index is not None and args.repeat_index >= 1,
                "formal freezer requires --repeat-index >= 1")
        require(args.graph_host in ("127.0.0.1", "localhost"),
                "formal graph endpoint must be localhost")
        require(args.graph_port > 0, "formal freezer requires --graph-port > 0")
        require(query_count == FORMAL_QUERY_COUNT, "formal truth must contain 1700 queries")
        require(len(labels) == 34, "formal truth must cover exactly 34 edge types")
        required_receipts = (
            args.raw_dataset_manifest,
            args.raw_dataset_manifest_sha256,
            args.import_receipt,
            args.import_receipt_sha256,
            args.clone_receipt,
            args.clone_receipt_sha256,
        )
        require(all(value is not None for value in required_receipts),
                "formal freezer requires strict import and clone receipt paths/SHA-256")
        require(args.runtime_manifest is not None and args.runtime_manifest_sha256 is not None,
                "formal freezer requires runtime manifest path/SHA-256")
        for option in (
            args.repo_root,
            args.docker,
            args.runtime_manifest,
            args.raw_dataset_manifest,
            args.import_receipt,
            args.clone_receipt,
        ):
            require(option.is_absolute(), "formal freezer evidence paths must be absolute")
        repo = current_git_state(args.repo_root)
        require(repo["clean"] is True, "formal freezer requires a clean Git worktree")
        require(not is_within(output, Path(repo["root"])),
                "formal store manifest output must be outside the Git worktree")
        require(args.docker.is_file() and not args.docker.is_symlink(),
                "formal freezer Docker client is missing or symlinked")
        docker = args.docker.resolve()
        require(os.access(docker, os.X_OK),
                "formal freezer requires a real executable Docker client")
        runtime_reference, images = runtime_images(args)
        raw_reference = {
            "path": str(args.raw_dataset_manifest),
            "sha256": exact_sha(
                args.raw_dataset_manifest_sha256, "raw dataset manifest SHA-256"
            ),
        }
        raw_path = validate_file_ref(raw_reference, "raw dataset manifest", recompute=True)
        import_reference = {
            "path": str(args.import_receipt),
            "sha256": exact_sha(args.import_receipt_sha256, "import receipt SHA-256"),
        }
        clone_reference = {
            "path": str(args.clone_receipt),
            "sha256": exact_sha(args.clone_receipt_sha256, "clone receipt SHA-256"),
        }
        require(not paths_overlap(root, Path(import_reference["path"])),
                "import receipt must be outside the mutable clone")
        require(not paths_overlap(root, Path(clone_reference["path"])),
                "clone receipt must be outside the mutable clone")
        for evidence_name, evidence_path in (
            ("dataset", dataset),
            ("truth", truth),
            ("runtime manifest", Path(runtime_reference["path"])),
            ("raw dataset manifest", raw_path),
        ):
            require(not paths_overlap(root, evidence_path),
                    f"{evidence_name} must be outside the mutable clone")
        assert_offline((root,), docker=docker)
        offline_checks += 1
        frozen_tree = tree_identity(root)
        _, import_value = validate_import_receipt(
            import_reference,
            repo=repo,
            raw_dataset_manifest_path=raw_path,
            raw_dataset_manifest_sha256=raw_reference["sha256"],
            dataset_path=dataset,
            dataset_sha256=dataset_reference["sha256"],
            truth_path=truth,
            truth_sha256=truth_reference["sha256"],
            runtime_manifest_path=Path(runtime_reference["path"]),
            runtime_manifest_sha256=runtime_reference["sha256"],
            runtime_images=images,
            recompute_artifacts=True,
        )
        validate_clone_receipt(
            clone_reference,
            repo=repo,
            imported_tree=import_value["store"],
            target_path=root,
            target_tree=frozen_tree,
            run_id=args.run_id,
            repeat_index=args.repeat_index,
            recompute_tool=True,
        )
        assert_offline((root,), docker=docker)
        offline_checks += 1
    else:
        forbidden = (
            args.repo_root,
            args.runtime_manifest,
            args.runtime_manifest_sha256,
            args.raw_dataset_manifest,
            args.raw_dataset_manifest_sha256,
            args.import_receipt,
            args.import_receipt_sha256,
            args.clone_receipt,
            args.clone_receipt_sha256,
        )
        require(all(value is None for value in forbidden),
                "fixture freezer must not claim formal repo/runtime/import/clone evidence")
        require(args.run_id is None and args.repeat_index is None,
                "fixture freezer must not claim a formal clone run/repeat")
        require(args.graph_port == 0, "fixture graph port must be zero before Docker allocation")
        frozen_tree = tree_identity(root, allow_empty=True)

    manifest = {
        "schema_version": STORE_SCHEMA,
        "system_version": SYSTEM_VERSION,
        "formal_eligible": formal,
        "performance_eligible": formal,
        "lineage_stage": "offline-cloned-prestart-v2" if formal else "empty-fixture-root-v2",
        "data_root": {"path": str(root), **frozen_tree},
        "dataset": dataset_reference,
        "truth": {**truth_reference, "query_count": query_count},
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
        "containers": containers,
        "logical_hosts": logical_hosts,
        "network": network,
        "repo": repo,
        "runtime_manifest": runtime_reference,
        "import_receipt": import_reference,
        "clone_receipt": clone_reference,
        "snapshot_safety": {
            "offline_check_count": offline_checks,
            "symlink_policy": SYMLINK_POLICY,
            "manifest_outside_store": True,
            "output_noreplace": True,
        },
    }
    write_json_exclusive(output, manifest)
    print(output)


def main() -> int:
    try:
        run(parse_args())
        return 0
    except (ContractError, OSError, subprocess.SubprocessError) as exc:
        print(f"build_store_manifest: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
