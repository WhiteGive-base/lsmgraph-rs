#!/usr/bin/env python3
"""Fail-closed validation and resolution of CIDR P20 canonical profiles."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


STAGE_IDS = [f"A{i}" for i in range(7)]
FEATURE_ORDER = [
    "exact_evidence_admission",
    "semantic_routing",
    "budgeted_degree_promotion",
    "feedback_priority",
    "semantic_compaction",
    "automatic_lifecycle_control",
]
EXPECTED_FIXED_INPUTS = {
    "sample_plan": "shared-per-scale-and-workload",
    "truth": "shared-per-scale-and-workload",
    "import_order": "shared-per-scale",
    "io_backend": "blocking",
    "semantic_budget_profile": "frozen-v1",
    "store_isolation": "clone-one-pristine-store-per-stage-and-independent-run",
}
EXPECTED_MODES = {
    "latency": {
        "performance_eligible": True,
        "query_cpu_phases": False,
        "automatic_maintenance": "a6-only",
        "post_round_manual_compaction": False,
        "max_query_streams": 1,
        "allowed_stages": STAGE_IDS,
    },
    "cpu-phase": {
        "performance_eligible": False,
        "query_cpu_phases": True,
        "automatic_maintenance": "never",
        "post_round_manual_compaction": False,
        "max_query_streams": 1,
        "allowed_stages": STAGE_IDS[:-1],
    },
    "closed-loop-resource": {
        "performance_eligible": True,
        "query_cpu_phases": False,
        "automatic_maintenance": "always",
        "post_round_manual_compaction": False,
        "max_query_streams": 1,
        "allowed_stages": ["A6"],
    },
    "correctness": {
        "performance_eligible": False,
        "query_cpu_phases": False,
        "automatic_maintenance": "a6-only",
        "post_round_manual_compaction": False,
        "max_query_streams": 1,
        "allowed_stages": STAGE_IDS,
    },
}
EXPECTED_WORKLOADS = {
    "typed-one-hop": {
        "args": ["--workload-mode", "one-hop", "--emit-result-digests"],
        "required_bindings": [],
    },
    "degree-stratified": {
        "args": [
            "--workload-mode",
            "one-hop",
            "--semantic-degree-hint",
            "--sample-plan-degree-hint",
            "--emit-result-digests",
        ],
        "required_bindings": [],
    },
    "property-presence": {
        "args": [
            "--workload-mode",
            "one-hop",
            "--property-predicate-mode",
            "presence",
            "--property-id",
            "${PROPERTY_ID}",
            "--emit-result-digests",
        ],
        "required_bindings": ["PROPERTY_ID"],
    },
}
EXPECTED_PRECONDITION = {
    "A0": ("none", None),
    "A1": ("none", None),
    "A2": ("none", None),
    "A3": ("fixed-trace-adaptation-control", None),
    "A4": ("fixed-trace-feedback-adaptation", "legacy-global"),
    "A5": ("fixed-trace-manual-feedback-compaction", "semantic-partitioned"),
    "A6": ("fixed-trace-automatic-maintenance", None),
}
PLACEHOLDER = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")
FORBIDDEN_WORKLOAD_ARGS = {
    "--l0-layout",
    "--query-control-stage",
    "--automatic-maintenance",
    "--auto-compact",
    "--query-cpu-phases",
    "--training-runs",
    "--training-feedback-compactions",
}


class ProfileError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ProfileError(message)


def require_keys(
    value: Any, *, required: set[str], allowed: set[str] | None = None, context: str
) -> None:
    require(isinstance(value, dict), f"{context} must be an object")
    allowed = required if allowed is None else allowed
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - allowed)
    require(not missing, f"{context}: missing keys: {', '.join(missing)}")
    require(not unknown, f"{context}: unknown keys: {', '.join(unknown)}")


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProfileError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_profiles(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"cannot load {path}: {exc}") from exc
    require(isinstance(doc, dict), "top-level JSON must be an object")
    return doc


def validate(doc: dict[str, Any]) -> None:
    require_keys(
        doc,
        required={
            "schema_version",
            "experiment_id",
            "description",
            "fixed_inputs",
            "feature_order",
            "stages",
            "modes",
            "workloads",
            "matrix",
        },
        context="top level",
    )
    require(doc.get("schema_version") == 1, "schema_version must be exactly 1")
    require(doc.get("experiment_id") == "P20-G2-QUERY-CONTROL-STAIRCASE", "experiment_id changed")
    require(isinstance(doc.get("description"), str) and doc["description"], "description must be non-empty")
    require_keys(
        doc["fixed_inputs"],
        required={
            "sample_plan",
            "truth",
            "import_order",
            "io_backend",
            "semantic_budget_profile",
            "store_isolation",
        },
        context="fixed_inputs",
    )
    require(doc["fixed_inputs"] == EXPECTED_FIXED_INPUTS, "fixed_inputs values changed")
    require(doc.get("feature_order") == FEATURE_ORDER, "feature_order changed or reordered")

    stages = doc.get("stages")
    require(isinstance(stages, list), "stages must be a list")
    require([stage.get("id") for stage in stages] == STAGE_IDS, "stages must be exactly A0..A6")

    for index, stage in enumerate(stages):
        stage_id = STAGE_IDS[index]
        require_keys(
            stage,
            required={"id", "label", "l0_layout", "features", "pre_measurement"},
            context=f"stage {stage_id}",
        )
        require(isinstance(stage.get("label"), str) and stage["label"], f"{stage_id}: label must be non-empty")
        expected_layout = "semantic-budgeted"
        require(stage.get("l0_layout") == expected_layout, f"{stage_id}: l0_layout must be {expected_layout}")
        expected_features = [feature_index < index for feature_index in range(6)]
        require(stage.get("features") == expected_features, f"{stage_id}: features are not cumulative one-switch staircase")

        pre = stage.get("pre_measurement")
        require(isinstance(pre, dict), f"{stage_id}: pre_measurement must be an object")
        expected_kind, expected_output = EXPECTED_PRECONDITION[stage_id]
        if stage_id in {"A0", "A1", "A2"}:
            require_keys(pre, required={"kind"}, context=f"{stage_id}.pre_measurement")
        elif stage_id == "A3":
            require_keys(
                pre,
                required={
                    "kind",
                    "trace",
                    "training_runs",
                    "training_start_cache_state",
                    "adaptation_prestate",
                    "feedback",
                    "feedback_compactions",
                    "paired_stage",
                    "claim_scope",
                },
                context=f"{stage_id}.pre_measurement",
            )
        elif stage_id == "A4":
            require_keys(
                pre,
                required={
                    "kind",
                    "trace",
                    "training_runs",
                    "training_start_cache_state",
                    "adaptation_prestate",
                    "feedback",
                    "feedback_compactions",
                    "paired_stage",
                    "compaction_output",
                    "claim_scope",
                },
                context=f"{stage_id}.pre_measurement",
            )
        elif stage_id == "A5":
            require_keys(
                pre,
                required={"kind", "trace", "compaction_output"},
                context=f"{stage_id}.pre_measurement",
            )
        else:
            require_keys(
                pre,
                required={"kind", "trace", "settle"},
                context=f"{stage_id}.pre_measurement",
            )
        require(pre.get("kind") == expected_kind, f"{stage_id}: invalid pre_measurement.kind")
        if stage_id == "A3":
            require(pre.get("trace") == "shared-training-trace", "A3: shared training trace required")
            require(pre.get("training_runs") == 1, "A3: exactly one training replay required")
            require(
                pre.get("training_start_cache_state") == "fresh-clone-process-start"
                and pre.get("adaptation_prestate") == "post-identical-training-trace",
                "A3: paired cache/adaptation prestate drift",
            )
            require(pre.get("feedback") == "disabled", "A3: feedback must remain disabled")
            require(pre.get("feedback_compactions") == 0, "A3: feedback compaction must remain disabled")
            require(pre.get("paired_stage") == "A4", "A3: must identify A4 paired treatment")
            require(
                pre.get("claim_scope") == "control-for-feedback-driven-adaptation",
                "A3: claim scope drift",
            )
        if stage_id == "A4":
            require(pre.get("training_runs") == 1, "A4: exactly one training replay required")
            require(
                pre.get("training_start_cache_state") == "fresh-clone-process-start"
                and pre.get("adaptation_prestate") == "post-identical-training-trace",
                "A4: paired cache/adaptation prestate drift",
            )
            require(pre.get("feedback") == "enabled", "A4: feedback must be enabled")
            require(pre.get("feedback_compactions") == 1, "A4: exactly one feedback compaction required")
            require(pre.get("paired_stage") == "A3", "A4: must identify A3 paired control")
            require(
                pre.get("claim_scope")
                == "feedback-driven-adaptation-including-triggered-compaction",
                "A4: claim scope drift",
            )
        if expected_output is not None:
            require(pre.get("trace") == "shared-training-trace", f"{stage_id}: shared training trace required")
            require(pre.get("compaction_output") == expected_output, f"{stage_id}: compaction output must be {expected_output}")
        if stage_id == "A6":
            require(pre.get("trace") == "shared-training-trace", "A6: shared training trace required")
            require(pre.get("settle") == "engine-quiescence", "A6: engine quiescence gate required")

    modes = doc.get("modes")
    require(isinstance(modes, dict), "modes must be an object")
    require(set(modes) == {"latency", "cpu-phase", "closed-loop-resource", "correctness"}, "unexpected mode set")
    require(modes == EXPECTED_MODES, "mode policy changed")
    for name, mode in modes.items():
        require_keys(
            mode,
            required={
                "performance_eligible",
                "query_cpu_phases",
                "automatic_maintenance",
                "post_round_manual_compaction",
                "max_query_streams",
                "allowed_stages",
            },
            context=f"mode {name}",
        )
        allowed = mode.get("allowed_stages")
        require(isinstance(allowed, list) and allowed, f"mode {name}: allowed_stages must be non-empty")
        require(len(allowed) == len(set(allowed)), f"mode {name}: duplicate stage")
        require(all(stage in STAGE_IDS for stage in allowed), f"mode {name}: unknown stage")
        require(mode.get("max_query_streams") == 1, f"mode {name}: P20 must be single-stream")
        require(mode.get("post_round_manual_compaction") is False, f"mode {name}: measured run must not compact post-round")

    latency = modes["latency"]
    require(latency.get("performance_eligible") is True, "latency must be performance eligible")
    require(latency.get("query_cpu_phases") is False, "latency must disable CPU phase instrumentation")
    require(latency.get("automatic_maintenance") == "a6-only", "latency automatic maintenance must be A6-only")

    cpu = modes["cpu-phase"]
    require(cpu.get("performance_eligible") is False, "CPU-phase diagnostics cannot be formal latency")
    require(cpu.get("query_cpu_phases") is True, "CPU-phase mode must enable instrumentation")
    require(cpu.get("automatic_maintenance") == "never", "CPU-phase mode must disable background maintenance")
    require("A6" not in cpu.get("allowed_stages", []), "A6 is excluded from CPU phases because its defining switch is background maintenance")

    closed = modes["closed-loop-resource"]
    require(closed.get("allowed_stages") == ["A6"], "closed-loop-resource must be A6-only")
    require(closed.get("automatic_maintenance") == "always", "closed-loop-resource must enable maintenance")
    require(closed.get("query_cpu_phases") is False, "closed-loop-resource must disable CPU phase instrumentation")

    workloads = doc.get("workloads")
    require(isinstance(workloads, dict), "workloads must be an object")
    require(set(workloads) == {"typed-one-hop", "degree-stratified", "property-presence"}, "unexpected workload set")
    require(workloads == EXPECTED_WORKLOADS, "workload arguments changed")
    for name, workload in workloads.items():
        require_keys(
            workload,
            required={"args", "required_bindings"},
            context=f"workload {name}",
        )
        args = workload.get("args")
        required = workload.get("required_bindings")
        require(isinstance(args, list) and all(isinstance(arg, str) for arg in args), f"workload {name}: args must be strings")
        require(isinstance(required, list) and all(isinstance(item, str) for item in required), f"workload {name}: invalid bindings")
        placeholders = {match.group(1) for arg in args if (match := PLACEHOLDER.match(arg))}
        require(placeholders == set(required), f"workload {name}: placeholders and required_bindings differ")
        require("--emit-result-digests" in args, f"workload {name}: result digests are mandatory")
        for token in args:
            for forbidden in FORBIDDEN_WORKLOAD_ARGS:
                require(
                    token != forbidden and not token.startswith(forbidden + "="),
                    f"workload {name}: forbidden control argument {token}",
                )

    matrix = doc.get("matrix")
    require(isinstance(matrix, dict) and set(matrix) == {"sf10", "sf30"}, "matrix must contain exactly sf10 and sf30")
    expected_matrix_stages = {"sf10": STAGE_IDS, "sf30": ["A0", "A2", "A4", "A6"]}
    for scale, cell in matrix.items():
        require_keys(
            cell,
            required={"stages", "workloads", "minimum_independent_runs"},
            context=f"matrix {scale}",
        )
        require(cell.get("stages") == expected_matrix_stages[scale], f"{scale}: unexpected stage matrix")
        require(cell.get("workloads") == list(workloads), f"{scale}: all three workloads required in canonical order")
        require(cell.get("minimum_independent_runs") == 3, f"{scale}: minimum independent runs must be 3")


def parse_bindings(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in values:
        if "=" not in raw:
            raise ProfileError(f"invalid --bind {raw!r}; expected NAME=VALUE")
        name, value = raw.split("=", 1)
        require(bool(re.fullmatch(r"[A-Z][A-Z0-9_]*", name)), f"invalid binding name {name!r}")
        require(value != "", f"binding {name} cannot be empty")
        require(name not in result, f"duplicate binding {name}")
        result[name] = value
    return result


def resolve(
    doc: dict[str, Any], *, scale: str, stage_id: str, mode_name: str, workload_name: str, bindings: dict[str, str]
) -> dict[str, Any]:
    validate(doc)
    require(scale in doc["matrix"], f"unknown scale {scale}")
    require(stage_id in doc["matrix"][scale]["stages"], f"{stage_id} is not in the canonical {scale} matrix")
    require(mode_name in doc["modes"], f"unknown mode {mode_name}")
    mode = doc["modes"][mode_name]
    require(stage_id in mode["allowed_stages"], f"{stage_id} is forbidden in mode {mode_name}")
    require(workload_name in doc["matrix"][scale]["workloads"], f"unknown workload {workload_name}")

    stage = next(stage for stage in doc["stages"] if stage["id"] == stage_id)
    workload = doc["workloads"][workload_name]
    missing = sorted(set(workload["required_bindings"]) - set(bindings))
    extra = sorted(set(bindings) - set(workload["required_bindings"]))
    require(not missing, f"missing bindings: {', '.join(missing)}")
    require(not extra, f"unexpected bindings: {', '.join(extra)}")

    args: list[str] = [
        "storage-bench",
        "--l0-layout", stage["l0_layout"],
        "--query-control-stage", stage_id.lower(),
    ]
    for raw in workload["args"]:
        match = PLACEHOLDER.match(raw)
        args.append(bindings[match.group(1)] if match else raw)

    policy = mode["automatic_maintenance"]
    automatic = policy == "always" or (policy == "a6-only" and stage_id == "A6")
    if automatic:
        args.append("--automatic-maintenance")
    if mode["query_cpu_phases"]:
        args.append("--query-cpu-phases")
    precondition_kind = stage["pre_measurement"]["kind"]
    if precondition_kind == "fixed-trace-adaptation-control":
        args.extend(["--training-runs", "1"])
    elif precondition_kind == "fixed-trace-feedback-adaptation":
        args.extend(["--training-runs", "1", "--training-feedback-compactions", "1"])
    elif precondition_kind == "fixed-trace-manual-feedback-compaction":
        args.extend(["--training-runs", "1", "--training-feedback-compactions", "1"])
    elif precondition_kind == "fixed-trace-automatic-maintenance":
        args.extend(["--training-runs", "1"])

    return {
        "schema_version": doc["schema_version"],
        "experiment_id": doc["experiment_id"],
        "scale": scale,
        "stage": stage,
        "mode": {"name": mode_name, **mode, "resolved_automatic_maintenance": automatic},
        "workload": {"name": workload_name, **workload},
        "minimum_independent_runs": doc["matrix"][scale]["minimum_independent_runs"],
        "storage_bench_args": args,
        "fixed_inputs": doc["fixed_inputs"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profiles", type=Path, default=Path(__file__).with_name("profiles.json"))
    parser.add_argument("--resolve", action="store_true")
    parser.add_argument("--scale", choices=("sf10", "sf30"))
    parser.add_argument("--stage", choices=STAGE_IDS)
    parser.add_argument("--mode", choices=("latency", "cpu-phase", "closed-loop-resource", "correctness"))
    parser.add_argument("--workload", choices=("typed-one-hop", "degree-stratified", "property-presence"))
    parser.add_argument("--bind", action="append", default=[], metavar="NAME=VALUE")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        doc = load_profiles(args.profiles)
        validate(doc)
        if args.resolve:
            missing = [name for name in ("scale", "stage", "mode", "workload") if getattr(args, name) is None]
            require(not missing, "--resolve requires " + ", ".join(f"--{name}" for name in missing))
            output = resolve(
                doc,
                scale=args.scale,
                stage_id=args.stage,
                mode_name=args.mode,
                workload_name=args.workload,
                bindings=parse_bindings(args.bind),
            )
            print(json.dumps(output, indent=2, sort_keys=True))
        else:
            require(not args.bind, "--bind is only valid with --resolve")
            print(f"PASS: {args.profiles} is a valid P20 canonical profile set")
        return 0
    except ProfileError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
