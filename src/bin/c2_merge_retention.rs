// C2 semantic merge retention smoke runner.
//
// Builds N exact (src_label, edge_type) L1 segments, then runs an L1->L2
// compaction under two policies — naive (size-only split) vs semantic
// ((src_label, edge_type) split) — and reports, in one table, the pruning-surface
// retention (headline) together with the write/rewrite cost, plus a before/after
// query workload and a correctness check. This validates the Stage 3 measurement
// chain end to end at SF1-class scale before the SF30 run.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use anyhow::{ensure, Result};
use clap::Parser;
use lsmgraph::config::LsmGraphConfig;
use lsmgraph::csr::format::CsrSegmentMeta;
use lsmgraph::graph::{Engine, LevelMergePolicy};
use lsmgraph::types::{
    EdgeType, LevelId, VertexId, VertexLabel, MIXED_EDGE_TYPE, UNKNOWN_SOURCE_LABEL,
};
use lsmgraph::GraphAccessSignature;
use serde::Serialize;
use serde_json::{json, Value};

#[derive(Parser, Debug)]
#[command(
    name = "c2-merge-retention",
    about = "C2 semantic merge retention smoke runner: naive vs semantic L1->L2 merge"
)]
struct Cli {
    #[arg(long, default_value = "target/c2-merge-retention-store")]
    store_dir: PathBuf,
    #[arg(long)]
    output: Option<PathBuf>,
    #[arg(long, default_value_t = 500)]
    sources: u64,
    #[arg(long, value_delimiter = ',', default_value = "101,102,103,104")]
    edge_types: Vec<EdgeType>,
    /// Open an existing Engine store (e.g. an imported LDBC SF30 store) and run a
    /// single policy's L1->L2 merge on it, instead of the synthetic two-policy run.
    #[arg(long)]
    open: Option<PathBuf>,
    /// Policy for --open mode: "naive" or "semantic".
    #[arg(long, default_value = "semantic")]
    policy: String,
    /// Before merging, drain live L0 into one exact L1 segment per
    /// (src_label, edge_type) partition (for stores imported without compaction).
    #[arg(long, default_value_t = false)]
    build_l1_from_l0: bool,
}

#[derive(Debug, Serialize)]
struct PolicyRow {
    policy: &'static str,
    semantic_partition_outputs: bool,
    decisions: usize,
    input_segments: usize,
    output_segments: usize,
    input_bytes: u64,
    output_bytes: u64,
    rewrite_bytes: u64,
    logical_update_bytes: u64,
    write_amp: f64,
    exact_surface_ratio_before: f64,
    exact_surface_ratio_after: f64,
    mixed_ratio_before: f64,
    mixed_ratio_after: f64,
    pruning_retention: f64,
    read_bytes_before: u64,
    read_bytes_after: u64,
    filter_passed_before: u64,
    filter_passed_after: u64,
    correctness_mismatches: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    read_amp_proxy: Option<ReadAmpProxy>,
}

#[derive(Debug, Clone, Default)]
struct PartitionStats {
    segments: usize,
    bytes: u64,
    edges: u64,
}

#[derive(Debug, Serialize)]
struct ReadAmpProxyRow {
    src_label: i32,
    edge_type: EdgeType,
    exact_segments_before: usize,
    exact_bytes_before: u64,
    exact_edges_before: u64,
    candidate_segments_after: usize,
    candidate_bytes_after: u64,
    pruned_segments_after: usize,
    mixed_candidate_segments_after: usize,
    unknown_candidate_segments_after: usize,
    read_amp_vs_exact_bytes: f64,
}

#[derive(Debug, Serialize)]
struct ReadAmpProxy {
    kind: &'static str,
    output_level: LevelId,
    query_count: usize,
    output_segments: usize,
    output_bytes: u64,
    exact_bytes_before: u64,
    candidate_segments_total: u64,
    candidate_bytes_total: u64,
    pruned_segments_total: u64,
    exact_candidate_segments_total: u64,
    mixed_candidate_segments_total: u64,
    unknown_candidate_segments_total: u64,
    avg_candidate_segments_per_query: f64,
    avg_candidate_bytes_per_query: f64,
    weighted_read_amp_vs_exact_bytes: f64,
    max_read_amp_vs_exact_bytes: f64,
    rows: Vec<ReadAmpProxyRow>,
}

fn encoded(label: VertexLabel, local: u64) -> VertexId {
    ((label as u64) << 56) | local
}
fn encoded_label(label: i32, local: u64) -> VertexId {
    ((label.max(0) as u64) << 56) | local
}
fn metric_u64(m: &Value, section: &str, name: &str) -> u64 {
    m[section][name].as_u64().unwrap_or_default()
}
fn metric_delta(before: &Value, after: &Value, section: &str, name: &str) -> u64 {
    metric_u64(after, section, name).saturating_sub(metric_u64(before, section, name))
}

/// Build one exact (Person, edge_type) L1 segment per edge type.
async fn build_exact_l1_segments(
    engine: &Arc<Engine>,
    sources: u64,
    edge_types: &[EdgeType],
) -> Result<()> {
    let label = VertexLabel::Person;
    for edge_type in edge_types.iter().copied() {
        for s in 0..sources {
            let src = encoded(label, 1_000 + s);
            let dst = encoded(
                VertexLabel::Comment,
                1_000_000 + (edge_type as u64) * 1_000_000 + s,
            );
            engine.insert_edge(src, dst, edge_type).await?;
        }
        engine.flush_active().await?;
        engine
            .compact_l0_partition_to_l1(label as i32, Some(edge_type))
            .await?;
    }
    Ok(())
}

type NeighborResults = BTreeMap<(u64, EdgeType), Vec<VertexId>>;

/// Run the typed-neighbor workload; return (metrics_before, metrics_after, results).
async fn query_workload(
    engine: &Arc<Engine>,
    sources: u64,
    edge_types: &[EdgeType],
) -> Result<(Value, Value, NeighborResults)> {
    let label = VertexLabel::Person;
    let before = engine.metrics().snapshot_json();
    let snapshot = engine.current_snapshot();
    let mut results: NeighborResults = BTreeMap::new();
    for s in 0..sources {
        for edge_type in edge_types.iter().copied() {
            let src = encoded(label, 1_000 + s);
            let neighbors = engine.get_neighbors_typed(src, edge_type, snapshot).await?;
            let mut dsts: Vec<VertexId> = neighbors.iter().map(|e| e.dst).collect();
            dsts.sort_unstable();
            results.insert((s, edge_type), dsts);
        }
    }
    let after = engine.metrics().snapshot_json();
    Ok((before, after, results))
}

async fn run_policy(
    name: &'static str,
    semantic: bool,
    base_dir: &Path,
    sources: u64,
    edge_types: &[EdgeType],
) -> Result<PolicyRow> {
    let dir = base_dir.join(name);
    if dir.exists() {
        fs::remove_dir_all(&dir)?;
    }
    fs::create_dir_all(&dir)?;
    let config = LsmGraphConfig::new(&dir).with_memgraph_capacity(64 * 1024 * 1024);
    let engine = Engine::create(config).await?;

    build_exact_l1_segments(&engine, sources, edge_types).await?;

    // Workload BEFORE the merge.
    engine.metrics().reset();
    let (b0, b1, results_before) = query_workload(&engine, sources, edge_types).await?;
    let read_bytes_before = metric_delta(&b0, &b1, "io", "read_bytes");
    let filter_passed_before = metric_delta(&b0, &b1, "csr", "filter_passed_segments");

    // The merge under test.
    let decisions = engine
        .compact_levels_with_policy(LevelMergePolicy {
            fanout: 2,
            min_input_segments: 2,
            max_output_level: 2,
            semantic_partition_outputs: semantic,
        })
        .await?;
    ensure!(
        !decisions.is_empty(),
        "expected at least one L1->L2 compaction for policy {name}"
    );

    let (mut input_segments, mut output_segments) = (0usize, 0usize);
    let (mut input_bytes, mut output_bytes, mut logical_update_bytes) = (0u64, 0u64, 0u64);
    let (mut exact_before, mut exact_after) = (0u64, 0u64);
    let (mut mixed_before, mut mixed_after) = (0u64, 0u64);
    let (mut edges_before, mut edges_after) = (0u64, 0u64);
    for d in &decisions {
        input_segments += d.input_segments;
        output_segments += d.output_segments;
        input_bytes += d.input_bytes;
        output_bytes += d.output_bytes;
        logical_update_bytes += d.logical_update_bytes;
        exact_before += d.surface_before.topology_exact_edges;
        exact_after += d.surface_after.topology_exact_edges;
        mixed_before += d.surface_before.topology_mixed_edges;
        mixed_after += d.surface_after.topology_mixed_edges;
        edges_before += d.surface_before.edge_count;
        edges_after += d.surface_after.edge_count;
    }
    let ratio = |num: u64, den: u64| if den == 0 { 0.0 } else { num as f64 / den as f64 };
    let exact_surface_ratio_before = ratio(exact_before, edges_before);
    let exact_surface_ratio_after = ratio(exact_after, edges_after);
    let pruning_retention = if exact_surface_ratio_before == 0.0 {
        exact_surface_ratio_after
    } else {
        exact_surface_ratio_after / exact_surface_ratio_before
    };
    let write_amp = if logical_update_bytes == 0 {
        0.0
    } else {
        output_bytes as f64 / logical_update_bytes as f64
    };

    // Workload AFTER the merge.
    engine.metrics().reset();
    let (a0, a1, results_after) = query_workload(&engine, sources, edge_types).await?;
    let read_bytes_after = metric_delta(&a0, &a1, "io", "read_bytes");
    let filter_passed_after = metric_delta(&a0, &a1, "csr", "filter_passed_segments");

    let mut correctness_mismatches = 0u64;
    for (key, before) in &results_before {
        match results_after.get(key) {
            Some(after) if after == before => {}
            _ => correctness_mismatches += 1,
        }
    }

    Ok(PolicyRow {
        policy: name,
        semantic_partition_outputs: semantic,
        decisions: decisions.len(),
        input_segments,
        output_segments,
        input_bytes,
        output_bytes,
        rewrite_bytes: output_bytes,
        logical_update_bytes,
        write_amp,
        exact_surface_ratio_before,
        exact_surface_ratio_after,
        mixed_ratio_before: ratio(mixed_before, edges_before),
        mixed_ratio_after: ratio(mixed_after, edges_after),
        pruning_retention,
        read_bytes_before,
        read_bytes_after,
        filter_passed_before,
        filter_passed_after,
        correctness_mismatches,
        read_amp_proxy: None,
    })
}

fn collect_level_segments(engine: &Arc<Engine>, level: LevelId) -> Vec<CsrSegmentMeta> {
    let guard = engine.version_guard();
    guard
        .version()
        .levels
        .get(level as usize)
        .cloned()
        .unwrap_or_default()
}

fn collect_exact_partition_stats(
    engine: &Arc<Engine>,
    level: LevelId,
) -> BTreeMap<(i32, EdgeType), PartitionStats> {
    let mut out: BTreeMap<(i32, EdgeType), PartitionStats> = BTreeMap::new();
    for meta in collect_level_segments(engine, level) {
        if meta.src_label == UNKNOWN_SOURCE_LABEL || meta.edge_type_partition == MIXED_EDGE_TYPE {
            continue;
        }
        let entry = out
            .entry((meta.src_label, meta.edge_type_partition))
            .or_default();
        entry.segments += 1;
        entry.bytes = entry.bytes.saturating_add(meta.segment_bytes);
        entry.edges = entry.edges.saturating_add(meta.edge_count);
    }
    out
}

fn build_read_amp_proxy(
    output_level: LevelId,
    exact_before: &BTreeMap<(i32, EdgeType), PartitionStats>,
    output_segments: &[CsrSegmentMeta],
) -> Option<ReadAmpProxy> {
    if exact_before.is_empty() || output_segments.is_empty() {
        return None;
    }

    let mut rows = Vec::with_capacity(exact_before.len());
    let mut candidate_segments_total = 0u64;
    let mut candidate_bytes_total = 0u64;
    let mut pruned_segments_total = 0u64;
    let mut exact_candidate_segments_total = 0u64;
    let mut mixed_candidate_segments_total = 0u64;
    let mut unknown_candidate_segments_total = 0u64;
    let mut exact_bytes_before_total = 0u64;
    let mut max_read_amp_vs_exact_bytes = 0.0f64;

    for ((src_label, edge_type), before) in exact_before {
        let signature =
            GraphAccessSignature::neighbor_scan(encoded_label(*src_label, 1), Some(*edge_type));
        let mut candidate_segments_after = 0usize;
        let mut candidate_bytes_after = 0u64;
        let mut pruned_segments_after = 0usize;
        let mut mixed_candidate_segments_after = 0usize;
        let mut unknown_candidate_segments_after = 0usize;

        for meta in output_segments {
            let decision = meta.signature_pruning_decision(&signature);
            if decision.pruned {
                pruned_segments_after += 1;
                continue;
            }
            candidate_segments_after += 1;
            candidate_bytes_after = candidate_bytes_after.saturating_add(meta.segment_bytes);
            if decision.reason == "mixed_unknown_fallback" {
                unknown_candidate_segments_after += 1;
            } else if meta.src_label == UNKNOWN_SOURCE_LABEL
                || meta.edge_type_partition == MIXED_EDGE_TYPE
            {
                mixed_candidate_segments_after += 1;
            }
        }

        let read_amp_vs_exact_bytes = if before.bytes == 0 {
            0.0
        } else {
            candidate_bytes_after as f64 / before.bytes as f64
        };
        max_read_amp_vs_exact_bytes = max_read_amp_vs_exact_bytes.max(read_amp_vs_exact_bytes);
        exact_bytes_before_total = exact_bytes_before_total.saturating_add(before.bytes);
        candidate_segments_total =
            candidate_segments_total.saturating_add(candidate_segments_after as u64);
        candidate_bytes_total = candidate_bytes_total.saturating_add(candidate_bytes_after);
        pruned_segments_total = pruned_segments_total.saturating_add(pruned_segments_after as u64);
        mixed_candidate_segments_total = mixed_candidate_segments_total
            .saturating_add(mixed_candidate_segments_after as u64);
        unknown_candidate_segments_total = unknown_candidate_segments_total
            .saturating_add(unknown_candidate_segments_after as u64);
        exact_candidate_segments_total = exact_candidate_segments_total.saturating_add(
            candidate_segments_after
                .saturating_sub(mixed_candidate_segments_after)
                .saturating_sub(unknown_candidate_segments_after) as u64,
        );

        rows.push(ReadAmpProxyRow {
            src_label: *src_label,
            edge_type: *edge_type,
            exact_segments_before: before.segments,
            exact_bytes_before: before.bytes,
            exact_edges_before: before.edges,
            candidate_segments_after,
            candidate_bytes_after,
            pruned_segments_after,
            mixed_candidate_segments_after,
            unknown_candidate_segments_after,
            read_amp_vs_exact_bytes,
        });
    }

    let query_count = rows.len();
    let output_bytes = output_segments
        .iter()
        .fold(0u64, |acc, meta| acc.saturating_add(meta.segment_bytes));
    let denom = query_count.max(1) as f64;
    let weighted_read_amp_vs_exact_bytes = if exact_bytes_before_total == 0 {
        0.0
    } else {
        candidate_bytes_total as f64 / exact_bytes_before_total as f64
    };

    Some(ReadAmpProxy {
        kind: "metadata_typed_neighbor_partition_replay",
        output_level,
        query_count,
        output_segments: output_segments.len(),
        output_bytes,
        exact_bytes_before: exact_bytes_before_total,
        candidate_segments_total,
        candidate_bytes_total,
        pruned_segments_total,
        exact_candidate_segments_total,
        mixed_candidate_segments_total,
        unknown_candidate_segments_total,
        avg_candidate_segments_per_query: candidate_segments_total as f64 / denom,
        avg_candidate_bytes_per_query: candidate_bytes_total as f64 / denom,
        weighted_read_amp_vs_exact_bytes,
        max_read_amp_vs_exact_bytes,
        rows,
    })
}

/// Open an existing Engine store and run one policy's L1->L2 merge; report
/// surface + cost from the decisions. Correctness of compaction is covered by the
/// engine test suite (lossless merge), so open mode does not re-run a workload.
async fn run_open(semantic: bool, store_dir: &Path, build_l1: bool) -> Result<PolicyRow> {
    let config = LsmGraphConfig::new(store_dir);
    let engine = Engine::open(config).await?;
    if build_l1 {
        // Drain live L0 into one exact L1 segment per (src_label, edge_type)
        // partition, recreating the controlled multi-L1 state on real data.
        let partitions: BTreeSet<(i32, EdgeType)> = {
            let guard = engine.version_guard();
            let l0 = guard.version().levels.get(0).cloned().unwrap_or_default();
            l0.iter()
                .filter(|m| {
                    m.src_label != UNKNOWN_SOURCE_LABEL && m.edge_type_partition != MIXED_EDGE_TYPE
                })
                .map(|m| (m.src_label, m.edge_type_partition))
                .collect()
        };
        eprintln!(
            "[build-l1] draining {} (src_label, edge_type) L0 partitions -> exact L1",
            partitions.len()
        );
        for (label, edge_type) in &partitions {
            engine
                .compact_l0_partition_to_l1(*label, Some(*edge_type))
                .await?;
        }
    }
    let exact_partitions_before = collect_exact_partition_stats(&engine, 1);
    let decisions = engine
        .compact_levels_with_policy(LevelMergePolicy {
            fanout: 2,
            min_input_segments: 2,
            max_output_level: 2,
            semantic_partition_outputs: semantic,
        })
        .await?;
    ensure!(
        !decisions.is_empty(),
        "no L1->L2 compaction triggered on {} (need >=2 L1 segments)",
        store_dir.display()
    );
    let (mut input_segments, mut output_segments) = (0usize, 0usize);
    let (mut input_bytes, mut output_bytes, mut logical_update_bytes) = (0u64, 0u64, 0u64);
    let (mut exact_before, mut exact_after) = (0u64, 0u64);
    let (mut mixed_before, mut mixed_after) = (0u64, 0u64);
    let (mut edges_before, mut edges_after) = (0u64, 0u64);
    for d in &decisions {
        input_segments += d.input_segments;
        output_segments += d.output_segments;
        input_bytes += d.input_bytes;
        output_bytes += d.output_bytes;
        logical_update_bytes += d.logical_update_bytes;
        exact_before += d.surface_before.topology_exact_edges;
        exact_after += d.surface_after.topology_exact_edges;
        mixed_before += d.surface_before.topology_mixed_edges;
        mixed_after += d.surface_after.topology_mixed_edges;
        edges_before += d.surface_before.edge_count;
        edges_after += d.surface_after.edge_count;
    }
    let output_level = decisions
        .iter()
        .map(|d| d.target_level)
        .max()
        .unwrap_or(2);
    let output_segments_after = collect_level_segments(&engine, output_level);
    let read_amp_proxy =
        build_read_amp_proxy(output_level, &exact_partitions_before, &output_segments_after);
    let ratio = |num: u64, den: u64| if den == 0 { 0.0 } else { num as f64 / den as f64 };
    let exact_surface_ratio_before = ratio(exact_before, edges_before);
    let exact_surface_ratio_after = ratio(exact_after, edges_after);
    let pruning_retention = if exact_surface_ratio_before == 0.0 {
        exact_surface_ratio_after
    } else {
        exact_surface_ratio_after / exact_surface_ratio_before
    };
    let write_amp = if logical_update_bytes == 0 {
        0.0
    } else {
        output_bytes as f64 / logical_update_bytes as f64
    };
    Ok(PolicyRow {
        policy: if semantic { "semantic" } else { "naive" },
        semantic_partition_outputs: semantic,
        decisions: decisions.len(),
        input_segments,
        output_segments,
        input_bytes,
        output_bytes,
        rewrite_bytes: output_bytes,
        logical_update_bytes,
        write_amp,
        exact_surface_ratio_before,
        exact_surface_ratio_after,
        mixed_ratio_before: ratio(mixed_before, edges_before),
        mixed_ratio_after: ratio(mixed_after, edges_after),
        pruning_retention,
        read_bytes_before: 0,
        read_bytes_after: 0,
        filter_passed_before: 0,
        filter_passed_after: 0,
        correctness_mismatches: 0,
        read_amp_proxy,
    })
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();

    if let Some(open) = cli.open.clone() {
        let semantic = match cli.policy.as_str() {
            "semantic" => true,
            "naive" => false,
            other => anyhow::bail!("--policy must be naive|semantic, got {other}"),
        };
        let row = run_open(semantic, &open, cli.build_l1_from_l0).await?;
        let report = json!({
            "runner": "c2-merge-retention",
            "mode": "open",
            "store": open,
            "rows": [&row],
        });
        let text = serde_json::to_string_pretty(&report)?;
        if let Some(path) = &cli.output {
            fs::write(path, &text)?;
        }
        println!("{text}");
        eprintln!(
            "C2 open[{}]: exact_after={:.3} retention={:.3} output_segments={} write_amp={:.3} input_segments={} proxy_amp={}",
            row.policy,
            row.exact_surface_ratio_after,
            row.pruning_retention,
            row.output_segments,
            row.write_amp,
            row.input_segments,
            row.read_amp_proxy
                .as_ref()
                .map(|p| format!("{:.3}", p.weighted_read_amp_vs_exact_bytes))
                .unwrap_or_else(|| "n/a".to_string()),
        );
        return Ok(());
    }

    fs::create_dir_all(&cli.store_dir)?;

    let naive = run_policy("naive", false, &cli.store_dir, cli.sources, &cli.edge_types).await?;
    let semantic =
        run_policy("semantic", true, &cli.store_dir, cli.sources, &cli.edge_types).await?;

    let report = json!({
        "runner": "c2-merge-retention",
        "sources": cli.sources,
        "edge_types": cli.edge_types,
        "rows": [&naive, &semantic],
    });
    let text = serde_json::to_string_pretty(&report)?;
    if let Some(path) = &cli.output {
        fs::write(path, &text)?;
    }
    println!("{text}");

    eprintln!(
        "C2 smoke: naive exact_after={:.3} mixed_after={:.3} retention={:.3} | semantic exact_after={:.3} retention={:.3} | mismatches naive={} semantic={}",
        naive.exact_surface_ratio_after,
        naive.mixed_ratio_after,
        naive.pruning_retention,
        semantic.exact_surface_ratio_after,
        semantic.pruning_retention,
        naive.correctness_mismatches,
        semantic.correctness_mismatches,
    );
    Ok(())
}
