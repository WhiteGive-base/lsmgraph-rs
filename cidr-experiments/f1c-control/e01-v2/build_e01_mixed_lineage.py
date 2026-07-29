#!/usr/bin/env python3
"""Build the read-only Figure 1 mixed-lineage composition plan.

The builder consumes the frozen 18-cell L5 composition and only hashes the
small receipts that it names explicitly.  It never writes below the legacy
root and never creates a formal timing root.  The result is intentionally
HOLD until the four-cell incremental campaign supplies fresh sealed evidence.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SOURCE_SCHEMA = "cidr-l5-conditional-split-lineage-composition-v1"
OUTPUT_SCHEMA = "cidr-e01-mixed-lineage-composition-v1"
LEGACY_SYSTEMS = ("seml0", "livegraph", "aster", "tugraph", "neo4j", "nebulagraph")
LEGACY_KEYS = tuple(
    f"{system}:r{repeat}" for system in LEGACY_SYSTEMS for repeat in (1, 2, 3)
)
INCREMENTAL_CELLS = (
    ("seml0:bridge-canary", "seml0", 1, "comparability_gate", False),
    ("seml0-naive:r1", "seml0-naive", 1, "figure_row", True),
    ("seml0-naive:r2", "seml0-naive", 2, "figure_row", True),
    ("seml0-naive:r3", "seml0-naive", 3, "figure_row", True),
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CompositionError(RuntimeError):
    pass


def _pairs_no_duplicates(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise CompositionError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def read_json(path: Path, label: str) -> Dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_pairs_no_duplicates
        )
    except (OSError, ValueError) as exc:
        raise CompositionError(f"{label}: cannot read strict JSON {path}: {exc}") from exc
    if type(value) is not dict:
        raise CompositionError(f"{label}: top-level object required")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_ref(path: Path, label: str, expected: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    path = path.resolve()
    if not path.is_file() or path.is_symlink():
        raise CompositionError(f"{label}: regular non-symlink file required: {path}")
    actual = {
        "path": str(path),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }
    if expected is not None:
        if expected.get("path") != str(path):
            raise CompositionError(f"{label}: path drift")
        if expected.get("sha256") != actual["sha256"]:
            raise CompositionError(f"{label}: SHA-256 drift")
        if expected.get("size_bytes") != actual["size_bytes"]:
            raise CompositionError(f"{label}: size drift")
    return actual


def verified_ref(value: Any, label: str) -> Dict[str, Any]:
    if type(value) is not dict:
        raise CompositionError(f"{label}: receipt reference object required")
    for key in ("path", "sha256", "size_bytes"):
        if key not in value:
            raise CompositionError(f"{label}: missing {key}")
    if type(value["path"]) is not str or not Path(value["path"]).is_absolute():
        raise CompositionError(f"{label}: absolute frozen path required")
    if type(value["sha256"]) is not str or not SHA256_RE.fullmatch(value["sha256"]):
        raise CompositionError(f"{label}: invalid SHA-256")
    if type(value["size_bytes"]) is not int or value["size_bytes"] < 0:
        raise CompositionError(f"{label}: invalid size")
    return file_ref(Path(value["path"]), label, value)


def request_ref(validated: Mapping[str, Any], label: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    declared = validated.get("request")
    if type(declared) is not dict:
        raise CompositionError(f"{label}.request: object required")
    path = Path(str(declared.get("path", "")))
    ref = file_ref(path, f"{label}.request")
    if declared.get("sha256") != ref["sha256"]:
        raise CompositionError(f"{label}.request: validated-result backlink drift")
    return ref, read_json(path, f"{label}.request")


def _required_sha(value: Any, label: str) -> str:
    if type(value) is not str or not SHA256_RE.fullmatch(value):
        raise CompositionError(f"{label}: SHA-256 required")
    return value


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    if type(value) not in (int, float) or isinstance(value, bool):
        raise CompositionError(f"{label}: number required")
    result = float(value)
    if positive and result <= 0:
        raise CompositionError(f"{label}: positive number required")
    return result


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise CompositionError(f"{label}: integer >= {minimum} required")
    return value


def _stable_adapter_provenance(
    repeat: Mapping[str, Any],
    validated: Mapping[str, Any],
    conditional_bridge: Optional[Mapping[str, Any]],
    label: str,
) -> Dict[str, Any]:
    if conditional_bridge is not None and type(conditional_bridge.get("adapter_provenance")) is dict:
        return verified_ref(
            conditional_bridge["adapter_provenance"],
            f"{label}.conditional_bridge.adapter_provenance",
        )
    artifacts = validated.get("adapter_artifacts")
    if type(artifacts) is not dict:
        raise CompositionError(f"{label}.adapter_artifacts: object required")
    return verified_ref(
        artifacts.get("adapter-provenance.json"),
        f"{label}.adapter_artifacts.adapter-provenance.json",
    )


def _p31_bridge(
    repeat: Mapping[str, Any], validated: Mapping[str, Any], label: str
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    p31 = validated.get("p31")
    if type(p31) is not dict:
        raise CompositionError(f"{label}.p31: object required")
    embedded_validation_sha = _required_sha(
        p31.get("validation_sha256"), f"{label}.p31.validation_sha256"
    )
    if repeat.get("p31_validation") is not None:
        validation = verified_ref(repeat["p31_validation"], f"{label}.p31_validation")
        done = verified_ref(repeat.get("p31_done"), f"{label}.p31_done")
        validation_value = read_json(Path(validation["path"]), f"{label}.p31_validation")
        if validation_value.get("state") != "PASS":
            raise CompositionError(f"{label}: direct P31 validation is not PASS")
        if embedded_validation_sha != validation["sha256"]:
            raise CompositionError(f"{label}: P31 validation backlink drift")
        return (
            {
                "mode": "direct_pass_receipts",
                "state": "PASS",
                "embedded_validation_sha256": embedded_validation_sha,
                "validation": validation,
                "done": done,
                "conditional_revalidation": None,
                "post_timing_revalidation": None,
                "original_validation": None,
                "original_failed_marker": None,
                "original_artifacts_modified": False,
            },
            None,
        )

    conditional_ref = verified_ref(
        repeat.get("conditional_p31_revalidation"),
        f"{label}.conditional_p31_revalidation",
    )
    conditional = read_json(
        Path(conditional_ref["path"]), f"{label}.conditional_p31_revalidation"
    )
    post_ref = verified_ref(
        repeat.get("post_timing_revalidation"), f"{label}.post_timing_revalidation"
    )
    post = read_json(Path(post_ref["path"]), f"{label}.post_timing_revalidation")
    original_validation = verified_ref(
        repeat.get("original_p31_validation_preserved"),
        f"{label}.original_p31_validation_preserved",
    )
    original_failed = verified_ref(
        repeat.get("original_p31_failed_marker_preserved"),
        f"{label}.original_p31_failed_marker_preserved",
    )
    if embedded_validation_sha != original_validation["sha256"]:
        raise CompositionError(f"{label}: preserved P31 validation backlink drift")
    if conditional.get("state") != "PASS" or post.get("state") != "PASS":
        raise CompositionError(f"{label}: read-only P31 bridge is not PASS")
    if conditional.get("original_artifacts_modified") is not False:
        raise CompositionError(f"{label}: P31 bridge modified original artifacts")
    for document_name, document in (("conditional", conditional), ("post", post)):
        for eligibility in ("formal_eligible", "performance_eligible", "paper_claim_eligible"):
            if document.get(eligibility) is not False:
                raise CompositionError(
                    f"{label}: {document_name} bridge must preserve {eligibility}=false"
                )
    return (
        {
            "mode": "read_only_post_timing_revalidation",
            "state": "PASS",
            "embedded_validation_sha256": embedded_validation_sha,
            "validation": None,
            "done": None,
            "conditional_revalidation": conditional_ref,
            "post_timing_revalidation": post_ref,
            "original_validation": original_validation,
            "original_failed_marker": original_failed,
            "original_artifacts_modified": False,
        },
        conditional,
    )


def _failed_markers(
    repeat: Mapping[str, Any], p31_bridge: Mapping[str, Any], label: str
) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    source = repeat.get("source_root_failed_marker_preserved")
    if source is not None:
        result.append(
            {
                "kind": "source_run_failed_marker",
                "receipt": verified_ref(source, f"{label}.source_root_failed_marker"),
                "preserved": True,
            }
        )
    original = p31_bridge.get("original_failed_marker")
    if original is not None:
        result.append(
            {
                "kind": "original_p31_failed_marker",
                "receipt": original,
                "preserved": True,
            }
        )
    return result


def _legacy_cell(repeat: Mapping[str, Any]) -> Dict[str, Any]:
    system = repeat.get("system_id")
    repeat_index = repeat.get("repeat_index")
    label = f"legacy[{system}:r{repeat_index}]"
    if system not in LEGACY_SYSTEMS:
        raise CompositionError(f"{label}: unexpected system")
    _integer(repeat_index, f"{label}.repeat_index", minimum=1)
    if repeat_index not in (1, 2, 3):
        raise CompositionError(f"{label}: repeat must be 1..3")
    validated_ref = verified_ref(repeat.get("validated_result"), f"{label}.validated_result")
    validated = read_json(Path(validated_ref["path"]), f"{label}.validated_result")
    if validated.get("system_id") != system or validated.get("repeat_index") != repeat_index:
        raise CompositionError(f"{label}: validated identity drift")
    request, request_value = request_ref(validated, label)
    if request_value.get("system_id") != system or request_value.get("repeat_index") != repeat_index:
        raise CompositionError(f"{label}: request identity drift")
    if request_value.get("interface_scope") != "typed-neighbor-dense-id-v1":
        raise CompositionError(f"{label}: interface drift")
    p31_bridge, conditional = _p31_bridge(repeat, validated, label)
    provenance = _stable_adapter_provenance(repeat, validated, conditional, label)
    dataset = request_value.get("dataset")
    truth = request_value.get("truth")
    binary = request_value.get("binary")
    if not all(type(item) is dict for item in (dataset, truth, binary)):
        raise CompositionError(f"{label}: request dataset/truth/binary objects required")
    request_timing = request_value.get("timing")
    if type(request_timing) is not dict:
        raise CompositionError(f"{label}: request timing object required")
    per_query_timeout_ms = _integer(
        request_timing.get("per_query_timeout_ms"),
        f"{label}.request.per_query_timeout_ms",
        minimum=1,
    )
    if validated.get("per_query_timeout_ms") != per_query_timeout_ms:
        raise CompositionError(f"{label}: request/validated deadline drift")
    expected_digest = _required_sha(
        validated.get("expected_digest_sha256"), f"{label}.expected_digest"
    )
    actual_digest = _required_sha(
        validated.get("actual_digest_sha256"), f"{label}.actual_digest"
    )
    mismatch = _integer(validated.get("mismatch_queries"), f"{label}.mismatch_queries")
    timeout = _integer(validated.get("timeout_queries"), f"{label}.timeout_queries")
    completed = _integer(
        validated.get("completed_queries"), f"{label}.completed_queries", minimum=1
    )
    query_count = _integer(validated.get("query_count"), f"{label}.query_count", minimum=1)
    if expected_digest != actual_digest or mismatch != 0 or timeout != 0 or completed != query_count:
        raise CompositionError(f"{label}: correctness is not complete")
    p31 = validated["p31"]
    host = p31.get("host")
    repo = p31.get("repo")
    if type(host) is not dict or type(repo) is not dict:
        raise CompositionError(f"{label}: P31 host/repo binding required")
    return {
        "cell_key": f"{system}:r{repeat_index}",
        "system_id": system,
        "repeat_index": repeat_index,
        "lineage_class": "legacy_validated",
        "classification": {
            "campaign_lineage": "conditional_historical",
            "formal_eligible": False,
            "performance_eligible": False,
            "paper_claim_eligible": False,
            "formal_upgrade_performed": False,
        },
        "identity": {
            "display_name": validated.get("display_name"),
            "system_version": validated.get("system_version"),
            "host_fingerprint": _required_sha(
                host.get("fingerprint_sha256"), f"{label}.host_fingerprint"
            ),
            "git_sha": repo.get("git_sha"),
            "binary_sha256": _required_sha(binary.get("sha256"), f"{label}.binary.sha256"),
            "physical_input_sha256": _required_sha(
                dataset.get("sha256"), f"{label}.dataset.sha256"
            ),
            "truth_sha256": _required_sha(truth.get("sha256"), f"{label}.truth.sha256"),
            "adapter_request": request,
            "adapter_provenance": provenance,
        },
        "protocol": {
            "interface_scope": validated.get("interface_scope"),
            "concurrency": validated.get("concurrency"),
            "query_count": query_count,
            "warmup_passes": validated.get("warmup_passes"),
            "measured_passes": validated.get("measured_passes"),
            "per_query_timeout_ms": per_query_timeout_ms,
            "clock": validated.get("clock"),
            "timing_boundary": validated.get("timing_boundary"),
            "process_lifetime": validated.get("process_lifetime"),
        },
        "metrics": {
            "latency_p50_us": _number(
                validated.get("latency_p50_us"), f"{label}.latency_p50_us", positive=True
            ),
            "latency_p95_us": _number(
                validated.get("latency_p95_us"), f"{label}.latency_p95_us", positive=True
            ),
            "latency_p99_us": _number(
                validated.get("latency_p99_us"), f"{label}.latency_p99_us", positive=True
            ),
            "measurement_s": _number(
                validated.get("measurement_s"), f"{label}.measurement_s", positive=True
            ),
            "warmup_s": _number(validated.get("warmup_s"), f"{label}.warmup_s"),
            "qps": _number(validated.get("qps"), f"{label}.qps", positive=True),
            "completed_queries": completed,
            "timeout_queries": timeout,
            "mismatch_queries": mismatch,
            "expected_digest_sha256": expected_digest,
            "actual_digest_sha256": actual_digest,
            "import_wall_s": validated.get("import_wall_s"),
            "import_store_logical_bytes": validated.get("import_store_logical_bytes"),
            "import_store_allocated_bytes": validated.get("import_store_allocated_bytes"),
        },
        "receipts": {
            "validated_result": validated_ref,
            "p31_bridge": p31_bridge,
            "failed_marker_lineage": _failed_markers(repeat, p31_bridge, label),
        },
    }


def _logical_dataset(
    cells: Sequence[Mapping[str, Any]],
    dataset_manifest_path: Path,
    dense_summary_path: Path,
) -> Dict[str, Any]:
    dataset_manifest_ref = file_ref(dataset_manifest_path, "logical dataset source manifest")
    dataset_manifest = read_json(dataset_manifest_path, "logical dataset source manifest")
    dense_summary_ref = file_ref(dense_summary_path, "dense conversion summary")
    dense_summary = read_json(dense_summary_path, "dense conversion summary")
    if dataset_manifest.get("schema_version") != "p02b-dataset-manifest-v1":
        raise CompositionError("logical dataset source manifest schema drift")
    if dense_summary.get("vertex_count") != 29987835 or dense_summary.get("edge_count") != 355185382:
        raise CompositionError("dense conversion cardinality drift")
    physical: Dict[str, List[str]] = {}
    truths = set()
    expected_digests = set()
    hosts = set()
    protocols = set()
    for cell in cells:
        identity = cell["identity"]
        physical.setdefault(identity["physical_input_sha256"], []).append(cell["system_id"])
        truths.add(identity["truth_sha256"])
        expected_digests.add(cell["metrics"]["expected_digest_sha256"])
        hosts.add(identity["host_fingerprint"])
        protocols.add(
            (
                cell["protocol"]["interface_scope"],
                cell["protocol"]["concurrency"],
                cell["protocol"]["query_count"],
                cell["protocol"]["warmup_passes"],
                cell["protocol"]["measured_passes"],
                cell["protocol"]["clock"],
                cell["protocol"]["timing_boundary"],
            )
        )
    if len(truths) != 1 or len(expected_digests) != 1 or len(hosts) != 1 or len(protocols) != 1:
        raise CompositionError("legacy logical dataset/protocol invariants drifted")
    dense_sha = "9727fed3d710a3f0897b1ee7379411a091e0568bef05902de8b1787efc39d258"
    source_sha = dataset_manifest.get("dataset_sha256")
    if set(physical) != {dense_sha, source_sha}:
        raise CompositionError(f"unexpected physical input representations: {sorted(physical)}")
    if set(physical[dense_sha]) != set(LEGACY_SYSTEMS) - {"neo4j"}:
        raise CompositionError("dense input is not bound to the expected five systems")
    if set(physical[source_sha]) != {"neo4j"}:
        raise CompositionError("source-tree input is not bound only to Neo4j")
    return {
        "logical_dataset_id": "ldbc-snb-sf10-typed-neighbor-logical-v1",
        "scale_factor": 10,
        "vertex_count": dense_summary["vertex_count"],
        "directed_edge_count": dense_summary["edge_count"],
        "query_count": 1700,
        "interface_scope": "typed-neighbor-dense-id-v1",
        "truth_sha256": next(iter(truths)),
        "expected_digest_sha256": next(iter(expected_digests)),
        "equivalence_scope": "query-interface-correctness-and-cardinality-v1",
        "physical_byte_identity_required": False,
        "source_tree_manifest": dataset_manifest_ref,
        "dense_conversion_summary": dense_summary_ref,
        "physical_representations": [
            {
                "kind": "dense_typed_edge_list",
                "sha256": dense_sha,
                "systems": sorted(set(physical[dense_sha])),
            },
            {
                "kind": "ldbc_source_tree",
                "sha256": source_sha,
                "systems": ["neo4j"],
            },
        ],
        "checks": {
            "common_truth_sha256": "PASS",
            "common_expected_digest_sha256": "PASS",
            "zero_mismatch_and_timeout": "PASS",
            "common_host_and_timing_protocol": "PASS",
            "physical_representation_disclosed": "PASS",
            "byte_equivalence_claimed": False,
        },
    }


def _system_import_lineage(cells: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    for system in LEGACY_SYSTEMS:
        selected = [cell for cell in cells if cell["system_id"] == system]
        physical = {cell["identity"]["physical_input_sha256"] for cell in selected}
        binary = {cell["identity"]["binary_sha256"] for cell in selected}
        version = {cell["identity"]["system_version"] for cell in selected}
        if len(physical) != 1 or len(binary) != 1 or len(version) != 1:
            raise CompositionError(f"{system}: system import identity drift across repeats")
        result.append(
            {
                "system_id": system,
                "physical_input_kind": (
                    "ldbc_source_tree" if system == "neo4j" else "dense_typed_edge_list"
                ),
                "physical_input_sha256": next(iter(physical)),
                "binary_sha256": next(iter(binary)),
                "system_version": next(iter(version)),
                "repeat_adapter_request_sha256": [
                    cell["identity"]["adapter_request"]["sha256"] for cell in selected
                ],
                "repeat_adapter_provenance_sha256": [
                    cell["identity"]["adapter_provenance"]["sha256"] for cell in selected
                ],
                "binding_scope": "frozen-request-and-adapter-provenance-v1",
            }
        )
    return result


def _incremental_plan() -> Dict[str, Any]:
    canary_contract: Dict[str, Any] = {
        "schema_version": "cidr-e01-bridge-canary-comparability-contract-v1",
        "frozen_before_timing": True,
        "candidate_cell_key": "seml0:bridge-canary",
        "legacy_reference_keys": ["seml0:r1", "seml0:r2", "seml0:r3"],
        "identity_exact_match": [
            "logical_dataset_id",
            "truth_sha256",
            "query_count",
            "interface_scope",
            "concurrency",
            "warmup_passes",
            "measured_passes",
            "per_query_timeout_ms",
            "clock",
            "timing_boundary",
            "host_fingerprint",
        ],
        "correctness": {
            "completed_queries": 1700,
            "timeout_queries": 0,
            "mismatch_queries": 0,
            "expected_digest_equals_actual": True,
        },
        "performance_rule": {
            "baseline_statistic": "median_of_legacy_seml0_r1_r2_r3",
            "ratio_definition": "fresh_canary_over_legacy_median",
            "completed_qps_min": 0.85,
            "completed_qps_max": 1.15,
            "latency_p50_min": 0.67,
            "latency_p50_max": 1.50,
            "latency_p95_min": 0.67,
            "latency_p95_max": 1.50,
            "latency_p99_min": 0.50,
            "latency_p99_max": 2.00,
            "all_bounds_inclusive": True,
        },
        "failure_policy": {
            "normalizer_output": "REJECT",
            "automatic_strict_21_rerun": False,
            "targeted_fallback": "fresh_seml0_r1_r2_r3_plus_seml0_naive_r1_r2_r3",
        },
    }
    canary_contract["contract_sha256"] = hashlib.sha256(
        json.dumps(canary_contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "state": "HOLD",
        "reason": "fresh assets, gates, lease, four receipts, and canary comparison are absent",
        "timing_cell_count": 4,
        "figure_row_count": 3,
        "canary_count": 1,
        "cells": [
            {
                "cell_key": key,
                "system_id": system,
                "repeat_index": repeat,
                "role": role,
                "included_in_figure_rows": included,
                "required_execution_mode": "formal",
                "required_fresh_receipts": [
                    "asset_seal",
                    "adapter_identity",
                    "p03",
                    "p02b",
                    "batch_lease",
                    "resource_gate",
                    "validated_result",
                    "p31",
                    "cleanup",
                ],
            }
            for key, system, repeat, role, included in INCREMENTAL_CELLS
        ],
        "pre_output_policy": {
            "formal_root_creation": "REJECT_UNTIL_FRESH_ASSETS_AND_GATES_PASS",
            "timing_start": "REJECT_UNTIL_FRESH_ASSETS_AND_GATES_PASS",
            "normalizer_output": "REJECT_UNTIL_ALL_4_CELLS_AND_CANARY_GATE_PASS",
            "legacy_root_mutation": "FORBIDDEN",
            "strict_all_formal_claim": "FORBIDDEN",
        },
        "required_campaign_gates": [
            "fresh_asset_seal",
            "adapter_identity",
            "fresh_p03",
            "fresh_p02b",
            "fresh_batch_lease",
            "fresh_resource_gate",
            "strict_serial_scheduler",
            "all_four_cells",
            "bridge_canary_comparability",
            "cleanup",
        ],
        "bridge_canary_comparability_contract": canary_contract,
        "incremental_evidence": {
            "schema_version": "cidr-e01-incremental-evidence-v2",
            "state": "ABSENT",
            "reason": "formal MATRIX-DONE has not been adapted and assembled",
        },
    }


def build_composition(
    source_path: Path,
    dataset_manifest_path: Path,
    dense_summary_path: Path,
    *,
    created_at_utc: Optional[str] = None,
) -> Dict[str, Any]:
    source_path = source_path.resolve()
    source = read_json(source_path, "legacy composition")
    if source.get("schema_version") != SOURCE_SCHEMA or source.get("state") != "PASS":
        raise CompositionError("legacy source is not the frozen PASS composition")
    if source.get("repeat_count") != 18 or source.get("source_data_copied") is not False:
        raise CompositionError("legacy source count/copy policy drift")
    for key in ("formal_eligible", "performance_eligible", "paper_claim_eligible"):
        if source.get(key) is not False:
            raise CompositionError(f"legacy source unexpectedly has {key}=true")
    repeats = source.get("repeats")
    if type(repeats) is not list or len(repeats) != 18:
        raise CompositionError("legacy source must contain 18 repeats")
    indexed = {}
    for repeat in repeats:
        if type(repeat) is not dict:
            raise CompositionError("legacy repeat object required")
        key = f"{repeat.get('system_id')}:r{repeat.get('repeat_index')}"
        if key in indexed:
            raise CompositionError(f"duplicate legacy cell: {key}")
        indexed[key] = repeat
    if set(indexed) != set(LEGACY_KEYS):
        raise CompositionError("legacy 18-cell key set drift")
    cells = [_legacy_cell(indexed[key]) for key in LEGACY_KEYS]
    logical = _logical_dataset(cells, dataset_manifest_path.resolve(), dense_summary_path.resolve())
    created = created_at_utc or dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    result = {
        "schema_version": OUTPUT_SCHEMA,
        "state": "HOLD",
        "experiment_id": "E01",
        "composition_id": "E01-F1-MIXED-LINEAGE-INCREMENTAL-V1",
        "created_at_utc": created,
        "classification": {
            "lineage": "mixed_legacy_validated_plus_fresh_incremental",
            "formal_eligible": False,
            "performance_eligible": False,
            "paper_claim_eligible": False,
            "legacy_formal_upgrade_performed": False,
            "strict_all_formal_claim": False,
        },
        "legacy_source": file_ref(source_path, "legacy composition"),
        "logical_dataset_identity": logical,
        "system_import_lineage": _system_import_lineage(cells),
        "legacy_cells": cells,
        "incremental_plan": _incremental_plan(),
        "output_contract": {
            "expected_figure_row_count": 21,
            "legacy_figure_row_count": 18,
            "fresh_figure_row_count": 3,
            "bridge_canary_row_count": 1,
            "bridge_canary_included_in_figure": False,
            "lineage_column_required": True,
            "mixed_lineage_disclosure_required": True,
        },
    }
    return result


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--dataset-manifest", type=Path, required=True)
    parser.add_argument("--dense-conversion-summary", type=Path, required=True)
    parser.add_argument("--created-at-utc")
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        result = build_composition(
            args.source,
            args.dataset_manifest,
            args.dense_conversion_summary,
            created_at_utc=args.created_at_utc,
        )
        payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.output is None:
            print(payload, end="")
            return 0
        output = args.output.resolve()
        if output.exists():
            raise CompositionError(f"refusing to overwrite output: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
        print(json.dumps({"state": result["state"], "output": str(output)}, sort_keys=True))
        return 0
    except (CompositionError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
