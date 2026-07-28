#!/usr/bin/env python3
"""Version an E01 lifecycle plan after an evidenced reflink failure.

This is a receipt-only builder.  It never walks, hashes, copies, chmods, or
deletes a store.  The output remains HOLD and enables an explicit full-copy
method for both stores on the same source/target filesystem.  Failed staging
from the parent attempt is retained; every fallback row receives a new,
generation-qualified staging path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence


PARENT_SCHEMA = "cidr-e01-store-lifecycle-plan-v1"
SCHEMA = "cidr-e01-store-lifecycle-plan-v2"
FULL_COPY_METHOD = "cp-archive-reflink-never"
MAX_SMALL_BYTES = 16 * 1024 * 1024
GENERATION_RE = re.compile(r"^attempt[1-9][0-9]*$")
FALSE_ELIGIBILITY = {
    "formal_eligible": False,
    "performance_eligible": False,
    "paper_claim_eligible": False,
}


class FallbackPlanError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise FallbackPlanError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path, label: str) -> Dict[str, Any]:
    require(path.is_file() and not path.is_symlink(), f"{label}: missing/symlink")
    require(0 < path.stat().st_size <= MAX_SMALL_BYTES, f"{label}: size")
    value = json.loads(path.read_text(encoding="utf-8"))
    require(type(value) is dict, f"{label}: object required")
    return value


def file_ref(path: Path) -> Dict[str, Any]:
    path = path.resolve()
    require(path.is_file() and not path.is_symlink(), f"{path}: missing/symlink")
    size = path.stat().st_size
    require(0 < size <= MAX_SMALL_BYTES, f"{path}: size")
    return {"path": str(path), "sha256": sha256_file(path), "size_bytes": size}


def _inside(child: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath((str(child.resolve()), str(parent.resolve()))) == str(
            parent.resolve()
        )
    except ValueError:
        return False


def build(
    *,
    parent_plan_path: Path,
    failure_receipt_path: Path,
    staging_generation: str,
) -> Dict[str, Any]:
    require(GENERATION_RE.fullmatch(staging_generation) is not None, "staging generation")
    parent_ref = file_ref(parent_plan_path)
    parent = load_json(parent_plan_path, "parent lifecycle plan")
    require(parent.get("schema_version") == PARENT_SCHEMA, "parent schema")
    require(parent.get("state") == "HOLD", "parent must HOLD")
    require(parent.get("strict_serial") is True, "parent strict serial")
    require(parent.get("max_live_mutable_clones") == 1, "parent clone limit")
    failure_ref = file_ref(failure_receipt_path)
    failure = load_json(failure_receipt_path, "reflink failure receipt")
    require(failure.get("state") == "FAILED_RETAINED", "failure state")
    require(failure.get("variant") == "budg-b64", "failure variant")
    require(failure.get("reason") == "reflink copy failed rc=1", "failure reason")
    require(failure.get("source_modified") is False, "failure source mutation")
    require(failure.get("staging_retained") is True, "failure staging retention")
    failed_staging = Path(str(failure.get("staging_root", ""))).resolve()
    require(failed_staging.is_dir() and not failed_staging.is_symlink(), "failed staging missing")
    failed_attempt_root = failure_receipt_path.resolve().parent
    prior_preflight_ref = file_ref(failed_attempt_root / "PREFLIGHT.json")
    prior_source_manifest_ref = file_ref(failed_attempt_root / "SOURCE-MANIFEST.json")
    prior_copy_stderr_ref = file_ref(failed_attempt_root / "COPY.stderr")
    prior_preflight = load_json(
        Path(prior_preflight_ref["path"]), "prior attempt preflight"
    )
    prior_source_manifest = load_json(
        Path(prior_source_manifest_ref["path"]), "prior fresh source manifest"
    )
    require(prior_preflight.get("state") == "PASS", "prior preflight state")
    require(prior_preflight.get("variant") == "budg-b64", "prior preflight variant")
    require(
        prior_source_manifest.get("store_sha256")
        == prior_preflight.get("expected", {}).get("store_sha256"),
        "prior fresh source SHA drift",
    )

    immutable_root = Path(str(parent.get("immutable_asset_root", ""))).resolve()
    require(immutable_root.is_absolute(), "immutable root absolute")
    require(_inside(failed_staging, immutable_root), "failed staging outside immutable root")
    stores = parent.get("stores")
    require(type(stores) is list and len(stores) == 2, "two store rows")
    by_variant = {
        row.get("variant"): row for row in stores if type(row) is dict
    }
    require(set(by_variant) == {"budg-b64", "naive"}, "store variants")
    require(
        Path(by_variant["budg-b64"]["source_root"]).resolve()
        == Path(failure["source_root"]).resolve(),
        "failure source drift",
    )
    require(
        Path(by_variant["budg-b64"]["staging_root"]).resolve() == failed_staging,
        "failed staging/parent drift",
    )

    fallback_rows = []
    for variant in ("budg-b64", "naive"):
        row = dict(by_variant[variant])
        target = Path(row["immutable_root"]).resolve()
        require(not target.exists(), f"{variant}: immutable target must be absent")
        staging = (
            immutable_root
            / f"staging-{staging_generation}"
            / f"{variant}.full-copy.tmp"
        )
        require(not staging.exists(), f"{variant}: fallback staging must be absent")
        require(staging != Path(row["staging_root"]).resolve(), f"{variant}: staging reused")
        row["staging_root"] = str(staging)
        row["copy_method"] = FULL_COPY_METHOD
        row["full_copy_fallback"] = {
            "enabled": True,
            "method": FULL_COPY_METHOD,
            "reason": "reflink_operation_not_supported",
            "failure_receipt": failure_ref,
            "prior_preflight_receipt": prior_preflight_ref,
            "prior_fresh_source_manifest": prior_source_manifest_ref,
            "prior_copy_stderr": prior_copy_stderr_ref,
            "failed_staging_retained": str(failed_staging),
            "same_filesystem_scope": True,
            "minimum_free_bytes_plus_expected_total_bytes": True,
        }
        policy = dict(row.get("copy_policy", {}))
        policy["preferred_method"] = "cp-reflink-never"
        policy["full_copy_fallback_requires_explicit_enable"] = True
        policy["full_copy_fallback_explicitly_enabled"] = True
        policy["staging_then_atomic_rename"] = True
        row["copy_policy"] = policy
        fallback_rows.append(row)

    value = dict(parent)
    value["schema_version"] = SCHEMA
    value["parent_lifecycle_plan"] = parent_ref
    value["copy_fallback"] = {
        "state": "EXPLICITLY_ENABLED",
        "method": FULL_COPY_METHOD,
        "reason": "reflink_operation_not_supported",
        "failure_receipt": failure_ref,
        "prior_preflight_receipt": prior_preflight_ref,
        "prior_fresh_source_manifest": prior_source_manifest_ref,
        "prior_copy_stderr": prior_copy_stderr_ref,
        "failed_attempt_staging_retained": str(failed_staging),
        "staging_generation": staging_generation,
        "applies_to_variants": ["budg-b64", "naive"],
    }
    value["stores"] = fallback_rows
    fallback_cells = []
    for cell in parent.get("cells", []):
        require(type(cell) is dict, "cell row object")
        row = dict(cell)
        policy = dict(row.get("clone_policy", {}))
        policy["method"] = FULL_COPY_METHOD
        policy["full_copy_fallback"] = True
        policy["full_copy_fallback_explicitly_enabled"] = True
        policy["fallback_failure_receipt"] = failure_ref
        policy["minimum_free_bytes_plus_expected_total_bytes"] = True
        row["clone_policy"] = policy
        fallback_cells.append(row)
    value["cells"] = fallback_cells
    value["large_content_read_now"] = False
    value["large_copy_performed_now"] = False
    value["source_store_modified_now"] = False
    value["blockers"] = list(parent.get("blockers", [])) + [
        "full-copy fallback not yet executed",
    ]
    value.update(FALSE_ELIGIBILITY)
    return validate(value, verify_refs=False)


def validate(value: Mapping[str, Any], *, verify_refs: bool = True) -> Dict[str, Any]:
    require(type(value) is dict, "fallback plan object")
    require(value.get("schema_version") == SCHEMA, "fallback schema")
    require(value.get("state") == "HOLD", "fallback plan must HOLD")
    require(value.get("strict_serial") is True, "strict serial")
    require(value.get("max_live_mutable_clones") == 1, "one clone")
    for key in FALSE_ELIGIBILITY:
        require(value.get(key) is False, f"{key} must remain false")
    if verify_refs:
        require(
            file_ref(Path(value["parent_lifecycle_plan"]["path"]))
            == value["parent_lifecycle_plan"],
            "parent plan ref drift",
        )
        require(
            file_ref(Path(value["copy_fallback"]["failure_receipt"]["path"]))
            == value["copy_fallback"]["failure_receipt"],
            "failure ref drift",
        )
        for key in (
            "prior_preflight_receipt",
            "prior_fresh_source_manifest",
            "prior_copy_stderr",
        ):
            require(
                file_ref(Path(value["copy_fallback"][key]["path"]))
                == value["copy_fallback"][key],
                f"{key} drift",
            )
    fallback = value.get("copy_fallback")
    require(type(fallback) is dict, "copy fallback contract")
    require(fallback.get("state") == "EXPLICITLY_ENABLED", "fallback state")
    require(fallback.get("method") == FULL_COPY_METHOD, "fallback method")
    stores = value.get("stores")
    require(
        type(stores) is list
        and [row.get("variant") for row in stores if type(row) is dict]
        == ["budg-b64", "naive"],
        "store order",
    )
    old_staging = Path(fallback["failed_attempt_staging_retained"]).resolve()
    for row in stores:
        require(row.get("copy_method") == FULL_COPY_METHOD, "row copy method")
        contract = row.get("full_copy_fallback")
        require(type(contract) is dict and contract.get("enabled") is True, "row fallback")
        require(contract.get("method") == FULL_COPY_METHOD, "row fallback method")
        require(
            contract.get("minimum_free_bytes_plus_expected_total_bytes") is True,
            "row capacity reserve",
        )
        require(Path(row["staging_root"]).resolve() != old_staging, "failed staging reuse")
        require(not Path(row["staging_root"]).exists(), "fallback staging must be absent")
        require(not Path(row["immutable_root"]).exists(), "target must be absent")
    cells = value.get("cells")
    require(type(cells) is list, "cell rows")
    for row in cells:
        policy = row.get("clone_policy")
        require(type(policy) is dict, "cell clone policy")
        require(policy.get("method") == FULL_COPY_METHOD, "cell full-copy method")
        require(policy.get("full_copy_fallback") is True, "cell fallback enabled")
        require(
            policy.get("minimum_free_bytes_plus_expected_total_bytes") is True,
            "cell capacity reserve",
        )
    require(value.get("large_content_read_now") is False, "large read forbidden")
    require(value.get("large_copy_performed_now") is False, "large copy forbidden")
    require(value.get("source_store_modified_now") is False, "source mutation forbidden")
    return dict(value)


def atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    require(not path.exists(), f"refusing overwrite: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-plan", type=Path, required=True)
    parser.add_argument("--failure-receipt", type=Path, required=True)
    parser.add_argument("--staging-generation", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        value = build(
            parent_plan_path=args.parent_plan.resolve(),
            failure_receipt_path=args.failure_receipt.resolve(),
            staging_generation=args.staging_generation,
        )
        validate(value)
        atomic_write(args.output.resolve(), value)
    except (FallbackPlanError, OSError, ValueError) as error:
        print(f"E01 FALLBACK PLAN BLOCKED: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "state": value["state"],
                "schema_version": value["schema_version"],
                "copy_method": value["copy_fallback"]["method"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
