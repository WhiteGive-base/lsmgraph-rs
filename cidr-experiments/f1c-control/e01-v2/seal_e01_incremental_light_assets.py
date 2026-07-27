#!/usr/bin/env python3
"""Seal only the small/immediate E01 incremental assets.

This command hashes the SemL0 adapter, the 8 MiB engine binary, and explicitly
listed small lineage files.  It never opens the dense dataset or either store
tree and never grants formal/performance/paper eligibility.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

import build_e01_mixed_lineage as mixed
import inventory_e01_seml0_assets as inventory


ADAPTER_SCHEMA = "cidr-e01-seml0-adapter-artifact-identity-v1"
BINARY_SCHEMA = "cidr-e01-seml0-binary-file-seal-v1"
LINEAGE_SCHEMA = "cidr-e01-shared-dataset-trace-truth-lineage-seal-v1"
MAX_BINARY_BYTES = 64 * 1024 * 1024
FALSE_ELIGIBILITY = {
    "formal_eligible": False,
    "performance_eligible": False,
    "paper_claim_eligible": False,
}


class SealError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SealError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path, label: str) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SealError(f"{label}: cannot read JSON: {exc}") from exc
    require(type(value) is dict, f"{label}: object required")
    return value


def small_ref(path: Path, label: str) -> Dict[str, Any]:
    path = path.resolve()
    require(path.is_file(), f"{label}: file missing")
    require(not path.is_symlink(), f"{label}: symlink forbidden")
    require(path.stat().st_size <= inventory.MAX_SMALL_FILE_BYTES, f"{label}: too large")
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _stat_identity(path: Path) -> Dict[str, Any]:
    value = path.stat()
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "size_bytes": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
        "mode_octal": format(stat.S_IMODE(value.st_mode), "04o"),
        "uid": value.st_uid,
        "gid": value.st_gid,
    }


def _git_state(repo_root: Path) -> Dict[str, Any]:
    repo_root = repo_root.resolve()
    head = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status_lines = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain=v1"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    require(len(head) == 40, "git HEAD invalid")
    require(not status_lines, "repo must be clean when adapter identity is captured")
    return {
        "root": str(repo_root),
        "head": head,
        "clean_at_capture": True,
        "status_lines": [],
    }


def adapter_identity(
    adapter_path: Path,
    repo_root: Path,
    mixed_plan_path: Path,
    captured_at_utc: str,
) -> Dict[str, Any]:
    adapter_path = adapter_path.resolve()
    repo_root = repo_root.resolve()
    try:
        relative = adapter_path.relative_to(repo_root)
    except ValueError as exc:
        raise SealError("adapter must be inside repo root") from exc
    variants = inventory.parse_adapter_variants(adapter_path)
    for name, expected in inventory.EXPECTED_VARIANTS.items():
        require(variants.get(name) == expected, f"adapter variant mapping drift: {name}")
    mode = stat.S_IMODE(adapter_path.stat().st_mode)
    return {
        "schema_version": ADAPTER_SCHEMA,
        "state": "PASS",
        "captured_at_utc": captured_at_utc,
        "scope": "seml0-current-bridge-canary-plus-naive-r1-r3",
        "mixed_lineage_plan": small_ref(mixed_plan_path, "mixed-lineage plan"),
        "repo": _git_state(repo_root),
        "adapter": small_ref(adapter_path, "SemL0 adapter"),
        "repo_relative_path": relative.as_posix(),
        "adapter_kind": "python-script",
        "variant_bindings": {
            key: {
                "l0_layout": value[0],
                "semantic_degree_hint": value[1],
            }
            for key, value in inventory.EXPECTED_VARIANTS.items()
        },
        "path_group_or_world_writable": bool(mode & (stat.S_IWGRP | stat.S_IWOTH)),
        "revalidate_path_size_sha_before_each_cell": True,
        "fresh_identity_captured": True,
        "production_ready": False,
        "production_blocker": "fresh admission/resource gates and store seals absent",
        **FALSE_ELIGIBILITY,
    }


def binary_file_seal(
    binary_path: Path,
    asset_inventory_path: Path,
    captured_at_utc: str,
) -> Dict[str, Any]:
    binary_path = binary_path.resolve()
    asset_inventory_ref = small_ref(asset_inventory_path, "asset inventory")
    asset_inventory = load_json(asset_inventory_path, "asset inventory")
    require(
        asset_inventory.get("schema_version") == inventory.SCHEMA,
        "asset inventory schema drift",
    )
    expected = asset_inventory.get("engine_binary", {}).get("candidate_sha256")
    require(type(expected) is str and mixed.SHA256_RE.fullmatch(expected), "candidate SHA missing")
    require(binary_path.is_file() and not binary_path.is_symlink(), "binary file invalid")
    require(binary_path.stat().st_size <= MAX_BINARY_BYTES, "binary exceeds light seal limit")
    before = _stat_identity(binary_path)
    digest = sha256_file(binary_path)
    after = _stat_identity(binary_path)
    require(before == after, "binary identity changed while hashing")
    require(digest == expected, "binary SHA drift from legacy candidate")
    mode = int(before["mode_octal"], 8)
    if os.name == "posix":
        require(
            not (mode & (stat.S_IWGRP | stat.S_IWOTH)),
            "binary is group/world writable",
        )
    return {
        "schema_version": BINARY_SCHEMA,
        "state": "PASS",
        "captured_at_utc": captured_at_utc,
        "scope": "same-engine-binary-for-budg-b64-and-naive",
        "asset_inventory": asset_inventory_ref,
        "binary": {
            "path": str(binary_path),
            "sha256": digest,
            "size_bytes": before["size_bytes"],
        },
        "identity_before_and_after": before,
        "content_hashed_now": True,
        "bytes_read_now": before["size_bytes"],
        "max_light_file_bytes": MAX_BINARY_BYTES,
        "variants": ["budg-b64", "naive"],
        "revalidate_identity_before_each_cell": True,
        "fresh_file_seal": True,
        "production_ready": False,
        "production_blocker": "fresh store seals and admission/resource gates absent",
        **FALSE_ELIGIBILITY,
    }


def lineage_seal(
    mixed_plan_path: Path,
    dataset_manifest_path: Path,
    dense_summary_path: Path,
    shared_plan_path: Path,
    preflight_path: Path,
    truth_path: Path,
    id_map_manifest_path: Path,
    captured_at_utc: str,
) -> Dict[str, Any]:
    plan_ref = small_ref(mixed_plan_path, "mixed-lineage plan")
    plan = load_json(mixed_plan_path, "mixed-lineage plan")
    require(plan.get("schema_version") == mixed.OUTPUT_SCHEMA, "mixed plan schema drift")
    logical = plan["logical_dataset_identity"]
    dataset_ref = small_ref(dataset_manifest_path, "dataset manifest")
    dataset = load_json(dataset_manifest_path, "dataset manifest")
    dense_ref = small_ref(dense_summary_path, "dense conversion summary")
    dense = load_json(dense_summary_path, "dense conversion summary")
    shared_ref = small_ref(shared_plan_path, "shared sample plan")
    shared = load_json(shared_plan_path, "shared sample plan")
    preflight_ref = small_ref(preflight_path, "shared plan preflight")
    preflight = load_json(preflight_path, "shared plan preflight")
    truth_ref = small_ref(truth_path, "shared truth TSV")
    id_map_ref = small_ref(id_map_manifest_path, "id-map manifest")
    id_map = load_json(id_map_manifest_path, "id-map manifest")
    require(dataset.get("dataset_sha256") == "baa7c4b9701936253ebaeb4b3436c16403373af0b48c4276dacef780479952c5", "source dataset SHA drift")
    require(
        dense.get("vertex_count") == logical["vertex_count"]
        and dense.get("edge_count") == logical["directed_edge_count"],
        "dense cardinality drift",
    )
    dense_representation = next(
        item
        for item in logical["physical_representations"]
        if item["kind"] == "dense_typed_edge_list"
    )
    require(
        truth_ref["sha256"] == logical["truth_sha256"],
        "truth SHA drift",
    )
    entries = shared.get("entries")
    require(type(entries) is list and entries, "shared sample plan entries required")
    query_count = sum(
        len(entry.get("samples", [])) for entry in entries if type(entry) is dict
    )
    require(query_count == logical["query_count"] == 1700, "shared plan query count drift")
    require(preflight.get("truth_rows") == 1700, "preflight truth row drift")
    verification = preflight.get("verification")
    require(
        type(verification) is dict
        and verification.get("status") == "PASS"
        and verification.get("checked") == 1700
        and verification.get("mismatches") == 0,
        "shared plan preflight correctness drift",
    )
    require(
        Path(preflight.get("truth_tsv", "")).resolve() == truth_path.resolve(),
        "preflight truth path drift",
    )
    require(
        Path(preflight.get("id_map_dir", "")).resolve()
        == id_map_manifest_path.resolve().parent,
        "preflight id-map directory drift",
    )
    require(
        id_map.get("vertex_count") == logical["vertex_count"],
        "id-map vertex count drift",
    )
    return {
        "schema_version": LINEAGE_SCHEMA,
        "state": "PASS",
        "captured_at_utc": captured_at_utc,
        "scope": "logical-dataset-shared-plan-truth-id-map-lineage-only",
        "mixed_lineage_plan": plan_ref,
        "logical_dataset_id": logical["logical_dataset_id"],
        "logical_dataset": {
            "scale_factor": logical["scale_factor"],
            "vertex_count": logical["vertex_count"],
            "directed_edge_count": logical["directed_edge_count"],
            "source_tree_sha256": dataset["dataset_sha256"],
            "dense_edge_list_sha256": dense_representation["sha256"],
            "dataset_manifest": dataset_ref,
            "dense_conversion_summary": dense_ref,
        },
        "query_trace": {
            "workload_id": "sf10-shared-truth-plan-s50-v1",
            "query_count": query_count,
            "sha256": shared_ref["sha256"],
            "receipt": shared_ref,
        },
        "truth": {
            "sha256": truth_ref["sha256"],
            "query_count": 1700,
            "receipt": truth_ref,
            "expected_sequence_digest_sha256": logical["expected_digest_sha256"],
        },
        "id_map": {
            "manifest": id_map_ref,
            "manifest_sha256": id_map_ref["sha256"],
            "mapping_hash": id_map.get("mapping_hash"),
            "vertex_count": id_map.get("vertex_count"),
        },
        "preflight": preflight_ref,
        "checks": {
            "query_count_1700": "PASS",
            "truth_sha_matches_mixed_plan": "PASS",
            "cardinality_matches_mixed_plan": "PASS",
            "preflight_zero_mismatch": "PASS",
            "id_map_manifest_small_receipt": "PASS",
            "dense_dataset_content_rehash": "NOT_PERFORMED",
            "source_tree_content_rehash": "NOT_PERFORMED",
            "id_map_tree_content_rehash": "NOT_PERFORMED",
        },
        "large_content_rehashed_now": False,
        "lineage_only": True,
        "physical_byte_equivalence_claimed": False,
        "production_ready": False,
        "production_blocker": "fresh store seals and admission/resource gates absent",
        **FALSE_ELIGIBILITY,
    }


def write_bundle(
    output_dir: Path,
    *,
    adapter: Mapping[str, Any],
    binary: Mapping[str, Any],
    lineage: Mapping[str, Any],
) -> None:
    output_dir = output_dir.resolve()
    require(not output_dir.exists(), f"refusing to overwrite output directory: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=".e01-light-seals-", dir=str(output_dir.parent))
    )
    try:
        for name, value in (
            ("adapter-artifact-identity.json", adapter),
            ("binary-file-seal.json", binary),
            ("dataset-trace-truth-lineage-seal.json", lineage),
        ):
            (temporary / name).write_text(
                json.dumps(value, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        sums = "\n".join(
            f"{sha256_file(temporary / name)}  {name}"
            for name in (
                "adapter-artifact-identity.json",
                "binary-file-seal.json",
                "dataset-trace-truth-lineage-seal.json",
            )
        ) + "\n"
        (temporary / "SHA256SUMS").write_text(sums, encoding="utf-8")
        os.replace(str(temporary), str(output_dir))
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mixed-plan", type=Path, required=True)
    parser.add_argument("--asset-inventory", type=Path, required=True)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--dataset-manifest", type=Path, required=True)
    parser.add_argument("--dense-summary", type=Path, required=True)
    parser.add_argument("--shared-plan", type=Path, required=True)
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--truth", type=Path, required=True)
    parser.add_argument("--id-map-manifest", type=Path, required=True)
    parser.add_argument("--captured-at-utc", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        adapter = adapter_identity(
            args.adapter, args.repo_root, args.mixed_plan, args.captured_at_utc
        )
        binary = binary_file_seal(
            args.binary, args.asset_inventory, args.captured_at_utc
        )
        lineage = lineage_seal(
            args.mixed_plan,
            args.dataset_manifest,
            args.dense_summary,
            args.shared_plan,
            args.preflight,
            args.truth,
            args.id_map_manifest,
            args.captured_at_utc,
        )
        write_bundle(
            args.output_dir, adapter=adapter, binary=binary, lineage=lineage
        )
        print(
            json.dumps(
                {
                    "state": "PASS",
                    "output_dir": str(args.output_dir.resolve()),
                    "binary_bytes_hashed": binary["bytes_read_now"],
                    "large_content_rehashed_now": False,
                    **FALSE_ELIGIBILITY,
                },
                sort_keys=True,
            )
        )
        return 0
    except (SealError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
