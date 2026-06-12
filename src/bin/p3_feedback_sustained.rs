use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{ensure, Context, Result};
use clap::Parser;
use lsmgraph::config::{L0LayoutPolicy, LsmGraphConfig};
use lsmgraph::graph::{Engine, L0CompactionDecision};
use lsmgraph::types::{EdgeLabel, VertexId, VertexLabel};
use serde::Serialize;
use serde_json::{json, Value};

#[derive(Parser, Debug)]
#[command(
    name = "p3-feedback-sustained",
    about = "Sustained P3 feedback-vs-no-feedback trace with periodic checkpoints"
)]
struct Cli {
    #[arg(long, default_value = "target/p3-feedback-sustained-store")]
    store_dir: PathBuf,
    #[arg(long)]
    no_feedback_store_dir: Option<PathBuf>,
    #[arg(long)]
    output_json: PathBuf,
    #[arg(long)]
    output_tsv: PathBuf,
    #[arg(long, default_value_t = false)]
    reset_store: bool,
    #[arg(long, default_value_t = 1800)]
    duration_secs: u64,
    #[arg(long, default_value_t = 300)]
    checkpoint_secs: u64,
    #[arg(long, default_value_t = 600)]
    phase_window_secs: u64,
    #[arg(long, default_value_t = 1)]
    segments_per_checkpoint: u64,
    #[arg(long, default_value_t = 5)]
    repeats: usize,
    #[arg(long, default_value_t = 256)]
    range_bucket_size: u64,
}

#[derive(Debug, Clone, Serialize)]
struct QueryMetrics {
    repeats: usize,
    elapsed_us: u64,
    avg_us: f64,
    neighbors_total: usize,
    candidate_l0_segments: u64,
    avg_candidate_l0_segments: f64,
    filter_passed_segments: u64,
    matched_l0_segments: u64,
    csr_body_reads: u64,
    csr_offset_reads: u64,
    io_read_bytes: u64,
}

#[derive(Debug, Clone, Serialize)]
struct CompactionDecisionSummary {
    src_label: i32,
    edge_type: i32,
    range_start: VertexId,
    range_end: VertexId,
    score: f64,
    estimated_rewrite_bytes: u64,
    selected_l0_segments: usize,
    query_count: u64,
    avg_candidate_segments: f64,
    output_segments: usize,
    output_edges: u64,
}

#[derive(Debug, Clone, Serialize)]
struct CompactionMetricsSummary {
    elapsed_us: u64,
    compaction_count: u64,
    compaction_latency_count: u64,
    compaction_latency_sum_us: u64,
    compaction_latency_max_us: u64,
    compaction_input_bytes: u64,
    compaction_output_bytes: u64,
    io_read_bytes: u64,
    io_write_bytes: u64,
    write_blocking_latency_count: u64,
    write_blocking_latency_sum_us: u64,
    sync_blocking_latency_count: u64,
    sync_blocking_latency_sum_us: u64,
}

#[derive(Debug, Clone, Serialize)]
struct CompactionReport {
    decision: Option<CompactionDecisionSummary>,
    metrics: CompactionMetricsSummary,
}

#[derive(Debug, Clone, Serialize)]
struct CheckpointRecord {
    checkpoint_index: u64,
    elapsed_s: u64,
    phase: &'static str,
    active_src: VertexId,
    active_loaded_edges: u64,
    feedback_l0_files_before: usize,
    feedback_l0_files_after: usize,
    no_feedback_l0_files: usize,
    feedback_before: QueryMetrics,
    feedback_compaction: CompactionReport,
    feedback_after: QueryMetrics,
    no_feedback: QueryMetrics,
}

#[derive(Debug, Serialize)]
struct SustainedReport {
    bench: &'static str,
    config: Value,
    setup: Value,
    checkpoints: Vec<CheckpointRecord>,
    summary: Value,
}

#[tokio::main(flavor = "multi_thread")]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    ensure!(cli.duration_secs > 0, "--duration-secs must be positive");
    ensure!(
        cli.checkpoint_secs > 0,
        "--checkpoint-secs must be positive"
    );
    ensure!(
        cli.phase_window_secs > 0,
        "--phase-window-secs must be positive"
    );
    ensure!(
        cli.segments_per_checkpoint > 0,
        "--segments-per-checkpoint must be positive"
    );
    ensure!(cli.repeats > 0, "--repeats must be positive");

    prepare_store(&cli.store_dir, cli.reset_store)?;
    let no_feedback_store_dir = cli
        .no_feedback_store_dir
        .clone()
        .unwrap_or_else(|| PathBuf::from(format!("{}-no-feedback", cli.store_dir.display())));
    prepare_store(&no_feedback_store_dir, cli.reset_store)?;
    ensure_parent(&cli.output_json)?;
    ensure_parent(&cli.output_tsv)?;
    write_tsv_header(&cli.output_tsv)?;

    let feedback = create_engine(&cli.store_dir, cli.range_bucket_size).await?;
    let no_feedback = create_engine(&no_feedback_store_dir, cli.range_bucket_size).await?;
    let edge_type = EdgeLabel::Knows.as_i32();
    let phase_srcs = [
        encoded(VertexLabel::Person, 40_000),
        encoded(VertexLabel::Person, 41_024),
        encoded(VertexLabel::Person, 42_048),
    ];
    let phase_names = ["A", "B", "C"];
    ensure_distinct_buckets(&phase_srcs, cli.range_bucket_size)?;

    let started = Instant::now();
    let checkpoints = cli.duration_secs / cli.checkpoint_secs;
    let mut loaded_edges = [0u64; 3];
    let mut records = Vec::new();

    for checkpoint_index in 0..=checkpoints {
        let elapsed_s = started.elapsed().as_secs().min(cli.duration_secs);
        let phase_idx = phase_index(elapsed_s, cli.phase_window_secs);
        let phase = phase_names[phase_idx];
        let src = phase_srcs[phase_idx];

        load_checkpoint_edges(
            &feedback,
            &no_feedback,
            src,
            phase_idx,
            edge_type,
            loaded_edges[phase_idx],
            cli.segments_per_checkpoint,
        )
        .await?;
        loaded_edges[phase_idx] += cli.segments_per_checkpoint;

        let expected_neighbors = loaded_edges[phase_idx] as usize;
        let feedback_l0_files_before = l0_files(&feedback);
        let feedback_before =
            collect_query_metrics(&feedback, src, edge_type, expected_neighbors, cli.repeats)
                .await?;
        let feedback_compaction = compact_once(&feedback).await?;
        let feedback_after =
            collect_query_metrics(&feedback, src, edge_type, expected_neighbors, 1).await?;
        let feedback_l0_files_after = l0_files(&feedback);
        let no_feedback_metrics = collect_query_metrics(
            &no_feedback,
            src,
            edge_type,
            expected_neighbors,
            cli.repeats,
        )
        .await?;
        let no_feedback_l0_files = l0_files(&no_feedback);

        let record = CheckpointRecord {
            checkpoint_index,
            elapsed_s,
            phase,
            active_src: src,
            active_loaded_edges: loaded_edges[phase_idx],
            feedback_l0_files_before,
            feedback_l0_files_after,
            no_feedback_l0_files,
            feedback_before,
            feedback_compaction,
            feedback_after,
            no_feedback: no_feedback_metrics,
        };
        append_tsv(&cli.output_tsv, &record)?;
        records.push(record);
        write_json_report(
            &cli,
            &records,
            &phase_srcs,
            edge_type,
            loaded_edges,
            cli.duration_secs,
        )?;

        if checkpoint_index < checkpoints {
            let next_due = Duration::from_secs((checkpoint_index + 1) * cli.checkpoint_secs);
            let elapsed = started.elapsed();
            if next_due > elapsed {
                tokio::time::sleep(next_due - elapsed).await;
            }
        }
    }

    Ok(())
}

async fn create_engine(store_dir: &PathBuf, range_bucket_size: u64) -> Result<Arc<Engine>> {
    let mut config = LsmGraphConfig::new(store_dir)
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(1)
        .with_semantic_budget_min_exact_bytes(1)
        .with_semantic_budget_min_benefit_score(0.0);
    config.l0_ra_range_bucket_size = range_bucket_size;
    config.l0_ra_min_queries = 1;
    config.l0_ra_min_l0_segments = 1;
    config.l0_ra_min_score = 0.0;
    Engine::create(config).await
}

async fn load_checkpoint_edges(
    feedback: &Arc<Engine>,
    no_feedback: &Arc<Engine>,
    src: VertexId,
    phase_idx: usize,
    edge_type: i32,
    loaded_before: u64,
    segments: u64,
) -> Result<()> {
    for i in 0..segments {
        let local_dst = 100_000 + (phase_idx as u64 * 1_000_000) + loaded_before + i;
        let dst = encoded(VertexLabel::Person, local_dst);
        feedback.insert_edge(src, dst, edge_type).await?;
        feedback.flush_active().await?;
        no_feedback.insert_edge(src, dst, edge_type).await?;
        no_feedback.flush_active().await?;
    }
    Ok(())
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
    let elapsed_us = started.elapsed().as_micros() as u64;
    let metrics = engine.metrics().snapshot_json();
    let candidate_l0_segments = metric_u64(&metrics, "csr", "candidate_l0_segments");
    Ok(QueryMetrics {
        repeats,
        elapsed_us,
        avg_us: elapsed_us as f64 / repeats as f64,
        neighbors_total,
        candidate_l0_segments,
        avg_candidate_l0_segments: candidate_l0_segments as f64 / repeats as f64,
        filter_passed_segments: metric_u64(&metrics, "csr", "filter_passed_segments"),
        matched_l0_segments: metric_u64(&metrics, "csr", "matched_l0_segments"),
        csr_body_reads: metric_u64(&metrics, "csr", "body_reads"),
        csr_offset_reads: metric_u64(&metrics, "csr", "offset_reads"),
        io_read_bytes: metric_u64(&metrics, "io", "read_bytes"),
    })
}

async fn compact_once(engine: &Arc<Engine>) -> Result<CompactionReport> {
    let metrics_before = engine.metrics().snapshot_json();
    let started = Instant::now();
    let decision = engine.compact_best_l0_partition_by_score().await?;
    let elapsed_us = started.elapsed().as_micros() as u64;
    let metrics_after = engine.metrics().snapshot_json();
    Ok(CompactionReport {
        decision: decision.map(summarize_decision),
        metrics: summarize_compaction_metrics(elapsed_us, metrics_before, metrics_after),
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
        selected_l0_segments: decision.selected_l0_segments,
        query_count: decision.query_count,
        avg_candidate_segments: decision.avg_candidate_segments,
        output_segments: decision.outputs.len(),
        output_edges: decision.outputs.iter().map(|meta| meta.edge_count).sum(),
    }
}

fn summarize_compaction_metrics(
    elapsed_us: u64,
    metrics_before: Value,
    metrics_after: Value,
) -> CompactionMetricsSummary {
    CompactionMetricsSummary {
        elapsed_us,
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
        io_write_bytes: metric_delta_u64(&metrics_after, &metrics_before, &["io", "write_bytes"]),
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
    }
}

fn write_json_report(
    cli: &Cli,
    records: &[CheckpointRecord],
    phase_srcs: &[VertexId; 3],
    edge_type: i32,
    loaded_edges: [u64; 3],
    duration_secs: u64,
) -> Result<()> {
    let selected_ranges: Vec<_> = records
        .iter()
        .filter_map(|record| record.feedback_compaction.decision.as_ref())
        .map(|decision| {
            json!({
                "range_start": decision.range_start,
                "range_end": decision.range_end,
                "selected_l0_segments": decision.selected_l0_segments,
                "output_edges": decision.output_edges
            })
        })
        .collect();
    let selected_ranges_changed = selected_ranges
        .windows(2)
        .any(|window| window[0]["range_start"] != window[1]["range_start"]);
    let feedback_after_max = records
        .iter()
        .map(|record| record.feedback_after.avg_candidate_l0_segments)
        .fold(0.0, f64::max);
    let no_feedback_after_max = records
        .iter()
        .map(|record| record.no_feedback.avg_candidate_l0_segments)
        .fold(0.0, f64::max);
    let report = SustainedReport {
        bench: "p3-feedback-sustained",
        config: json!({
            "duration_secs": duration_secs,
            "checkpoint_secs": cli.checkpoint_secs,
            "phase_window_secs": cli.phase_window_secs,
            "segments_per_checkpoint": cli.segments_per_checkpoint,
            "repeats": cli.repeats,
            "range_bucket_size": cli.range_bucket_size,
            "l0_ra_min_queries": 1,
            "l0_ra_min_l0_segments": 1,
            "l0_ra_min_score": 0.0,
            "l0_layout": "SemanticBudgeted"
        }),
        setup: json!({
            "feedback_store_dir": cli.store_dir,
            "no_feedback_store_dir": cli.no_feedback_store_dir,
            "phase_srcs": phase_srcs,
            "edge_type": edge_type,
            "loaded_edges_by_phase": loaded_edges
        }),
        checkpoints: records.to_vec(),
        summary: json!({
            "checkpoint_count": records.len(),
            "selected_ranges_changed": selected_ranges_changed,
            "feedback_after_max_avg_candidate_l0_segments": feedback_after_max,
            "no_feedback_max_avg_candidate_l0_segments": no_feedback_after_max,
            "feedback_kept_lower_l0_than_no_feedback": feedback_after_max < no_feedback_after_max,
            "compactions": records
                .iter()
                .map(|record| record.feedback_compaction.metrics.compaction_count)
                .sum::<u64>(),
            "compaction_input_bytes": records
                .iter()
                .map(|record| record.feedback_compaction.metrics.compaction_input_bytes)
                .sum::<u64>(),
            "compaction_output_bytes": records
                .iter()
                .map(|record| record.feedback_compaction.metrics.compaction_output_bytes)
                .sum::<u64>(),
            "io_write_bytes": records
                .iter()
                .map(|record| record.feedback_compaction.metrics.io_write_bytes)
                .sum::<u64>(),
        }),
    };
    fs::write(&cli.output_json, serde_json::to_string_pretty(&report)?)?;
    Ok(())
}

fn write_tsv_header(path: &PathBuf) -> Result<()> {
    let mut file = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(path)?;
    writeln!(
        file,
        "checkpoint\telapsed_s\tphase\tactive_loaded_edges\tfeedback_l0_before\tfeedback_l0_after\tno_feedback_l0\tfeedback_before_avg_candidate_l0\tfeedback_after_avg_candidate_l0\tno_feedback_avg_candidate_l0\tfeedback_before_avg_us\tfeedback_after_avg_us\tno_feedback_avg_us\tselected_range_start\tselected_range_end\tselected_l0_segments\tcompaction_input_bytes\tcompaction_output_bytes\tio_write_bytes\twrite_blocking_latency_sum_us"
    )?;
    Ok(())
}

fn append_tsv(path: &PathBuf, record: &CheckpointRecord) -> Result<()> {
    let mut file = OpenOptions::new().append(true).open(path)?;
    let decision = record.feedback_compaction.decision.as_ref();
    writeln!(
        file,
        "{}\t{}\t{}\t{}\t{}\t{}\t{}\t{:.3}\t{:.3}\t{:.3}\t{:.3}\t{:.3}\t{:.3}\t{}\t{}\t{}\t{}\t{}\t{}\t{}",
        record.checkpoint_index,
        record.elapsed_s,
        record.phase,
        record.active_loaded_edges,
        record.feedback_l0_files_before,
        record.feedback_l0_files_after,
        record.no_feedback_l0_files,
        record.feedback_before.avg_candidate_l0_segments,
        record.feedback_after.avg_candidate_l0_segments,
        record.no_feedback.avg_candidate_l0_segments,
        record.feedback_before.avg_us,
        record.feedback_after.avg_us,
        record.no_feedback.avg_us,
        decision.map(|d| d.range_start.to_string()).unwrap_or_default(),
        decision.map(|d| d.range_end.to_string()).unwrap_or_default(),
        decision.map(|d| d.selected_l0_segments.to_string()).unwrap_or_default(),
        record.feedback_compaction.metrics.compaction_input_bytes,
        record.feedback_compaction.metrics.compaction_output_bytes,
        record.feedback_compaction.metrics.io_write_bytes,
        record
            .feedback_compaction
            .metrics
            .write_blocking_latency_sum_us
    )?;
    Ok(())
}

fn ensure_parent(path: &PathBuf) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    Ok(())
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

fn ensure_distinct_buckets(srcs: &[VertexId; 3], bucket_size: u64) -> Result<()> {
    ensure!(
        bucket_start(srcs[0], bucket_size) != bucket_start(srcs[1], bucket_size)
            && bucket_start(srcs[1], bucket_size) != bucket_start(srcs[2], bucket_size)
            && bucket_start(srcs[0], bucket_size) != bucket_start(srcs[2], bucket_size),
        "phase sources must map to different range buckets"
    );
    Ok(())
}

fn phase_index(elapsed_s: u64, phase_window_secs: u64) -> usize {
    ((elapsed_s / phase_window_secs) as usize).min(2)
}

fn l0_files(engine: &Arc<Engine>) -> usize {
    engine
        .live_file_count_by_level()
        .first()
        .copied()
        .unwrap_or_default()
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
