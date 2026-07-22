#!/usr/bin/env python3
"""Prepare all P20 truths and A0--A6 correctness-only PASS evidence.

The driver never produces performance-eligible data.  It verifies frozen
binary/dataset/store inputs, derives each truth from an independently supplied
trusted reference output, clones the pristine store for every stage/workload,
runs the canonical ``correctness`` profile, compares every result digest to the
truth, and finally invokes the integrated ``build_correctness_pass.py``.
"""

from __future__ import print_function

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import build_pristine_inventory as inventory
import build_property_stratified_plan as property_plan_builder
import build_workload_truth as truth_builder


CONFIG_SCHEMA = "p20-correctness-prep-config-v2"
OWNERSHIP_MARKER = ".p20-prep-owned"
WORKLOADS = ("typed-one-hop", "degree-stratified", "property-presence")
CANONICAL_STAGES = {
    "sf10": ["A0", "A1", "A2", "A3", "A4", "A5", "A6"],
    "sf30": ["A0", "A2", "A4", "A6"],
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CPUSET_RE = re.compile(r"^[0-9,-]+$")
FORBIDDEN_ENVIRONMENT = {"SNB_SKIP_SEM_INDEX", "SNB_SKIP_ADJ_CACHE"}


class PrepError(ValueError):
    pass


def utc_marker():
    # Timestamps are deliberately kept out of truths and PASS decisions.  This
    # compact UTC marker is used only in failure/progress receipts.
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_sha(value, label):
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise PrepError("{} must be a lowercase SHA-256".format(label))
    return value


def absolute(path, label):
    if not isinstance(path, str) and not isinstance(path, Path):
        raise PrepError("{} path must be a string".format(label))
    value = Path(path)
    if not value.is_absolute():
        raise PrepError("{} must be absolute".format(label))
    value = Path(os.path.abspath(str(value)))
    if value.is_symlink():
        raise PrepError("{} must not be a symlink".format(label))
    return value.resolve()


def file_ref(row, label, executable=False):
    if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
        raise PrepError("{} must contain exactly path and sha256".format(label))
    path = absolute(row["path"], label)
    if not path.is_file():
        raise PrepError("{} is not a file: {}".format(label, path))
    expected = normalize_sha(row["sha256"], label + " sha256")
    actual = sha256_file(path)
    if actual != expected:
        raise PrepError("{} SHA-256 mismatch".format(label))
    if executable and os.name != "nt" and not os.access(str(path), os.X_OK):
        raise PrepError("{} is not executable".format(label))
    return path, actual


def load_config_with_sha(path):
    try:
        resolved, raw, config_sha = truth_builder.read_stable_file(path, "config")
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=truth_builder.unique_object,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, truth_builder.TruthError) as exc:
        raise PrepError("cannot read config: {}".format(exc))
    if not isinstance(value, dict):
        raise PrepError("config must be a JSON object")
    expected_top = {
        "schema_version", "scale", "binary", "dataset", "pristine_store",
        "p20", "runtime", "workloads",
    }
    if set(value) != expected_top:
        raise PrepError("config keys must be exactly {}".format(sorted(expected_top)))
    if value.get("schema_version") != CONFIG_SCHEMA:
        raise PrepError("config schema_version drift")
    if value.get("scale") not in CANONICAL_STAGES:
        raise PrepError("scale must be sf10 or sf30")
    if not isinstance(value.get("workloads"), dict) or set(value["workloads"]) != set(WORKLOADS):
        raise PrepError("config must contain exactly the three canonical workloads")
    return value, resolved, config_sha


def load_config(path):
    return load_config_with_sha(path)[0]


def verify_dataset(row):
    expected = {"path", "sha256", "manifest", "manifest_sha256"}
    if not isinstance(row, dict) or set(row) != expected:
        raise PrepError("dataset must contain exactly {}".format(sorted(expected)))
    path = absolute(row["path"], "dataset")
    if not path.exists():
        raise PrepError("dataset does not exist")
    dataset_sha = normalize_sha(row["sha256"], "dataset sha256")
    manifest_path = absolute(row["manifest"], "dataset manifest")
    if not manifest_path.is_file():
        raise PrepError("dataset manifest is not a file")
    manifest_sha = normalize_sha(row["manifest_sha256"], "dataset manifest sha256")
    if sha256_file(manifest_path) != manifest_sha:
        raise PrepError("dataset manifest SHA-256 mismatch")
    manifest = inventory.load_json(manifest_path, "dataset manifest")
    if manifest.get("schema_version") != "p02b-dataset-manifest-v1":
        raise PrepError("dataset manifest schema drift")
    try:
        bound_root = Path(manifest.get("dataset_root", "")).resolve()
    except (TypeError, OSError):
        raise PrepError("dataset manifest root is invalid")
    if bound_root != path or manifest.get("dataset_sha256") != dataset_sha:
        raise PrepError("dataset manifest path/SHA binding drift")
    if manifest.get("hash_method") != "sha256-tree-v1(relative-path,size,file-sha256)":
        raise PrepError("dataset manifest hash method drift")
    for field in ("file_count", "total_bytes"):
        item = manifest.get(field)
        if not isinstance(item, int) or isinstance(item, bool) or item <= 0:
            raise PrepError("dataset manifest {} is invalid".format(field))
    before = inventory.quick_inventory(path)
    if not before:
        raise PrepError("dataset tree is empty")
    tree_digest = hashlib.sha256()
    for item in sorted(before, key=lambda value: value["path"].encode("utf-8")):
        file_path = path / item["path"]
        before_stat = os.lstat(str(file_path))
        file_sha = inventory.sha256_file(file_path)
        after_stat = os.lstat(str(file_path))
        expected_identity = (
            item["device"], item["inode"], item["size_bytes"],
            item["mtime_ns"], item["ctime_ns"],
        )
        if (
            before_stat.st_dev, before_stat.st_ino, before_stat.st_size,
            before_stat.st_mtime_ns, before_stat.st_ctime_ns,
        ) != expected_identity or (
            after_stat.st_dev, after_stat.st_ino, after_stat.st_size,
            after_stat.st_mtime_ns, after_stat.st_ctime_ns,
        ) != expected_identity:
            raise PrepError("dataset file changed while hashing: {}".format(file_path))
        record = "file\0{}\0{}\0{}\n".format(
            item["path"], item["size_bytes"], file_sha
        ).encode("utf-8")
        tree_digest.update(record)
    after = inventory.quick_inventory(path)
    if before != after:
        raise PrepError("dataset tree changed during verification")
    if tree_digest.hexdigest() != dataset_sha:
        raise PrepError("live dataset tree SHA-256 differs from manifest")
    if len(before) != manifest["file_count"]:
        raise PrepError("live dataset file_count differs from manifest")
    if sum(item["size_bytes"] for item in before) != manifest["total_bytes"]:
        raise PrepError("live dataset total_bytes differs from manifest")
    return path, dataset_sha, manifest_path, manifest_sha


def verify_pristine(row, scale, dataset_sha, binary_sha):
    expected = {"path", "inventory_sha256", "manifest", "manifest_sha256"}
    if not isinstance(row, dict) or set(row) != expected:
        raise PrepError("pristine_store must contain exactly {}".format(sorted(expected)))
    store = absolute(row["path"], "pristine store")
    if store.is_symlink() or not store.is_dir():
        raise PrepError("pristine store must be a non-link directory")
    inventory_sha = normalize_sha(row["inventory_sha256"], "pristine inventory sha256")
    manifest_path = absolute(row["manifest"], "pristine manifest")
    if not manifest_path.is_file():
        raise PrepError("pristine manifest is not a file")
    manifest_sha = normalize_sha(row["manifest_sha256"], "pristine manifest sha256")
    if sha256_file(manifest_path) != manifest_sha:
        raise PrepError("pristine manifest file SHA-256 mismatch")
    manifest = inventory.load_json(manifest_path, "pristine manifest")
    inventory.validate_manifest(manifest, store, scale, dataset_sha, binary_sha)
    if manifest.get("inventory_sha256") != inventory_sha:
        raise PrepError("pristine manifest/invocation inventory SHA drift")
    # One full live-content verification occurs before any clone is created.
    inventory.verify_content(store, manifest)
    return store, inventory_sha, manifest_path, manifest_sha, manifest


def verify_p20(row):
    expected = {"profiles", "profile_validator", "correctness_builder"}
    if not isinstance(row, dict) or set(row) != expected:
        raise PrepError("p20 must contain exactly {}".format(sorted(expected)))
    result = {}
    for name in sorted(expected):
        path, digest = file_ref(row[name], "p20 {}".format(name))
        result[name] = path
        result[name + "_sha256"] = digest
    return result


def validate_cpuset(raw):
    if raw is None:
        return None
    if not isinstance(raw, str) or not CPUSET_RE.fullmatch(raw):
        raise PrepError("runtime cpuset is invalid")
    cpus = []
    for part in raw.split(","):
        if not part:
            raise PrepError("runtime cpuset contains an empty component")
        if "-" in part:
            fields = part.split("-")
            if len(fields) != 2:
                raise PrepError("runtime cpuset range is invalid")
            start, finish = (int(item) for item in fields)
            if start > finish:
                raise PrepError("runtime cpuset range descends")
            cpus.extend(range(start, finish + 1))
        else:
            cpus.append(int(part))
    if not cpus or len(cpus) != len(set(cpus)):
        raise PrepError("runtime cpuset is empty or duplicates CPUs")
    return raw


def verify_runtime(row):
    expected = {"cpuset", "worker_threads", "csr_metadata_cache_entries"}
    if not isinstance(row, dict) or set(row) != expected:
        raise PrepError("runtime must contain exactly {}".format(sorted(expected)))
    cpuset = validate_cpuset(row["cpuset"])
    for field in ("worker_threads", "csr_metadata_cache_entries"):
        item = row[field]
        if not isinstance(item, int) or isinstance(item, bool) or item <= 0:
            raise PrepError("runtime {} must be positive".format(field))
    return cpuset, row["worker_threads"], row["csr_metadata_cache_entries"]


def validate_workload_row(name, row):
    expected = {
        "sample_plan", "sample_plan_sha256", "reference_output",
        "reference_output_sha256", "reference_stage", "property_id",
    }
    if name == "property-presence":
        expected.update({
            "expected_queries", "min_positive", "min_mixed", "min_zero",
            "candidate_pool_requested",
            "stratified_plan_receipt", "stratified_plan_receipt_sha256",
        })
    if not isinstance(row, dict) or set(row) != expected:
        raise PrepError("workload {} keys drift".format(name))
    sample_path, sample_sha = truth_builder.verify_file(
        Path(row["sample_plan"]), row["sample_plan_sha256"], "{} sample plan".format(name)
    )
    reference_path, reference_sha = truth_builder.verify_file(
        Path(row["reference_output"]), row["reference_output_sha256"], "{} reference".format(name)
    )
    stage = row["reference_stage"]
    if stage not in truth_builder.STAGES:
        raise PrepError("workload {} reference_stage is invalid".format(name))
    property_id = row["property_id"]
    truth_builder._expected_property(name, property_id)
    result = {
        "sample_plan": sample_path,
        "sample_plan_sha256": sample_sha,
        "reference_output": reference_path,
        "reference_output_sha256": reference_sha,
        "reference_stage": stage,
        "property_id": property_id,
    }
    if name == "property-presence":
        requirements = {
            "expected_queries": row["expected_queries"],
            "min_positive": row["min_positive"],
            "min_mixed": row["min_mixed"],
            "min_zero": row["min_zero"],
        }
        truth_builder.canonical_property_requirements(name, requirements)
        if row["candidate_pool_requested"] != property_plan_builder.CANDIDATE_POOL_REQUESTED:
            raise PrepError("property candidate_pool_requested drift")
        receipt_path, receipt_sha, receipt, _ = truth_builder.load_verified_json(
            Path(row["stratified_plan_receipt"]),
            row["stratified_plan_receipt_sha256"],
            "property stratified-plan receipt",
        )
        result.update({
            "property_requirements": requirements,
            "candidate_pool_requested": row["candidate_pool_requested"],
            "stratified_plan_receipt": receipt_path,
            "stratified_plan_receipt_sha256": receipt_sha,
            "stratified_plan_receipt_doc": receipt,
        })
    return result


def validate_property_plan_receipt(
    row, typed_row, scale, binary, binary_sha, dataset_sha, pristine,
    pristine_sha, pristine_manifest, pristine_manifest_sha,
):
    receipt = row["stratified_plan_receipt_doc"]
    expected_top = {
        "schema_version": 1,
        "state": "PASS",
        "gate": property_plan_builder.PROFILE,
        "scale": scale,
        "property_id": row["property_id"],
        "sampling_design": "balanced_diagnostic",
        "natural_prevalence_claim": False,
        "natural_prevalence_eligible": False,
        "sample_plan_role": "balanced_diagnostic",
        "formal_plan_eligible": True,
        "downstream_input_eligible": True,
        "performance_eligible": False,
        "selection_seed": property_plan_builder.SELECTION_SEED,
        "candidate_pool_requested": row["candidate_pool_requested"],
        "shuffle_algorithm": property_plan_builder.SHUFFLE_ALGORITHM,
        "thresholds": row["property_requirements"],
    }
    for field, expected in expected_top.items():
        if receipt.get(field) != expected:
            raise PrepError("property stratified-plan receipt {} drift".format(field))
    output_plan = receipt.get("output_plan")
    if not isinstance(output_plan, dict):
        raise PrepError("property stratified-plan receipt lacks output_plan")
    try:
        receipt_plan_path = Path(output_plan.get("path", "")).resolve()
    except (TypeError, OSError):
        raise PrepError("property stratified-plan output path is invalid")
    if (
        receipt_plan_path != row["sample_plan"]
        or output_plan.get("sha256") != row["sample_plan_sha256"]
    ):
        raise PrepError("property stratified-plan receipt does not bind sample plan")
    binary_row = receipt.get("binary")
    if not isinstance(binary_row, dict):
        raise PrepError("property stratified-plan receipt lacks binary binding")
    try:
        receipt_binary = Path(binary_row.get("path", "")).resolve()
    except (TypeError, OSError):
        raise PrepError("property stratified-plan binary path is invalid")
    if receipt_binary != binary or binary_row.get("sha256") != binary_sha:
        raise PrepError("property stratified-plan binary binding drift")
    if receipt.get("dataset_sha256") != dataset_sha:
        raise PrepError("property stratified-plan dataset binding drift")
    store_row = receipt.get("store")
    if not isinstance(store_row, dict):
        raise PrepError("property stratified-plan receipt lacks store binding")
    try:
        receipt_store = Path(store_row.get("path", "")).resolve()
    except (TypeError, OSError):
        raise PrepError("property stratified-plan store path is invalid")
    snapshot = store_row.get("snapshot")
    if receipt_store != pristine or not isinstance(snapshot, int) or isinstance(snapshot, bool) or snapshot <= 0:
        raise PrepError("property stratified-plan store/snapshot binding drift")
    if store_row.get("inventory_sha256") != pristine_sha:
        raise PrepError("property stratified-plan store inventory binding drift")
    pristine_binding = receipt.get("pristine_inventory")
    expected_pristine_binding = {
        "path": str(pristine_manifest),
        "sha256": pristine_manifest_sha,
        "inventory_sha256": pristine_sha,
        "store_path": str(pristine),
        "scale": scale,
        "dataset_sha256": dataset_sha,
        "binary_sha256": binary_sha,
    }
    if pristine_binding != expected_pristine_binding:
        raise PrepError("property stratified-plan pristine inventory binding drift")
    generation_row = receipt.get("generation_receipt")
    if not isinstance(generation_row, dict):
        raise PrepError("property stratified-plan lacks generation receipt binding")
    try:
        generation = property_plan_builder.validate_generation_receipt(
            Path(generation_row.get("path", "")), generation_row.get("sha256", "")
        )
    except (
        property_plan_builder.PlanError, inventory.InventoryError,
        truth_builder.TruthError, OSError, ValueError,
    ) as exc:
        raise PrepError("property generation receipt validation failed: {}".format(exc))
    if (
        generation["scale"] != scale
        or generation["binary_path"] != binary
        or generation["binary_sha"] != binary_sha
        or generation["store"] != pristine
        or generation["snapshot"] != snapshot
        or generation["manifest_path"] != pristine_manifest
        or generation["manifest_sha"] != pristine_manifest_sha
    ):
        raise PrepError("property stratified-plan generation receipt binding drift")
    expected_generation_artifacts = {
        "candidate_plan": {
            "path": str(generation["candidate_path"]),
            "sha256": generation["candidate_sha"],
        },
        "candidate_result": {
            "path": str(generation["result_path"]),
            "sha256": generation["result_sha"],
        },
        "typed_plan": {
            "path": str(generation["typed_path"]),
            "sha256": generation["typed_sha"],
        },
    }
    for field, expected in expected_generation_artifacts.items():
        if receipt.get(field) != expected:
            raise PrepError(
                "property stratified-plan {} differs from generation receipt".format(field)
            )
    typed = receipt.get("typed_plan")
    if not isinstance(typed, dict):
        raise PrepError("property stratified-plan receipt lacks typed-plan binding")
    try:
        typed_path = Path(typed.get("path", "")).resolve()
    except (TypeError, OSError):
        raise PrepError("property stratified-plan typed-plan path is invalid")
    if typed_path != typed_row["sample_plan"] or typed.get("sha256") != typed_row["sample_plan_sha256"]:
        raise PrepError("property stratified-plan typed-plan binding drift")
    for field in ("candidate_plan", "candidate_result", "typed_plan"):
        artifact = receipt.get(field)
        if not isinstance(artifact, dict):
            raise PrepError("property stratified-plan receipt lacks {}".format(field))
        truth_builder.verify_file(
            Path(artifact.get("path", "")), artifact.get("sha256", ""),
            "property {}".format(field),
        )
    selected = receipt.get("selected")
    if not isinstance(selected, dict):
        raise PrepError("property stratified-plan receipt lacks selected audit")
    required_selected = {
        "queries": 1000,
        "unique_sources": 1000,
        "positive": 500,
        "mixed": 500,
        "zero": 500,
    }
    for field, expected in required_selected.items():
        if selected.get(field) != expected:
            raise PrepError("property stratified-plan selected {} drift".format(field))
    samples = selected.get("samples")
    if not isinstance(samples, list) or len(samples) != 1000:
        raise PrepError("property stratified-plan selected sample coverage drift")
    if len({item.get("src") for item in samples if isinstance(item, dict)}) != 1000:
        raise PrepError("property stratified-plan selected sources are not unique")
    if selected.get("source_assignment_sha256") != property_plan_builder.canonical_sha(samples):
        raise PrepError("property stratified-plan selected assignment SHA drift")
    degree_classes = selected.get("degree_classes")
    if not isinstance(degree_classes, list) or len(set(degree_classes)) < 2:
        raise PrepError("property stratified-plan degree-class coverage drift")
    _, _, plan, _ = truth_builder.load_verified_json(
        row["sample_plan"], row["sample_plan_sha256"], "property sample plan"
    )
    plan_entries, total, _ = truth_builder.validate_sample_plan(plan)
    if total != 1000 or len(plan_entries) != 1 or plan_entries[0].get("edge_type") is not None:
        raise PrepError("property stratified sample-plan structure drift")
    plan_samples = plan_entries[0]["samples"]
    receipt_samples = [
        {"src": item.get("src"), "degree": item.get("degree")}
        for item in samples if isinstance(item, dict)
    ]
    if receipt_samples != plan_samples:
        raise PrepError("property stratified receipt/sample-plan order drift")


def resolve_profile(p20, scale, stage, workload, property_id):
    for name in ("profiles", "profile_validator"):
        if sha256_file(p20[name]) != p20[name + "_sha256"]:
            raise PrepError("p20 {} changed after input verification".format(name))
    command = [
        sys.executable, str(p20["profile_validator"]),
        "--profiles", str(p20["profiles"]),
        "--resolve", "--scale", scale, "--stage", stage,
        "--mode", "correctness", "--workload", workload,
    ]
    if workload == "property-presence":
        command.extend(["--bind", "PROPERTY_ID={}".format(property_id)])
    completed = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    if completed.returncode != 0:
        raise PrepError("profile resolver failed: {}".format(completed.stderr.strip()))
    try:
        resolved = json.loads(completed.stdout, object_pairs_hook=truth_builder.unique_object)
    except (json.JSONDecodeError, truth_builder.TruthError) as exc:
        raise PrepError("profile resolver output is invalid: {}".format(exc))
    if resolved.get("scale") != scale or resolved.get("stage", {}).get("id") != stage:
        raise PrepError("resolved scale/stage drift")
    if resolved.get("mode", {}).get("name") != "correctness":
        raise PrepError("resolved mode drift")
    if resolved.get("mode", {}).get("performance_eligible") is not False:
        raise PrepError("correctness profile must be performance_eligible=false")
    if resolved.get("mode", {}).get("max_query_streams") != 1:
        raise PrepError("correctness profile must be single-stream")
    if resolved.get("workload", {}).get("name") != workload:
        raise PrepError("resolved workload drift")
    if resolved.get("stage", {}).get("l0_layout") != "semantic-budgeted":
        raise PrepError("resolved stage layout drift")
    if resolved.get("fixed_inputs", {}).get("io_backend") != "blocking":
        raise PrepError("resolved io_backend drift")
    storage_args = resolved.get("storage_bench_args")
    if not isinstance(storage_args, list) or not storage_args or storage_args[0] != "storage-bench":
        raise PrepError("resolved storage-bench argv is invalid")
    if not all(isinstance(item, str) for item in storage_args):
        raise PrepError("resolved storage-bench argv contains a non-string")
    if "--emit-result-digests" not in storage_args:
        raise PrepError("correctness profile must emit result digests")
    runtime_owned = (
        "--data-dir", "--sample-plan-in", "--warmup-runs", "--repeats",
        "--io-backend", "--csr-metadata-cache-entries",
    )
    for token in storage_args:
        if token == "--auto-compact" or token.startswith("--auto-compact="):
            raise PrepError("correctness profile cannot use measured --auto-compact")
        if any(token == option or token.startswith(option + "=") for option in runtime_owned):
            raise PrepError("profile tries to override runtime-owned option {}".format(token))
    return resolved, command


def compare_observation(raw, plan, plan_path, frozen_truth, workload, property_id, stage):
    requirements = None
    if workload == "property-presence":
        requirements = {
            "expected_queries": frozen_truth.get("expected_queries"),
            "min_positive": frozen_truth.get("min_positive"),
            "min_mixed": frozen_truth.get("min_mixed"),
            "min_zero": frozen_truth.get("min_zero"),
        }
    entries, checked, positive, _, distribution = truth_builder.validate_reference(
        raw, plan, plan_path, workload, property_id, stage, requirements,
    )
    if entries != frozen_truth.get("entries"):
        raise PrepError("{} {} current result digests differ from frozen truth".format(stage, workload))
    if checked != frozen_truth.get("expected_queries"):
        raise PrepError("{} {} query coverage differs from frozen truth".format(stage, workload))
    if positive != frozen_truth.get("positive_result_samples"):
        raise PrepError("{} {} positive-result coverage differs from frozen truth".format(stage, workload))
    if workload == "property-presence":
        if distribution["mixed_result_samples"] != frozen_truth.get("mixed_result_samples"):
            raise PrepError("{} {} mixed-result coverage differs from frozen truth".format(stage, workload))
        if distribution["zero_result_samples"] != frozen_truth.get("zero_result_samples"):
            raise PrepError("{} {} zero-result coverage differs from frozen truth".format(stage, workload))
    return checked


def clone_store(source, destination, manifest):
    if os.path.lexists(str(destination)):
        raise PrepError("clone destination must be absent")
    if os.name == "nt":
        shutil.copytree(str(source), str(destination), copy_function=shutil.copy2)
        command = ["python-shutil.copytree", str(source), str(destination)]
    else:
        destination.mkdir()
        command = ["cp", "-a", "--reflink=auto", str(source) + "/.", str(destination)]
        completed = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        if completed.returncode != 0:
            raise PrepError("store clone failed: {}".format(completed.stderr.strip()))
    actual = inventory.quick_inventory(destination)
    actual_sizes = {item["path"]: item["size_bytes"] for item in actual}
    expected_sizes = {item["path"]: item["size_bytes"] for item in manifest["files"]}
    if actual_sizes != expected_sizes:
        raise PrepError("clone file-set/size differs from pristine manifest")
    return command


def safe_cleanup_clone(path, work_root):
    path = Path(os.path.abspath(str(path)))
    work_root = Path(work_root).resolve()
    if (
        path.parent.resolve() != work_root
        or path == work_root
        or path.is_symlink()
        or not path.is_dir()
    ):
        raise PrepError("refusing unsafe clone cleanup: {}".format(path))
    shutil.rmtree(str(path))


def atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(temporary), str(path))


def write_bytes_new(path, raw):
    """Create a small frozen input copy without overwriting any prior path."""
    descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def write_failure(output_root, reason):
    output_root.mkdir(parents=True, exist_ok=True)
    atomic_json(output_root / "P20-PREP-FAILED.json", {
        "schema_version": 1,
        "state": "FAILED",
        "correctness_only": True,
        "performance_eligible": False,
        "failed_at_utc": utc_marker(),
        "reason": str(reason),
    })


def invoke_correctness_builder(p20, output, scale, workload, binary, binary_sha,
                               dataset, dataset_sha, sample, sample_sha, truth,
                               truth_sha, observations):
    if sha256_file(p20["correctness_builder"]) != p20["correctness_builder_sha256"]:
        raise PrepError("p20 correctness_builder changed after input verification")
    command = [
        sys.executable, str(p20["correctness_builder"]),
        "--output", str(output), "--scale", scale, "--workload", workload,
        "--binary", str(binary), "--binary-sha256", binary_sha,
        "--dataset", str(dataset), "--dataset-sha256", dataset_sha,
        "--sample-plan", str(sample), "--sample-plan-sha256", sample_sha,
        "--truth", str(truth), "--truth-sha256", truth_sha,
    ]
    for stage in CANONICAL_STAGES[scale]:
        command.extend(["--observation", "{}={}".format(stage, observations[stage])])
    completed = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    return command, completed


def run(args):
    config_path = absolute(args.config, "config")
    config, config_path, config_sha = load_config_with_sha(config_path)
    output_root = absolute(args.output_root, "output root")
    work_root = absolute(args.work_root, "work root")
    if os.path.lexists(str(output_root)):
        raise PrepError("output root must be absent")
    if not work_root.is_dir() or work_root.is_symlink():
        raise PrepError("work root must be an existing non-link directory")
    if any(work_root.iterdir()):
        raise PrepError("work root must start empty")
    scale = config["scale"]
    binary, binary_sha = file_ref(config["binary"], "binary", executable=True)
    dataset, dataset_sha, dataset_manifest, dataset_manifest_sha = verify_dataset(config["dataset"])
    p20 = verify_p20(config["p20"])
    cpuset, worker_threads, cache_entries = verify_runtime(config["runtime"])
    pristine, pristine_sha, pristine_manifest, pristine_manifest_sha, store_manifest = verify_pristine(
        config["pristine_store"], scale, dataset_sha, binary_sha
    )
    for left, right, label in (
        (pristine, work_root, "pristine/work"),
        (pristine, output_root, "pristine/output"),
        (work_root, output_root, "work/output"),
    ):
        if left == right or left in right.parents or right in left.parents:
            raise PrepError("{} paths overlap".format(label))
    workload_rows = {
        name: validate_workload_row(name, config["workloads"][name]) for name in WORKLOADS
    }
    property_row = workload_rows["property-presence"]
    for other in ("typed-one-hop", "degree-stratified"):
        other_row = workload_rows[other]
        if (
            property_row["sample_plan"] == other_row["sample_plan"]
            or property_row["sample_plan_sha256"] == other_row["sample_plan_sha256"]
        ):
            raise PrepError(
                "property-presence requires an independent sample plan from {}".format(other)
            )
    validate_property_plan_receipt(
        property_row, workload_rows["typed-one-hop"], scale,
        binary, binary_sha, dataset_sha, pristine, pristine_sha,
        pristine_manifest, pristine_manifest_sha,
    )

    output_root.mkdir(parents=True)
    (output_root / OWNERSHIP_MARKER).write_text(
        "created-by=prepare_correctness.py\n", encoding="utf-8"
    )
    (output_root / "truth").mkdir()
    (output_root / "raw").mkdir()
    (output_root / "frozen-inputs").mkdir()
    atomic_json(output_root / "input-receipt.json", {
        "schema_version": 1,
        "state": "VERIFIED",
        "correctness_only": True,
        "performance_eligible": False,
        "config": {"path": str(config_path), "sha256": config_sha},
        "binary": {"path": str(binary), "sha256": binary_sha},
        "dataset": {"path": str(dataset), "sha256": dataset_sha,
                    "manifest": str(dataset_manifest), "manifest_sha256": dataset_manifest_sha},
        "pristine_store": {"path": str(pristine), "inventory_sha256": pristine_sha,
                           "manifest": str(pristine_manifest), "manifest_sha256": pristine_manifest_sha,
                           "full_content_verified": True},
        "p20": {name: str(value) if isinstance(value, Path) else value for name, value in p20.items()},
        "property_stratified_plan_receipt": {
            "path": str(property_row["stratified_plan_receipt"]),
            "sha256": property_row["stratified_plan_receipt_sha256"],
            "expected_queries": property_row["property_requirements"]["expected_queries"],
            "candidate_pool_requested": property_row["candidate_pool_requested"],
            "min_positive": property_row["property_requirements"]["min_positive"],
            "min_mixed": property_row["property_requirements"]["min_mixed"],
            "min_zero": property_row["property_requirements"]["min_zero"],
        },
    })

    truths = {}
    observations_by_workload = {}
    env = os.environ.copy()
    for name in FORBIDDEN_ENVIRONMENT:
        env.pop(name, None)
    env["RAYON_NUM_THREADS"] = str(worker_threads)
    env["TOKIO_WORKER_THREADS"] = str(worker_threads)

    for workload in WORKLOADS:
        row = workload_rows[workload]
        _, sample_raw, live_sample_sha = truth_builder.read_stable_file(
            row["sample_plan"], "{} sample plan".format(workload)
        )
        if live_sample_sha != row["sample_plan_sha256"]:
            raise PrepError("{} sample plan changed after input verification".format(workload))
        _, reference_raw, live_reference_sha = truth_builder.read_stable_file(
            row["reference_output"], "{} reference output".format(workload)
        )
        if live_reference_sha != row["reference_output_sha256"]:
            raise PrepError("{} reference output changed after input verification".format(workload))
        frozen_plan = output_root / "frozen-inputs" / (workload + "-sample-plan.json")
        frozen_reference = output_root / "frozen-inputs" / (workload + "-reference-output.json")
        write_bytes_new(frozen_plan, sample_raw)
        write_bytes_new(frozen_reference, reference_raw)
        truth_path = output_root / "truth" / (workload + ".json")
        built_truth = truth_builder.build_truth(
            scale, workload, row["property_id"], row["sample_plan"],
            row["sample_plan_sha256"], row["reference_output"],
            row["reference_output_sha256"], row["reference_stage"],
            row.get("property_requirements"),
        )
        atomic_json(truth_path, built_truth)
        truths[workload] = (truth_path, sha256_file(truth_path), built_truth)
        _, _, plan, _ = truth_builder.load_verified_json(
            frozen_plan, row["sample_plan_sha256"], "{} frozen sample plan".format(workload)
        )
        observations = {}
        for stage in CANONICAL_STAGES[scale]:
            cell_dir = output_root / "raw" / workload / stage
            cell_dir.mkdir(parents=True)
            clone = work_root / ("{}-{}".format(workload, stage))
            clone_argv = clone_store(pristine, clone, store_manifest)
            resolved, resolver_argv = resolve_profile(
                p20, scale, stage, workload, row["property_id"]
            )
            benchmark_argv = [
                str(binary), "--io-backend", "blocking",
                "--csr-metadata-cache-entries", str(cache_entries),
            ] + list(resolved["storage_bench_args"]) + [
                "--data-dir", str(clone), "--sample-plan-in", str(frozen_plan),
                "--warmup-runs", "0", "--repeats", "1",
            ]
            invoked_argv = list(benchmark_argv)
            if cpuset is not None:
                if shutil.which("taskset") is None:
                    raise PrepError("runtime cpuset requires taskset")
                invoked_argv = ["taskset", "-c", cpuset] + invoked_argv
            atomic_json(cell_dir / "command.json", {
                "schema_version": 1,
                "correctness_only": True,
                "performance_eligible": False,
                "scale": scale,
                "stage": stage,
                "workload": workload,
                "clone_argv": clone_argv,
                "resolver_argv": resolver_argv,
                "benchmark_argv": benchmark_argv,
                "invoked_argv": invoked_argv,
                "environment": {
                    "RAYON_NUM_THREADS": env["RAYON_NUM_THREADS"],
                    "TOKIO_WORKER_THREADS": env["TOKIO_WORKER_THREADS"],
                    "SNB_SKIP_SEM_INDEX": "absent",
                    "SNB_SKIP_ADJ_CACHE": "absent",
                },
                "pristine_inventory_sha256": pristine_sha,
                "sample_plan_sha256": row["sample_plan_sha256"],
                "frozen_reference_output": str(frozen_reference),
                "frozen_reference_output_sha256": row["reference_output_sha256"],
            })
            if sha256_file(frozen_plan) != row["sample_plan_sha256"]:
                raise PrepError("{} frozen sample plan changed before {}".format(workload, stage))
            completed = subprocess.run(
                invoked_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=env,
            )
            if sha256_file(frozen_plan) != row["sample_plan_sha256"]:
                raise PrepError("{} frozen sample plan changed during {}".format(workload, stage))
            (cell_dir / "storage-bench.stdout").write_text(completed.stdout, encoding="utf-8")
            (cell_dir / "storage-bench.stderr").write_text(completed.stderr, encoding="utf-8")
            if completed.returncode != 0:
                raise PrepError("{} {} storage-bench failed with {}".format(stage, workload, completed.returncode))
            try:
                raw = json.loads(completed.stdout, object_pairs_hook=truth_builder.unique_object)
            except (json.JSONDecodeError, truth_builder.TruthError) as exc:
                raise PrepError("{} {} emitted invalid JSON: {}".format(stage, workload, exc))
            if not isinstance(raw, dict):
                raise PrepError("{} {} output is not a JSON object".format(stage, workload))
            compare_observation(
                raw, plan, frozen_plan, built_truth, workload,
                row["property_id"], stage,
            )
            observation_path = cell_dir / "observation.json"
            atomic_json(observation_path, raw)
            observations[stage] = observation_path
            atomic_json(cell_dir / "PASS.json", {
                "schema_version": 1,
                "state": "PASS",
                "correctness_only": True,
                "performance_eligible": False,
                "observation": str(observation_path),
                "observation_sha256": sha256_file(observation_path),
                "truth_sha256": truths[workload][1],
                "checked": built_truth["expected_queries"],
                "mismatches": 0,
            })
            if args.cleanup_clones:
                safe_cleanup_clone(clone, work_root)
        observations_by_workload[workload] = observations
        gate_path = output_root / ("correctness-pass-{}.json".format(workload))
        builder_argv, built = invoke_correctness_builder(
            p20, gate_path, scale, workload, binary, binary_sha, dataset,
            dataset_sha, frozen_plan, row["sample_plan_sha256"],
            truth_path, truths[workload][1], observations,
        )
        workload_dir = output_root / "raw" / workload
        atomic_json(workload_dir / "correctness-builder-command.json", {"argv": builder_argv})
        (workload_dir / "correctness-builder.stdout").write_text(built.stdout, encoding="utf-8")
        (workload_dir / "correctness-builder.stderr").write_text(built.stderr, encoding="utf-8")
        if built.returncode != 0 or not gate_path.is_file():
            raise PrepError("{} correctness PASS builder failed: {}".format(workload, built.stderr.strip()))
        gate = inventory.load_json(gate_path, "{} correctness PASS".format(workload))
        expected_checked = built_truth["expected_queries"] * len(CANONICAL_STAGES[scale])
        if (
            gate.get("schema_version") != 1
            or gate.get("state") != "PASS"
            or gate.get("correctness_only") is not True
            or gate.get("performance_eligible") is not False
            or gate.get("scale") != scale
            or gate.get("workload") != workload
            or gate.get("digest_schema") != "storage-bench-result-digest-v1"
            or gate.get("covered_stages") != CANONICAL_STAGES[scale]
            or gate.get("expected_queries_per_stage") != built_truth["expected_queries"]
            or gate.get("checked") != expected_checked
            or gate.get("mismatches") != 0
            or gate.get("truth_sha256") != truths[workload][1]
            or gate.get("sample_plan_sha256") != row["sample_plan_sha256"]
            or gate.get("binary_sha256") != binary_sha
            or gate.get("dataset_sha256") != dataset_sha
        ):
            raise PrepError("{} correctness PASS output drift".format(workload))

    # A second full pass proves the pristine source stayed frozen throughout
    # all clones and all correctness observations.
    inventory.verify_content(pristine, store_manifest)

    summary = {
        "schema_version": 1,
        "state": "PASS",
        "correctness_only": True,
        "performance_eligible": False,
        "scale": scale,
        "canonical_stages": CANONICAL_STAGES[scale],
        "workloads": {},
        "pristine_inventory_sha256": pristine_sha,
        "binary_sha256": binary_sha,
        "dataset_sha256": dataset_sha,
    }
    for workload in WORKLOADS:
        truth_path, truth_sha, built_truth = truths[workload]
        gate_path = output_root / ("correctness-pass-{}.json".format(workload))
        summary["workloads"][workload] = {
            "sample_plan_sha256": workload_rows[workload]["sample_plan_sha256"],
            "truth": str(truth_path),
            "truth_sha256": truth_sha,
            "correctness_pass": str(gate_path),
            "correctness_pass_sha256": sha256_file(gate_path),
            "expected_queries_per_stage": built_truth["expected_queries"],
            "checked": built_truth["expected_queries"] * len(CANONICAL_STAGES[scale]),
            "mismatches": 0,
        }
        if workload == "property-presence":
            summary["workloads"][workload].update({
                "sampling_design": "balanced_diagnostic",
                "natural_prevalence_claim": False,
                "min_positive": built_truth["min_positive"],
                "min_mixed": built_truth["min_mixed"],
                "min_zero": built_truth["min_zero"],
                "positive_result_samples": built_truth["positive_result_samples"],
                "mixed_result_samples": built_truth["mixed_result_samples"],
                "zero_result_samples": built_truth["zero_result_samples"],
            })
    atomic_json(output_root / "summary.json", summary)
    (output_root / "PASS").write_text(sha256_file(output_root / "summary.json") + "  summary.json\n", encoding="utf-8")
    print(str(output_root / "summary.json"))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--cleanup-clones", action="store_true")
    args = parser.parse_args()
    output_root = None
    try:
        output_root = absolute(args.output_root, "output root")
        return run(args)
    except (PrepError, inventory.InventoryError, truth_builder.TruthError, OSError) as exc:
        if output_root is not None and (output_root / OWNERSHIP_MARKER).is_file():
            try:
                write_failure(output_root, exc)
            except OSError:
                pass
        print("FAIL: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
