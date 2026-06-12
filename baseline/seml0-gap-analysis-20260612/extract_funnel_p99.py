#!/usr/bin/env python3
"""Zero-rerun extraction of (a) latency percentiles and (b) the L0 pruning funnel
from existing SF100 strong-baseline bench JSONs.

Reads:  remote-logs/qslsm-sf100-strong-baseline-20260610/*-bench.json  (read-only)
Writes: latency-percentiles-sf100.{tsv,md}, funnel-sf100.{tsv,md} next to this script.

Percentiles are merged across edge types by summing the per-benchmark latency
histogram buckets (neighbor_metrics.storage.get_neighbors_latency.buckets).
Buckets are coarse (16 fixed bounds, see src/metrics.rs LATENCY_BUCKET_US), so we
report both the conservative bucket upper bound (p*_ub) and a linear
interpolation within the bucket (p*_interp). Numbers are for internal analysis;
final paper numbers need finer buckets (plan item C9).

kv-style-bench.json is a Python simulation derived from the schema bench json
(see codex_qslsm_sf100_strong_baseline.sh, step 4); it is tagged simulated and
has no real funnel counters.
"""

import glob
import json
import os
import sys

RUN_DIR = "/data/WorkSpace/lsmgraph-rs/remote-logs/qslsm-sf100-strong-baseline-20260610"
CROSS_TSV = "/data/WorkSpace/lsmgraph-rs/baseline/sf100-results-s5000.tsv"
OUT_DIR = os.path.dirname(os.path.abspath(__file__))

VARIANT_ORDER = [
    "naive", "schema", "edge-type-only", "semantic",
    "budg-b64", "budg-b256", "budg-b1024", "kv-style",
]
SIMULATED = {"kv-style"}

FUNNEL_FIELDS = [
    # (json key, output column) — stage order matches the read path in
    # src/graph.rs:2081-2145: candidate -> range filter -> bloom -> filter
    # passed -> body read -> matched.
    ("candidate_l0_segments", "candidate"),
    ("range_filtered_segments", "range_filtered"),
    ("bloom_filtered_segments", "bloom_filtered"),
    ("filter_passed_segments", "filter_passed"),
    ("body_reads", "body_reads"),
    ("matched_l0_segments", "matched"),
    ("read_bytes", "read_bytes"),
    ("body_bytes", "body_bytes"),
]


def load_variant(path):
    with open(path) as f:
        doc = json.load(f)
    name = doc.get("baseline") or os.path.basename(path).replace("-bench.json", "")
    return name, doc.get("benchmarks", [])


def summary_values(benchmarks, key):
    vals = []
    for b in benchmarks:
        ns = b.get("neighbor_summary") or {}
        vals.append(ns.get(key))
    return vals


def detect_cumulative(per_type_ops):
    """True if per-benchmark summaries are cumulative (monotone growth well
    beyond a single edge type's op count) instead of per-type deltas."""
    nums = [v for v in per_type_ops if isinstance(v, (int, float))]
    if len(nums) < 2:
        return False
    return nums[-1] >= 1.8 * nums[0] and all(b >= a for a, b in zip(nums, nums[1:]))


def merged_buckets(benchmarks):
    """Sum latency histogram buckets across edge types; returns (bounds, counts,
    total, sum_us, cumulative_flag)."""
    snapshots = []
    for b in benchmarks:
        node = (((b.get("neighbor_metrics") or {}).get("storage") or {})
                .get("get_neighbors_latency") or {})
        if node.get("buckets"):
            snapshots.append(node)
    if not snapshots:
        return None
    counts_list = [[bk["count"] for bk in s["buckets"]] for s in snapshots]
    bounds = [bk["upper_bound_us"] for bk in snapshots[0]["buckets"]]
    totals = [sum(c) for c in counts_list]
    cumulative = detect_cumulative(totals)
    if cumulative:
        merged = counts_list[-1]
        count = sum(merged)
        sum_us = snapshots[-1].get("avg_us", 0) * count
    else:
        merged = [sum(col) for col in zip(*counts_list)]
        count = sum(merged)
        sum_us = sum(s.get("avg_us", 0) * t for s, t in zip(snapshots, totals))
    return bounds, merged, count, sum_us, cumulative


def percentile(bounds, counts, total, q, interpolate):
    if total == 0:
        return 0.0
    target = q * total
    acc = 0
    prev_bound = 0
    for bound, c in zip(bounds, counts):
        if acc + c >= target:
            if not interpolate or c == 0:
                return float(bound)
            frac = (target - acc) / c
            return prev_bound + frac * (bound - prev_bound)
        acc += c
        prev_bound = bound
    return float(bounds[-1])


def fmt(v):
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.1f}"
    return f"{v:,}" if isinstance(v, int) and abs(v) >= 10000 else str(v)


def main():
    paths = {}
    for p in glob.glob(os.path.join(RUN_DIR, "*-bench.json")):
        name = os.path.basename(p).replace("-bench.json", "")
        paths[name] = p
    variants = [v for v in VARIANT_ORDER if v in paths]
    variants += sorted(set(paths) - set(variants))

    cross = {}
    if os.path.exists(CROSS_TSV):
        with open(CROSS_TSV) as f:
            header = f.readline().strip().split("\t")
            for line in f:
                row = dict(zip(header, line.strip().split("\t")))
                cross[row["variant"]] = row

    lat_rows, funnel_rows, checks = [], [], []
    for name in variants:
        vname, benchmarks = load_variant(paths[name])
        source = "simulated(model)" if name in SIMULATED else "measured"

        # ---- funnel (sum per-type summaries; handle cumulative defensively)
        frow = {"variant": name, "source": source}
        ops_series = summary_values(benchmarks, "get_neighbors_ops")
        cumulative = detect_cumulative(ops_series)
        for key, col in FUNNEL_FIELDS:
            vals = [v for v in summary_values(benchmarks, key) if v is not None]
            if not vals:
                frow[col] = None
            else:
                frow[col] = vals[-1] if cumulative else sum(vals)
        frow["ops"] = (ops_series[-1] if cumulative
                       else sum(v or 0 for v in ops_series))
        funnel_rows.append(frow)

        if name in cross and frow.get("candidate") is not None:
            ref = int(cross[name]["candidate_l0"])
            ok = ref == frow["candidate"]
            checks.append((name, "candidate_l0", frow["candidate"], ref, ok))
            ref_rb = int(cross[name]["read_bytes"])
            checks.append((name, "read_bytes", frow["read_bytes"], ref_rb,
                           ref_rb == frow["read_bytes"]))

        # ---- latency percentiles from merged histogram buckets
        mb = merged_buckets(benchmarks)
        lrow = {"variant": name, "source": source, "ops": frow["ops"]}
        if mb:
            bounds, counts, total, sum_us, cum = mb
            lrow["hist_count"] = total
            lrow["avg_us"] = sum_us / total if total else 0.0
            for q, label in [(0.50, "p50"), (0.90, "p90"), (0.99, "p99")]:
                lrow[f"{label}_ub_us"] = percentile(bounds, counts, total, q, False)
                lrow[f"{label}_interp_us"] = percentile(bounds, counts, total, q, True)
            lrow["merge_mode"] = "cumulative-last" if cum else "sum-per-type"
        else:
            # simulation has only per-type summary percentiles; report ops-weighted avg
            p99s = [v for v in summary_values(benchmarks, "get_neighbors_p99_us") if v]
            lrow["hist_count"] = None
            lrow["avg_us"] = None
            lrow["p99_ub_us"] = max(p99s) if p99s else None
            lrow["merge_mode"] = "per-type-summary-only"
        lat_rows.append(lrow)

    lat_cols = ["variant", "source", "ops", "hist_count", "avg_us",
                "p50_ub_us", "p50_interp_us", "p90_ub_us", "p90_interp_us",
                "p99_ub_us", "p99_interp_us", "merge_mode"]
    funnel_cols = (["variant", "source", "ops"] +
                   [c for _, c in FUNNEL_FIELDS])

    def write_tables(rows, cols, stem, title, notes):
        tsv = os.path.join(OUT_DIR, stem + ".tsv")
        with open(tsv, "w") as f:
            f.write("\t".join(cols) + "\n")
            for r in rows:
                f.write("\t".join("" if r.get(c) is None else str(r.get(c))
                                  for c in cols) + "\n")
        md = os.path.join(OUT_DIR, stem + ".md")
        with open(md, "w") as f:
            f.write(f"# {title}\n\n{notes}\n\n")
            f.write("| " + " | ".join(cols) + " |\n")
            f.write("|" + "|".join(["---"] * len(cols)) + "|\n")
            for r in rows:
                f.write("| " + " | ".join(fmt(r.get(c)) for c in cols) + " |\n")
        print(f"wrote {tsv}\nwrote {md}")

    write_tables(
        lat_rows, lat_cols, "latency-percentiles-sf100",
        "SF100 neighbor latency percentiles (merged across edge types)",
        "Source: per-edge-type histogram buckets in *-bench.json "
        "(neighbor_metrics.storage.get_neighbors_latency). Buckets are coarse "
        "(16 bounds, src/metrics.rs); `*_ub` = conservative bucket upper bound, "
        "`*_interp` = linear interpolation inside the bucket. Internal analysis "
        "only — final paper numbers need finer buckets (C9). kv-style is a "
        "simulation; no real histogram exists.")

    write_tables(
        funnel_rows, funnel_cols, "funnel-sf100",
        "SF100 L0 pruning funnel (per-stage segment counts, summed over 9 edge types)",
        "Stage order matches src/graph.rs:2081-2145: semantic-index candidates "
        "-> src-range filter -> SourceBloom -> filter_passed (body read issued) "
        "-> matched (non-empty). candidate = after semantic L0 index; "
        "range/bloom_filtered = pruned by that stage. kv-style is simulated.")

    print("\ncross-check vs baseline/sf100-results-s5000.tsv:")
    bad = 0
    for name, field, got, ref, ok in checks:
        status = "OK" if ok else "MISMATCH"
        if not ok:
            bad += 1
        print(f"  {name:16s} {field:14s} extracted={got:,} reference={ref:,} {status}")
    if bad:
        print(f"  -> {bad} mismatches; investigate before using tables")
        sys.exit(1)
    print("  all checks passed")


if __name__ == "__main__":
    main()
