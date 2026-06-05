use std::fs;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Instant;

use anyhow::{ensure, Context, Result};
use clap::Parser;
use lsmgraph::config::{L0LayoutPolicy, LsmGraphConfig};
use lsmgraph::graph::{Engine, L0CompactionDecision};
use lsmgraph::types::{EdgeLabel, VertexId, VertexLabel};
use serde::Serialize;
use serde_json::{json, Value};

#[derive(Parser, Debug)]
#[command(
    name = "p3-feedback-bench",
    about = "Small deterministic P3 feedback compaction workload-shift microbench"
)]
struct Cli {
    #[arg(long, default_value = "target/p3-feedback-workload-shift-store")]
    store_dir: PathBuf,
    #[arg(long)]
    output: Option<PathBuf>,
    #[arg(long, default_value_t = false)]
    reset_store: bool,
    #[arg(long, default_value_t = false)]
    compare_no_feedback: bool,
    #[arg(long)]
    no_feedback_store_dir: Option<PathBuf>,
    #[arg(long, default_value_t = 3)]
    segments_per_phase: u64,
    #[arg(long, default_value_t = 3)]
    repeats: usize,
    #[arg(long, default_value_t = 256)]
    range_bucket_size: u64,
    #[arg(long, default_value_t = 40_000)]
    phase_a_local_src: u64,
    #[arg(long, default_value_t = 41_024)]
    phase_b_local_src: u64,
}

#[derive(Debug, Serialize)]
struct QueryMetrics {
    repeats: usize,
    elapsed_ms: f64,
    neighbors_total: usize,
    candidate_l0_segments: u64,
    avg_candidate_l0_segments: f64,
    filter_passed_segments: u64,
    matched_l0_segments: u64,
    csr_body_reads: u64,
    csr_offset_reads: u64,
    io_read_bytes: u64,
    l0_partitions: Value,
}

#[derive(Debug, Serialize)]
struct CompactionDecisionSummary {
    src_label: i32,
    edge_type: i32,
    range_start: VertexId,
    range_end: VertexId,
    score: f64,
    estimated_rewrite_bytes: u64,
    rewrite_mib: f64,
    selected_l0_segments: usize,
    query_count: u64,
    avg_candidate_segments: f64,
    offset_cache_miss_rate: f64,
    output_segments: usize,
    output_edges: u64,
}

#[derive(Debug, Serialize)]
struct CompactionMetricsSummary {
    compaction_count: u64,
    compaction_latency_count: u64,
    compaction_latency_sum_us: u64,
    compaction_latency_max_us: u64,
    compaction_input_bytes: u64,
    compaction_output_bytes: u64,
    io_read_bytes: u64,
    io_read_syscalls: u64,
    io_write_bytes: u64,
    io_write_syscalls: u64,
    write_blocking_latency_count: u64,
    write_blocking_latency_sum_us: u64,
    sync_blocking_latency_count: u64,
    sync_blocking_latency_sum_us: u64,
    rebuild_index_latency_count: u64,
    rebuild_index_latency_sum_us: u64,
}

#[derive(Debug, Serialize)]
struct CompactionMetricsReport {
    elapsed_ms: f64,
    summary: CompactionMetricsSummary,
    metrics_before: Value,
    metrics_after: Value,
}

#[derive(Debug, Serialize)]
struct PhaseReport {
    name: &'static str,
    src: VertexId,
    edge_type: i32,
    expected_neighbors: usize,
    before: QueryMetrics,
    decision: CompactionDecisionSummary,
    compaction: CompactionMetricsReport,
    after: QueryMetrics,
}

#[derive(Debug, Serialize)]
struct NoFeedbackPhaseReport {
    name: &'static str,
    src: VertexId,
    edge_type: i32,
    expected_neighbors: usize,
    before: QueryMetrics,
    after_without_compaction: QueryMetrics,
}

#[derive(Debug, Serialize)]
struct NoFeedbackReport {
    store_dir: String,
    levels_initial: Vec<usize>,
    levels_final: Vec<usize>,
    phase_a: NoFeedbackPhaseReport,
    phase_b: NoFeedbackPhaseReport,
}

#[tokio::main(flavor = "multi_thread")]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    ensure!(
        cli.segments_per_phase >= 2,
        "--segments-per-phase must be at least 2"
    );
    ensure!(cli.repeats >= 2, "--repeats must be at least 2");

    let src_a = encoded(VertexLabel::Person, cli.phase_a_local_src);
    let src_b = encoded(VertexLabel::Person, cli.phase_b_local_src);
    let edge_type = EdgeLabel::Knows.as_i32();

    ensure!(
        bucket_start(src_a, cli.range_bucket_size) != bucket_start(src_b, cli.range_bucket_size),
        "phase A and phase B sources must map to different range buckets"
    );

    let no_feedback = if cli.compare_no_feedback {
        let no_feedback_store_dir = cli
            .no_feedback_store_dir
            .clone()
            .unwrap_or_else(|| PathBuf::from(format!("{}-no-feedback", cli.store_dir.display())));
        let baseline = create_loaded_engine(
            &no_feedback_store_dir,
            cli.reset_store,
            cli.range_bucket_size,
            src_a,
            src_b,
            edge_type,
            cli.segments_per_phase,
        )
        .await?;
        let levels_initial = baseline.live_file_count_by_level();
        let phase_a = run_no_feedback_phase(
            &baseline,
            "A",
            src_a,
            edge_type,
            cli.segments_per_phase as usize,
            cli.repeats,
        )
        .await?;
        let phase_b = run_no_feedback_phase(
            &baseline,
            "B",
            src_b,
            edge_type,
            cli.segments_per_phase as usize,
            cli.repeats,
        )
        .await?;
        Some(NoFeedbackReport {
            store_dir: no_feedback_store_dir.display().to_string(),
            levels_initial,
            levels_final: baseline.live_file_count_by_level(),
            phase_a,
            phase_b,
        })
    } else {
        None
    };

    let engine = create_loaded_engine(
        &cli.store_dir,
        cli.reset_store,
        cli.range_bucket_size,
        src_a,
        src_b,
        edge_type,
        cli.segments_per_phase,
    )
    .await?;
    let initial_levels = engine.live_file_count_by_level();

    let phase_a = run_phase(
        &engine,
        "A",
        src_a,
        edge_type,
        cli.segments_per_phase as usize,
        cli.repeats,
    )
    .await?;
    ensure!(
        phase_a.decision.range_start <= src_a && src_a <= phase_a.decision.range_end,
        "phase A auto-pick did not select phase A source range"
    );
    ensure!(
        src_b < phase_a.decision.range_start || src_b > phase_a.decision.range_end,
        "phase A auto-pick unexpectedly overlapped phase B source range"
    );
    let after_phase_a_levels = engine.live_file_count_by_level();

    let phase_b = run_phase(
        &engine,
        "B",
        src_b,
        edge_type,
        cli.segments_per_phase as usize,
        cli.repeats,
    )
    .await?;
    ensure!(
        phase_b.decision.range_start <= src_b && src_b <= phase_b.decision.range_end,
        "phase B auto-pick did not select phase B source range"
    );
    ensure!(
        phase_a.decision.range_start != phase_b.decision.range_start,
        "workload shift did not move compaction priority to a new range"
    );
    let final_levels = engine.live_file_count_by_level();

    let store_dir = cli.store_dir.display().to_string();
    let phase_a_candidate_l0_before = phase_a.before.candidate_l0_segments;
    let phase_a_candidate_l0_after = phase_a.after.candidate_l0_segments;
    let phase_b_candidate_l0_before = phase_b.before.candidate_l0_segments;
    let phase_b_candidate_l0_after = phase_b.after.candidate_l0_segments;
    let selected_ranges_changed = phase_a.decision.range_start != phase_b.decision.range_start;
    let no_feedback_summary = no_feedback.as_ref().map(|report| {
        json!({
            "phase_a_after_avg_candidate_l0_segments": report
                .phase_a
                .after_without_compaction
                .avg_candidate_l0_segments,
            "phase_b_after_avg_candidate_l0_segments": report
                .phase_b
                .after_without_compaction
                .avg_candidate_l0_segments,
            "phase_a_after_candidate_l0_segments": report
                .phase_a
                .after_without_compaction
                .candidate_l0_segments,
            "phase_b_after_candidate_l0_segments": report
                .phase_b
                .after_without_compaction
                .candidate_l0_segments
        })
    });

    let result = json!({
        "bench": "p3-feedback-workload-shift",
        "store_dir": store_dir,
        "config": {
            "segments_per_phase": cli.segments_per_phase,
            "repeats": cli.repeats,
            "range_bucket_size": cli.range_bucket_size,
            "l0_ra_min_queries": 2,
            "l0_ra_min_l0_segments": 2,
            "l0_ra_min_score": 0.0,
            "l0_layout": "SemanticBudgeted"
        },
        "setup": {
            "phase_a_src": src_a,
            "phase_b_src": src_b,
            "edge_type": edge_type,
            "phase_a_bucket_start": bucket_start(src_a, cli.range_bucket_size),
            "phase_b_bucket_start": bucket_start(src_b, cli.range_bucket_size)
        },
        "no_feedback": &no_feedback,
        "levels": {
            "initial": initial_levels,
            "after_phase_a": after_phase_a_levels,
            "final": final_levels
        },
        "phase_a": &phase_a,
        "phase_b": &phase_b,
        "summary": {
            "phase_a_candidate_l0_before": phase_a_candidate_l0_before,
            "phase_a_candidate_l0_after": phase_a_candidate_l0_after,
            "phase_b_candidate_l0_before": phase_b_candidate_l0_before,
            "phase_b_candidate_l0_after": phase_b_candidate_l0_after,
            "selected_ranges_changed": selected_ranges_changed,
            "no_feedback": no_feedback_summary
        }
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

async fn create_loaded_engine(
    store_dir: &PathBuf,
    reset_store: bool,
    range_bucket_size: u64,
    src_a: VertexId,
    src_b: VertexId,
    edge_type: i32,
    segments_per_phase: u64,
) -> Result<Arc<Engine>> {
    prepare_store(store_dir, reset_store)?;
    let mut config = LsmGraphConfig::new(store_dir)
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(1)
        .with_semantic_budget_min_exact_bytes(1)
        .with_semantic_budget_min_benefit_score(0.0);
    config.l0_ra_range_bucket_size = range_bucket_size;
    config.l0_ra_min_queries = 2;
    config.l0_ra_min_l0_segments = 2;
    config.l0_ra_min_score = 0.0;

    let engine = Engine::create(config).await?;
    load_phase_edges(&engine, src_a, 50_000, edge_type, segments_per_phase).await?;
    load_phase_edges(&engine, src_b, 60_000, edge_type, segments_per_phase).await?;
    Ok(engine)
}

fn prepare_store(store_dir: &PathBuf, reset_store: bool) -> Result<()> {
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

async fn load_phase_edges(
    engine: &Arc<Engine>,
    src: VertexId,
    dst_local_base: u64,
    edge_type: i32,
    segments: u64,
) -> Result<()> {
    for i in 0..segments {
        engine
            .insert_edge(
                src,
                encoded(VertexLabel::Person, dst_local_base + i),
                edge_type,
            )
            .await?;
        engine.flush_active().await?;
    }
    Ok(())
}

async fn run_phase(
    engine: &Arc<Engine>,
    name: &'static str,
    src: VertexId,
    edge_type: i32,
    expected_neighbors: usize,
    repeats: usize,
) -> Result<PhaseReport> {
    let before = collect_query_metrics(engine, src, edge_type, expected_neighbors, repeats).await?;
    ensure!(
        before.candidate_l0_segments >= (expected_neighbors * repeats) as u64,
        "phase {name} did not observe the expected L0 read amplification before compaction"
    );
    let compaction_metrics_before = engine.metrics().snapshot_json();
    let compaction_started = Instant::now();
    let decision = engine
        .compact_best_l0_partition_by_score()
        .await?
        .context("feedback compaction did not pick a partition")?;
    let compaction_elapsed_ms = compaction_started.elapsed().as_secs_f64() * 1000.0;
    let compaction_metrics_after = engine.metrics().snapshot_json();
    let decision = summarize_decision(decision);
    let compaction = summarize_compaction_metrics(
        compaction_elapsed_ms,
        compaction_metrics_before,
        compaction_metrics_after,
    );
    ensure!(
        decision.selected_l0_segments >= expected_neighbors,
        "phase {name} selected too few L0 segments"
    );
    ensure!(
        decision.output_segments > 0,
        "phase {name} compaction produced no output segment"
    );
    let after = collect_query_metrics(engine, src, edge_type, expected_neighbors, 1).await?;
    ensure!(
        after.candidate_l0_segments == 0,
        "phase {name} still probes L0 files after feedback compaction"
    );
    Ok(PhaseReport {
        name,
        src,
        edge_type,
        expected_neighbors,
        before,
        decision,
        compaction,
        after,
    })
}

async fn run_no_feedback_phase(
    engine: &Arc<Engine>,
    name: &'static str,
    src: VertexId,
    edge_type: i32,
    expected_neighbors: usize,
    repeats: usize,
) -> Result<NoFeedbackPhaseReport> {
    let before = collect_query_metrics(engine, src, edge_type, expected_neighbors, repeats).await?;
    ensure!(
        before.candidate_l0_segments >= (expected_neighbors * repeats) as u64,
        "no-feedback phase {name} did not observe the expected L0 read amplification"
    );
    let after_without_compaction =
        collect_query_metrics(engine, src, edge_type, expected_neighbors, 1).await?;
    ensure!(
        after_without_compaction.candidate_l0_segments >= expected_neighbors as u64,
        "no-feedback phase {name} unexpectedly reduced L0 candidates without compaction"
    );
    Ok(NoFeedbackPhaseReport {
        name,
        src,
        edge_type,
        expected_neighbors,
        before,
        after_without_compaction,
    })
}

async fn collect_query_metrics(
    engine: &Arc<Engine>,
    src: VertexId,
    edge_type: i32,
    expected_neighbors: usize,
    repeats: usize,
) -> Result<QueryMetrics> {
    engine.metrics().reset();
    let started = Instant::now();
    let mut neighbors_total = 0usize;
    for _ in 0..repeats {
        let neighbors = engine
            .get_neighbors_typed(src, edge_type, engine.current_snapshot())
            .await?;
        ensure!(
            neighbors.len() == expected_neighbors,
            "unexpected neighbor count for src {src}: got {}, expected {expected_neighbors}",
            neighbors.len()
        );
        neighbors_total += neighbors.len();
    }
    let elapsed_ms = started.elapsed().as_secs_f64() * 1000.0;
    let metrics = engine.metrics().snapshot_json();
    let candidate_l0_segments = metric_u64(&metrics, "csr", "candidate_l0_segments");
    Ok(QueryMetrics {
        repeats,
        elapsed_ms,
        neighbors_total,
        candidate_l0_segments,
        avg_candidate_l0_segments: candidate_l0_segments as f64 / repeats as f64,
        filter_passed_segments: metric_u64(&metrics, "csr", "filter_passed_segments"),
        matched_l0_segments: metric_u64(&metrics, "csr", "matched_l0_segments"),
        csr_body_reads: metric_u64(&metrics, "csr", "body_reads"),
        csr_offset_reads: metric_u64(&metrics, "csr", "offset_reads"),
        io_read_bytes: metric_u64(&metrics, "io", "read_bytes"),
        l0_partitions: metrics["csr"]["l0_partitions"].clone(),
    })
}

fn summarize_decision(decision: L0CompactionDecision) -> CompactionDecisionSummary {
    CompactionDecisionSummary {
        src_label: decision.key.src_label,
        edge_type: decision.key.edge_type,
        range_start: decision.key.range_start,
        range_end: decision.key.range_end,
        score: decision.score,
        estimated_rewrite_bytes: decision.estimated_rewrite_bytes,
        rewrite_mib: (decision.estimated_rewrite_bytes as f64 / 1_048_576.0).max(1.0),
        selected_l0_segments: decision.selected_l0_segments,
        query_count: decision.query_count,
        avg_candidate_segments: decision.avg_candidate_segments,
        offset_cache_miss_rate: decision.offset_cache_miss_rate,
        output_segments: decision.outputs.len(),
        output_edges: decision.outputs.iter().map(|meta| meta.edge_count).sum(),
    }
}

fn summarize_compaction_metrics(
    elapsed_ms: f64,
    metrics_before: Value,
    metrics_after: Value,
) -> CompactionMetricsReport {
    let summary = CompactionMetricsSummary {
        compaction_count: metric_delta_u64(
            &metrics_after,
            &metrics_before,
            &["storage", "compaction_count"],
        ),
        compaction_latency_count: metric_delta_u64(
            &metrics_after,
            &metrics_before,
            &["storage", "compaction_latency", "count"],
        ),
        compaction_latency_sum_us: metric_delta_u64(
            &metrics_after,
            &metrics_before,
            &["storage", "compaction_latency", "sum_us"],
        ),
        compaction_latency_max_us: metric_path_u64(
            &metrics_after,
            &["storage", "compaction_latency", "max_us"],
        ),
        compaction_input_bytes: metric_delta_u64(
            &metrics_after,
            &metrics_before,
            &["storage", "compaction_input_bytes"],
        ),
        compaction_output_bytes: metric_delta_u64(
            &metrics_after,
            &metrics_before,
            &["storage", "compaction_output_bytes"],
        ),
        io_read_bytes: metric_delta_u64(&metrics_after, &metrics_before, &["io", "read_bytes"]),
        io_read_syscalls: metric_delta_u64(
            &metrics_after,
            &metrics_before,
            &["io", "read_syscalls"],
        ),
        io_write_bytes: metric_delta_u64(&metrics_after, &metrics_before, &["io", "write_bytes"]),
        io_write_syscalls: metric_delta_u64(
            &metrics_after,
            &metrics_before,
            &["io", "write_syscalls"],
        ),
        write_blocking_latency_count: metric_delta_u64(
            &metrics_after,
            &metrics_before,
            &["io", "write_blocking_latency", "count"],
        ),
        write_blocking_latency_sum_us: metric_delta_u64(
            &metrics_after,
            &metrics_before,
            &["io", "write_blocking_latency", "sum_us"],
        ),
        sync_blocking_latency_count: metric_delta_u64(
            &metrics_after,
            &metrics_before,
            &["io", "sync_blocking_latency", "count"],
        ),
        sync_blocking_latency_sum_us: metric_delta_u64(
            &metrics_after,
            &metrics_before,
            &["io", "sync_blocking_latency", "sum_us"],
        ),
        rebuild_index_latency_count: metric_delta_u64(
            &metrics_after,
            &metrics_before,
            &["storage", "rebuild_index_latency", "count"],
        ),
        rebuild_index_latency_sum_us: metric_delta_u64(
            &metrics_after,
            &metrics_before,
            &["storage", "rebuild_index_latency", "sum_us"],
        ),
    };
    CompactionMetricsReport {
        elapsed_ms,
        summary,
        metrics_before,
        metrics_after,
    }
}

fn encoded(label: VertexLabel, local: u64) -> VertexId {
    ((label as u64) << 56) | local
}

fn bucket_start(src: VertexId, bucket_size: u64) -> u64 {
    let bucket_size = bucket_size.max(1);
    (src / bucket_size) * bucket_size
}

fn metric_u64(metrics: &Value, group: &str, name: &str) -> u64 {
    metrics[group][name].as_u64().unwrap_or_default()
}

fn metric_path_u64(metrics: &Value, path: &[&str]) -> u64 {
    let mut current = metrics;
    for key in path {
        current = &current[*key];
    }
    current.as_u64().unwrap_or_default()
}

fn metric_delta_u64(after: &Value, before: &Value, path: &[&str]) -> u64 {
    metric_path_u64(after, path).saturating_sub(metric_path_u64(before, path))
}
