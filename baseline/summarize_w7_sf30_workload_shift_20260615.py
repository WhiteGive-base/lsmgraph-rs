#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


def fmt_opt(value):
    return "n/a" if value is None else str(value)


def fmt_bytes(value):
    value = int(value or 0)
    if value >= 1024**3:
        return f"{value / 1024**3:.2f} GiB"
    if value >= 1024**2:
        return f"{value / 1024**2:.2f} MiB"
    if value >= 1024:
        return f"{value / 1024:.2f} KiB"
    return f"{value} B"


def per_query(query, field):
    queries = max(int(query.get("queries", 0)), 1)
    return float(query.get(field, 0)) / queries


def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def phase_last_flush(variant, phase):
    rows = [row for row in variant.get("flushes", []) if row.get("phase") == phase]
    return rows[-1] if rows else None


def compaction_bytes(variant, phase):
    out = {
        "input": 0,
        "output": 0,
        "write": 0,
        "hot": 0,
    }
    for row in variant.get("flushes", []):
        if row.get("phase") != phase:
            continue
        comp = row.get("compaction") or {}
        metrics = comp.get("metrics") or {}
        out["input"] += int(metrics.get("compaction_input_bytes") or 0)
        out["output"] += int(metrics.get("compaction_output_bytes") or 0)
        out["write"] += int(metrics.get("io_write_bytes") or 0)
        decision = comp.get("decision") or {}
        if decision.get("selected_hot_partition"):
            out["hot"] += 1
    return out


def variant_gate(variant):
    if variant.get("variant") != "feedback-only":
        return "n/a"
    summary = variant.get("summary", {})
    a = summary.get("phase_a", {})
    b = summary.get("phase_b", {})
    if (
        a.get("first_hot_compaction_flush") is not None
        and b.get("first_hot_compaction_flush") is not None
        and summary.get("selected_ranges_changed")
    ):
        return "GO"
    return "FALLBACK"


def write_summary(data, output, source_label):
    variants = data.get("variants", [])
    feedback = next((v for v in variants if v.get("variant") == "feedback-only"), None)
    gate = variant_gate(feedback or {})

    lines = []
    lines.append("# W7 SF30-derived workload-shift summary")
    lines.append("")
    lines.append(f"Source: `{source_label}`")
    lines.append(f"Runner: `{data.get('runner')}`")
    lines.append(f"Scale: `{data.get('scale')}`")
    lines.append("")
    lines.append("## Run Parameters")
    lines.append("")
    lines.append("| item | value |")
    lines.append("|---|---:|")
    for key in [
        "phase_flushes",
        "hot_sources_per_phase",
        "edges_per_source_per_flush",
        "queries_per_source",
        "max_scan_rows_per_phase",
    ]:
        lines.append(f"| {key} | {data.get(key)} |")
    lines.append("")

    lines.append("## Gate")
    lines.append("")
    lines.append(f"Gate: {gate}")
    if gate == "GO":
        lines.append("")
        lines.append(
            "Feedback-only selected a hot semantic partition in both phase A and phase B, and the selected hot range changed after the workload shift."
        )
    else:
        lines.append("")
        lines.append(
            "Feedback-only did not prove both hot-partition selections plus range change; keep W7 as controlled/limited evidence."
        )
    lines.append("")

    lines.append("## Migration Summary")
    lines.append("")
    lines.append(
        "| variant | phase A selected flush | phase A feedback-weight flush | phase A hot-compaction flush | phase B selected flush | phase B feedback-weight flush | phase B hot-compaction flush | selected ranges changed |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---|")
    for variant in variants:
        summary = variant.get("summary", {})
        a = summary.get("phase_a", {})
        b = summary.get("phase_b", {})
        lines.append(
            "| {variant} | {a_sel} | {a_fw} | {a_hot} | {b_sel} | {b_fw} | {b_hot} | {changed} |".format(
                variant=variant.get("variant"),
                a_sel=fmt_opt(a.get("first_selected_flush")),
                a_fw=fmt_opt(a.get("first_feedback_weight_flush")),
                a_hot=fmt_opt(a.get("first_hot_compaction_flush")),
                b_sel=fmt_opt(b.get("first_selected_flush")),
                b_fw=fmt_opt(b.get("first_feedback_weight_flush")),
                b_hot=fmt_opt(b.get("first_hot_compaction_flush")),
                changed=summary.get("selected_ranges_changed"),
            )
        )
    lines.append("")

    lines.append("## Last-Flush Query Cost")
    lines.append("")
    lines.append(
        "| variant | phase | candidate/query before | candidate/query after | read bytes/query before | read bytes/query after | body reads/query before | body reads/query after |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for variant in variants:
        for phase in ["A", "B"]:
            row = phase_last_flush(variant, phase)
            if not row:
                continue
            before = row.get("query_before_compaction") or {}
            after = row.get("query_after_compaction") or {}
            lines.append(
                "| {variant} | {phase} | {cb:.3f} | {ca} | {rb:.1f} | {ra} | {bb:.3f} | {ba} |".format(
                    variant=variant.get("variant"),
                    phase=phase,
                    cb=per_query(before, "candidate_l0_segments"),
                    ca="n/a" if not after else f"{per_query(after, 'candidate_l0_segments'):.3f}",
                    rb=per_query(before, "io_read_bytes"),
                    ra="n/a" if not after else f"{per_query(after, 'io_read_bytes'):.1f}",
                    bb=per_query(before, "csr_body_reads"),
                    ba="n/a" if not after else f"{per_query(after, 'csr_body_reads'):.3f}",
                )
            )
    lines.append("")

    lines.append("## Rewrite Cost")
    lines.append("")
    lines.append("| variant | phase | hot compactions | compaction input | compaction output | io write bytes | write amplification |")
    lines.append("|---|---|---:|---:|---:|---:|---:|")
    for variant in variants:
        for phase in ["A", "B"]:
            b = compaction_bytes(variant, phase)
            wa = (b["write"] / max(b["input"], 1)) if b["input"] else 0.0
            lines.append(
                f"| {variant.get('variant')} | {phase} | {b['hot']} | {fmt_bytes(b['input'])} | {fmt_bytes(b['output'])} | {fmt_bytes(b['write'])} | {wa:.3f} |"
            )
    lines.append("")

    lines.append("## Claim Boundary")
    lines.append("")
    if gate == "GO":
        lines.append(
            "Safe claim: W7 provides real-SF30-derived evidence that feedback-driven SemL0 can move semantic compaction priority after a workload shift. This is still a derived workload, not a full-store SF30 production trace."
        )
    else:
        lines.append(
            "Safe claim: W7 remains controlled/derived evidence only; do not claim formal workload-shift self-tuning until the gate is GO."
        )
    lines.append("")

    Path(output).write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--source-label", required=True)
    args = parser.parse_args()
    write_summary(load_json(args.input), args.output, args.source_label)


if __name__ == "__main__":
    main()
