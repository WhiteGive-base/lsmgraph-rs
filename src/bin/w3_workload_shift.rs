use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Instant;

use anyhow::{ensure, Context, Result};
use clap::Parser;
use lsmgraph::config::{L0LayoutPolicy, LsmGraphConfig};
use lsmgraph::graph::Engine;
use lsmgraph::types::{EdgeLabel, EdgeType, VertexId, VertexLabel};
use serde::Serialize;
use serde_json::{json, Value};

#[derive(Parser, Debug)]
#[command(
    name = "w3-workload-shift",
    about = "W3 C12 feedback-only semantic-budget workload-shift smoke runner"
)]
struct Cli {
    #[arg(long, default_value = "target/w3-workload-shift-store")]
    store_dir: PathBuf,
    #[arg(long)]
    output: Option<PathBuf>,
    #[arg(long, default_value_t = false)]
    reset_store: bool,
    #[arg(long, default_value_t = 4)]
    phase_flushes: u64,
    #[arg(long, default_value_t = 3)]
    queries_per_flush: usize,
    #[arg(long, value_delimiter = ',', default_value = "42")]
    phase_a_edge_types: Vec<EdgeType>,
    #[arg(long, value_delimiter = ',', default_value = "43")]
    phase_b_edge_types: Vec<EdgeType>,
    #[arg(long, default_value_t = 128 * 1024)]
    semantic_budget_min_edge_type_bytes: usize,
    #[arg(long, default_value_t = 0.20)]
    semantic_budget_min_edge_type_score: f64,
    #[arg(long, default_value_t = true)]
    include_core_distractor: bool,
}

#[derive(Debug, Clone, Copy)]
struct VariantConfig {
    name: &'static str,
    feedback_only: bool,
    disable_feedback: bool,
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
    io_read_bytes: u64,
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

#[derive(Debug, Serialize)]
struct FlushReport {
    phase: &'static str,
    flush_in_phase: u64,
    snapshot: u64,
    flush_elapsed_ms: f64,
    phase_edge_types: Vec<EdgeType>,
    query: QueryDelta,
    budget_candidates: Vec<BudgetCandidateRow>,
    levels: Vec<usize>,
}

#[derive(Debug, Serialize)]
struct VariantReport {
    variant: &'static str,
    store_dir: String,
    config: Value,
    flushes: Vec<FlushReport>,
    all_budget_candidates: Vec<BudgetCandidateRow>,
    metrics_final: Value,
}

#[tokio::main(flavor = "multi_thread")]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    ensure!(cli.phase_flushes >= 2, "--phase-flushes must be at least 2");
    ensure!(
        cli.queries_per_flush >= 1,
        "--queries-per-flush must be at least 1"
    );
    ensure!(
        !cli.phase_a_edge_types.is_empty() && !cli.phase_b_edge_types.is_empty(),
        "phase edge type sets must be non-empty"
    );
    ensure!(
        cli.phase_a_edge_types != cli.phase_b_edge_types,
        "phase A and phase B should use different edge type sets"
    );

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
        let report = run_variant(&cli, variant).await?;
        eprintln!(
            "[w3-workload-shift] completed variant={} flushes={}",
            report.variant,
            report.flushes.len()
        );
        reports.push(report);
    }

    let result = json!({
        "runner": "w3-workload-shift",
        "scale": "synthetic-sf1-smoke",
        "phase_flushes": cli.phase_flushes,
        "queries_per_flush": cli.queries_per_flush,
        "phase_a_edge_types": cli.phase_a_edge_types,
        "phase_b_edge_types": cli.phase_b_edge_types,
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

async fn run_variant(cli: &Cli, variant: VariantConfig) -> Result<VariantReport> {
    let store_dir = cli.store_dir.join(variant.name);
    prepare_store(&store_dir, cli.reset_store)?;
    let engine = create_engine(&store_dir, cli, variant).await?;

    let mut flushes = Vec::new();
    run_phase(
        &engine,
        &store_dir,
        cli,
        "A",
        10_000,
        &cli.phase_a_edge_types,
        &mut flushes,
    )
    .await?;
    run_phase(
        &engine,
        &store_dir,
        cli,
        "B",
        20_000,
        &cli.phase_b_edge_types,
        &mut flushes,
    )
    .await?;

    let all_budget_candidates = read_budget_candidates(&store_dir)?;
    Ok(VariantReport {
        variant: variant.name,
        store_dir: store_dir.display().to_string(),
        config: json!({
            "l0_layout": "SemanticBudgeted",
            "semantic_budget_feedback_only": variant.feedback_only,
            "semantic_budget_disable_feedback": variant.disable_feedback,
            "semantic_budget_min_edge_type_bytes": cli.semantic_budget_min_edge_type_bytes,
            "semantic_budget_min_edge_type_score": cli.semantic_budget_min_edge_type_score,
            "include_core_distractor": cli.include_core_distractor,
        }),
        flushes,
        all_budget_candidates,
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
    phase: &'static str,
    local_src_base: u64,
    edge_types: &[EdgeType],
    reports: &mut Vec<FlushReport>,
) -> Result<()> {
    for flush_in_phase in 0..cli.phase_flushes {
        let started = Instant::now();
        insert_flush_edges(
            engine,
            local_src_base,
            flush_in_phase,
            edge_types,
            cli.include_core_distractor,
        )
        .await?;
        engine.flush_active().await?;
        let flush_elapsed_ms = started.elapsed().as_secs_f64() * 1000.0;
        let snapshot = engine.current_snapshot();
        let budget_candidates = read_budget_candidates_for_snapshot(store_dir, snapshot)?;
        let query = collect_phase_queries(
            engine,
            local_src_base,
            flush_in_phase + 1,
            edge_types,
            cli.queries_per_flush,
        )
        .await?;
        reports.push(FlushReport {
            phase,
            flush_in_phase,
            snapshot,
            flush_elapsed_ms,
            phase_edge_types: edge_types.to_vec(),
            query,
            budget_candidates,
            levels: engine.live_file_count_by_level(),
        });
    }
    Ok(())
}

async fn insert_flush_edges(
    engine: &Arc<Engine>,
    local_src_base: u64,
    flush_in_phase: u64,
    edge_types: &[EdgeType],
    include_core_distractor: bool,
) -> Result<()> {
    for edge_type in edge_types {
        let src = encoded(VertexLabel::Person, local_src_base + *edge_type as u64);
        let dst = encoded(
            VertexLabel::TagClass,
            local_src_base + 100_000 + flush_in_phase * 100 + *edge_type as u64,
        );
        engine.insert_edge(src, dst, *edge_type).await?;
    }
    if include_core_distractor {
        let src = encoded(VertexLabel::Person, local_src_base + 900_000);
        let dst = encoded(
            VertexLabel::Person,
            local_src_base + 910_000 + flush_in_phase,
        );
        engine
            .insert_edge(src, dst, EdgeLabel::Knows.as_i32())
            .await?;
    }
    Ok(())
}

async fn collect_phase_queries(
    engine: &Arc<Engine>,
    local_src_base: u64,
    expected_neighbors: u64,
    edge_types: &[EdgeType],
    queries_per_edge_type: usize,
) -> Result<QueryDelta> {
    let before = engine.metrics().snapshot_json();
    let started = Instant::now();
    let mut neighbors_total = 0usize;
    for edge_type in edge_types {
        let src = encoded(VertexLabel::Person, local_src_base + *edge_type as u64);
        for _ in 0..queries_per_edge_type {
            let neighbors = engine
                .get_neighbors_typed(src, *edge_type, engine.current_snapshot())
                .await?;
            ensure!(
                neighbors.len() == expected_neighbors as usize,
                "unexpected neighbor count for edge_type {edge_type}: got {}, expected {expected_neighbors}",
                neighbors.len()
            );
            neighbors_total += neighbors.len();
        }
    }
    let elapsed_ms = started.elapsed().as_secs_f64() * 1000.0;
    let after = engine.metrics().snapshot_json();
    Ok(QueryDelta {
        elapsed_ms,
        queries: edge_types.len() * queries_per_edge_type,
        neighbors_total,
        candidate_l0_segments: metric_delta(&before, &after, "csr", "candidate_l0_segments"),
        filter_passed_segments: metric_delta(&before, &after, "csr", "filter_passed_segments"),
        matched_l0_segments: metric_delta(&before, &after, "csr", "matched_l0_segments"),
        csr_body_reads: metric_delta(&before, &after, "csr", "body_reads"),
        io_read_bytes: metric_delta(&before, &after, "io", "read_bytes"),
    })
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
    metric_u64(after, section, name).saturating_sub(metric_u64(before, section, name))
}

fn metric_u64(metrics: &Value, section: &str, name: &str) -> u64 {
    metrics[section][name].as_u64().unwrap_or_default()
}

fn encoded(label: VertexLabel, local: u64) -> VertexId {
    ((label as u64) << 56) | local
}
