use std::collections::HashSet;
use std::path::PathBuf;
use std::time::Instant;

use anyhow::Result;
use clap::{Parser, Subcommand};
use lsmgraph::config::{IoBackendKind, LsmGraphConfig};
use lsmgraph::graph::Engine;
use lsmgraph::loader::{import_person_knows, import_snb_topology, validate_person_knows};
use lsmgraph::snb::{
    import_snb_full, import_snb_updates, rebuild_snb_edge_props, start_dgs_compatible_server,
    validate_ic1_ic14, validate_ic_batch, validate_mixed_tugraph, SnbGraph,
};
use serde_json::json;

const DEFAULT_DATA: &str = "/data/WorkSpace/dgs/data/social_network_tugraph";
const DEFAULT_STORE: &str = "/data/WorkSpace/lsmgraph-rs/store/sf1";

#[derive(Parser)]
#[command(name = "lsmgraph", about = "LSMGraph storage prototype")]
struct Cli {
    #[arg(long, default_value = "blocking")]
    io_backend: IoBackendKind,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    Import {
        #[arg(long, default_value = DEFAULT_DATA)]
        input: PathBuf,
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
        #[arg(long, default_value = "person_knows")]
        relation: String,
        #[arg(long, default_value_t = 64 * 1024 * 1024)]
        memgraph_bytes: usize,
        #[arg(long, default_value_t = false)]
        compact: bool,
    },
    Neighbors {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
        #[arg(long)]
        src: u64,
    },
    Scan {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
    },
    Stats {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
    },
    Compact {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
    },
    ValidateKnows {
        #[arg(long, default_value = DEFAULT_DATA)]
        input: PathBuf,
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
        #[arg(long, default_value_t = 1024)]
        max_vertices: usize,
    },
    SnbValidate {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
        #[arg(
            long,
            default_value = "/data/WorkSpace/dgs/deps/ldbc_snb_interactive_impls/dgs/validation_params.csv"
        )]
        validation_params: PathBuf,
        #[arg(long, default_value_t = 100)]
        max_lines: usize,
    },
    SnbValidateBatch {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
        #[arg(
            long,
            default_value = "/data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/dgs/validation_splits"
        )]
        validation_dir: PathBuf,
        #[arg(long, default_value = "ic")]
        queries: String,
        #[arg(long, default_value_t = 100)]
        max_lines_per_query: usize,
    },
    SnbValidateMixed {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
        #[arg(
            long,
            default_value = "/data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/dgs/validation_params_tugraph.csv"
        )]
        validation_params: PathBuf,
        #[arg(long, default_value_t = 100)]
        max_lines: usize,
    },
    SnbServer {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
        #[arg(long, default_value = "127.0.0.1")]
        host: String,
        #[arg(long, default_value_t = 9090)]
        port: u16,
    },
    SnbCache {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
    },
    SnbProps {
        #[arg(long, default_value = DEFAULT_DATA)]
        input: PathBuf,
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
    },
    SnbUpdates {
        #[arg(long, default_value = DEFAULT_DATA)]
        input: PathBuf,
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
    },
    StorageBench {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
        #[arg(long, default_value_t = 100)]
        samples: usize,
        #[arg(long, default_value_t = true)]
        scan: bool,
    },
}

#[tokio::main(flavor = "multi_thread")]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    let io_backend = cli.io_backend;
    match cli.command {
        Command::Import {
            input,
            data_dir,
            relation,
            memgraph_bytes,
            compact,
        } => {
            let config = LsmGraphConfig::new(&data_dir)
                .with_memgraph_capacity(memgraph_bytes)
                .with_io_backend(io_backend);
            let engine = Engine::create(config).await?;
            let stats = match relation.as_str() {
                "person_knows" => import_person_knows(engine.clone(), &input).await?,
                "all-topology" | "topology" | "all" => {
                    import_snb_topology(engine.clone(), &input).await?
                }
                "snb-full" | "full" => import_snb_full(engine.clone(), &input, &data_dir).await?,
                _ => anyhow::bail!(
                    "unsupported --relation {relation}; use person_knows, all-topology, or snb-full"
                ),
            };
            if compact {
                engine.compact_l0_to_l1().await?;
            }
            println!(
                "{{\"input_rows\":{},\"directed_edges\":{},\"snapshot\":{}}}",
                stats.input_rows,
                stats.directed_edges,
                engine.current_snapshot()
            );
        }
        Command::Neighbors { data_dir, src } => {
            let engine =
                Engine::open(LsmGraphConfig::new(data_dir).with_io_backend(io_backend)).await?;
            let snapshot = engine.current_snapshot();
            let neighbors = engine.get_neighbors(src, snapshot).await?;
            println!("{}", serde_json::to_string_pretty(&neighbors)?);
        }
        Command::Scan { data_dir } => {
            let engine =
                Engine::open(LsmGraphConfig::new(data_dir).with_io_backend(io_backend)).await?;
            let snapshot = engine.current_snapshot();
            let edges = engine.scan_edges(snapshot).await?;
            println!(
                "{{\"snapshot\":{},\"directed_edges\":{}}}",
                snapshot,
                edges.len()
            );
        }
        Command::Stats { data_dir } => {
            let engine =
                Engine::open(LsmGraphConfig::new(data_dir).with_io_backend(io_backend)).await?;
            let levels = engine.live_file_count_by_level();
            let metrics = engine.metrics();
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "snapshot": engine.current_snapshot(),
                    "levels": levels,
                    "metrics": metrics.snapshot_json(),
                }))?
            );
        }
        Command::Compact { data_dir } => {
            let engine =
                Engine::open(LsmGraphConfig::new(data_dir).with_io_backend(io_backend)).await?;
            let meta = engine.compact_l0_to_l1().await?;
            println!("{}", serde_json::to_string_pretty(&meta)?);
        }
        Command::ValidateKnows {
            input,
            data_dir,
            max_vertices,
        } => {
            let engine =
                Engine::open(LsmGraphConfig::new(data_dir).with_io_backend(io_backend)).await?;
            let stats = validate_person_knows(engine, &input, max_vertices).await?;
            println!(
                "{{\"checked_vertices\":{},\"expected_directed_edges\":{},\"actual_directed_edges\":{}}}",
                stats.checked_vertices, stats.expected_directed_edges, stats.actual_directed_edges
            );
        }
        Command::SnbValidate {
            data_dir,
            validation_params,
            max_lines,
        } => {
            let engine =
                Engine::open(LsmGraphConfig::new(&data_dir).with_io_backend(io_backend)).await?;
            let report =
                validate_ic1_ic14(engine, &data_dir, &validation_params, max_lines).await?;
            println!("{}", serde_json::to_string_pretty(&report)?);
        }
        Command::SnbValidateBatch {
            data_dir,
            validation_dir,
            queries,
            max_lines_per_query,
        } => {
            let engine =
                Engine::open(LsmGraphConfig::new(&data_dir).with_io_backend(io_backend)).await?;
            let query_list = parse_query_list(&queries);
            let report = validate_ic_batch(
                engine,
                &data_dir,
                &validation_dir,
                &query_list,
                max_lines_per_query,
            )
            .await?;
            println!("{}", serde_json::to_string_pretty(&report)?);
        }
        Command::SnbValidateMixed {
            data_dir,
            validation_params,
            max_lines,
        } => {
            let engine =
                Engine::open(LsmGraphConfig::new(&data_dir).with_io_backend(io_backend)).await?;
            let report =
                validate_mixed_tugraph(engine, &data_dir, &validation_params, max_lines).await?;
            println!("{}", serde_json::to_string_pretty(&report)?);
        }
        Command::SnbServer {
            data_dir,
            host,
            port,
        } => {
            let total_started = Instant::now();
            eprintln!(
                "[snb-server] opening engine data_dir={}",
                data_dir.display()
            );
            let engine_started = Instant::now();
            let engine =
                Engine::open(LsmGraphConfig::new(&data_dir).with_io_backend(io_backend)).await?;
            eprintln!(
                "[snb-server] engine open complete elapsed_s={:.1}",
                engine_started.elapsed().as_secs_f64()
            );
            let snb_started = Instant::now();
            eprintln!("[snb-server] loading SNB vertices/properties/adjacency cache");
            let snb = SnbGraph::open(engine, &data_dir).await?;
            eprintln!(
                "[snb-server] SNB load complete elapsed_s={:.1} total_elapsed_s={:.1}",
                snb_started.elapsed().as_secs_f64(),
                total_started.elapsed().as_secs_f64()
            );
            eprintln!(
                "[snb-server] binding http adapter addr={host}:{port} total_elapsed_s={:.1}",
                total_started.elapsed().as_secs_f64()
            );
            start_dgs_compatible_server(snb, &format!("{host}:{port}")).await?;
        }
        Command::SnbCache { data_dir } => {
            let engine =
                Engine::open(LsmGraphConfig::new(&data_dir).with_io_backend(io_backend)).await?;
            let groups = SnbGraph::build_adjacency_cache(engine.clone(), &data_dir).await?;
            let metrics = engine.metrics();
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "data_dir": data_dir,
                    "adjacency_groups": groups,
                    "metrics": metrics.snapshot_json(),
                }))?
            );
        }
        Command::SnbProps { input, data_dir } => {
            let rows = rebuild_snb_edge_props(&input, &data_dir)?;
            println!(
                "{{\"data_dir\":\"{}\",\"input_rows\":{},\"edge_props\":\"{}\"}}",
                data_dir.display(),
                rows,
                data_dir.join("snb_edge_props.jsonl").display()
            );
        }
        Command::SnbUpdates { input, data_dir } => {
            let engine =
                Engine::open(LsmGraphConfig::new(&data_dir).with_io_backend(io_backend)).await?;
            let stats = import_snb_updates(engine.clone(), &input, &data_dir).await?;
            println!(
                "{{\"input_rows\":{},\"directed_edges\":{},\"snapshot\":{}}}",
                stats.input_rows,
                stats.directed_edges,
                engine.current_snapshot()
            );
        }
        Command::StorageBench {
            data_dir,
            samples,
            scan,
        } => {
            let engine =
                Engine::open(LsmGraphConfig::new(&data_dir).with_io_backend(io_backend)).await?;
            let snapshot = engine.current_snapshot();
            let started = Instant::now();
            let edges = engine.scan_edges(snapshot).await?;
            let scan_elapsed_ms = started.elapsed().as_millis();
            let mut srcs = Vec::new();
            let mut seen = HashSet::new();
            let stride = (edges.len() / samples.max(1)).max(1);
            for edge in edges.iter().step_by(stride) {
                if seen.insert(edge.src) {
                    srcs.push(edge.src);
                    if srcs.len() >= samples {
                        break;
                    }
                }
            }
            let mut neighbor_edges = 0usize;
            let neighbor_started = Instant::now();
            for src in &srcs {
                neighbor_edges += engine.get_neighbors(*src, snapshot).await?.len();
            }
            let get_neighbors_elapsed_ms = neighbor_started.elapsed().as_millis();
            let metrics = engine.metrics();
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "data_dir": data_dir,
                    "snapshot": snapshot,
                    "scan_requested": scan,
                    "scan_edges": edges.len(),
                    "scan_elapsed_ms": scan_elapsed_ms,
                    "sampled_vertices": srcs.len(),
                    "neighbor_edges": neighbor_edges,
                    "get_neighbors_elapsed_ms": get_neighbors_elapsed_ms,
                    "levels": engine.live_file_count_by_level(),
                    "metrics": metrics.snapshot_json(),
                }))?
            );
        }
    }
    Ok(())
}

fn parse_query_list(raw: &str) -> Vec<String> {
    match raw.trim().to_ascii_lowercase().as_str() {
        "ic" | "all-ic" | "ic1-ic14" => (1..=14).map(|i| format!("ic{i}")).collect(),
        "is" | "all-is" | "is1-is7" => (1..=7).map(|i| format!("is{i}")).collect(),
        "read" | "reads" | "all-read" | "all-reads" => (1..=14)
            .map(|i| format!("ic{i}"))
            .chain((1..=7).map(|i| format!("is{i}")))
            .collect(),
        other => other
            .split(',')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(|s| s.to_ascii_lowercase())
            .collect(),
    }
}
