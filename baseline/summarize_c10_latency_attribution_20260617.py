#!/usr/bin/env python3
import json
import os
import re
import statistics
import sys
from pathlib import Path


REPO = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/data/WorkSpace/lsmgraph-rs")
OUT_DIR = REPO / "baseline"

SOURCES = [
    ("W6", "sf100-core", REPO / "remote-logs/w6-sf100-matrix-20260613-132325"),
    ("W8", "sf30-property-2hop", REPO / "remote-logs/w8-property-2hop-20260614-2025"),
    ("W14", "sf100-semantic-necessity", REPO / "remote-logs/w14-sf100-minimal-reuse-20260617-codex1"),
]

LATENCY_KEYS = [
    "get_neighbors_latency",
    "probe_setup_latency",
    "probe_bloom_latency",
    "probe_offset_lookup_latency",
    "probe_body_read_latency",
    "probe_total_latency",
]

CSR_COUNTERS = [
    "candidate_l0_segments",
    "filter_passed_segments",
    "bloom_filtered_segments",
    "range_filtered_segments",
    "matched_l0_segments",
    "body_reads",
    "body_bytes",
    "full_scan_reads",
    "offset_reads",
    "offset_bytes",
    "probe_bloom_negative",
    "probe_offset_miss",
    "probe_body_hit",
]

IO_COUNTERS = ["read_syscalls", "read_bytes", "write_syscalls", "write_bytes"]


def safe_load_json(path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        start = text.find("{")
        if start > 0:
            text = text[start:]
        end = text.rfind("}")
        if end >= 0:
            text = text[: end + 1]
        return json.loads(text)
    except Exception as exc:
        return {"__error__": str(exc)}


def latency_sum_us(node):
    if not isinstance(node, dict):
        return 0
    for key in ("sum_us", "total_us"):
        value = node.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    count = node.get("count")
    avg = node.get("avg_us")
    if isinstance(count, (int, float)) and isinstance(avg, (int, float)):
        return int(count * avg)
    return 0


def latency_count(node):
    if not isinstance(node, dict):
        return 0
    value = node.get("count")
    return int(value) if isinstance(value, (int, float)) else 0


def nested(dct, *keys):
    cur = dct
    for key in keys:
        if not isinstance(cur, dict):
            return {}
        cur = cur.get(key)
    return cur if isinstance(cur, dict) else {}


def parse_open_seconds(stderr_path):
    if not stderr_path.exists():
        return ""
    text = stderr_path.read_text(encoding="utf-8", errors="replace")
    matches = re.findall(r"total_open_s=([0-9.]+)", text)
    return matches[-1] if matches else ""


def bench_files():
    w6 = REPO / "remote-logs/w6-sf100-matrix-20260613-132325"
    if w6.exists():
        for path in sorted(w6.glob("*-bench.json")):
            variant = path.name[: -len("-bench.json")]
            yield "W6", "sf100-core", variant, "core", path, path.with_suffix(".stderr")

    w8 = REPO / "remote-logs/w8-property-2hop-20260614-2025"
    if w8.exists():
        for variant_dir in sorted(p for p in w8.iterdir() if p.is_dir()):
            variant = variant_dir.name
            for path in sorted(variant_dir.glob("*.json")):
                if path.name == "import.json":
                    continue
                yield "W8", "sf30-property-2hop", variant, path.stem, path, path.with_name(path.stem + ".stderr")

    w14 = REPO / "remote-logs/w14-sf100-minimal-reuse-20260617-codex1"
    if w14.exists():
        for scenario_dir in sorted(p for p in w14.iterdir() if p.is_dir()):
            scenario = scenario_dir.name
            for path in sorted(scenario_dir.glob("*.json")):
                if path.name.startswith("digest-compare"):
                    continue
                if path.name in {"sample-plan.json", "schema-plan-source.json"}:
                    continue
                yield "W14", "sf100-semantic-necessity", path.stem, scenario, path, path.with_name(path.stem + "-bench.stderr")


def summarize_file(source, suite, variant, scenario, path, stderr_path):
    data = safe_load_json(path)
    rows = data.get("benchmarks", []) if isinstance(data, dict) else []
    row_count = len(rows)
    elapsed_values = []
    sums = {key: 0 for key in LATENCY_KEYS}
    counts = {key: 0 for key in LATENCY_KEYS}
    csr = {key: 0 for key in CSR_COUNTERS}
    io = {key: 0 for key in IO_COUNTERS}
    result_count = 0
    neighbor_edges = 0

    for row in rows:
        elapsed = row.get("get_neighbors_elapsed_ms")
        if isinstance(elapsed, (int, float)):
            elapsed_values.append(float(elapsed))
        result_count += int(row.get("result_count") or 0)
        neighbor_edges += int(row.get("neighbor_edges") or 0)
        metrics = row.get("neighbor_metrics") or {}
        storage_metrics = nested(metrics, "storage")
        csr_metrics = nested(metrics, "csr")
        io_metrics = nested(metrics, "io")
        for key in LATENCY_KEYS:
            location = storage_metrics if key == "get_neighbors_latency" else csr_metrics
            node = location.get(key, {})
            sums[key] += latency_sum_us(node)
            counts[key] += latency_count(node)
        for key in CSR_COUNTERS:
            value = csr_metrics.get(key, 0)
            if isinstance(value, (int, float)):
                csr[key] += int(value)
        for key in IO_COUNTERS:
            value = io_metrics.get(key, 0)
            if isinstance(value, (int, float)):
                io[key] += int(value)

    elapsed_sum_ms = sum(elapsed_values)
    elapsed_avg_ms = statistics.mean(elapsed_values) if elapsed_values else 0.0
    elapsed_p99_ms = sorted(elapsed_values)[int(0.99 * (len(elapsed_values) - 1))] if elapsed_values else 0.0
    probe_total = sums["probe_total_latency"]
    stage_sum = (
        sums["probe_setup_latency"]
        + sums["probe_bloom_latency"]
        + sums["probe_offset_lookup_latency"]
        + sums["probe_body_read_latency"]
    )
    storage_total = sums["get_neighbors_latency"]
    unaccounted = max(storage_total - probe_total, 0)
    return {
        "source": source,
        "suite": suite,
        "scenario": scenario,
        "variant": variant,
        "path": str(path.relative_to(REPO)),
        "rows": row_count,
        "elapsed_sum_ms": int(elapsed_sum_ms),
        "elapsed_avg_ms": round(elapsed_avg_ms, 3),
        "elapsed_p99_ms": round(elapsed_p99_ms, 3),
        "open_s": parse_open_seconds(stderr_path),
        "storage_us": storage_total,
        "probe_total_us": probe_total,
        "probe_stage_sum_us": stage_sum,
        "unaccounted_storage_us": unaccounted,
        "probe_count": counts["probe_total_latency"],
        "offset_lookup_us": sums["probe_offset_lookup_latency"],
        "body_read_us": sums["probe_body_read_latency"],
        "bloom_us": sums["probe_bloom_latency"],
        "setup_us": sums["probe_setup_latency"],
        "result_count": result_count,
        "neighbor_edges": neighbor_edges,
        **{f"csr_{key}": value for key, value in csr.items()},
        **{f"io_{key}": value for key, value in io.items()},
    }


def ratio(num, den):
    if not den:
        return ""
    return f"{num / den:.4f}"


def write_tsv(rows, path):
    headers = [
        "source",
        "suite",
        "scenario",
        "variant",
        "rows",
        "elapsed_sum_ms",
        "elapsed_avg_ms",
        "elapsed_p99_ms",
        "open_s",
        "storage_us",
        "probe_total_us",
        "probe_stage_sum_us",
        "unaccounted_storage_us",
        "probe_count",
        "offset_lookup_us",
        "body_read_us",
        "bloom_us",
        "setup_us",
        "offset_share_of_probe",
        "body_share_of_probe",
        "probe_share_of_storage",
        "csr_candidate_l0_segments",
        "csr_filter_passed_segments",
        "csr_bloom_filtered_segments",
        "csr_body_reads",
        "csr_body_bytes",
        "io_read_bytes",
        "io_read_syscalls",
        "result_count",
        "neighbor_edges",
        "path",
    ]
    with path.open("w", encoding="utf-8") as handle:
        handle.write("\t".join(headers) + "\n")
        for row in rows:
            values = []
            for key in headers:
                if key == "offset_share_of_probe":
                    values.append(ratio(row["offset_lookup_us"], row["probe_total_us"]))
                elif key == "body_share_of_probe":
                    values.append(ratio(row["body_read_us"], row["probe_total_us"]))
                elif key == "probe_share_of_storage":
                    values.append(ratio(row["probe_total_us"], row["storage_us"]))
                else:
                    values.append(str(row.get(key, "")))
            handle.write("\t".join(values) + "\n")


def select_rows(rows, source=None, scenarios=None, variants=None):
    result = rows
    if source:
        result = [r for r in result if r["source"] == source]
    if scenarios:
        result = [r for r in result if r["scenario"] in scenarios]
    if variants:
        result = [r for r in result if r["variant"] in variants]
    return result


def short_row(row):
    return (
        f"| {row['source']} | {row['scenario']} | {row['variant']} | "
        f"{row['rows']} | {row['elapsed_sum_ms']} | {row['open_s']} | "
        f"{row['probe_total_us']} | {row['offset_lookup_us']} | {row['body_read_us']} | "
        f"{row['csr_candidate_l0_segments']} | {row['csr_filter_passed_segments']} | "
        f"{row['csr_body_reads']} | {row['csr_body_bytes']} |"
    )


def write_md(rows, path, tsv_name):
    focus = []
    focus.extend(select_rows(rows, "W6", scenarios={"core"}, variants={"schema", "edge-type-only", "budg-b64", "semantic"}))
    focus.extend(select_rows(rows, "W8", scenarios={"property-presence", "property-equality", "2hop-typed"}, variants={"schema", "budg-b64", "semantic"}))
    focus.extend(select_rows(rows, "W14", scenarios={"property-required", "degree-class"}, variants={"schema", "edge-type-only", "budg-b64", "semantic"}))

    lines = [
        "# C10 Latency Attribution Summary",
        "",
        "Last update: 2026-06-17 14:00 CST",
        "",
        "## Scope",
        "",
        "This is an offline attribution pass. It reuses existing W6/W8/W14 JSON and stderr logs only; no new SF30/SF100 runner was started.",
        "",
        "Raw TSV:",
        "",
        "```text",
        f"baseline/{tsv_name}",
        "```",
        "",
        "## Focus Rows",
        "",
        "| Source | Scenario | Variant | rows | elapsed_sum_ms | open_s | probe_total_us | offset_lookup_us | body_read_us | candidate_l0 | filter_passed | body_reads | body_bytes |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    lines.extend(short_row(row) for row in focus)

    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- C10 confirms that latency cannot be inferred from read bytes alone. The available metrics split end-to-end storage time into CSR probe stages, and the dominant visible fixed cost remains offset/probe work plus engine open/materialization effects.",
            "- W14 degree-class is the clearest current evidence: schema spends about 775s in the 5000-sample run, while edge-type-only/budg-b64/semantic finish in seconds after reuse-store open. This supports schema-vs-pruned latency cliff avoidance.",
            "- W14 does not support a strong composite-over-edge-type-only latency claim: edge-type-only, budg-b64, and semantic are close on degree-class, and property-required reports weak/zero candidate/body counters in the current JSON.",
            "- W6/W8 should be used for read-amplification and candidate/body-read attribution. C10 should label latency as explained/qualified, not as a standalone broad advantage claim.",
            "",
            "## Verdict",
            "",
            "```text",
            "G2/C10 verdict: FALLBACK / CLAIM-SAFETY",
            "Allowed in paper: explain why read/candidate reductions do not always translate to latency; use degree-class schema-vs-pruned cliff as bounded latency evidence.",
            "Not allowed in paper: broad SemL0 latency superiority, or composite semantic latency superiority over edge-type-only.",
            "Next gate: G3/S0 can run only as a small targeted semantic-dilution diagnosis, not as full S3 implementation.",
            "```",
            "",
            "## Next Minimal Action",
            "",
            "Run S0 only if it reuses existing stores/logs or stays small enough to diagnose semantic dilution. Do not start S1/S2/S3 implementation.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    rows = [summarize_file(*entry) for entry in bench_files()]
    rows.sort(key=lambda r: (r["source"], r["suite"], r["scenario"], r["variant"]))
    tsv = OUT_DIR / "c10-latency-attribution-summary-20260617.tsv"
    md = OUT_DIR / "c10-latency-attribution-summary-20260617-cn.md"
    write_tsv(rows, tsv)
    write_md(rows, md, tsv.name)
    print(f"rows={len(rows)}")
    print(tsv)
    print(md)


if __name__ == "__main__":
    main()
