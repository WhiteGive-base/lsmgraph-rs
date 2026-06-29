#!/usr/bin/env python3
"""Summarize the RQ3 feedback-tail experiment from raw JSONL outputs.

Arms (all --variant semantic, same workload; only L0 compaction strategy differs):
  feedback : score/read-amp targeted compaction (compact_best_l0_partition_by_score)
  full     : blind full L0->L1 compaction at the same cadence (fair non-strawman baseline)
  none     : no compaction (W9 as-run)

Deltas are reported vs the `none` arm. The compaction-fired check (rewrite bytes /
compaction count per arm) is the smoke gate: feedback/full must be > 0, none == 0.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
from typing import Any


def fmt_num(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    value = float(value)
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value)):,}"
    return f"{value:,.2f}"


def fmt_bytes(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    value = float(value)
    if value >= 1024**3:
        return f"{value / 1024**3:.2f} GiB"
    if value >= 1024**2:
        return f"{value / 1024**2:.2f} MiB"
    if value >= 1024:
        return f"{value / 1024:.2f} KiB"
    return f"{value:,.0f} B"


def pct_delta(new: float | None, base: float | None) -> str:
    if new is None or base in (None, 0):
        return "n/a"
    pct = (float(new) - float(base)) / float(base) * 100.0
    sign = "+" if pct > 0 else ""
    return f"{sign}{pct:.1f}%"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{lineno}: invalid JSONL: {exc}") from exc
    return rows


def checkpoint_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("event") == "checkpoint"]


def done_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in reversed(rows):
        if row.get("event") == "done":
            return row
    return None


def nested(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
    cur: Any = row
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def sum_nested(rows: list[dict[str, Any]], *keys: str) -> float:
    return sum(float(nested(row, *keys, default=0) or 0) for row in rows)


def max_nested(rows: list[dict[str, Any]], *keys: str) -> float | None:
    values = [float(nested(row, *keys)) for row in rows if nested(row, *keys) is not None]
    return max(values) if values else None


def mean_nested(rows: list[dict[str, Any]], *keys: str) -> float | None:
    values = [float(nested(row, *keys)) for row in rows if nested(row, *keys) is not None]
    return mean(values) if values else None


def summarize_arm(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cps = checkpoint_rows(rows)
    done = done_row(rows)
    first_p99 = nested(cps[0], "latency_us", "p99") if cps else None
    last_p99 = nested(cps[-1], "latency_us", "p99") if cps else None
    slope = None
    if first_p99 not in (None, 0) and last_p99 is not None:
        slope = float(last_p99) / float(first_p99)
    return {
        "line_count": len(rows),
        "checkpoint_count": len(cps),
        "done": done is not None,
        "last_event": rows[-1].get("event") if rows else "missing",
        "total_queries": sum_nested(cps, "queries"),
        "mean_candidate_l0": mean_nested(cps, "candidate_l0_segments", "mean"),
        "mean_p50_us": mean_nested(cps, "latency_us", "p50"),
        "mean_p99_us": mean_nested(cps, "latency_us", "p99"),
        "first_p99_us": first_p99,
        "last_p99_us": last_p99,
        "p99_slope": slope,
        "max_p99_us": max_nested(cps, "latency_us", "p99"),
        "last_l0_files": nested(cps[-1], "l0_files") if cps else None,
        "compactions": sum_nested(cps, "metrics_delta", "storage", "compaction_count"),
        "rewrite_bytes": sum_nested(cps, "compaction_rewrite_bytes_delta"),
        "io_write_bytes": sum_nested(cps, "metrics_delta", "io", "write_bytes"),
        "writer_errors": sum_nested(cps, "writer_delta", "errors"),
        "writer_slow_ops": sum_nested(cps, "writer_delta", "slow_ops"),
        "writer_max_op_us": max_nested(cps, "flush_stall_proxy", "writer_max_op_us_observed"),
        "flush_stall_proxy_count": sum_nested(
            cps, "flush_stall_proxy", "io_write_blocking_latency_count_delta"
        )
        + sum_nested(cps, "flush_stall_proxy", "io_sync_blocking_latency_count_delta"),
        "done_levels": done.get("levels_final") if done else None,
    }


def build_report(log_root: Path, arms: list[str], source_label: str | None) -> str:
    all_rows = {arm: load_jsonl(log_root / f"{arm}.jsonl") for arm in arms}
    summaries = {arm: summarize_arm(rows) for arm, rows in all_rows.items()}
    done_marker = (log_root / "DONE").exists()
    failed_marker = (log_root / "FAILED").exists()
    abort_marker = (log_root / "ABORT").exists()
    all_done = all(summaries[a]["done"] for a in arms)
    all_present = all(summaries[a]["line_count"] > 0 for a in arms)
    writer_error_total = sum(float(summaries[a]["writer_errors"]) for a in arms)
    writer_slow_total = sum(float(summaries[a]["writer_slow_ops"]) for a in arms)

    # compaction-fired gate: feedback/full must compact, none must not.
    def fired(arm: str) -> bool:
        return float(summaries[arm]["rewrite_bytes"]) > 0 or float(summaries[arm]["compactions"]) > 0

    compaction_gate_ok = True
    gate_notes: list[str] = []
    for arm in arms:
        if arm in ("feedback", "full"):
            if not fired(arm):
                compaction_gate_ok = False
                gate_notes.append(f"`{arm}` did NOT compact (rewrite=0) — lower thresholds / raise COMPACT cadence")
        if arm == "none" and fired("none"):
            compaction_gate_ok = False
            gate_notes.append("`none` compacted unexpectedly (should be 0)")

    status = "ready-for-paper with caveats"
    if failed_marker or abort_marker or writer_error_total or writer_slow_total or not compaction_gate_ok:
        status = "fallback-required"
    elif not done_marker or not all_done or not all_present:
        status = "partial-running"

    out: list[str] = []
    out.append("# RQ3 feedback-vs-no-feedback tail latency under churn\n")
    out.append("Generated from raw JSONL outputs under:\n")
    out.append(f"`{source_label or str(log_root)}`\n")
    out.append(f"Status: `{status}`\n")
    out.append(
        f"Markers: DONE={done_marker}, FAILED={failed_marker}, ABORT={abort_marker}. "
        "Single variable = L0 compaction strategy; all arms `--variant semantic` on identical "
        "copies of the same base store under the same mixed read/write workload.\n"
    )
    if gate_notes:
        out.append("**Compaction gate problems:** " + "; ".join(gate_notes) + "\n")

    out.append("## Run validation\n")
    out.append("| arm | JSONL lines | checkpoints | final event | done | compactions | rewrite bytes | writer errors | writer slow ops |")
    out.append("|---|---:|---:|---|---:|---:|---:|---:|---:|")
    for arm in arms:
        s = summaries[arm]
        out.append(
            f"| `{arm}` | {int(s['line_count'])} | {int(s['checkpoint_count'])} | "
            f"`{s['last_event']}` | {str(bool(s['done'])).lower()} | "
            f"{fmt_num(s['compactions'])} | {fmt_bytes(s['rewrite_bytes'])} | "
            f"{fmt_num(s['writer_errors'])} | {fmt_num(s['writer_slow_ops'])} |"
        )

    out.append("\n## Aggregate (tail vs maintenance cost)\n")
    out.append(
        "| arm | total queries | mean p50 us | mean p99 us | first p99 us | last p99 us | "
        "p99 slope (last/first) | max p99 us | mean candidate L0 | last L0 files | "
        "rewrite bytes | io write bytes | writer max op us | flush-stall proxy |"
    )
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for arm in arms:
        s = summaries[arm]
        out.append(
            f"| `{arm}` | {fmt_num(s['total_queries'])} | {fmt_num(s['mean_p50_us'])} | "
            f"{fmt_num(s['mean_p99_us'])} | {fmt_num(s['first_p99_us'])} | {fmt_num(s['last_p99_us'])} | "
            f"{fmt_num(s['p99_slope'])} | {fmt_num(s['max_p99_us'])} | {fmt_num(s['mean_candidate_l0'])} | "
            f"{fmt_num(s['last_l0_files'])} | {fmt_bytes(s['rewrite_bytes'])} | "
            f"{fmt_bytes(s['io_write_bytes'])} | {fmt_num(s['writer_max_op_us'])} | "
            f"{fmt_num(s['flush_stall_proxy_count'])} |"
        )

    out.append("\n## Per-checkpoint p99 trajectory\n")
    out.append("| arm | elapsed s | queries | p50 us | p99 us | candidate L0 mean | L0 files | compactions | rewrite bytes | writer max op us |")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for arm in arms:
        for row in checkpoint_rows(all_rows[arm]):
            out.append(
                f"| `{arm}` | {fmt_num(row.get('elapsed_secs'))} | {fmt_num(row.get('queries'))} | "
                f"{fmt_num(nested(row, 'latency_us', 'p50'))} | {fmt_num(nested(row, 'latency_us', 'p99'))} | "
                f"{fmt_num(nested(row, 'candidate_l0_segments', 'mean'))} | {fmt_num(row.get('l0_files'))} | "
                f"{fmt_num(nested(row, 'metrics_delta', 'storage', 'compaction_count'))} | "
                f"{fmt_bytes(row.get('compaction_rewrite_bytes_delta'))} | "
                f"{fmt_num(nested(row, 'flush_stall_proxy', 'writer_max_op_us_observed'))} |"
            )

    if "none" in summaries:
        out.append("\n## Deltas vs none (the feedback question)\n")
        out.append("| arm | mean p99 delta | last p99 delta | p99 slope delta | mean candidate L0 delta | rewrite bytes | io write bytes delta |")
        out.append("|---|---:|---:|---:|---:|---:|---:|")
        base = summaries["none"]
        for arm in arms:
            if arm == "none":
                continue
            s = summaries[arm]
            out.append(
                f"| `{arm}` | {pct_delta(s['mean_p99_us'], base['mean_p99_us'])} | "
                f"{pct_delta(s['last_p99_us'], base['last_p99_us'])} | "
                f"{pct_delta(s['p99_slope'], base['p99_slope'])} | "
                f"{pct_delta(s['mean_candidate_l0'], base['mean_candidate_l0'])} | "
                f"{fmt_bytes(s['rewrite_bytes'])} | "
                f"{pct_delta(s['io_write_bytes'], base['io_write_bytes'])} |"
            )

    out.append("\n## Conclusion (read the p99 ordering, then claim accordingly)\n")
    if status == "partial-running":
        out.append("Run incomplete. Use only as a progress snapshot until DONE + all arms emit `done`.")
    elif status == "fallback-required":
        out.append(
            "Fallback required: a FAILED/ABORT marker, writer error/slow-op, or a compaction-gate "
            "problem was observed. Fix the gate (feedback/full must compact, none must not) before "
            "making any RQ3 claim."
        )
    else:
        out.append(
            "Map the p99 ordering to a claim:\n"
            "- `feedback < full < none` -> RQ3 strong: feedback compaction lowers tail AND the gain is "
            "from the targeting signal (beats blind compaction at equal cadence).\n"
            "- `feedback ~= full < none` -> drop the strong claim; report efficiency: same tail as full "
            "at lower rewrite bytes / writer-max-op.\n"
            "- `none ~= feedback` -> at this churn rate/horizon semantic pruning alone holds the tail; "
            "feedback value moves to bounding L0 growth over longer horizons (extend duration).\n"
            "- `feedback > none` -> compaction's write-blocking cost dominates here; report as a when-not-to-use bound.\n\n"
            "Boundaries (state in paper): semantic-layout-only (no general feedback claim); single churn "
            "rate + single workload shape; `full` is a strong/coarse non-feedback baseline (round-robin would "
            "be fairer); n=1, SF30, no confidence intervals; the window may be too short to surface the "
            "none-vs-feedback long-horizon divergence."
        )

    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-root", required=True, type=Path)
    parser.add_argument("--arms", default="feedback full none")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-label", default=None)
    args = parser.parse_args()
    arms = args.arms.split()
    report = build_report(args.log_root, arms, args.source_label)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
