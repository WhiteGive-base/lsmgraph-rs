#!/usr/bin/env python3
"""Generate and attest the P20 property candidate pool with exact commands."""

from __future__ import print_function

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import build_pristine_inventory as inventory
import build_workload_truth as truth_builder


GATE = "p20-property-candidate-generation-v1"
CANDIDATE_POOL_REQUESTED = 300000
PROPERTY_ID = 5
U64_MAX = (1 << 64) - 1


class GenerationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise GenerationError(message)


def absolute_existing_directory(path, label):
    path = truth_builder.reject_symlink_components(Path(path), label)
    info = os.lstat(str(path))
    if not stat.S_ISDIR(info.st_mode):
        raise GenerationError("{} must be a directory".format(label))
    return path.resolve()


def output_root_absent(path):
    path = Path(path)
    if not path.is_absolute():
        raise GenerationError("output root must be absolute")
    path = Path(os.path.abspath(str(path)))
    if os.path.lexists(str(path)):
        raise GenerationError("output root must be absent")
    parent = truth_builder.reject_symlink_components(path.parent, "output parent")
    if not parent.is_dir():
        raise GenerationError("output parent must be a directory")
    return path


def write_bytes_new(path, raw):
    path = Path(path)
    descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path):
    return truth_builder.read_stable_file(path, "generated artifact")[2]


def parse_json_bytes(raw, label):
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=truth_builder.unique_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError, truth_builder.TruthError) as exc:
        raise GenerationError("{} is not strict JSON: {}".format(label, exc))
    if not isinstance(value, dict):
        raise GenerationError("{} must be a JSON object".format(label))
    return value


def verify_binary(path, expected_sha):
    path, actual = truth_builder.verify_file(path, expected_sha, "binary")
    if os.name != "nt" and not os.access(str(path), os.X_OK):
        raise GenerationError("binary must be executable")
    return path, actual


def store_identity(store, scale, dataset_sha, binary_sha, formal_manifest):
    if formal_manifest is not None:
        return inventory.verify_content(store, formal_manifest)
    return inventory.scan_full_manifest(store, scale, dataset_sha, binary_sha)


def exact_commands(binary, store, candidate_plan):
    plan_command = [
        str(binary), "storage-bench",
        "--data-dir", str(store),
        "--samples", str(CANDIDATE_POOL_REQUESTED),
        "--warmup-runs", "0",
        "--repeats", "1",
        "--sample-plan-degree-hint",
        "--sample-plan-out", str(candidate_plan),
    ]
    result_command = [
        str(binary), "storage-bench",
        "--data-dir", str(store),
        "--sample-plan-in", str(candidate_plan),
        "--warmup-runs", "0",
        "--repeats", "1",
        "--workload-mode", "one-hop",
        "--property-predicate-mode", "presence",
        "--property-id", str(PROPERTY_ID),
        "--emit-result-digests",
    ]
    return plan_command, result_command


def validate_u64(value, label):
    require(
        isinstance(value, int) and not isinstance(value, bool)
        and 0 <= value <= U64_MAX,
        "{} must be a u64".format(label),
    )
    return value


def validate_generated_candidate_plan(plan):
    """Validate that scan sampling covered all available sources up to the cap."""
    expected_top = {
        "version": 1,
        "source": "scan",
        "samples_per_edge_type": CANDIDATE_POOL_REQUESTED,
        "semantic_degree_hint": True,
        "force_signature": False,
        "src_label": None,
        "dst_label": None,
    }
    for field, expected in expected_top.items():
        require(plan.get(field) == expected,
                "generated candidate plan {} drift".format(field))
    entries = plan.get("entries")
    require(isinstance(entries, list) and len(entries) == 1,
            "generated candidate plan must contain exactly one entry")
    entry = entries[0]
    require(isinstance(entry, dict), "generated candidate plan entry must be an object")
    for field in ("edge_type", "src_label", "dst_label"):
        require(entry.get(field) is None,
                "generated candidate plan entry {} must be null".format(field))
    candidate_edges = validate_u64(
        entry.get("candidate_edges_for_sampling"), "candidate edge count"
    )
    candidate_sources = validate_u64(
        entry.get("candidate_sources_for_sampling"), "candidate source count"
    )
    require(candidate_edges >= candidate_sources,
            "candidate edge count cannot be smaller than candidate source count")
    samples = entry.get("samples")
    require(isinstance(samples, list), "generated candidate plan samples must be a list")
    expected_samples = min(CANDIDATE_POOL_REQUESTED, candidate_sources)
    require(len(samples) == expected_samples,
            "generated candidate plan did not cover the available source population")
    require(expected_samples > 0, "generated candidate plan has no available source")
    seen = set()
    for index, sample in enumerate(samples):
        require(isinstance(sample, dict),
                "generated candidate sample {} must be an object".format(index))
        src = validate_u64(sample.get("src"), "generated candidate sample src")
        degree = validate_u64(sample.get("degree"), "generated candidate sample degree")
        require(degree > 0, "generated candidate sample degree must be positive")
        require(src not in seen,
                "generated candidate plan contains duplicate source {}".format(src))
        seen.add(src)
    require(len(seen) == expected_samples,
            "generated candidate plan unique-source coverage drift")
    return entry, samples


def validate_plan_generation_output(output, plan, store, snapshot, candidate_plan):
    """Bind storage-bench's scan evidence to the plan it wrote."""
    expected_top = {
        "snapshot": snapshot,
        "edge_type": None,
        "edge_types": [],
        "src_label": None,
        "dst_label": None,
        "semantic_degree_hint": False,
        "force_signature": False,
        "emit_result_digests": False,
        "sample_plan_degree_hint": True,
        "workload_mode": "one_hop",
        "property_predicate_mode": "none",
        "property_id": 0,
        "warmup_runs": 0,
        "repeats": 1,
        "sample_plan_in": None,
        "sample_plan_version": 1,
        "scan_requested": True,
    }
    for field, expected in expected_top.items():
        require(output.get(field) == expected,
                "candidate plan generation output {} drift".format(field))
    raw_store = output.get("data_dir")
    require(isinstance(raw_store, str) and raw_store,
            "candidate plan generation output data_dir drift")
    require(Path(raw_store).resolve() == store,
            "candidate plan generation store drift")
    raw_output = output.get("sample_plan_out")
    require(isinstance(raw_output, str) and raw_output,
            "candidate plan generation sample_plan_out drift")
    require(Path(raw_output).is_absolute(),
            "candidate plan generation sample_plan_out must be absolute")
    require(Path(raw_output).resolve() == candidate_plan,
            "candidate plan generation sample_plan_out drift")

    entry, samples = validate_generated_candidate_plan(plan)
    benchmarks = output.get("benchmarks")
    require(isinstance(benchmarks, list) and len(benchmarks) == 1,
            "candidate plan generation must contain exactly one benchmark")
    benchmark = benchmarks[0]
    require(isinstance(benchmark, dict),
            "candidate plan generation benchmark must be an object")
    expected_benchmark = {
        "edge_type": None,
        "src_label": None,
        "dst_label": None,
        "candidate_edges_for_sampling": entry["candidate_edges_for_sampling"],
        "candidate_sources_for_sampling": entry["candidate_sources_for_sampling"],
        "semantic_degree_hint": False,
        "force_signature": False,
        "workload_mode": "one_hop",
        "property_predicate_mode": "none",
        "property_id": 0,
        "sampled_vertices": len(samples),
        "sampled_srcs": [sample["src"] for sample in samples],
        "sample_degrees": samples,
        "warmup_runs": 0,
        "repeats": 1,
        "emit_result_digests": False,
    }
    for field, expected in expected_benchmark.items():
        require(benchmark.get(field) == expected,
                "candidate plan generation benchmark {} drift".format(field))
    return entry, samples


def run_command(command, environment):
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
    )


def load_formal_inventory(args, store, binary_sha, dataset_sha):
    if args.scale == "sf1":
        if args.inventory_manifest is not None or args.inventory_manifest_sha256 is not None:
            raise GenerationError("SF1 calibration must not claim a formal pristine inventory")
        return None, None, None
    if args.inventory_manifest is None or args.inventory_manifest_sha256 is None:
        raise GenerationError("SF10 requires a pristine inventory manifest and SHA-256")
    path, manifest_sha, manifest, _ = truth_builder.load_verified_json(
        args.inventory_manifest,
        args.inventory_manifest_sha256,
        "pristine inventory manifest",
    )
    inventory.validate_manifest(manifest, store, "sf10", dataset_sha, binary_sha)
    return path, manifest_sha, manifest


def generate(args):
    require(args.property_id == PROPERTY_ID, "property_id must be exactly 5")
    require(args.snapshot > 0, "snapshot must be positive")
    dataset_sha = truth_builder.normalize_sha(args.dataset_sha256, "dataset SHA-256")
    root = output_root_absent(args.output_root)
    store = absolute_existing_directory(args.store, "store")
    binary, binary_sha = verify_binary(args.binary, args.binary_sha256)
    typed_plan, typed_sha = truth_builder.verify_file(
        args.typed_plan, args.typed_plan_sha256, "typed plan"
    )
    inventory_path, inventory_sha, formal_manifest = load_formal_inventory(
        args, store, binary_sha, dataset_sha
    )

    root.mkdir()
    candidate_plan = root / "candidate-plan.json"
    plan_generation_output = root / "candidate-plan-generation.json"
    plan_generation_stderr = root / "candidate-plan-generation.stderr"
    candidate_result = root / "candidate-result.json"
    candidate_result_stderr = root / "candidate-result.stderr"
    receipt_path = root / "generation-receipt.json"

    before = store_identity(store, args.scale, dataset_sha, binary_sha, formal_manifest)
    plan_command, result_command = exact_commands(binary, store, candidate_plan)
    environment = os.environ.copy()
    environment.pop("SNB_SKIP_SEM_INDEX", None)
    environment.pop("SNB_SKIP_ADJ_CACHE", None)

    plan_run = run_command(plan_command, environment)
    write_bytes_new(plan_generation_output, plan_run.stdout)
    write_bytes_new(plan_generation_stderr, plan_run.stderr)
    require(plan_run.returncode == 0, "candidate plan generation exited {}".format(plan_run.returncode))
    require(candidate_plan.is_file() and not candidate_plan.is_symlink(),
            "storage-bench did not create a regular candidate plan")
    plan_doc = truth_builder.load_json(candidate_plan, "generated candidate plan")
    plan_generation_doc = parse_json_bytes(plan_run.stdout, "candidate plan generation output")
    validate_plan_generation_output(
        plan_generation_doc, plan_doc, store, args.snapshot, candidate_plan
    )

    result_run = run_command(result_command, environment)
    write_bytes_new(candidate_result, result_run.stdout)
    write_bytes_new(candidate_result_stderr, result_run.stderr)
    require(result_run.returncode == 0, "candidate result command exited {}".format(result_run.returncode))
    result_doc = parse_json_bytes(result_run.stdout, "candidate result")
    require(Path(result_doc.get("data_dir", "")).resolve() == store,
            "candidate result store drift")
    require(result_doc.get("snapshot") == args.snapshot,
            "candidate result snapshot drift")
    require(Path(result_doc.get("sample_plan_in", "")).resolve() == candidate_plan,
            "candidate result sample-plan binding drift")

    after = store_identity(store, args.scale, dataset_sha, binary_sha, formal_manifest)
    require(before["files"] == after["files"], "full store content changed during generation")
    require(before["inventory_sha256"] == after["inventory_sha256"],
            "store inventory SHA changed during generation")
    verify_binary(binary, binary_sha)
    truth_builder.verify_file(typed_plan, typed_sha, "typed plan")
    if formal_manifest is not None:
        _, live_manifest_sha = truth_builder.verify_file(
            inventory_path, inventory_sha, "pristine inventory manifest"
        )
        require(live_manifest_sha == inventory_sha, "inventory manifest changed during generation")

    candidate_plan_sha = sha256_file(candidate_plan)
    candidate_result_sha = sha256_file(candidate_result)
    formal_input = formal_manifest is not None
    receipt = {
        "schema_version": 1,
        "state": "PASS",
        "gate": GATE,
        "scale": args.scale,
        "property_id": PROPERTY_ID,
        "candidate_pool_requested": CANDIDATE_POOL_REQUESTED,
        "sample_plan_role": "candidate_pool",
        "performance_eligible": False,
        "formal_plan_eligible": False,
        "downstream_input_eligible": formal_input,
        "binary": {"path": str(binary), "sha256": binary_sha},
        "typed_plan": {"path": str(typed_plan), "sha256": typed_sha},
        "store": {
            "path": str(store),
            "snapshot": args.snapshot,
            "inventory_sha256_before": before["inventory_sha256"],
            "inventory_sha256_after": after["inventory_sha256"],
            "file_count": after["file_count"],
            "total_bytes": after["total_bytes"],
            "full_content_verified_before": True,
            "full_content_verified_after": True,
        },
        "dataset_sha256": dataset_sha,
        "pristine_inventory": None if formal_manifest is None else {
            "path": str(inventory_path),
            "sha256": inventory_sha,
            "inventory_sha256": formal_manifest["inventory_sha256"],
            "store_path": formal_manifest["store_path"],
            "scale": formal_manifest["scale"],
            "dataset_sha256": formal_manifest["dataset_sha256"],
            "binary_sha256": formal_manifest["binary_sha256"],
        },
        "commands": {
            "candidate_plan": {"argv": plan_command, "exit_code": plan_run.returncode},
            "candidate_result": {"argv": result_command, "exit_code": result_run.returncode},
            "forbidden_environment": {
                "SNB_SKIP_SEM_INDEX": "absent",
                "SNB_SKIP_ADJ_CACHE": "absent",
            },
        },
        "artifacts": {
            "candidate_plan": {
                "path": str(candidate_plan), "sha256": candidate_plan_sha,
            },
            "candidate_plan_generation": {
                "path": str(plan_generation_output),
                "sha256": sha256_file(plan_generation_output),
            },
            "candidate_plan_stderr": {
                "path": str(plan_generation_stderr),
                "sha256": sha256_file(plan_generation_stderr),
            },
            "candidate_result": {
                "path": str(candidate_result), "sha256": candidate_result_sha,
            },
            "candidate_result_stderr": {
                "path": str(candidate_result_stderr),
                "sha256": sha256_file(candidate_result_stderr),
            },
        },
    }
    inventory.write_json_new(receipt_path, receipt)
    return receipt_path, sha256_file(receipt_path), receipt


def write_failure(root, error):
    if root is None or not root.is_dir():
        return
    failure = root / "FAILED.json"
    if failure.exists():
        return
    try:
        inventory.write_json_new(failure, {
            "schema_version": 1,
            "state": "FAIL",
            "gate": GATE,
            "performance_eligible": False,
            "formal_plan_eligible": False,
            "downstream_input_eligible": False,
            "error": str(error),
        })
    except OSError:
        pass


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--scale", required=True, choices=("sf1", "sf10"))
    value.add_argument("--property-id", required=True, type=int)
    value.add_argument("--binary", required=True, type=Path)
    value.add_argument("--binary-sha256", required=True)
    value.add_argument("--store", required=True, type=Path)
    value.add_argument("--snapshot", required=True, type=int)
    value.add_argument("--dataset-sha256", required=True)
    value.add_argument("--typed-plan", required=True, type=Path)
    value.add_argument("--typed-plan-sha256", required=True)
    value.add_argument("--inventory-manifest", type=Path)
    value.add_argument("--inventory-manifest-sha256")
    value.add_argument("--output-root", required=True, type=Path)
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    root = None
    try:
        root = output_root_absent(args.output_root)
        receipt_path, receipt_sha, receipt = generate(args)
    except (
        GenerationError, inventory.InventoryError, truth_builder.TruthError,
        OSError, ValueError,
    ) as exc:
        write_failure(root, exc)
        print("FAIL: {}".format(exc), file=sys.stderr)
        return 2
    print(json.dumps({
        "state": "PASS",
        "receipt": str(receipt_path),
        "receipt_sha256": receipt_sha,
        "downstream_input_eligible": receipt["downstream_input_eligible"],
        "performance_eligible": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
