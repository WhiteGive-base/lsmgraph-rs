#!/usr/bin/env python3
"""Extract E11 SemL0 ablation-matrix results into normalized CSV + markdown tables.

Reads the per-variant logs produced by run-e11-baseline-matrix.sh under
  remote-logs/e11-{vid}-{date_tag}/
and writes, under remote-logs/e11-normalized-{date_tag}/:
  - e11-variants-{scale}.tsv          full machine-readable per-variant rows
  - e11-mechanism-isolation.csv       render-e11-baseline-tables.py compatible
  - e11-tables-{scale}.md             P1 read-amplification + P2 write-cost tables

This extractor is self-contained: it does not depend on summarize-e11 (which has
drifted from the matrix output naming). Field names match the lsmgraph
storage-bench `neighbor_summary` / neighbor-compare JSON emitted by this binary.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_VARIANTS = [
    "schema_only", "naive", "lsmgraph_style", "label_only", "edge_type_only",
    "degree_only", "full_semantic", "benefit_scored", "full_compact",
]

DISPLAY = {
    "naive": "Naive L0 scan",
    "lsmgraph_style": "LSMGraph-style LSM-CSR",
    "schema_only": "Schema-only",
    "label_only": "Label-only",
    "edge_type_only": "Edge-type-only",
    "degree_only": "Degree-only / degree-aware",
    "full_semantic": "Full semantic (upper bound)",
    "benefit_scored": "Budgeted semantic / SemL0",
    "full_compact": "Full L0->L1 compact",
}
SIGNAL = {
    "naive": "unstructured L0 scan",
    "lsmgraph_style": "key/range only",
    "schema_only": "src_label",
    "label_only": "src_label only",
    "edge_type_only": "edge_type only",
    "degree_only": "degree_class only",
    "full_semantic": "label+edge_type+degree",
    "benefit_scored": "benefit-scored subset",
    "full_compact": "L0 eliminated",
}

# metric keys that are additive counters (summed across bench entries per repeat)
ADDITIVE = {
    "read_bytes", "body_reads", "body_bytes", "header_reads", "header_bytes",
    "offset_reads", "offset_bytes", "full_scan_reads",
    "candidate_l0_segments", "matched_l0_segments", "filter_passed_segments",
    "range_filtered_segments", "bloom_filtered_segments",
    "bloom_false_positive_probes", "offset_cache_hits", "offset_cache_misses",
    "get_neighbors_ops",
}
# latency keys (percentiles): aggregated as simple mean across entries
LATENCY = {
    "get_neighbors_avg_us", "get_neighbors_p50_us", "get_neighbors_p90_us",
    "get_neighbors_p95_us", "get_neighbors_p99_us",
}


def num(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if s in ("", "N/A", "NA", "null", "None"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def read_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def parse_tsv(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                out[parts[0]] = parts[1]
    except OSError:
        pass
    return out


def parse_wall_seconds(wall: str) -> float:
    """'h:mm:ss', 'm:ss.ss' or '12.3s' -> seconds."""
    wall = wall.strip().rstrip("s")
    if not wall:
        return 0.0
    if ":" in wall:
        parts = [float(p) for p in wall.split(":")]
        sec = 0.0
        for p in parts:
            sec = sec * 60 + p
        return sec
    try:
        return float(wall)
    except ValueError:
        return 0.0


def parse_time_log(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "Elapsed (wall clock)" in line:
                out["wall_time"] = line.rsplit("):", 1)[-1].strip()
            elif "Maximum resident set size" in line:
                out["max_rss_kb"] = line.split(":", 1)[1].strip()
    except OSError:
        pass
    return out


def aggregate_fair(logdir: Path, kind: str, samples: int) -> Dict[str, float]:
    """Average storage-bench neighbor_summary over 3 repeats.

    Additive counters are summed across bench entries (the whole workload) per
    repeat, then averaged across repeats. Latency percentiles are meaned.
    """
    per_repeat_add: List[Dict[str, float]] = []
    per_repeat_lat: List[Dict[str, float]] = []
    per_repeat_elapsed: List[float] = []
    per_repeat_neighbors: List[float] = []
    for r in (1, 2, 3):
        data = read_json(logdir / f"fair-{kind}-s{samples}-r{r}.json")
        if not isinstance(data, dict):
            continue
        add: Dict[str, float] = {}
        lat_acc: Dict[str, List[float]] = {}
        elapsed = 0.0
        neighbors = 0.0
        for bench in data.get("benchmarks", []):
            if not isinstance(bench, dict):
                continue
            summary = bench.get("neighbor_summary", {})
            if not isinstance(summary, dict):
                continue
            for k, v in summary.items():
                if k.endswith("_us"):  # latency percentiles -> mean, never summed
                    lat_acc.setdefault(k, []).append(num(v))
                else:                  # counters/bytes -> summed across bench entries
                    add[k] = add.get(k, 0.0) + num(v)
            elapsed += num(bench.get("get_neighbors_elapsed_ms"))
            neighbors += num(bench.get("neighbor_edges"))
        if add or lat_acc:
            per_repeat_add.append(add)
            per_repeat_lat.append({k: sum(vs) / len(vs) for k, vs in lat_acc.items() if vs})
            per_repeat_elapsed.append(elapsed)
            per_repeat_neighbors.append(neighbors)
    if not per_repeat_add:
        return {}
    n = len(per_repeat_add)
    out: Dict[str, float] = {}
    keys = set()
    for d in per_repeat_add:
        keys.update(d.keys())
    for k in keys:
        out[k] = round(sum(d.get(k, 0.0) for d in per_repeat_add) / n, 3)
    latkeys = set()
    for d in per_repeat_lat:
        latkeys.update(d.keys())
    for k in latkeys:
        vals = [d[k] for d in per_repeat_lat if k in d]
        out[k] = round(sum(vals) / len(vals), 3) if vals else 0.0
    out["elapsed_ms"] = round(sum(per_repeat_elapsed) / n, 3)
    out["neighbor_edges"] = round(sum(per_repeat_neighbors) / n, 3)
    return out


def aggregate_neighbor_compare(logdir: Path) -> Dict[str, float]:
    checked = passed = mism = 0.0
    found = False
    for kind in ("core", "alltypes"):
        data = read_json(logdir / f"neighbor-compare-{kind}.json")
        if isinstance(data, dict):
            found = True
            checked += num(data.get("checked"))
            passed += num(data.get("passed"))
            mism += num(data.get("mismatches"))
    if not found:
        return {}
    return {"checked": checked, "passed": passed, "mismatches": mism}


def extract_flush_compaction(logdir: Path) -> Dict[str, float]:
    """flush_latency avg + compaction in/out bytes from stats.log metrics."""
    data = read_json(logdir / "stats.log")
    out: Dict[str, float] = {}
    if not isinstance(data, dict):
        return out
    storage = (data.get("metrics") or {}).get("storage") or data.get("storage") or {}
    if isinstance(storage, dict):
        fl = storage.get("flush_latency") or {}
        if isinstance(fl, dict):
            out["flush_count"] = num(fl.get("count"))
            out["flush_avg_us"] = num(fl.get("avg_us") or fl.get("sum_us"))
        out["compaction_input_bytes"] = num(storage.get("compaction_input_bytes"))
        out["compaction_output_bytes"] = num(storage.get("compaction_output_bytes"))
    return out


def build_row(root: Path, scale: str, vid: str, date_tag: str, core_s: int, all_s: int) -> Dict[str, Any]:
    logdir = root / f"remote-logs/e11-{scale}-{vid}-{date_tag}"
    imp = read_json(logdir / "import.stdout") or {}
    fsum = parse_tsv(logdir / "file-summary.tsv")
    tlog = parse_time_log(logdir / "time.log")
    core = aggregate_fair(logdir, "core", core_s)
    allt = aggregate_fair(logdir, "alltypes", all_s)
    cmp = aggregate_neighbor_compare(logdir)
    fc = extract_flush_compaction(logdir)

    wall = tlog.get("wall_time", "")
    import_s = parse_wall_seconds(wall)
    directed = num(imp.get("directed_edges"))
    row: Dict[str, Any] = {
        "variant_id": vid,
        "variant_name": DISPLAY.get(vid, vid),
        "layout_signal": SIGNAL.get(vid, ""),
        "import_wall_time": wall or "N/A",
        "import_s": round(import_s, 2) if import_s else "N/A",
        "import_max_rss_kb": tlog.get("max_rss_kb", "N/A"),
        "directed_edges": int(directed) if directed else "N/A",
        "update_throughput_eps": round(directed / import_s) if import_s > 0 else "N/A",
        "store_bytes": fsum.get("bytes", "N/A"),
        "l0_files": fsum.get("l0_files", "N/A"),
        "l0_bytes": fsum.get("l0_bytes", "N/A"),
        "manifest_bytes": fsum.get("manifest_bytes", "N/A"),
        "flush_count": int(fc.get("flush_count", 0)) if fc else "N/A",
        "compaction_input_bytes": int(fc.get("compaction_input_bytes", 0)) if fc else "N/A",
        "compaction_output_bytes": int(fc.get("compaction_output_bytes", 0)) if fc else "N/A",
        "neighbor_checked": int(cmp.get("checked", 0)) if cmp else ("0" if vid == "schema_only" else "N/A"),
        "neighbor_mismatches": int(cmp.get("mismatches", 0)) if cmp else ("0" if vid == "schema_only" else "N/A"),
    }
    for prefix, agg in (("core", core), ("alltypes", allt)):
        for k, v in agg.items():
            row[f"{prefix}_{k}"] = v
    return row


def md_table(headers: List[str], rows: List[List[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:,.0f}" if v >= 100 else f"{v:,.2f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def pct_red(base: float, val: float) -> str:
    if base <= 0:
        return "N/A"
    return f"{(base - val) / base * 100:.1f}%"


def write_outputs(root: Path, scale: str, date_tag: str, rows: List[Dict[str, Any]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    # full machine-readable TSV
    if rows:
        cols: List[str] = []
        for r in rows:
            for k in r:
                if k not in cols:
                    cols.append(k)
        with (out_dir / f"e11-variants-{scale}.tsv").open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, delimiter="\t", extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    # render-e11 compatible CSV (comma)
    with (out_dir / "e11-mechanism-isolation.csv").open("w", encoding="utf-8", newline="") as fh:
        if rows:
            cols = list(rows[0].keys())
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    by_id = {r["variant_id"]: r for r in rows}
    naive_rb = num(by_id.get("naive", {}).get("core_read_bytes"))
    base_rb = naive_rb or num(by_id.get("schema_only", {}).get("core_read_bytes"))

    # P1 read-amplification table
    p1_headers = ["Variant", "L0 signal", "cand. L0 segs", "read bytes",
                  "vs baseline", "header reads", "body reads", "avg us", "p99 us", "mismatches"]
    p1_rows = []
    for vid in DEFAULT_VARIANTS:
        r = by_id.get(vid)
        if not r:
            continue
        rb = num(r.get("core_read_bytes"))
        p1_rows.append([
            r["variant_name"], r["layout_signal"],
            fmt(r.get("core_candidate_l0_segments", "N/A")),
            fmt(rb) if rb else "N/A",
            pct_red(base_rb, rb) if base_rb else "N/A",
            fmt(r.get("core_header_reads", "N/A")),
            fmt(r.get("core_body_reads", "N/A")),
            fmt(r.get("core_get_neighbors_avg_us", "N/A")),
            fmt(r.get("core_get_neighbors_p99_us", "N/A")),
            r.get("neighbor_mismatches", "N/A"),
        ])

    # P2 write/maintenance-cost table
    p2_headers = ["Variant", "import s", "throughput e/s", "store bytes", "L0 files",
                  "L0 bytes", "manifest bytes", "max RSS kb"]
    p2_rows = []
    for vid in DEFAULT_VARIANTS:
        r = by_id.get(vid)
        if not r:
            continue
        p2_rows.append([
            r["variant_name"], r.get("import_s", "N/A"), fmt(r.get("update_throughput_eps", "N/A")),
            fmt(num(r.get("store_bytes"))) if r.get("store_bytes") not in (None, "N/A") else "N/A",
            r.get("l0_files", "N/A"),
            fmt(num(r.get("l0_bytes"))) if r.get("l0_bytes") not in (None, "N/A") else "N/A",
            r.get("manifest_bytes", "N/A"), r.get("import_max_rss_kb", "N/A"),
        ])

    md = [f"# E11 SemL0 Ablation Tables ({scale}, date_tag={date_tag})", "",
          "## P1 — Read-amplification comparison (core query workload; counters are deterministic, single pass)",
          "", md_table(p1_headers, p1_rows), "",
          "_'vs baseline' = read-byte reduction relative to the naive/schema baseline._", "",
          "## P2 — Write / maintenance cost",
          "", md_table(p2_headers, p2_rows), ""]
    (out_dir / f"e11-tables-{scale}.md").write_text("\n".join(md), encoding="utf-8")
    print(str(out_dir / f"e11-tables-{scale}.md"))


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract E11 SemL0 ablation tables.")
    ap.add_argument("--root", default=".")
    ap.add_argument("--scale", required=True)
    ap.add_argument("--date-tag", required=True)
    ap.add_argument("--variants", default=",".join(DEFAULT_VARIANTS))
    ap.add_argument("--core-samples", type=int, default=200)
    ap.add_argument("--alltypes-samples", type=int, default=50)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    root = Path(args.root)
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    out_dir = Path(args.out_dir) if args.out_dir else root / f"remote-logs/e11-normalized-{args.date_tag}"
    rows = []
    for vid in variants:
        if not (root / f"remote-logs/e11-{args.scale}-{vid}-{args.date_tag}").is_dir():
            continue
        rows.append(build_row(root, args.scale, vid, args.date_tag, args.core_samples, args.alltypes_samples))
    write_outputs(root, args.scale, args.date_tag, rows, out_dir)


if __name__ == "__main__":
    main()
