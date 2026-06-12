use std::path::PathBuf;
use std::str::FromStr;

use anyhow::Result;

use crate::schema::SchemaEpoch;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IoBackendKind {
    Blocking,
    Direct,
    Uring,
}

impl FromStr for IoBackendKind {
    type Err = anyhow::Error;

    fn from_str(s: &str) -> Result<Self> {
        match s.to_ascii_lowercase().as_str() {
            "blocking" | "pread" | "default" => Ok(Self::Blocking),
            "direct" | "direct-io" | "odirect" => Ok(Self::Direct),
            "uring" | "io-uring" | "io_uring" => Ok(Self::Uring),
            _ => anyhow::bail!("unknown io backend: {s}"),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum L0LayoutPolicy {
    Naive,
    Schema,
    LabelOnly,
    EdgeTypeOnly,
    DegreeOnly,
    Semantic,
    SemanticBudgeted,
    LsmGraphStyle,
    FullCompact,
    OracleSemantic,
    /// RocksDB-style KV-LSM baseline: CSR segments with conservative semantic metadata.
    /// This simulates: "what if RocksDB encoded edges as KV but still had CSR segments?"
    /// All semantic metadata is set to Unknown/Conservative, so may_contain_signature
    /// always returns true (no semantic pruning benefit). This proves SemL0's contribution:
    /// the semantic metadata is USELESS without the pruning logic in the read path.
    RocksDbStyle,
}

impl FromStr for L0LayoutPolicy {
    type Err = anyhow::Error;

    fn from_str(s: &str) -> Result<Self> {
        match s.to_ascii_lowercase().as_str() {
            "naive" | "plain" | "default" => Ok(Self::Naive),
            "schema" | "graph-aware" | "graph_aware" | "graph-aware-l0" => Ok(Self::Schema),
            "label-only" | "label_only" | "src-label-only" | "src_label_only" => {
                Ok(Self::LabelOnly)
            }
            "edge-type-only" | "edge_type_only" | "etype-only" | "etype_only" => {
                Ok(Self::EdgeTypeOnly)
            }
            "degree-only" | "degree_only" => Ok(Self::DegreeOnly),
            "semantic" | "query-semantic" | "query_semantic" | "qslsm" => Ok(Self::Semantic),
            "semantic-budgeted"
            | "semantic_budgeted"
            | "query-semantic-budgeted"
            | "query_semantic_budgeted"
            | "qslsm-budgeted"
            | "qslsm_budgeted" => Ok(Self::SemanticBudgeted),
            "lsmgraph-style" | "lsmgraph_style" | "lsmgraphstyle" | "lsmgraph"
            | "lsmsem-baseline" | "seml0-baseline" => Ok(Self::LsmGraphStyle),
            "full-compact" | "fullcompact" | "full_compact" | "write-optimized"
            | "write_optimized" => Ok(Self::FullCompact),
            "oracle-semantic" | "oracle_semantic" | "oracle" | "oracle-sem" | "oracle_sem" => {
                Ok(Self::OracleSemantic)
            }
            "rocksdb-style" | "rocksdb_style" | "rocksdbstyle" | "rocksdb" | "kv-lsm"
            | "kv_lsm" | "kvbaseline" | "kv-baseline" => Ok(Self::RocksDbStyle),
            _ => anyhow::bail!("unknown L0 layout policy: {s}"),
        }
    }
}

#[derive(Debug, Clone)]
pub struct LsmGraphConfig {
    pub store_dir: PathBuf,
    pub memgraph_capacity_bytes: usize,
    pub inline_segment_capacity: usize,
    pub level_fanout: usize,
    pub max_levels: usize,
    pub l0_file_threshold: usize,
    pub segment_target_bytes: usize,
    pub min_l0_partition_bytes: usize,
    pub max_l0_segments_per_flush: usize,
    pub max_background_flushes: usize,
    pub max_outstanding_io: usize,
    pub io_backend: IoBackendKind,
    pub auto_compaction: bool,
    pub schema_epoch: SchemaEpoch,
    pub l0_layout: L0LayoutPolicy,
    pub semantic_budget_min_edge_type_bytes: usize,
    pub semantic_budget_min_edge_type_score: f64,
    pub semantic_budget_core_edge_weight: f64,
    pub semantic_budget_reverse_core_edge_weight: f64,
    pub semantic_budget_other_edge_weight: f64,
    pub semantic_budget_max_extra_l0_files: Option<usize>,
    pub semantic_budget_edge_type_allowlist: Vec<i32>,
    pub semantic_budget_min_exact_bytes: usize,
    pub semantic_budget_min_benefit_score: f64,
    pub semantic_budget_degree_weight: f64,
    pub l0_ra_range_bucket_size: u64,
    pub l0_ra_min_queries: u64,
    pub l0_ra_min_l0_segments: usize,
    pub l0_ra_min_score: f64,
    /// Capacity (entries) of the CSR metadata cache (header + offset array +
    /// SourceBloom per L0/L1 file). Layouts whose L0 file count exceeds this
    /// thrash the cache and re-read whole offset arrays on every miss.
    pub metadata_cache_entries: usize,
}

impl LsmGraphConfig {
    pub fn new(store_dir: impl Into<PathBuf>) -> Self {
        Self {
            store_dir: store_dir.into(),
            memgraph_capacity_bytes: 64 * 1024 * 1024,
            inline_segment_capacity: 8,
            level_fanout: 10,
            max_levels: 5,
            l0_file_threshold: 4,
            segment_target_bytes: 64 * 1024 * 1024,
            min_l0_partition_bytes: 4 * 1024 * 1024,
            max_l0_segments_per_flush: 128,
            max_background_flushes: 2,
            max_outstanding_io: 128,
            io_backend: IoBackendKind::Blocking,
            auto_compaction: false,
            schema_epoch: 0,
            l0_layout: L0LayoutPolicy::Naive,
            semantic_budget_min_edge_type_bytes: 4 * 1024 * 1024,
            semantic_budget_min_edge_type_score: 1.0e308,
            semantic_budget_core_edge_weight: 4.0,
            semantic_budget_reverse_core_edge_weight: 2.0,
            semantic_budget_other_edge_weight: 0.5,
            semantic_budget_max_extra_l0_files: None,
            semantic_budget_edge_type_allowlist: Vec::new(),
            semantic_budget_min_exact_bytes: 4 * 1024 * 1024,
            semantic_budget_min_benefit_score: 1.0,
            semantic_budget_degree_weight: 1.0,
            l0_ra_range_bucket_size: 1 << 20,
            l0_ra_min_queries: 10,
            l0_ra_min_l0_segments: 2,
            l0_ra_min_score: 10.0,
            metadata_cache_entries: 4096,
        }
    }

    pub fn with_metadata_cache_entries(mut self, entries: usize) -> Self {
        self.metadata_cache_entries = entries.max(1);
        self
    }

    pub fn with_memgraph_capacity(mut self, bytes: usize) -> Self {
        self.memgraph_capacity_bytes = bytes;
        self
    }

    pub fn with_io_backend(mut self, backend: IoBackendKind) -> Self {
        self.io_backend = backend;
        self
    }

    pub fn with_auto_compaction(mut self, enabled: bool) -> Self {
        self.auto_compaction = enabled;
        self
    }

    pub fn with_schema_epoch(mut self, epoch: SchemaEpoch) -> Self {
        self.schema_epoch = epoch;
        self
    }

    pub fn with_graph_aware_l0(mut self, enabled: bool) -> Self {
        self.l0_layout = if enabled {
            L0LayoutPolicy::Schema
        } else {
            L0LayoutPolicy::Naive
        };
        self
    }

    pub fn with_l0_layout(mut self, policy: L0LayoutPolicy) -> Self {
        self.l0_layout = policy;
        self
    }

    pub fn with_semantic_budget_min_edge_type_bytes(mut self, bytes: usize) -> Self {
        self.semantic_budget_min_edge_type_bytes = bytes;
        self
    }

    pub fn with_semantic_budget_min_edge_type_score(mut self, score: f64) -> Self {
        self.semantic_budget_min_edge_type_score = score;
        self
    }

    pub fn with_semantic_budget_core_edge_weight(mut self, weight: f64) -> Self {
        self.semantic_budget_core_edge_weight = weight;
        self
    }

    pub fn with_semantic_budget_reverse_core_edge_weight(mut self, weight: f64) -> Self {
        self.semantic_budget_reverse_core_edge_weight = weight;
        self
    }

    pub fn with_semantic_budget_other_edge_weight(mut self, weight: f64) -> Self {
        self.semantic_budget_other_edge_weight = weight;
        self
    }

    pub fn with_semantic_budget_max_extra_l0_files(mut self, files: Option<usize>) -> Self {
        self.semantic_budget_max_extra_l0_files = files;
        self
    }

    pub fn with_semantic_budget_edge_type_allowlist(mut self, edge_types: Vec<i32>) -> Self {
        self.semantic_budget_edge_type_allowlist = edge_types;
        self
    }

    pub fn with_semantic_budget_min_exact_bytes(mut self, bytes: usize) -> Self {
        self.semantic_budget_min_exact_bytes = bytes;
        self
    }

    pub fn with_semantic_budget_min_benefit_score(mut self, score: f64) -> Self {
        self.semantic_budget_min_benefit_score = score;
        self
    }

    pub fn with_semantic_budget_degree_weight(mut self, weight: f64) -> Self {
        self.semantic_budget_degree_weight = weight;
        self
    }

    pub fn with_rocksdb_style(mut self) -> Self {
        self.l0_layout = L0LayoutPolicy::RocksDbStyle;
        self
    }

    pub fn supports_feedback_compaction(&self) -> bool {
        !matches!(
            self.l0_layout,
            L0LayoutPolicy::LsmGraphStyle
                | L0LayoutPolicy::FullCompact
                | L0LayoutPolicy::OracleSemantic
                | L0LayoutPolicy::RocksDbStyle
        )
    }
}
