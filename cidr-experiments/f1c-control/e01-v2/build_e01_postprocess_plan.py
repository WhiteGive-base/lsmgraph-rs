#!/usr/bin/env python3
"""Build the fail-closed E01 composition/tidy/renderer/QA interface plan.

Only small source files and HOLD plans are hashed.  No matrix result, figure,
renderer, or QA process is started.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence


SCHEMA = "cidr-e01-postprocess-interface-plan-v1"
BOUNDARY_SCHEMA = "cidr-e01-exact-timing-boundary-plan-v1"
MAX_SMALL_FILE_BYTES = 16 * 1024 * 1024
NORMALIZED_COLUMNS = (
    "experiment_id",
    "run_key",
    "system_key",
    "repeat_index",
    "lineage_class",
    "plot_role",
    "formal_eligible",
    "performance_eligible",
    "paper_claim_eligible",
    "host_fingerprint",
    "git_sha",
    "binary_sha256",
    "physical_input_sha256",
    "truth_sha256",
    "interface_scope",
    "concurrency",
    "query_count",
    "warmup_passes",
    "measured_passes",
    "measurement_s",
    "warmup_s",
    "latency_p50_us",
    "latency_p95_us",
    "latency_p99_us",
    "completed_qps",
    "completed_queries",
    "timeout_queries",
    "mismatch_queries",
    "validated_result_sha256",
    "p31_evidence_sha256",
)
RENDERER_EXTRA_COLUMNS = (
    "run_id",
    "timestamp_utc",
    "system",
    "variant",
    "dataset_id",
    "input_sha256",
    "workload_id",
    "query_trace_sha256",
    "vertex_count",
    "directed_edge_count",
    "property_count",
    "seed",
    "cache_state",
    "scale_factor",
    "digest_pass",
    "mismatch_count",
    "load_wall_s",
    "final_disk_bytes",
    "system_version",
)
FALSE_ELIGIBILITY = {
    "formal_eligible": False,
    "performance_eligible": False,
    "paper_claim_eligible": False,
}


class PostprocessError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PostprocessError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path, label: str) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PostprocessError(f"{label}: cannot read JSON: {exc}") from exc
    require(type(value) is dict, f"{label}: object required")
    return value


def file_ref(path: Path, label: str) -> Dict[str, Any]:
    path = path.resolve()
    require(path.is_file(), f"{label}: file missing")
    require(not path.is_symlink(), f"{label}: symlink forbidden")
    size = path.stat().st_size
    require(0 < size <= MAX_SMALL_FILE_BYTES, f"{label}: small file size invalid")
    return {"path": str(path), "sha256": sha256_file(path), "size_bytes": size}


def verify_ref(value: Any, label: str) -> Dict[str, Any]:
    require(type(value) is dict, f"{label}: reference required")
    require(set(value) == {"path", "sha256", "size_bytes"}, f"{label}: keys drift")
    actual = file_ref(Path(value["path"]), label)
    require(actual == value, f"{label}: path/size/SHA drift")
    return actual


def _f1_contract_fields(requirements_path: Path) -> list[str]:
    with requirements_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        require(reader.fieldnames is not None, "figure requirements header missing")
        rows = [row for row in reader if row.get("figure_id") in {"COMMON", "F1"}]
    require(rows, "F1 requirements missing")
    if "field_name" in reader.fieldnames:
        fields = [
            row["field_name"]
            for row in rows
            if row.get("required", "").strip().lower() == "yes"
            and row.get("field_name")
        ]
        require(fields, "F1 required field set is empty")
        return list(dict.fromkeys(fields))
    fields: list[str] = []
    for row in rows:
        raw = row.get("required_fields") or row.get("fields") or ""
        for field in raw.replace(",", " ").split():
            if field not in fields:
                fields.append(field)
    return fields


def build(
    *,
    mixed_plan_path: Path,
    boundary_plan_path: Path,
    production_backend_path: Path,
    canary_evaluator_path: Path,
    normalizer_path: Path,
    renderer_path: Path,
    plot_support_path: Path,
    render_validator_path: Path,
    figure_requirements_path: Path,
    output_root: Path,
) -> Dict[str, Any]:
    mixed_ref = file_ref(mixed_plan_path, "mixed plan")
    mixed = load_json(mixed_plan_path, "mixed plan")
    require(mixed.get("schema_version") == "cidr-e01-mixed-lineage-composition-v1", "mixed plan schema drift")
    require(mixed.get("state") == "HOLD", "input mixed plan must HOLD")
    boundary_ref = file_ref(boundary_plan_path, "timing boundary plan")
    boundary = load_json(boundary_plan_path, "timing boundary plan")
    require(boundary.get("schema_version") == BOUNDARY_SCHEMA, "boundary schema drift")
    require(boundary.get("state") == "HOLD", "boundary plan must HOLD")
    require(output_root.is_absolute(), "absolute output root required")
    output_root = output_root.resolve()
    require(not output_root.exists(), "postprocess output root must remain absent")
    tools = {
        "production_backend": file_ref(production_backend_path, "production backend"),
        "canary_evaluator": file_ref(canary_evaluator_path, "canary evaluator"),
        "mixed_normalizer": file_ref(normalizer_path, "mixed normalizer"),
        "figure1_renderer": file_ref(renderer_path, "Figure 1 renderer"),
        "plot_support": file_ref(plot_support_path, "plot support"),
        "render_validator": file_ref(render_validator_path, "render validator"),
        "figure_data_requirements": file_ref(figure_requirements_path, "figure requirements"),
        "incremental_postprocess": file_ref(
            Path(__file__).resolve().parent / "postprocess_e01_incremental.py",
            "incremental postprocess",
        ),
    }
    frozen_fields = _f1_contract_fields(figure_requirements_path)
    required_tidy = sorted(set(NORMALIZED_COLUMNS) | set(RENDERER_EXTRA_COLUMNS) | set(frozen_fields))
    missing_after_normalizer = sorted(set(required_tidy) - set(NORMALIZED_COLUMNS))
    require("load_wall_s" in missing_after_normalizer, "cost field gap must remain explicit")
    require("final_disk_bytes" in missing_after_normalizer, "disk field gap must remain explicit")
    return {
        "schema_version": SCHEMA,
        "state": "HOLD",
        "execution_state": "CORE_IMPLEMENTED_REMAINDER_HOLD",
        "experiment_id": "E01",
        "output_root": str(output_root),
        "mixed_lineage_plan": mixed_ref,
        "timing_boundary_plan": boundary_ref,
        "tools": tools,
        "stage_order": [
            "matrix-evidence-adapter",
            "bridge-canary-evaluator",
            "pass-composition-assembler",
            "mixed-lineage-normalizer",
            "cost-lineage-admission",
            "frozen-tidy-transform",
            "figure1-renderer",
            "rendered-figure-qa",
            "claim-receipt",
        ],
        "interfaces": {
            "matrix_evidence_adapter": {
                "state": "IMPLEMENTED_NOT_RUN",
                "tool": tools["incremental_postprocess"],
                "input": "4/4 production MATRIX-DONE plus seven receipts per cell",
                "output": "cidr-e01-incremental-evidence-v2",
            },
            "bridge_canary_evaluator": {
                "state": "IMPLEMENTED_NOT_RUN",
                "tool": tools["canary_evaluator"],
            },
            "pass_composition_assembler": {
                "state": "IMPLEMENTED_NOT_RUN",
                "tool": tools["incremental_postprocess"],
                "input": "frozen HOLD plan plus incremental evidence and canary PASS",
                "output": "PASS cidr-e01-mixed-lineage-composition-v1",
            },
            "mixed_lineage_normalizer": {
                "state": "IMPLEMENTED_NOT_RUN",
                "tool": tools["mixed_normalizer"],
                "row_contract": "legacy18+fresh-naive3",
                "row_count": 21,
                "output_columns": list(NORMALIZED_COLUMNS),
            },
            "cost_lineage_admission": {
                "state": "NOT_IMPLEMENTED",
                "required_fields": ["load_wall_s", "final_disk_bytes"],
                "historical_cost_implicit_reuse_forbidden": True,
                "fresh_or_explicitly_admitted_cost_receipt_required": True,
            },
            "frozen_tidy_transform": {
                "state": "NOT_IMPLEMENTED",
                "required_output_columns": required_tidy,
                "missing_after_current_normalizer": missing_after_normalizer,
                "eligibility_upgrade_forbidden": True,
            },
            "figure1_renderer": {
                "state": "IMPLEMENTED_NOT_RUN",
                "tool": tools["figure1_renderer"],
                "plot_support": tools["plot_support"],
                "input": str(output_root / "E01-figure1-frozen-tidy.tsv"),
                "output_dir": str(output_root / "figure"),
            },
            "rendered_figure_qa": {
                "state": "IMPLEMENTED_REQUIRES_EXPLICIT_LINUX_TOOLS",
                "tool": tools["render_validator"],
                "pdfinfo": None,
                "pdftoppm": None,
                "visual_qa_receipt": None,
            },
            "claim_receipt": {
                "state": "NOT_IMPLEMENTED",
                "mixed_lineage_disclosure_required": True,
                "paper_claim_eligible": False,
            },
        },
        "large_content_read_now": False,
        "renderer_invoked": False,
        "qa_invoked": False,
        "blockers": [
            "4/4 production MATRIX-DONE absent",
            "SemL0-naive cost receipt/admission absent",
            "frozen tidy transformer not implemented",
            "explicit Linux Poppler/Pillow QA binding absent",
            "mixed-lineage claim receipt and user policy decision absent",
        ],
        **FALSE_ELIGIBILITY,
    }


def validate(value_or_path: Any, *, verify_inputs: bool = True) -> Dict[str, Any]:
    value = (
        load_json(value_or_path.resolve(), "postprocess plan")
        if isinstance(value_or_path, Path)
        else value_or_path
    )
    require(type(value) is dict, "postprocess plan object required")
    require(value.get("schema_version") == SCHEMA, "postprocess schema drift")
    require(value.get("state") == "HOLD", "postprocess plan must HOLD")
    require(
        value.get("execution_state") == "CORE_IMPLEMENTED_REMAINDER_HOLD",
        "execution state drift",
    )
    require(value.get("large_content_read_now") is False, "large read forbidden")
    require(value.get("renderer_invoked") is False, "renderer invocation forbidden")
    require(value.get("qa_invoked") is False, "QA invocation forbidden")
    for key in FALSE_ELIGIBILITY:
        require(value.get(key) is False, f"{key} must remain false")
    require(not Path(value["output_root"]).exists(), "output root must remain absent")
    if verify_inputs:
        verify_ref(value.get("mixed_lineage_plan"), "mixed plan")
        verify_ref(value.get("timing_boundary_plan"), "timing boundary plan")
        for name, descriptor in value.get("tools", {}).items():
            verify_ref(descriptor, name)
    interfaces = value.get("interfaces")
    require(type(interfaces) is dict, "interfaces required")
    for name in ("matrix_evidence_adapter", "pass_composition_assembler"):
        require(
            interfaces.get(name, {}).get("state") == "IMPLEMENTED_NOT_RUN",
            f"{name} implementation state drift",
        )
        verify_ref(interfaces[name].get("tool"), f"{name} tool")
    tidy = interfaces.get("frozen_tidy_transform")
    require(tidy.get("state") == "NOT_IMPLEMENTED", "tidy must remain blocked")
    missing = set(tidy.get("missing_after_current_normalizer", []))
    require({"run_id", "load_wall_s", "final_disk_bytes"} <= missing, "critical tidy gaps missing")
    cost = interfaces.get("cost_lineage_admission")
    require(cost.get("historical_cost_implicit_reuse_forbidden") is True, "implicit cost reuse forbidden")
    require(interfaces.get("claim_receipt", {}).get("paper_claim_eligible") is False, "claim gate drift")
    require(type(value.get("blockers")) is list and value["blockers"], "HOLD blockers required")
    return value


def atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    require(not path.exists(), f"refusing to overwrite output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mixed-plan", type=Path, required=True)
    parser.add_argument("--boundary-plan", type=Path, required=True)
    parser.add_argument("--production-backend", type=Path, required=True)
    parser.add_argument("--canary-evaluator", type=Path, required=True)
    parser.add_argument("--normalizer", type=Path, required=True)
    parser.add_argument("--renderer", type=Path, required=True)
    parser.add_argument("--plot-support", type=Path, required=True)
    parser.add_argument("--render-validator", type=Path, required=True)
    parser.add_argument("--figure-requirements", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        value = build(
            mixed_plan_path=args.mixed_plan.resolve(),
            boundary_plan_path=args.boundary_plan.resolve(),
            production_backend_path=args.production_backend.resolve(),
            canary_evaluator_path=args.canary_evaluator.resolve(),
            normalizer_path=args.normalizer.resolve(),
            renderer_path=args.renderer.resolve(),
            plot_support_path=args.plot_support.resolve(),
            render_validator_path=args.render_validator.resolve(),
            figure_requirements_path=args.figure_requirements.resolve(),
            output_root=args.output_root,
        )
        validate(value)
        atomic_write(args.output.resolve(), value)
        print(json.dumps({"state": "HOLD", "renderer_invoked": False, "qa_invoked": False}, sort_keys=True))
        return 0
    except (PostprocessError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
