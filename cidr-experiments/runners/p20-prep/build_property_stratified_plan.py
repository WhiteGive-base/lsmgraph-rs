#!/usr/bin/env python3
"""Build one fail-closed balanced P20 property diagnostic sample plan.

The input pool and its property-presence result are produced by the existing
``storage-bench`` command.  This tool never mutates the graph store; it does
re-verify the generation receipt and the full store inventory before selection.
It verifies and binds those artifacts, then deterministically selects exactly
500 mixed-positive and 500 zero-result unique sources.
"""

from __future__ import print_function

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

import build_pristine_inventory as inventory
import generate_property_candidate_pool as candidate_generator


PROFILE = "p20-balanced-property-diagnostic-v1"
EXPECTED_QUERIES = 1000
MIN_POSITIVE = 500
MIN_MIXED = 500
MIN_ZERO = 500
MIXED_TARGET = 500
ZERO_TARGET = 500
SELECTION_SEED = 20260722
CANDIDATE_POOL_REQUESTED = 300000
SHUFFLE_ALGORITHM = "stable-src-then-sha256-counter-fisher-yates-v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{16}$")
U64_MAX = (1 << 64) - 1
FNV_OFFSET_BASIS = 0xCBF29CE484222325
FNV_PRIME = 0x00000100000001B3
U64_MASK = (1 << 64) - 1


class PlanError(ValueError):
    pass


class InsufficientCandidates(PlanError):
    def __init__(self, observed):
        self.observed = observed
        super(InsufficientCandidates, self).__init__(
            "candidate pool cannot satisfy frozen 500-mixed/500-zero contract: "
            "mixed={} zero={} total={}".format(
                observed["mixed"], observed["zero"], observed["total"]
            )
        )


def require(condition, message):
    if not condition:
        raise PlanError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PlanError("duplicate JSON key: {}".format(key))
        result[key] = value
    return result


def normalize_sha(value, label):
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise PlanError("{} must be a lowercase SHA-256".format(label))
    return value


def absolute_lexical(path, label):
    path = Path(path)
    if not path.is_absolute():
        raise PlanError("{} path must be absolute".format(label))
    return Path(os.path.abspath(str(path)))


def reject_symlink_components(path, label, allow_missing_final=False):
    path = absolute_lexical(path, label)
    parts = path.parts
    current = Path(parts[0])
    for index, part in enumerate(parts[1:], 1):
        current = current / part
        final = index == len(parts) - 1
        if not os.path.lexists(str(current)):
            if allow_missing_final and final:
                return path
            raise PlanError("{} path component does not exist: {}".format(label, current))
        try:
            mode = os.lstat(str(current)).st_mode
        except OSError as exc:
            raise PlanError("cannot inspect {}: {}".format(label, exc))
        if stat.S_ISLNK(mode):
            raise PlanError("{} contains symlink component: {}".format(label, current))
    return path


def read_stable_file(path, expected_sha, label, executable=False):
    path = reject_symlink_components(path, label)
    expected_sha = normalize_sha(expected_sha, label + " SHA-256")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        raise PlanError("cannot open {}: {}".format(label, exc))
    digest = hashlib.sha256()
    chunks = []
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise PlanError("{} must be a regular file".format(label))
        if executable and os.name != "nt" and not (before.st_mode & 0o111):
            raise PlanError("{} must be executable".format(label))
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            chunks.append(chunk)
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    identity_before = (
        before.st_dev, before.st_ino, before.st_size,
        before.st_mtime_ns, before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev, after.st_ino, after.st_size,
        after.st_mtime_ns, after.st_ctime_ns,
    )
    if identity_before != identity_after:
        raise PlanError("{} changed while it was read".format(label))
    reject_symlink_components(path, label)
    final = os.lstat(str(path))
    if (final.st_dev, final.st_ino) != (before.st_dev, before.st_ino):
        raise PlanError("{} path identity changed while it was read".format(label))
    actual_sha = digest.hexdigest()
    if actual_sha != expected_sha:
        raise PlanError("{} SHA-256 mismatch".format(label))
    return path.resolve(), b"".join(chunks), actual_sha, identity_before


def load_verified_json(path, expected_sha, label):
    path, raw, actual_sha, identity = read_stable_file(path, expected_sha, label)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, PlanError) as exc:
        raise PlanError("cannot parse {}: {}".format(label, exc))
    if not isinstance(value, dict):
        raise PlanError("{} must be a JSON object".format(label))
    return path, actual_sha, value, identity


def validate_store(path):
    path = reject_symlink_components(path, "store")
    info = os.lstat(str(path))
    if not stat.S_ISDIR(info.st_mode):
        raise PlanError("store must be a directory")
    return path.resolve(), (info.st_dev, info.st_ino)


def validate_output_path(path, label):
    path = absolute_lexical(path, label)
    if os.path.lexists(str(path)):
        raise PlanError("{} must be absent".format(label))
    parent = reject_symlink_components(path.parent, label + " parent")
    if not parent.is_dir():
        raise PlanError("{} parent must be a directory".format(label))
    reject_symlink_components(path, label, allow_missing_final=True)
    return path


def validate_u64(value, label):
    if not isinstance(value, int) or isinstance(value, bool) or not (0 <= value <= U64_MAX):
        raise PlanError("{} must be a u64".format(label))
    return value


def degree_class(degree):
    if degree <= 16:
        return "low"
    if degree <= 1024:
        return "medium"
    return "high"


def validate_candidate_plan(plan):
    require(plan.get("version") == 1, "candidate plan version must be exactly 1")
    require(plan.get("source") == "scan", "candidate plan must be generated from a storage-bench scan")
    require(plan.get("semantic_degree_hint") is True, "candidate plan must record sample degrees")
    require(plan.get("force_signature") is False, "candidate plan cannot force signatures")
    require(plan.get("samples_per_edge_type") == CANDIDATE_POOL_REQUESTED,
            "candidate plan samples_per_edge_type must be exactly {}".format(
                CANDIDATE_POOL_REQUESTED
            ))
    entries = plan.get("entries")
    require(isinstance(entries, list) and len(entries) == 1, "candidate plan must have exactly one entry")
    entry = entries[0]
    require(isinstance(entry, dict), "candidate plan entry must be an object")
    require(entry.get("edge_type") is None, "candidate property entry must use edge_type=null")
    require(entry.get("src_label") is None and entry.get("dst_label") is None,
            "candidate property entry must not constrain labels")
    samples = entry.get("samples")
    require(isinstance(samples, list) and samples, "candidate plan samples must be non-empty")
    seen = set()
    normalized = []
    for index, sample in enumerate(samples):
        require(isinstance(sample, dict), "candidate sample {} must be an object".format(index))
        src = validate_u64(sample.get("src"), "candidate sample src")
        degree = validate_u64(sample.get("degree"), "candidate sample degree")
        require(degree > 0, "candidate sample degree must be positive")
        require(src not in seen, "candidate plan contains duplicate source {}".format(src))
        seen.add(src)
        normalized.append({"src": src, "degree": degree})
    return entry, normalized


def validate_typed_plan(plan):
    require(plan.get("version") == 1, "typed plan version must be exactly 1")
    entries = plan.get("entries")
    require(isinstance(entries, list) and entries, "typed plan entries must be non-empty")
    for index, entry in enumerate(entries):
        require(isinstance(entry, dict), "typed plan entry {} must be an object".format(index))
        edge_type = entry.get("edge_type")
        require(isinstance(edge_type, int) and not isinstance(edge_type, bool),
                "typed plan entries must use a concrete edge type")


def fnv1a_update(value, raw):
    for byte in raw:
        value ^= byte
        value = (value * FNV_PRIME) & U64_MASK
    return value


def fold_entry_digest(samples):
    value = FNV_OFFSET_BASIS
    for sample in samples:
        value = fnv1a_update(value, sample["src"].to_bytes(8, "little", signed=False))
        value = fnv1a_update(
            value, int(sample["result_digest"], 16).to_bytes(8, "little", signed=False)
        )
    return "{:016x}".format(value)


def result_store_path(result):
    raw = result.get("data_dir")
    if not isinstance(raw, str) or not raw:
        raise PlanError("candidate result has invalid data_dir")
    return reject_symlink_components(Path(raw), "candidate result store").resolve()


def validate_candidate_result(result, plan, plan_path, samples, store, snapshot, property_id):
    required_top = {
        "edge_type": None,
        "edge_types": [],
        "src_label": None,
        "dst_label": None,
        "sample_plan_version": 1,
        "sample_plan_out": None,
        "scan_requested": False,
        "emit_result_digests": True,
        "workload_mode": "one_hop",
        "property_predicate_mode": "presence",
        "property_id": property_id,
        "semantic_degree_hint": False,
        "sample_plan_degree_hint": False,
        "force_signature": False,
        "warmup_runs": 0,
        "repeats": 1,
    }
    for field, expected in required_top.items():
        require(result.get(field) == expected, "candidate result {} drift".format(field))
    require(result_store_path(result) == store, "candidate result store binding drift")
    require(result.get("snapshot") == snapshot, "candidate result snapshot binding drift")
    bound_plan = result.get("sample_plan_in")
    require(isinstance(bound_plan, str) and bound_plan, "candidate result lacks sample_plan_in")
    bound_plan = reject_symlink_components(Path(bound_plan), "candidate result sample plan").resolve()
    require(bound_plan == plan_path, "candidate result does not bind candidate plan path")
    benchmarks = result.get("benchmarks")
    require(isinstance(benchmarks, list) and len(benchmarks) == 1,
            "candidate result must contain exactly one benchmark")
    benchmark = benchmarks[0]
    require(isinstance(benchmark, dict), "candidate benchmark must be an object")
    plan_entry = plan["entries"][0]
    required_benchmark = {
        "edge_type": None,
        "src_label": None,
        "dst_label": None,
        "emit_result_digests": True,
        "workload_mode": "one_hop",
        "property_predicate_mode": "presence",
        "property_id": property_id,
        "semantic_degree_hint": False,
        "force_signature": False,
        "sampled_vertices": len(samples),
        "sampled_srcs": [sample["src"] for sample in samples],
        "sample_degrees": samples,
        "warmup_runs": 0,
        "repeats": 1,
    }
    for field, expected in required_benchmark.items():
        require(benchmark.get(field) == expected, "candidate benchmark {} drift".format(field))
    digests = benchmark.get("result_digests")
    require(isinstance(digests, list) and len(digests) == len(samples),
            "candidate result digest coverage drift")
    classified = []
    for index, (sample, digest) in enumerate(zip(samples, digests)):
        require(isinstance(digest, dict), "candidate digest {} must be an object".format(index))
        expected_fields = {
            "src": sample["src"],
            "degree": sample["degree"],
            "edge_type": None,
            "dst_label": plan_entry.get("dst_label"),
            "property_predicate_mode": "presence",
        }
        for field, expected in expected_fields.items():
            require(digest.get(field) == expected,
                    "candidate digest {} {} drift".format(index, field))
        count = digest.get("result_count")
        require(isinstance(count, int) and not isinstance(count, bool) and count >= 0,
                "candidate result_count must be a non-negative integer")
        require(count <= sample["degree"], "candidate property count exceeds all-edge degree")
        result_digest = digest.get("result_digest")
        require(isinstance(result_digest, str) and DIGEST_RE.fullmatch(result_digest),
                "candidate result digest must be lowercase 16-hex")
        if count == 0:
            stratum = "zero"
        elif count < sample["degree"]:
            stratum = "mixed"
        else:
            stratum = "full_positive"
        classified.append({
            "src": sample["src"],
            "degree": sample["degree"],
            "property_count": count,
            "result_digest": result_digest,
            "stratum": stratum,
            "degree_class": degree_class(sample["degree"]),
        })
    compact = [
        {"src": row["src"], "result_count": row["property_count"],
         "result_digest": row["result_digest"]}
        for row in classified
    ]
    folded = fold_entry_digest(compact)
    require(benchmark.get("entry_result_digest") == folded,
            "candidate benchmark entry digest does not fold sample digests")
    rounds = benchmark.get("rounds")
    require(isinstance(rounds, list) and len(rounds) == 1 and isinstance(rounds[0], dict),
            "candidate benchmark must contain exactly one measured round")
    require(rounds[0].get("kind") == "measured" and rounds[0].get("round") == 1,
            "candidate measured round identity drift")
    return classified


def deterministic_shuffle(rows, stream):
    values = list(sorted(rows, key=lambda row: row["src"]))
    counter = 0
    for index in range(len(values) - 1, 0, -1):
        payload = "{}\0{}\0{}\0{}".format(
            PROFILE, SELECTION_SEED, stream, counter
        ).encode("utf-8")
        choice = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") % (index + 1)
        values[index], values[choice] = values[choice], values[index]
        counter += 1
    return values


def canonical_sha(value):
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def safe_unlink_owned(path, identity):
    try:
        current = os.lstat(str(path))
    except OSError:
        return
    if (current.st_dev, current.st_ino) == identity and stat.S_ISREG(current.st_mode):
        os.unlink(str(path))


def write_pair_new(first_path, first_raw, second_path, second_raw):
    temporaries = {}
    published = {}
    try:
        for path, raw in ((first_path, first_raw), (second_path, second_raw)):
            temporary = path.with_name(path.name + ".tmp")
            descriptor = os.open(
                str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            temporary_info = os.fstat(descriptor)
            temporary_identity = (temporary_info.st_dev, temporary_info.st_ino)
            temporaries[temporary] = temporary_identity
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        for path in (first_path, second_path):
            temporary = path.with_name(path.name + ".tmp")
            os.link(str(temporary), str(path))
            published[path] = temporaries[temporary]
        for temporary, identity in temporaries.items():
            safe_unlink_owned(temporary, identity)
    except Exception:
        for temporary, identity in temporaries.items():
            safe_unlink_owned(temporary, identity)
        for path, identity in published.items():
            safe_unlink_owned(path, identity)
        raise


def receipt_artifact(receipt, name, parse_json=False):
    artifacts = receipt.get("artifacts")
    require(isinstance(artifacts, dict), "generation receipt lacks artifacts")
    row = artifacts.get(name)
    require(isinstance(row, dict), "generation receipt lacks {} artifact".format(name))
    if parse_json:
        return load_verified_json(row.get("path"), row.get("sha256"), name)
    path, _, sha, _ = read_stable_file(
        row.get("path"), row.get("sha256"), name
    )
    return path, sha


def validate_generation_receipt(path, expected_sha):
    receipt_path, receipt_sha, receipt, _ = load_verified_json(
        path, expected_sha, "generation receipt"
    )
    scale = receipt.get("scale")
    require(scale in ("sf1", "sf10"), "generation receipt scale drift")
    expected_top = {
        "schema_version": 1,
        "state": "PASS",
        "gate": candidate_generator.GATE,
        "property_id": 5,
        "candidate_pool_requested": CANDIDATE_POOL_REQUESTED,
        "sample_plan_role": "candidate_pool",
        "performance_eligible": False,
        "formal_plan_eligible": False,
        "downstream_input_eligible": scale == "sf10",
    }
    for field, expected in expected_top.items():
        require(receipt.get(field) == expected,
                "generation receipt {} drift".format(field))
    dataset_sha = normalize_sha(receipt.get("dataset_sha256"), "dataset SHA-256")
    binary_row = receipt.get("binary")
    require(isinstance(binary_row, dict), "generation receipt lacks binary")
    binary_path, _, binary_sha, _ = read_stable_file(
        binary_row.get("path"), binary_row.get("sha256"), "generation binary",
        executable=True,
    )
    typed_row = receipt.get("typed_plan")
    require(isinstance(typed_row, dict), "generation receipt lacks typed plan")
    typed_path, typed_sha, typed_plan, _ = load_verified_json(
        typed_row.get("path"), typed_row.get("sha256"), "typed plan"
    )
    validate_typed_plan(typed_plan)
    store_row = receipt.get("store")
    require(isinstance(store_row, dict), "generation receipt lacks store")
    store, _ = validate_store(store_row.get("path"))
    snapshot = store_row.get("snapshot")
    require(isinstance(snapshot, int) and not isinstance(snapshot, bool) and snapshot > 0,
            "generation receipt snapshot drift")
    require(store_row.get("full_content_verified_before") is True and
            store_row.get("full_content_verified_after") is True,
            "generation receipt lacks full store verification")
    before_sha = normalize_sha(
        store_row.get("inventory_sha256_before"), "pre-generation inventory SHA-256"
    )
    after_sha = normalize_sha(
        store_row.get("inventory_sha256_after"), "post-generation inventory SHA-256"
    )
    require(before_sha == after_sha, "generation receipt store identity drift")

    pristine_binding = receipt.get("pristine_inventory")
    if scale == "sf10":
        require(isinstance(pristine_binding, dict),
                "SF10 generation receipt lacks pristine inventory")
        manifest_path, manifest_sha, manifest, _ = load_verified_json(
            pristine_binding.get("path"), pristine_binding.get("sha256"),
            "pristine inventory manifest",
        )
        inventory.validate_manifest(
            manifest, store, "sf10", dataset_sha, binary_sha
        )
        live_inventory = inventory.verify_content(store, manifest)
        expected_inventory_binding = {
            "path": str(manifest_path),
            "sha256": manifest_sha,
            "inventory_sha256": manifest["inventory_sha256"],
            "store_path": manifest["store_path"],
            "scale": manifest["scale"],
            "dataset_sha256": manifest["dataset_sha256"],
            "binary_sha256": manifest["binary_sha256"],
        }
        require(pristine_binding == expected_inventory_binding,
                "generation receipt pristine inventory binding drift")
    else:
        require(pristine_binding is None,
                "SF1 calibration cannot bind a formal pristine inventory")
        manifest_path = None
        manifest_sha = None
        live_inventory = inventory.scan_full_manifest(
            store, "sf1", dataset_sha, binary_sha
        )
    require(live_inventory["inventory_sha256"] == before_sha,
            "live store differs from generation receipt")
    require(store_row.get("file_count") == live_inventory["file_count"] and
            store_row.get("total_bytes") == live_inventory["total_bytes"],
            "generation receipt store size/count drift")

    candidate_path, candidate_sha, candidate_plan, _ = receipt_artifact(
        receipt, "candidate_plan", parse_json=True
    )
    result_path, result_sha, candidate_result, _ = receipt_artifact(
        receipt, "candidate_result", parse_json=True
    )
    plan_generation_path, plan_generation_sha, plan_generation_doc, _ = receipt_artifact(
        receipt, "candidate_plan_generation", parse_json=True
    )
    for name in ("candidate_plan_stderr", "candidate_result_stderr"):
        receipt_artifact(receipt, name)
    require(candidate_path != typed_path and candidate_sha != typed_sha,
            "property candidate and typed plan path/SHA-256 must be independent")
    commands = receipt.get("commands")
    require(isinstance(commands, dict), "generation receipt lacks commands")
    expected_plan_argv, expected_result_argv = candidate_generator.exact_commands(
        binary_path, store, candidate_path
    )
    expected_commands = {
        "candidate_plan": {"argv": expected_plan_argv, "exit_code": 0},
        "candidate_result": {"argv": expected_result_argv, "exit_code": 0},
        "forbidden_environment": {
            "SNB_SKIP_SEM_INDEX": "absent",
            "SNB_SKIP_ADJ_CACHE": "absent",
        },
    }
    require(commands == expected_commands,
            "generation receipt exact storage-bench command drift")
    candidate_generator.validate_plan_generation_output(
        plan_generation_doc, candidate_plan, store, snapshot, candidate_path
    )
    entry, samples = validate_candidate_plan(candidate_plan)
    classified = validate_candidate_result(
        candidate_result, candidate_plan, candidate_path, samples,
        store, snapshot, 5,
    )
    # Recheck trust roots after all JSON and full-store validation.
    read_stable_file(binary_path, binary_sha, "generation binary", executable=True)
    read_stable_file(typed_path, typed_sha, "typed plan")
    read_stable_file(receipt_path, receipt_sha, "generation receipt")
    if manifest_path is not None:
        read_stable_file(manifest_path, manifest_sha, "pristine inventory manifest")
    return {
        "receipt_path": receipt_path,
        "receipt_sha": receipt_sha,
        "receipt": receipt,
        "scale": scale,
        "dataset_sha": dataset_sha,
        "binary_path": binary_path,
        "binary_sha": binary_sha,
        "typed_path": typed_path,
        "typed_sha": typed_sha,
        "store": store,
        "snapshot": snapshot,
        "manifest_path": manifest_path,
        "manifest_sha": manifest_sha,
        "pristine_binding": pristine_binding,
        "candidate_path": candidate_path,
        "candidate_sha": candidate_sha,
        "candidate_plan": candidate_plan,
        "plan_generation_path": plan_generation_path,
        "plan_generation_sha": plan_generation_sha,
        "result_path": result_path,
        "result_sha": result_sha,
        "classified": classified,
        "entry": entry,
        "samples": samples,
    }


def build(args):
    output_plan = validate_output_path(args.output_plan, "output plan")
    output_receipt = validate_output_path(args.output_receipt, "output receipt")
    require(output_plan != output_receipt, "output plan and receipt paths must differ")
    generation = validate_generation_receipt(
        args.generation_receipt, args.generation_receipt_sha256
    )
    input_paths = {
        generation["receipt_path"], generation["candidate_path"],
        generation["result_path"], generation["typed_path"],
        generation["binary_path"],
    }
    require(output_plan not in input_paths and output_receipt not in input_paths,
            "outputs must not overlap generation inputs")
    candidate_sha = generation["candidate_sha"]
    typed_sha = generation["typed_sha"]
    entry = generation["entry"]
    samples = generation["samples"]
    classified = generation["classified"]
    observed = {
        "total": len(classified),
        "positive": sum(row["property_count"] > 0 for row in classified),
        "mixed": sum(row["stratum"] == "mixed" for row in classified),
        "zero": sum(row["stratum"] == "zero" for row in classified),
        "full_positive": sum(row["stratum"] == "full_positive" for row in classified),
        "degree_classes": {
            name: sum(row["degree_class"] == name for row in classified)
            for name in ("low", "medium", "high")
        },
    }
    mixed = [row for row in classified if row["stratum"] == "mixed"]
    zero = [row for row in classified if row["stratum"] == "zero"]
    if len(mixed) < MIXED_TARGET or len(zero) < ZERO_TARGET:
        raise InsufficientCandidates(observed)
    selected = (
        deterministic_shuffle(mixed, "mixed")[:MIXED_TARGET]
        + deterministic_shuffle(zero, "zero")[:ZERO_TARGET]
    )
    selected = deterministic_shuffle(selected, "combined")
    require(len(selected) == EXPECTED_QUERIES, "selected query count drift")
    require(len({row["src"] for row in selected}) == EXPECTED_QUERIES,
            "selected sources are not globally unique")
    selected_classes = sorted({row["degree_class"] for row in selected})
    require(len(selected_classes) >= 2,
            "selected plan must cover at least two frozen degree classes")
    final_entry = {
        "edge_type": None,
        "src_label": None,
        "dst_label": None,
        "candidate_edges_for_sampling": entry.get("candidate_edges_for_sampling"),
        "candidate_sources_for_sampling": entry.get("candidate_sources_for_sampling"),
        "samples": [
            {"src": row["src"], "degree": row["degree"]} for row in selected
        ],
    }
    require(isinstance(final_entry["candidate_edges_for_sampling"], int) and
            final_entry["candidate_edges_for_sampling"] >= EXPECTED_QUERIES,
            "candidate edge count is invalid")
    require(isinstance(final_entry["candidate_sources_for_sampling"], int) and
            final_entry["candidate_sources_for_sampling"] >= len(samples),
            "candidate source count is invalid")
    final_plan = {
        "version": 1,
        "source": PROFILE,
        "samples_per_edge_type": EXPECTED_QUERIES,
        "semantic_degree_hint": True,
        "force_signature": False,
        "src_label": None,
        "dst_label": None,
        "entries": [final_entry],
    }
    plan_raw = json_bytes(final_plan)
    plan_sha = hashlib.sha256(plan_raw).hexdigest()
    require(plan_sha != typed_sha, "final property and typed plan SHA-256 must differ")
    selected_audit = [
        {
            "src": row["src"],
            "degree": row["degree"],
            "property_count": row["property_count"],
            "stratum": row["stratum"],
            "degree_class": row["degree_class"],
        }
        for row in selected
    ]
    formal_eligible = (
        generation["scale"] == "sf10"
        and generation["receipt"].get("downstream_input_eligible") is True
        and generation["pristine_binding"] is not None
    )
    receipt = {
        "schema_version": 1,
        "state": "PASS",
        "gate": PROFILE,
        "scale": generation["scale"],
        "property_id": 5,
        "sampling_design": "balanced_diagnostic",
        "natural_prevalence_claim": False,
        "natural_prevalence_eligible": False,
        "selection_seed": SELECTION_SEED,
        "candidate_pool_requested": CANDIDATE_POOL_REQUESTED,
        "shuffle_algorithm": SHUFFLE_ALGORITHM,
        "sample_plan_role": "balanced_diagnostic" if formal_eligible else "calibration_only",
        "formal_plan_eligible": formal_eligible,
        "downstream_input_eligible": formal_eligible,
        "performance_eligible": False,
        "thresholds": {
            "expected_queries": EXPECTED_QUERIES,
            "min_positive": MIN_POSITIVE,
            "min_mixed": MIN_MIXED,
            "min_zero": MIN_ZERO,
        },
        "observed_pool": observed,
        "selected": {
            "queries": len(selected),
            "unique_sources": len({row["src"] for row in selected}),
            "positive": sum(row["property_count"] > 0 for row in selected),
            "mixed": sum(row["stratum"] == "mixed" for row in selected),
            "zero": sum(row["stratum"] == "zero" for row in selected),
            "degree_classes": selected_classes,
            "source_assignment_sha256": canonical_sha(selected_audit),
            "samples": selected_audit,
        },
        "generation_receipt": {
            "path": str(generation["receipt_path"]),
            "sha256": generation["receipt_sha"],
        },
        "store": {
            "path": str(generation["store"]),
            "snapshot": generation["snapshot"],
            "inventory_sha256": generation["receipt"]["store"]["inventory_sha256_after"],
        },
        "dataset_sha256": generation["dataset_sha"],
        "pristine_inventory": generation["pristine_binding"],
        "binary": {
            "path": str(generation["binary_path"]),
            "sha256": generation["binary_sha"],
        },
        "candidate_plan": {
            "path": str(generation["candidate_path"]), "sha256": candidate_sha,
        },
        "candidate_result": {
            "path": str(generation["result_path"]), "sha256": generation["result_sha"],
        },
        "typed_plan": {
            "path": str(generation["typed_path"]), "sha256": typed_sha,
        },
        "output_plan": {"path": str(output_plan), "sha256": plan_sha},
        "plan_constraints": {
            "entries": 1,
            "edge_type": None,
            "src_label": None,
            "dst_label": None,
            "minimum_degree_classes": 2,
        },
    }
    receipt_raw = json_bytes(receipt)
    write_pair_new(output_plan, plan_raw, output_receipt, receipt_raw)
    return receipt, hashlib.sha256(receipt_raw).hexdigest()


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--generation-receipt", required=True, type=Path)
    value.add_argument("--generation-receipt-sha256", required=True)
    value.add_argument("--output-plan", required=True, type=Path)
    value.add_argument("--output-receipt", required=True, type=Path)
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        receipt, receipt_sha = build(args)
    except InsufficientCandidates as exc:
        print(json.dumps({
            "state": "FAIL",
            "reason": str(exc),
            "observed_pool": exc.observed,
            "required": {
                "expected_queries": EXPECTED_QUERIES,
                "min_positive": MIN_POSITIVE,
                "min_mixed": MIN_MIXED,
                "min_zero": MIN_ZERO,
            },
        }, sort_keys=True), file=sys.stderr)
        return 2
    except (
        PlanError, inventory.InventoryError, candidate_generator.GenerationError,
        OSError, ValueError,
    ) as exc:
        print("FAIL: {}".format(exc), file=sys.stderr)
        return 2
    print(json.dumps({
        "state": "PASS",
        "plan": receipt["output_plan"]["path"],
        "plan_sha256": receipt["output_plan"]["sha256"],
        "receipt": str(Path(args.output_receipt).resolve()),
        "receipt_sha256": receipt_sha,
        "formal_plan_eligible": receipt["formal_plan_eligible"],
        "downstream_input_eligible": receipt["downstream_input_eligible"],
        "performance_eligible": receipt["performance_eligible"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
