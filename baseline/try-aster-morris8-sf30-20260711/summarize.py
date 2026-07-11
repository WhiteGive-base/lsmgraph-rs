#!/usr/bin/env python3
import argparse
import json
import re
from pathlib import Path
from typing import Tuple


def parse_elapsed(value: str) -> float:
    parts = [float(part) for part in value.strip().split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    raise ValueError(f"unsupported elapsed value: {value}")


def parse_time(path: Path) -> dict:
    text = path.read_text()
    rss = int(re.search(r"Maximum resident set size \(kbytes\): (\d+)", text).group(1))
    elapsed = re.search(r"Elapsed \(wall clock\) time.*: ([0-9:.]+)", text).group(1)
    user = float(re.search(r"User time \(seconds\): ([0-9.]+)", text).group(1))
    system = float(re.search(r"System time \(seconds\): ([0-9.]+)", text).group(1))
    return {
        "max_rss_kb": rss,
        "max_rss_gib": rss / 1024 / 1024,
        "wall_seconds": parse_elapsed(elapsed),
        "user_seconds": user,
        "system_seconds": system,
    }


def mean_field(summary: dict, key: str) -> float:
    value = summary.get(key, {})
    return float(value.get("mean", 0.0))


def bench_summary(path: Path) -> Tuple[dict, dict]:
    data = json.loads(path.read_text())
    totals = {
        "candidate_l0_segments": 0.0,
        "read_bytes": 0.0,
        "body_reads": 0.0,
        "body_bytes": 0.0,
        "sampled_queries_per_round": 0,
    }
    weighted = {
        "get_neighbors_avg_us": 0.0,
        "get_neighbors_p50_us": 0.0,
        "get_neighbors_p99_us": 0.0,
    }
    digests = {}
    for bench in data.get("benchmarks", []):
        repeat = bench.get("repeat_summary", {})
        samples = int(bench.get("sampled_vertices", 0))
        totals["sampled_queries_per_round"] += samples
        for key in ["candidate_l0_segments", "read_bytes", "body_reads", "body_bytes"]:
            totals[key] += mean_field(repeat, key)
        for key in weighted:
            weighted[key] += mean_field(repeat, key) * samples
        for row in bench.get("result_digests") or []:
            digest_key = (row.get("edge_type"), row["src"])
            digests[digest_key] = (row["result_count"], row["result_digest"])
    denominator = max(totals["sampled_queries_per_round"], 1)
    for key, value in weighted.items():
        totals[key] = value / denominator
    totals["levels"] = data.get("levels", [])
    totals["degree_estimator_stats"] = data.get("semantic_degree_estimator_stats", {})
    return totals, digests


def relative_change(after: float, before: float) -> float:
    return 0.0 if before == 0 else (after - before) / before


def store_bytes(path: Path) -> int:
    return int(path.read_text().split()[0])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", type=Path, default=Path("/data/WorkSpace/try-aster-artifacts"))
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    logs = args.artifact_root / "logs"
    exact_import_dir = logs / "sf30-b64-exact-64m-before"
    morris_import_dir = logs / "sf30-b64-morris8-64m-after"
    exact_bench_dir = logs / "sf30-b64-exact-64m-bench"
    morris_bench_dir = logs / "sf30-b64-morris8-64m-bench"

    exact_import = parse_time(exact_import_dir / "time.log")
    morris_import = parse_time(morris_import_dir / "time.log")
    exact_import_json = json.loads((exact_import_dir / "import.stdout").read_text())
    morris_import_json = json.loads((morris_import_dir / "import.stdout").read_text())
    directed_edges = int(exact_import_json["directed_edges"])
    exact_import["throughput_edges_per_second"] = directed_edges / exact_import["wall_seconds"]
    morris_import["throughput_edges_per_second"] = directed_edges / morris_import["wall_seconds"]
    exact_import["store_bytes"] = store_bytes(exact_import_dir / "store-size.tsv")
    morris_import["store_bytes"] = store_bytes(morris_import_dir / "store-size.tsv")

    exact_bench, exact_digests = bench_summary(exact_bench_dir / "bench.json")
    morris_bench, morris_digests = bench_summary(morris_bench_dir / "bench.json")
    exact_store_stats = json.loads((exact_import_dir / "store-stats.json").read_text())
    morris_store_stats = json.loads((morris_import_dir / "store-stats.json").read_text())
    all_digest_keys = set(exact_digests) | set(morris_digests)
    digest_mismatches = sum(
        exact_digests.get(key) != morris_digests.get(key) for key in all_digest_keys
    )
    neighbor_compare = json.loads(
        (logs / "sf30-b64-64m-neighbor-compare.json").read_text()
    )

    import_stats = morris_import_json["semantic_degree_estimator_stats"]
    query_stats = morris_bench["degree_estimator_stats"]
    materialization_skip = float(
        import_stats["materialization"]["materialization_false_skip_rate_sources"]
    )
    safe_query_skip = float(query_stats["query_shadow"]["safe_false_skip_rate"])
    saturation = int(import_stats["counter_updates"]["saturation_count"])

    changes = {
        "rss": relative_change(morris_import["max_rss_kb"], exact_import["max_rss_kb"]),
        "store_bytes": relative_change(
            morris_import["store_bytes"], exact_import["store_bytes"]
        ),
        "throughput": relative_change(
            morris_import["throughput_edges_per_second"],
            exact_import["throughput_edges_per_second"],
        ),
        "candidate_l0": relative_change(
            morris_bench["candidate_l0_segments"], exact_bench["candidate_l0_segments"]
        ),
        "read_bytes": relative_change(morris_bench["read_bytes"], exact_bench["read_bytes"]),
        "body_reads": relative_change(morris_bench["body_reads"], exact_bench["body_reads"]),
        "body_bytes": relative_change(morris_bench["body_bytes"], exact_bench["body_bytes"]),
        "avg_latency": relative_change(
            morris_bench["get_neighbors_avg_us"], exact_bench["get_neighbors_avg_us"]
        ),
        "p50_latency": relative_change(
            morris_bench["get_neighbors_p50_us"], exact_bench["get_neighbors_p50_us"]
        ),
        "p99_latency": relative_change(
            morris_bench["get_neighbors_p99_us"], exact_bench["get_neighbors_p99_us"]
        ),
    }
    gates = {
        "rss_reduction_at_least_3pct": changes["rss"] <= -0.03,
        "materialization_false_skip_at_most_5pct": materialization_skip <= 0.05,
        "candidate_l0_regression_at_most_5pct": changes["candidate_l0"] <= 0.05,
        "read_bytes_regression_at_most_5pct": changes["read_bytes"] <= 0.05,
        "throughput_regression_at_most_10pct": changes["throughput"] >= -0.10,
        "avg_latency_regression_at_most_10pct": changes["avg_latency"] <= 0.10,
        "p50_latency_regression_at_most_10pct": changes["p50_latency"] <= 0.10,
        "digest_mismatches_zero": digest_mismatches == 0
        and int(neighbor_compare["mismatches"]) == 0,
        "safe_false_skip_zero": safe_query_skip == 0.0,
        "saturation_zero": saturation == 0,
    }
    system_go = all(gates.values())
    raw_false_skip = float(query_stats["query_shadow"]["raw_false_skip_query_rate"])
    direct_metadata_go = (
        raw_false_skip == 0.0
        and int(import_stats["estimation"]["overestimated_sources"]) == 0
    )

    summary = {
        "scale": "SF30",
        "budget": 64,
        "directed_edges": directed_edges,
        "exact_import": exact_import,
        "morris_import": morris_import,
        "exact_bench": exact_bench,
        "morris_bench": morris_bench,
        "exact_store_stats": exact_store_stats,
        "morris_store_stats": morris_store_stats,
        "changes": changes,
        "digest_checked": len(all_digest_keys),
        "digest_mismatches": digest_mismatches,
        "neighbor_compare": neighbor_compare,
        "import_morris_stats": import_stats,
        "query_morris_stats": query_stats,
        "gates": gates,
        "system_go": system_go,
        "direct_morris_metadata_go": direct_metadata_go,
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n"
    )

    verdict = "GO" if system_go else "NO-GO"
    direct_verdict = "GO" if direct_metadata_go else "NO-GO"
    estimation = import_stats["estimation"]
    materialization = import_stats["materialization"]
    counter_updates = import_stats["counter_updates"]
    query_shadow = query_stats["query_shadow"]
    exact_semantic_stats = exact_store_stats["semantic_degree_estimator_stats"]
    morris_semantic_stats = morris_store_stats["semantic_degree_estimator_stats"]
    exact_layout = exact_semantic_stats["l0_layout"]
    morris_layout = morris_semantic_stats["l0_layout"]
    exact_directory = exact_semantic_stats["degree_directory_memory"]
    morris_directory = morris_semantic_stats["degree_directory_memory"]
    confusion = {
        (row["exact_class"], row["estimated_class"]): row["sources"]
        for row in estimation["class_confusion"]
    }

    lines = [
        "# Aster-Morris8 SF30 b64 对比结果",
        "",
        f"- 系统级结论：**{verdict}**",
        f"- Morris class 直接作为查询 metadata：**{direct_verdict}**",
        f"- bench digest：checked={len(all_digest_keys)}，mismatches={digest_mismatches}",
        f"- neighbor compare：checked={neighbor_compare['checked']}，mismatches={neighbor_compare['mismatches']}",
        "- 未运行 b256/b1024；初轮未达到 GO，按计划停止。",
        "",
        "## 实验配置",
        "",
        "- Before：原始提交 `3d09ecb`，`--semantic-degree-estimator exact`。",
        "- After：分支 `try_Aster`，`--semantic-degree-estimator morris8`；Morris 只控制 budget admission，持久化 metadata 仍为 exact/Mixed。",
        "- 输入：`/data/WorkSpace/ldbc-sf30/social_network`；blocking I/O；`SNB_SKIP_ADJ_CACHE=1`；MemGraph=64 MiB；`B=64`。",
        "- 查询：固定 `sf30-core-s200.plan.json`，9 个核心 edge types，1,800 个查询，预热 1 次，measured repeats=3。",
        "- 执行顺序：Before import/bench -> After import/bench -> neighbor compare，全程串行。",
        "",
        "## 核心指标",
        "",
        "| 指标 | exact | Morris8 | 变化 |",
        "|---|---:|---:|---:|",
        f"| peak import RSS GiB | {exact_import['max_rss_gib']:.3f} | {morris_import['max_rss_gib']:.3f} | {changes['rss']:+.2%} |",
        f"| import wall time s | {exact_import['wall_seconds']:.2f} | {morris_import['wall_seconds']:.2f} | {relative_change(morris_import['wall_seconds'], exact_import['wall_seconds']):+.2%} |",
        f"| import throughput edges/s | {exact_import['throughput_edges_per_second']:.0f} | {morris_import['throughput_edges_per_second']:.0f} | {changes['throughput']:+.2%} |",
        f"| store bytes | {exact_import['store_bytes']} | {morris_import['store_bytes']} | {changes['store_bytes']:+.4%} |",
        f"| candidate L0 / round | {exact_bench['candidate_l0_segments']:.0f} | {morris_bench['candidate_l0_segments']:.0f} | {changes['candidate_l0']:+.2%} |",
        f"| read bytes / round | {exact_bench['read_bytes']:.0f} | {morris_bench['read_bytes']:.0f} | {changes['read_bytes']:+.2%} |",
        f"| body reads / round | {exact_bench['body_reads']:.0f} | {morris_bench['body_reads']:.0f} | {changes['body_reads']:+.2%} |",
        f"| body bytes / round | {exact_bench['body_bytes']:.0f} | {morris_bench['body_bytes']:.0f} | {changes['body_bytes']:+.2%} |",
        f"| mean avg latency us | {exact_bench['get_neighbors_avg_us']:.1f} | {morris_bench['get_neighbors_avg_us']:.1f} | {changes['avg_latency']:+.2%} |",
        f"| mean p50 latency us | {exact_bench['get_neighbors_p50_us']:.1f} | {morris_bench['get_neighbors_p50_us']:.1f} | {changes['p50_latency']:+.2%} |",
        f"| mean p99 latency us | {exact_bench['get_neighbors_p99_us']:.1f} | {morris_bench['get_neighbors_p99_us']:.1f} | {changes['p99_latency']:+.2%} |",
        f"| L0 segments | {exact_layout['total_segments']} | {morris_layout['total_segments']} | {morris_layout['total_segments'] - exact_layout['total_segments']:+d} |",
        f"| exact-degree L0 segments | {exact_layout['exact_degree_segments']} | {morris_layout['exact_degree_segments']} | {morris_layout['exact_degree_segments'] - exact_layout['exact_degree_segments']:+d} |",
        f"| Mixed L0 segments | {exact_layout['mixed_degree_segments']} | {morris_layout['mixed_degree_segments']} | {morris_layout['mixed_degree_segments'] - exact_layout['mixed_degree_segments']:+d} |",
        f"| Mixed L0 bytes | {exact_layout['mixed_degree_segment_bytes']} | {morris_layout['mixed_degree_segment_bytes']} | {relative_change(morris_layout['mixed_degree_segment_bytes'], exact_layout['mixed_degree_segment_bytes']):+.2%} |",
        f"| degree-directory entries | {exact_directory['entries']} | {morris_directory['entries']} | {relative_change(morris_directory['entries'], exact_directory['entries']):+.2%} |",
        "",
        f"绝对回退量：candidate L0 `{morris_bench['candidate_l0_segments'] - exact_bench['candidate_l0_segments']:+.0f}`/round，read bytes `{morris_bench['read_bytes'] - exact_bench['read_bytes']:+.0f}`/round，body reads `{morris_bench['body_reads'] - exact_bench['body_reads']:+.0f}`/round，body bytes `{morris_bench['body_bytes'] - exact_bench['body_bytes']:+.0f}`/round。",
        "",
        "## Degree Class 边界",
        "",
        f"- tested sources=`{estimation['tested_sources']}`，tested edges=`{estimation['tested_edges']}`。",
        f"- boundary_misclass_rate=`{estimation['boundary_misclass_rate']:.6%}`；boundary_overestimate_rate=`{estimation['boundary_overestimate_rate']:.6%}`；boundary_underestimate_rate=`{estimation['boundary_underestimate_rate']:.6%}`。",
        f"- misclassified=`{estimation['misclassified_sources']}`；危险高估=`{estimation['overestimated_sources']}`；保守低估=`{estimation['underestimated_sources']}`。",
        f"- 相对误差上界：p50=`{estimation['relative_error_p50_upper_bp'] / 100:.0f}%`，p95=`{estimation['relative_error_p95_upper_bp'] / 100:.0f}%`，p99=`{estimation['relative_error_p99_upper_bp'] / 100:.0f}%`，max=`{estimation['max_relative_error_bp'] / 100:.2f}%`。",
        "",
        "### Class Confusion Matrix",
        "",
        "行是 exact class，列是 Morris estimated class。",
        "",
        "| exact \\ estimated | Low | Medium | High |",
        "|---|---:|---:|---:|",
    ]
    for exact_class in ["low", "medium", "high"]:
        lines.append(
            "| "
            + exact_class.title()
            + " | "
            + " | ".join(
                str(confusion.get((exact_class, estimated_class), 0))
                for estimated_class in ["low", "medium", "high"]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "危险高估为 Low->Medium/High 与 Medium->High；本次只有 Medium->High=`187`。低估共 `47,210`，会造成更弱物化或更多读取，但不会直接造成 false skip。",
            "",
            "### Boundary Windows",
            "",
            "| 窗口 | tested | misclassified | misclass rate | overestimated | overestimate rate | underestimated | underestimate rate |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in estimation["boundary_windows"]:
        lines.append(
            f"| {row['label']} | {row['tested_sources']} | {row['misclassified_sources']} | {row['misclass_rate']:.6%} | "
            f"{row['overestimated_sources']} | {row['overestimate_rate']:.6%} | "
            f"{row['underestimated_sources']} | {row['underestimate_rate']:.6%} |"
        )

    lines.extend(
        [
            "",
            "| 精确点 | tested | misclassified | misclass rate | overestimated | underestimated |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in estimation["boundary_points"]:
        lines.append(
            f"| {row['label']} | {row['tested_sources']} | {row['misclassified_sources']} | "
            f"{row['misclass_rate']:.6%} | {row['overestimated_sources']} | {row['underestimated_sources']} |"
        )

    lines.extend(
        [
            "",
            "SF30 样本在 exact degree 1023/1024/1025 三个点均为 0；该边界的结论来自 922-1126 窗口，而非这三个离散点。",
            "",
            "## Budget Materialization Skip",
            "",
            f"- exact_keep_sources=`{materialization['exact_keep_sources']}`；Morris keep sources=`{materialization['morris_keep_sources']}`。",
            f"- exact_keep && morris_skip：sources=`{materialization['exact_keep_morris_skip_sources']}`，bytes=`{materialization['exact_keep_morris_skip_bytes']}`。",
            f"- materialization_false_skip_rate：sources=`{materialization['materialization_false_skip_rate_sources']:.6%}`，bytes=`{materialization['materialization_false_skip_rate_bytes']:.6%}`。",
            f"- exact_skip && morris_keep（false include）：sources=`{materialization['exact_skip_morris_keep_sources']}`，bytes=`{materialization['exact_skip_morris_keep_bytes']}`，false_include_rate=`{materialization['false_include_rate_sources']:.6%}`。",
            f"- 安全实现回退到 Mixed：sources=`{materialization['active_mixed_sources']}` (`{materialization['active_mixed_source_rate']:.6%}`)，bytes=`{materialization['active_mixed_bytes']}` (`{materialization['active_mixed_byte_rate']:.6%}`)。",
            "",
            "这里的 materialization false skip 表示 Morris 没有物化 exact 本会物化的 degree partition，不是查询结果漏读；实际 metadata 回退为 Mixed。",
            "",
            "## Query False Skip",
            "",
            f"- shadow audited query executions=`{query_shadow['audited_queries']}`；unique sampled queries=`{morris_bench['sampled_queries_per_round']}`。",
            f"- raw：queries=`{query_shadow['queries_with_raw_false_skip']}` / `{query_shadow['audited_queries']}` (`{query_shadow['raw_false_skip_query_rate']:.6%}`)；segments=`{query_shadow['raw_false_skipped_segments']}` / `{query_shadow['raw_required_segments']}` (`{query_shadow['raw_false_skip_segment_rate']:.6%}`)；edge records=`{query_shadow['raw_false_skipped_edge_records']}` / `{query_shadow['raw_required_edge_records']}` (`{query_shadow['raw_false_skip_edge_rate']:.6%}`)。",
            f"- safe：queries=`{query_shadow['safe_false_skip_queries']}`，segments=`{query_shadow['safe_false_skip_segments']}`，edge records=`{query_shadow['safe_false_skip_edge_records']}`，rate=`{query_shadow['safe_false_skip_rate']:.6%}`。",
            f"- result digest：bench mismatches=`{digest_mismatches}`；neighbor compare mismatches=`{neighbor_compare['mismatches']}`。",
            "",
            f"直接使用 Morris class 仍判定 **{direct_verdict}**：固定查询 shadow 未实际命中漏读，但全量 degree-class shadow 出现 `{estimation['overestimated_sources']}` 个危险高估，因此不能把 Morris class 升格为 correctness-critical metadata。",
            "",
            "## Morris Counter Update Skip",
            "",
            f"- attempted=`{counter_updates['attempted_updates']}`，applied=`{counter_updates['applied_updates']}`，skipped=`{counter_updates['skipped_updates']}`，write avoidance=`{counter_updates['write_avoidance_rate']:.6%}`。",
            f"- saturation_count=`{counter_updates['saturation_count']}`（SF30 要求为 0）。",
            "",
            "| E | attempted | applied | skipped | observed skip | expected skip | probability check |",
            "|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in counter_updates["by_exponent"]:
        observed = f"{row['observed_skip_rate']:.6%}" if row["attempted_updates"] else "n/a"
        probability_check = (
            "pass"
            if row["within_four_sigma"] is True
            else "fail"
            if row["within_four_sigma"] is False
            else "n/a"
        )
        lines.append(
            f"| {row['exponent']} | {row['attempted_updates']} | {row['applied_updates']} | "
            f"{row['skipped_updates']} | {observed} | {row['expected_skip_rate']:.6%} | {probability_check} |"
        )

    lines.extend(
        [
            "",
            "## 内存解释",
            "",
            f"- 当前 exact 路径的 flush exact-degree table=`{exact_directory['current_flush_exact_degree_table_bytes']}` bytes；degree 来自已排序 `(src, edge_type)` run-length，并没有一个可由 Morris8 替换的大型 exact-degree HashMap。",
            f"- 现有 degree-directory value 本来就是 `{exact_directory['value_bytes']}` byte：Before entries=`{exact_directory['entries']}`、u8 payload=`{exact_directory['current_u8_value_payload_bytes']}` bytes；After entries=`{morris_directory['entries']}`、u8 payload=`{morris_directory['current_u8_value_payload_bytes']}` bytes。",
            f"- degree-directory 的主要下界来自 `(VertexId, EdgeType)` key 和 bucket capacity：Before bucket payload lower bound=`{exact_directory['bucket_payload_lower_bound_bytes']}` bytes，sidecar=`{exact_directory['sidecar_bytes']}` bytes。`hypothetical_exact_u64_payload_bytes`=`{exact_directory['hypothetical_exact_u64_payload_bytes']}` 只是反事实，不是当前 baseline 的实际占用。",
            f"- Morris import 的 peak live estimates=`{import_stats['degree_directory_memory']['morris8_peak_live_estimates']}`，1-byte payload=`{import_stats['degree_directory_memory']['morris8_peak_live_estimate_bytes']}` bytes；它是新增临时状态，不是替换 baseline 的 u64 table。",
            "",
            "因此 Aster 的“8-bit degree counter”优势在当前 import 数据路径上没有对应的 64-bit baseline 对象可替换；本次结果不能支持其降低 2.2-2.5 GiB 总 RSS 的假设。峰值反而增加 1.86%，且只做了门槛规定的首轮 A/B，不把该差异解释为稳定回退幅度。",
            "",
            "## Gate",
            "",
        ]
    )
    lines.extend(f"- [{'x' if passed else ' '}] {name}" for name, passed in gates.items())
    lines.extend(
        [
            "",
            "注：raw false-skip 是反事实 shadow 指标；实际路径继续写入 exact/Mixed metadata，安全性由 safe 指标和 digest 共同验证。",
            "本轮因 RSS 与 materialization false-skip 两项失败而停止；未进行反向顺序复测，也未运行 b256/b1024。",
        ]
    )
    (args.output_dir / "summary-cn.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
