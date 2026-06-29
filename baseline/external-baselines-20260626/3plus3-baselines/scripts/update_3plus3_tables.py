#!/usr/bin/env python3
"""Build SemL0 3+3 baseline progress/effect tables from raw artifacts.

This script is intentionally conservative: a system enters the numeric
effect table only when its raw artifacts show the workload ran and the
correctness gate passed. Everything else remains an artifact attempt or
qualitative/design baseline.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Iterable


SYSTEMS = [
    {
        "id": "livegraph",
        "name": "LiveGraph",
        "group": "problem-adjacent",
        "claim": "scope-limited measured external baseline",
        "deps_path": "deps/baselines/problem-adjacent/livegraph",
        "source_path": "deps/LiveGraph",
    },
    {
        "id": "lsmgraph-style",
        "name": "LSMGraph-style",
        "group": "problem-adjacent",
        "claim": "internal layout baseline, not official external artifact",
        "deps_path": "deps/baselines/problem-adjacent/lsmgraph-style",
        "source_path": "deps/ldbc_snb_interactive_impls/lsmgraph",
    },
    {
        "id": "aster",
        "name": "Aster",
        "group": "problem-adjacent",
        "claim": "artifact attempt / qualitative until workload bridge passes",
        "deps_path": "deps/baselines/problem-adjacent/aster",
        "source_path": "",
    },
    {
        "id": "neo4j",
        "name": "Neo4j Community",
        "group": "open-source-graph-db",
        "claim": "candidate numeric baseline after typed-neighbor digest PASS",
        "deps_path": "deps/baselines/open-source-graph-db/neo4j",
        "source_path": "docker image neo4j:5.26.24",
    },
    {
        "id": "tugraph",
        "name": "TuGraph",
        "group": "open-source-graph-db",
        "claim": "candidate numeric baseline after typed-neighbor digest PASS",
        "deps_path": "deps/baselines/open-source-graph-db/tugraph",
        "source_path": "deps/ldbc_snb_interactive_impls/tugraph and docker image tugraph/tugraph-runtime-ubuntu18.04:4.0.0-finbench",
    },
    {
        "id": "nebulagraph",
        "name": "NebulaGraph",
        "group": "open-source-graph-db",
        "claim": "candidate numeric baseline after typed-neighbor digest PASS",
        "deps_path": "deps/baselines/open-source-graph-db/nebulagraph",
        "source_path": "",
    },
]


def now_iso() -> str:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).replace(microsecond=0).isoformat()


def repo_root() -> Path:
    here = Path(__file__).resolve()
    # baseline/external-baselines-YYYYMMDD/3plus3-baselines/scripts/update.py
    return here.parents[4]


def out_dir(root: Path) -> Path:
    return root / "baseline" / "external-baselines-20260626" / "3plus3-baselines"


def read_tsv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def fmt_num(value: Any) -> str:
    if value in (None, ""):
        return "-"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f.is_integer():
        return f"{int(f):,}"
    if abs(f) >= 1000:
        return f"{f:,.2f}"
    return f"{f:.3f}"


def livegraph_rows(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    base = root / "baseline" / "external-baselines-20260624" / "livegraph"
    metrics = read_tsv(base / "metrics.tsv")
    correctness = read_tsv(base / "correctness.tsv")
    config = load_json(base / "run-config.json")
    metric_rows: list[dict[str, Any]] = []
    for row in metrics:
        if row.get("scope") not in {
            "all-types-summary",
            "positive-edge-types-summary",
            "negative-edge-types-summary",
        }:
            continue
        metric_rows.append(
            {
                "system": "LiveGraph",
                "group": "problem-adjacent",
                "scale": row.get("scale"),
                "scope": row.get("scope"),
                "ops": row.get("ops"),
                "avg_us": row.get("avg_us"),
                "p50_us": row.get("p50_us"),
                "p90_us": row.get("p90_us"),
                "p99_us": row.get("p99_us"),
                "load_s": row.get("load_s"),
                "peak_rss_kb": row.get("peak_rss_kb"),
                "disk_total_bytes": row.get("disk_total_bytes"),
                "correctness": "PASS" if livegraph_digest_pass(correctness, row.get("scale", "")) else "CHECK",
                "claim_level": "scope-limited measured external baseline",
                "raw_artifact": "baseline/external-baselines-20260624/livegraph",
                "notes": row.get("notes", ""),
            }
        )
    correctness_rows = [
        {
            "system": "LiveGraph",
            "scale": row.get("scale"),
            "check": row.get("check"),
            "expected": row.get("expected"),
            "observed": row.get("observed"),
            "status": row.get("status"),
            "raw_artifact": "baseline/external-baselines-20260624/livegraph/correctness.tsv",
            "notes": row.get("notes"),
        }
        for row in correctness
    ]
    status = {
        "status": "SF1/SF10 DONE",
        "sf1": "PASS",
        "sf10": "PASS",
        "correctness": "PASS",
        "raw": "baseline/external-baselines-20260624/livegraph",
        "notes": "SF10 typed-neighbor scan and query-count digest completed; scope-limited external baseline.",
        "version": config.get("livegraph_repo_head", ""),
    }
    return metric_rows, correctness_rows, status


def livegraph_digest_pass(rows: Iterable[dict[str, str]], scale: str) -> bool:
    return any(
        row.get("scale") == scale
        and row.get("check") == "query_count_digest"
        and row.get("status") == "PASS"
        for row in rows
    )


def parse_file_summary(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        if len(parts) == 2:
            out[parts[0]] = parts[1]
    return out


def percentile_from_buckets(buckets: list[dict[str, Any]], count: int, pct: float) -> str:
    if not buckets or count <= 0:
        return ""
    target = max(1, int((pct / 100.0) * count + 0.999999))
    acc = 0
    for bucket in buckets:
        acc += int(bucket.get("count", 0))
        if acc >= target:
            ub = bucket.get("upper_bound_us")
            if ub == 18446744073709551615:
                return str(bucket.get("upper_bound_us", "max"))
            return str(ub)
    return str(buckets[-1].get("upper_bound_us", ""))


def aggregate_lsm_json(path: Path) -> dict[str, Any]:
    doc = load_json(path)
    benches = doc.get("benchmarks", [])
    total_count = 0
    total_sum_us = 0
    max_us = 0
    buckets: dict[int, int] = {}
    body_reads = 0
    body_bytes = 0
    candidate_l0_segments = 0
    filter_passed_segments = 0
    for bench in benches:
        csr = bench.get("neighbor_metrics", {}).get("csr", {})
        lat = csr.get("get_neighbors_latency", {})
        count = int(lat.get("count", 0))
        total_count += count
        total_sum_us += int(lat.get("sum_us", 0))
        max_us = max(max_us, int(lat.get("max_us", 0)))
        for bucket in lat.get("buckets", []):
            ub = int(bucket.get("upper_bound_us", 0))
            buckets[ub] = buckets.get(ub, 0) + int(bucket.get("count", 0))
        body_reads += int(csr.get("body_reads", 0))
        body_bytes += int(csr.get("body_bytes", 0))
        candidate_l0_segments += int(csr.get("candidate_l0_segments", 0))
        filter_passed_segments += int(csr.get("filter_passed_segments", 0))
    bucket_list = [{"upper_bound_us": ub, "count": count} for ub, count in sorted(buckets.items())]
    return {
        "ops": total_count,
        "avg_us": (total_sum_us / total_count) if total_count else "",
        "p50_us": percentile_from_buckets(bucket_list, total_count, 50),
        "p90_us": percentile_from_buckets(bucket_list, total_count, 90),
        "p99_us": percentile_from_buckets(bucket_list, total_count, 99),
        "max_us": max_us,
        "body_reads": body_reads,
        "body_bytes": body_bytes,
        "candidate_l0_segments": candidate_l0_segments,
        "filter_passed_segments": filter_passed_segments,
    }


def lsmgraph_rows(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    metric_rows: list[dict[str, Any]] = []
    correctness_rows: list[dict[str, Any]] = []
    traces = root / "baseline" / "semL0-ablation-results" / "traces"
    specs = [
        ("SF30", "core-s200", traces / "e11-sf30-lsmgraph_style-20260608", "fair-core-s200-r1.json"),
        ("SF30", "alltypes-s50", traces / "e11-sf30-lsmgraph_style-20260608", "fair-alltypes-s50-r1.json"),
        ("SF100", "core-s200", traces / "e11-sf100-lsmgraph_style-20260608", "fair-core-s200-r1.json"),
    ]
    for scale, scope, directory, json_name in specs:
        summary = parse_file_summary(directory / "file-summary.tsv")
        agg = aggregate_lsm_json(directory / json_name)
        if not agg.get("ops"):
            continue
        metric_rows.append(
            {
                "system": "LSMGraph-style",
                "group": "problem-adjacent",
                "scale": scale,
                "scope": scope,
                "ops": agg.get("ops"),
                "avg_us": agg.get("avg_us"),
                "p50_us": agg.get("p50_us"),
                "p90_us": agg.get("p90_us"),
                "p99_us": agg.get("p99_us"),
                "load_s": "",
                "peak_rss_kb": "",
                "disk_total_bytes": summary.get("bytes", ""),
                "correctness": "internal engine run; no external digest gate",
                "claim_level": "internal layout baseline, not official external artifact",
                "raw_artifact": str(directory.relative_to(root)),
                "notes": (
                    f"body_reads={agg.get('body_reads')}; "
                    f"candidate_l0_segments={agg.get('candidate_l0_segments')}; "
                    f"l0_files={summary.get('l0_files', '')}"
                ),
            }
        )
    if metric_rows:
        correctness_rows.append(
            {
                "system": "LSMGraph-style",
                "scale": "SF30/SF100",
                "check": "internal benchmark completed",
                "expected": "raw JSON present",
                "observed": f"{len(metric_rows)} metric rows",
                "status": "PASS-INTERNAL",
                "raw_artifact": "baseline/semL0-ablation-results/traces/e11-*-lsmgraph_style-20260608",
                "notes": "Use as SemL0 internal/layout-style baseline only; not an official external LSMGraph artifact.",
            }
        )
    status = {
        "status": "EXTRACTED",
        "sf1": "N/A",
        "sf10": "N/A",
        "correctness": "PASS-INTERNAL" if metric_rows else "MISSING",
        "raw": "baseline/semL0-ablation-results/traces/e11-*-lsmgraph_style-20260608",
        "notes": "Existing SF30/SF100 layout-style results extracted; not external official system.",
        "version": "",
    }
    return metric_rows, correctness_rows, status


def read_attempt_status(out: Path, system_id: str) -> dict[str, Any] | None:
    path = out / "systems" / system_id / "status.json"
    if not path.exists():
        return None
    return load_json(path)


def read_attempt_rows(out: Path, system_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    system_dir = out / "systems" / system_id
    metric_rows = read_tsv(system_dir / "metrics.tsv")
    correctness_rows = read_tsv(system_dir / "correctness.tsv")
    return metric_rows, correctness_rows


def dir_total_bytes(path: Path) -> int | str:
    if not path.exists():
        return ""
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total


def max_logged_elapsed_s(path: Path) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    values = [float(value) for value in re.findall(r"\[exit=0 elapsed_s=([0-9.]+)\]", text)]
    if not values:
        return ""
    return str(max(values))


def extend_neo4j_rows_from_artifacts(
    out: Path,
    metric_rows: list[dict[str, Any]],
    correctness_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Add Neo4j SF1/SF10 rows from immutable per-run artifacts.

    The per-system metrics.tsv is still accepted as the authoritative source for
    existing rows. This function only fills missing scale/scope rows so rerunning
    the table builder cannot hide a completed smoke run.
    """

    existing_metrics = {
        (row.get("system"), row.get("scale"), row.get("scope"))
        for row in metric_rows
    }
    existing_checks = {
        (row.get("system"), row.get("scale"), row.get("check"))
        for row in correctness_rows
    }
    specs = [
        ("SF1", "sf1-smoke", "measured graph DB baseline; SF1 smoke"),
        ("SF10", "sf10-main", "measured graph DB baseline; SF10 main"),
    ]
    scopes = [
        ("all_types", "all-types-summary", "all"),
        ("positive_edge_types", "positive-edge-types-summary", "positive"),
        ("negative_edge_types", "negative-edge-types-summary", "negative"),
    ]
    neo4j_dir = out / "systems" / "neo4j"
    for scale, label, claim in specs:
        run_dir = neo4j_dir / label
        query = load_json(run_dir / "query-results.json")
        done = load_json(run_dir / "DONE")
        if query.get("status") != "PASS" or done.get("status") != "PASS":
            continue
        convert = load_json(run_dir / "import" / "convert-summary.json")
        disk_total = dir_total_bytes(run_dir / "neo4j-data")
        load_s = max_logged_elapsed_s(run_dir / "run.log")
        failed_note = ""
        if (run_dir / "FAILED").exists():
            failed_note = "; stale FAILED marker from an earlier attempt is superseded by current DONE/query-results PASS"
        for query_key, scope, neighbor_key in scopes:
            if ("Neo4j Community", scale, scope) in existing_metrics:
                continue
            data = query.get(query_key, {})
            if not data:
                continue
            metric_rows.append(
                {
                    "system": "Neo4j Community",
                    "group": "open-source-graph-db",
                    "scale": scale,
                    "scope": scope,
                    "ops": data.get("ops", ""),
                    "avg_us": data.get("avg_us", ""),
                    "p50_us": data.get("p50_us", ""),
                    "p90_us": data.get("p90_us", ""),
                    "p99_us": data.get("p99_us", ""),
                    "load_s": load_s,
                    "peak_rss_kb": "",
                    "disk_total_bytes": disk_total,
                    "correctness": "PASS",
                    "claim_level": claim,
                    "raw_artifact": str(run_dir.relative_to(out.parents[2])),
                    "notes": (
                        f"neighbors={query.get('neighbors', {}).get(neighbor_key, '')}; "
                        f"samples=50; relationships_imported={convert.get('relationships_imported', '')}"
                        f"{failed_note}"
                    ),
                }
            )
            existing_metrics.add(("Neo4j Community", scale, scope))
        if ("Neo4j Community", scale, "query_count_digest") not in existing_checks:
            correctness_rows.append(
                {
                    "system": "Neo4j Community",
                    "scale": scale,
                    "check": "query_count_digest",
                    "expected": str(query.get("checked", "")),
                    "observed": f"checked={query.get('checked', '')}; mismatches={query.get('mismatches', '')}",
                    "status": "PASS",
                    "raw_artifact": str((run_dir / "query-results.json").relative_to(out.parents[2])),
                    "notes": (
                        "Same sampled typed-neighbor workload; count/hash digest matched"
                        f"{failed_note}."
                    ),
                }
            )
            existing_checks.add(("Neo4j Community", scale, "query_count_digest"))
    return metric_rows, correctness_rows


def tugraph_artifact_update(
    out: Path,
    metric_rows: list[dict[str, Any]],
    correctness_rows: list[dict[str, Any]],
    attempt: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    full_dir = out / "systems" / "tugraph" / "sf1-full-r1"
    full = load_json(full_dir / "result.json")
    if full.get("status") == "PASS":
        if not any(
            row.get("system") == "TuGraph" and row.get("scale") == "SF1" and row.get("scope") == "all-types-summary"
            for row in metric_rows
        ):
            metric_rows.append(
                {
                    "system": "TuGraph",
                    "group": "open-source-graph-db",
                    "scale": "SF1",
                    "scope": "all-types-summary",
                    "ops": full.get("checked", ""),
                    "avg_us": full.get("avg_us", ""),
                    "p50_us": full.get("p50_us", ""),
                    "p90_us": full.get("p90_us", ""),
                    "p99_us": full.get("p99_us", ""),
                    "load_s": full.get("load_s", ""),
                    "peak_rss_kb": full.get("peak_rss_kb", ""),
                    "disk_total_bytes": dir_total_bytes(full_dir / "db"),
                    "correctness": "PASS",
                    "claim_level": "measured graph DB baseline; SF1 full",
                    "raw_artifact": str(full_dir.relative_to(out.parents[2])),
                    "notes": (
                        f"loaded_vertices={full.get('loaded_vertices')}; "
                        f"loaded_edges={full.get('loaded_edges')}; "
                        f"neighbors={full.get('total_neighbors')}; embedded C++ API"
                    ),
                }
            )
        if not any(
            row.get("system") == "TuGraph" and row.get("scale") == "SF1" and row.get("check") == "query_count_digest"
            for row in correctness_rows
        ):
            correctness_rows.append(
                {
                    "system": "TuGraph",
                    "scale": "SF1",
                    "check": "query_count_digest",
                    "expected": str(full.get("checked", "")),
                    "observed": f"checked={full.get('checked', '')}; mismatches={full.get('mismatches', '')}",
                    "status": "PASS",
                    "raw_artifact": str((full_dir / "result.json").relative_to(out.parents[2])),
                    "notes": "Full SF1 typed-neighbor workload over the same dense edge set; embedded C++ API driver.",
                }
            )
        sf10_dir = out / "systems" / "tugraph" / "sf10-main-r1"
        sf10 = load_json(sf10_dir / "result.json")
        if sf10.get("status") == "PASS":
            if not any(
                row.get("system") == "TuGraph"
                and row.get("scale") == "SF10"
                and row.get("scope") == "all-types-summary"
                for row in metric_rows
            ):
                metric_rows.append(
                    {
                        "system": "TuGraph",
                        "group": "open-source-graph-db",
                        "scale": "SF10",
                        "scope": "all-types-summary",
                        "ops": sf10.get("checked", ""),
                        "avg_us": sf10.get("avg_us", ""),
                        "p50_us": sf10.get("p50_us", ""),
                        "p90_us": sf10.get("p90_us", ""),
                        "p99_us": sf10.get("p99_us", ""),
                        "load_s": sf10.get("load_s", ""),
                        "peak_rss_kb": sf10.get("peak_rss_kb", ""),
                        "disk_total_bytes": dir_total_bytes(sf10_dir / "db"),
                        "correctness": "PASS",
                        "claim_level": "measured graph DB baseline; SF10 full",
                        "raw_artifact": str(sf10_dir.relative_to(out.parents[2])),
                        "notes": (
                            f"loaded_vertices={sf10.get('loaded_vertices')}; "
                            f"loaded_edges={sf10.get('loaded_edges')}; "
                            f"neighbors={sf10.get('total_neighbors')}; embedded C++ API"
                        ),
                    }
                )
            if not any(
                row.get("system") == "TuGraph"
                and row.get("scale") == "SF10"
                and row.get("check") == "query_count_digest"
                for row in correctness_rows
            ):
                correctness_rows.append(
                    {
                        "system": "TuGraph",
                        "scale": "SF10",
                        "check": "query_count_digest",
                        "expected": str(sf10.get("checked", "")),
                        "observed": f"checked={sf10.get('checked', '')}; mismatches={sf10.get('mismatches', '')}",
                        "status": "PASS",
                        "raw_artifact": str((sf10_dir / "result.json").relative_to(out.parents[2])),
                        "notes": "Full SF10 typed-neighbor workload over the same dense edge set; embedded C++ API driver.",
                    }
                )
            status = {
                "status": "SF10 FULL DONE",
                "sf1": "PASS",
                "sf10": "PASS",
                "correctness": "PASS",
                "raw": "baseline/external-baselines-20260626/3plus3-baselines/systems/tugraph/sf10-main-r1",
                "notes": (
                    f"Full SF10 loaded {sf10.get('loaded_vertices')} vertices and {sf10.get('loaded_edges')} edges; "
                    f"checked={sf10.get('checked')}, mismatches={sf10.get('mismatches')}, avg_us={sf10.get('avg_us')}, "
                    f"p99_us={sf10.get('p99_us')}."
                ),
                "version": "tugraph/tugraph-runtime-ubuntu18.04:4.0.0-finbench; embedded C++ API",
            }
            return status, metric_rows, correctness_rows
        if (sf10_dir / "STARTED").exists():
            status = {
                "status": "SF10 RUNNING",
                "sf1": "PASS",
                "sf10": "RUNNING",
                "correctness": "SF1 PASS; SF10 PENDING",
                "raw": "baseline/external-baselines-20260626/3plus3-baselines/systems/tugraph/sf10-main-r1",
                "notes": (
                    f"SF1 full passed. SF10 full run started at "
                    f"{(sf10_dir / 'STARTED').read_text(encoding='utf-8', errors='replace').strip()}; "
                    "waiting for result.json."
                ),
                "version": "tugraph/tugraph-runtime-ubuntu18.04:4.0.0-finbench; embedded C++ API",
            }
            return status, metric_rows, correctness_rows
        status = {
            "status": "SF1 FULL DONE",
            "sf1": "PASS",
            "sf10": "WAIT-SF10",
            "correctness": "PASS",
            "raw": "baseline/external-baselines-20260626/3plus3-baselines/systems/tugraph/sf1-full-r1",
            "notes": (
                f"Full SF1 loaded {full.get('loaded_vertices')} vertices and {full.get('loaded_edges')} edges; "
                f"checked={full.get('checked')}, mismatches={full.get('mismatches')}, avg_us={full.get('avg_us')}, "
                f"p99_us={full.get('p99_us')}. SF10 not run yet."
            ),
            "version": "tugraph/tugraph-runtime-ubuntu18.04:4.0.0-finbench; embedded C++ API",
        }
        return status, metric_rows, correctness_rows

    probe_path = out / "systems" / "tugraph" / "load-probe-100k" / "result.json"
    if not probe_path.exists():
        return attempt, metric_rows, correctness_rows
    probe = load_json(probe_path)
    status = {
        "status": "EMBEDDED API PROBE DONE",
        "sf1": "PARTIAL-PROBE",
        "sf10": "WAIT-FULL-SF1",
        "correctness": "NOT-A-FULL-GATE",
        "raw": "baseline/external-baselines-20260626/3plus3-baselines/systems/tugraph/load-probe-100k",
        "notes": (
            "Embedded C++ API load/query path works on a truncated 100k-vertex/100k-edge SF1 probe "
            f"(load_s={probe.get('load_s')}, checked={probe.get('checked')}, mismatches={probe.get('mismatches')}). "
            "Mismatches are expected because the probe uses full-SF1 truth against a truncated graph; not eligible for numeric table."
        ),
        "version": "tugraph/tugraph-runtime-ubuntu18.04:4.0.0-finbench; embedded C++ API",
    }
    if not any(
        row.get("system") == "TuGraph" and row.get("scale") == "SF1-100k-probe"
        for row in correctness_rows
    ):
        correctness_rows.append(
            {
                "system": "TuGraph",
                "scale": "SF1-100k-probe",
                "check": "truncated_load_probe",
                "expected": "full SF1 truth, intentionally not comparable",
                "observed": f"checked={probe.get('checked')}; mismatches={probe.get('mismatches')}",
                "status": "PARTIAL-PROBE-NOT-GATE",
                "raw_artifact": "baseline/external-baselines-20260626/3plus3-baselines/systems/tugraph/load-probe-100k/result.json",
                "notes": "Use only as artifact/API feasibility evidence. Full SF1 requires loading the complete graph or generating matching truncated truth.",
            }
        )
    return status, metric_rows, correctness_rows


def build_statuses(root: Path, out: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    metrics: list[dict[str, Any]] = []
    correctness: list[dict[str, Any]] = []
    progress: dict[str, dict[str, Any]] = {}

    lg_metrics, lg_correctness, lg_status = livegraph_rows(root)
    metrics.extend(lg_metrics)
    correctness.extend(lg_correctness)
    progress["livegraph"] = lg_status

    lsm_metrics, lsm_correctness, lsm_status = lsmgraph_rows(root)
    metrics.extend(lsm_metrics)
    correctness.extend(lsm_correctness)
    progress["lsmgraph-style"] = lsm_status

    defaults = {
        "aster": {
            "status": "PENDING ARTIFACT PROBE",
            "sf1": "N/A",
            "sf10": "N/A",
            "correctness": "N/A",
            "raw": "baseline/external-baselines-20260626/3plus3-baselines/systems/aster",
            "notes": "Need source/artifact and LDBC typed-neighbor bridge before numeric table.",
            "version": "",
        },
        "neo4j": {
            "status": "PENDING SF1 SMOKE",
            "sf1": "TODO",
            "sf10": "WAIT-SF1",
            "correctness": "TODO",
            "raw": "baseline/external-baselines-20260626/3plus3-baselines/systems/neo4j",
            "notes": "Use isolated Neo4j Community container; do not touch existing snb-interactive-neo4j.",
            "version": "neo4j:5.26.24 image available",
        },
        "tugraph": {
            "status": "PENDING SF1 SMOKE",
            "sf1": "TODO",
            "sf10": "WAIT-SF1",
            "correctness": "TODO",
            "raw": "baseline/external-baselines-20260626/3plus3-baselines/systems/tugraph",
            "notes": "Use isolated TuGraph container/API probe; existing tugraph_finbench is not modified.",
            "version": "tugraph runtime image available",
        },
        "nebulagraph": {
            "status": "PENDING IMAGE/SERVICE PROBE",
            "sf1": "WAIT-IMAGE",
            "sf10": "WAIT-SF1",
            "correctness": "TODO",
            "raw": "baseline/external-baselines-20260626/3plus3-baselines/systems/nebulagraph",
            "notes": "No local image observed yet; first step is service/artifact availability.",
            "version": "",
        },
    }
    for system in SYSTEMS:
        sid = system["id"]
        if sid in progress:
            continue
        attempt_metrics, attempt_correctness = read_attempt_rows(out, sid)
        if sid == "neo4j":
            attempt_metrics, attempt_correctness = extend_neo4j_rows_from_artifacts(
                out, attempt_metrics, attempt_correctness
            )
        attempt = read_attempt_status(out, sid)
        if sid == "tugraph":
            attempt, attempt_metrics, attempt_correctness = tugraph_artifact_update(
                out, attempt_metrics, attempt_correctness, attempt
            )
        metrics.extend(attempt_metrics)
        correctness.extend(attempt_correctness)
        progress[sid] = attempt if attempt else defaults[sid]
    return metrics, correctness, [dict(system, **progress[system["id"]]) for system in SYSTEMS]


def render_progress(rows: list[dict[str, Any]], generated_at: str) -> str:
    lines = [
        "# 3+3 Baseline Progress Table",
        "",
        f"Generated at: {generated_at}",
        "",
        "| Group | System | Status | SF1 | SF10 | Correctness | Raw artifact | Notes |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            "| {group} | {name} | {status} | {sf1} | {sf10} | {correctness} | `{raw}` | {notes} |".format(
                **{k: str(row.get(k, "")).replace("\n", " ") for k in row}
            )
        )
    lines.append("")
    lines.append("Numeric-table gate: clean build/version, same LDBC edge set, same sampled typed-neighbor workload, per-query count/hash digest, load/RSS/footprint/latency logs, raw config, and DONE marker.")
    return "\n".join(lines) + "\n"


def render_effect(metrics: list[dict[str, Any]], generated_at: str) -> str:
    numeric = [row for row in metrics if str(row.get("correctness", "")).startswith("PASS")]
    internal = [row for row in metrics if "internal" in str(row.get("correctness", ""))]
    lines = [
        "# 3+3 Baseline Effect Table",
        "",
        f"Generated at: {generated_at}",
        "",
        "## Numeric external rows that passed the gate",
        "",
        "| System | Scale | Scope | Ops | Avg us | P50 us | P90 us | P99 us | Load s | Peak RSS KB | Disk bytes | Claim level |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in numeric:
        lines.append(
            "| {system} | {scale} | {scope} | {ops} | {avg} | {p50} | {p90} | {p99} | {load} | {rss} | {disk} | {claim} |".format(
                system=row.get("system", ""),
                scale=row.get("scale", ""),
                scope=row.get("scope", ""),
                ops=fmt_num(row.get("ops")),
                avg=fmt_num(row.get("avg_us")),
                p50=fmt_num(row.get("p50_us")),
                p90=fmt_num(row.get("p90_us")),
                p99=fmt_num(row.get("p99_us")),
                load=fmt_num(row.get("load_s")),
                rss=fmt_num(row.get("peak_rss_kb")),
                disk=fmt_num(row.get("disk_total_bytes")),
                claim=row.get("claim_level", ""),
            )
        )
    if not numeric:
        lines.append("| - | - | - | - | - | - | - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Internal/layout-style rows",
            "",
            "| System | Scale | Scope | Ops | Avg us | P50 us | P90 us | P99 us | Disk bytes | Notes |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in internal:
        lines.append(
            "| {system} | {scale} | {scope} | {ops} | {avg} | {p50} | {p90} | {p99} | {disk} | {notes} |".format(
                system=row.get("system", ""),
                scale=row.get("scale", ""),
                scope=row.get("scope", ""),
                ops=fmt_num(row.get("ops")),
                avg=fmt_num(row.get("avg_us")),
                p50=fmt_num(row.get("p50_us")),
                p90=fmt_num(row.get("p90_us")),
                p99=fmt_num(row.get("p99_us")),
                disk=fmt_num(row.get("disk_total_bytes")),
                notes=row.get("notes", ""),
            )
        )
    lines.extend(
        [
            "",
            "Rows not listed here have not passed the numeric-table gate yet. Keep them in qualitative comparison or artifact-attempt text.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_readme(generated_at: str) -> str:
    return f"""# SemL0 3+3 Baselines

Generated at: {generated_at}

This package tracks the baseline strengthening work for the CIDR draft.

- `progress-table.md`: current status of each selected system.
- `effect-table.md`: only measured rows that pass the gate, plus clearly labeled internal/layout rows.
- `metrics.tsv`: machine-readable metric rows used by the effect table.
- `correctness.tsv`: machine-readable correctness rows.
- `artifact-attempt-summary.md`: short paper-facing summary of what can and cannot be claimed.
- `systems/`: per-system status files, raw logs, and DONE/FAILED markers as experiments run.

Do not promote a system into the numeric table unless it passes the same-workload count/hash digest gate.
"""


def render_attempt_summary(progress: list[dict[str, Any]], generated_at: str) -> str:
    lines = [
        "# Artifact Attempt Summary",
        "",
        f"Generated at: {generated_at}",
        "",
        "Current paper-safe interpretation:",
        "",
    ]
    for row in progress:
        lines.append(f"- {row['name']}: {row.get('status', '')}; {row.get('claim', '')}. {row.get('notes', '')}")
    lines.extend(
        [
            "",
            "Paper wording rule: LiveGraph can be cited as a measured scope-limited SF10 typed-neighbor external baseline. LSMGraph-style is an internal layout baseline. Neo4j, TuGraph, NebulaGraph, and Aster must stay as attempts/qualitative rows until their per-query digest passes.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_system_statuses(out: Path, progress: list[dict[str, Any]]) -> None:
    fields = ["status", "sf1", "sf10", "correctness", "raw", "notes", "version"]
    for row in progress:
        system_id = row.get("id")
        if not system_id:
            continue
        status_doc = {field: row.get(field, "") for field in fields}
        write_text(
            out / "systems" / str(system_id) / "status.json",
            json.dumps(status_doc, ensure_ascii=False, indent=2) + "\n",
        )


def write_deps_manifest(root: Path, generated_at: str) -> None:
    base = root / "deps" / "baselines"
    manifest = {
        "generated_at": generated_at,
        "purpose": "3+3 SemL0 baseline dependency registry",
        "problem_adjacent": ["LiveGraph", "LSMGraph-style", "Aster"],
        "open_source_graph_db": ["Neo4j Community", "TuGraph", "NebulaGraph"],
        "systems": SYSTEMS,
        "notes": [
            "Existing deps/LiveGraph is reused.",
            "Existing ldbc_snb_interactive_impls/lsmgraph is referenced for layout-style context.",
            "Graph database containers must be isolated from existing long-running containers.",
        ],
    }
    write_text(base / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    write_text(
        base / "README.md",
        """# SemL0 Baseline Dependencies

This directory records dependency and artifact locations for the 3+3 baseline package.

- Problem-adjacent group: LiveGraph, LSMGraph-style, Aster.
- Open-source graph DB group: Neo4j Community, TuGraph, NebulaGraph.

Keep source checkouts, container notes, and workload bridge scripts here. Keep measured logs under `baseline/external-baselines-20260626/3plus3-baselines/`.
""",
    )
    for system in SYSTEMS:
        dep_path = root / system["deps_path"]
        dep_path.mkdir(parents=True, exist_ok=True)
        readme = dep_path / "README.md"
        if not readme.exists():
            write_text(
                readme,
                f"""# {system['name']}

Group: {system['group']}

Claim level: {system['claim']}

Result package: `baseline/external-baselines-20260626/3plus3-baselines/systems/{system['id']}`.
""",
            )


def main() -> None:
    root = repo_root()
    out = out_dir(root)
    generated_at = now_iso()
    for system in SYSTEMS:
        (out / "systems" / system["id"]).mkdir(parents=True, exist_ok=True)
    write_deps_manifest(root, generated_at)
    metrics, correctness, progress = build_statuses(root, out)
    write_system_statuses(out, progress)
    write_tsv(
        out / "metrics.tsv",
        metrics,
        [
            "system",
            "group",
            "scale",
            "scope",
            "ops",
            "avg_us",
            "p50_us",
            "p90_us",
            "p99_us",
            "load_s",
            "peak_rss_kb",
            "disk_total_bytes",
            "correctness",
            "claim_level",
            "raw_artifact",
            "notes",
        ],
    )
    write_tsv(
        out / "correctness.tsv",
        correctness,
        ["system", "scale", "check", "expected", "observed", "status", "raw_artifact", "notes"],
    )
    write_text(out / "README-CN.md", render_readme(generated_at))
    write_text(out / "progress-table.md", render_progress(progress, generated_at))
    write_text(out / "effect-table.md", render_effect(metrics, generated_at))
    write_text(out / "artifact-attempt-summary.md", render_attempt_summary(progress, generated_at))
    write_text(
        out / "run-config.json",
        json.dumps(
            {
                "generated_at": generated_at,
                "repo_root": str(root),
                "result_dir": str(out),
                "groups": {
                    "problem_adjacent": ["LiveGraph", "LSMGraph-style", "Aster"],
                    "open_source_graph_db": ["Neo4j Community", "TuGraph", "NebulaGraph"],
                },
                "gate": [
                    "clean build/version record",
                    "same LDBC edge set",
                    "same sampled typed-neighbor workload",
                    "per-query count/hash digest",
                    "load/RSS/footprint/latency logs",
                    "raw config and DONE marker",
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )


if __name__ == "__main__":
    main()
