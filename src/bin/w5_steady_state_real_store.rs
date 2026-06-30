use std::collections::HashSet;
use std::fs::{self, OpenOptions};
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use anyhow::{ensure, Context, Result};
use clap::Parser;
use lsmgraph::config::{IoBackendKind, L0LayoutPolicy, LsmGraphConfig};
use lsmgraph::graph::Engine;
use lsmgraph::types::{EdgeType, VertexId};
use serde::Serialize;
use serde_json::{json, Value};

#[derive(Parser, Debug)]
#[command(
    name = "w5-steady-state-real-store",
    about = "W5 real-store steady-state mixed read/write runner"
)]
struct Cli {
    #[arg(long)]
    data_dir: PathBuf,
    #[arg(long, default_value = "../data/social_network_tugraph")]
    input: PathBuf,
    #[arg(long)]
    output: Option<PathBuf>,
    #[arg(long, default_value = "schema")]
    variant: Variant,
    #[arg(long, default_value = "blocking")]
    io_backend: IoBackendKind,
    #[arg(long, default_value_t = 4096)]
    csr_metadata_cache_entries: usize,
    #[arg(long, default_value_t = 64 * 1024 * 1024)]
    memgraph_bytes: usize,
    #[arg(long, default_value_t = 1)]
    edge_type: EdgeType,
    #[arg(long, default_value = "dynamic/person_knows_person_0_0.csv")]
    query_csv: PathBuf,
    #[arg(long)]
    mutation_csv: Option<PathBuf>,
    #[arg(long, default_value_t = 3600)]
    duration_secs: u64,
    #[arg(long, default_value_t = 60)]
    checkpoint_secs: u64,
    #[arg(long, default_value_t = 200)]
    query_rate_per_sec: u64,
    #[arg(long, default_value_t = 200)]
    writes_per_sec: u64,
    #[arg(long, default_value_t = 5000)]
    max_sample_sources: usize,
    #[arg(long, default_value_t = 100000)]
    max_mutation_edges: usize,
    #[arg(long, default_value_t = 1024)]
    flush_every_writes: u64,
    #[arg(long, default_value_t = 0)]
    compact_best_every_secs: u64,
    #[arg(long, default_value_t = 0)]
    full_compact_every_secs: u64,
    #[arg(long, default_value_t = 2)]
    delete_every: u64,
    #[arg(long)]
    semantic_budget_max_extra_l0_files: Option<usize>,
    #[arg(long, default_value_t = 4 * 1024 * 1024)]
    semantic_budget_min_edge_type_bytes: usize,
    #[arg(long, default_value_t = 1.0e308)]
    semantic_budget_min_edge_type_score: f64,
    #[arg(long, default_value_t = 4 * 1024 * 1024)]
    semantic_budget_min_exact_bytes: usize,
    #[arg(long, default_value_t = 1.0)]
    semantic_budget_min_benefit_score: f64,
    #[arg(long, default_value_t = 1 << 20)]
    l0_ra_range_bucket_size: u64,
    #[arg(long, default_value_t = 10)]
    l0_ra_min_queries: u64,
    #[arg(long, default_value_t = 2)]
    l0_ra_min_l0_segments: usize,
    #[arg(long, default_value_t = 10.0)]
    l0_ra_min_score: f64,
    #[arg(long, default_value_t = 1)]
    abort_min_queries_per_checkpoint: usize,
    #[arg(long, default_value_t = 30000)]
    abort_max_write_op_ms: u64,
}

#[derive(Debug, Clone, Copy)]
enum Variant {
    Schema,
    BudgB64,
    Semantic,
}

impl std::str::FromStr for Variant {
    type Err = anyhow::Error;

    fn from_str(value: &str) -> Result<Self> {
        match value.to_ascii_lowercase().as_str() {
            "schema" => Ok(Self::Schema),
            "budg-b64" | "budget-b64" | "semantic-budgeted-b64" => Ok(Self::BudgB64),
            "semantic" => Ok(Self::Semantic),
            _ => anyhow::bail!("unknown --variant {value}; use schema, budg-b64, or semantic"),
        }
    }
}

impl std::fmt::Display for Variant {
    fn fmt(&self, out: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Schema => out.write_str("schema"),
            Self::BudgB64 => out.write_str("budg-b64"),
            Self::Semantic => out.write_str("semantic"),
        }
    }
}

#[derive(Debug, Clone, Copy)]
struct MutationEdge {
    src: VertexId,
    dst: VertexId,
    edge_type: EdgeType,
}

#[derive(Debug, Default)]
struct WriterCounters {
    attempts: AtomicU64,
    inserts: AtomicU64,
    deletes: AtomicU64,
    flushes: AtomicU64,
    compactions: AtomicU64,
    errors: AtomicU64,
    op_latency_sum_us: AtomicU64,
    op_latency_max_us: AtomicU64,
    slow_ops: AtomicU64,
}

#[derive(Debug, Clone, Copy)]
struct WriterSnapshot {
    attempts: u64,
    inserts: u64,
    deletes: u64,
    flushes: u64,
    compactions: u64,
    errors: u64,
    op_latency_sum_us: u64,
    op_latency_max_us: u64,
    slow_ops: u64,
}

#[derive(Debug, Serialize)]
struct SeriesStats {
    count: usize,
    sum: f64,
    mean: f64,
    p50: f64,
    p99: f64,
    max: f64,
}

#[derive(Debug, Serialize)]
struct Checkpoint {
    event: &'static str,
    variant: String,
    elapsed_secs: f64,
    interval_secs: f64,
    queries: usize,
    query_rate_per_sec: f64,
    typed_neighbors_seen: u64,
    latency_us: SeriesStats,
    candidate_l0_segments: SeriesStats,
    l0_files: usize,
    level_files: Vec<usize>,
    metrics_delta: Value,
    metrics_after: Value,
    l0_partitions: Value,
    compaction_rewrite_bytes_delta: u64,
    flush_stall_proxy: Value,
    writer_delta: Value,
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
        cli.max_sample_sources > 0,
        "--max-sample-sources must be positive"
    );
    ensure!(
        cli.max_mutation_edges > 0,
        "--max-mutation-edges must be positive"
    );
    ensure!(
        !(cli.compact_best_every_secs > 0 && cli.full_compact_every_secs > 0),
        "use only one compaction mode: --compact-best-every-secs or --full-compact-every-secs"
    );

    let query_csv = resolve_under_input(&cli.input, &cli.query_csv);
    let mutation_csv = cli
        .mutation_csv
        .as_ref()
        .map(|path| resolve_under_input(&cli.input, path))
        .unwrap_or_else(|| query_csv.clone());
    let (sources, mutations) = load_edge_csv(
        &query_csv,
        &mutation_csv,
        cli.edge_type,
        cli.max_sample_sources,
        cli.max_mutation_edges,
    )?;

    let config = config_for_variant(&cli);
    let engine = Engine::open(config).await?;
    let mut out = open_output(cli.output.as_deref())?;
    write_json_line(
        &mut out,
        &json!({
            "event": "start",
            "runner": "w5_steady_state_real_store",
            "variant": cli.variant.to_string(),
            "data_dir": cli.data_dir,
            "input": cli.input,
            "query_csv": query_csv,
            "mutation_csv": mutation_csv,
            "edge_type": cli.edge_type,
            "duration_secs": cli.duration_secs,
            "checkpoint_secs": cli.checkpoint_secs,
            "query_rate_per_sec": cli.query_rate_per_sec,
            "writes_per_sec": cli.writes_per_sec,
            "flush_every_writes": cli.flush_every_writes,
            "delete_every": cli.delete_every,
            "compact_best_every_secs": cli.compact_best_every_secs,
            "full_compact_every_secs": cli.full_compact_every_secs,
            "sample_sources": sources.len(),
            "mutation_edges": mutations.len(),
            "eta_secs": cli.duration_secs,
            "abort_conditions": {
                "min_queries_per_checkpoint": cli.abort_min_queries_per_checkpoint,
                "max_write_op_ms": cli.abort_max_write_op_ms
            },
            "progress_signals": [
                "elapsed_secs",
                "candidate_l0_segments",
                "latency p50/p99",
                "l0_files",
                "compaction_rewrite_bytes_delta",
                "flush_stall_proxy",
                "io.write_bytes"
            ]
        }),
    )?;

    let stop = Arc::new(AtomicBool::new(false));
    let writer_counters = Arc::new(WriterCounters::default());
    let mut mutator: Option<std::thread::JoinHandle<Result<()>>> = None;
    if cli.writes_per_sec > 0 {
        let engine = engine.clone();
        let stop = stop.clone();
        let writer_counters = writer_counters.clone();
        let writes_per_sec = cli.writes_per_sec;
        let flush_every_writes = cli.flush_every_writes;
        let delete_every = cli.delete_every;
        let abort_max_write_op_ms = cli.abort_max_write_op_ms;
        mutator = Some(std::thread::spawn(move || {
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()?;
            rt.block_on(run_mutator(
                engine,
                mutations,
                writes_per_sec,
                flush_every_writes,
                delete_every,
                abort_max_write_op_ms,
                stop,
                writer_counters,
            ))
        }));
    }

    let mut compactor: Option<std::thread::JoinHandle<Result<()>>> = None;
    let compaction_interval = match (cli.compact_best_every_secs, cli.full_compact_every_secs) {
        (secs, 0) if secs > 0 => Some((Duration::from_secs(secs), true)),
        (0, secs) if secs > 0 => Some((Duration::from_secs(secs), false)),
        _ => None,
    };
    if let Some((interval, best_partition)) = compaction_interval {
        let engine = engine.clone();
        let stop = stop.clone();
        let writer_counters = writer_counters.clone();
        compactor = Some(std::thread::spawn(move || {
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()?;
            rt.block_on(run_compactor(
                engine,
                interval,
                best_partition,
                stop,
                writer_counters,
            ))
        }));
    }

    let run_result = run_queries(&cli, &engine, &sources, &writer_counters, &mut out).await;
    stop.store(true, Ordering::Relaxed);

    if let Some(task) = mutator {
        task.join()
            .map_err(|_| anyhow::anyhow!("mutator thread panicked"))??;
    }
    if let Some(task) = compactor {
        task.join()
            .map_err(|_| anyhow::anyhow!("compactor thread panicked"))??;
    }
    run_result?;
    engine.wait_for_flushes().await?;

    write_json_line(
        &mut out,
        &json!({
            "event": "done",
            "variant": cli.variant.to_string(),
            "unix_ms": unix_ms(),
            "levels_final": engine.live_file_count_by_level(),
            "metrics_final": engine.metrics().snapshot_json(),
            "writer_final": writer_snapshot_json(snapshot_writer(&writer_counters)),
        }),
    )?;
    out.flush()?;
    Ok(())
}

fn config_for_variant(cli: &Cli) -> LsmGraphConfig {
    let mut config = LsmGraphConfig::new(&cli.data_dir)
        .with_memgraph_capacity(cli.memgraph_bytes)
        .with_io_backend(cli.io_backend)
        .with_metadata_cache_entries(cli.csr_metadata_cache_entries)
        .with_semantic_budget_min_edge_type_bytes(cli.semantic_budget_min_edge_type_bytes)
        .with_semantic_budget_min_edge_type_score(cli.semantic_budget_min_edge_type_score)
        .with_semantic_budget_min_exact_bytes(cli.semantic_budget_min_exact_bytes)
        .with_semantic_budget_min_benefit_score(cli.semantic_budget_min_benefit_score);
    config.l0_ra_range_bucket_size = cli.l0_ra_range_bucket_size;
    config.l0_ra_min_queries = cli.l0_ra_min_queries;
    config.l0_ra_min_l0_segments = cli.l0_ra_min_l0_segments;
    config.l0_ra_min_score = cli.l0_ra_min_score;
    match cli.variant {
        Variant::Schema => config.with_l0_layout(L0LayoutPolicy::Schema),
        Variant::Semantic => config.with_l0_layout(L0LayoutPolicy::Semantic),
        Variant::BudgB64 => config
            .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
            .with_semantic_budget_max_extra_l0_files(
                cli.semantic_budget_max_extra_l0_files.or(Some(64)),
            ),
    }
}

async fn run_queries(
    cli: &Cli,
    engine: &Arc<Engine>,
    sources: &[VertexId],
    writer_counters: &Arc<WriterCounters>,
    out: &mut BufWriter<Box<dyn Write>>,
) -> Result<()> {
    let started = Instant::now();
    let deadline = started + Duration::from_secs(cli.duration_secs);
    let checkpoint_period = Duration::from_secs(cli.checkpoint_secs);
    let query_period = if cli.query_rate_per_sec == 0 {
        None
    } else {
        Some(Duration::from_secs_f64(1.0 / cli.query_rate_per_sec as f64))
    };
    let mut next_checkpoint = started + checkpoint_period;
    let mut interval_started = started;
    let mut rng = Lcg::new(unix_ms() ^ 0x5755_0005);

    let mut metrics_before = engine.metrics().snapshot_json();
    let mut writer_before = snapshot_writer(writer_counters);
    let mut latency_us = Vec::new();
    let mut candidate_l0 = Vec::new();
    let mut typed_neighbors_seen = 0u64;

    while Instant::now() < deadline {
        if let Some(period) = query_period {
            let query_started = Instant::now();
            let candidate_before = engine
                .metrics()
                .candidate_l0_segments
                .load(Ordering::Relaxed);
            let src = sources[rng.next_usize(sources.len())];
            let neighbors = engine
                .get_neighbors_typed(src, cli.edge_type, engine.current_snapshot())
                .await?;
            let candidate_after = engine
                .metrics()
                .candidate_l0_segments
                .load(Ordering::Relaxed);
            latency_us.push(query_started.elapsed().as_secs_f64() * 1_000_000.0);
            candidate_l0.push(candidate_after.saturating_sub(candidate_before) as f64);
            typed_neighbors_seen += neighbors.len() as u64;
            let elapsed = query_started.elapsed();
            if elapsed < period {
                tokio::time::sleep(period - elapsed).await;
            }
        } else {
            tokio::time::sleep(Duration::from_millis(50)).await;
        }

        let now = Instant::now();
        if now >= next_checkpoint || now >= deadline {
            let metrics_after = engine.metrics().snapshot_json();
            let metrics_before_next = metrics_after.clone();
            let writer_after = snapshot_writer(writer_counters);
            let interval_secs = now.duration_since(interval_started).as_secs_f64();
            let levels = engine.live_file_count_by_level();
            let metrics_delta = checkpoint_metric_delta(&metrics_after, &metrics_before);
            let compaction_rewrite_bytes_delta =
                metric_path_u64(&metrics_delta, &["storage", "compaction_input_bytes"])
                    + metric_path_u64(&metrics_delta, &["storage", "compaction_output_bytes"]);
            let checkpoint = Checkpoint {
                event: "checkpoint",
                variant: cli.variant.to_string(),
                elapsed_secs: now.duration_since(started).as_secs_f64(),
                interval_secs,
                queries: latency_us.len(),
                query_rate_per_sec: latency_us.len() as f64 / interval_secs.max(0.001),
                typed_neighbors_seen,
                latency_us: series_stats(&latency_us),
                candidate_l0_segments: series_stats(&candidate_l0),
                l0_files: levels.first().copied().unwrap_or_default(),
                level_files: levels,
                l0_partitions: metrics_after["csr"]["l0_partitions"].clone(),
                compaction_rewrite_bytes_delta,
                flush_stall_proxy: json!({
                    "io_write_blocking_latency_count_delta": metric_path_u64(&metrics_delta, &["io", "write_blocking_latency", "count"]),
                    "io_write_blocking_latency_sum_us_delta": metric_path_u64(&metrics_delta, &["io", "write_blocking_latency", "sum_us"]),
                    "io_write_blocking_latency_max_us_observed": metric_path_u64(&metrics_after, &["io", "write_blocking_latency", "max_us"]),
                    "io_sync_blocking_latency_count_delta": metric_path_u64(&metrics_delta, &["io", "sync_blocking_latency", "count"]),
                    "io_sync_blocking_latency_sum_us_delta": metric_path_u64(&metrics_delta, &["io", "sync_blocking_latency", "sum_us"]),
                    "writer_slow_ops_delta": writer_after.slow_ops.saturating_sub(writer_before.slow_ops),
                    "writer_max_op_us_observed": writer_after.op_latency_max_us,
                }),
                writer_delta: writer_delta_json(writer_before, writer_after),
                metrics_delta,
                metrics_after,
            };
            write_json_line(out, &checkpoint)?;
            out.flush()?;

            ensure!(
                latency_us.len() >= cli.abort_min_queries_per_checkpoint
                    || cli.query_rate_per_sec == 0,
                "abort: only {} queries completed in last checkpoint, below --abort-min-queries-per-checkpoint={}",
                latency_us.len(),
                cli.abort_min_queries_per_checkpoint
            );
            ensure!(
                writer_after.errors == writer_before.errors,
                "abort: background worker reported {} new errors",
                writer_after.errors.saturating_sub(writer_before.errors)
            );

            interval_started = now;
            metrics_before = metrics_before_next;
            writer_before = writer_after;
            latency_us.clear();
            candidate_l0.clear();
            typed_neighbors_seen = 0;
            while next_checkpoint <= now {
                next_checkpoint += checkpoint_period;
            }
        }
    }
    Ok(())
}

async fn run_mutator(
    engine: Arc<Engine>,
    edges: Vec<MutationEdge>,
    writes_per_sec: u64,
    flush_every_writes: u64,
    delete_every: u64,
    abort_max_write_op_ms: u64,
    stop: Arc<AtomicBool>,
    counters: Arc<WriterCounters>,
) -> Result<()> {
    let period = Duration::from_secs_f64(1.0 / writes_per_sec as f64);
    let mut index = 0usize;
    while !stop.load(Ordering::Relaxed) {
        let started = Instant::now();
        let edge = edges[index % edges.len()];
        let attempt = counters.attempts.fetch_add(1, Ordering::Relaxed) + 1;
        let result = if delete_every > 0 && attempt % delete_every == 0 {
            counters.deletes.fetch_add(1, Ordering::Relaxed);
            engine.delete_edge(edge.src, edge.dst, edge.edge_type).await
        } else {
            counters.inserts.fetch_add(1, Ordering::Relaxed);
            engine.insert_edge(edge.src, edge.dst, edge.edge_type).await
        };
        if result.is_err() {
            counters.errors.fetch_add(1, Ordering::Relaxed);
        }

        if flush_every_writes > 0 && attempt % flush_every_writes == 0 {
            if engine.flush_active().await.is_err() {
                counters.errors.fetch_add(1, Ordering::Relaxed);
            } else {
                counters.flushes.fetch_add(1, Ordering::Relaxed);
            }
        }

        let elapsed = started.elapsed();
        let elapsed_us = elapsed.as_micros() as u64;
        counters
            .op_latency_sum_us
            .fetch_add(elapsed_us, Ordering::Relaxed);
        fetch_max(&counters.op_latency_max_us, elapsed_us);
        if elapsed > Duration::from_millis(abort_max_write_op_ms) {
            counters.slow_ops.fetch_add(1, Ordering::Relaxed);
        }
        if elapsed < period {
            tokio::time::sleep(period - elapsed).await;
        }
        index = index.wrapping_add(1);
    }
    engine.flush_active().await?;
    counters.flushes.fetch_add(1, Ordering::Relaxed);
    Ok(())
}

async fn run_compactor(
    engine: Arc<Engine>,
    interval: Duration,
    best_partition: bool,
    stop: Arc<AtomicBool>,
    counters: Arc<WriterCounters>,
) -> Result<()> {
    while !stop.load(Ordering::Relaxed) {
        tokio::time::sleep(interval).await;
        if stop.load(Ordering::Relaxed) {
            break;
        }
        let result = if best_partition {
            engine
                .compact_best_l0_partition_by_score()
                .await
                .map(|_| ())
        } else {
            engine.compact_l0_to_l1().await.map(|_| ())
        };
        match result {
            Ok(()) => {
                counters.compactions.fetch_add(1, Ordering::Relaxed);
            }
            Err(_) => {
                counters.errors.fetch_add(1, Ordering::Relaxed);
            }
        }
    }
    Ok(())
}

fn load_edge_csv(
    query_csv: &Path,
    mutation_csv: &Path,
    edge_type: EdgeType,
    max_sources: usize,
    max_edges: usize,
) -> Result<(Vec<VertexId>, Vec<MutationEdge>)> {
    let sources = load_sample_sources(query_csv, max_sources)
        .with_context(|| format!("load query sources from {}", query_csv.display()))?;
    let mutations = load_mutation_edges(mutation_csv, edge_type, max_edges)
        .with_context(|| format!("load mutation edges from {}", mutation_csv.display()))?;
    ensure!(!sources.is_empty(), "no sampled query sources loaded");
    ensure!(!mutations.is_empty(), "no mutation edges loaded");
    Ok((sources, mutations))
}

fn load_sample_sources(path: &Path, max_sources: usize) -> Result<Vec<VertexId>> {
    let mut rdr = csv::ReaderBuilder::new()
        .delimiter(b'|')
        .has_headers(true)
        .from_path(path)?;
    let mut seen = HashSet::new();
    let mut sources = Vec::new();
    for rec in rdr.records() {
        let rec = rec?;
        let src: VertexId = rec
            .get(0)
            .context("missing source column")?
            .trim()
            .parse()?;
        if seen.insert(src) {
            sources.push(src);
            if sources.len() >= max_sources {
                break;
            }
        }
    }
    Ok(sources)
}

fn load_mutation_edges(
    path: &Path,
    edge_type: EdgeType,
    max_edges: usize,
) -> Result<Vec<MutationEdge>> {
    let mut rdr = csv::ReaderBuilder::new()
        .delimiter(b'|')
        .has_headers(true)
        .from_path(path)?;
    let mut edges = Vec::new();
    for rec in rdr.records() {
        let rec = rec?;
        let src: VertexId = rec
            .get(0)
            .context("missing source column")?
            .trim()
            .parse()?;
        let dst: VertexId = rec
            .get(1)
            .context("missing destination column")?
            .trim()
            .parse()?;
        edges.push(MutationEdge {
            src,
            dst,
            edge_type,
        });
        if edges.len() >= max_edges {
            break;
        }
        edges.push(MutationEdge {
            src: dst,
            dst: src,
            edge_type,
        });
        if edges.len() >= max_edges {
            break;
        }
    }
    Ok(edges)
}

fn resolve_under_input(input: &Path, path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        input.join(path)
    }
}

fn open_output(path: Option<&Path>) -> Result<BufWriter<Box<dyn Write>>> {
    match path {
        Some(path) => {
            if let Some(parent) = path.parent() {
                fs::create_dir_all(parent)?;
            }
            let file = OpenOptions::new()
                .create(true)
                .truncate(true)
                .write(true)
                .open(path)?;
            Ok(BufWriter::new(Box::new(file)))
        }
        None => Ok(BufWriter::new(Box::new(std::io::stdout()))),
    }
}

fn write_json_line<T: Serialize>(out: &mut BufWriter<Box<dyn Write>>, value: &T) -> Result<()> {
    serde_json::to_writer(&mut *out, value)?;
    out.write_all(b"\n")?;
    Ok(())
}

fn checkpoint_metric_delta(after: &Value, before: &Value) -> Value {
    json!({
        "storage": {
            "insert_ops": metric_delta_u64(after, before, &["storage", "insert_ops"]),
            "delete_ops": metric_delta_u64(after, before, &["storage", "delete_ops"]),
            "get_neighbors_ops": metric_delta_u64(after, before, &["storage", "get_neighbors_ops"]),
            "flush_count": metric_delta_u64(after, before, &["storage", "flush_count"]),
            "compaction_count": metric_delta_u64(after, before, &["storage", "compaction_count"]),
            "compaction_input_bytes": metric_delta_u64(after, before, &["storage", "compaction_input_bytes"]),
            "compaction_output_bytes": metric_delta_u64(after, before, &["storage", "compaction_output_bytes"]),
            "get_neighbors_latency": latency_delta(after, before, &["storage", "get_neighbors_latency"]),
            "insert_latency": latency_delta(after, before, &["storage", "insert_latency"]),
            "delete_latency": latency_delta(after, before, &["storage", "delete_latency"]),
            "flush_latency": latency_delta(after, before, &["storage", "flush_latency"]),
            "compaction_latency": latency_delta(after, before, &["storage", "compaction_latency"]),
        },
        "io": {
            "read_bytes": metric_delta_u64(after, before, &["io", "read_bytes"]),
            "write_bytes": metric_delta_u64(after, before, &["io", "write_bytes"]),
            "read_syscalls": metric_delta_u64(after, before, &["io", "read_syscalls"]),
            "write_syscalls": metric_delta_u64(after, before, &["io", "write_syscalls"]),
            "write_blocking_latency": latency_delta(after, before, &["io", "write_blocking_latency"]),
            "sync_blocking_latency": latency_delta(after, before, &["io", "sync_blocking_latency"]),
        },
        "csr": {
            "candidate_l0_segments": metric_delta_u64(after, before, &["csr", "candidate_l0_segments"]),
            "filter_passed_segments": metric_delta_u64(after, before, &["csr", "filter_passed_segments"]),
            "matched_l0_segments": metric_delta_u64(after, before, &["csr", "matched_l0_segments"]),
            "body_reads": metric_delta_u64(after, before, &["csr", "body_reads"]),
            "offset_reads": metric_delta_u64(after, before, &["csr", "offset_reads"]),
            "read_offsets_latency": latency_delta(after, before, &["csr", "read_offsets_latency"]),
            "get_neighbors_latency": latency_delta(after, before, &["csr", "get_neighbors_latency"]),
        }
    })
}

fn latency_delta(after: &Value, before: &Value, path: &[&str]) -> Value {
    json!({
        "count": metric_delta_u64_with_suffix(after, before, path, "count"),
        "sum_us": metric_delta_u64_with_suffix(after, before, path, "sum_us"),
        "max_us_observed": metric_path_u64_with_suffix(after, path, "max_us"),
        "p50_us_observed": metric_path_u64_with_suffix(after, path, "p50_us"),
        "p99_us_observed": metric_path_u64_with_suffix(after, path, "p99_us"),
    })
}

fn metric_delta_u64(after: &Value, before: &Value, path: &[&str]) -> u64 {
    metric_path_u64(after, path).saturating_sub(metric_path_u64(before, path))
}

fn metric_delta_u64_with_suffix(after: &Value, before: &Value, path: &[&str], suffix: &str) -> u64 {
    metric_path_u64_with_suffix(after, path, suffix)
        .saturating_sub(metric_path_u64_with_suffix(before, path, suffix))
}

fn metric_path_u64_with_suffix(metrics: &Value, path: &[&str], suffix: &str) -> u64 {
    let mut full_path = Vec::with_capacity(path.len() + 1);
    full_path.extend_from_slice(path);
    full_path.push(suffix);
    metric_path_u64(metrics, &full_path)
}

fn metric_path_u64(metrics: &Value, path: &[&str]) -> u64 {
    let mut current = metrics;
    for key in path {
        current = &current[*key];
    }
    current.as_u64().unwrap_or_default()
}

fn series_stats(values: &[f64]) -> SeriesStats {
    if values.is_empty() {
        return SeriesStats {
            count: 0,
            sum: 0.0,
            mean: 0.0,
            p50: 0.0,
            p99: 0.0,
            max: 0.0,
        };
    }
    let mut sorted = values.to_vec();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let sum = sorted.iter().sum::<f64>();
    SeriesStats {
        count: sorted.len(),
        sum,
        mean: sum / sorted.len() as f64,
        p50: percentile(&sorted, 0.50),
        p99: percentile(&sorted, 0.99),
        max: *sorted.last().unwrap_or(&0.0),
    }
}

fn percentile(sorted: &[f64], q: f64) -> f64 {
    let idx = ((sorted.len().saturating_sub(1)) as f64 * q).ceil() as usize;
    sorted[idx.min(sorted.len() - 1)]
}

fn snapshot_writer(counters: &WriterCounters) -> WriterSnapshot {
    WriterSnapshot {
        attempts: counters.attempts.load(Ordering::Relaxed),
        inserts: counters.inserts.load(Ordering::Relaxed),
        deletes: counters.deletes.load(Ordering::Relaxed),
        flushes: counters.flushes.load(Ordering::Relaxed),
        compactions: counters.compactions.load(Ordering::Relaxed),
        errors: counters.errors.load(Ordering::Relaxed),
        op_latency_sum_us: counters.op_latency_sum_us.load(Ordering::Relaxed),
        op_latency_max_us: counters.op_latency_max_us.load(Ordering::Relaxed),
        slow_ops: counters.slow_ops.load(Ordering::Relaxed),
    }
}

fn writer_delta_json(before: WriterSnapshot, after: WriterSnapshot) -> Value {
    json!({
        "attempts": after.attempts.saturating_sub(before.attempts),
        "inserts": after.inserts.saturating_sub(before.inserts),
        "deletes": after.deletes.saturating_sub(before.deletes),
        "flushes": after.flushes.saturating_sub(before.flushes),
        "compactions": after.compactions.saturating_sub(before.compactions),
        "errors": after.errors.saturating_sub(before.errors),
        "op_latency_sum_us": after.op_latency_sum_us.saturating_sub(before.op_latency_sum_us),
        "op_latency_max_us_observed": after.op_latency_max_us,
        "slow_ops": after.slow_ops.saturating_sub(before.slow_ops),
    })
}

fn writer_snapshot_json(snapshot: WriterSnapshot) -> Value {
    json!({
        "attempts": snapshot.attempts,
        "inserts": snapshot.inserts,
        "deletes": snapshot.deletes,
        "flushes": snapshot.flushes,
        "compactions": snapshot.compactions,
        "errors": snapshot.errors,
        "op_latency_sum_us": snapshot.op_latency_sum_us,
        "op_latency_max_us": snapshot.op_latency_max_us,
        "slow_ops": snapshot.slow_ops,
    })
}

fn fetch_max(target: &AtomicU64, value: u64) {
    let mut current = target.load(Ordering::Relaxed);
    while value > current {
        match target.compare_exchange(current, value, Ordering::Relaxed, Ordering::Relaxed) {
            Ok(_) => break,
            Err(next) => current = next,
        }
    }
}

fn unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

#[derive(Debug)]
struct Lcg {
    state: u64,
}

impl Lcg {
    fn new(seed: u64) -> Self {
        Self { state: seed.max(1) }
    }

    fn next_u64(&mut self) -> u64 {
        self.state = self
            .state
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        self.state
    }

    fn next_usize(&mut self, upper: usize) -> usize {
        (self.next_u64() as usize) % upper
    }
}
