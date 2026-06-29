#!/usr/bin/env python3
"""Regenerate the W9 steady-state summary from raw JSONL outputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
from typing import Any


VARIANTS = ("schema", "budg-b64", "semantic")


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


def summarize_variant(rows: list[dict[str, Any]]) -> dict[str, Any]:
    checkpoints = checkpoint_rows(rows)
    done = done_row(rows)
    return {
        "line_count": len(rows),
        "checkpoint_count": len(checkpoints),
        "done": done is not None,
        "last_event": rows[-1].get("event") if rows else "missing",
        "total_queries": sum_nested(checkpoints, "queries"),
        "mean_query_rate": mean_nested(checkpoints, "query_rate_per_sec"),
        "mean_candidate_l0": mean_nested(checkpoints, "candidate_l0_segments", "mean"),
        "last_candidate_l0": nested(checkpoints[-1], "candidate_l0_segments", "mean") if checkpoints else None,
        "mean_p50_us": mean_nested(checkpoints, "latency_us", "p50"),
        "mean_p99_us": mean_nested(checkpoints, "latency_us", "p99"),
        "max_p99_us": max_nested(checkpoints, "latency_us", "p99"),
        "last_l0_files": nested(checkpoints[-1], "l0_files") if checkpoints else None,
        "max_l0_files": max_nested(checkpoints, "l0_files"),
        "rewrite_bytes": sum_nested(checkpoints, "compaction_rewrite_bytes_delta"),
        "io_write_bytes": sum_nested(checkpoints, "metrics_delta", "io", "write_bytes"),
        "flushes": sum_nested(checkpoints, "writer_delta", "flushes"),
        "writer_errors": sum_nested(checkpoints, "writer_delta", "errors"),
        "writer_slow_ops": sum_nested(checkpoints, "writer_delta", "slow_ops"),
        "writer_max_op_us": max_nested(checkpoints, "writer_delta", "op_latency_max_us_observed"),
        "flush_stall_proxy_count": sum_nested(
            checkpoints, "flush_stall_proxy", "io_write_blocking_latency_count_delta"
        )
        + sum_nested(checkpoints, "flush_stall_proxy", "io_sync_blocking_latency_count_delta"),
        "done_levels": done.get("levels_final") if done else None,
    }


def build_report(log_root: Path, source_label: str | None = None) -> str:
    all_rows = {variant: load_jsonl(log_root / f"{variant}.jsonl") for variant in VARIANTS}
    summaries = {variant: summarize_variant(rows) for variant, rows in all_rows.items()}
    done_marker = (log_root / "DONE").exists()
    failed_marker = (log_root / "FAILED").exists()
    abort_marker = (log_root / "ABORT").exists()
    all_variant_done = all(summaries[v]["done"] for v in VARIANTS)
    all_present = all(summaries[v]["line_count"] > 0 for v in VARIANTS)
    writer_error_total = sum(float(summaries[v]["writer_errors"]) for v in VARIANTS)
    writer_slow_total = sum(float(summaries[v]["writer_slow_ops"]) for v in VARIANTS)

    status = "ready-for-paper with caveats"
    if failed_marker or abort_marker or writer_error_total or writer_slow_total:
        status = "fallback-required"
    elif not done_marker or not all_variant_done or not all_present:
        status = "partial-running"

    out: list[str] = []
    out.append("# W9 mixed read/write steady-state summary\n")
    out.append("Generated from raw JSONL outputs under:\n")
    out.append(f"`{source_label or str(log_root)}`\n")
    out.append(f"Status: `{status}`\n")
    out.append(
        f"Markers: DONE={done_marker}, FAILED={failed_marker}, ABORT={abort_marker}. "
        "The runner reuses W8 frozen SF30 stores as mutable steady-state starting stores.\n"
    )

    out.append("## Run validation\n")
    out.append("| variant | JSONL lines | checkpoints | final event | done event | writer errors | writer slow ops |")
    out.append("|---|---:|---:|---|---:|---:|---:|")
    for variant in VARIANTS:
        s = summaries[variant]
        out.append(
            f"| `{variant}` | {int(s['line_count'])} | {int(s['checkpoint_count'])} | "
            f"`{s['last_event']}` | {str(bool(s['done'])).lower()} | "
            f"{fmt_num(s['writer_errors'])} | {fmt_num(s['writer_slow_ops'])} |"
        )

    out.append("\n## Aggregate checkpoint summary\n")
    out.append(
        "| variant | total queries | mean query rate/s | mean candidate L0 | last candidate L0 | "
        "mean p50 us | mean p99 us | max p99 us | last L0 files | rewrite bytes | "
        "io write bytes | flushes | writer max op us | flush-stall proxy count |"
    )
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for variant in VARIANTS:
        s = summaries[variant]
        out.append(
            f"| `{variant}` | {fmt_num(s['total_queries'])} | {fmt_num(s['mean_query_rate'])} | "
            f"{fmt_num(s['mean_candidate_l0'])} | {fmt_num(s['last_candidate_l0'])} | "
            f"{fmt_num(s['mean_p50_us'])} | {fmt_num(s['mean_p99_us'])} | "
            f"{fmt_num(s['max_p99_us'])} | {fmt_num(s['last_l0_files'])} | "
            f"{fmt_bytes(s['rewrite_bytes'])} | {fmt_bytes(s['io_write_bytes'])} | "
            f"{fmt_num(s['flushes'])} | {fmt_num(s['writer_max_op_us'])} | "
            f"{fmt_num(s['flush_stall_proxy_count'])} |"
        )

    out.append("\n## Per-checkpoint trace\n")
    out.append(
        "| variant | elapsed s | queries | query rate/s | p50 us | p99 us | candidate L0 mean | "
        "L0 files | rewrite bytes | io write bytes | writer errors | writer slow ops |"
    )
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for variant in VARIANTS:
        for row in checkpoint_rows(all_rows[variant]):
            out.append(
                f"| `{variant}` | {fmt_num(row.get('elapsed_secs'))} | {fmt_num(row.get('queries'))} | "
                f"{fmt_num(row.get('query_rate_per_sec'))} | "
                f"{fmt_num(nested(row, 'latency_us', 'p50'))} | "
                f"{fmt_num(nested(row, 'latency_us', 'p99'))} | "
                f"{fmt_num(nested(row, 'candidate_l0_segments', 'mean'))} | "
                f"{fmt_num(row.get('l0_files'))} | {fmt_bytes(row.get('compaction_rewrite_bytes_delta'))} | "
                f"{fmt_bytes(nested(row, 'metrics_delta', 'io', 'write_bytes'))} | "
                f"{fmt_num(nested(row, 'writer_delta', 'errors'))} | "
                f"{fmt_num(nested(row, 'writer_delta', 'slow_ops'))} |"
            )

    out.append("\n## Deltas vs schema\n")
    out.append(
        "| variant | mean candidate L0 delta | mean p50 delta | mean p99 delta | "
        "last L0 files delta | io write bytes delta |"
    )
    out.append("|---|---:|---:|---:|---:|---:|")
    base = summaries["schema"]
    for variant in ("budg-b64", "semantic"):
        s = summaries[variant]
        if not s["checkpoint_count"]:
            out.append(f"| `{variant}` | n/a | n/a | n/a | n/a | n/a |")
        else:
            out.append(
                f"| `{variant}` | {pct_delta(s['mean_candidate_l0'], base['mean_candidate_l0'])} | "
                f"{pct_delta(s['mean_p50_us'], base['mean_p50_us'])} | "
                f"{pct_delta(s['mean_p99_us'], base['mean_p99_us'])} | "
                f"{pct_delta(s['last_l0_files'], base['last_l0_files'])} | "
                f"{pct_delta(s['io_write_bytes'], base['io_write_bytes'])} |"
            )

    out.append("\n## Conclusion\n")
    if status == "partial-running":
        out.append(
            "W9 is still running or incomplete. Do not use W9 as a paper claim yet; use this file "
            "only as a progress snapshot until DONE exists and all three variants have final `done` events."
        )
    elif status == "fallback-required":
        out.append(
            "W9 needs fallback handling because a failure/abort marker or writer error/slow-op signal was observed. "
            "Use the trace table for debugging and write mixed read/write as limitation unless rerun evidence clears it."
        )
    else:
        out.append(
            "W9 is usable as steady-state evidence with cautious wording. The safe claim is that the mixed "
            "read/write workload completed for schema, budg-b64, and semantic on SF30 stores with explicit "
            "checkpoint traces for candidate L0, latency, L0 files, rewrite bytes, flush-stall proxy, and IO write bytes. "
            "Only claim budg-b64/semantic advantages where the delta table shows them; otherwise phrase as stability "
            "and workload-coverage evidence."
        )

    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--log-root",
        default="remote-logs/w9-steady-state-formal-20260615-1200",
        type=Path,
    )
    parser.add_argument(
        "--output",
        default="baseline/w9-steady-state-summary-20260615-cn.md",
        type=Path,
    )
    parser.add_argument("--source-label", default=None)
    args = parser.parse_args()
    report = build_report(args.log_root, args.source_label)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")


if __name__ == "__main__":
    main()
