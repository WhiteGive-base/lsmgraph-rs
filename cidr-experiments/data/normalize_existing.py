#!/usr/bin/env python3
"""Normalize reusable and provisional legacy evidence without hand-copied values."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Iterable


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean_cell(value: str) -> str:
    return value.strip().strip("`").strip()


def markdown_table(path: Path, heading_contains: str) -> list[dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(
        (index for index, line in enumerate(lines) if line.startswith("##") and heading_contains.lower() in line.lower()),
        None,
    )
    if start is None:
        raise ValueError(f"heading {heading_contains!r} not found in {path}")
    header_index = next((index for index in range(start + 1, len(lines)) if lines[index].lstrip().startswith("|")), None)
    if header_index is None or header_index + 1 >= len(lines):
        raise ValueError(f"table after {heading_contains!r} not found in {path}")

    def cells(line: str) -> list[str]:
        return [clean_cell(value) for value in line.strip().strip("|").split("|")]

    header = cells(lines[header_index])
    rows: list[dict[str, str]] = []
    for line in lines[header_index + 2 :]:
        if not line.lstrip().startswith("|"):
            break
        values = cells(line)
        if len(values) != len(header):
            raise ValueError(f"bad table row in {path}: {line}")
        rows.append(dict(zip(header, values)))
    return rows


def number(value: str) -> float:
    return float(value.replace(",", "").strip())


def integer(value: str) -> int:
    return int(number(value))


def mean_std(value: str) -> tuple[float, float]:
    match = re.fullmatch(r"\s*([0-9,.]+)\s*\+/-\s*([0-9,.]+)\s*", value)
    if not match:
        raise ValueError(f"expected mean +/- std, got {value!r}")
    return number(match.group(1)), number(match.group(2))


def quantity(value: str, target: str) -> float:
    match = re.fullmatch(r"\s*([0-9,.]+)\s*(B|KiB|MiB|GiB|ms|s|min)\s*", value)
    if not match:
        raise ValueError(f"bad quantity {value!r}")
    raw = number(match.group(1))
    unit = match.group(2)
    if target == "bytes":
        factors = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}
    elif target == "seconds":
        factors = {"ms": 1e-3, "s": 1, "min": 60}
    else:
        raise ValueError(target)
    if unit not in factors:
        raise ValueError(f"cannot convert {unit} to {target}")
    return raw * factors[unit]


def elapsed_seconds(value: str) -> float:
    parts = value.strip().split(":")
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return float(hours) * 3600 + float(minutes) * 60 + float(seconds)
    if len(parts) == 2:
        minutes, seconds = parts
        return float(minutes) * 60 + float(seconds)
    return float(value)


def write_tsv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def normalize_w6(source: Path, out: Path) -> tuple[list[Path], list[Path]]:
    inputs = [source / "w6-sf10-priority-20260709-summary.md", source / "sf100-matrix-20260613-cn.md"]
    rows: list[dict[str, object]] = []
    store_map: dict[str, tuple[float, int]] = {}
    for scale, path in (("SF10", inputs[0]), ("SF100", inputs[1])):
        for item in markdown_table(path, "Aggregate Matrix"):
            candidate_mean, candidate_std = mean_std(item["candidate L0 mean+/-std"])
            read_mean, read_std = mean_std(item["read bytes mean+/-std"])
            avg_mean, avg_std = mean_std(item["avg us mean+/-std"])
            percentile_values = re.findall(
                r"[0-9,.]+\s*\+/-\s*[0-9,.]+",
                item["p50/p90/p99 edge-mean us"],
            )
            if len(percentile_values) != 3:
                raise ValueError(f"expected three percentile pairs in {item['p50/p90/p99 edge-mean us']!r}")
            percentiles = [mean_std(value) for value in percentile_values]
            compare = item["compare vs naive"]
            mismatch_count = 0 if compare == "anchor" or compare.startswith("PASS") else ""
            row = {
                "source_id": f"W6-{scale}",
                "scale": scale,
                "variant": item["variant"],
                "layout": item["layout"],
                "store_gib": number(item["store GiB"]),
                "l0_files": integer(item["L0 files"]),
                "ops_per_repeat": integer(item["ops/repeat"]),
                "candidate_l0_mean": candidate_mean,
                "candidate_l0_std": candidate_std,
                "candidate_per_op": candidate_mean / integer(item["ops/repeat"]),
                "read_bytes_mean": read_mean,
                "read_bytes_std": read_std,
                "read_bytes_per_op": read_mean / integer(item["ops/repeat"]),
                "avg_us_mean": avg_mean,
                "avg_us_std": avg_std,
                "p50_us_mean": percentiles[0][0],
                "p50_us_std": percentiles[0][1],
                "p90_us_mean": percentiles[1][0],
                "p90_us_std": percentiles[1][1],
                "p99_us_mean": percentiles[2][0],
                "p99_us_std": percentiles[2][1],
                "mismatch_count": mismatch_count,
                "repeat_semantics": "3 repeats in one process/store",
                "verdict": "REUSE" if scale == "SF100" else "FIX",
                "claim_scope": "layout/typed-neighbor diagnostic; not component staircase or matched external",
                "source_path": path.as_posix(),
                "source_sha256": sha256(path),
            }
            rows.append(row)
            if scale == "SF10":
                store_map[item["variant"]] = (row["store_gib"], row["l0_files"])

    fields = list(rows[0])
    w6_out = out / "w6-layout-typed-neighbor.tsv"
    write_tsv(w6_out, rows, fields)

    time_dir = source / "w6-sf10-priority-20260709"
    resource_rows: list[dict[str, object]] = []
    label_to_variant = {
        "schema": "schema",
        "naive": "naive",
        "kv-lsm": "kv-lsm",
        "edge-type-only": "edge-type-only",
        "semantic": "semantic",
        "budg-b64": "budg-b64",
        "budg-b256": "budg-b256",
        "budg-b1024": "budg-b1024",
        "oracle": "oracle",
    }
    patterns = {
        "user_cpu_s": r"User time \(seconds\):\s*([0-9.]+)",
        "sys_cpu_s": r"System time \(seconds\):\s*([0-9.]+)",
        "cpu_pct": r"Percent of CPU this job got:\s*([0-9]+)%",
        "elapsed_raw": r"Elapsed \(wall clock\) time \(h:mm:ss or m:ss\):\s*([^\s]+)",
        "max_rss_kb": r"Maximum resident set size \(kbytes\):\s*([0-9]+)",
        "fs_inputs": r"File system inputs:\s*([0-9]+)",
        "fs_outputs": r"File system outputs:\s*([0-9]+)",
        "exit_status": r"Exit status:\s*([0-9]+)",
    }
    for label, variant in label_to_variant.items():
        path = time_dir / f"{label}-import.stderr"
        text = path.read_text(encoding="utf-8", errors="replace")
        parsed: dict[str, str] = {}
        for field, pattern in patterns.items():
            match = re.search(pattern, text)
            if not match:
                raise ValueError(f"{field} not found in {path}")
            parsed[field] = match.group(1)
        store_gib, l0_files = store_map[variant]
        resource_rows.append(
            {
                "source_id": "W6-SF10-time-v",
                "variant": variant,
                "phase": "import",
                "store_gib": store_gib,
                "l0_files": l0_files,
                "elapsed_raw": parsed["elapsed_raw"],
                "elapsed_s": elapsed_seconds(parsed["elapsed_raw"]),
                "user_cpu_s": float(parsed["user_cpu_s"]),
                "sys_cpu_s": float(parsed["sys_cpu_s"]),
                "cpu_pct": int(parsed["cpu_pct"]),
                "max_rss_kb": int(parsed["max_rss_kb"]),
                "max_rss_gib": int(parsed["max_rss_kb"]) / 1024**2,
                "fs_inputs": int(parsed["fs_inputs"]),
                "fs_outputs": int(parsed["fs_outputs"]),
                "exit_status": int(parsed["exit_status"]),
                "verdict": "FIXED_PARSER_REUSE",
                "claim_scope": "import-only resource cost; not steady-state RSS or CPU/op",
                "source_path": path.as_posix(),
                "source_sha256": sha256(path),
            }
        )
        inputs.append(path)
    resource_out = out / "w6-sf10-import-resource.tsv"
    write_tsv(resource_out, resource_rows, list(resource_rows[0]))
    return inputs, [w6_out, resource_out]


def normalize_w8(source: Path, out: Path) -> tuple[list[Path], list[Path]]:
    path = source / "w8-property-2hop-summary-20260615-cn.md"
    rows: list[dict[str, object]] = []
    for item in markdown_table(path, "Property predicate results"):
        rows.append(
            {
                "source_id": "W8-SF30",
                "category": "property",
                "variant": item["variant"],
                "predicate": item["predicate"],
                "candidate_l0_mean": integer(item["candidate L0 mean"]),
                "body_reads_mean": integer(item["body reads mean"]),
                "read_bytes_mean": int(quantity(item["read bytes mean"], "bytes")),
                "elapsed_s_mean": quantity(item["elapsed mean"], "seconds"),
                "p50_us": integer(item["p50 us"]),
                "p90_us": integer(item["p90 us"]),
                "p99_us": integer(item["p99 us"]),
                "output_edges_mean": integer(item["one-hop edges mean"]),
                "repeat_semantics": "3 repeats in one process/store",
                "verdict": "FIX",
                "claim_scope": "property coverage; equality is prototype; no query digest",
                "source_path": path.as_posix(),
                "source_sha256": sha256(path),
            }
        )
    for item in markdown_table(path, "2-hop results"):
        rows.append(
            {
                "source_id": "W8-SF30",
                "category": "two-hop",
                "variant": item["variant"],
                "predicate": "two-hop",
                "candidate_l0_mean": integer(item["candidate L0 mean"]),
                "body_reads_mean": integer(item["body reads mean"]),
                "read_bytes_mean": int(quantity(item["read bytes mean"], "bytes")),
                "elapsed_s_mean": quantity(item["elapsed mean"], "seconds"),
                "p50_us": integer(item["p50 us"]),
                "p90_us": integer(item["p90 us"]),
                "p99_us": integer(item["p99 us"]),
                "output_edges_mean": integer(item["two-hop edges mean"]),
                "repeat_semantics": "3 repeats in one process/store",
                "verdict": "FIX",
                "claim_scope": "two-hop coverage; candidate count regresses; no query digest",
                "source_path": path.as_posix(),
                "source_sha256": sha256(path),
            }
        )
    output = out / "w8-property-twohop.tsv"
    write_tsv(output, rows, list(rows[0]))
    return [path], [output]


def normalize_w9(source: Path, out: Path) -> tuple[list[Path], list[Path]]:
    path = source / "w9-steady-state-summary-20260615-cn.md"
    rows: list[dict[str, object]] = []
    for item in markdown_table(path, "Per-checkpoint trace"):
        rows.append(
            {
                "source_id": "W9-SF30",
                "variant": item["variant"],
                "elapsed_s": number(item["elapsed s"]),
                "queries": integer(item["queries"]),
                "query_rate_s": number(item["query rate/s"]),
                "p50_us": number(item["p50 us"]),
                "p99_us": number(item["p99 us"]),
                "candidate_l0_mean": number(item["candidate L0 mean"]),
                "l0_files": integer(item["L0 files"]),
                "rewrite_bytes": int(quantity(item["rewrite bytes"], "bytes")),
                "io_write_bytes": int(quantity(item["io write bytes"], "bytes")),
                "writer_errors": integer(item["writer errors"]),
                "writer_slow_ops": integer(item["writer slow ops"]),
                "compaction_enabled": False,
                "verdict": "DIAGNOSTIC_ONLY",
                "claim_scope": "append-only/no-compaction accumulation; one run per variant",
                "source_path": path.as_posix(),
                "source_sha256": sha256(path),
            }
        )
    output = out / "w9-no-compaction-timeline.tsv"
    write_tsv(output, rows, list(rows[0]))
    return [path], [output]


def normalize_rq3(source: Path, out: Path) -> tuple[list[Path], list[Path]]:
    root = source / "rq3-formal-1800-20260623-174053"
    rows: list[dict[str, object]] = []
    inputs: list[Path] = []
    for arm in ("feedback", "full", "none"):
        path = root / f"{arm}.jsonl"
        inputs.append(path)
        cumulative_rewrite = 0
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                event = json.loads(line)
                if event.get("event") != "checkpoint":
                    continue
                metrics = event.get("metrics_delta", {})
                storage = metrics.get("storage", {})
                writer = event.get("writer_delta", {})
                rewrite_delta = int(event.get("compaction_rewrite_bytes_delta", 0))
                cumulative_rewrite += rewrite_delta
                rows.append(
                    {
                        "source_id": "RQ3-SF30",
                        "arm": arm,
                        "elapsed_s": event["elapsed_secs"],
                        "queries": event["queries"],
                        "query_rate_s": event["query_rate_per_sec"],
                        "p50_us": event["latency_us"]["p50"],
                        "p99_us": event["latency_us"]["p99"],
                        "candidate_l0_mean": event["candidate_l0_segments"]["mean"],
                        "l0_files": event["l0_files"],
                        "compactions_delta": storage.get("compaction_count", writer.get("compactions", 0)),
                        "compaction_output_bytes_delta": storage.get("compaction_output_bytes", 0),
                        "compaction_rewrite_bytes_delta": rewrite_delta,
                        "compaction_rewrite_bytes_cumulative": cumulative_rewrite,
                        "writer_max_op_us": writer.get("op_latency_max_us_observed", 0),
                        "writer_errors": writer.get("errors", 0),
                        "repeat_index": 1,
                        "verdict": "PROVISIONAL",
                        "claim_scope": "one run/arm; wall-clock RNG means query sequences differ; no digest/CPU/RSS",
                        "source_path": path.as_posix(),
                        "source_sha256": sha256(path),
                    }
                )
    output = out / "rq3-compaction-provisional.tsv"
    write_tsv(output, rows, list(rows[0]))
    return inputs, [output]


def normalize_c2(source: Path, out: Path) -> tuple[list[Path], list[Path]]:
    path = source / "stage7-sf30-readamp-proxy-summary-20260622-cn.md"
    rows: list[dict[str, object]] = []
    for item in markdown_table(path, "核心结果"):
        rows.append(
            {
                "source_id": "C2-SF30-proxy",
                "policy": item["policy"],
                "retention": number(item["retention"]),
                "write_amp": number(item["write_amp"]),
                "output_segments": integer(item["output segs"]),
                "query_partitions": integer(item["query partitions"]),
                "avg_candidate_segments_per_query": number(item["avg candidate segs/query"]),
                "candidate_bytes_total": int(number(item["candidate bytes total"].split()[0]) * 10**9),
                "exact_bytes_before": int(number(item["exact bytes before"].split()[0]) * 10**9),
                "weighted_read_amp_proxy": number(item["weighted read-amp proxy"].rstrip("x")),
                "verdict": "REUSE_NARROW",
                "claim_scope": "real-SF30 metadata replay; not full body read or end-to-end latency",
                "source_path": path.as_posix(),
                "source_sha256": sha256(path),
            }
        )
    output = out / "c2-sf30-metadata-proxy.tsv"
    write_tsv(output, rows, list(rows[0]))
    return [path], [output]


def normalize_external(source: Path, out: Path) -> tuple[list[Path], list[Path]]:
    path = source / "metrics.tsv"
    with path.open(encoding="utf-8", newline="") as handle:
        raw = list(csv.DictReader(handle, delimiter="\t"))
    rows: list[dict[str, object]] = []
    for item in raw:
        if item["scale"] != "SF10" or item["system"] == "LSMGraph-style":
            continue
        row: dict[str, object] = {key: value for key, value in item.items()}
        row.update(
            {
                "verdict": "CONTEXT_ONLY",
                "claim_scope": "historical old-hardware scope-limited baseline; not matched to SemL0 workload",
                "source_path": path.as_posix(),
                "source_sha256": sha256(path),
            }
        )
        rows.append(row)
    output = out / "external-sf10-context.tsv"
    write_tsv(output, rows, list(rows[0]))
    return [path], [output]


def normalize_w13(source: Path, out: Path) -> tuple[list[Path], list[Path]]:
    path = source / "w13-schema-evolution-20260615-143649" / "summary.md"
    lines = path.read_text(encoding="utf-8").splitlines()
    run_id = next(line.split("=", 1)[1] for line in lines if line.startswith("run_id="))
    header = lines.index("test\tstatus\tlog_dir")
    rows: list[dict[str, object]] = []
    for line in lines[header + 1 :]:
        if not line.strip():
            break
        test, status, log_dir = line.split("\t")
        rows.append(
            {
                "source_id": "W13",
                "run_id": run_id,
                "test": test,
                "status": status,
                "log_dir": log_dir,
                "verdict": "REUSE_NARROW",
                "claim_scope": "bounded named test; not randomized migration/reclamation stress",
                "source_path": path.as_posix(),
                "source_sha256": sha256(path),
            }
        )
    output = out / "w13-bounded-correctness.tsv"
    write_tsv(output, rows, list(rows[0]))
    return [path], [output]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, default=Path(__file__).resolve().parent / "source-cache")
    parser.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parent / "normalized" / "provisional")
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    inputs: list[Path] = []
    outputs: list[Path] = []
    for normalizer in (normalize_w6, normalize_w8, normalize_w9, normalize_rq3, normalize_c2, normalize_external, normalize_w13):
        used, made = normalizer(args.source_dir, args.out_dir)
        inputs.extend(used)
        outputs.extend(made)

    manifest = {
        "status": "PROVISIONAL_ONLY",
        "generator": str(Path(__file__).resolve()),
        "generator_sha256": sha256(Path(__file__).resolve()),
        "inputs": [{"path": str(path.resolve()), "sha256": sha256(path)} for path in sorted(set(inputs))],
        "outputs": [{"path": str(path.resolve()), "sha256": sha256(path)} for path in sorted(outputs)],
        "warning": "These files preserve legacy evidence and caveats; they do not satisfy the frozen final-figure run contract.",
    }
    manifest_path = args.out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(outputs)} TSV files and {manifest_path}")


if __name__ == "__main__":
    main()
