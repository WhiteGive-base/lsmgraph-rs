#!/usr/bin/env python3
import argparse
import csv
import json
from pathlib import Path
from typing import Dict, Iterable, List


DATE_TAG = "20260604"
LATEST_CONTROLLED_TAG = "20260604-p57"
LATEST_CONTROLLED_DOC = "seml0-stage-p5-7-latest-code-sf1-controlled-results-20260604.md"
LATEST_BENEFIT_TAG = "20260604-p58"
LATEST_BENEFIT_SUMMARY = "remote-logs/p1-c1-sf1-benefit-scored-20260604-p58/summary.tsv"


def clean_cell(cell: str) -> str:
    return cell.strip().strip("`").replace(",", "")


def parse_markdown_table_after(path: Path, heading: str) -> List[Dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    except StopIteration:
        return []

    table_start = None
    for idx in range(start + 1, len(lines)):
        if lines[idx].lstrip().startswith("|"):
            table_start = idx
            break
    if table_start is None or table_start + 1 >= len(lines):
        return []

    header = [clean_cell(cell) for cell in lines[table_start].strip().strip("|").split("|")]
    rows: List[Dict[str, str]] = []
    for line in lines[table_start + 2 :]:
        if not line.lstrip().startswith("|"):
            break
        cells = [clean_cell(cell) for cell in line.strip().strip("|").split("|")]
        if len(cells) != len(header):
            continue
        rows.append(dict(zip(header, cells)))
    return rows


def write_csv(path: Path, fieldnames: Iterable[str], rows: List[Dict[str, object]]) -> None:
    fields = list(fieldnames)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def read_tsv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def num(value: object) -> float:
    if value is None:
        return 0.0
    try:
        return float(str(value).replace("%", "").replace(",", ""))
    except ValueError:
        return 0.0


def pct_reduction(base: object, value: object) -> float:
    base_num = num(base)
    if base_num <= 0:
        return 0.0
    return round((base_num - num(value)) / base_num * 100.0, 2)


def load_p1_rows(root: Path) -> List[Dict[str, str]]:
    p17 = root / f"seml0-stage-p1-7-results-benefit-scored-sf1-{DATE_TAG}.md"
    p111 = root / f"seml0-stage-p1-11-results-engine-lifetime-budget-sf1-{DATE_TAG}.md"
    rows = parse_markdown_table_after(p17, "## 4. Result Table")
    rows.extend(parse_markdown_table_after(p111, "## 2. Result Table"))

    dedup: Dict[str, Dict[str, str]] = {}
    for row in rows:
        variant = row.get("Variant", "")
        if variant:
            dedup[variant] = row
    return list(dedup.values())


def latest_controlled_rows(root: Path) -> List[Dict[str, object]]:
    path = root / LATEST_CONTROLLED_DOC
    if not path.exists():
        return []

    import_rows = {row.get("layout", ""): row for row in parse_markdown_table_after(path, "## Import And Store Cost")}
    core_rows = {row.get("layout", ""): row for row in parse_markdown_table_after(path, "## Core Storage Bench Mean")}
    all_rows = {row.get("layout", ""): row for row in parse_markdown_table_after(path, "## All-Types Storage Bench Mean")}
    ldbc_rows = {row.get("layout", ""): row for row in parse_markdown_table_after(path, "## LDBC Passing Batch")}
    compare_rows = parse_markdown_table_after(path, "## Neighbor Compare Correctness")

    compare_mismatches = {
        "Schema": "baseline",
        "Full semantic": "",
        "Budgeted semantic": "",
    }
    for layout, prefix in (
        ("Full semantic", "Schema vs full semantic"),
        ("Budgeted semantic", "Schema vs budgeted"),
    ):
        rows = [row for row in compare_rows if row.get("gate", "").startswith(prefix)]
        if rows:
            compare_mismatches[layout] = str(sum(int(num(row.get("mismatches", 0))) for row in rows))

    out: List[Dict[str, object]] = []
    for layout in ("Schema", "Full semantic", "Budgeted semantic"):
        import_row = import_rows.get(layout, {})
        core = core_rows.get(layout, {})
        alltypes = all_rows.get(layout, {})
        ldbc = ldbc_rows.get(layout, {})
        out.append(
            {
                "scale": "SF1",
                "date_tag": LATEST_CONTROLLED_TAG,
                "layout": layout,
                "store_bytes": import_row.get("store bytes", ""),
                "l0_files": import_row.get("L0 files", ""),
                "manifest_bytes": import_row.get("MANIFEST bytes", ""),
                "core_elapsed_ms": core.get("elapsed ms", ""),
                "core_candidate_l0": core.get("candidate L0", ""),
                "core_read_bytes": core.get("IO read bytes", ""),
                "core_read_reduction_pct": core.get("reduction vs schema IO", ""),
                "alltypes_elapsed_ms": alltypes.get("elapsed ms", ""),
                "alltypes_candidate_l0": alltypes.get("candidate L0", ""),
                "alltypes_read_bytes": alltypes.get("IO read bytes", ""),
                "alltypes_read_reduction_pct": alltypes.get("reduction vs schema IO", ""),
                "ldbc_failed": ldbc.get("failed", ""),
                "neighbor_mismatches": compare_mismatches.get(layout, ""),
                "source": LATEST_CONTROLLED_DOC,
                "refresh_status": "latest_code_controlled",
            }
        )
    return out


def latest_benefit_rows(root: Path) -> List[Dict[str, object]]:
    source = root / LATEST_BENEFIT_SUMMARY
    rows = read_tsv_rows(source)
    if not rows:
        return []
    by_variant = {row.get("variant", ""): row for row in rows}
    schema = by_variant.get("schema", {})

    out: List[Dict[str, object]] = []
    for variant in ("schema", "full_semantic", "benefit_scored"):
        row = by_variant.get(variant, {})
        if not row:
            continue
        out.append(
            {
                "scale": "SF1",
                "date_tag": LATEST_BENEFIT_TAG,
                "variant": variant,
                "store_bytes": row.get("store_bytes", ""),
                "l0_files": row.get("l0_files", ""),
                "manifest_bytes": row.get("manifest_bytes", ""),
                "edge_exact_files": row.get("edge_exact_files", ""),
                "mixed_edge_files": row.get("mixed_edge_files", ""),
                "core_read_bytes": row.get("core_read_bytes", ""),
                "core_read_reduction_pct": pct_reduction(
                    schema.get("core_read_bytes"), row.get("core_read_bytes")
                ),
                "core_candidate_l0": row.get("core_candidate_l0", ""),
                "alltypes_read_bytes": row.get("alltypes_read_bytes", ""),
                "alltypes_read_reduction_pct": pct_reduction(
                    schema.get("alltypes_read_bytes"), row.get("alltypes_read_bytes")
                ),
                "alltypes_candidate_l0": row.get("alltypes_candidate_l0", ""),
                "core_mismatches": row.get("core_mismatches", ""),
                "alltypes_mismatches": row.get("alltypes_mismatches", ""),
                "source": LATEST_BENEFIT_SUMMARY,
                "refresh_status": "latest_code_benefit_scored",
            }
        )
    return out


def main_performance_rows(p1_rows: List[Dict[str, str]]) -> List[Dict[str, object]]:
    by_variant = {row["Variant"]: row for row in p1_rows if "Variant" in row}
    schema = by_variant.get("schema", {})
    selected = [
        ("schema", "baseline"),
        ("full semantic", "upper_bound_semantic"),
        ("benefit scored", "best_current_budgeted_sf1"),
    ]
    out: List[Dict[str, object]] = []
    for variant, role in selected:
        row = by_variant.get(variant, {})
        if not row:
            continue
        out.append(
            {
                "scale": "SF1",
                "layout": variant,
                "role": role,
                "store_bytes": row.get("Store bytes", ""),
                "l0_files": row.get("L0 files", ""),
                "manifest_bytes": row.get("Manifest bytes", ""),
                "core_read_bytes": row.get("Core read bytes", ""),
                "alltypes_read_bytes": row.get("All-types read bytes", ""),
                "core_read_reduction_pct": pct_reduction(
                    schema.get("Core read bytes"), row.get("Core read bytes")
                ),
                "alltypes_read_reduction_pct": pct_reduction(
                    schema.get("All-types read bytes"), row.get("All-types read bytes")
                ),
                "correctness": "pass" if row.get("Core mismatches", "0") in ("0", "NA") else "check",
                "source": f"seml0-stage-p1-7-results-benefit-scored-sf1-{DATE_TAG}.md",
                "refresh_status": "needs_latest_code_refresh",
            }
        )

    out.append(
        {
            "scale": "SF30",
            "layout": "best_current_semantic",
            "role": "candidate_large_scale_evidence",
            "store_bytes": "",
            "l0_files": "",
            "manifest_bytes": "",
            "core_read_bytes": "",
            "alltypes_read_bytes": "",
            "core_read_reduction_pct": "70.73_elapsed_candidate",
            "alltypes_read_reduction_pct": "79.53_io_candidate",
            "correctness": "needs_refresh",
            "source": "seml0-final-paper-direction-20260603.md",
            "refresh_status": "candidate_evidence_refresh_before_submission",
        }
    )
    return out


def ablation_rows(p1_rows: List[Dict[str, str]]) -> List[Dict[str, object]]:
    interpretations = {
        "schema": "baseline",
        "full semantic": "strong read reduction but higher fanout",
        "fine512k": "threshold policy with good reads and high fanout",
        "high640k": "low fanout but weak reads",
        "benefit scored": "best current SF1 read/fanout tradeoff",
        "globalbudget512": "hard budget binds but early-flush bias regresses reads",
    }
    out = []
    for row in p1_rows:
        variant = row.get("Variant", "")
        if variant not in interpretations:
            continue
        out.append(
            {
                "scale": "SF1",
                "variant": variant,
                "core_read_bytes": row.get("Core read bytes", ""),
                "alltypes_read_bytes": row.get("All-types read bytes", ""),
                "l0_files": row.get("L0 files", ""),
                "store_bytes": row.get("Store bytes", ""),
                "manifest_bytes": row.get("Manifest bytes", ""),
                "interpretation": interpretations[variant],
                "source": "P1.7/P1.11 stage docs",
                "refresh_status": "needs_latest_code_refresh",
            }
        )
    return out


def feedback_rows(root: Path) -> List[Dict[str, object]]:
    path = root / "remote-logs" / f"p3-feedback-vs-no-feedback-p33-{DATE_TAG}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    rows: List[Dict[str, object]] = []
    for variant in ("no_feedback", "feedback"):
        for phase_key, phase_label in (("phase_a", "A"), ("phase_b", "B")):
            if variant == "no_feedback":
                phase = data["no_feedback"][phase_key]
                before = phase["before"]
                after = phase["after_without_compaction"]
                selected = 0
                outputs = 0
                changed = "N/A"
            else:
                phase = data[phase_key]
                before = phase["before"]
                after = phase["after"]
                decision = phase.get("decision", {})
                selected = decision.get("selected_l0_segments", "")
                outputs = decision.get("output_segments", "")
                changed = data.get("summary", {}).get("selected_ranges_changed", "") if phase_label == "B" else "N/A"
            rows.append(
                {
                    "variant": "no-feedback" if variant == "no_feedback" else "feedback",
                    "phase": phase_label,
                    "before_candidate_l0": before.get("candidate_l0_segments", ""),
                    "after_candidate_l0": after.get("candidate_l0_segments", ""),
                    "after_avg_candidate_l0_per_query": after.get("avg_candidate_l0_segments", ""),
                    "before_elapsed_ms": before.get("elapsed_ms", ""),
                    "after_elapsed_ms": after.get("elapsed_ms", ""),
                    "before_io_read_bytes": before.get("io_read_bytes", ""),
                    "after_io_read_bytes": after.get("io_read_bytes", ""),
                    "selected_l0_segments": selected,
                    "output_segments": outputs,
                    "selected_range_changed": changed,
                    "source": str(path),
                }
            )
    return rows


def correctness_rows() -> List[Dict[str, object]]:
    items = [
        ("schema_epoch", "old segments remain readable", "schema_epoch_change_keeps_old_segments_readable"),
        ("schema_catalog", "catalog persists and drives new epoch", "schema_catalog_persists_changes_and_drives_new_segment_epoch"),
        ("new_edge_label", "exact old segments prune; mixed old segments read", "new_edge_label_prunes_exact_segments_but_reads_mixed_segments"),
        ("property_presence", "required property prunes exact-absent insert-only records", "property_required_predicate_prunes_exact_absent_segments"),
        ("legacy_metadata", "missing property summary is conservative", "legacy_missing_property_summary_keeps_required_property_conservative"),
        ("tombstone_snapshot", "old snapshot sees insert before delete", "snapshot_read_sees_insert_before_later_delete_across_reopen"),
        ("compaction_history", "compaction preserves old snapshot after tombstone", "compaction_preserves_old_snapshot_after_tombstone"),
        ("degree_invalidation", "degree-changing tombstones keep pruning conservative", "degree_change_tombstones_keep_explicit_low_degree_query_conservative"),
        ("schema_snapshot", "edge-label schema change composes with snapshots", "schema_epoch_snapshot_mixed_delta_survives_compaction_and_reopen"),
        ("property_snapshot", "property schema change composes with snapshots", "property_schema_snapshot_mixed_delta_survives_compaction_and_reopen"),
        ("property_tombstone", "exact-absent tombstones retained for merge safety", "required_property_keeps_exact_absent_tombstone_segments"),
        ("snapshot_gc", "history collapse requires safe snapshot", "snapshot_retention_*"),
    ]
    return [
        {
            "category": category,
            "claim": claim,
            "evidence": evidence,
            "latest_status": "pass",
            "latest_gate": "engine_tests 26 passed; lib tests 16 passed after P4.5",
        }
        for category, claim, evidence in items
    ]


def artifact_rows() -> List[Dict[str, object]]:
    return [
        {
            "goal": "SF1 controlled summary",
            "command_or_script": "summarize-p1-c1-sf1-controlled.py",
            "output": f"seml0-stage-p1-results-sf1-controlled-{DATE_TAG}.md",
        },
        {
            "goal": "Benefit-scored SF1",
            "command_or_script": "run-p1-c1-benefit-scored-sf1.sh",
            "output": f"remote-logs/p1-c1-sf1-benefit-scored-{DATE_TAG}/summary.tsv",
        },
        {
            "goal": "Feedback workload shift",
            "command_or_script": "run-p3-feedback-workload-shift-microbench.sh",
            "output": f"remote-logs/p3-feedback-workload-shift-p32-{DATE_TAG}.json",
        },
        {
            "goal": "Feedback vs no-feedback",
            "command_or_script": "run-p3-feedback-vs-no-feedback-microbench.sh",
            "output": f"remote-logs/p3-feedback-vs-no-feedback-p33-{DATE_TAG}.json",
        },
        {
            "goal": "Engine correctness",
            "command_or_script": "cargo test --test engine_tests",
            "output": "26 passed",
        },
        {
            "goal": "Library correctness",
            "command_or_script": "cargo test --lib",
            "output": "16 passed",
        },
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--out-dir", default=f"remote-logs/p5-normalized-evaluation-{DATE_TAG}")
    args = parser.parse_args()

    root = Path(args.root)
    out_dir = root / args.out_dir
    p1_rows = load_p1_rows(root)

    outputs = {
        "main_performance.csv": (
            [
                "scale",
                "layout",
                "role",
                "store_bytes",
                "l0_files",
                "manifest_bytes",
                "core_read_bytes",
                "alltypes_read_bytes",
                "core_read_reduction_pct",
                "alltypes_read_reduction_pct",
                "correctness",
                "source",
                "refresh_status",
            ],
            main_performance_rows(p1_rows),
        ),
        "ablation.csv": (
            [
                "scale",
                "variant",
                "core_read_bytes",
                "alltypes_read_bytes",
                "l0_files",
                "store_bytes",
                "manifest_bytes",
                "interpretation",
                "source",
                "refresh_status",
            ],
            ablation_rows(p1_rows),
        ),
        "feedback_adaptation.csv": (
            [
                "variant",
                "phase",
                "before_candidate_l0",
                "after_candidate_l0",
                "after_avg_candidate_l0_per_query",
                "before_elapsed_ms",
                "after_elapsed_ms",
                "before_io_read_bytes",
                "after_io_read_bytes",
                "selected_l0_segments",
                "output_segments",
                "selected_range_changed",
                "source",
            ],
            feedback_rows(root),
        ),
        "latest_code_controlled_sf1.csv": (
            [
                "scale",
                "date_tag",
                "layout",
                "store_bytes",
                "l0_files",
                "manifest_bytes",
                "core_elapsed_ms",
                "core_candidate_l0",
                "core_read_bytes",
                "core_read_reduction_pct",
                "alltypes_elapsed_ms",
                "alltypes_candidate_l0",
                "alltypes_read_bytes",
                "alltypes_read_reduction_pct",
                "ldbc_failed",
                "neighbor_mismatches",
                "source",
                "refresh_status",
            ],
            latest_controlled_rows(root),
        ),
        "latest_code_benefit_scored_sf1.csv": (
            [
                "scale",
                "date_tag",
                "variant",
                "store_bytes",
                "l0_files",
                "manifest_bytes",
                "edge_exact_files",
                "mixed_edge_files",
                "core_read_bytes",
                "core_read_reduction_pct",
                "core_candidate_l0",
                "alltypes_read_bytes",
                "alltypes_read_reduction_pct",
                "alltypes_candidate_l0",
                "core_mismatches",
                "alltypes_mismatches",
                "source",
                "refresh_status",
            ],
            latest_benefit_rows(root),
        ),
        "correctness.csv": (
            ["category", "claim", "evidence", "latest_status", "latest_gate"],
            correctness_rows(),
        ),
        "artifact_commands.csv": (
            ["goal", "command_or_script", "output"],
            artifact_rows(),
        ),
    }

    for filename, (fields, rows) in outputs.items():
        write_csv(out_dir / filename, fields, rows)

    manifest = {
        "date_tag": DATE_TAG,
        "output_dir": str(out_dir),
        "files": sorted(outputs.keys()),
        "notes": [
            "P1 performance rows are extracted from stage documents and should be refreshed before submission.",
            "P5.7 latest-code controlled rows are extracted separately and do not replace benefit-scored rows.",
            "P5.8 latest-code benefit-scored rows refresh the candidate budgeted policy.",
            "P3 feedback rows are extracted from JSON artifacts.",
            "P4 correctness rows record latest verified gate after P4.5.",
        ],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(out_dir)


if __name__ == "__main__":
    main()
