use std::collections::HashMap;
use std::fs;
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand, ValueEnum};
use lsmgraph::base_graph::{build_from_snb, BuildConfig, IoConfig};
use lsmgraph::config::{IoBackendKind, L0LayoutPolicy, LsmGraphConfig, QueryControlStage};
use lsmgraph::csr::CsrPropertyValuePredicate;
use lsmgraph::graph::Engine;
use lsmgraph::loader::{import_person_knows, import_snb_topology, validate_person_knows};
use lsmgraph::metrics::process_cpu_time_ns;
use lsmgraph::shared_truth::{
    build_storage_sample_plan, read_truth_tsv, verify_engine_truth, DenseDigest, SharedIdMap,
    TruthRow,
};
use lsmgraph::snb::{
    audit_snb_edge_property_materialization, import_snb_full, import_snb_full_multi,
    import_snb_full_multi_with_edge_properties, import_snb_full_with_edge_properties,
    import_snb_updates, rebuild_snb_edge_props, start_dgs_compatible_server,
    validate_ic1_ic14_dynamic, validate_ic_batch_dynamic, validate_mixed_tugraph_dynamic,
    SnbEdgePropertyMaterialization, SnbGraph,
};
use lsmgraph::types::{source_label_from_vertex_id, EdgeMarker, UNKNOWN_SOURCE_LABEL};
use lsmgraph::{
    DegreeClass, DynamicGraphView, EdgeRecord, GraphAccessSignature, NewPropertyEntry,
    PropertyOwner, PropertyValue,
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
    /// Capacity (entries) of the CSR metadata cache (header + offsets +
    /// SourceBloom per file). Raise it when an L0 layout produces more files
    /// than this, otherwise reads thrash the cache and re-load whole offset
    /// arrays on every miss.
    #[arg(long, default_value_t = 4096)]
    csr_metadata_cache_entries: usize,
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
        compact_after_import: bool,
        #[arg(long, default_value_t = false)]
        auto_compact: bool,
        #[arg(long, default_value_t = 0)]
        schema_epoch: u64,
        #[arg(long, default_value_t = false)]
        graph_aware_l0: bool,
        #[arg(long, default_value = "naive")]
        l0_layout: L0LayoutPolicy,
        #[arg(long, default_value = "a6")]
        query_control_stage: QueryControlStage,
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
        #[arg(long, default_value_t = false)]
        semantic_budget_feedback_only: bool,
        #[arg(long, default_value_t = false)]
        semantic_budget_disable_feedback: bool,
        /// Materialize the real LDBC `person_knows_person.creationDate` value into Engine/CSR.
        /// This is opt-in because it changes the physical store and schema epoch.
        #[arg(long, default_value_t = false)]
        materialize_knows_creation_date: bool,
        #[arg(long, default_value_t = 5)]
        knows_creation_date_property_id: u32,
    },
    ImportMany {
        #[arg(long, default_value = DEFAULT_DATA)]
        input: PathBuf,
        #[arg(long, default_value = "snb-full")]
        relation: String,
        /// Repeated as `--layout-store layout:/absolute/store/path`.
        ///
        /// This is an experimental W14 import path: it parses the SNB CSV stream once and fans
        /// each edge out to multiple Engine instances. It currently requires
        /// `SNB_SKIP_ADJ_CACHE=1`, because vertex JSONL, edge-prop JSONL, and adjacency cache are
        /// single-store artifacts in the legacy import pipeline.
        #[arg(long = "layout-store", required = true)]
        layout_stores: Vec<String>,
        #[arg(long, default_value_t = 64 * 1024 * 1024)]
        memgraph_bytes: usize,
        #[arg(long, default_value_t = 0)]
        schema_epoch: u64,
        #[arg(long, default_value_t = false)]
        auto_compact: bool,
        #[arg(long, default_value = "a6")]
        query_control_stage: QueryControlStage,
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
        #[arg(long, default_value_t = false)]
        semantic_budget_feedback_only: bool,
        #[arg(long, default_value_t = false)]
        semantic_budget_disable_feedback: bool,
        /// Materialize the real LDBC `person_knows_person.creationDate` value into every store.
        #[arg(long, default_value_t = false)]
        materialize_knows_creation_date: bool,
        #[arg(long, default_value_t = 5)]
        knows_creation_date_property_id: u32,
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
        #[arg(long)]
        dump_edges: Option<PathBuf>,
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
        /// Defaults to A0 to preserve the legacy single-output compaction CLI.
        #[arg(long, default_value = "a0")]
        query_control_stage: QueryControlStage,
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
    /// Re-open-time fail-closed audit for the formal SNB edge-property store.
    SnbPropertyAudit {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
        #[arg(long, default_value_t = 5)]
        property_id: u32,
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
        #[arg(long, default_value_t = 0)]
        warmup_runs: usize,
        #[arg(long, default_value_t = 1)]
        repeats: usize,
        /// Replay the complete frozen sample plan this many times before
        /// warmup/measurement. These queries stay in the same Engine process
        /// so feedback state can drive the A4/A5 pre-measurement compaction.
        #[arg(long, default_value_t = 0)]
        training_runs: usize,
        /// Number of feedback-selected L0 partitions to compact after the
        /// training trace and before measurement. Valid only for A4/A5 with
        /// automatic maintenance disabled.
        #[arg(long, default_value_t = 0)]
        training_feedback_compactions: usize,
        #[arg(long)]
        edge_type: Option<i32>,
        #[arg(long, value_delimiter = ',')]
        edge_types: Vec<i32>,
        #[arg(long)]
        src_label: Option<i32>,
        #[arg(long)]
        dst_label: Option<i32>,
        #[arg(long, default_value_t = false)]
        semantic_degree_hint: bool,
        #[arg(long, default_value_t = false)]
        force_signature: bool,
        /// Emit a deterministic per-sample result digest (stable hash over the sorted visible
        /// EdgeRecords) so schema vs variant correctness can be compared from the bench JSON
        /// directly, without re-opening two engines via `neighbor-compare`.
        #[arg(long, default_value_t = false)]
        emit_result_digests: bool,
        #[arg(long, default_value_t = false)]
        sample_plan_degree_hint: bool,
        #[arg(long)]
        sample_plan_in: Option<PathBuf>,
        #[arg(long)]
        sample_plan_out: Option<PathBuf>,
        /// Enable the strict P10 adapter path and publish raw per-query
        /// observations into this directory.  This mode requires
        /// --sample-plan-in, --p10-truth-tsv, and --p10-id-map-dir.
        #[arg(long)]
        p10_raw_output_dir: Option<PathBuf>,
        /// P01/P02B dense-ID typed-neighbor truth consumed by P10 raw mode.
        #[arg(long)]
        p10_truth_tsv: Option<PathBuf>,
        /// P01 bidirectional dense/original ID map consumed by P10 raw mode.
        #[arg(long)]
        p10_id_map_dir: Option<PathBuf>,
        /// Fail-closed deadline for each typed-neighbor call plus result
        /// materialization and dense digest in P10 raw mode.
        #[arg(long)]
        p10_per_query_timeout_ms: Option<u64>,
        #[arg(long, default_value_t = true)]
        scan: bool,
        #[arg(long, default_value_t = false)]
        auto_compact: bool,
        /// Enable the engine's asynchronous/closed-loop maintenance. This is
        /// distinct from `--auto-compact`, the legacy post-round manual pick.
        #[arg(long, default_value_t = false)]
        automatic_maintenance: bool,
        /// Collect mutually-exclusive process-CPU phase counters. Use only in
        /// a single-stream diagnostic run with automatic maintenance disabled.
        #[arg(long, default_value_t = false)]
        query_cpu_phases: bool,
        #[arg(long)]
        l0_layout: Option<L0LayoutPolicy>,
        #[arg(long, default_value = "a6")]
        query_control_stage: QueryControlStage,
        #[arg(long, value_enum, default_value = "one-hop")]
        workload_mode: StorageBenchWorkloadMode,
        #[arg(long, value_enum, default_value = "none")]
        property_predicate_mode: StorageBenchPropertyPredicateMode,
        #[arg(long, default_value_t = 0)]
        property_id: u32,
        #[arg(long, default_value_t = 0)]
        property_value_i64: i64,
        #[arg(long, default_value_t = 0)]
        property_default_i64: i64,
        #[arg(long, default_value_t = 64)]
        two_hop_fanout: usize,
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
        #[arg(long, value_enum, default_value = "none")]
        property_predicate_mode: StorageBenchPropertyPredicateMode,
        #[arg(long, default_value_t = 0)]
        property_id: u32,
        #[arg(long, default_value_t = 0)]
        property_value_i64: i64,
        #[arg(long, default_value_t = 0)]
        property_default_i64: i64,
        #[arg(long, default_value_t = 1)]
        max_mismatches: usize,
    },
    /// Verify the versioned dense-ID truth TSV against one SemL0 store.
    ///
    /// This is a correctness consumer, not a timing harness.  When
    /// --sample-plan-out is supplied it also emits a storage-bench-compatible
    /// plan so later matched performance runs consume the identical query order.
    SharedTruthVerify {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
        #[arg(long)]
        truth_tsv: PathBuf,
        #[arg(long)]
        id_map_dir: PathBuf,
        #[arg(long)]
        sample_plan_out: Option<PathBuf>,
        #[arg(long)]
        output: Option<PathBuf>,
        #[arg(long)]
        expected_queries: Option<usize>,
        #[arg(long, default_value_t = false)]
        semantic_degree_hint: bool,
        #[arg(long, default_value_t = false)]
        force_signature: bool,
        #[arg(long)]
        l0_layout: Option<L0LayoutPolicy>,
        #[arg(long, default_value_t = 20)]
        max_mismatch_details: usize,
    },
    OracleIndex {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
    },
    AdjCacheBench {
        #[arg(long, default_value = DEFAULT_STORE)]
        data_dir: PathBuf,
        #[arg(long, default_value_t = 100)]
        samples: usize,
        #[arg(long)]
        edge_type: Option<i32>,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct StorageBenchSamplePlan {
    version: u32,
    source: String,
    samples_per_edge_type: usize,
    semantic_degree_hint: bool,
    #[serde(default)]
    force_signature: bool,
    #[serde(default)]
    src_label: Option<i32>,
    #[serde(default)]
    dst_label: Option<i32>,
    entries: Vec<StorageBenchSampleEntry>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct StorageBenchSampleEntry {
    edge_type: Option<i32>,
    #[serde(default)]
    src_label: Option<i32>,
    #[serde(default)]
    dst_label: Option<i32>,
    candidate_edges_for_sampling: usize,
    candidate_sources_for_sampling: usize,
    samples: Vec<StorageBenchSample>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct StorageBenchSample {
    src: u64,
    degree: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, ValueEnum)]
#[serde(rename_all = "snake_case")]
enum StorageBenchWorkloadMode {
    OneHop,
    TwoHop,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, ValueEnum)]
#[serde(rename_all = "snake_case")]
enum StorageBenchPropertyPredicateMode {
    None,
    RequiredProperty,
    Presence,
    Equality,
    AbsentDefault,
}

#[derive(Debug, Clone)]
struct ImportManyLayoutStore {
    layout: L0LayoutPolicy,
    data_dir: PathBuf,
}

fn parse_import_many_layout_store(raw: &str) -> Result<ImportManyLayoutStore> {
    let Some((layout_raw, data_dir_raw)) = raw.split_once(':') else {
        anyhow::bail!("invalid --layout-store {raw:?}; expected layout:/absolute/store/path");
    };
    if data_dir_raw.is_empty() {
        anyhow::bail!("invalid --layout-store {raw:?}; empty store path");
    }
    let layout = layout_raw.parse::<L0LayoutPolicy>()?;
    Ok(ImportManyLayoutStore {
        layout,
        data_dir: PathBuf::from(data_dir_raw),
    })
}

#[tokio::main(flavor = "multi_thread")]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    let io_backend = cli.io_backend;
    let csr_metadata_cache_entries = cli.csr_metadata_cache_entries;
    match cli.command {
        Command::Import {
            input,
            data_dir,
            relation,
            memgraph_bytes,
            compact,
            compact_after_import,
            auto_compact,
            schema_epoch,
            graph_aware_l0,
            l0_layout,
            query_control_stage,
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
            semantic_budget_feedback_only,
            semantic_budget_disable_feedback,
            materialize_knows_creation_date,
            knows_creation_date_property_id,
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
            if materialize_knows_creation_date && !matches!(relation.as_str(), "snb-full" | "full")
            {
                anyhow::bail!("--materialize-knows-creation-date requires --relation snb-full");
            }
            if materialize_knows_creation_date
                && (compact
                    || compact_after_import
                    || matches!(l0_layout, L0LayoutPolicy::FullCompact))
            {
                anyhow::bail!(
                    "property materialization cannot be combined with import-time compaction; import a pristine store, close/reopen/audit it, then compact an isolated clone"
                );
            }
            let config = LsmGraphConfig::new(&data_dir)
                .with_memgraph_capacity(memgraph_bytes)
                .with_io_backend(io_backend)
                .with_metadata_cache_entries(csr_metadata_cache_entries)
                .with_auto_compaction(auto_compact)
                .with_schema_epoch(schema_epoch)
                .with_l0_layout(l0_layout)
                .with_query_control_stage(query_control_stage)
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
                .with_semantic_budget_degree_weight(semantic_budget_degree_weight)
                .with_semantic_budget_feedback_only(semantic_budget_feedback_only)
                .with_semantic_budget_disable_feedback(semantic_budget_disable_feedback);
            let engine = Engine::create(config).await?;
            let mut property_materialization = None;
            let stats = match relation.as_str() {
                "person_knows" => import_person_knows(engine.clone(), &input).await?,
                "all-topology" | "topology" | "all" => {
                    import_snb_topology(engine.clone(), &input).await?
                }
                "snb-full" | "full" if materialize_knows_creation_date => {
                    let (stats, receipt) = import_snb_full_with_edge_properties(
                        engine.clone(),
                        &input,
                        &data_dir,
                        SnbEdgePropertyMaterialization::knows_creation_date(
                            knows_creation_date_property_id,
                        ),
                    )
                    .await?;
                    property_materialization = Some(receipt);
                    stats
                }
                "snb-full" | "full" => import_snb_full(engine.clone(), &input, &data_dir).await?,
                _ => anyhow::bail!(
                    "unsupported --relation {relation}; use person_knows, all-topology, or snb-full"
                ),
            };
            if compact {
                engine.compact_l0_to_l1().await?;
            }
            if compact_after_import || matches!(l0_layout, L0LayoutPolicy::FullCompact) {
                let compact_start = Instant::now();
                eprintln!("[import] FullCompact: triggering post-import L0->L1 compaction");
                engine.compact_l0_to_l1().await?;
                eprintln!(
                    "[import] FullCompact: compaction complete in {:.1}s",
                    compact_start.elapsed().as_secs_f64()
                );
            }
            let sidecar_start = Instant::now();
            eprintln!(
                "[import] persist semantic sidecars start elapsed_s={:.1}",
                sidecar_start.elapsed().as_secs_f64()
            );
            engine.persist_semantic_sidecars()?;
            eprintln!(
                "[import] persist semantic sidecars complete elapsed_s={:.1}",
                sidecar_start.elapsed().as_secs_f64()
            );
            if let Some(receipt) = property_materialization.as_ref() {
                println!(
                    "{}",
                    serde_json::to_string_pretty(&json!({
                        "input_rows": stats.input_rows,
                        "directed_edges": stats.directed_edges,
                        "snapshot": engine.current_snapshot(),
                        "l0_layout": format!("{:?}", l0_layout),
                        "query_control_stage": format!("{:?}", query_control_stage),
                        "property_materialization": {
                            "profile": receipt.profile,
                            "property_id": receipt.property_id,
                            "edge_type": receipt.edge_type,
                            "schema_epoch": receipt.schema_epoch,
                            "materialized_property_values": receipt.materialized_property_values,
                            "topology_only_directed_edges": receipt.topology_only_directed_edges,
                            "property_bitmap_nonzero_segments_per_store": receipt.property_bitmap_nonzero_segments_per_store,
                            "property_bitmap_zero_segments_per_store": receipt.property_bitmap_zero_segments_per_store,
                            "missing_semantics": "source creationDate is required for positive Knows edges; synthetic -1 reverse index and all other relations are absent/null",
                            "formal_query_constraint": "property-only: sample-plan edge_type must be null",
                        },
                    }))?
                );
            } else {
                // Preserve the canonical BASE output byte-for-byte.
                println!(
                    "{{\"input_rows\":{},\"directed_edges\":{},\"snapshot\":{},\"l0_layout\":\"{:?}\",\"query_control_stage\":\"{:?}\"}}",
                    stats.input_rows,
                    stats.directed_edges,
                    engine.current_snapshot(),
                    l0_layout,
                    query_control_stage
                );
            }
        }
        Command::ImportMany {
            input,
            relation,
            layout_stores,
            memgraph_bytes,
            schema_epoch,
            auto_compact,
            query_control_stage,
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
            semantic_budget_feedback_only,
            semantic_budget_disable_feedback,
            materialize_knows_creation_date,
            knows_creation_date_property_id,
        } => {
            if !matches!(relation.as_str(), "snb-full" | "full") {
                anyhow::bail!("import-many currently supports only --relation snb-full");
            }
            let layout_stores = layout_stores
                .iter()
                .map(|raw| parse_import_many_layout_store(raw))
                .collect::<Result<Vec<_>>>()?;
            if layout_stores.is_empty() {
                anyhow::bail!("import-many requires at least one --layout-store");
            }
            let mut engines = Vec::with_capacity(layout_stores.len());
            for spec in &layout_stores {
                let config = LsmGraphConfig::new(&spec.data_dir)
                    .with_memgraph_capacity(memgraph_bytes)
                    .with_io_backend(io_backend)
                    .with_metadata_cache_entries(csr_metadata_cache_entries)
                    .with_auto_compaction(auto_compact)
                    .with_schema_epoch(schema_epoch)
                    .with_l0_layout(spec.layout)
                    .with_query_control_stage(query_control_stage)
                    .with_semantic_budget_min_edge_type_bytes(semantic_budget_min_edge_type_bytes)
                    .with_semantic_budget_min_edge_type_score(semantic_budget_min_edge_type_score)
                    .with_semantic_budget_core_edge_weight(semantic_budget_core_edge_weight)
                    .with_semantic_budget_reverse_core_edge_weight(
                        semantic_budget_reverse_core_edge_weight,
                    )
                    .with_semantic_budget_other_edge_weight(semantic_budget_other_edge_weight)
                    .with_semantic_budget_max_extra_l0_files(semantic_budget_max_extra_l0_files)
                    .with_semantic_budget_edge_type_allowlist(
                        semantic_budget_edge_type_allowlist.clone(),
                    )
                    .with_semantic_budget_min_exact_bytes(semantic_budget_min_exact_bytes)
                    .with_semantic_budget_min_benefit_score(semantic_budget_min_benefit_score)
                    .with_semantic_budget_degree_weight(semantic_budget_degree_weight)
                    .with_semantic_budget_feedback_only(semantic_budget_feedback_only)
                    .with_semantic_budget_disable_feedback(semantic_budget_disable_feedback);
                engines.push(Engine::create(config).await?);
            }
            let store_dirs = layout_stores
                .iter()
                .map(|spec| spec.data_dir.clone())
                .collect::<Vec<_>>();
            let (stats, property_materialization) = if materialize_knows_creation_date {
                let (stats, receipt) = import_snb_full_multi_with_edge_properties(
                    &engines,
                    &input,
                    &store_dirs,
                    SnbEdgePropertyMaterialization::knows_creation_date(
                        knows_creation_date_property_id,
                    ),
                )
                .await?;
                (stats, Some(receipt))
            } else {
                (
                    import_snb_full_multi(&engines, &input, &store_dirs).await?,
                    None,
                )
            };
            let sidecar_start = Instant::now();
            eprintln!(
                "[import-many] persist semantic sidecars start stores={} elapsed_s={:.1}",
                engines.len(),
                sidecar_start.elapsed().as_secs_f64()
            );
            for (idx, engine) in engines.iter().enumerate() {
                eprintln!(
                    "[import-many] persist semantic sidecars engine_index={} start",
                    idx
                );
                engine.persist_semantic_sidecars()?;
                eprintln!(
                    "[import-many] persist semantic sidecars engine_index={} complete",
                    idx
                );
            }
            eprintln!(
                "[import-many] persist semantic sidecars complete elapsed_s={:.1}",
                sidecar_start.elapsed().as_secs_f64()
            );
            let stores = layout_stores
                .iter()
                .zip(engines.iter())
                .map(|(spec, engine)| {
                    json!({
                        "layout": format!("{:?}", spec.layout),
                        "query_control_stage": format!("{:?}", query_control_stage),
                        "data_dir": spec.data_dir.display().to_string(),
                        "snapshot": engine.current_snapshot(),
                    })
                })
                .collect::<Vec<_>>();
            let output = if let Some(receipt) = property_materialization.as_ref() {
                json!({
                    "input_rows": stats.input_rows,
                    "directed_edges": stats.directed_edges,
                    "query_control_stage": format!("{:?}", query_control_stage),
                    "stores": stores,
                    "property_materialization": {
                        "profile": receipt.profile,
                        "property_id": receipt.property_id,
                        "edge_type": receipt.edge_type,
                        "schema_epoch": receipt.schema_epoch,
                        "materialized_property_values_per_store": receipt.materialized_property_values,
                        "topology_only_directed_edges_per_store": receipt.topology_only_directed_edges,
                        "property_bitmap_nonzero_segments_per_store": receipt.property_bitmap_nonzero_segments_per_store,
                        "property_bitmap_zero_segments_per_store": receipt.property_bitmap_zero_segments_per_store,
                        "missing_semantics": "source creationDate is required for positive Knows edges; synthetic -1 reverse index and all other relations are absent/null",
                        "formal_query_constraint": "property-only: sample-plan edge_type must be null",
                    },
                })
            } else {
                // Preserve the canonical BASE output fields exactly.
                json!({
                    "input_rows": stats.input_rows,
                    "directed_edges": stats.directed_edges,
                    "query_control_stage": format!("{:?}", query_control_stage),
                    "stores": stores,
                })
            };
            println!("{}", serde_json::to_string_pretty(&output)?);
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
            let engine = Engine::open(
                LsmGraphConfig::new(data_dir)
                    .with_io_backend(io_backend)
                    .with_metadata_cache_entries(csr_metadata_cache_entries),
            )
            .await?;
            let snapshot = engine.current_snapshot();
            let neighbors = if let Some(edge_type) = edge_type {
                engine.get_neighbors_typed(src, edge_type, snapshot).await?
            } else {
                engine.get_neighbors(src, snapshot).await?
            };
            println!("{}", serde_json::to_string_pretty(&neighbors)?);
        }
        Command::Scan {
            data_dir,
            dump_edges,
        } => {
            let engine = Engine::open(
                LsmGraphConfig::new(data_dir)
                    .with_io_backend(io_backend)
                    .with_metadata_cache_entries(csr_metadata_cache_entries),
            )
            .await?;
            let snapshot = engine.current_snapshot();
            let edges = engine.scan_edges(snapshot).await?;
            if let Some(path) = dump_edges {
                let mut writer = BufWriter::new(fs::File::create(&path)?);
                for edge in &edges {
                    if edge.marker == EdgeMarker::Insert {
                        writeln!(writer, "{} {} {}", edge.src, edge.edge_type, edge.dst)?;
                    }
                }
                writer.flush()?;
            }
            println!(
                "{{\"snapshot\":{},\"directed_edges\":{}}}",
                snapshot,
                edges.len()
            );
        }
        Command::Stats { data_dir } => {
            let engine = Engine::open(
                LsmGraphConfig::new(data_dir)
                    .with_io_backend(io_backend)
                    .with_metadata_cache_entries(csr_metadata_cache_entries),
            )
            .await?;
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
            let engine = Engine::open(
                LsmGraphConfig::new(data_dir)
                    .with_io_backend(io_backend)
                    .with_metadata_cache_entries(csr_metadata_cache_entries),
            )
            .await?;
            println!(
                "{}",
                serde_json::to_string_pretty(&engine.schema_catalog_snapshot())?
            );
        }
        Command::SchemaEvolutionReport { data_dir } => {
            let engine = Engine::open(
                LsmGraphConfig::new(data_dir)
                    .with_io_backend(io_backend)
                    .with_metadata_cache_entries(csr_metadata_cache_entries),
            )
            .await?;
            println!(
                "{}",
                serde_json::to_string_pretty(&engine.schema_evolution_report())?
            );
        }
        Command::SchemaAddVertexLabel { data_dir, id, name } => {
            let engine = Engine::open(
                LsmGraphConfig::new(data_dir)
                    .with_io_backend(io_backend)
                    .with_metadata_cache_entries(csr_metadata_cache_entries),
            )
            .await?;
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
            let engine = Engine::open(
                LsmGraphConfig::new(data_dir)
                    .with_io_backend(io_backend)
                    .with_metadata_cache_entries(csr_metadata_cache_entries),
            )
            .await?;
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
            let engine = Engine::open(
                LsmGraphConfig::new(data_dir)
                    .with_io_backend(io_backend)
                    .with_metadata_cache_entries(csr_metadata_cache_entries),
            )
            .await?;
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
            let engine = Engine::open(
                LsmGraphConfig::new(data_dir)
                    .with_io_backend(io_backend)
                    .with_metadata_cache_entries(csr_metadata_cache_entries),
            )
            .await?;
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
            let engine = Engine::open(
                LsmGraphConfig::new(data_dir)
                    .with_io_backend(io_backend)
                    .with_metadata_cache_entries(csr_metadata_cache_entries),
            )
            .await?;
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
            let engine = Engine::open(
                LsmGraphConfig::new(data_dir)
                    .with_io_backend(io_backend)
                    .with_metadata_cache_entries(csr_metadata_cache_entries),
            )
            .await?;
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
            let engine = Engine::open(
                LsmGraphConfig::new(data_dir)
                    .with_io_backend(io_backend)
                    .with_metadata_cache_entries(csr_metadata_cache_entries),
            )
            .await?;
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
            let engine = Engine::open(
                LsmGraphConfig::new(data_dir)
                    .with_io_backend(io_backend)
                    .with_metadata_cache_entries(csr_metadata_cache_entries),
            )
            .await?;
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
            let engine = Engine::open(
                LsmGraphConfig::new(data_dir)
                    .with_io_backend(io_backend)
                    .with_metadata_cache_entries(csr_metadata_cache_entries),
            )
            .await?;
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
            let engine = Engine::open(
                LsmGraphConfig::new(data_dir)
                    .with_io_backend(io_backend)
                    .with_metadata_cache_entries(csr_metadata_cache_entries),
            )
            .await?;
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
            query_control_stage,
            auto_pick,
            src_label,
            edge_type,
            min_src,
            max_src,
        } => {
            let engine = Engine::open(
                LsmGraphConfig::new(data_dir)
                    .with_io_backend(io_backend)
                    .with_metadata_cache_entries(csr_metadata_cache_entries)
                    .with_query_control_stage(query_control_stage),
            )
            .await?;
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
                if query_control_stage.semantic_compaction_enabled() {
                    let outputs = engine.compact_l0_to_l1_partitioned().await?;
                    println!(
                        "{}",
                        serde_json::to_string_pretty(&json!({
                            "query_control_stage": format!("{:?}", query_control_stage),
                            "semantic_partition_outputs": true,
                            "output_count": outputs.len(),
                            "outputs": outputs,
                        }))?
                    );
                } else {
                    let meta = engine.compact_l0_to_l1().await?;
                    println!("{}", serde_json::to_string_pretty(&meta)?);
                }
            }
        }
        Command::ValidateKnows {
            input,
            data_dir,
            max_vertices,
        } => {
            let engine = Engine::open(
                LsmGraphConfig::new(data_dir)
                    .with_io_backend(io_backend)
                    .with_metadata_cache_entries(csr_metadata_cache_entries),
            )
            .await?;
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
            let engine = Engine::open(
                LsmGraphConfig::new(&data_dir)
                    .with_io_backend(io_backend)
                    .with_metadata_cache_entries(csr_metadata_cache_entries),
            )
            .await?;
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
        Command::SnbPropertyAudit {
            data_dir,
            property_id,
        } => {
            let engine = Engine::open(
                LsmGraphConfig::new(&data_dir)
                    .with_io_backend(io_backend)
                    .with_metadata_cache_entries(csr_metadata_cache_entries),
            )
            .await?;
            let audit = audit_snb_edge_property_materialization(
                &engine,
                SnbEdgePropertyMaterialization::knows_creation_date(property_id),
            )?;
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "profile": audit.profile,
                    "data_dir": data_dir,
                    "property_id": audit.property_id,
                    "edge_type": audit.edge_type,
                    "snapshot": audit.snapshot,
                    "property_bitmap_nonzero_segments": audit.property_bitmap_nonzero_segments,
                    "property_bitmap_zero_segments": audit.property_bitmap_zero_segments,
                    "reopened": true,
                }))?
            );
        }
        Command::SnbUpdates { input, data_dir } => {
            let engine = Engine::open(
                LsmGraphConfig::new(&data_dir)
                    .with_io_backend(io_backend)
                    .with_metadata_cache_entries(csr_metadata_cache_entries),
            )
            .await?;
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
            warmup_runs,
            repeats,
            training_runs,
            training_feedback_compactions,
            edge_type,
            edge_types,
            src_label,
            dst_label,
            semantic_degree_hint,
            force_signature,
            emit_result_digests,
            sample_plan_degree_hint,
            sample_plan_in,
            sample_plan_out,
            p10_raw_output_dir,
            p10_truth_tsv,
            p10_id_map_dir,
            p10_per_query_timeout_ms,
            scan,
            auto_compact,
            automatic_maintenance,
            query_cpu_phases,
            l0_layout,
            query_control_stage,
            workload_mode,
            property_predicate_mode,
            property_id,
            property_value_i64,
            property_default_i64,
            two_hop_fanout,
            ra_min_queries,
            ra_min_score,
            ra_min_l0_segments,
        } => {
            if query_cpu_phases && automatic_maintenance {
                anyhow::bail!(
                    "--query-cpu-phases requires --automatic-maintenance=false; run closed-loop maintenance as a separate resource trial"
                );
            }
            if training_feedback_compactions > 0 && training_runs == 0 {
                anyhow::bail!("--training-feedback-compactions requires --training-runs > 0");
            }
            if training_feedback_compactions > 0 && !query_control_stage.feedback_priority_enabled()
            {
                anyhow::bail!(
                    "--training-feedback-compactions requires query-control stage A4 or A5"
                );
            }
            if training_feedback_compactions > 0 && automatic_maintenance {
                anyhow::bail!(
                    "manual training compaction and --automatic-maintenance are mutually exclusive"
                );
            }
            if training_feedback_compactions > 0 && auto_compact {
                anyhow::bail!(
                    "--training-feedback-compactions and measured --auto-compact are mutually exclusive"
                );
            }
            let p10_option_count = [
                p10_raw_output_dir.is_some(),
                p10_truth_tsv.is_some(),
                p10_id_map_dir.is_some(),
                p10_per_query_timeout_ms.is_some(),
            ]
            .into_iter()
            .filter(|present| *present)
            .count();
            if p10_option_count != 0 && p10_option_count != 4 {
                bail!(
                    "P10 raw mode requires all of --p10-raw-output-dir, --p10-truth-tsv, --p10-id-map-dir, and --p10-per-query-timeout-ms"
                );
            }
            if p10_option_count == 4 {
                if sample_plan_in.is_none() {
                    bail!("P10 raw mode requires --sample-plan-in");
                }
                if warmup_runs == 0 || repeats == 0 {
                    bail!("P10 raw mode requires --warmup-runs > 0 and --repeats > 0");
                }
                if p10_per_query_timeout_ms == Some(0) {
                    bail!("P10 raw mode requires --p10-per-query-timeout-ms > 0");
                }
                if training_runs != 0
                    || training_feedback_compactions != 0
                    || auto_compact
                    || automatic_maintenance
                    || query_cpu_phases
                {
                    bail!(
                        "P10 raw mode forbids training, compaction, automatic maintenance, and query CPU instrumentation"
                    );
                }
                if workload_mode != StorageBenchWorkloadMode::OneHop
                    || property_predicate_mode != StorageBenchPropertyPredicateMode::None
                    || src_label.is_some()
                    || dst_label.is_some()
                    || edge_type.is_some()
                    || !edge_types.is_empty()
                {
                    bail!(
                        "P10 raw mode is frozen to plan-driven one-hop typed-neighbor queries without labels or properties"
                    );
                }
                if emit_result_digests || sample_plan_degree_hint || sample_plan_out.is_some() {
                    bail!(
                        "P10 raw mode owns dense result digests and forbids legacy digest/plan-output options"
                    );
                }
            }
            let repeats = repeats.max(1);
            let mut config = LsmGraphConfig::new(&data_dir)
                .with_io_backend(io_backend)
                .with_metadata_cache_entries(csr_metadata_cache_entries)
                .with_query_control_stage(query_control_stage)
                .with_auto_compaction(automatic_maintenance)
                .with_query_cpu_phase_instrumentation(query_cpu_phases);
            if let Some(l0_layout) = l0_layout {
                config.l0_layout = l0_layout;
            }
            config.l0_ra_min_queries = ra_min_queries;
            config.l0_ra_min_score = ra_min_score;
            config.l0_ra_min_l0_segments = ra_min_l0_segments;
            let engine = Engine::open(config).await?;
            if let (
                Some(raw_output_dir),
                Some(truth_tsv),
                Some(id_map_dir),
                Some(per_query_timeout_ms),
            ) = (
                p10_raw_output_dir.as_ref(),
                p10_truth_tsv.as_ref(),
                p10_id_map_dir.as_ref(),
                p10_per_query_timeout_ms,
            ) {
                let sample_plan_path = sample_plan_in
                    .as_ref()
                    .expect("P10 option validation requires a sample plan");
                let sample_plan: StorageBenchSamplePlan = serde_json::from_slice(
                    &fs::read(sample_plan_path)
                        .with_context(|| format!("read {}", sample_plan_path.display()))?,
                )
                .with_context(|| format!("parse {}", sample_plan_path.display()))?;
                let truth_rows = read_truth_tsv(truth_tsv)?;
                let ids = SharedIdMap::load(id_map_dir)?;
                let summary = run_p10_storage_bench(
                    &engine,
                    &sample_plan,
                    &truth_rows,
                    &ids,
                    semantic_degree_hint,
                    force_signature,
                    warmup_runs,
                    repeats,
                    per_query_timeout_ms,
                    raw_output_dir,
                )
                .await?;
                println!("{}", serde_json::to_string_pretty(&summary)?);
                return Ok(());
            }
            let oracle_index_stats = if matches!(l0_layout, Some(L0LayoutPolicy::OracleSemantic)) {
                Some(engine.build_oracle_index().await?)
            } else {
                None
            };
            let snapshot = engine.current_snapshot();
            let metrics = engine.metrics();
            let cache_state_before = storage_bench_cache_state();
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
                    src_label,
                    dst_label,
                    semantic_degree_hint,
                    force_signature,
                    sample_plan_degree_hint,
                )
            };
            let effective_force_signature = force_signature || sample_plan.force_signature;
            if let Some(path) = &sample_plan_out {
                if let Some(parent) = path
                    .parent()
                    .filter(|parent| !parent.as_os_str().is_empty())
                {
                    fs::create_dir_all(parent)?;
                }
                fs::write(path, serde_json::to_vec_pretty(&sample_plan)?)?;
            }

            // A4/A5 feedback is process-local. Replay the whole frozen trace
            // before measurement and compact in this same Engine instance;
            // a separate `compact` process would silently lose the feedback
            // map and invalidate the staircase.
            let levels_before_training = engine.live_file_count_by_level();
            let mut training_rounds = Vec::new();
            if training_runs > 0 {
                metrics.reset();
                let mut first_training_entry = true;
                for training_round in 0..training_runs {
                    for (entry_index, entry) in sample_plan.entries.iter().enumerate() {
                        let result = run_storage_bench_round(
                            &engine,
                            snapshot,
                            entry,
                            semantic_degree_hint,
                            effective_force_signature,
                            workload_mode,
                            property_predicate_mode,
                            property_id,
                            property_value_i64,
                            property_default_i64,
                            two_hop_fanout,
                            first_training_entry,
                            false,
                            false,
                        )
                        .await?;
                        first_training_entry = false;
                        training_rounds.push(json!({
                            "training_round": training_round + 1,
                            "entry_index": entry_index,
                            "edge_type": entry.edge_type,
                            "result": storage_bench_round_json(
                                "training",
                                training_round + 1,
                                result,
                            ),
                        }));
                    }
                }
            }
            let mut training_feedback_compaction_results = Vec::new();
            for compaction_index in 0..training_feedback_compactions {
                let decision = engine
                    .compact_best_l0_partition_by_score()
                    .await?
                    .ok_or_else(|| {
                        anyhow::anyhow!(
                            "training feedback compaction {} found no eligible L0 partition",
                            compaction_index + 1
                        )
                    })?;
                training_feedback_compaction_results.push(decision);
            }
            let levels_after_training = engine.live_file_count_by_level();
            metrics.reset_observations_preserve_controller();
            let mut benchmarks = Vec::new();
            for entry in &sample_plan.entries {
                let requested_edge_type = entry.edge_type;
                let srcs: Vec<u64> = entry.samples.iter().map(|sample| sample.src).collect();
                let mut warmup_rounds = Vec::new();
                for round in 0..warmup_runs {
                    let result = run_storage_bench_round(
                        &engine,
                        snapshot,
                        entry,
                        semantic_degree_hint,
                        effective_force_signature,
                        workload_mode,
                        property_predicate_mode,
                        property_id,
                        property_value_i64,
                        property_default_i64,
                        two_hop_fanout,
                        true,
                        false,
                        false,
                    )
                    .await?;
                    warmup_rounds.push(storage_bench_round_json("warmup", round + 1, result));
                }
                let mut measured_rounds = Vec::new();
                for round in 0..repeats {
                    let result = run_storage_bench_round(
                        &engine,
                        snapshot,
                        entry,
                        semantic_degree_hint,
                        effective_force_signature,
                        workload_mode,
                        property_predicate_mode,
                        property_id,
                        property_value_i64,
                        property_default_i64,
                        two_hop_fanout,
                        true,
                        emit_result_digests,
                        query_cpu_phases,
                    )
                    .await?;
                    measured_rounds.push(storage_bench_round_json("measured", round + 1, result));
                }
                let last_round = measured_rounds.last().cloned().unwrap_or_else(|| json!({}));
                let neighbor_edges = last_round
                    .get("neighbor_edges")
                    .and_then(Value::as_u64)
                    .unwrap_or(0);
                let get_neighbors_elapsed_ms = last_round
                    .get("get_neighbors_elapsed_ms")
                    .and_then(Value::as_u64)
                    .unwrap_or(0);
                let neighbor_summary = last_round
                    .get("neighbor_summary")
                    .cloned()
                    .unwrap_or(Value::Null);
                let neighbor_metrics = last_round
                    .get("neighbor_metrics")
                    .cloned()
                    .unwrap_or(Value::Null);
                let post_round_feedback_compaction = if auto_compact {
                    engine.compact_best_l0_partition_by_score().await?
                } else {
                    None
                };
                let post_round_feedback_compaction_metrics = if auto_compact {
                    Some(metrics.snapshot_json())
                } else {
                    None
                };
                // Digests are generated inside the final measured round. A
                // second replay would double the query trace, mutate A6's
                // controller after measurement, and pollute P31 process totals.
                let result_digests = last_round.get("result_digests").cloned();
                let entry_result_digest = last_round
                    .get("entry_result_digest")
                    .and_then(Value::as_str)
                    .map(str::to_string);
                benchmarks.push(json!({
                    "edge_type": requested_edge_type,
                    "src_label": entry.src_label,
                    "dst_label": entry.dst_label,
                    "candidate_edges_for_sampling": entry.candidate_edges_for_sampling,
                    "candidate_sources_for_sampling": entry.candidate_sources_for_sampling,
                    "semantic_degree_hint": semantic_degree_hint,
                    "force_signature": effective_force_signature,
                    "workload_mode": workload_mode,
                    "property_predicate_mode": property_predicate_mode,
                    "property_id": property_id,
                    "property_value_i64": property_value_i64,
                    "property_default_i64": property_default_i64,
                    "two_hop_fanout": two_hop_fanout,
                    "sampled_vertices": srcs.len(),
                    "sampled_srcs": srcs,
                    "sample_degrees": entry.samples,
                    "neighbor_edges": neighbor_edges,
                    "get_neighbors_elapsed_ms": get_neighbors_elapsed_ms,
                    "warmup_runs": warmup_runs,
                    "repeats": repeats,
                    "warmup_rounds": warmup_rounds,
                    "rounds": measured_rounds,
                    "repeat_summary": storage_bench_repeat_summary(&measured_rounds),
                    "post_round_feedback_compaction": post_round_feedback_compaction,
                    "post_round_feedback_compaction_metrics_source": if auto_compact { Some("last_measured_repeat") } else { None },
                    "levels_after_run": engine.live_file_count_by_level(),
                    "neighbor_summary": neighbor_summary,
                    "neighbor_metrics": neighbor_metrics,
                    "post_round_feedback_compaction_metrics": post_round_feedback_compaction_metrics,
                    "emit_result_digests": emit_result_digests,
                    "entry_result_digest": entry_result_digest,
                    "result_digests": result_digests,
                }));
            }
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "data_dir": data_dir,
                    "snapshot": snapshot,
                    "edge_type": edge_type,
                    "edge_types": edge_types,
                    "src_label": src_label,
                    "dst_label": dst_label,
                    "semantic_degree_hint": semantic_degree_hint,
                    "force_signature": effective_force_signature,
                    "emit_result_digests": emit_result_digests,
                    "sample_plan_degree_hint": sample_plan_degree_hint,
                    "workload_mode": workload_mode,
                    "property_predicate_mode": property_predicate_mode,
                    "property_id": property_id,
                    "property_value_i64": property_value_i64,
                    "property_default_i64": property_default_i64,
                    "two_hop_fanout": two_hop_fanout,
                    "warmup_runs": warmup_runs,
                    "repeats": repeats,
                    "training_runs": training_runs,
                    "training_feedback_compactions": training_feedback_compactions,
                    "training_rounds": training_rounds,
                    "training_feedback_compaction_results": training_feedback_compaction_results,
                    "levels_before_training": levels_before_training,
                    "levels_after_training": levels_after_training,
                    "sample_plan_in": sample_plan_in,
                    "sample_plan_out": sample_plan_out,
                    "l0_layout": l0_layout.map(|layout| format!("{:?}", layout)),
                    "query_control_stage": format!("{:?}", query_control_stage),
                    "feature_switches": {
                        "exact_evidence_admission": query_control_stage.admission_enabled(),
                        "semantic_routing": query_control_stage.semantic_routing_enabled(),
                        "budgeted_degree_promotion": query_control_stage.degree_promotion_enabled(),
                        "feedback_priority": query_control_stage.feedback_priority_enabled(),
                        "semantic_compaction": query_control_stage.semantic_compaction_enabled(),
                        "automatic_maintenance": automatic_maintenance,
                        "query_cpu_phase_instrumentation": query_cpu_phases,
                        "post_round_feedback_compact": auto_compact,
                    },
                    "oracle_index_stats": oracle_index_stats,
                    "sample_plan_version": sample_plan.version,
                    "scan_requested": scan && sample_plan.source == "scan",
                    "scan_edges": scan_edges,
                    "scan_elapsed_ms": scan_elapsed_ms,
                    "levels": engine.live_file_count_by_level(),
                    "scan_summary": scan_summary,
                    "scan_metrics": scan_metrics,
                    "cache_state_before": cache_state_before,
                    "cache_state_after": storage_bench_cache_state(),
                    "benchmarks": benchmarks,
                }))?
            );
        }
        Command::NeighborCompare {
            left_data_dir,
            right_data_dir,
            sample_plan,
            right_semantic_degree_hint,
            property_predicate_mode,
            property_id,
            property_value_i64,
            property_default_i64,
            max_mismatches,
        } => {
            let plan: StorageBenchSamplePlan =
                serde_json::from_str(&fs::read_to_string(&sample_plan)?)?;
            let left = Engine::open(
                LsmGraphConfig::new(&left_data_dir)
                    .with_io_backend(io_backend)
                    .with_metadata_cache_entries(csr_metadata_cache_entries),
            )
            .await?;
            let right = Engine::open(
                LsmGraphConfig::new(&right_data_dir)
                    .with_io_backend(io_backend)
                    .with_metadata_cache_entries(csr_metadata_cache_entries),
            )
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
                    let mut left_edges = run_storage_bench_query(
                        &left,
                        left_snapshot,
                        sample.src,
                        entry.edge_type,
                        false,
                        plan.force_signature,
                        sample.degree,
                        entry.dst_label,
                        property_predicate_mode,
                        property_id,
                        property_value_i64,
                        property_default_i64,
                    )
                    .await?;
                    let mut right_edges = run_storage_bench_query(
                        &right,
                        right_snapshot,
                        sample.src,
                        entry.edge_type,
                        right_semantic_degree_hint,
                        plan.force_signature,
                        sample.degree,
                        entry.dst_label,
                        property_predicate_mode,
                        property_id,
                        property_value_i64,
                        property_default_i64,
                    )
                    .await?;
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
                                "src_label": entry.src_label,
                                "dst_label": entry.dst_label,
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
                    "property_predicate_mode": property_predicate_mode,
                    "property_id": property_id,
                    "property_value_i64": property_value_i64,
                    "property_default_i64": property_default_i64,
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
        Command::SharedTruthVerify {
            data_dir,
            truth_tsv,
            id_map_dir,
            sample_plan_out,
            output,
            expected_queries,
            semantic_degree_hint,
            force_signature,
            l0_layout,
            max_mismatch_details,
        } => {
            let rows = read_truth_tsv(&truth_tsv)?;
            if let Some(expected_queries) = expected_queries {
                if rows.len() != expected_queries {
                    anyhow::bail!(
                        "shared truth query count mismatch: expected {} found {}",
                        expected_queries,
                        rows.len()
                    );
                }
            }
            let ids = SharedIdMap::load(&id_map_dir)?;
            if let Some(path) = &sample_plan_out {
                if let Some(parent) = path
                    .parent()
                    .filter(|parent| !parent.as_os_str().is_empty())
                {
                    fs::create_dir_all(parent)?;
                }
                let plan =
                    build_storage_sample_plan(&rows, &ids, semantic_degree_hint, force_signature)?;
                fs::write(path, serde_json::to_vec_pretty(&plan)?)?;
            }

            let mut config = LsmGraphConfig::new(&data_dir)
                .with_io_backend(io_backend)
                .with_metadata_cache_entries(csr_metadata_cache_entries);
            if let Some(l0_layout) = l0_layout {
                config.l0_layout = l0_layout;
            }
            let engine = Engine::open(config).await?;
            let verification = verify_engine_truth(
                &engine,
                &rows,
                &ids,
                semantic_degree_hint,
                force_signature,
                max_mismatch_details,
            )
            .await?;
            let mismatches = verification.mismatches;
            let report = json!({
                "consumer": "seml0-shared-truth-v1",
                "correctness_only": true,
                "performance_eligible": false,
                "data_dir": data_dir,
                "truth_tsv": truth_tsv,
                "truth_rows": rows.len(),
                "id_map_dir": id_map_dir,
                "id_map_dense_to_original_sha256": ids.dense_to_original_sha256(),
                "id_map_original_to_dense_sha256": ids.original_to_dense_sha256(),
                "sample_plan_out": sample_plan_out,
                "semantic_degree_hint": semantic_degree_hint,
                "force_signature": force_signature,
                "verification": verification,
            });
            let encoded = serde_json::to_vec_pretty(&report)?;
            if let Some(path) = output {
                if let Some(parent) = path
                    .parent()
                    .filter(|parent| !parent.as_os_str().is_empty())
                {
                    fs::create_dir_all(parent)?;
                }
                fs::write(path, &encoded)?;
            } else {
                println!("{}", String::from_utf8(encoded)?);
            }
            if mismatches != 0 {
                anyhow::bail!("shared truth verification found {mismatches} mismatches");
            }
        }
        Command::OracleIndex { data_dir } => {
            let mut config = LsmGraphConfig::new(&data_dir)
                .with_io_backend(io_backend)
                .with_metadata_cache_entries(csr_metadata_cache_entries);
            config.l0_layout = L0LayoutPolicy::OracleSemantic;
            let engine = Engine::open(config).await?;
            let started = Instant::now();
            eprintln!("[oracle-index] building oracle index from L0 segments");
            let stats = engine.build_oracle_index().await?;
            eprintln!(
                "[oracle-index] built oracle index with {} entries in {:.1}ms",
                stats.entry_count,
                started.elapsed().as_millis() as f64
            );
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "data_dir": data_dir,
                    "oracle_index_stats": stats,
                }))?
            );
        }
        Command::AdjCacheBench {
            data_dir,
            samples,
            edge_type,
        } => {
            let engine = Engine::open(
                LsmGraphConfig::new(&data_dir)
                    .with_io_backend(io_backend)
                    .with_metadata_cache_entries(csr_metadata_cache_entries),
            )
            .await?;
            let snapshot = engine.current_snapshot();
            let scan_start = Instant::now();
            let edges = engine.scan_edges(snapshot).await?;
            let scan_elapsed_ms = scan_start.elapsed().as_millis();
            let mut by_key: std::collections::HashMap<(u64, Option<i32>), Vec<u64>> =
                std::collections::HashMap::new();
            for edge in &edges {
                by_key
                    .entry((edge.src, Some(edge.edge_type)))
                    .or_default()
                    .push(edge.dst);
            }
            for dsts in by_key.values_mut() {
                dsts.sort_unstable();
                dsts.dedup();
            }
            let group_count = by_key.len();
            let total_edges: usize = by_key.values().map(|v| v.len()).sum();
            let cache_bytes = group_count * 24 + total_edges * 8;
            let edge_type_filter = edge_type.map(|et| et as i32);
            let sample_keys: Vec<_> = by_key
                .keys()
                .filter(|(_, et)| edge_type_filter.map(|f| *et == Some(f)).unwrap_or(true))
                .copied()
                .collect();
            let step = (sample_keys.len() / samples.max(1)).max(1);
            let query_start = Instant::now();
            let mut query_count = 0usize;
            for (idx, key) in sample_keys.iter().enumerate() {
                if idx % step == 0 {
                    let _ = by_key.get(key);
                    query_count += 1;
                }
            }
            let query_elapsed_ms = query_start.elapsed().as_millis();
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "data_dir": data_dir,
                    "snapshot": snapshot,
                    "total_edges": edges.len(),
                    "adjacency_groups": group_count,
                    "unique_edges": total_edges,
                    "cache_bytes": cache_bytes,
                    "scan_elapsed_ms": scan_elapsed_ms,
                    "query_count": query_count,
                    "query_elapsed_ms": query_elapsed_ms,
                    "edge_type": edge_type,
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

const FNV_OFFSET_BASIS: u64 = 0xcbf2_9ce4_8422_2325;
const FNV_PRIME: u64 = 0x0000_0100_0000_01b3;

fn fnv1a_update(hash: &mut u64, bytes: &[u8]) {
    for byte in bytes {
        *hash ^= *byte as u64;
        *hash = hash.wrapping_mul(FNV_PRIME);
    }
}

/// Deterministic digest over the canonical (sorted) visible EdgeRecords for one sample query.
/// Stable across processes/machines: plain FNV-1a over the little-endian field bytes, no random
/// seed. Sorting first makes the digest order-independent so schema and variant agree whenever
/// they return the same logical edge set.
fn storage_bench_result_digest(edges: &mut Vec<EdgeRecord>) -> u64 {
    sort_edge_records(edges);
    let mut hash = FNV_OFFSET_BASIS;
    for edge in edges.iter() {
        fnv1a_update(&mut hash, &edge.src.to_le_bytes());
        fnv1a_update(&mut hash, &edge.dst.to_le_bytes());
        fnv1a_update(&mut hash, &edge.edge_type.to_le_bytes());
        fnv1a_update(&mut hash, &edge.ts.to_le_bytes());
        fnv1a_update(&mut hash, &[edge.marker as u8]);
    }
    hash
}

/// Fold the per-sample (src, digest) pairs into one entry-level digest so a whole edge-type
/// entry can be compared with a single value before drilling into per-sample digests.
fn storage_bench_entry_digest(samples: &[(u64, u64)]) -> u64 {
    let mut hash = FNV_OFFSET_BASIS;
    for (src, digest) in samples {
        fnv1a_update(&mut hash, &src.to_le_bytes());
        fnv1a_update(&mut hash, &digest.to_le_bytes());
    }
    hash
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

const P10_RAW_SCHEMA_VERSION: &str = "p10-seml0-storage-bench-raw-v1";
const P10_RAW_TIMING_BOUNDARY: &str =
    "typed-neighbor-call-plus-result-materialization-and-digest-v1";

#[derive(Debug, Clone)]
struct P10PlanQuery {
    truth: TruthRow,
    original_src: u64,
    degree: u64,
}

#[derive(Debug, Clone)]
struct P10RawObservation {
    phase: &'static str,
    pass_index: usize,
    truth: TruthRow,
    actual: Option<DenseDigest>,
    status: &'static str,
    latency_ns: u64,
}

#[derive(Debug, Clone, Serialize)]
struct P10RawPhaseSummary {
    passes: usize,
    requested_queries: usize,
    completed_queries: usize,
    timeout_queries: usize,
    mismatch_queries: usize,
    started_monotonic_ns: u64,
    ended_monotonic_ns: u64,
    elapsed_ns: u64,
}

fn p10_validate_plan(
    plan: &StorageBenchSamplePlan,
    truth_rows: &[TruthRow],
    ids: &SharedIdMap,
) -> Result<Vec<P10PlanQuery>> {
    if plan.version != 1 || plan.source != "shared-truth-tsv" {
        bail!(
            "P10 sample plan must be version 1 from shared-truth-tsv, found version={} source={:?}",
            plan.version,
            plan.source
        );
    }
    if plan.src_label.is_some() || plan.dst_label.is_some() {
        bail!("P10 sample plan must not carry source/destination label filters");
    }
    let sample_count: usize = plan.entries.iter().map(|entry| entry.samples.len()).sum();
    if sample_count != truth_rows.len() {
        bail!(
            "P10 sample plan query count {} differs from truth count {}",
            sample_count,
            truth_rows.len()
        );
    }

    let mut queries = Vec::with_capacity(truth_rows.len());
    for entry in &plan.entries {
        if entry.src_label.is_some() || entry.dst_label.is_some() {
            bail!("P10 sample-plan entries must not carry label filters");
        }
        let edge_type = entry
            .edge_type
            .context("P10 sample-plan entry is missing a typed edge_type")?;
        for sample in &entry.samples {
            let truth = truth_rows
                .get(queries.len())
                .context("P10 sample plan contains more queries than truth")?;
            if edge_type != truth.edge_type {
                bail!(
                    "P10 plan/truth order mismatch at query {}: edge_type {} != {}",
                    truth.query_index,
                    edge_type,
                    truth.edge_type
                );
            }
            let original_src = ids.original_for_dense(truth.src).with_context(|| {
                format!(
                    "P10 truth query {} references unmapped dense source {}",
                    truth.query_index, truth.src
                )
            })?;
            if sample.src != original_src {
                bail!(
                    "P10 plan/truth order mismatch at query {}: original src {} != {}",
                    truth.query_index,
                    sample.src,
                    original_src
                );
            }
            if sample.degree != truth.count {
                bail!(
                    "P10 plan/truth degree mismatch at query {}: {} != {}",
                    truth.query_index,
                    sample.degree,
                    truth.count
                );
            }
            queries.push(P10PlanQuery {
                truth: truth.clone(),
                original_src,
                degree: sample.degree,
            });
        }
    }
    if queries.len() != truth_rows.len() {
        bail!("P10 sample plan omitted one or more truth queries");
    }
    Ok(queries)
}

fn p10_monotonic_ns() -> Result<u64> {
    let mut value = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    // SAFETY: value points to writable timespec storage for the duration of
    // the libc call, and CLOCK_MONOTONIC is supported on the Linux runner.
    let rc = unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut value) };
    if rc != 0 || value.tv_sec < 0 || value.tv_nsec < 0 {
        bail!(
            "clock_gettime(CLOCK_MONOTONIC) failed: {}",
            std::io::Error::last_os_error()
        );
    }
    let seconds = u64::try_from(value.tv_sec).context("CLOCK_MONOTONIC seconds overflow")?;
    let nanos = u64::try_from(value.tv_nsec).context("CLOCK_MONOTONIC nanos overflow")?;
    seconds
        .checked_mul(1_000_000_000)
        .and_then(|base| base.checked_add(nanos))
        .context("CLOCK_MONOTONIC nanoseconds overflow")
}

async fn run_p10_phase(
    engine: &Engine,
    snapshot: u64,
    queries: &[P10PlanQuery],
    ids: &SharedIdMap,
    phase: &'static str,
    passes: usize,
    semantic_degree_hint: bool,
    force_signature: bool,
    timeout_ms: u64,
) -> Result<(Vec<P10RawObservation>, P10RawPhaseSummary)> {
    let timeout_ns = timeout_ms
        .checked_mul(1_000_000)
        .context("P10 timeout nanoseconds overflow")?;
    let deadline = Duration::from_millis(timeout_ms);
    let phase_started = p10_monotonic_ns()?;
    let mut observations = Vec::with_capacity(queries.len() * passes);
    let mut completed = 0usize;
    let mut timeouts = 0usize;
    let mut mismatches = 0usize;

    for pass_index in 0..passes {
        for query in queries {
            let query_started = p10_monotonic_ns()?;
            let outcome = tokio::time::timeout(
                deadline,
                run_storage_bench_query(
                    engine,
                    snapshot,
                    query.original_src,
                    Some(query.truth.edge_type),
                    semantic_degree_hint,
                    force_signature,
                    query.degree,
                    None,
                    StorageBenchPropertyPredicateMode::None,
                    0,
                    0,
                    0,
                ),
            )
            .await;

            let mut actual = None;
            let mut status = "timeout";
            match outcome {
                Ok(result) => {
                    let edges = result.with_context(|| {
                        format!(
                            "P10 typed-neighbor query {} failed",
                            query.truth.query_index
                        )
                    })?;
                    let mut digest = DenseDigest::default();
                    for edge in edges {
                        let dense_dst = ids.dense_for_original(edge.dst).with_context(|| {
                            format!(
                                "P10 query {} returned unmapped original destination {}",
                                query.truth.query_index, edge.dst
                            )
                        })?;
                        digest.add(dense_dst);
                    }
                    actual = Some(digest);
                    status = "ok";
                }
                Err(_) => {}
            }
            let query_ended = p10_monotonic_ns()?;
            let mut latency_ns = query_ended.saturating_sub(query_started);
            if latency_ns > timeout_ns {
                actual = None;
                status = "timeout";
            }
            if status == "timeout" {
                latency_ns = latency_ns.max(timeout_ns);
                timeouts += 1;
            } else {
                completed += 1;
                if actual != Some(query.truth.digest()) {
                    mismatches += 1;
                }
            }
            observations.push(P10RawObservation {
                phase,
                pass_index,
                truth: query.truth.clone(),
                actual,
                status,
                latency_ns,
            });
        }
    }
    let phase_ended = p10_monotonic_ns()?;
    Ok((
        observations,
        P10RawPhaseSummary {
            passes,
            requested_queries: passes * queries.len(),
            completed_queries: completed,
            timeout_queries: timeouts,
            mismatch_queries: mismatches,
            started_monotonic_ns: phase_started,
            ended_monotonic_ns: phase_ended,
            elapsed_ns: phase_ended.saturating_sub(phase_started),
        },
    ))
}

fn p10_write_raw_artifacts(
    output_dir: &Path,
    observations: &[P10RawObservation],
    warmup: &P10RawPhaseSummary,
    measured: &P10RawPhaseSummary,
    result: &Value,
) -> Result<()> {
    fs::create_dir_all(output_dir)
        .with_context(|| format!("create P10 raw output directory {}", output_dir.display()))?;
    let observation_path = output_dir.join("p10-raw-observations.tsv");
    let event_path = output_dir.join("p10-raw-phase-events.jsonl");
    let result_path = output_dir.join("p10-raw-result.json");
    for path in [&observation_path, &event_path, &result_path] {
        if path.exists() {
            bail!("P10 raw output refuses to overwrite {}", path.display());
        }
    }
    let suffix = format!(".tmp.{}", std::process::id());
    let observation_tmp = observation_path.with_file_name(format!(
        "{}{}",
        observation_path.file_name().unwrap().to_string_lossy(),
        suffix
    ));
    let event_tmp = event_path.with_file_name(format!(
        "{}{}",
        event_path.file_name().unwrap().to_string_lossy(),
        suffix
    ));
    let result_tmp = result_path.with_file_name(format!(
        "{}{}",
        result_path.file_name().unwrap().to_string_lossy(),
        suffix
    ));

    {
        let mut writer = BufWriter::new(fs::File::create(&observation_tmp)?);
        writeln!(
            writer,
            "phase\tpass_index\tquery_index\tedge_type\tsrc\texpected_count\tactual_count\texpected_sum_hash\tactual_sum_hash\texpected_xor_hash\tactual_xor_hash\tstatus\tlatency_ns"
        )?;
        for row in observations {
            let (actual_count, actual_sum, actual_xor) = row
                .actual
                .map(|digest| {
                    (
                        digest.count.to_string(),
                        digest.sum_hash.to_string(),
                        digest.xor_hash.to_string(),
                    )
                })
                .unwrap_or_else(|| (String::new(), String::new(), String::new()));
            writeln!(
                writer,
                "{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}",
                row.phase,
                row.pass_index,
                row.truth.query_index,
                row.truth.edge_type,
                row.truth.src,
                row.truth.count,
                actual_count,
                row.truth.sum_hash,
                actual_sum,
                row.truth.xor_hash,
                actual_xor,
                row.status,
                row.latency_ns,
            )?;
        }
        writer.flush()?;
    }
    {
        let mut writer = BufWriter::new(fs::File::create(&event_tmp)?);
        for (phase, event, timestamp) in [
            ("warmup", "start", warmup.started_monotonic_ns),
            ("warmup", "end", warmup.ended_monotonic_ns),
            ("measured", "start", measured.started_monotonic_ns),
            ("measured", "end", measured.ended_monotonic_ns),
        ] {
            serde_json::to_writer(
                &mut writer,
                &json!({"phase": phase, "event": event, "monotonic_ns": timestamp}),
            )?;
            writeln!(writer)?;
        }
        writer.flush()?;
    }
    fs::write(&result_tmp, serde_json::to_vec_pretty(result)?)?;
    fs::rename(&observation_tmp, &observation_path)?;
    fs::rename(&event_tmp, &event_path)?;
    // The result is the completion marker and is deliberately published last.
    fs::rename(&result_tmp, &result_path)?;
    Ok(())
}

#[allow(clippy::too_many_arguments)]
async fn run_p10_storage_bench(
    engine: &Engine,
    plan: &StorageBenchSamplePlan,
    truth_rows: &[TruthRow],
    ids: &SharedIdMap,
    semantic_degree_hint: bool,
    force_signature: bool,
    warmup_passes: usize,
    measured_passes: usize,
    timeout_ms: u64,
    output_dir: &Path,
) -> Result<Value> {
    let queries = p10_validate_plan(plan, truth_rows, ids)?;
    let snapshot = engine.current_snapshot();
    let effective_force_signature = force_signature || plan.force_signature;
    let (mut observations, warmup) = run_p10_phase(
        engine,
        snapshot,
        &queries,
        ids,
        "warmup",
        warmup_passes,
        semantic_degree_hint,
        effective_force_signature,
        timeout_ms,
    )
    .await?;
    let (measured_observations, measured) = run_p10_phase(
        engine,
        snapshot,
        &queries,
        ids,
        "measured",
        measured_passes,
        semantic_degree_hint,
        effective_force_signature,
        timeout_ms,
    )
    .await?;
    observations.extend(measured_observations);
    let result = json!({
        "schema_version": P10_RAW_SCHEMA_VERSION,
        "clock": "CLOCK_MONOTONIC",
        "timing_boundary": P10_RAW_TIMING_BOUNDARY,
        "snapshot": snapshot,
        "query_count": truth_rows.len(),
        "semantic_degree_hint": semantic_degree_hint,
        "force_signature": effective_force_signature,
        "per_query_timeout_ms": timeout_ms,
        "mapping_hash": ids.mapping_hash(),
        "id_map_vertex_count": ids.vertex_count(),
        "warmup": warmup,
        "measured": measured,
    });
    p10_write_raw_artifacts(output_dir, &observations, &warmup, &measured, &result)?;
    Ok(result)
}

struct StorageBenchRoundResult {
    neighbor_edges: usize,
    one_hop_edges: usize,
    two_hop_edges: usize,
    two_hop_sources: usize,
    get_neighbors_elapsed_ms: u64,
    workload_mode: StorageBenchWorkloadMode,
    property_predicate_mode: StorageBenchPropertyPredicateMode,
    property_id: u32,
    property_value_i64: i64,
    property_default_i64: i64,
    two_hop_fanout: usize,
    query_cpu_total_ns: Option<u64>,
    neighbor_summary: Value,
    neighbor_metrics: Value,
    entry_result_digest: Option<String>,
    result_digests: Option<Value>,
}

async fn run_storage_bench_round(
    engine: &Engine,
    snapshot: u64,
    entry: &StorageBenchSampleEntry,
    semantic_degree_hint: bool,
    force_signature: bool,
    workload_mode: StorageBenchWorkloadMode,
    property_predicate_mode: StorageBenchPropertyPredicateMode,
    property_id: u32,
    property_value_i64: i64,
    property_default_i64: i64,
    two_hop_fanout: usize,
    reset_metrics: bool,
    emit_result_digests: bool,
    measure_process_cpu: bool,
) -> Result<StorageBenchRoundResult> {
    let metrics = engine.metrics();
    if reset_metrics {
        metrics.reset_observations_preserve_controller();
    }
    let process_cpu_started_ns = measure_process_cpu.then(process_cpu_time_ns).flatten();
    let requested_edge_type = entry.edge_type;
    let degree_by_src: HashMap<u64, u64> = entry
        .samples
        .iter()
        .map(|sample| (sample.src, sample.degree))
        .collect();
    let mut neighbor_edges = 0usize;
    let mut one_hop_edges = 0usize;
    let mut two_hop_edges = 0usize;
    let mut two_hop_sources = 0usize;
    let mut per_sample_digests = Vec::new();
    let mut folded_digests = Vec::new();
    let neighbor_started = Instant::now();
    for sample in &entry.samples {
        let mut first_hop = run_storage_bench_query(
            engine,
            snapshot,
            sample.src,
            requested_edge_type,
            semantic_degree_hint,
            force_signature,
            degree_by_src.get(&sample.src).copied().unwrap_or(0),
            entry.dst_label,
            property_predicate_mode,
            property_id,
            property_value_i64,
            property_default_i64,
        )
        .await?;
        if emit_result_digests {
            let digest = storage_bench_result_digest(&mut first_hop);
            folded_digests.push((sample.src, digest));
            per_sample_digests.push(json!({
                "src": sample.src,
                "degree": sample.degree,
                "edge_type": requested_edge_type,
                "dst_label": entry.dst_label,
                "property_predicate_mode": property_predicate_mode,
                "result_count": first_hop.len(),
                "result_digest": format!("{:016x}", digest),
            }));
        }
        one_hop_edges += first_hop.len();
        neighbor_edges += first_hop.len();

        if workload_mode == StorageBenchWorkloadMode::TwoHop {
            for edge in truncated_two_hop_frontier(&first_hop, two_hop_fanout) {
                let second_edge_type = requested_edge_type.or(Some(edge.edge_type));
                let second_hop = run_storage_bench_query(
                    engine,
                    snapshot,
                    edge.dst,
                    second_edge_type,
                    false,
                    false,
                    0,
                    None,
                    StorageBenchPropertyPredicateMode::None,
                    property_id,
                    property_value_i64,
                    property_default_i64,
                )
                .await?;
                two_hop_sources += 1;
                two_hop_edges += second_hop.len();
                neighbor_edges += second_hop.len();
            }
        }
    }
    let get_neighbors_elapsed_ms = neighbor_started.elapsed().as_millis() as u64;
    let query_cpu_total_ns = process_cpu_started_ns
        .zip(process_cpu_time_ns())
        .map(|(start, finish)| finish.saturating_sub(start));
    let neighbor_metrics = metrics.snapshot_json();
    let neighbor_summary = storage_bench_metric_summary(&neighbor_metrics);
    let entry_result_digest = emit_result_digests
        .then(|| format!("{:016x}", storage_bench_entry_digest(&folded_digests)));
    let result_digests = emit_result_digests.then(|| Value::Array(per_sample_digests));
    Ok(StorageBenchRoundResult {
        neighbor_edges,
        one_hop_edges,
        two_hop_edges,
        two_hop_sources,
        get_neighbors_elapsed_ms,
        workload_mode,
        property_predicate_mode,
        property_id,
        property_value_i64,
        property_default_i64,
        two_hop_fanout,
        query_cpu_total_ns,
        neighbor_summary,
        neighbor_metrics,
        entry_result_digest,
        result_digests,
    })
}

async fn run_storage_bench_query(
    engine: &Engine,
    snapshot: u64,
    src: u64,
    requested_edge_type: Option<i32>,
    semantic_degree_hint: bool,
    force_signature: bool,
    degree: u64,
    dst_label: Option<i32>,
    property_predicate_mode: StorageBenchPropertyPredicateMode,
    property_id: u32,
    property_value_i64: i64,
    property_default_i64: i64,
) -> Result<Vec<EdgeRecord>> {
    match property_predicate_mode {
        StorageBenchPropertyPredicateMode::None => {
            if let Some(edge_type) = requested_edge_type {
                if force_signature || semantic_degree_hint || dst_label.is_some() {
                    engine
                        .get_neighbors_by_signature_builder(snapshot, || {
                            storage_bench_signature(
                                src,
                                Some(edge_type),
                                semantic_degree_hint,
                                degree,
                                dst_label,
                            )
                        })
                        .await
                } else {
                    engine.get_neighbors_typed(src, edge_type, snapshot).await
                }
            } else if force_signature || dst_label.is_some() {
                engine
                    .get_neighbors_by_signature_builder(snapshot, || {
                        storage_bench_signature(src, None, semantic_degree_hint, degree, dst_label)
                    })
                    .await
            } else {
                engine.get_neighbors(src, snapshot).await
            }
        }
        StorageBenchPropertyPredicateMode::RequiredProperty => {
            engine
                .get_neighbors_by_signature_builder(snapshot, || {
                    storage_bench_signature(
                        src,
                        requested_edge_type,
                        semantic_degree_hint,
                        degree,
                        dst_label,
                    )
                    .with_required_property(property_id)
                })
                .await
        }
        StorageBenchPropertyPredicateMode::Presence => {
            let edges = engine
                .get_neighbors_with_present_property_prototype(
                    src,
                    requested_edge_type,
                    snapshot,
                    property_id,
                )
                .await?;
            Ok(storage_bench_filter_dst_label(dst_label, edges))
        }
        StorageBenchPropertyPredicateMode::Equality => {
            let edges = engine
                .get_neighbors_matching_csr_property_value_prototype(
                    src,
                    requested_edge_type,
                    snapshot,
                    CsrPropertyValuePredicate::equals(
                        property_id,
                        PropertyValue::I64(property_value_i64),
                    ),
                )
                .await?;
            Ok(storage_bench_filter_dst_label(dst_label, edges))
        }
        StorageBenchPropertyPredicateMode::AbsentDefault => {
            let edges = engine
                .get_neighbors_matching_csr_property_value_prototype(
                    src,
                    requested_edge_type,
                    snapshot,
                    CsrPropertyValuePredicate::equals_with_schema_default(
                        property_id,
                        PropertyValue::I64(property_value_i64),
                        PropertyValue::I64(property_default_i64),
                    ),
                )
                .await?;
            Ok(storage_bench_filter_dst_label(dst_label, edges))
        }
    }
}

fn storage_bench_signature(
    src: u64,
    requested_edge_type: Option<i32>,
    semantic_degree_hint: bool,
    degree: u64,
    dst_label: Option<i32>,
) -> GraphAccessSignature {
    let mut signature = GraphAccessSignature::neighbor_scan(src, requested_edge_type);
    if semantic_degree_hint {
        signature = signature.with_degree_class(DegreeClass::from_max_degree(degree));
    }
    if let Some(dst_label) = dst_label {
        signature = signature.with_dst_label(dst_label);
    }
    signature
}

fn storage_bench_filter_dst_label(
    dst_label: Option<i32>,
    edges: Vec<EdgeRecord>,
) -> Vec<EdgeRecord> {
    let Some(dst_label) = dst_label else {
        return edges;
    };
    edges
        .into_iter()
        .filter(|edge| source_label_from_vertex_id(edge.dst) == dst_label)
        .collect()
}

fn truncated_two_hop_frontier(edges: &[EdgeRecord], limit: usize) -> Vec<EdgeRecord> {
    let mut out = edges.to_vec();
    out.sort_unstable_by_key(|edge| (edge.edge_type, edge.dst));
    out.truncate(limit);
    out
}

fn storage_bench_round_json(kind: &str, round: usize, result: StorageBenchRoundResult) -> Value {
    json!({
        "kind": kind,
        "round": round,
        "workload_mode": result.workload_mode,
        "property_predicate_mode": result.property_predicate_mode,
        "property_id": result.property_id,
        "property_value_i64": result.property_value_i64,
        "property_default_i64": result.property_default_i64,
        "two_hop_fanout": result.two_hop_fanout,
        "neighbor_edges": result.neighbor_edges,
        "one_hop_edges": result.one_hop_edges,
        "two_hop_edges": result.two_hop_edges,
        "two_hop_sources": result.two_hop_sources,
        "get_neighbors_elapsed_ms": result.get_neighbors_elapsed_ms,
        "query_cpu_total_ns": result.query_cpu_total_ns,
        "routed_l0_segments": result.neighbor_summary["routed_l0_segments"].clone(),
        "candidate_l0_segments": result.neighbor_summary["candidate_l0_segments"].clone(),
        "body_reads": result.neighbor_summary["body_reads"].clone(),
        "body_bytes": result.neighbor_summary["body_bytes"].clone(),
        "query_cpu_query_setup_ns": result.neighbor_summary["query_cpu_query_setup_ns"].clone(),
        "query_cpu_metadata_admission_ns": result.neighbor_summary["query_cpu_metadata_admission_ns"].clone(),
        "query_cpu_routing_index_ns": result.neighbor_summary["query_cpu_routing_index_ns"].clone(),
        "query_cpu_body_decode_filter_ns": result.neighbor_summary["query_cpu_body_decode_filter_ns"].clone(),
        "query_cpu_mvcc_result_ns": result.neighbor_summary["query_cpu_mvcc_result_ns"].clone(),
        "query_cpu_clock_failures": result.neighbor_summary["query_cpu_clock_failures"].clone(),
        "get_neighbors_latency_sum_us": result.neighbor_metrics["storage"]["get_neighbors_latency"]["sum_us"].clone(),
        "entry_result_digest": result.entry_result_digest,
        "result_digests": result.result_digests,
        "neighbor_summary": result.neighbor_summary,
        "neighbor_metrics": result.neighbor_metrics,
    })
}

fn build_storage_bench_sample_plan(
    edges: &[EdgeRecord],
    requested_edge_types: &[Option<i32>],
    samples: usize,
    src_label: Option<i32>,
    dst_label: Option<i32>,
    semantic_degree_hint: bool,
    force_signature: bool,
    sample_plan_degree_hint: bool,
) -> StorageBenchSamplePlan {
    let record_sample_degrees = semantic_degree_hint || sample_plan_degree_hint;
    let mut entries = Vec::new();
    for requested_edge_type in requested_edge_types {
        let mut candidate_edges = 0usize;
        let mut candidate_srcs = Vec::new();
        for edge in edges {
            if storage_bench_edge_matches(edge, *requested_edge_type, src_label, dst_label) {
                candidate_edges += 1;
                candidate_srcs.push(edge.src);
            }
        }
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
        let mut degree_by_src = HashMap::new();
        if record_sample_degrees {
            for src in &sampled_srcs {
                degree_by_src.insert(*src, 0);
            }
            if !degree_by_src.is_empty() {
                for edge in edges {
                    if storage_bench_edge_matches(edge, *requested_edge_type, src_label, dst_label)
                    {
                        if let Some(degree) = degree_by_src.get_mut(&edge.src) {
                            *degree += 1;
                        }
                    }
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
            src_label,
            dst_label,
            candidate_edges_for_sampling: candidate_edges,
            candidate_sources_for_sampling: candidate_sources,
            samples: sampled,
        });
    }
    StorageBenchSamplePlan {
        version: 1,
        source: "scan".to_string(),
        samples_per_edge_type: samples,
        semantic_degree_hint: record_sample_degrees,
        force_signature,
        src_label,
        dst_label,
        entries,
    }
}

fn storage_bench_edge_matches(
    edge: &EdgeRecord,
    requested_edge_type: Option<i32>,
    src_label: Option<i32>,
    dst_label: Option<i32>,
) -> bool {
    requested_edge_type
        .map(|wanted| edge.edge_type == wanted)
        .unwrap_or(true)
        && src_label
            .map(|wanted| source_label_from_vertex_id(edge.src) == wanted)
            .unwrap_or(true)
        && dst_label
            .map(|wanted| source_label_from_vertex_id(edge.dst) == wanted)
            .unwrap_or(true)
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
        "routed_l0_segments": metric_u64(metrics, &["csr", "routed_l0_segments"]),
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
        "query_cpu_query_setup_ns": metric_u64(metrics, &["storage", "query_cpu_ns", "query_setup"]),
        "query_cpu_metadata_admission_ns": metric_u64(metrics, &["storage", "query_cpu_ns", "metadata_admission"]),
        "query_cpu_routing_index_ns": metric_u64(metrics, &["storage", "query_cpu_ns", "routing_index"]),
        "query_cpu_body_decode_filter_ns": metric_u64(metrics, &["storage", "query_cpu_ns", "body_decode_filter"]),
        "query_cpu_mvcc_result_ns": metric_u64(metrics, &["storage", "query_cpu_ns", "mvcc_result"]),
        "query_cpu_clock_failures": metric_u64(metrics, &["storage", "query_cpu_ns", "clock_failures"]),
        "get_neighbors_avg_us": metric_u64(metrics, &["storage", "get_neighbors_latency", "avg_us"]),
        "get_neighbors_p50_us": metric_u64(metrics, &["storage", "get_neighbors_latency", "p50_us"]),
        "get_neighbors_p90_us": metric_u64(metrics, &["storage", "get_neighbors_latency", "p90_us"]),
        "get_neighbors_p99_us": metric_u64(metrics, &["storage", "get_neighbors_latency", "p99_us"]),
        "csr_get_neighbors_avg_us": metric_u64(metrics, &["csr", "get_neighbors_latency", "avg_us"]),
        "csr_get_neighbors_p50_us": metric_u64(metrics, &["csr", "get_neighbors_latency", "p50_us"]),
        "csr_get_neighbors_p90_us": metric_u64(metrics, &["csr", "get_neighbors_latency", "p90_us"]),
        "csr_get_neighbors_p99_us": metric_u64(metrics, &["csr", "get_neighbors_latency", "p99_us"]),
        "pruning_reasons": metrics["csr"]["pruning_reasons"].clone(),
    })
}

fn storage_bench_repeat_summary(rounds: &[Value]) -> Value {
    json!({
        "elapsed_ms": series_stats(&round_field_series(rounds, "get_neighbors_elapsed_ms")),
        "neighbor_edges": series_stats(&round_field_series(rounds, "neighbor_edges")),
        "one_hop_edges": series_stats(&round_field_series(rounds, "one_hop_edges")),
        "two_hop_edges": series_stats(&round_field_series(rounds, "two_hop_edges")),
        "two_hop_sources": series_stats(&round_field_series(rounds, "two_hop_sources")),
        "routed_l0_segments": series_stats(&round_summary_series(rounds, "routed_l0_segments")),
        "candidate_l0_segments": series_stats(&round_summary_series(rounds, "candidate_l0_segments")),
        "read_bytes": series_stats(&round_summary_series(rounds, "read_bytes")),
        "body_reads": series_stats(&round_summary_series(rounds, "body_reads")),
        "body_bytes": series_stats(&round_summary_series(rounds, "body_bytes")),
        "query_cpu_query_setup_ns": series_stats(&round_summary_series(rounds, "query_cpu_query_setup_ns")),
        "query_cpu_metadata_admission_ns": series_stats(&round_summary_series(rounds, "query_cpu_metadata_admission_ns")),
        "query_cpu_routing_index_ns": series_stats(&round_summary_series(rounds, "query_cpu_routing_index_ns")),
        "query_cpu_body_decode_filter_ns": series_stats(&round_summary_series(rounds, "query_cpu_body_decode_filter_ns")),
        "query_cpu_mvcc_result_ns": series_stats(&round_summary_series(rounds, "query_cpu_mvcc_result_ns")),
        "query_cpu_total_ns": series_stats(&round_field_series(rounds, "query_cpu_total_ns")),
        "query_cpu_clock_failures": series_stats(&round_summary_series(rounds, "query_cpu_clock_failures")),
        "get_neighbors_latency_sum_us": series_stats(&round_metric_latency_series(rounds, "sum_us")),
        "get_neighbors_avg_us": series_stats(&round_summary_series(rounds, "get_neighbors_avg_us")),
        "get_neighbors_p50_us": series_stats(&round_summary_series(rounds, "get_neighbors_p50_us")),
        "get_neighbors_p90_us": series_stats(&round_summary_series(rounds, "get_neighbors_p90_us")),
        "get_neighbors_p99_us": series_stats(&round_summary_series(rounds, "get_neighbors_p99_us")),
        "csr_get_neighbors_avg_us": series_stats(&round_summary_series(rounds, "csr_get_neighbors_avg_us")),
        "csr_get_neighbors_p50_us": series_stats(&round_summary_series(rounds, "csr_get_neighbors_p50_us")),
        "csr_get_neighbors_p90_us": series_stats(&round_summary_series(rounds, "csr_get_neighbors_p90_us")),
        "csr_get_neighbors_p99_us": series_stats(&round_summary_series(rounds, "csr_get_neighbors_p99_us")),
    })
}

fn round_field_series(rounds: &[Value], key: &str) -> Vec<f64> {
    rounds
        .iter()
        .filter_map(|round| round.get(key).and_then(Value::as_f64))
        .collect()
}

fn round_summary_series(rounds: &[Value], key: &str) -> Vec<f64> {
    rounds
        .iter()
        .filter_map(|round| {
            round
                .get("neighbor_summary")
                .and_then(|summary| summary.get(key))
                .and_then(Value::as_f64)
        })
        .collect()
}

fn round_metric_latency_series(rounds: &[Value], key: &str) -> Vec<f64> {
    rounds
        .iter()
        .filter_map(|round| {
            round
                .get("neighbor_metrics")
                .and_then(|metrics| metrics.get("storage"))
                .and_then(|storage| storage.get("get_neighbors_latency"))
                .and_then(|latency| latency.get(key))
                .and_then(Value::as_f64)
        })
        .collect()
}

fn series_stats(values: &[f64]) -> Value {
    if values.is_empty() {
        return Value::Null;
    }
    let count = values.len() as f64;
    let sum: f64 = values.iter().sum();
    let mean = sum / count;
    let variance = values
        .iter()
        .map(|value| {
            let delta = value - mean;
            delta * delta
        })
        .sum::<f64>()
        / count;
    let min = values.iter().copied().fold(f64::INFINITY, f64::min);
    let max = values.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    json!({
        "count": values.len(),
        "mean": mean,
        "stddev": variance.sqrt(),
        "min": min,
        "max": max,
    })
}

fn storage_bench_cache_state() -> Value {
    let mut meminfo = serde_json::Map::new();
    if let Ok(text) = fs::read_to_string("/proc/meminfo") {
        for line in text.lines() {
            let Some((key, rest)) = line.split_once(':') else {
                continue;
            };
            if matches!(
                key,
                "MemAvailable" | "MemFree" | "Buffers" | "Cached" | "SwapFree" | "SwapTotal"
            ) {
                if let Some(value) = rest.split_whitespace().next() {
                    if let Ok(kib) = value.parse::<u64>() {
                        meminfo.insert(key.to_string(), json!(kib));
                    }
                }
            }
        }
    }
    let loadavg = fs::read_to_string("/proc/loadavg")
        .ok()
        .map(|text| text.trim().to_string());
    let unix_epoch_s = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .ok()
        .map(|duration| duration.as_secs());
    json!({
        "unix_epoch_s": unix_epoch_s,
        "loadavg": loadavg,
        "meminfo_kib": meminfo,
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shared_truth_plan_is_storage_bench_compatible() -> Result<()> {
        let ids = SharedIdMap::from_dense_to_original(vec![100, 400])?;
        let rows = vec![lsmgraph::shared_truth::TruthRow {
            query_index: 0,
            edge_type: -7,
            src: 1,
            count: 3,
            sum_hash: 4,
            xor_hash: 5,
        }];
        let shared = build_storage_sample_plan(&rows, &ids, true, false)?;
        let encoded = serde_json::to_string(&shared)?;
        let parsed: StorageBenchSamplePlan = serde_json::from_str(&encoded)?;
        assert_eq!(parsed.source, "shared-truth-tsv");
        assert_eq!(parsed.entries.len(), 1);
        assert_eq!(parsed.entries[0].edge_type, Some(-7));
        assert_eq!(parsed.entries[0].samples[0].src, 400);
        assert_eq!(parsed.entries[0].samples[0].degree, 3);
        Ok(())
    }

    #[test]
    fn p10_plan_contract_rejects_truth_reordering() -> Result<()> {
        let ids = SharedIdMap::from_dense_to_original(vec![100, 400])?;
        let rows = vec![lsmgraph::shared_truth::TruthRow {
            query_index: 0,
            edge_type: 7,
            src: 1,
            count: 0,
            sum_hash: 0,
            xor_hash: 0,
        }];
        let shared = build_storage_sample_plan(&rows, &ids, false, false)?;
        let encoded = serde_json::to_string(&shared)?;
        let mut parsed: StorageBenchSamplePlan = serde_json::from_str(&encoded)?;
        parsed.entries[0].samples[0].src = 100;
        let error = p10_validate_plan(&parsed, &rows, &ids).unwrap_err();
        assert!(error.to_string().contains("plan/truth order mismatch"));
        Ok(())
    }

    #[tokio::test]
    async fn p10_raw_mode_runs_whole_trace_warmup_then_measured() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let engine = Engine::create(LsmGraphConfig::new(temp.path().join("store"))).await?;
        engine.insert_edge(100, 200, 7).await?;
        engine.insert_edge(100, 300, 7).await?;
        let ids = SharedIdMap::from_dense_to_original(vec![100, 200, 300])?;
        let mut digest = DenseDigest::default();
        digest.add(1);
        digest.add(2);
        let rows = vec![TruthRow {
            query_index: 0,
            edge_type: 7,
            src: 0,
            count: digest.count,
            sum_hash: digest.sum_hash,
            xor_hash: digest.xor_hash,
        }];
        let shared = build_storage_sample_plan(&rows, &ids, false, false)?;
        let plan: StorageBenchSamplePlan = serde_json::from_str(&serde_json::to_string(&shared)?)?;
        let output = temp.path().join("raw");
        let summary = run_p10_storage_bench(
            &engine, &plan, &rows, &ids, false, false, 1, 1, 1_000, &output,
        )
        .await?;
        assert_eq!(summary["warmup"]["mismatch_queries"], 0);
        assert_eq!(summary["measured"]["mismatch_queries"], 0);
        assert!(output.join("p10-raw-observations.tsv").is_file());
        assert!(output.join("p10-raw-phase-events.jsonl").is_file());
        assert!(output.join("p10-raw-result.json").is_file());
        Ok(())
    }

    #[test]
    fn w4_two_hop_frontier_truncation_is_stable() {
        let edges = vec![
            EdgeRecord::insert(1, 30, 2, 1),
            EdgeRecord::insert(1, 10, 1, 2),
            EdgeRecord::insert(1, 20, 1, 3),
            EdgeRecord::insert(1, 40, 2, 4),
        ];

        let truncated = truncated_two_hop_frontier(&edges, 3);
        let keys: Vec<_> = truncated
            .iter()
            .map(|edge| (edge.edge_type, edge.dst))
            .collect();

        assert_eq!(keys, vec![(1, 10), (1, 20), (2, 30)]);
        assert_eq!(truncated_two_hop_frontier(&edges, 64).len(), edges.len());
        assert!(truncated_two_hop_frontier(&edges, 0).is_empty());
    }

    #[test]
    fn result_digest_is_order_independent_and_deterministic() {
        let mut a = vec![
            EdgeRecord::insert(1, 30, 2, 1),
            EdgeRecord::insert(1, 10, 1, 2),
            EdgeRecord::insert(1, 20, 1, 3),
        ];
        let mut b = vec![
            EdgeRecord::insert(1, 20, 1, 3),
            EdgeRecord::insert(1, 30, 2, 1),
            EdgeRecord::insert(1, 10, 1, 2),
        ];
        let digest_a = storage_bench_result_digest(&mut a);
        let digest_b = storage_bench_result_digest(&mut b);
        assert_eq!(digest_a, digest_b, "digest must be order-independent");
        // Recomputing the (now sorted) input is stable.
        assert_eq!(digest_a, storage_bench_result_digest(&mut a));
        // A fixed hex width so schema/variant JSON strings compare byte-for-byte.
        assert_eq!(format!("{:016x}", digest_a).len(), 16);
    }

    #[test]
    fn result_digest_detects_edge_set_changes() {
        let mut base = vec![
            EdgeRecord::insert(1, 10, 1, 2),
            EdgeRecord::insert(1, 20, 1, 3),
        ];
        let mut extra = base.clone();
        extra.push(EdgeRecord::insert(1, 30, 1, 4));
        let mut dst_changed = vec![
            EdgeRecord::insert(1, 11, 1, 2),
            EdgeRecord::insert(1, 20, 1, 3),
        ];
        let mut marker_changed = vec![
            EdgeRecord::delete(1, 10, 1, 2),
            EdgeRecord::insert(1, 20, 1, 3),
        ];
        let baseline = storage_bench_result_digest(&mut base);
        assert_ne!(baseline, storage_bench_result_digest(&mut extra));
        assert_ne!(baseline, storage_bench_result_digest(&mut dst_changed));
        assert_ne!(baseline, storage_bench_result_digest(&mut marker_changed));
        // Empty result is well-defined and distinct from any non-empty set.
        let mut empty: Vec<EdgeRecord> = Vec::new();
        assert_ne!(baseline, storage_bench_result_digest(&mut empty));
    }

    #[test]
    fn entry_digest_folds_per_sample_digests() {
        let samples = vec![
            (1u64, 0xaaaa_aaaa_aaaa_aaaau64),
            (2u64, 0xbbbb_bbbb_bbbb_bbbbu64),
        ];
        let folded = storage_bench_entry_digest(&samples);
        // Stable and sensitive to per-sample order (each src is keyed in).
        assert_eq!(folded, storage_bench_entry_digest(&samples));
        let reordered = vec![samples[1], samples[0]];
        assert_ne!(folded, storage_bench_entry_digest(&reordered));
    }
}
