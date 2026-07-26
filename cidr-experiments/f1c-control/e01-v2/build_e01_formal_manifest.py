#!/usr/bin/env python3
"""Build a frozen, fail-closed E01 formal launch manifest.

This is an engineering-only builder.  It reads only small, explicit receipts,
does not run a benchmark, and deliberately keeps all eligibility flags false.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping


SYSTEMS = (
    ("seml0", "SemL0"),
    ("seml0-naive", "SemL0-naive"),
    ("livegraph", "LiveGraph"),
    ("aster", "Aster RocksGraph"),
    ("tugraph", "TuGraph"),
    ("nebulagraph", "NebulaGraph"),
    ("neo4j", "Neo4j"),
)
REPEATS = (1, 2, 3)
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_RECEIPT_BYTES = 16 * 1024 * 1024
FALSE_ELIGIBILITY = {
    "formal_eligible": False,
    "performance_eligible": False,
    "paper_claim_eligible": False,
}


class BuildError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise BuildError(message)


def require_exact_keys(
    value: Mapping[str, Any], required: set[str], label: str
) -> None:
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required)
    require(not missing, f"{label}: missing keys {missing}")
    require(not extra, f"{label}: unexpected keys {extra}")


def require_false_eligibility(value: Mapping[str, Any], label: str) -> None:
    for key, expected in FALSE_ELIGIBILITY.items():
        require(value.get(key) is expected, f"{label}: {key} must be false")


def require_sha1(value: Any, label: str) -> str:
    require(type(value) is str and SHA1_RE.fullmatch(value) is not None, f"{label}: bad SHA-1")
    return value


def require_sha256(value: Any, label: str) -> str:
    require(
        type(value) is str and SHA256_RE.fullmatch(value) is not None,
        f"{label}: bad SHA-256",
    )
    return value


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return sha256_bytes(payload)


def load_json(path: Path, label: str) -> dict[str, Any]:
    require(path.is_file(), f"{label}: missing file {path}")
    require(not path.is_symlink(), f"{label}: symlink is forbidden")
    size = path.stat().st_size
    require(size <= MAX_RECEIPT_BYTES, f"{label}: receipt exceeds size limit")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError(f"{label}: cannot read strict JSON: {exc}") from exc
    require(type(value) is dict, f"{label}: top-level object required")
    return value


def receipt_ref(path: Path, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    require(path.is_file(), f"{label}: missing file {path}")
    require(not path.is_symlink(), f"{label}: symlink is forbidden")
    resolved = path.resolve()
    value = load_json(resolved, label)
    payload = resolved.read_bytes()
    return (
        {
            "path": str(resolved),
            "sha256": sha256_bytes(payload),
            "size_bytes": len(payload),
        },
        value,
    )


def atomic_json_exclusive(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def validate_protocol(protocol: Mapping[str, Any]) -> str:
    required = {
        "dataset_id",
        "scale_factor",
        "vertex_count",
        "directed_edge_count",
        "property_count",
        "query_count",
        "input_sha256",
        "workload_id",
        "query_trace_sha256",
        "seed",
        "cache_state",
        "concurrency",
        "interface_scope",
        "host_fingerprint",
        "harness_git_sha",
    }
    require_exact_keys(protocol, required, "campaign protocol")
    for key in ("dataset_id", "workload_id", "interface_scope"):
        require(type(protocol[key]) is str and protocol[key].strip(), f"protocol.{key}: text required")
    for key in ("vertex_count", "directed_edge_count", "query_count", "concurrency"):
        require(type(protocol[key]) is int and protocol[key] > 0, f"protocol.{key}: positive integer required")
    for key in ("property_count", "seed"):
        require(type(protocol[key]) is int and protocol[key] >= 0, f"protocol.{key}: nonnegative integer required")
    require(
        type(protocol["scale_factor"]) in (int, float)
        and not isinstance(protocol["scale_factor"], bool)
        and protocol["scale_factor"] > 0,
        "protocol.scale_factor: positive number required",
    )
    require(protocol["cache_state"] in ("cold", "warm", "fixed_budget"), "protocol.cache_state: unsupported")
    require_sha256(protocol["input_sha256"], "protocol.input_sha256")
    require_sha256(protocol["query_trace_sha256"], "protocol.query_trace_sha256")
    require_sha256(protocol["host_fingerprint"], "protocol.host_fingerprint")
    require_sha1(protocol["harness_git_sha"], "protocol.harness_git_sha")
    return canonical_sha256(protocol)


def validate_fresh_receipt(
    value: Mapping[str, Any],
    *,
    schema: str,
    campaign_id: str,
    protocol_sha: str,
    host_sha: str,
    git_sha: str,
    label: str,
) -> None:
    require(value.get("schema_version") == schema, f"{label}: schema drift")
    require(value.get("state") == "PASS", f"{label}: state is not PASS")
    require(value.get("campaign_id") == campaign_id, f"{label}: campaign drift")
    require(value.get("protocol_sha256") == protocol_sha, f"{label}: protocol drift")
    require(value.get("host_fingerprint") == host_sha, f"{label}: host drift")
    require(value.get("git_sha") == git_sha, f"{label}: git drift")
    require(value.get("fresh") is True, f"{label}: fresh=true required")
    require_false_eligibility(value, label)


def validate_asset_seal(
    value: Mapping[str, Any],
    *,
    campaign_id: str,
    kind: str,
    asset_id: str,
    content_sha: str,
    protocol_sha: str,
    label: str,
) -> None:
    require(value.get("schema_version") == "cidr-e01-asset-seal-v1", f"{label}: schema drift")
    require(value.get("state") == "SEALED", f"{label}: state is not SEALED")
    require(value.get("campaign_id") == campaign_id, f"{label}: campaign drift")
    require(value.get("asset_kind") == kind, f"{label}: asset kind drift")
    require(value.get("asset_id") == asset_id, f"{label}: asset id drift")
    require(value.get("protocol_sha256") == protocol_sha, f"{label}: protocol drift")
    require(value.get("content_sha256") == content_sha, f"{label}: content SHA drift")
    require(value.get("immutable") is True, f"{label}: immutable=true required")
    require(value.get("fresh_for_campaign") is True, f"{label}: fresh_for_campaign=true required")
    require_false_eligibility(value, label)


def build_manifest(spec_path: Path, output: Path) -> dict[str, Any]:
    require(spec_path.is_file(), f"campaign spec: missing file {spec_path}")
    require(not spec_path.is_symlink(), "campaign spec: symlink is forbidden")
    resolved_spec = spec_path.resolve()
    spec_root = resolved_spec.parent
    spec = load_json(resolved_spec, "campaign spec")
    required = {
        "schema_version",
        "state",
        "campaign_id",
        "created_at_utc",
        "classification",
        "protocol",
        "repeat_indices",
        "serial_formal_timing",
        "legacy_conditional_evidence_allowed",
        "admission",
        "dataset_seal",
        "trace_seal",
        "systems",
    }
    require_exact_keys(spec, required, "campaign spec")
    require(spec["schema_version"] == "cidr-e01-formal-launch-spec-v1", "campaign spec: schema drift")
    require(spec["state"] == "READY", "campaign spec: state must be READY")
    campaign_id = spec["campaign_id"]
    require(type(campaign_id) is str and campaign_id.strip(), "campaign_id: text required")
    require(type(spec["created_at_utc"]) is str and spec["created_at_utc"].endswith("Z"), "created_at_utc: UTC Z timestamp required")
    require(type(spec["classification"]) is dict, "classification: object required")
    require_false_eligibility(spec["classification"], "campaign classification")
    require(spec["repeat_indices"] == list(REPEATS), "repeat order/count drift")
    require(spec["serial_formal_timing"] is True, "serial_formal_timing=true required")
    require(spec["legacy_conditional_evidence_allowed"] is False, "legacy conditional evidence is forbidden")
    protocol = spec["protocol"]
    require(type(protocol) is dict, "protocol: object required")
    protocol_sha = validate_protocol(protocol)
    host_sha = protocol["host_fingerprint"]
    git_sha = protocol["harness_git_sha"]

    def resolve_ref(raw: Any, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
        require(type(raw) is str and raw, f"{label}: path required")
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = spec_root / candidate
        return receipt_ref(candidate, label)

    admission = spec["admission"]
    require(type(admission) is dict, "admission: object required")
    require_exact_keys(
        admission, {"p03_clean_ready", "p02b_gate", "batch_lease", "lease_marker"}, "admission"
    )
    p03_ref, p03 = resolve_ref(admission["p03_clean_ready"], "P03 clean-ready receipt")
    validate_fresh_receipt(
        p03,
        schema="cidr-e01-p03-clean-ready-v1",
        campaign_id=campaign_id,
        protocol_sha=protocol_sha,
        host_sha=host_sha,
        git_sha=git_sha,
        label="P03 clean-ready receipt",
    )
    gate_ref, gate = resolve_ref(admission["p02b_gate"], "P02B gate receipt")
    validate_fresh_receipt(
        gate,
        schema="cidr-e01-p02b-gate-v1",
        campaign_id=campaign_id,
        protocol_sha=protocol_sha,
        host_sha=host_sha,
        git_sha=git_sha,
        label="P02B gate receipt",
    )
    require(gate.get("p03_receipt_sha256") == p03_ref["sha256"], "P02B gate: P03 lineage mismatch")
    lease_ref, lease = resolve_ref(admission["batch_lease"], "batch lease")
    validate_fresh_receipt(
        lease,
        schema="cidr-e01-formal-lease-v1",
        campaign_id=campaign_id,
        protocol_sha=protocol_sha,
        host_sha=host_sha,
        git_sha=git_sha,
        label="batch lease",
    )
    require(lease.get("p02b_gate_sha256") == gate_ref["sha256"], "batch lease: P02B lineage mismatch")
    require(lease.get("exclusive_single_host") is True, "batch lease: exclusive_single_host=true required")
    require(lease.get("serial_formal_timing") is True, "batch lease: serial_formal_timing=true required")
    marker_ref, marker = resolve_ref(admission["lease_marker"], "lease marker")
    require(marker.get("schema_version") == "cidr-e01-formal-lease-marker-v1", "lease marker: schema drift")
    require(marker.get("state") == "PASS", "lease marker: state is not PASS")
    require(marker.get("campaign_id") == campaign_id, "lease marker: campaign drift")
    require(marker.get("lease_sha256") == lease_ref["sha256"], "lease marker: lease lineage mismatch")
    require_false_eligibility(marker, "lease marker")

    dataset_ref, dataset_seal = resolve_ref(spec["dataset_seal"], "dataset seal")
    validate_asset_seal(
        dataset_seal,
        campaign_id=campaign_id,
        kind="dataset",
        asset_id=protocol["dataset_id"],
        content_sha=protocol["input_sha256"],
        protocol_sha=protocol_sha,
        label="dataset seal",
    )
    trace_ref, trace_seal = resolve_ref(spec["trace_seal"], "trace seal")
    validate_asset_seal(
        trace_seal,
        campaign_id=campaign_id,
        kind="query_trace",
        asset_id=protocol["workload_id"],
        content_sha=protocol["query_trace_sha256"],
        protocol_sha=protocol_sha,
        label="trace seal",
    )

    systems = spec["systems"]
    require(type(systems) is list and len(systems) == len(SYSTEMS), "systems: exactly seven required")
    require(
        [(row.get("system_key"), row.get("display_name")) for row in systems if type(row) is dict]
        == list(SYSTEMS),
        "systems: fixed order/name drift",
    )
    frozen_systems = []
    for row in systems:
        require_exact_keys(
            row,
            {
                "system_key",
                "display_name",
                "variant",
                "binary_sha256",
                "store_sha256",
                "binary_seal",
                "store_seal",
            },
            f"system {row.get('system_key')}",
        )
        key = row["system_key"]
        require(type(row["variant"]) is str and row["variant"].strip(), f"{key}: variant required")
        binary_sha = require_sha256(row["binary_sha256"], f"{key}.binary_sha256")
        store_sha = require_sha256(row["store_sha256"], f"{key}.store_sha256")
        binary_ref, binary_seal = resolve_ref(row["binary_seal"], f"{key} binary seal")
        validate_asset_seal(
            binary_seal,
            campaign_id=campaign_id,
            kind="binary",
            asset_id=key,
            content_sha=binary_sha,
            protocol_sha=protocol_sha,
            label=f"{key} binary seal",
        )
        store_ref, store_seal = resolve_ref(row["store_seal"], f"{key} store seal")
        validate_asset_seal(
            store_seal,
            campaign_id=campaign_id,
            kind="store",
            asset_id=key,
            content_sha=store_sha,
            protocol_sha=protocol_sha,
            label=f"{key} store seal",
        )
        frozen_systems.append(
            {
                "system_key": key,
                "display_name": row["display_name"],
                "variant": row["variant"],
                "binary_sha256": binary_sha,
                "store_sha256": store_sha,
                "binary_seal": binary_ref,
                "store_seal": store_ref,
            }
        )

    runs = []
    for system in frozen_systems:
        for repeat in REPEATS:
            runs.append(
                {
                    "ordinal": len(runs) + 1,
                    "run_key": f"{system['system_key']}:r{repeat}",
                    "system_key": system["system_key"],
                    "display_name": system["display_name"],
                    "variant": system["variant"],
                    "repeat_index": repeat,
                    "host_fingerprint": host_sha,
                    "dataset_sha256": protocol["input_sha256"],
                    "query_trace_sha256": protocol["query_trace_sha256"],
                    "cache_state": protocol["cache_state"],
                    "concurrency": protocol["concurrency"],
                    "interface_scope": protocol["interface_scope"],
                    "git_sha": git_sha,
                    "binary_sha256": system["binary_sha256"],
                    "store_sha256": system["store_sha256"],
                    **FALSE_ELIGIBILITY,
                }
            )
    require(len(runs) == 21, "internal error: expected 21 runs")

    manifest = {
        "schema_version": "cidr-e01-formal-launch-manifest-v1",
        "state": "PASS",
        "campaign_id": campaign_id,
        "created_at_utc": spec["created_at_utc"],
        "source_spec": {
            "path": str(resolved_spec),
            "sha256": sha256_bytes(resolved_spec.read_bytes()),
            "size_bytes": resolved_spec.stat().st_size,
        },
        "protocol": protocol,
        "protocol_sha256": protocol_sha,
        "systems": frozen_systems,
        "repeat_indices": list(REPEATS),
        "run_count": 21,
        "serial_formal_timing": True,
        "legacy_conditional_evidence_allowed": False,
        "admission": {
            "p03_clean_ready": p03_ref,
            "p02b_gate": gate_ref,
            "batch_lease": lease_ref,
            "lease_marker": marker_ref,
        },
        "asset_seals": {"dataset": dataset_ref, "query_trace": trace_ref},
        "runs": runs,
        **FALSE_ELIGIBILITY,
    }
    atomic_json_exclusive(output.resolve(), manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = build_manifest(args.spec, args.output)
    print(
        json.dumps(
            {
                "state": "PASS",
                "output": str(args.output.resolve()),
                "run_count": manifest["run_count"],
                "manifest_sha256": sha256_bytes(args.output.resolve().read_bytes()),
                **FALSE_ELIGIBILITY,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (BuildError, OSError, ValueError) as exc:
        print(f"E01 FORMAL MANIFEST BUILD FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
