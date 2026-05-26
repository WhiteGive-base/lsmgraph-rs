use std::path::PathBuf;
use std::sync::atomic::Ordering;

use anyhow::Result;
use clap::{Parser, Subcommand};
use lsmgraph::config::{IoBackendKind, LsmGraphConfig};
use lsmgraph::graph::Engine;
use lsmgraph::loader::{import_person_knows, import_snb_topology, validate_person_knows};
use lsmgraph::snb::{import_snb_full, validate_ic1_ic2, SnbGraph};

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
    SnbCache {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
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
                "{{\"snapshot\":{},\"levels\":{:?},\"read_bytes\":{},\"write_bytes\":{},\"flush_count\":{},\"compaction_count\":{}}}",
                engine.current_snapshot(),
                levels,
                metrics.read_bytes.load(Ordering::Relaxed),
                metrics.write_bytes.load(Ordering::Relaxed),
                metrics.flush_count.load(Ordering::Relaxed),
                metrics.compaction_count.load(Ordering::Relaxed)
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
            let report = validate_ic1_ic2(engine, &data_dir, &validation_params, max_lines).await?;
            println!("{}", serde_json::to_string_pretty(&report)?);
        }
        Command::SnbCache { data_dir } => {
            let engine =
                Engine::open(LsmGraphConfig::new(&data_dir).with_io_backend(io_backend)).await?;
            let groups = SnbGraph::build_adjacency_cache(engine, &data_dir).await?;
            println!(
                "{{\"data_dir\":\"{}\",\"adjacency_groups\":{}}}",
                data_dir.display(),
                groups
            );
        }
    }
    Ok(())
}
