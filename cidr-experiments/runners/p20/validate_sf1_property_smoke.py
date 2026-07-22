#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path


class GateError(Exception):
    pass


def reject_symlink_components(path, label):
    absolute = Path(os.path.abspath(str(path)))
    parts = absolute.parts
    current = Path(parts[0])
    for part in parts[1:]:
        current = current / part
        if os.path.lexists(str(current)) and current.is_symlink():
            raise GateError("{} path contains symlink component: {}".format(label, current))
    return absolute


def load_json(path, label):
    unresolved = reject_symlink_components(path, label)
    path = unresolved.resolve(strict=True)
    if not path.is_file():
        raise GateError("{} must be a regular non-symlink file".format(label))
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GateError("{} is not valid UTF-8 JSON: {}".format(label, error))
    if not isinstance(value, dict):
        raise GateError("{} must be a JSON object".format(label))
    return path, hashlib.sha256(raw).hexdigest(), value


def require(condition, message):
    if not condition:
        raise GateError(message)


def validate_import(receipt, property_id):
    materialization = receipt.get("property_materialization")
    require(isinstance(materialization, dict), "import receipt has no property materialization")
    require(materialization.get("profile") == "snb-knows-creation-date-v1", "profile drift")
    require(materialization.get("property_id") == property_id, "property id drift")
    require(materialization.get("edge_type") == 1, "property owner edge type drift")
    values = materialization.get("materialized_property_values")
    if values is None:
        values = materialization.get("materialized_property_values_per_store")
    topology_only = materialization.get("topology_only_directed_edges")
    if topology_only is None:
        topology_only = materialization.get("topology_only_directed_edges_per_store")
    require(isinstance(values, int) and values > 0, "materialized property count is vacuous")
    require(isinstance(topology_only, int) and topology_only > 0, "no property-absent records")
    nonzero = materialization.get("property_bitmap_nonzero_segments_per_store")
    zero = materialization.get("property_bitmap_zero_segments_per_store")
    require(isinstance(nonzero, list) and nonzero, "missing nonzero bitmap distribution")
    require(isinstance(zero, list) and len(zero) == len(nonzero), "bitmap distribution mismatch")
    require(all(isinstance(value, int) and value > 0 for value in nonzero), "a store has no property segment")
    require(all(isinstance(value, int) and value > 0 for value in zero), "a store has no exact-negative segment")
    try:
        store = reject_symlink_components(receipt.get("data_dir", ""), "import store").resolve(strict=True)
    except (TypeError, OSError):
        raise GateError("import receipt has invalid data_dir")
    snapshot = receipt.get("snapshot")
    require(str(store) != str(Path(".").resolve()), "import receipt has empty data_dir")
    require(isinstance(snapshot, int) and snapshot > 0, "import receipt has invalid snapshot")
    require(receipt.get("directed_edges") == snapshot, "import snapshot/directed edge count drift")
    return values, topology_only, nonzero, zero, store, snapshot


def validate_reopen(audit, property_id, store, snapshot):
    require(audit.get("profile") == "snb-knows-creation-date-v1", "reopen profile drift")
    require(audit.get("reopened") is True, "audit is not from a reopened process")
    require(audit.get("property_id") == property_id, "reopen property id drift")
    require(audit.get("edge_type") == 1, "reopen property owner drift")
    require(audit.get("property_bitmap_nonzero_segments", 0) > 0, "reopen lost property bitmap")
    require(audit.get("property_bitmap_zero_segments", 0) > 0, "reopen lost exact-negative segments")
    try:
        audit_store = reject_symlink_components(audit.get("data_dir", ""), "audit store").resolve(strict=True)
    except (TypeError, OSError):
        raise GateError("reopen audit has invalid data_dir")
    require(audit_store == store, "reopen audit store identity drift")
    require(audit.get("snapshot") == snapshot, "reopen audit snapshot drift")


def validate_plan(plan, property_plan):
    entries = plan.get("entries")
    require(isinstance(entries, list) and entries, "sample plan has no entries")
    total = 0
    for index, entry in enumerate(entries):
        require(isinstance(entry, dict), "sample plan entry {} is invalid".format(index))
        if property_plan:
            require(entry.get("edge_type") is None, "property plan must use edge_type=null")
        samples = entry.get("samples")
        require(isinstance(samples, list) and samples, "sample plan entry has no samples")
        total += len(samples)
    if not property_plan:
        require(any(entry.get("edge_type") is not None for entry in entries), "typed plan has no typed entry")
    return entries, total


def validate_result(result, plan_path, plan_entries, property_id, property_result, store, snapshot):
    expected_mode = "presence" if property_result else "none"
    expected_id = property_id if property_result else 0
    require(result.get("emit_result_digests") is True, "result digests were not emitted")
    require(result.get("property_predicate_mode") == expected_mode, "property mode drift")
    require(result.get("property_id") == expected_id, "result property id drift")
    require(result.get("workload_mode") == "one_hop", "workload mode drift")
    require(result.get("sample_plan_version") == 1, "sample plan version drift")
    require(result.get("scan_requested") is False, "formal smoke result must use frozen sample-plan input")
    try:
        bound_plan = reject_symlink_components(result.get("sample_plan_in", ""), "result sample plan").resolve(strict=True)
        result_store = reject_symlink_components(result.get("data_dir", ""), "result store").resolve(strict=True)
    except (TypeError, OSError):
        raise GateError("result has invalid path binding")
    require(bound_plan == plan_path, "result sample_plan_in identity drift")
    require(result_store == store, "result store identity drift")
    require(result.get("snapshot") == snapshot, "result snapshot drift")
    benchmarks = result.get("benchmarks")
    require(isinstance(benchmarks, list) and len(benchmarks) == len(plan_entries), "benchmark coverage drift")
    positive_queries = 0
    zero_queries = 0
    mixed_queries = 0
    positive_records = 0
    absent_records_lower_bound = 0
    signature = []
    for entry, benchmark in zip(plan_entries, benchmarks):
        require(benchmark.get("edge_type") == entry.get("edge_type"), "benchmark edge type drift")
        require(benchmark.get("property_predicate_mode") == expected_mode, "benchmark mode drift")
        require(benchmark.get("property_id") == expected_id, "benchmark property id drift")
        digests = benchmark.get("result_digests")
        samples = entry.get("samples")
        require(benchmark.get("sample_degrees") == samples, "benchmark sample_degrees drift")
        require(benchmark.get("sampled_srcs") == [item.get("src") for item in samples], "benchmark source order drift")
        require(benchmark.get("sampled_vertices") == len(samples), "benchmark sampled_vertices drift")
        require(isinstance(digests, list) and len(digests) == len(samples), "sample digest coverage drift")
        for sample, digest in zip(samples, digests):
            count = digest.get("result_count")
            degree = sample.get("degree")
            require(isinstance(count, int) and count >= 0, "invalid result count")
            require(isinstance(degree, int) and degree >= 0, "invalid sample degree")
            require(digest.get("src") == sample.get("src"), "sample source drift")
            require(digest.get("degree") == degree, "sample digest degree drift")
            require(digest.get("edge_type") == entry.get("edge_type"), "sample digest edge type drift")
            require(digest.get("property_predicate_mode") == expected_mode, "sample digest property mode drift")
            signature.append((digest.get("src"), count, digest.get("result_digest")))
            if count > 0:
                positive_queries += 1
                positive_records += count
            else:
                zero_queries += 1
            if property_result:
                require(count <= degree, "property result exceeds all-edge degree")
                absent_records_lower_bound += degree - count
                if count > 0 and count < degree:
                    mixed_queries += 1
    return {
        "positive_queries": positive_queries,
        "zero_queries": zero_queries,
        "mixed_queries": mixed_queries,
        "positive_records": positive_records,
        "absent_records_lower_bound": absent_records_lower_bound,
        "signature": signature,
    }


def write_new(path, value):
    path = Path(path).resolve()
    if path.exists():
        raise GateError("output already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--import-receipt", required=True)
    parser.add_argument("--reopen-audit", required=True)
    parser.add_argument("--property-plan", required=True)
    parser.add_argument("--property-result", required=True)
    parser.add_argument("--typed-plan", required=True)
    parser.add_argument("--typed-result", required=True)
    parser.add_argument("--property-id", type=int, default=5)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    import_path, import_sha, import_receipt = load_json(args.import_receipt, "import receipt")
    audit_path, audit_sha, audit = load_json(args.reopen_audit, "reopen audit")
    property_plan_path, property_plan_sha, property_plan = load_json(args.property_plan, "property plan")
    property_result_path, property_result_sha, property_result = load_json(args.property_result, "property result")
    typed_plan_path, typed_plan_sha, typed_plan = load_json(args.typed_plan, "typed plan")
    typed_result_path, typed_result_sha, typed_result = load_json(args.typed_result, "typed result")
    require(property_plan_sha != typed_plan_sha, "property and typed plans must have distinct SHA-256")

    values, topology_only, bitmap_nonzero, bitmap_zero, store, snapshot = validate_import(import_receipt, args.property_id)
    validate_reopen(audit, args.property_id, store, snapshot)
    property_entries, property_queries = validate_plan(property_plan, True)
    typed_entries, typed_queries = validate_plan(typed_plan, False)
    property_stats = validate_result(
        property_result, property_plan_path, property_entries, args.property_id, True, store, snapshot
    )
    typed_stats = validate_result(
        typed_result, typed_plan_path, typed_entries, args.property_id, False, store, snapshot
    )
    require(property_stats["positive_queries"] > 0, "property workload has no positive query")
    require(property_stats["zero_queries"] > 0, "property workload has no negative query")
    require(property_stats["mixed_queries"] > 0, "property workload has no source with present and absent records")
    require(property_stats["absent_records_lower_bound"] > 0, "property workload has no property-absent record")
    require(property_stats["signature"] != typed_stats["signature"], "property and typed workload result distributions are identical")

    output = {
        "schema_version": 1,
        "gate": "p20-sf1-property-smoke-v1",
        "status": "PASS",
        "property_id": args.property_id,
        "materialized_property_values_per_store": values,
        "topology_only_directed_edges_per_store": topology_only,
        "bitmap_nonzero_segments_per_store": bitmap_nonzero,
        "bitmap_zero_segments_per_store": bitmap_zero,
        "property_queries": property_queries,
        "typed_queries": typed_queries,
        "property_positive_queries": property_stats["positive_queries"],
        "property_positive_rate": property_stats["positive_queries"] / property_queries,
        "property_zero_queries": property_stats["zero_queries"],
        "property_mixed_queries": property_stats["mixed_queries"],
        "property_positive_records": property_stats["positive_records"],
        "property_absent_records_lower_bound": property_stats["absent_records_lower_bound"],
        "sample_plan_role": "smoke_only",
        "performance_eligible": False,
        "formal_plan_eligible": False,
        "inputs": {
            "import_receipt": {"path": str(import_path), "sha256": import_sha},
            "reopen_audit": {"path": str(audit_path), "sha256": audit_sha},
            "property_plan": {"path": str(property_plan_path), "sha256": property_plan_sha},
            "property_result": {"path": str(property_result_path), "sha256": property_result_sha},
            "typed_plan": {"path": str(typed_plan_path), "sha256": typed_plan_sha},
            "typed_result": {"path": str(typed_result_path), "sha256": typed_result_sha},
        },
    }
    write_new(args.output, output)
    print(json.dumps({"status": "PASS", "output": str(Path(args.output).resolve())}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (GateError, OSError, ValueError) as error:
        print("ERROR: {}".format(error), file=sys.stderr)
        sys.exit(2)
