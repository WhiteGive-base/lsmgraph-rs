#!/usr/bin/env python3
import csv
import sys
from pathlib import Path


REPO = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/data/WorkSpace/lsmgraph-rs")
IN_TSV = REPO / "baseline/c10-latency-attribution-summary-20260617.tsv"
OUT_TSV = REPO / "baseline/s0-semantic-dilution-summary-20260617.tsv"
OUT_MD = REPO / "baseline/s0-semantic-dilution-summary-20260617-cn.md"


def as_int(row, key):
    try:
        return int(float(row.get(key, "") or 0))
    except ValueError:
        return 0


def as_float(row, key):
    try:
        return float(row.get(key, "") or 0)
    except ValueError:
        return 0.0


def ratio(a, b):
    if not b:
        return ""
    return f"{a / b:.3f}"


def load_rows():
    with IN_TSV.open("r", encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def by_key(rows):
    result = {}
    for row in rows:
        key = (row["source"], row["scenario"], row["variant"])
        result[key] = row
    return result


def compare(rows, source, scenario, variant):
    schema = rows.get((source, scenario, "schema"))
    other = rows.get((source, scenario, variant))
    if not schema or not other:
        return None
    fields = [
        "elapsed_sum_ms",
        "csr_candidate_l0_segments",
        "csr_filter_passed_segments",
        "csr_body_reads",
        "csr_body_bytes",
        "io_read_bytes",
        "offset_lookup_us",
    ]
    out = {
        "source": source,
        "scenario": scenario,
        "variant": variant,
    }
    for field in fields:
        out[f"schema_{field}"] = schema.get(field, "")
        out[f"variant_{field}"] = other.get(field, "")
        out[f"{field}_ratio_schema_over_variant"] = ratio(as_float(schema, field), as_float(other, field))
    return out


def main():
    rows = by_key(load_rows())
    comparisons = []
    for source, scenario, variants in [
        ("W14", "degree-class", ["edge-type-only", "budg-b64", "semantic"]),
        ("W14", "property-required", ["edge-type-only", "budg-b64", "semantic"]),
        ("W8", "2hop-typed", ["budg-b64", "semantic"]),
        ("W8", "property-presence", ["budg-b64", "semantic"]),
        ("W8", "property-equality", ["budg-b64", "semantic"]),
        ("W6", "core", ["edge-type-only", "budg-b64", "semantic"]),
    ]:
        for variant in variants:
            item = compare(rows, source, scenario, variant)
            if item:
                comparisons.append(item)

    headers = list(comparisons[0].keys()) if comparisons else []
    with OUT_TSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, delimiter="\t")
        writer.writeheader()
        writer.writerows(comparisons)

    def line(item):
        return (
            f"| {item['source']} | {item['scenario']} | {item['variant']} | "
            f"{item['elapsed_sum_ms_ratio_schema_over_variant']} | "
            f"{item['csr_filter_passed_segments_ratio_schema_over_variant']} | "
            f"{item['csr_body_reads_ratio_schema_over_variant']} | "
            f"{item['io_read_bytes_ratio_schema_over_variant']} | "
            f"{item['offset_lookup_us_ratio_schema_over_variant']} |"
        )

    md = [
        "# S0 Semantic Dilution Diagnosis",
        "",
        "Last update: 2026-06-17 14:07 CST",
        "",
        "## Scope",
        "",
        "This is an offline/proxy diagnosis. It does not run compaction, create a new store, or implement S1/S2/S3. It compares existing schema/coarse evidence against semantic-pruned variants using C10 attribution rows.",
        "",
        "Raw TSV:",
        "",
        "```text",
        f"baseline/{OUT_TSV.name}",
        "```",
        "",
        "## Proxy Dilution Table",
        "",
        "Ratio means `schema/coarse baseline divided by variant`. Larger than 1 means the variant avoids work that the coarse layout cannot prune.",
        "",
        "| Source | Scenario | Variant | elapsed ratio | filter-passed ratio | body-read ratio | read-byte ratio | offset-lookup ratio |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    md.extend(line(item) for item in comparisons)
    md.extend(
        [
            "",
            "## Interpretation",
            "",
            "- W14 degree-class is a strong proxy signal for pruning-surface loss: schema/coarse behavior passes far more segments than edge-type-only/budg-b64/semantic and pays a much larger offset/probe cost.",
            "- W8 2-hop and property scenarios provide smaller but useful proxy evidence that property/query-aware pruning can reduce passed segments and latency versus schema.",
            "- W6 core is weak for S0: semantic is not consistently better than edge-type-only/budg-b64, so it should not be used as evidence for composite semantic superiority.",
            "- This is still proxy evidence. It does not directly measure post-compaction L1/L2 exactness retention, schema/tombstone fallback after merge, or rewrite cost.",
            "",
            "## Verdict",
            "",
            "```text",
            "G3/S0 verdict: FALLBACK / PROXY SIGNAL",
            "What it supports: semantic dilution is plausible and visible when exact pruning surfaces are replaced by coarse/schema behavior.",
            "What it does not support: starting S1/S2/S3 implementation for this submission, or claiming measured multi-level semantic compaction.",
            "Paper handling: write as limitation/future-work motivation unless a later small targeted compaction diagnosis is explicitly approved.",
            "```",
            "",
            "## Next Minimal Action",
            "",
            "Do not start full S3. Return to W10 claim safety / writing freeze with W14, C10, and S0 caveats integrated.",
            "",
        ]
    )
    OUT_MD.write_text("\n".join(md), encoding="utf-8")
    print(f"rows={len(comparisons)}")
    print(OUT_TSV)
    print(OUT_MD)


if __name__ == "__main__":
    main()
