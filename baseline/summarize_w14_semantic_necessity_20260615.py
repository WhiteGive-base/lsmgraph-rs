#!/usr/bin/env python3
import argparse
import json
import math
from pathlib import Path


VARIANTS = ["schema", "edge-type-only", "budg-b64", "semantic"]
NON_EDGE_REASONS = {
    "src_label",
    "dst_label",
    "degree",
    "property_absence",
    "schema_tombstone_fallback",
    "mixed_unknown_fallback",
    "budgeted_not_materialized",
}


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def first_benchmark(data):
    benches = data.get("benchmarks") or []
    return benches[0] if benches else {}


def stats_mean(node, key, default=0.0):
    value = (((node.get("repeat_summary") or {}).get(key) or {}).get("mean"))
    if value is None:
        return default
    return float(value)


def metric(node, key, default=0.0):
    summary = node.get("neighbor_summary") or {}
    value = summary.get(key)
    if value is None:
        return default
    return float(value)


def latency_sum_mean(node):
    return stats_mean(node, "get_neighbors_latency_sum_us")


def pruning_reasons(node):
    summary = node.get("neighbor_summary") or {}
    reasons = summary.get("pruning_reasons") or {}
    if not reasons:
        metrics = node.get("neighbor_metrics") or {}
        reasons = (((metrics.get("csr") or {}).get("pruning_reasons")) or {})
    return reasons


def reason_pruned_total(reasons):
    total = 0
    for data in reasons.values():
        if isinstance(data, dict):
            total += int(data.get("pruned_segments") or 0)
    return total


def non_edge_reason_total(reasons):
    total = 0
    for reason, data in reasons.items():
        if reason in NON_EDGE_REASONS and isinstance(data, dict):
            total += int(data.get("pruned_segments") or 0)
    return total


def fmt_num(value):
    if value is None:
        return "NA"
    if isinstance(value, float) and not math.isfinite(value):
        return "NA"
    if abs(float(value) - int(float(value))) < 1e-9:
        return f"{int(value):,}"
    return f"{float(value):,.2f}"


def ratio(a, b):
    if b in (None, 0):
        return "NA"
    return f"{a / b:.2f}x"


def compare_status(scenario_dir: Path, variant: str):
    if variant == "schema":
        return "anchor"
    path = scenario_dir / f"compare-schema-vs-{variant}.json"
    if not path.exists():
        return "missing"
    data = load_json(path)
    checked = data.get("checked", 0)
    mismatches = data.get("mismatches", 0)
    return f"checked={checked}, mismatches={mismatches}"


def scenario_rows(scenario_dir: Path):
    rows = []
    for variant in VARIANTS:
        path = scenario_dir / f"{variant}.json"
        if not path.exists():
            rows.append({"variant": variant, "missing": True})
            continue
        data = load_json(path)
        bench = first_benchmark(data)
        reasons = pruning_reasons(bench)
        rows.append(
            {
                "variant": variant,
                "missing": False,
                "candidate": metric(bench, "candidate_l0_segments"),
                "body_reads": metric(bench, "body_reads"),
                "body_bytes": metric(bench, "body_bytes"),
                "read_bytes": metric(bench, "read_bytes"),
                "elapsed_ms": stats_mean(bench, "elapsed_ms"),
                "p99_us": stats_mean(bench, "get_neighbors_p99_us"),
                "pruned_total": reason_pruned_total(reasons),
                "non_edge_pruned": non_edge_reason_total(reasons),
                "reasons": reasons,
                "compare": compare_status(scenario_dir, variant),
            }
        )
    return rows


def verdict(all_rows):
    missing = any(row.get("missing") for rows in all_rows.values() for row in rows)
    mismatch = any(
        isinstance(row.get("compare"), str) and "mismatches=0" not in row["compare"] and row["compare"] != "anchor"
        for rows in all_rows.values()
        for row in rows
        if not row.get("missing")
    )
    if missing or mismatch:
        return "NO-GO", "有缺失结果或 compare mismatch，不能写必要性 claim。"

    positive_scenarios = []
    for scenario, rows in all_rows.items():
        by_variant = {row["variant"]: row for row in rows if not row.get("missing")}
        edge = by_variant.get("edge-type-only")
        sem = by_variant.get("semantic")
        budg = by_variant.get("budg-b64")
        if not edge or not sem:
            continue
        semantic_better = (
            sem["candidate"] < edge["candidate"]
            or sem["body_reads"] < edge["body_reads"]
            or sem["read_bytes"] < edge["read_bytes"]
        )
        budgeted_better = bool(
            budg
            and (
                budg["candidate"] < edge["candidate"]
                or budg["body_reads"] < edge["body_reads"]
                or budg["read_bytes"] < edge["read_bytes"]
            )
        )
        non_edge_signal = sem["non_edge_pruned"] > 0 or (budg and budg["non_edge_pruned"] > 0)
        if non_edge_signal and (semantic_better or budgeted_better):
            positive_scenarios.append(scenario)

    if positive_scenarios:
        return (
            "GO",
            "非 edge-type 语义维度产生了可观测 pruning，并且至少一个 composite 场景相对 edge-type-only 改善 candidate/body/read 指标。",
        )
    return (
        "FALLBACK",
        "compare 正确且 telemetry 有效，但没有稳定证明 composite semantics 优于 edge-type-only；论文只能把 edge-type-only 写成 SemL0 的一维特例，必要性 claim 要收窄。",
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    scenario_dirs = sorted(
        path for path in args.run_root.iterdir() if path.is_dir() and (path / "sample-plan.json").exists()
    )
    all_rows = {path.name: scenario_rows(path) for path in scenario_dirs}
    gate, reason = verdict(all_rows)

    lines = []
    lines.append("# W14 SemL0 必要性实验 Summary")
    lines.append("")
    lines.append(f"- Run root: `{args.run_root}`")
    lines.append(f"- Verdict: **{gate}**")
    lines.append(f"- Reason: {reason}")
    lines.append("")
    lines.append("## Table X: semantic necessity matrix")
    lines.append("")
    lines.append(
        "| Scenario | Variant | Candidate L0 | Body reads | Body bytes | Read bytes | Elapsed ms mean | p99 us mean | Pruned total | Non-edge pruned | Compare |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for scenario, rows in all_rows.items():
        for row in rows:
            if row.get("missing"):
                lines.append(f"| {scenario} | {row['variant']} | MISSING | MISSING | MISSING | MISSING | MISSING | MISSING | MISSING | MISSING | missing |")
                continue
            lines.append(
                "| {scenario} | {variant} | {candidate} | {body_reads} | {body_bytes} | {read_bytes} | {elapsed} | {p99} | {pruned} | {non_edge} | {compare} |".format(
                    scenario=scenario,
                    variant=row["variant"],
                    candidate=fmt_num(row["candidate"]),
                    body_reads=fmt_num(row["body_reads"]),
                    body_bytes=fmt_num(row["body_bytes"]),
                    read_bytes=fmt_num(row["read_bytes"]),
                    elapsed=fmt_num(row["elapsed_ms"]),
                    p99=fmt_num(row["p99_us"]),
                    pruned=fmt_num(row["pruned_total"]),
                    non_edge=fmt_num(row["non_edge_pruned"]),
                    compare=row["compare"],
                )
            )
    lines.append("")
    lines.append("## Pruning reason breakdown")
    lines.append("")
    for scenario, rows in all_rows.items():
        lines.append(f"### {scenario}")
        lines.append("")
        lines.append("| Variant | Reason | Pruned segments | Kept segments | Saved segment bytes | Saved body reads |")
        lines.append("|---|---|---:|---:|---:|---:|")
        for row in rows:
            if row.get("missing"):
                continue
            reasons = row.get("reasons") or {}
            if not reasons:
                lines.append(f"| {row['variant']} | none | 0 | 0 | 0 | 0 |")
                continue
            for reason_name, data in sorted(reasons.items()):
                lines.append(
                    "| {variant} | {reason} | {pruned} | {kept} | {bytes_} | {reads} |".format(
                        variant=row["variant"],
                        reason=reason_name,
                        pruned=fmt_num((data or {}).get("pruned_segments", 0)),
                        kept=fmt_num((data or {}).get("kept_segments", 0)),
                        bytes_=fmt_num((data or {}).get("estimated_saved_segment_bytes", 0)),
                        reads=fmt_num((data or {}).get("estimated_saved_body_reads", 0)),
                    )
                )
        lines.append("")
    lines.append("## Claim rule")
    lines.append("")
    lines.append("- GO: 可以写 edge-type-only 是 SemL0 的一维特例，真实 property-graph semantics 还能进一步降低 candidate/body/read。")
    lines.append("- FALLBACK: 只能写 SemL0 暴露了可验证的 semantic telemetry，必要性 claim 收窄，不写稳定优于 edge-type-only。")
    lines.append("- NO-GO: 有 mismatch、缺字段或缺结果，W14 不能进入论文主 claim。")
    lines.append("")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
