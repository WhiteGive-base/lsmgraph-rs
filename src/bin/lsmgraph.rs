use std::collections::HashMap;
use std::fs;
use std::path::PathBuf;
use std::time::Instant;

use anyhow::Result;
use clap::{Parser, Subcommand};
use lsmgraph::base_graph::{build_from_snb, BuildConfig, IoConfig};
use lsmgraph::config::{IoBackendKind, L0LayoutPolicy, LsmGraphConfig};
use lsmgraph::graph::Engine;
use lsmgraph::loader::{import_person_knows, import_snb_topology, validate_person_knows};
use lsmgraph::snb::{
    import_snb_full, import_snb_updates, rebuild_snb_edge_props, start_dgs_compatible_server,
    validate_ic1_ic14_dynamic, validate_ic_batch_dynamic, validate_mixed_tugraph_dynamic, SnbGraph,
};
use lsmgraph::types::UNKNOWN_SOURCE_LABEL;
use lsmgraph::{
    DegreeClass, DynamicGraphView, EdgeRecord, GraphAccessSignature, NewPropertyEntry,
    PropertyOwner,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

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
        #[arg(long, default_value_t = false)]
        auto_compact: bool,
        #[arg(long, default_value_t = 0)]
        schema_epoch: u64,
        #[arg(long, default_value_t = false)]
        graph_aware_l0: bool,
        #[arg(long, default_value = "naive")]
        l0_layout: L0LayoutPolicy,
        #[arg(long, default_value_t = 4 * 1024 * 1024)]
        semantic_budget_min_edge_type_bytes: usize,
        #[arg(long, default_value_t = 1.0e308)]
        semantic_budget_min_edge_type_score: f64,
        #[arg(long, default_value_t = 4.0)]
        semantic_budget_core_edge_weight: f64,
        #[arg(long, default_value_t = 2.0)]
        semantic_budget_reverse_core_edge_weight: f64,
        #[arg(long, default_value_t = 0.5)]
        semantic_budget_other_edge_weight: f64,
        #[arg(long)]
        semantic_budget_max_extra_l0_files: Option<usize>,
        #[arg(long, value_delimiter = ',', allow_hyphen_values = true)]
        semantic_budget_edge_type_allowlist: Vec<i32>,
        #[arg(long, default_value_t = 4 * 1024 * 1024)]
        semantic_budget_min_exact_bytes: usize,
        #[arg(long, default_value_t = 1.0)]
        semantic_budget_min_benefit_score: f64,
        #[arg(long, default_value_t = 1.0)]
        semantic_budget_degree_weight: f64,
    },
    BaseBuild {
        #[arg(long, default_value = DEFAULT_DATA)]
        input: PathBuf,
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
    },
    DynamicStats {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
    },
    Neighbors {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
        #[arg(long)]
        src: u64,
        #[arg(long)]
        edge_type: Option<i32>,
    },
    Scan {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
    },
    Stats {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
    },
    SchemaShow {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
    },
    SchemaEvolutionReport {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
    },
    SchemaAddVertexLabel {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
        #[arg(long)]
        id: i32,
        #[arg(long)]
        name: String,
    },
    SchemaAddEdgeLabel {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
        #[arg(long)]
        id: i32,
        #[arg(long)]
        name: String,
        #[arg(long)]
        src_label_id: i32,
        #[arg(long)]
        dst_label_id: i32,
    },
    SchemaAddProperty {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
        #[arg(long)]
        id: u32,
        #[arg(long)]
        owner_kind: String,
        #[arg(long)]
        owner_id: i32,
        #[arg(long)]
        name: String,
        #[arg(long)]
        logical_type: String,
        #[arg(long)]
        physical_encoding: String,
        #[arg(long, default_value_t = 1)]
        encoding_version: u16,
        #[arg(long, default_value = "null")]
        default_or_null_rule: String,
    },
    SchemaAliasVertexLabel {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
        #[arg(long)]
        id: i32,
        #[arg(long)]
        name: String,
        #[arg(long)]
        canonical_id: i32,
    },
    SchemaAliasEdgeLabel {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
        #[arg(long)]
        id: i32,
        #[arg(long)]
        name: String,
        #[arg(long)]
        canonical_id: i32,
    },
    SchemaAliasProperty {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
        #[arg(long)]
        id: u32,
        #[arg(long)]
        name: String,
        #[arg(long)]
        canonical_id: u32,
    },
    SchemaDropVertexLabel {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
        #[arg(long)]
        id: i32,
    },
    SchemaDropEdgeLabel {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
        #[arg(long)]
        id: i32,
    },
    SchemaDropProperty {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
        #[arg(long)]
        id: u32,
    },
    SchemaChangePropertyEncoding {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
        #[arg(long)]
        id: u32,
        #[arg(long)]
        logical_type: String,
        #[arg(long)]
        physical_encoding: String,
        #[arg(long, default_value_t = 1)]
        encoding_version: u16,
        #[arg(long, default_value = "null")]
        default_or_null_rule: String,
    },
    Compact {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
        #[arg(long, default_value_t = false)]
        auto_pick: bool,
        #[arg(long)]
        src_label: Option<i32>,
        #[arg(long)]
        edge_type: Option<i32>,
        #[arg(long)]
        min_src: Option<u64>,
        #[arg(long)]
        max_src: Option<u64>,
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
        #[arg(long)]
        edge_type: Option<i32>,
        #[arg(long, value_delimiter = ',')]
        edge_types: Vec<i32>,
        #[arg(long, default_value_t = false)]
        semantic_degree_hint: bool,
        #[arg(long)]
        sample_plan_in: Option<PathBuf>,
        #[arg(long)]
        sample_plan_out: Option<PathBuf>,
        #[arg(long, default_value_t = true)]
        scan: bool,
        #[arg(long, default_value_t = false)]
        auto_compact: bool,
        #[arg(long, default_value_t = 10)]
        ra_min_queries: u64,
        #[arg(long, default_value_t = 10.0)]
        ra_min_score: f64,
        #[arg(long, default_value_t = 2)]
        ra_min_l0_segments: usize,
    },
    NeighborCompare {
        #[arg(long)]
        left_data_dir: PathBuf,
        #[arg(long)]
        right_data_dir: PathBuf,
        #[arg(long)]
        sample_plan: PathBuf,
        #[arg(long, default_value_t = false)]
        right_semantic_degree_hint: bool,
        #[arg(long, default_value_t = 1)]
        max_mismatches: usize,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct StorageBenchSamplePlan {
    version: u32,
    source: String,
    samples_per_edge_type: usize,
    semantic_degree_hint: bool,
    entries: Vec<StorageBenchSampleEntry>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct StorageBenchSampleEntry {
    edge_type: Option<i32>,
    candidate_edges_for_sampling: usize,
    candidate_sources_for_sampling: usize,
    samples: Vec<StorageBenchSample>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct StorageBenchSample {
    src: u64,
    degree: u64,
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
            auto_compact,
            schema_epoch,
            graph_aware_l0,
            l0_layout,
            semantic_budget_min_edge_type_bytes,
            semantic_budget_min_edge_type_score,
            semantic_budget_core_edge_weight,
            semantic_budget_reverse_core_edge_weight,
            semantic_budget_other_edge_weight,
            semantic_budget_max_extra_l0_files,
            semantic_budget_edge_type_allowlist,
            semantic_budget_min_exact_bytes,
            semantic_budget_min_benefit_score,
            semantic_budget_degree_weight,
        } => {
            if matches!(relation.as_str(), "snb-base" | "base" | "base-graph") {
                let output_dir = data_dir.join("base_graph");
                let config = BuildConfig::for_output(&output_dir);
                let stats = build_from_snb(&input, &output_dir, &config)?;
                println!("{}", serde_json::to_string_pretty(&stats)?);
                return Ok(());
            }
            let l0_layout = if graph_aware_l0 {
                L0LayoutPolicy::Schema
            } else {
                l0_layout
            };
            let config = LsmGraphConfig::new(&data_dir)
                .with_memgraph_capacity(memgraph_bytes)
                .with_io_backend(io_backend)
                .with_auto_compaction(auto_compact)
                .with_schema_epoch(schema_epoch)
                .with_l0_layout(l0_layout)
                .with_semantic_budget_min_edge_type_bytes(semantic_budget_min_edge_type_bytes)
                .with_semantic_budget_min_edge_type_score(semantic_budget_min_edge_type_score)
                .with_semantic_budget_core_edge_weight(semantic_budget_core_edge_weight)
                .with_semantic_budget_reverse_core_edge_weight(
                    semantic_budget_reverse_core_edge_weight,
                )
                .with_semantic_budget_other_edge_weight(semantic_budget_other_edge_weight)
                .with_semantic_budget_max_extra_l0_files(semantic_budget_max_extra_l0_files)
                .with_semantic_budget_edge_type_allowlist(semantic_budget_edge_type_allowlist)
                .with_semantic_budget_min_exact_bytes(semantic_budget_min_exact_bytes)
                .with_semantic_budget_min_benefit_score(semantic_budget_min_benefit_score)
                .with_semantic_budget_degree_weight(semantic_budget_degree_weight);
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
        Command::BaseBuild { input, data_dir } => {
            let output_dir = data_dir.join("base_graph");
            let config = BuildConfig::for_output(&output_dir);
            let stats = build_from_snb(&input, &output_dir, &config)?;
            println!("{}", serde_json::to_string_pretty(&stats)?);
        }
        Command::DynamicStats { data_dir } => {
            let view = DynamicGraphView::open(&data_dir, IoConfig::default(), io_backend).await?;
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "data_dir": data_dir,
                    "base_graph": view.base().map(|base| json!({
                        "base_dir": base.base_dir(),
                        "vertex_labels": base.catalog().vertex_labels,
                        "csr_count": base.catalog().csr_adjacencies.len(),
                        "single_column_count": base.catalog().single_columns.len(),
                        "derived_column_count": base.catalog().derived_columns.len(),
                    })),
                    "delta": view.delta().map(|delta| json!({
                        "dir": delta.dir(),
                        "snapshot": delta.current_snapshot(),
                    })),
                }))?
            );
        }
        Command::Neighbors {
            data_dir,
            src,
            edge_type,
        } => {
            let engine =
                Engine::open(LsmGraphConfig::new(data_dir).with_io_backend(io_backend)).await?;
            let snapshot = engine.current_snapshot();
            let neighbors = if let Some(edge_type) = edge_type {
                engine.get_neighbors_typed(src, edge_type, snapshot).await?
            } else {
                engine.get_neighbors(src, snapshot).await?
            };
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
        Command::SchemaShow { data_dir } => {
            let engine =
                Engine::open(LsmGraphConfig::new(data_dir).with_io_backend(io_backend)).await?;
            println!(
                "{}",
                serde_json::to_string_pretty(&engine.schema_catalog_snapshot())?
            );
        }
        Command::SchemaEvolutionReport { data_dir } => {
            let engine =
                Engine::open(LsmGraphConfig::new(data_dir).with_io_backend(io_backend)).await?;
            println!(
                "{}",
                serde_json::to_string_pretty(&engine.schema_evolution_report())?
            );
        }
        Command::SchemaAddVertexLabel { data_dir, id, name } => {
            let engine =
                Engine::open(LsmGraphConfig::new(data_dir).with_io_backend(io_backend)).await?;
            let epoch = engine.add_schema_vertex_label(id, name)?;
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "current_epoch": epoch,
                    "catalog": engine.schema_catalog_snapshot(),
                }))?
            );
        }
        Command::SchemaAddEdgeLabel {
            data_dir,
            id,
            name,
            src_label_id,
            dst_label_id,
        } => {
            let engine =
                Engine::open(LsmGraphConfig::new(data_dir).with_io_backend(io_backend)).await?;
            let epoch = engine.add_schema_edge_label(id, name, src_label_id, dst_label_id)?;
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "current_epoch": epoch,
                    "catalog": engine.schema_catalog_snapshot(),
                }))?
            );
        }
        Command::SchemaAddProperty {
            data_dir,
            id,
            owner_kind,
            owner_id,
            name,
            logical_type,
            physical_encoding,
            encoding_version,
            default_or_null_rule,
        } => {
            let owner = match owner_kind.as_str() {
                "vertex-label" | "vertex_label" | "vertex" => PropertyOwner::VertexLabel(owner_id),
                "edge-label" | "edge_label" | "edge" => PropertyOwner::EdgeLabel(owner_id),
                _ => anyhow::bail!(
                    "unsupported --owner-kind {owner_kind}; use vertex-label or edge-label"
                ),
            };
            let engine =
                Engine::open(LsmGraphConfig::new(data_dir).with_io_backend(io_backend)).await?;
            let epoch = engine.add_schema_property(NewPropertyEntry {
                id,
                owner,
                name,
                logical_type,
                physical_encoding,
                encoding_version,
                default_or_null_rule,
            })?;
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "current_epoch": epoch,
                    "catalog": engine.schema_catalog_snapshot(),
                }))?
            );
        }
        Command::SchemaAliasVertexLabel {
            data_dir,
            id,
            name,
            canonical_id,
        } => {
            let engine =
                Engine::open(LsmGraphConfig::new(data_dir).with_io_backend(io_backend)).await?;
            let epoch = engine.alias_schema_vertex_label(id, name, canonical_id)?;
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "current_epoch": epoch,
                    "catalog": engine.schema_catalog_snapshot(),
                }))?
            );
        }
        Command::SchemaAliasEdgeLabel {
            data_dir,
            id,
            name,
            canonical_id,
        } => {
            let engine =
                Engine::open(LsmGraphConfig::new(data_dir).with_io_backend(io_backend)).await?;
            let epoch = engine.alias_schema_edge_label(id, name, canonical_id)?;
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "current_epoch": epoch,
                    "catalog": engine.schema_catalog_snapshot(),
                }))?
            );
        }
        Command::SchemaAliasProperty {
            data_dir,
            id,
            name,
            canonical_id,
        } => {
            let engine =
                Engine::open(LsmGraphConfig::new(data_dir).with_io_backend(io_backend)).await?;
            let epoch = engine.alias_schema_property(id, name, canonical_id)?;
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "current_epoch": epoch,
                    "catalog": engine.schema_catalog_snapshot(),
                }))?
            );
        }
        Command::SchemaDropVertexLabel { data_dir, id } => {
            let engine =
                Engine::open(LsmGraphConfig::new(data_dir).with_io_backend(io_backend)).await?;
            let epoch = engine.drop_schema_vertex_label(id)?;
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "current_epoch": epoch,
                    "catalog": engine.schema_catalog_snapshot(),
                }))?
            );
        }
        Command::SchemaDropEdgeLabel { data_dir, id } => {
            let engine =
                Engine::open(LsmGraphConfig::new(data_dir).with_io_backend(io_backend)).await?;
            let epoch = engine.drop_schema_edge_label(id)?;
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "current_epoch": epoch,
                    "catalog": engine.schema_catalog_snapshot(),
                }))?
            );
        }
        Command::SchemaDropProperty { data_dir, id } => {
            let engine =
                Engine::open(LsmGraphConfig::new(data_dir).with_io_backend(io_backend)).await?;
            let epoch = engine.drop_schema_property(id)?;
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "current_epoch": epoch,
                    "catalog": engine.schema_catalog_snapshot(),
                }))?
            );
        }
        Command::SchemaChangePropertyEncoding {
            data_dir,
            id,
            logical_type,
            physical_encoding,
            encoding_version,
            default_or_null_rule,
        } => {
            let engine =
                Engine::open(LsmGraphConfig::new(data_dir).with_io_backend(io_backend)).await?;
            let epoch = engine.change_schema_property_encoding(
                id,
                logical_type,
                physical_encoding,
                encoding_version,
                default_or_null_rule,
            )?;
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "current_epoch": epoch,
                    "catalog": engine.schema_catalog_snapshot(),
                }))?
            );
        }
        Command::Compact {
            data_dir,
            auto_pick,
            src_label,
            edge_type,
            min_src,
            max_src,
        } => {
            let engine =
                Engine::open(LsmGraphConfig::new(data_dir).with_io_backend(io_backend)).await?;
            if auto_pick {
                let decision = engine.compact_best_l0_partition_by_score().await?;
                println!("{}", serde_json::to_string_pretty(&decision)?);
            } else if src_label.is_some()
                || edge_type.is_some()
                || min_src.is_some()
                || max_src.is_some()
            {
                let src_label = src_label.unwrap_or(UNKNOWN_SOURCE_LABEL);
                let outputs = match (min_src, max_src) {
                    (Some(min_src), Some(max_src)) => {
                        engine
                            .compact_l0_range_to_l1(src_label, edge_type, min_src, max_src)
                            .await?
                    }
                    (None, None) => {
                        engine
                            .compact_l0_partition_to_l1(src_label, edge_type)
                            .await?
                    }
                    _ => anyhow::bail!("--min-src and --max-src must be supplied together"),
                };
                println!(
                    "{}",
                    serde_json::to_string_pretty(&json!({
                        "output_count": outputs.len(),
                        "outputs": outputs,
                    }))?
                );
            } else {
                let meta = engine.compact_l0_to_l1().await?;
                println!("{}", serde_json::to_string_pretty(&meta)?);
            }
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
            let report = validate_ic1_ic14_dynamic(
                &data_dir,
                IoConfig::default(),
                io_backend,
                &validation_params,
                max_lines,
            )
            .await?;
            println!("{}", serde_json::to_string_pretty(&report)?);
        }
        Command::SnbValidateBatch {
            data_dir,
            validation_dir,
            queries,
            max_lines_per_query,
        } => {
            let query_list = parse_query_list(&queries);
            let report = validate_ic_batch_dynamic(
                &data_dir,
                IoConfig::default(),
                io_backend,
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
            let report = validate_mixed_tugraph_dynamic(
                &data_dir,
                IoConfig::default(),
                io_backend,
                &validation_params,
                max_lines,
            )
            .await?;
            println!("{}", serde_json::to_string_pretty(&report)?);
        }
        Command::SnbServer {
            data_dir,
            host,
            port,
        } => {
            let total_started = Instant::now();
            eprintln!(
                "[snb-server] opening DynamicGraphView data_dir={}",
                data_dir.display()
            );
            let view_started = Instant::now();
            let snb = SnbGraph::open_dynamic(&data_dir, IoConfig::default(), io_backend).await?;
            eprintln!(
                "[snb-server] DynamicGraphView open complete elapsed_s={:.1} total_elapsed_s={:.1}",
                view_started.elapsed().as_secs_f64(),
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
            edge_type,
            edge_types,
            semantic_degree_hint,
            sample_plan_in,
            sample_plan_out,
            scan,
            auto_compact,
            ra_min_queries,
            ra_min_score,
            ra_min_l0_segments,
        } => {
            let mut config = LsmGraphConfig::new(&data_dir).with_io_backend(io_backend);
            config.l0_ra_min_queries = ra_min_queries;
            config.l0_ra_min_score = ra_min_score;
            config.l0_ra_min_l0_segments = ra_min_l0_segments;
            let engine = Engine::open(config).await?;
            let snapshot = engine.current_snapshot();
            let metrics = engine.metrics();
            let cli_requested_edge_types: Vec<Option<i32>> = if edge_types.is_empty() {
                vec![edge_type]
            } else {
                edge_types.iter().copied().map(Some).collect()
            };
            let mut scan_edges = 0usize;
            let mut scan_elapsed_ms = 0u128;
            let mut scan_metrics = Value::Null;
            let mut scan_summary = Value::Null;
            let sample_plan = if let Some(path) = &sample_plan_in {
                let text = fs::read_to_string(path)?;
                let mut plan = serde_json::from_str::<StorageBenchSamplePlan>(&text)?;
                plan.source = "file".to_string();
                plan
            } else {
                let started = Instant::now();
                let edges = engine.scan_edges(snapshot).await?;
                scan_elapsed_ms = started.elapsed().as_millis();
                scan_edges = edges.len();
                scan_metrics = metrics.snapshot_json();
                scan_summary = storage_bench_metric_summary(&scan_metrics);
                build_storage_bench_sample_plan(
                    &edges,
                    &cli_requested_edge_types,
                    samples,
                    semantic_degree_hint,
                )
            };
            if let Some(path) = &sample_plan_out {
                if let Some(parent) = path
                    .parent()
                    .filter(|parent| !parent.as_os_str().is_empty())
                {
                    fs::create_dir_all(parent)?;
                }
                fs::write(path, serde_json::to_vec_pretty(&sample_plan)?)?;
            }
            let mut benchmarks = Vec::new();
            for entry in &sample_plan.entries {
                let requested_edge_type = entry.edge_type;
                metrics.reset();
                let degree_by_src: HashMap<u64, u64> = entry
                    .samples
                    .iter()
                    .map(|sample| (sample.src, sample.degree))
                    .collect();
                let srcs: Vec<u64> = entry.samples.iter().map(|sample| sample.src).collect();
                let mut neighbor_edges = 0usize;
                let neighbor_started = Instant::now();
                for src in &srcs {
                    let neighbors = if let Some(edge_type) = requested_edge_type {
                        if semantic_degree_hint {
                            let degree = degree_by_src.get(src).copied().unwrap_or(0);
                            let signature =
                                GraphAccessSignature::neighbor_scan(*src, Some(edge_type))
                                    .with_degree_class(DegreeClass::from_max_degree(degree));
                            engine
                                .get_neighbors_by_signature(signature, snapshot)
                                .await?
                        } else {
                            engine
                                .get_neighbors_typed(*src, edge_type, snapshot)
                                .await?
                        }
                    } else {
                        engine.get_neighbors(*src, snapshot).await?
                    };
                    neighbor_edges += neighbors.len();
                }
                let get_neighbors_elapsed_ms = neighbor_started.elapsed().as_millis();
                let neighbor_metrics = metrics.snapshot_json();
                let neighbor_summary = storage_bench_metric_summary(&neighbor_metrics);
                let auto_compaction = if auto_compact {
                    engine.compact_best_l0_partition_by_score().await?
                } else {
                    None
                };
                let post_auto_compaction_metrics = if auto_compact {
                    Some(metrics.snapshot_json())
                } else {
                    None
                };
                benchmarks.push(json!({
                    "edge_type": requested_edge_type,
                    "candidate_edges_for_sampling": entry.candidate_edges_for_sampling,
                    "candidate_sources_for_sampling": entry.candidate_sources_for_sampling,
                    "semantic_degree_hint": semantic_degree_hint,
                    "sampled_vertices": srcs.len(),
                    "sampled_srcs": srcs,
                    "sample_degrees": entry.samples,
                    "neighbor_edges": neighbor_edges,
                    "get_neighbors_elapsed_ms": get_neighbors_elapsed_ms,
                    "auto_compaction": auto_compaction,
                    "levels_after_run": engine.live_file_count_by_level(),
                    "neighbor_summary": neighbor_summary,
                    "neighbor_metrics": neighbor_metrics,
                    "post_auto_compaction_metrics": post_auto_compaction_metrics,
                }));
            }
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "data_dir": data_dir,
                    "snapshot": snapshot,
                    "edge_type": edge_type,
                    "edge_types": edge_types,
                    "semantic_degree_hint": semantic_degree_hint,
                    "sample_plan_in": sample_plan_in,
                    "sample_plan_out": sample_plan_out,
                    "sample_plan_version": sample_plan.version,
                    "scan_requested": scan && sample_plan.source == "scan",
                    "scan_edges": scan_edges,
                    "scan_elapsed_ms": scan_elapsed_ms,
                    "levels": engine.live_file_count_by_level(),
                    "scan_summary": scan_summary,
                    "scan_metrics": scan_metrics,
                    "benchmarks": benchmarks,
                }))?
            );
        }
        Command::NeighborCompare {
            left_data_dir,
            right_data_dir,
            sample_plan,
            right_semantic_degree_hint,
            max_mismatches,
        } => {
            let plan: StorageBenchSamplePlan =
                serde_json::from_str(&fs::read_to_string(&sample_plan)?)?;
            let left =
                Engine::open(LsmGraphConfig::new(&left_data_dir).with_io_backend(io_backend))
                    .await?;
            let right =
                Engine::open(LsmGraphConfig::new(&right_data_dir).with_io_backend(io_backend))
                    .await?;
            let left_snapshot = left.current_snapshot();
            let right_snapshot = right.current_snapshot();
            let started = Instant::now();
            let mut checked = 0usize;
            let mut passed = 0usize;
            let mut mismatches = 0usize;
            let mut left_edges_total = 0usize;
            let mut right_edges_total = 0usize;
            let mut first_mismatch = None;

            'outer: for entry in &plan.entries {
                for sample in &entry.samples {
                    checked += 1;
                    let mut left_edges = if let Some(edge_type) = entry.edge_type {
                        left.get_neighbors_typed(sample.src, edge_type, left_snapshot)
                            .await?
                    } else {
                        left.get_neighbors(sample.src, left_snapshot).await?
                    };
                    let mut right_edges = if let Some(edge_type) = entry.edge_type {
                        if right_semantic_degree_hint {
                            let signature =
                                GraphAccessSignature::neighbor_scan(sample.src, Some(edge_type))
                                    .with_degree_class(DegreeClass::from_max_degree(sample.degree));
                            right
                                .get_neighbors_by_signature(signature, right_snapshot)
                                .await?
                        } else {
                            right
                                .get_neighbors_typed(sample.src, edge_type, right_snapshot)
                                .await?
                        }
                    } else {
                        right.get_neighbors(sample.src, right_snapshot).await?
                    };
                    sort_edge_records(&mut left_edges);
                    sort_edge_records(&mut right_edges);
                    left_edges_total += left_edges.len();
                    right_edges_total += right_edges.len();
                    if left_edges == right_edges {
                        passed += 1;
                    } else {
                        mismatches += 1;
                        if first_mismatch.is_none() {
                            first_mismatch = Some(json!({
                                "edge_type": entry.edge_type,
                                "src": sample.src,
                                "degree": sample.degree,
                                "left_count": left_edges.len(),
                                "right_count": right_edges.len(),
                                "left_preview": edge_preview(&left_edges),
                                "right_preview": edge_preview(&right_edges),
                            }));
                        }
                        if mismatches >= max_mismatches {
                            break 'outer;
                        }
                    }
                }
            }

            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "left_data_dir": left_data_dir,
                    "right_data_dir": right_data_dir,
                    "sample_plan": sample_plan,
                    "right_semantic_degree_hint": right_semantic_degree_hint,
                    "left_snapshot": left_snapshot,
                    "right_snapshot": right_snapshot,
                    "checked": checked,
                    "passed": passed,
                    "mismatches": mismatches,
                    "left_edges_total": left_edges_total,
                    "right_edges_total": right_edges_total,
                    "elapsed_ms": started.elapsed().as_millis(),
                    "first_mismatch": first_mismatch,
                }))?
            );
        }
    }
    Ok(())
}

fn sort_edge_records(edges: &mut [EdgeRecord]) {
    edges.sort_unstable_by_key(|edge| {
        (
            edge.src,
            edge.dst,
            edge.edge_type,
            edge.ts,
            edge.marker as u8,
        )
    });
}

fn edge_preview(edges: &[EdgeRecord]) -> Vec<Value> {
    edges
        .iter()
        .take(20)
        .map(|edge| {
            json!({
                "src": edge.src,
                "dst": edge.dst,
                "edge_type": edge.edge_type,
                "ts": edge.ts,
                "marker": edge.marker,
            })
        })
        .collect()
}

fn build_storage_bench_sample_plan(
    edges: &[EdgeRecord],
    requested_edge_types: &[Option<i32>],
    samples: usize,
    semantic_degree_hint: bool,
) -> StorageBenchSamplePlan {
    let mut entries = Vec::new();
    for requested_edge_type in requested_edge_types {
        let candidate_edges = edges
            .iter()
            .filter(|edge| {
                requested_edge_type
                    .map(|wanted| edge.edge_type == wanted)
                    .unwrap_or(true)
            })
            .count();
        let mut degree_by_src = HashMap::new();
        if semantic_degree_hint {
            for edge in edges {
                if requested_edge_type
                    .map(|wanted| edge.edge_type == wanted)
                    .unwrap_or(true)
                {
                    *degree_by_src.entry(edge.src).or_default() += 1;
                }
            }
        }
        let mut candidate_srcs: Vec<_> = edges
            .iter()
            .filter(|edge| {
                requested_edge_type
                    .map(|wanted| edge.edge_type == wanted)
                    .unwrap_or(true)
            })
            .map(|edge| edge.src)
            .collect();
        candidate_srcs.sort_unstable();
        candidate_srcs.dedup();
        let candidate_sources = candidate_srcs.len();
        let stride = (candidate_sources / samples.max(1)).max(1);
        let mut sampled_srcs = Vec::new();
        for (idx, src) in candidate_srcs.iter().copied().enumerate() {
            if idx % stride == 0 {
                sampled_srcs.push(src);
                if sampled_srcs.len() >= samples {
                    break;
                }
            }
        }
        if sampled_srcs.len() < samples {
            for src in candidate_srcs.iter().copied() {
                if !sampled_srcs.contains(&src) {
                    sampled_srcs.push(src);
                }
                if sampled_srcs.len() >= samples {
                    break;
                }
            }
        }
        let sampled = sampled_srcs
            .into_iter()
            .map(|src| StorageBenchSample {
                src,
                degree: degree_by_src.get(&src).copied().unwrap_or(0),
            })
            .collect();
        entries.push(StorageBenchSampleEntry {
            edge_type: *requested_edge_type,
            candidate_edges_for_sampling: candidate_edges,
            candidate_sources_for_sampling: candidate_sources,
            samples: sampled,
        });
    }
    StorageBenchSamplePlan {
        version: 1,
        source: "scan".to_string(),
        samples_per_edge_type: samples,
        semantic_degree_hint,
        entries,
    }
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

fn storage_bench_metric_summary(metrics: &Value) -> Value {
    json!({
        "candidate_l0_segments": metric_u64(metrics, &["csr", "candidate_l0_segments"]),
        "range_filtered_segments": metric_u64(metrics, &["csr", "range_filtered_segments"]),
        "bloom_filtered_segments": metric_u64(metrics, &["csr", "bloom_filtered_segments"]),
        "filter_passed_segments": metric_u64(metrics, &["csr", "filter_passed_segments"]),
        "matched_l0_segments": metric_u64(metrics, &["csr", "matched_l0_segments"]),
        "header_reads": metric_u64(metrics, &["csr", "header_reads"]),
        "offset_reads": metric_u64(metrics, &["csr", "offset_reads"]),
        "body_reads": metric_u64(metrics, &["csr", "body_reads"]),
        "body_bytes": metric_u64(metrics, &["csr", "body_bytes"]),
        "read_syscalls": metric_u64(metrics, &["io", "read_syscalls"]),
        "read_bytes": metric_u64(metrics, &["io", "read_bytes"]),
        "get_neighbors_ops": metric_u64(metrics, &["storage", "get_neighbors_ops"]),
        "get_neighbors_avg_us": metric_u64(metrics, &["storage", "get_neighbors_latency", "avg_us"]),
        "get_neighbors_p50_us": metric_u64(metrics, &["storage", "get_neighbors_latency", "p50_us"]),
        "get_neighbors_p90_us": metric_u64(metrics, &["storage", "get_neighbors_latency", "p90_us"]),
        "get_neighbors_p99_us": metric_u64(metrics, &["storage", "get_neighbors_latency", "p99_us"]),
        "csr_get_neighbors_avg_us": metric_u64(metrics, &["csr", "get_neighbors_latency", "avg_us"]),
        "csr_get_neighbors_p50_us": metric_u64(metrics, &["csr", "get_neighbors_latency", "p50_us"]),
        "csr_get_neighbors_p90_us": metric_u64(metrics, &["csr", "get_neighbors_latency", "p90_us"]),
        "csr_get_neighbors_p99_us": metric_u64(metrics, &["csr", "get_neighbors_latency", "p99_us"]),
    })
}

fn metric_u64(metrics: &Value, path: &[&str]) -> u64 {
    let mut current = metrics;
    for key in path {
        let Some(next) = current.get(*key) else {
            return 0;
        };
        current = next;
    }
    current.as_u64().unwrap_or(0)
}
