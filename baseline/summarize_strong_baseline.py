#!/usr/bin/env python3
"""Summarize a strong-baseline run produced by codex_qslsm_sf100_strong_baseline.sh.

Reads, under a run OUT_DIR (remote-logs/<run-id>/):
  - <variant>-bench.json   (storage-bench; neighbor_summary per edge type)
  - <variant>-stats.json   (levels = [L0, L1, ...])
  - <variant>-import.stderr (directed_edges / input_rows / elapsed_s)
  - manifest.tsv           (store_size_bytes per variant)

Optionally merges reused rows from a prior e11 run's `e11-variants-<scale>.tsv`
(e.g. naive/lsmgraph-style/label-only/degree-only) so a partial fresh run can still
produce a complete table. Pass --reuse-tsv and --reuse-variants.

Writes a markdown report (P1 read-amplification + P2 write-cost + budget-sweep curve)
and a machine-readable TSV.
"""
from __future__ import annotations
import argparse
import csv
import json
import pathlib
import re
import sys

NSUM_SUM_KEYS = [
    "read_bytes", "candidate_l0_segments", "filter_passed_segments",
    "matched_l0_segments", "bloom_filtered_segments",
    "header_reads", "offset_reads", "body_reads", "get_neighbors_ops",
]


def load_json(p: pathlib.Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def agg_bench(path: pathlib.Path) -> dict | None:
    d = load_json(path)
    if not d:
        return None
    out = {k: 0 for k in NSUM_SUM_KEYS}
    out["weighted_avg_us"] = 0.0
    out["max_p99_us"] = 0.0
    out["simulated"] = False
    for b in d.get("benchmarks", []):
        s = b.get("neighbor_summary") or {}
        # kv_style_baseline.py emits model-derived JSONs (kv_* fields) that must
        # never be presented as measured numbers.
        if any(k.startswith("kv_") for k in s):
            out["simulated"] = True
        ops = s.get("get_neighbors_ops", 0) or 0
        for k in NSUM_SUM_KEYS:
            out[k] += s.get(k, 0) or 0
        out["weighted_avg_us"] += (s.get("get_neighbors_avg_us") or 0) * ops
        out["max_p99_us"] = max(out["max_p99_us"], s.get("get_neighbors_p99_us") or 0)
    out["avg_us"] = round(out["weighted_avg_us"] / out["get_neighbors_ops"], 1) if out["get_neighbors_ops"] else 0
    out["scan_edges"] = d.get("scan_edges")
    return out


def l0_files(stats_path: pathlib.Path):
    d = load_json(stats_path)
    if not d:
        return None
    lv = d.get("levels")
    if isinstance(lv, list) and lv:
        return lv[0]
    return None


def import_facts(stderr_path: pathlib.Path) -> dict:
    facts = {"directed_edges": None, "input_rows": None, "import_s": None}
    if not stderr_path.exists():
        return facts
    txt = stderr_path.read_text(encoding="utf-8", errors="ignore")
    for key, pat in (("directed_edges", r"directed_edges=(\d+)"),
                     ("input_rows", r"input_rows=(\d+)")):
        m = re.findall(pat, txt)
        if m:
            facts[key] = int(m[-1])
    m = re.findall(r"total_elapsed_s=([\d.]+)", txt) or re.findall(r"elapsed_s=([\d.]+)", txt)
    if m:
        facts["import_s"] = float(m[-1])
    return facts


def store_sizes(manifest: pathlib.Path) -> dict:
    sizes = {}
    if not manifest.exists():
        return sizes
    with manifest.open() as f:
        for row in csv.DictReader(f, delimiter="\t"):
            try:
                sizes[row["variant"]] = max(sizes.get(row["variant"], 0), int(row["store_size_bytes"]))
            except (KeyError, ValueError):
                pass
    return sizes


def collect(out_dir: pathlib.Path) -> dict[str, dict]:
    rows = {}
    sizes = store_sizes(out_dir / "manifest.tsv")
    for bench in sorted(out_dir.glob("*-bench.json")):
        variant = bench.name[: -len("-bench.json")]
        if variant.endswith("-feedback"):
            continue
        a = agg_bench(bench)
        if a is None:
            continue
        a["variant"] = variant
        a["l0_files"] = l0_files(out_dir / f"{variant}-stats.json")
        a["store_bytes"] = sizes.get(variant)
        a.update(import_facts(out_dir / f"{variant}-import.stderr"))
        rows[variant] = a
    return rows


def reuse_rows(tsv: pathlib.Path, variants: list[str]) -> dict[str, dict]:
    rows = {}
    if not tsv.exists():
        return rows
    with tsv.open() as f:
        for r in csv.DictReader(f, delimiter="\t"):
            name = r.get("vid") or r.get("variant") or ""
            if variants and name not in variants:
                continue
            def gi(*keys):
                for k in keys:
                    v = r.get(k)
                    if v not in (None, "", "N/A"):
                        try:
                            return int(float(v))
                        except ValueError:
                            return None
                return None
            rows[name] = {
                "variant": name, "reused": True,
                "read_bytes": gi("read_bytes", "neighbor_read_bytes"),
                "candidate_l0_segments": gi("cand_l0", "candidate_l0_segments", "neighbor_candidate_l0_segments"),
                "body_reads": gi("body_reads", "neighbor_body_reads"),
                "header_reads": gi("header_reads", "neighbor_header_reads"),
                "l0_files": gi("l0_files", "L0_files"),
                "store_bytes": gi("store_bytes"),
                "directed_edges": gi("directed_edges"),
                "import_s": None,
            }
    return rows


def mib(b):
    return f"{b/1048576:.2f}" if isinstance(b, (int, float)) else "N/A"


def gib(b):
    return f"{b/1073741824:.1f}" if isinstance(b, (int, float)) else "N/A"


def fmt(v):
    return f"{v:,}" if isinstance(v, int) else ("N/A" if v is None else str(v))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--scale", default="sf100")
    ap.add_argument("--reuse-tsv", default=None)
    ap.add_argument("--reuse-variants", default="naive,lsmgraph-style,label-only,degree-only")
    ap.add_argument("--report", default=None)
    args = ap.parse_args()

    out_dir = pathlib.Path(args.out_dir)
    cfg = load_json(out_dir / "run-config.json") or {}
    rows = collect(out_dir)
    if args.reuse_tsv:
        reused = reuse_rows(pathlib.Path(args.reuse_tsv), args.reuse_variants.split(","))
        for k, v in reused.items():
            rows.setdefault(k, v)

    order = ["naive", "lsmgraph-style", "label-only", "edge-type-only", "schema",
             "degree-only", "semantic"]
    order += sorted([v for v in rows if v.startswith("budg-")],
                    key=lambda s: int(re.sub(r"\D", "", s) or 0))
    order += [v for v in rows if v not in order]
    seen = set()
    ordered = [v for v in order if v in rows and not (v in seen or seen.add(v))]

    edges = next((rows[v]["directed_edges"] for v in ordered
                  if rows[v].get("directed_edges")), None)
    md = [f"# Strong baseline ({args.scale}) — run {cfg.get('run_id','?')}",
          "",
          f"- input: `{cfg.get('input','?')}`  scale: **{args.scale}**  samples: {cfg.get('samples','?')}",
          f"- directed edges: **{fmt(edges)}**  edge_types: {cfg.get('edge_types','?')}",
          f"- budget sweep: {cfg.get('budget_sweep','?')}  (byte gate disabled; file budget is the cost axis)",
          "",
          "## P1 — Read amplification (shared sample plan)",
          "",
          "| Variant | read MiB | candidate L0 | body reads | header reads | avg us | L0 files | source |",
          "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    def src_label(r):
        if r.get("reused"):
            return "reused-e11"
        if r.get("simulated"):
            return "simulated(model)"
        return "measured"

    for v in ordered:
        r = rows[v]
        md.append(f"| {v} | {mib(r.get('read_bytes'))} | {fmt(r.get('candidate_l0_segments'))} | "
                  f"{fmt(r.get('body_reads'))} | {fmt(r.get('header_reads'))} | "
                  f"{r.get('avg_us','N/A')} | {fmt(r.get('l0_files'))} | {src_label(r)} |")
    if any(rows[v].get("simulated") for v in ordered):
        md += ["", "`simulated(model)` rows are derived analytically (kv_style_baseline.py) "
                   "from the schema bench json — not a real engine run; do not quote them as "
                   "measured numbers. A real run is available via `--l0-layout kv-lsm`."]
    md += ["", "## P2 — Maintenance cost", "",
           "| Variant | store GiB | L0 files | import s | source |",
           "| --- | ---: | ---: | ---: | --- |"]
    for v in ordered:
        r = rows[v]
        md.append(f"| {v} | {gib(r.get('store_bytes'))} | {fmt(r.get('l0_files'))} | "
                  f"{r.get('import_s') or 'N/A'} | {src_label(r)} |")

    schema_rb = rows.get("schema", {}).get("read_bytes")
    sem_rb = rows.get("semantic", {}).get("read_bytes")
    md += ["", "## Budget sweep (SemL0 controllable cost)", ""]
    if isinstance(schema_rb, int) and isinstance(sem_rb, int):
        md.append(f"schema = {mib(schema_rb)} MiB read; full-semantic = {mib(sem_rb)} MiB read. "
                  "Budgeted should interpolate monotonically and stay <= schema.")
    md.append("")
    md.append("| budget (extra L0 files) | read MiB | L0 files | vs schema |")
    md.append("| --- | ---: | ---: | ---: |")
    for v in ordered:
        if not v.startswith("budg-"):
            continue
        r = rows[v]
        b = re.sub(r"\D", "", v)
        vs = (f"{(1 - r['read_bytes']/schema_rb)*100:.1f}% less"
              if isinstance(schema_rb, int) and isinstance(r.get("read_bytes"), int) and schema_rb else "N/A")
        md.append(f"| {b} | {mib(r.get('read_bytes'))} | {fmt(r.get('l0_files'))} | {vs} |")

    report = pathlib.Path(args.report) if args.report else out_dir / f"summary-{args.scale}.md"
    report.write_text("\n".join(md) + "\n", encoding="utf-8")
    # machine-readable; lands next to --report so regeneration never overwrites
    # the original run dir's TSV unless explicitly asked to.
    tsv = report.parent / f"summary-{args.scale}.tsv" if args.report else out_dir / f"summary-{args.scale}.tsv"
    cols = ["variant", "read_bytes", "candidate_l0_segments", "body_reads", "header_reads",
            "avg_us", "l0_files", "store_bytes", "directed_edges", "import_s", "reused", "simulated"]
    with tsv.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter="\t", extrasaction="ignore")
        w.writeheader()
        for v in ordered:
            w.writerow({**{c: rows[v].get(c) for c in cols}, "variant": v})
    print(str(report))
    print(str(tsv))


if __name__ == "__main__":
    main()
