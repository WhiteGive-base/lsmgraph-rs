use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Instant;

use anyhow::{ensure, Context, Result};
use clap::Parser;
use lsmgraph::config::{L0LayoutPolicy, LsmGraphConfig};
use lsmgraph::graph::{Engine, L0CompactionDecision};
use lsmgraph::types::{EdgeType, VertexId, VERTEX_LABEL_SHIFT};
use serde::Serialize;
use serde_json::{json, Value};

#[derive(Parser, Debug)]
#[command(
    name = "w7-sf30-workload-shift",
    about = "W7 real-SF30-derived workload-shift formal runner"
)]
struct Cli {
    #[arg(long, default_value = "data/social_network")]
    input: PathBuf,
    #[arg(long, default_value = "target/w7-sf30-workload-shift-store")]
    store_dir: PathBuf,
    #[arg(long)]
    output: Option<PathBuf>,
    #[arg(long, default_value_t = false)]
    reset_store: bool,
    #[arg(long, default_value_t = 4)]
    phase_flushes: usize,
    #[arg(long, default_value_t = 16)]
    hot_sources_per_phase: usize,
    #[arg(long, default_value_t = 1)]
    edges_per_source_per_flush: usize,
    #[arg(long, default_value_t = 4)]
    queries_per_source: usize,
    #[arg(long, default_value_t = 500_000)]
    max_scan_rows_per_phase: usize,
    #[arg(long, default_value = "dynamic/person_knows_person_0_0.csv")]
    phase_a_file: PathBuf,
    #[arg(long, default_value_t = 1)]
    phase_a_edge_type: EdgeType,
    #[arg(long, default_value_t = 1)]
    phase_a_src_label: i32,
    #[arg(long, default_value_t = 1)]
    phase_a_dst_label: i32,
    #[arg(long, default_value = "dynamic/person_likes_post_0_0.csv")]
    phase_b_file: PathBuf,
    #[arg(long, default_value_t = 8)]
    phase_b_edge_type: EdgeType,
    #[arg(long, default_value_t = 1)]
    phase_b_src_label: i32,
    #[arg(long, default_value_t = 3)]
    phase_b_dst_label: i32,
    #[arg(long, default_value_t = 128 * 1024)]
    semantic_budget_min_edge_type_bytes: usize,
    #[arg(long, default_value_t = 0.20)]
    semantic_budget_min_edge_type_score: f64,
    #[arg(long, default_value_t = true)]
    compact_after_query: bool,
}

#[derive(Debug, Clone, Copy)]
struct VariantConfig {
    name: &'static str,
    feedback_only: bool,
    disable_feedback: bool,
}

#[derive(Debug, Clone, Serialize)]
struct EdgeSample {
    src: VertexId,
    dst: VertexId,
}

#[derive(Debug, Serialize)]
struct PhaseWorkload {
    name: &'static str,
    source_file: String,
    src_label: i32,
    dst_label: i32,
    edge_type: EdgeType,
    scanned_rows: usize,
    hot_sources: Vec<VertexId>,
    batches: Vec<Vec<EdgeSample>>,
}

#[derive(Debug, Serialize)]
struct QueryDelta {
    elapsed_ms: f64,
    queries: usize,
    neighbors_total: usize,
    candidate_l0_segments: u64,
    filter_passed_segments: u64,
    matched_l0_segments: u64,
    csr_body_reads: u64,
    csr_offset_reads: u64,
    io_read_bytes: u64,
}

#[derive(Debug, Default, Serialize)]
struct PartitionTelemetrySummary {
    partitions: usize,
    query_count: u64,
    candidate_segments: u64,
    avg_candidate_segments: f64,
    body_reads: u64,
    offset_cache_hits: u64,
    offset_cache_misses: u64,
    offset_cache_miss_rate: f64,
}

#[derive(Debug, Serialize)]
struct BudgetCandidateRow {
    flush_snapshot: u64,
    src_label: i32,
    edge_type: EdgeType,
    group_edges: usize,
    group_sources: usize,
    group_bytes: usize,
    estimated_exact_files: usize,
    estimated_merged_files: usize,
    query_weight: f64,
    score: f64,
    selected: bool,
    reason: String,
    configured_query_weight: f64,
    feedback_query_weight: f64,
    feedback_only: bool,
    feedback_disabled: bool,
}

#[derive(Debug, Default, Serialize)]
struct BudgetSummary {
    active_rows: usize,
    selected_rows: usize,
    selected_group_bytes: usize,
    max_score: f64,
    max_feedback_query_weight: f64,
    benefit_per_rewrite_mib: f64,
}

#[derive(Debug, Serialize)]
struct CompactionDecisionSummary {
    src_label: i32,
    edge_type: EdgeType,
    range_start: VertexId,
    range_end: VertexId,
    selected_hot_partition: bool,
    score: f64,
    estimated_rewrite_bytes: u64,
    selected_l0_segments: usize,
    query_count: u64,
    avg_candidate_segments: f64,
    offset_cache_miss_rate: f64,
    output_segments: usize,
    output_edges: u64,
}

#[derive(Debug, Serialize)]
struct CompactionMetricsSummary {
    elapsed_ms: f64,
    compaction_count: u64,
    compaction_latency_sum_us: u64,
    compaction_input_bytes: u64,
    compaction_output_bytes: u64,
    io_read_bytes: u64,
    io_write_bytes: u64,
    write_amplification: f64,
}

#[derive(Debug, Serialize)]
struct CompactionReport {
    decision: Option<CompactionDecisionSummary>,
    metrics: CompactionMetricsSummary,
}

#[derive(Debug, Serialize)]
struct FlushReport {
    phase: &'static str,
    flush_in_phase: usize,
    snapshot: u64,
    inserted_edges: usize,
    expected_neighbors_per_hot_source: usize,
    query_before_compaction: QueryDelta,
    active_partition_telemetry: PartitionTelemetrySummary,
    budget_candidates: Vec<BudgetCandidateRow>,
    active_budget: BudgetSummary,
    compaction: Option<CompactionReport>,
    query_after_compaction: Option<QueryDelta>,
    normalized_reduction: Value,
    levels: Vec<usize>,
}

#[derive(Debug, Serialize)]
struct PhaseSummary {
    first_selected_flush: Option<usize>,
    first_feedback_weight_flush: Option<usize>,
    first_hot_compaction_flush: Option<usize>,
    last_candidate_l0_per_query_before: f64,
    last_candidate_l0_per_query_after: Option<f64>,
    total_compaction_input_bytes: u64,
    total_compaction_output_bytes: u64,
    total_io_write_bytes: u64,
}

#[derive(Debug, Serialize)]
struct VariantSummary {
    phase_a: PhaseSummary,
    phase_b: PhaseSummary,
    selected_ranges_changed: bool,
}

#[derive(Debug, Serialize)]
struct VariantReport {
    variant: &'static str,
    store_dir: String,
    config: Value,
    flushes: Vec<FlushReport>,
    summary: VariantSummary,
    metrics_final: Value,
}

#[tokio::main(flavor = "multi_thread")]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    ensure!(cli.phase_flushes >= 2, "--phase-flushes must be at least 2");
    ensure!(
        cli.hot_sources_per_phase >= 1,
        "--hot-sources-per-phase must be positive"
    );
    ensure!(
        cli.edges_per_source_per_flush >= 1,
        "--edges-per-source-per-flush must be positive"
    );
    ensure!(
        cli.queries_per_source >= 1,
        "--queries-per-source must be positive"
    );

    let phase_a = load_phase_workload(
        &cli,
        "A",
        &cli.phase_a_file,
        cli.phase_a_src_label,
        cli.phase_a_dst_label,
        cli.phase_a_edge_type,
    )?;
    let phase_b = load_phase_workload(
        &cli,
        "B",
        &cli.phase_b_file,
        cli.phase_b_src_label,
        cli.phase_b_dst_label,
        cli.phase_b_edge_type,
    )?;

    let variants = [
        VariantConfig {
            name: "feedback-only",
            feedback_only: true,
            disable_feedback: false,
        },
        VariantConfig {
            name: "static-budgeted",
            feedback_only: false,
            disable_feedback: false,
        },
        VariantConfig {
            name: "no-feedback",
            feedback_only: false,
            disable_feedback: true,
        },
    ];

    let mut reports = Vec::new();
    for variant in variants {
        let report = run_variant(&cli, variant, &phase_a, &phase_b).await?;
        eprintln!(
            "[w7-sf30-workload-shift] completed variant={} flushes={}",
            report.variant,
            report.flushes.len()
        );
        reports.push(report);
    }

    let result = json!({
        "runner": "w7-sf30-workload-shift",
        "scale": "real-sf30-derived",
        "input": cli.input,
        "phase_flushes": cli.phase_flushes,
        "hot_sources_per_phase": cli.hot_sources_per_phase,
        "edges_per_source_per_flush": cli.edges_per_source_per_flush,
        "queries_per_source": cli.queries_per_source,
        "max_scan_rows_per_phase": cli.max_scan_rows_per_phase,
        "compact_after_query": cli.compact_after_query,
        "phase_a": phase_a,
        "phase_b": phase_b,
        "variants": reports,
    });

    let pretty = serde_json::to_string_pretty(&result)?;
    if let Some(output) = &cli.output {
        if let Some(parent) = output.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(output, &pretty)?;
    }
    println!("{pretty}");
    Ok(())
}

fn load_phase_workload(
    cli: &Cli,
    name: &'static str,
    file: &Path,
    src_label: i32,
    dst_label: i32,
    edge_type: EdgeType,
) -> Result<PhaseWorkload> {
    let path = cli.input.join(file);
    let required_edges = cli.phase_flushes * cli.edges_per_source_per_flush;
    let mut rdr = csv::ReaderBuilder::new()
        .delimiter(b'|')
        .has_headers(true)
        .from_path(&path)
        .with_context(|| format!("open {}", path.display()))?;
    let mut by_src: HashMap<VertexId, Vec<VertexId>> = HashMap::new();
    let mut scanned_rows = 0usize;
    for rec in rdr.records() {
        if scanned_rows >= cli.max_scan_rows_per_phase {
            break;
        }
        let rec = rec?;
        scanned_rows += 1;
        let Some(src_raw) = rec.get(0) else { continue };
        let Some(dst_raw) = rec.get(1) else { continue };
        let Ok(src_ext) = src_raw.parse::<u64>() else {
            continue;
        };
        let Ok(dst_ext) = dst_raw.parse::<u64>() else {
            continue;
        };
        let src = encode_vid(src_label, src_ext);
        let dst = encode_vid(dst_label, dst_ext);
        let entry = by_src.entry(src).or_default();
        if entry.len() < required_edges && !entry.contains(&dst) {
            entry.push(dst);
        }
        if scanned_rows % 1000 == 0
            && by_src
                .values()
                .filter(|edges| edges.len() >= required_edges)
                .count()
                >= cli.hot_sources_per_phase
        {
            break;
        }
    }

    let mut eligible: Vec<(VertexId, Vec<VertexId>)> = by_src
        .into_iter()
        .filter(|(_, edges)| edges.len() >= required_edges)
        .collect();
    eligible.sort_by_key(|(src, _)| *src);
    ensure!(
        eligible.len() >= cli.hot_sources_per_phase,
        "not enough hot sources in {}: need {}, found {} with >= {} edges after scanning {} rows",
        path.display(),
        cli.hot_sources_per_phase,
        eligible.len(),
        required_edges,
        scanned_rows
    );
    eligible.truncate(cli.hot_sources_per_phase);

    let hot_sources: Vec<VertexId> = eligible.iter().map(|(src, _)| *src).collect();
    let mut batches = Vec::with_capacity(cli.phase_flushes);
    for flush in 0..cli.phase_flushes {
        let mut batch = Vec::with_capacity(cli.hot_sources_per_phase * cli.edges_per_source_per_flush);
        for (src, edges) in &eligible {
            let start = flush * cli.edges_per_source_per_flush;
            for dst in &edges[start..start + cli.edges_per_source_per_flush] {
                batch.push(EdgeSample { src: *src, dst: *dst });
            }
        }
        batches.push(batch);
    }

    Ok(PhaseWorkload {
        name,
        source_file: file.display().to_string(),
        src_label,
        dst_label,
        edge_type,
        scanned_rows,
        hot_sources,
        batches,
    })
}

async fn run_variant(
    cli: &Cli,
    variant: VariantConfig,
    phase_a: &PhaseWorkload,
    phase_b: &PhaseWorkload,
) -> Result<VariantReport> {
    let store_dir = cli.store_dir.join(variant.name);
    prepare_store(&store_dir, cli.reset_store)?;
    let engine = create_engine(&store_dir, cli, variant).await?;

    let mut flushes = Vec::new();
    run_phase(&engine, &store_dir, cli, variant, phase_a, &mut flushes).await?;
    run_phase(&engine, &store_dir, cli, variant, phase_b, &mut flushes).await?;
    let summary = summarize_variant(&flushes);
    Ok(VariantReport {
        variant: variant.name,
        store_dir: store_dir.display().to_string(),
        config: json!({
            "l0_layout": "SemanticBudgeted",
            "semantic_budget_feedback_only": variant.feedback_only,
            "semantic_budget_disable_feedback": variant.disable_feedback,
            "semantic_budget_min_edge_type_bytes": cli.semantic_budget_min_edge_type_bytes,
            "semantic_budget_min_edge_type_score": cli.semantic_budget_min_edge_type_score,
            "compact_after_query": cli.compact_after_query && !variant.disable_feedback,
        }),
        flushes,
        summary,
        metrics_final: engine.metrics().snapshot_json(),
    })
}

async fn create_engine(store_dir: &Path, cli: &Cli, variant: VariantConfig) -> Result<Arc<Engine>> {
    let config = LsmGraphConfig::new(store_dir)
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(cli.semantic_budget_min_edge_type_bytes)
        .with_semantic_budget_min_edge_type_score(cli.semantic_budget_min_edge_type_score)
        .with_semantic_budget_core_edge_weight(4.0)
        .with_semantic_budget_reverse_core_edge_weight(2.0)
        .with_semantic_budget_other_edge_weight(0.5)
        .with_semantic_budget_min_exact_bytes(1)
        .with_semantic_budget_min_benefit_score(0.0)
        .with_semantic_budget_feedback_only(variant.feedback_only)
        .with_semantic_budget_disable_feedback(variant.disable_feedback);
    Engine::create(config).await
}

async fn run_phase(
    engine: &Arc<Engine>,
    store_dir: &Path,
    cli: &Cli,
    variant: VariantConfig,
    phase: &PhaseWorkload,
    reports: &mut Vec<FlushReport>,
) -> Result<()> {
    for (flush_in_phase, batch) in phase.batches.iter().enumerate() {
        for edge in batch {
            engine
                .insert_edge(edge.src, edge.dst, phase.edge_type)
                .await?;
        }
        engine.flush_active().await?;
        let snapshot = engine.current_snapshot();
        let budget_candidates = read_budget_candidates_for_snapshot(store_dir, snapshot)?;
        let expected_neighbors =
            (flush_in_phase + 1) * cli.edges_per_source_per_flush;
        let query_before_compaction =
            collect_phase_queries(engine, cli, phase, expected_neighbors).await?;
        let metrics_after_query = engine.metrics().snapshot_json();
        let active_partition_telemetry =
            summarize_partitions(&metrics_after_query, phase.src_label, phase.edge_type);
        let active_budget =
            summarize_budget(&budget_candidates, phase.src_label, phase.edge_type, &active_partition_telemetry);

        let mut compaction = None;
        let mut query_after_compaction = None;
        if cli.compact_after_query && !variant.disable_feedback {
            let before_compaction_metrics = engine.metrics().snapshot_json();
            let started = Instant::now();
            let decision = engine.compact_best_l0_partition_by_score().await?;
            let elapsed_ms = started.elapsed().as_secs_f64() * 1000.0;
            let after_compaction_metrics = engine.metrics().snapshot_json();
            let report = summarize_compaction(
                elapsed_ms,
                decision,
                before_compaction_metrics,
                after_compaction_metrics,
                phase,
            );
            compaction = Some(report);
            query_after_compaction =
                Some(collect_phase_queries(engine, cli, phase, expected_neighbors).await?);
        }

        let normalized_reduction =
            normalized_reduction(&query_before_compaction, query_after_compaction.as_ref());
        reports.push(FlushReport {
            phase: phase.name,
            flush_in_phase,
            snapshot,
            inserted_edges: batch.len(),
            expected_neighbors_per_hot_source: expected_neighbors,
            query_before_compaction,
            active_partition_telemetry,
            budget_candidates,
            active_budget,
            compaction,
            query_after_compaction,
            normalized_reduction,
            levels: engine.live_file_count_by_level(),
        });
    }
    Ok(())
}

async fn collect_phase_queries(
    engine: &Arc<Engine>,
    cli: &Cli,
    phase: &PhaseWorkload,
    expected_neighbors: usize,
) -> Result<QueryDelta> {
    let before = engine.metrics().snapshot_json();
    let started = Instant::now();
    let mut neighbors_total = 0usize;
    for src in &phase.hot_sources {
        for _ in 0..cli.queries_per_source {
            let neighbors = engine
                .get_neighbors_typed(*src, phase.edge_type, engine.current_snapshot())
                .await?;
            ensure!(
                neighbors.len() == expected_neighbors,
                "unexpected neighbors for phase {} src {} edge_type {}: got {}, expected {}",
                phase.name,
                src,
                phase.edge_type,
                neighbors.len(),
                expected_neighbors
            );
            neighbors_total += neighbors.len();
        }
    }
    let elapsed_ms = started.elapsed().as_secs_f64() * 1000.0;
    let after = engine.metrics().snapshot_json();
    Ok(QueryDelta {
        elapsed_ms,
        queries: phase.hot_sources.len() * cli.queries_per_source,
        neighbors_total,
        candidate_l0_segments: metric_delta(&before, &after, "csr", "candidate_l0_segments"),
        filter_passed_segments: metric_delta(&before, &after, "csr", "filter_passed_segments"),
        matched_l0_segments: metric_delta(&before, &after, "csr", "matched_l0_segments"),
        csr_body_reads: metric_delta(&before, &after, "csr", "body_reads"),
        csr_offset_reads: metric_delta(&before, &after, "csr", "offset_reads"),
        io_read_bytes: metric_delta(&before, &after, "io", "read_bytes"),
    })
}

fn summarize_partitions(metrics: &Value, src_label: i32, edge_type: EdgeType) -> PartitionTelemetrySummary {
    let mut out = PartitionTelemetrySummary::default();
    let Some(parts) = metrics["csr"]["l0_partitions"].as_array() else {
        return out;
    };
    for part in parts {
        if part["key"]["src_label"].as_i64() != Some(src_label as i64)
            || part["key"]["edge_type"].as_i64() != Some(edge_type as i64)
        {
            continue;
        }
        out.partitions += 1;
        out.query_count += value_u64(part, "query_count");
        out.candidate_segments += value_u64(part, "candidate_segments");
        out.body_reads += value_u64(part, "body_reads");
        out.offset_cache_hits += value_u64(part, "offset_cache_hits");
        out.offset_cache_misses += value_u64(part, "offset_cache_misses");
    }
    if out.query_count > 0 {
        out.avg_candidate_segments = out.candidate_segments as f64 / out.query_count as f64;
    }
    let offset_total = out.offset_cache_hits + out.offset_cache_misses;
    if offset_total > 0 {
        out.offset_cache_miss_rate = out.offset_cache_misses as f64 / offset_total as f64;
    }
    out
}

fn summarize_budget(
    rows: &[BudgetCandidateRow],
    src_label: i32,
    edge_type: EdgeType,
    telemetry: &PartitionTelemetrySummary,
) -> BudgetSummary {
    let mut out = BudgetSummary::default();
    for row in rows {
        if row.src_label != src_label || row.edge_type != edge_type {
            continue;
        }
        out.active_rows += 1;
        out.max_score = out.max_score.max(row.score);
        out.max_feedback_query_weight = out.max_feedback_query_weight.max(row.feedback_query_weight);
        if row.selected {
            out.selected_rows += 1;
            out.selected_group_bytes += row.group_bytes;
        }
    }
    let rewrite_mib = (out.selected_group_bytes as f64 / 1_048_576.0).max(1.0);
    out.benefit_per_rewrite_mib = telemetry.candidate_segments as f64 / rewrite_mib;
    out
}

fn summarize_compaction(
    elapsed_ms: f64,
    decision: Option<L0CompactionDecision>,
    before: Value,
    after: Value,
    phase: &PhaseWorkload,
) -> CompactionReport {
    let decision_summary = decision.map(|decision| {
        let selected_hot_partition = decision.key.src_label == phase.src_label
            && decision.key.edge_type == phase.edge_type
            && phase
                .hot_sources
                .iter()
                .any(|src| decision.key.range_start <= *src && *src <= decision.key.range_end);
        CompactionDecisionSummary {
            src_label: decision.key.src_label,
            edge_type: decision.key.edge_type,
            range_start: decision.key.range_start,
            range_end: decision.key.range_end,
            selected_hot_partition,
            score: decision.score,
            estimated_rewrite_bytes: decision.estimated_rewrite_bytes,
            selected_l0_segments: decision.selected_l0_segments,
            query_count: decision.query_count,
            avg_candidate_segments: decision.avg_candidate_segments,
            offset_cache_miss_rate: decision.offset_cache_miss_rate,
            output_segments: decision.outputs.len(),
            output_edges: decision.outputs.iter().map(|meta| meta.edge_count).sum(),
        }
    });
    let input_bytes = metric_path_delta(&before, &after, &["storage", "compaction_input_bytes"]);
    let output_bytes = metric_path_delta(&before, &after, &["storage", "compaction_output_bytes"]);
    let io_write_bytes = metric_path_delta(&before, &after, &["io", "write_bytes"]);
    CompactionReport {
        decision: decision_summary,
        metrics: CompactionMetricsSummary {
            elapsed_ms,
            compaction_count: metric_path_delta(&before, &after, &["storage", "compaction_count"]),
            compaction_latency_sum_us: metric_path_delta(
                &before,
                &after,
                &["storage", "compaction_latency", "sum_us"],
            ),
            compaction_input_bytes: input_bytes,
            compaction_output_bytes: output_bytes,
            io_read_bytes: metric_path_delta(&before, &after, &["io", "read_bytes"]),
            io_write_bytes,
            write_amplification: io_write_bytes as f64 / (input_bytes as f64).max(1.0),
        },
    }
}

fn normalized_reduction(before: &QueryDelta, after: Option<&QueryDelta>) -> Value {
    let Some(after) = after else {
        return json!(null);
    };
    let before_q = before.queries.max(1) as f64;
    let after_q = after.queries.max(1) as f64;
    let before_candidate = before.candidate_l0_segments as f64 / before_q;
    let after_candidate = after.candidate_l0_segments as f64 / after_q;
    let before_read = before.io_read_bytes as f64 / before_q;
    let after_read = after.io_read_bytes as f64 / after_q;
    let before_body = before.csr_body_reads as f64 / before_q;
    let after_body = after.csr_body_reads as f64 / after_q;
    json!({
        "candidate_l0_per_query_before": before_candidate,
        "candidate_l0_per_query_after": after_candidate,
        "candidate_l0_per_query_delta": after_candidate - before_candidate,
        "read_bytes_per_query_before": before_read,
        "read_bytes_per_query_after": after_read,
        "read_bytes_per_query_delta": after_read - before_read,
        "body_reads_per_query_before": before_body,
        "body_reads_per_query_after": after_body,
        "body_reads_per_query_delta": after_body - before_body
    })
}

fn summarize_variant(flushes: &[FlushReport]) -> VariantSummary {
    let phase_a = summarize_phase(flushes, "A");
    let phase_b = summarize_phase(flushes, "B");
    VariantSummary {
        selected_ranges_changed: phase_a.first_hot_compaction_flush.is_some()
            && phase_b.first_hot_compaction_flush.is_some(),
        phase_a,
        phase_b,
    }
}

fn summarize_phase(flushes: &[FlushReport], phase: &'static str) -> PhaseSummary {
    let phase_flushes: Vec<&FlushReport> = flushes.iter().filter(|flush| flush.phase == phase).collect();
    let first_selected_flush = phase_flushes
        .iter()
        .find(|flush| flush.active_budget.selected_rows > 0)
        .map(|flush| flush.flush_in_phase);
    let first_feedback_weight_flush = phase_flushes
        .iter()
        .find(|flush| flush.active_budget.max_feedback_query_weight > 0.0)
        .map(|flush| flush.flush_in_phase);
    let first_hot_compaction_flush = phase_flushes
        .iter()
        .find(|flush| {
            flush
                .compaction
                .as_ref()
                .and_then(|report| report.decision.as_ref())
                .map(|decision| decision.selected_hot_partition)
                .unwrap_or(false)
        })
        .map(|flush| flush.flush_in_phase);
    let last = phase_flushes.last();
    let last_candidate_l0_per_query_before = last
        .map(|flush| {
            flush.query_before_compaction.candidate_l0_segments as f64
                / flush.query_before_compaction.queries.max(1) as f64
        })
        .unwrap_or_default();
    let last_candidate_l0_per_query_after = last.and_then(|flush| {
        flush.query_after_compaction.as_ref().map(|query| {
            query.candidate_l0_segments as f64 / query.queries.max(1) as f64
        })
    });
    let mut total_compaction_input_bytes = 0;
    let mut total_compaction_output_bytes = 0;
    let mut total_io_write_bytes = 0;
    for flush in &phase_flushes {
        if let Some(report) = &flush.compaction {
            total_compaction_input_bytes += report.metrics.compaction_input_bytes;
            total_compaction_output_bytes += report.metrics.compaction_output_bytes;
            total_io_write_bytes += report.metrics.io_write_bytes;
        }
    }
    PhaseSummary {
        first_selected_flush,
        first_feedback_weight_flush,
        first_hot_compaction_flush,
        last_candidate_l0_per_query_before,
        last_candidate_l0_per_query_after,
        total_compaction_input_bytes,
        total_compaction_output_bytes,
        total_io_write_bytes,
    }
}

fn prepare_store(store_dir: &Path, reset_store: bool) -> Result<()> {
    if store_dir.exists() {
        ensure!(
            reset_store,
            "store already exists at {}; pass --reset-store to replace it",
            store_dir.display()
        );
        fs::remove_dir_all(store_dir).with_context(|| format!("remove {}", store_dir.display()))?;
    }
    fs::create_dir_all(store_dir).with_context(|| format!("create {}", store_dir.display()))?;
    Ok(())
}

fn read_budget_candidates_for_snapshot(
    store_dir: &Path,
    snapshot: u64,
) -> Result<Vec<BudgetCandidateRow>> {
    Ok(read_budget_candidates(store_dir)?
        .into_iter()
        .filter(|row| row.flush_snapshot == snapshot)
        .collect())
}

fn read_budget_candidates(store_dir: &Path) -> Result<Vec<BudgetCandidateRow>> {
    let path = store_dir.join("budgeted-edge-candidates.tsv");
    if !path.exists() {
        return Ok(Vec::new());
    }
    let text = fs::read_to_string(&path)?;
    text.lines()
        .skip(1)
        .filter(|line| !line.trim().is_empty())
        .map(parse_budget_candidate_row)
        .collect()
}

fn parse_budget_candidate_row(line: &str) -> Result<BudgetCandidateRow> {
    let cols: Vec<&str> = line.split('\t').collect();
    ensure!(
        cols.len() >= 12,
        "budget candidate row has too few columns: {line}"
    );
    Ok(BudgetCandidateRow {
        flush_snapshot: cols[0].parse()?,
        src_label: cols[1].parse()?,
        edge_type: cols[2].parse()?,
        group_edges: cols[3].parse()?,
        group_sources: cols[4].parse()?,
        group_bytes: cols[5].parse()?,
        estimated_exact_files: cols[6].parse()?,
        estimated_merged_files: cols[7].parse()?,
        query_weight: cols[8].parse()?,
        score: cols[9].parse()?,
        selected: cols[10].parse()?,
        reason: cols[11].to_string(),
        configured_query_weight: parse_optional_f64(&cols, 12),
        feedback_query_weight: parse_optional_f64(&cols, 13),
        feedback_only: parse_optional_bool(&cols, 14),
        feedback_disabled: parse_optional_bool(&cols, 15),
    })
}

fn parse_optional_f64(cols: &[&str], idx: usize) -> f64 {
    cols.get(idx)
        .and_then(|value| value.parse().ok())
        .unwrap_or_default()
}

fn parse_optional_bool(cols: &[&str], idx: usize) -> bool {
    cols.get(idx)
        .and_then(|value| value.parse().ok())
        .unwrap_or(false)
}

fn metric_delta(before: &Value, after: &Value, section: &str, name: &str) -> u64 {
    value_u64(&after[section], name).saturating_sub(value_u64(&before[section], name))
}

fn metric_path_delta(before: &Value, after: &Value, path: &[&str]) -> u64 {
    metric_path_u64(after, path).saturating_sub(metric_path_u64(before, path))
}

fn metric_path_u64(metrics: &Value, path: &[&str]) -> u64 {
    let mut current = metrics;
    for key in path {
        current = &current[*key];
    }
    current.as_u64().unwrap_or_default()
}

fn value_u64(value: &Value, key: &str) -> u64 {
    value[key].as_u64().unwrap_or_default()
}

fn encode_vid(label: i32, external_id: u64) -> VertexId {
    ((label as u64) << VERTEX_LABEL_SHIFT) | (external_id & ((1u64 << VERTEX_LABEL_SHIFT) - 1))
}
