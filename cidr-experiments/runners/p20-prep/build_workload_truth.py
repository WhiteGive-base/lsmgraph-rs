#!/usr/bin/env python3
"""Create one frozen P20 workload truth from a trusted storage-bench output."""

from __future__ import print_function

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{16}$")
WORKLOADS = ("typed-one-hop", "degree-stratified", "property-presence")
STAGES = tuple("A{}".format(index) for index in range(7))
FNV_OFFSET_BASIS = 0xCBF29CE484222325
FNV_PRIME = 0x00000100000001B3
U64_MASK = (1 << 64) - 1
PROPERTY_EXPECTED_QUERIES = 1000
PROPERTY_MIN_POSITIVE = 500
PROPERTY_MIN_MIXED = 500
PROPERTY_MIN_ZERO = 500


class TruthError(ValueError):
    pass


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TruthError("duplicate JSON key: {}".format(key))
        result[key] = value
    return result


def load_json(path, label):
    try:
        value = json.loads(
            Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique_object
        )
    except (OSError, json.JSONDecodeError, TruthError) as exc:
        raise TruthError("cannot read {}: {}".format(label, exc))
    if not isinstance(value, dict):
        raise TruthError("{} must be a JSON object".format(label))
    return value


def reject_symlink_components(path, label):
    path = Path(path)
    if not path.is_absolute():
        raise TruthError("{} path must be absolute".format(label))
    path = Path(os.path.abspath(str(path)))
    parts = path.parts
    current = Path(parts[0])
    for part in parts[1:]:
        current = current / part
        if not os.path.lexists(str(current)):
            raise TruthError("{} path component does not exist".format(label))
        try:
            mode = os.lstat(str(current)).st_mode
        except OSError as exc:
            raise TruthError("cannot inspect {}: {}".format(label, exc))
        if stat.S_ISLNK(mode):
            raise TruthError("{} contains a symlink component".format(label))
    return path


def read_stable_file(path, label):
    """Return ``(resolved_path, bytes, sha256)`` for one stable regular file."""
    path = reject_symlink_components(path, label)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        raise TruthError("cannot open {}: {}".format(label, exc))
    digest = hashlib.sha256()
    chunks = []
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise TruthError("{} must be a regular file".format(label))
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
        raise TruthError("{} changed while it was read".format(label))
    reject_symlink_components(path, label)
    final = os.lstat(str(path))
    if (final.st_dev, final.st_ino) != (before.st_dev, before.st_ino):
        raise TruthError("{} path identity changed while it was read".format(label))
    return path.resolve(), b"".join(chunks), digest.hexdigest()


def sha256_file(path):
    return read_stable_file(path, "hash input")[2]


def normalize_sha(value, label):
    value = value.strip().lower()
    if not SHA256_RE.fullmatch(value):
        raise TruthError("{} must be a lowercase SHA-256".format(label))
    return value


def verify_file(path, expected_sha, label):
    path, _, actual = read_stable_file(path, label)
    expected_sha = normalize_sha(expected_sha, label + " SHA-256")
    if actual != expected_sha:
        raise TruthError("{} SHA-256 mismatch".format(label))
    return path, actual


def load_verified_json(path, expected_sha, label):
    path, raw, actual = read_stable_file(path, label)
    expected_sha = normalize_sha(expected_sha, label + " SHA-256")
    if actual != expected_sha:
        raise TruthError("{} SHA-256 mismatch".format(label))
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, TruthError) as exc:
        raise TruthError("cannot read {}: {}".format(label, exc))
    if not isinstance(value, dict):
        raise TruthError("{} must be a JSON object".format(label))
    return path, actual, value, raw


def fnv1a_update(value, raw):
    for byte in raw:
        value ^= byte
        value = (value * FNV_PRIME) & U64_MASK
    return value


def fold_entry_digest(samples):
    value = FNV_OFFSET_BASIS
    for sample in samples:
        src = sample["src"]
        digest = int(sample["result_digest"], 16)
        value = fnv1a_update(value, src.to_bytes(8, "little", signed=False))
        value = fnv1a_update(value, digest.to_bytes(8, "little", signed=False))
    return "{:016x}".format(value)


def validate_sample_plan(plan):
    if plan.get("version") != 1:
        raise TruthError("sample plan version must be exactly 1")
    entries = plan.get("entries")
    if not isinstance(entries, list) or not entries:
        raise TruthError("sample plan entries must be non-empty")
    total = 0
    all_degrees = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise TruthError("sample plan entry {} is not an object".format(index))
        samples = entry.get("samples")
        if not isinstance(samples, list) or not samples:
            raise TruthError("sample plan entry {} has no samples".format(index))
        seen = set()
        for sample in samples:
            if not isinstance(sample, dict):
                raise TruthError("sample plan sample is not an object")
            src = sample.get("src")
            degree = sample.get("degree")
            if not isinstance(src, int) or isinstance(src, bool) or not (0 <= src <= U64_MASK):
                raise TruthError("sample src must be a u64")
            if src in seen:
                raise TruthError("duplicate sample src within entry")
            seen.add(src)
            if not isinstance(degree, int) or isinstance(degree, bool) or degree < 0:
                raise TruthError("sample degree must be a non-negative integer")
            all_degrees.append(degree)
            total += 1
    return entries, total, all_degrees


def _expected_property(workload, property_id):
    if not isinstance(property_id, int) or isinstance(property_id, bool) or not (0 <= property_id <= 0xFFFFFFFF):
        raise TruthError("property_id must be a u32")
    if workload == "property-presence":
        return "presence", property_id
    if property_id != 0:
        raise TruthError("non-property workloads require property_id=0")
    return "none", 0


def canonical_property_requirements(workload, requirements):
    if workload != "property-presence":
        if requirements not in (None, {}):
            raise TruthError("non-property workloads cannot set property sampling thresholds")
        return None
    expected = {
        "expected_queries": PROPERTY_EXPECTED_QUERIES,
        "min_positive": PROPERTY_MIN_POSITIVE,
        "min_mixed": PROPERTY_MIN_MIXED,
        "min_zero": PROPERTY_MIN_ZERO,
    }
    if requirements != expected:
        raise TruthError(
            "property-presence requires canonical thresholds {}".format(expected)
        )
    return expected


def property_degree_class(degree):
    if degree <= 16:
        return "low"
    if degree <= 1024:
        return "medium"
    return "high"


def validate_reference(
    reference, plan, plan_path, workload, property_id, reference_stage,
    property_requirements=None,
):
    plan_entries, expected_queries, all_degrees = validate_sample_plan(plan)
    property_mode, property_id = _expected_property(workload, property_id)
    property_requirements = canonical_property_requirements(
        workload, property_requirements
    )
    if workload == "property-presence":
        globally_seen = set()
        for index, entry in enumerate(plan_entries):
            if entry.get("edge_type") is not None:
                raise TruthError(
                    "property-presence plan entry {} must use edge_type=null".format(index)
                )
            for sample in entry["samples"]:
                if sample["src"] in globally_seen:
                    raise TruthError("property-presence plan has a duplicate source")
                globally_seen.add(sample["src"])
                if sample["degree"] <= 0:
                    raise TruthError("property-presence plan degrees must be positive")
    degree_mode = workload == "degree-stratified"
    effective_force_signature = bool(plan.get("force_signature", False))
    required_top = {
        "query_control_stage": reference_stage,
        "sample_plan_version": 1,
        "scan_requested": False,
        "emit_result_digests": True,
        "workload_mode": "one_hop",
        "property_predicate_mode": property_mode,
        "property_id": property_id,
        "semantic_degree_hint": degree_mode,
        "sample_plan_degree_hint": degree_mode,
        "force_signature": effective_force_signature,
        "warmup_runs": 0,
        "repeats": 1,
    }
    for field, expected in required_top.items():
        if reference.get(field) != expected:
            raise TruthError("reference output {} drift".format(field))
    try:
        bound_plan = Path(reference.get("sample_plan_in", "")).resolve()
    except (TypeError, OSError):
        raise TruthError("reference output has invalid sample_plan_in")
    if bound_plan != plan_path:
        raise TruthError("reference output does not bind the supplied sample plan path")
    benchmarks = reference.get("benchmarks")
    if not isinstance(benchmarks, list) or len(benchmarks) != len(plan_entries):
        raise TruthError("reference benchmark coverage differs from sample plan")
    truth_entries = []
    positive_results = 0
    mixed_results = 0
    zero_results = 0
    checked = 0
    for entry_index, (plan_entry, benchmark) in enumerate(zip(plan_entries, benchmarks)):
        if not isinstance(benchmark, dict):
            raise TruthError("benchmark {} is not an object".format(entry_index))
        for field in ("edge_type", "src_label", "dst_label"):
            if benchmark.get(field) != plan_entry.get(field):
                raise TruthError("benchmark {} {} differs from plan".format(entry_index, field))
        if benchmark.get("emit_result_digests") is not True:
            raise TruthError("benchmark {} did not emit result digests".format(entry_index))
        if benchmark.get("workload_mode") != "one_hop":
            raise TruthError("benchmark {} workload_mode drift".format(entry_index))
        if benchmark.get("semantic_degree_hint") is not degree_mode:
            raise TruthError("benchmark {} semantic degree-mode drift".format(entry_index))
        if benchmark.get("force_signature") is not effective_force_signature:
            raise TruthError("benchmark {} force_signature drift".format(entry_index))
        if benchmark.get("property_predicate_mode") != property_mode or benchmark.get("property_id") != property_id:
            raise TruthError("benchmark {} property binding drift".format(entry_index))
        plan_samples = plan_entry["samples"]
        expected_sources = [item["src"] for item in plan_samples]
        if benchmark.get("sampled_srcs") != expected_sources:
            raise TruthError("benchmark {} sampled source order drift".format(entry_index))
        if benchmark.get("sampled_vertices") != len(plan_samples):
            raise TruthError("benchmark {} sampled_vertices drift".format(entry_index))
        if benchmark.get("sample_degrees") != plan_samples:
            raise TruthError("benchmark {} sample_degrees drift".format(entry_index))
        actual_samples = benchmark.get("result_digests")
        if not isinstance(actual_samples, list) or len(actual_samples) != len(plan_samples):
            raise TruthError("benchmark {} digest coverage drift".format(entry_index))
        truth_samples = []
        for sample_index, (plan_sample, actual) in enumerate(zip(plan_samples, actual_samples)):
            if not isinstance(actual, dict):
                raise TruthError("benchmark sample digest is not an object")
            src = actual.get("src")
            count = actual.get("result_count")
            digest = actual.get("result_digest")
            if src != plan_sample["src"]:
                raise TruthError("benchmark {} sample {} source drift".format(entry_index, sample_index))
            expected_sample_fields = {
                "degree": plan_sample["degree"],
                "edge_type": plan_entry.get("edge_type"),
                "dst_label": plan_entry.get("dst_label"),
                "property_predicate_mode": property_mode,
            }
            for field, expected in expected_sample_fields.items():
                if actual.get(field) != expected:
                    raise TruthError(
                        "benchmark {} sample {} {} drift".format(
                            entry_index, sample_index, field
                        )
                    )
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise TruthError("result_count must be a non-negative integer")
            if workload == "property-presence":
                degree = plan_sample["degree"]
                if count > degree:
                    raise TruthError("property result exceeds all-edge degree")
                if count == 0:
                    zero_results += 1
                elif count < degree:
                    mixed_results += 1
            if not DIGEST_RE.fullmatch(digest or ""):
                raise TruthError("result_digest must be lowercase 16-hex")
            truth_samples.append({"src": src, "result_count": count, "result_digest": digest})
            checked += 1
            if count > 0:
                positive_results += 1
        folded = fold_entry_digest(truth_samples)
        if benchmark.get("entry_result_digest") != folded:
            raise TruthError("benchmark {} entry digest does not fold its sample digests".format(entry_index))
        rounds = benchmark.get("rounds")
        if not isinstance(rounds, list) or len(rounds) != 1 or not isinstance(rounds[0], dict):
            raise TruthError("benchmark {} must contain exactly one measured round".format(entry_index))
        last_round = rounds[0]
        if last_round.get("kind") != "measured" or last_round.get("round") != 1:
            raise TruthError("benchmark {} measured-round identity drift".format(entry_index))
        truth_entries.append(
            {
                "edge_type": benchmark.get("edge_type"),
                "src_label": benchmark.get("src_label"),
                "dst_label": benchmark.get("dst_label"),
                "entry_result_digest": folded,
                "samples": truth_samples,
            }
        )
    if checked != expected_queries:
        raise TruthError("reference output query coverage drift")
    if positive_results == 0:
        raise TruthError("workload has zero positive result samples; formal truth would be vacuous")
    if workload == "property-presence":
        if checked != property_requirements["expected_queries"]:
            raise TruthError("property expected_queries threshold drift")
        if positive_results < property_requirements["min_positive"]:
            raise TruthError("property positive-result coverage is below min_positive")
        if mixed_results < property_requirements["min_mixed"]:
            raise TruthError("property mixed-result coverage is below min_mixed")
        if zero_results < property_requirements["min_zero"]:
            raise TruthError("property zero-result coverage is below min_zero")
        degree_classes = sorted({property_degree_class(degree) for degree in all_degrees})
        if len(degree_classes) < 2:
            raise TruthError("property-presence plan covers fewer than two degree classes")
    else:
        degree_classes = sorted({property_degree_class(degree) for degree in all_degrees})
    if workload == "degree-stratified" and len(set(all_degrees)) < 2:
        raise TruthError("degree-stratified plan has no degree diversity")
    return truth_entries, checked, positive_results, property_mode, {
        "positive_result_samples": positive_results,
        "mixed_result_samples": mixed_results,
        "zero_result_samples": zero_results,
        "degree_classes": degree_classes,
    }


def build_truth(
    scale, workload, property_id, sample_path, sample_sha, reference_path,
    reference_sha, reference_stage, property_requirements=None,
):
    sample_path, sample_sha, plan, _ = load_verified_json(
        sample_path, sample_sha, "sample plan"
    )
    reference_path, reference_sha, reference, _ = load_verified_json(
        reference_path, reference_sha, "trusted reference output"
    )
    entries, checked, positive, property_mode, distribution = validate_reference(
        reference, plan, sample_path, workload, property_id, reference_stage,
        property_requirements,
    )
    result = {
        "schema_version": 1,
        "digest_schema": "storage-bench-result-digest-v1",
        "scale": scale,
        "workload": workload,
        "sample_plan_sha256": sample_sha,
        "property_predicate_mode": property_mode,
        "property_id": property_id,
        "reference_output_path": str(reference_path),
        "reference_output_sha256": reference_sha,
        "reference_query_control_stage": reference_stage,
        "expected_queries": checked,
        "positive_result_samples": positive,
        "entries": entries,
    }
    if workload == "property-presence":
        result.update({
            "min_positive": property_requirements["min_positive"],
            "min_mixed": property_requirements["min_mixed"],
            "min_zero": property_requirements["min_zero"],
            "mixed_result_samples": distribution["mixed_result_samples"],
            "zero_result_samples": distribution["zero_result_samples"],
            "degree_classes": distribution["degree_classes"],
            "sampling_design": "balanced_diagnostic",
            "natural_prevalence_claim": False,
        })
    return result


def write_json_new(path, value):
    temporary = path.with_name(path.name + ".tmp")
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary_identity = None
    published = False
    try:
        temporary_fd = os.open(
            str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
        info = os.fstat(temporary_fd)
        temporary_identity = (info.st_dev, info.st_ino)
        with os.fdopen(temporary_fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(str(temporary), str(path))
        published = True
        os.unlink(str(temporary))
    except Exception:
        for candidate in (temporary, path if published else None):
            if candidate is None or temporary_identity is None:
                continue
            try:
                current = os.lstat(str(candidate))
                if (
                    stat.S_ISREG(current.st_mode)
                    and (current.st_dev, current.st_ino) == temporary_identity
                ):
                    os.unlink(str(candidate))
            except OSError:
                pass
        raise


def run(args):
    output = Path(args.output)
    if not output.is_absolute():
        raise TruthError("output must be absolute")
    output_input = Path(os.path.abspath(str(output)))
    if os.path.lexists(str(output_input)):
        raise TruthError("output must be absent")
    output = output_input.resolve()
    threshold_values = (
        args.expected_queries, args.min_positive, args.min_mixed, args.min_zero
    )
    requirements = None
    if args.workload == "property-presence" or any(
        value is not None for value in threshold_values
    ):
        requirements = {
            "expected_queries": args.expected_queries,
            "min_positive": args.min_positive,
            "min_mixed": args.min_mixed,
            "min_zero": args.min_zero,
        }
    truth = build_truth(
        args.scale,
        args.workload,
        args.property_id,
        args.sample_plan,
        args.sample_plan_sha256,
        args.reference_output,
        args.reference_output_sha256,
        args.reference_stage,
        requirements,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json_new(output, truth)
    print(json.dumps({
        "state": "FROZEN",
        "truth": str(output),
        "truth_sha256": sha256_file(output),
        "workload": args.workload,
        "expected_queries": truth["expected_queries"],
        "positive_result_samples": truth["positive_result_samples"],
        "mixed_result_samples": truth.get("mixed_result_samples"),
        "zero_result_samples": truth.get("zero_result_samples"),
    }, sort_keys=True))
    return 0


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--output", required=True, type=Path)
    value.add_argument("--scale", required=True, choices=("sf10", "sf30"))
    value.add_argument("--workload", required=True, choices=WORKLOADS)
    value.add_argument("--property-id", required=True, type=int)
    value.add_argument("--sample-plan", required=True, type=Path)
    value.add_argument("--sample-plan-sha256", required=True)
    value.add_argument("--reference-output", required=True, type=Path)
    value.add_argument("--reference-output-sha256", required=True)
    value.add_argument("--reference-stage", required=True, choices=STAGES)
    value.add_argument("--expected-queries", type=int)
    value.add_argument("--min-positive", type=int)
    value.add_argument("--min-mixed", type=int)
    value.add_argument("--min-zero", type=int)
    return value


def main():
    args = parser().parse_args()
    try:
        return run(args)
    except (TruthError, OSError) as exc:
        print("FAIL: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
