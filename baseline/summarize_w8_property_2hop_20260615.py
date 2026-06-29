#!/usr/bin/env python3
"""Regenerate the W8 property + 2-hop summary from raw wrapped JSON outputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
from typing import Any


VARIANTS = ("schema", "budg-b64", "semantic")
PROPERTY_FILES = (
    ("required_property", "property-required-property.json"),
    ("presence", "property-presence.json"),
    ("equality", "property-equality.json"),
    ("absent_default", "property-absent-default.json"),
)
TWO_HOP_FILE = "2hop-typed.json"


def load_wrapped_json(path: Path) -> dict[str, Any]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if lines and lines[0].startswith("[w8]"):
        payload = "\n".join(lines[1:-1])
    else:
        payload = "\n".join(lines)
    return json.loads(payload)


def metric(summary: dict[str, Any], name: str) -> float:
    return float(summary[name]["mean"])


def fmt_num(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value)):,}"
    return f"{value:,.1f}"


def fmt_bytes(value: float) -> str:
    if value >= 1024**3:
        return f"{value / 1024**3:.2f} GiB"
    if value >= 1024**2:
        return f"{value / 1024**2:.2f} MiB"
    return f"{value:,.0f} B"


def fmt_ms(value: float) -> str:
    if value >= 60_000:
        return f"{value / 60_000:.2f} min"
    if value >= 1_000:
        return f"{value / 1_000:.2f} s"
    return f"{value:.1f} ms"


def pct_delta(new: float, base: float) -> str:
    if base == 0:
        return "n/a"
    pct = (new - base) / base * 100.0
    sign = "+" if pct > 0 else ""
    return f"{sign}{pct:.1f}%"


def row_for(path: Path) -> dict[str, Any]:
    obj = load_wrapped_json(path)
    bench = obj["benchmarks"][0]
    repeat = bench["repeat_summary"]
    return {
        "path": path,
        "workload_mode": obj.get("workload_mode"),
        "predicate": obj.get("property_predicate_mode") or "none",
        "candidate_l0": metric(repeat, "candidate_l0_segments"),
        "body_reads": metric(repeat, "body_reads"),
        "read_bytes": metric(repeat, "read_bytes"),
        "elapsed_ms": metric(repeat, "elapsed_ms"),
        "p50_us": metric(repeat, "get_neighbors_p50_us"),
        "p90_us": metric(repeat, "get_neighbors_p90_us"),
        "p99_us": metric(repeat, "get_neighbors_p99_us"),
        "one_hop_edges": metric(repeat, "one_hop_edges"),
        "two_hop_edges": metric(repeat, "two_hop_edges"),
        "two_hop_sources": metric(repeat, "two_hop_sources"),
    }


def avg(rows: list[dict[str, Any]], key: str) -> float:
    return mean(float(row[key]) for row in rows)


def build_report(log_root: Path, source_label: str | None = None) -> str:
    property_rows: dict[tuple[str, str], dict[str, Any]] = {}
    two_hop_rows: dict[str, dict[str, Any]] = {}
    import_rows: dict[str, dict[str, Any]] = {}

    for variant in VARIANTS:
        variant_root = log_root / variant
        import_rows[variant] = load_wrapped_json(variant_root / "import.json")
        for predicate, filename in PROPERTY_FILES:
            property_rows[(variant, predicate)] = row_for(variant_root / filename)
        two_hop_rows[variant] = row_for(variant_root / TWO_HOP_FILE)

    out: list[str] = []
    out.append("# W8 property + 2-hop summary\n")
    out.append("Generated from raw wrapped JSON outputs under:\n")
    out.append(f"`{source_label or str(log_root)}`\n")
    out.append("All W8 JSON payloads were parsed after stripping runner start/done lines.\n")

    out.append("## Import validation\n")
    out.append("| variant | input rows | directed edges | snapshot |")
    out.append("|---|---:|---:|---:|")
    for variant in VARIANTS:
        row = import_rows[variant]
        out.append(
            f"| `{variant}` | {int(row['input_rows']):,} | "
            f"{int(row['directed_edges']):,} | {int(row['snapshot']):,} |"
        )

    out.append("\n## Property predicate results\n")
    out.append(
        "| variant | predicate | candidate L0 mean | body reads mean | read bytes mean | "
        "elapsed mean | p50 us | p90 us | p99 us | one-hop edges mean |"
    )
    out.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for variant in VARIANTS:
        for predicate, _ in PROPERTY_FILES:
            row = property_rows[(variant, predicate)]
            out.append(
                f"| `{variant}` | `{predicate}` | {fmt_num(row['candidate_l0'])} | "
                f"{fmt_num(row['body_reads'])} | {fmt_bytes(row['read_bytes'])} | "
                f"{fmt_ms(row['elapsed_ms'])} | {fmt_num(row['p50_us'])} | "
                f"{fmt_num(row['p90_us'])} | {fmt_num(row['p99_us'])} | "
                f"{fmt_num(row['one_hop_edges'])} |"
            )

    nontrivial = ("presence", "equality", "absent_default")
    out.append("\n## Property deltas vs schema\n")
    out.append(
        "The required-property mode is an exact-prune case with zero candidates/body reads "
        "for all variants, so the aggregate below uses presence/equality/absent-default."
    )
    out.append("\n| variant | candidate L0 delta | body reads delta | read bytes delta | elapsed delta |")
    out.append("|---|---:|---:|---:|---:|")
    base_prop_rows = [property_rows[("schema", predicate)] for predicate in nontrivial]
    base_prop = {key: avg(base_prop_rows, key) for key in ("candidate_l0", "body_reads", "read_bytes", "elapsed_ms")}
    for variant in ("budg-b64", "semantic"):
        rows = [property_rows[(variant, predicate)] for predicate in nontrivial]
        cur = {key: avg(rows, key) for key in base_prop}
        out.append(
            f"| `{variant}` | {pct_delta(cur['candidate_l0'], base_prop['candidate_l0'])} | "
            f"{pct_delta(cur['body_reads'], base_prop['body_reads'])} | "
            f"{pct_delta(cur['read_bytes'], base_prop['read_bytes'])} | "
            f"{pct_delta(cur['elapsed_ms'], base_prop['elapsed_ms'])} |"
        )

    out.append("\n## 2-hop results\n")
    out.append(
        "| variant | candidate L0 mean | body reads mean | read bytes mean | elapsed mean | "
        "p50 us | p90 us | p99 us | one-hop edges mean | two-hop edges mean | two-hop sources mean |"
    )
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for variant in VARIANTS:
        row = two_hop_rows[variant]
        out.append(
            f"| `{variant}` | {fmt_num(row['candidate_l0'])} | {fmt_num(row['body_reads'])} | "
            f"{fmt_bytes(row['read_bytes'])} | {fmt_ms(row['elapsed_ms'])} | "
            f"{fmt_num(row['p50_us'])} | {fmt_num(row['p90_us'])} | {fmt_num(row['p99_us'])} | "
            f"{fmt_num(row['one_hop_edges'])} | {fmt_num(row['two_hop_edges'])} | "
            f"{fmt_num(row['two_hop_sources'])} |"
        )

    out.append("\n## 2-hop deltas vs schema\n")
    out.append("| variant | candidate L0 delta | body reads delta | read bytes delta | elapsed delta |")
    out.append("|---|---:|---:|---:|---:|")
    base_two = two_hop_rows["schema"]
    for variant in ("budg-b64", "semantic"):
        row = two_hop_rows[variant]
        out.append(
            f"| `{variant}` | {pct_delta(row['candidate_l0'], base_two['candidate_l0'])} | "
            f"{pct_delta(row['body_reads'], base_two['body_reads'])} | "
            f"{pct_delta(row['read_bytes'], base_two['read_bytes'])} | "
            f"{pct_delta(row['elapsed_ms'], base_two['elapsed_ms'])} |"
        )

    out.append("\n## Conclusion\n")
    out.append(
        "W8 is `ready-for-paper` with caveats. It supports the reviewer-facing claim that "
        "property predicates and 2-hop traversals are covered by the SemL0 evidence block. "
        "For property presence/equality/absent-default, `budg-b64` and `semantic` reduce "
        "candidate L0, body reads, read bytes, and elapsed time versus `schema`. For 2-hop, "
        "`budg-b64` and `semantic` reduce body reads, read bytes, and elapsed time, but their "
        "candidate L0 count is higher than `schema`; therefore the paper must not claim "
        "universal 2-hop candidate reduction."
    )
    out.append(
        "\nSafe claim: W8 strengthens the property-predicate and 2-hop coverage, but 2-hop "
        "candidate pruning should be described as mixed; latency/body/read-byte improvements "
        "are the defensible 2-hop result."
    )
    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--log-root",
        default="remote-logs/w8-property-2hop-20260614-2025",
        type=Path,
    )
    parser.add_argument(
        "--output",
        default="baseline/w8-property-2hop-summary-20260615-cn.md",
        type=Path,
    )
    parser.add_argument("--source-label", default=None)
    args = parser.parse_args()
    report = build_report(args.log_root, args.source_label)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")


if __name__ == "__main__":
    main()
