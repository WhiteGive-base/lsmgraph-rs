use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{BufReader, BufWriter, Read, Write};
use std::path::{Path, PathBuf};
use std::process;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use parking_lot::{Mutex, RwLock};
use tokio::sync::Mutex as AsyncMutex;
use tokio::task::JoinHandle;

use crate::config::{L0LayoutPolicy, LsmGraphConfig};
use crate::csr::format::property_presence_bit;
use crate::csr::{
    CsrEdgeRecordWithProperties, CsrMetadataCache, CsrPropertyValuePredicate, CsrReader,
    CsrSegmentMeta, CsrSegmentSemanticOverrides, CsrWriter, EdgePropertyValue,
    EdgeRecordWithProperties, Manifest, ManifestRecord,
};
use crate::error::Result;
use crate::index::{MultiLevelIndex, VertexLockTable};
use crate::io::AnyIoBackend;
use crate::levels::{split_levels, L0, L1};
use crate::memgraph::MemGraph;
use crate::metrics::{L0PartitionKey, L0PartitionProbe, L0PartitionSnapshot, Metrics};
use crate::property_encoding::{
    encode_with_encoding, parse_default_or_null_rule, PropertyEncodingRegistry,
    PropertyPhysicalEncoding, PropertyValue,
};
use crate::schema::{
    EncodingVersion, NewPropertyEntry, PropertyId, PropertyOwner, SchemaCatalog, SchemaEpoch,
};
use crate::semantic::{DegreeClass, GraphAccessSignature, PropertyPredicate};
use crate::types::{
    source_label_from_vertex_id, EdgeMarker, EdgeRecord, EdgeType, FileId, LevelId, SnapshotId,
    VertexId, MIXED_EDGE_TYPE, UNKNOWN_SOURCE_LABEL,
};
use crate::version::{Version, VersionGuard, VersionManager};

struct EngineState {
    active: Arc<MemGraph>,
    next_file_id: FileId,
    flush_tasks: Vec<JoinHandle<Result<()>>>,
    semantic_budget_used_extra_l0_files: usize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
struct L0SemanticIndexKey {
    src_label: i32,
    edge_type: EdgeType,
    degree_class: DegreeClass,
}

#[derive(Debug, Default)]
struct SemanticL0Index {
    groups: HashMap<L0SemanticIndexKey, Vec<CsrSegmentMeta>>,
}

impl SemanticL0Index {
    fn rebuild(l0_files: &[CsrSegmentMeta]) -> Self {
        let mut groups: HashMap<L0SemanticIndexKey, Vec<CsrSegmentMeta>> = HashMap::new();
        for meta in l0_files {
            let (src_label, edge_type, degree_class) =
                if meta.summary_completeness.allows_semantic_pruning() {
                    let degree_class = if meta.degree_class_exact {
                        meta.degree_class
                    } else {
                        DegreeClass::Mixed
                    };
                    (meta.src_label, meta.edge_type_partition, degree_class)
                } else {
                    (UNKNOWN_SOURCE_LABEL, MIXED_EDGE_TYPE, DegreeClass::Unknown)
                };
            groups
                .entry(L0SemanticIndexKey {
                    src_label,
                    edge_type,
                    degree_class,
                })
                .or_default()
                .push(*meta);
        }
        for metas in groups.values_mut() {
            metas.sort_by_key(|meta| (meta.min_src, meta.max_src, meta.file_id));
        }
        Self { groups }
    }

    fn candidates_with_degree_classes(
        &self,
        signature: &GraphAccessSignature,
        degree_classes_override: Option<DegreeClassMask>,
    ) -> Vec<CsrSegmentMeta> {
        let Some(edge_type) = signature.edge_type else {
            return Vec::new();
        };
        let mut out = Vec::new();
        let labels = if signature.src_label == UNKNOWN_SOURCE_LABEL {
            vec![UNKNOWN_SOURCE_LABEL]
        } else {
            vec![signature.src_label, UNKNOWN_SOURCE_LABEL]
        };
        let edge_types = [edge_type, MIXED_EDGE_TYPE];
        let degree_classes = match degree_classes_override {
            Some(mask) => degree_classes_that_may_contain_query_mask(mask),
            None => match signature.degree_class {
                Some(DegreeClass::Low) => {
                    vec![DegreeClass::Low, DegreeClass::Mixed, DegreeClass::Unknown]
                }
                Some(DegreeClass::Medium) => vec![
                    DegreeClass::Low,
                    DegreeClass::Medium,
                    DegreeClass::Mixed,
                    DegreeClass::Unknown,
                ],
                Some(DegreeClass::High) => vec![
                    DegreeClass::Low,
                    DegreeClass::Medium,
                    DegreeClass::High,
                    DegreeClass::Mixed,
                    DegreeClass::Unknown,
                ],
                Some(DegreeClass::Mixed | DegreeClass::Unknown) | None => vec![
                    DegreeClass::Low,
                    DegreeClass::Medium,
                    DegreeClass::High,
                    DegreeClass::Mixed,
                    DegreeClass::Unknown,
                ],
            },
        };

        for src_label in labels {
            for edge_type in edge_types {
                for degree_class in &degree_classes {
                    let key = L0SemanticIndexKey {
                        src_label,
                        edge_type,
                        degree_class: *degree_class,
                    };
                    let Some(group) = self.groups.get(&key) else {
                        continue;
                    };
                    for meta in group {
                        if meta.min_src > signature.src {
                            break;
                        }
                        if signature.src <= meta.max_src && meta.may_contain_signature(signature) {
                            out.push(*meta);
                        }
                    }
                }
            }
        }
        out.sort_by_key(|meta| meta.file_id);
        out.dedup_by_key(|meta| meta.file_id);
        out
    }
}

#[derive(Debug, Default)]
pub struct OracleL0Index {
    index: HashMap<(VertexId, Option<EdgeType>), Vec<FileId>>,
}

impl OracleL0Index {
    pub fn build_from_edges(edges: &[EdgeRecord], l0_files: &[CsrSegmentMeta]) -> Self {
        let mut index: HashMap<(VertexId, Option<EdgeType>), HashSet<FileId>> = HashMap::new();

        for edge in edges {
            for meta in l0_files {
                if meta.file_id == 0 {
                    continue;
                }
                let range_ok = edge.src >= meta.min_src && edge.src <= meta.max_src;
                let type_ok = meta.edge_type_partition == MIXED_EDGE_TYPE
                    || meta.edge_type_partition == edge.edge_type;
                let label_ok = meta.src_label == UNKNOWN_SOURCE_LABEL
                    || source_label_from_vertex_id(edge.src) == meta.src_label;
                if range_ok && type_ok && label_ok {
                    index
                        .entry((edge.src, Some(edge.edge_type)))
                        .or_default()
                        .insert(meta.file_id);
                }
            }
        }

        let index: HashMap<_, Vec<_>> = index
            .into_iter()
            .map(|(k, v)| {
                let mut ids: Vec<_> = v.into_iter().collect();
                ids.sort_unstable();
                (k, ids)
            })
            .collect();

        Self { index }
    }

    pub async fn build_from_reader(
        l0_files: &[CsrSegmentMeta],
        reader: &CsrReader<AnyIoBackend>,
    ) -> Result<Self> {
        let mut index: HashMap<(VertexId, Option<EdgeType>), HashSet<FileId>> = HashMap::new();

        for meta in l0_files {
            let edges = reader.read_all_edges(meta).await?;
            for edge in &edges {
                let key = (edge.src, Some(edge.edge_type));
                index.entry(key).or_default().insert(meta.file_id);
            }
        }

        let index: HashMap<_, Vec<_>> = index
            .into_iter()
            .map(|(k, v)| {
                let mut ids: Vec<_> = v.into_iter().collect();
                ids.sort_unstable();
                (k, ids)
            })
            .collect();

        Ok(Self { index })
    }

    pub fn get(&self, src: VertexId, edge_type: Option<EdgeType>) -> Vec<FileId> {
        self.index
            .get(&(src, edge_type))
            .cloned()
            .unwrap_or_default()
    }

    pub fn is_empty(&self) -> bool {
        self.index.is_empty()
    }

    pub fn len(&self) -> usize {
        self.index.len()
    }
}

type DegreeClassMask = u8;

const DEGREE_CLASS_LOW_MASK: DegreeClassMask = 1 << 0;
const DEGREE_CLASS_MEDIUM_MASK: DegreeClassMask = 1 << 1;
const DEGREE_CLASS_HIGH_MASK: DegreeClassMask = 1 << 2;

fn degree_class_mask(degree_class: DegreeClass) -> DegreeClassMask {
    match degree_class {
        DegreeClass::Low => DEGREE_CLASS_LOW_MASK,
        DegreeClass::Medium => DEGREE_CLASS_MEDIUM_MASK,
        DegreeClass::High => DEGREE_CLASS_HIGH_MASK,
        DegreeClass::Mixed | DegreeClass::Unknown => 0,
    }
}

fn insert_degree_class_mask(mask: &mut DegreeClassMask, degree_class: DegreeClass) {
    *mask |= degree_class_mask(degree_class);
}

fn degree_class_mask_count(mask: DegreeClassMask) -> u32 {
    mask.count_ones()
}

fn degree_classes_that_may_contain_query_mask(mask: DegreeClassMask) -> Vec<DegreeClass> {
    let mut out = Vec::new();
    for (bit, query_class) in [
        (DEGREE_CLASS_LOW_MASK, DegreeClass::Low),
        (DEGREE_CLASS_MEDIUM_MASK, DegreeClass::Medium),
        (DEGREE_CLASS_HIGH_MASK, DegreeClass::High),
    ] {
        if mask & bit == 0 {
            continue;
        }
        match query_class {
            DegreeClass::Low => out.push(DegreeClass::Low),
            DegreeClass::Medium => {
                out.push(DegreeClass::Low);
                out.push(DegreeClass::Medium);
            }
            DegreeClass::High => {
                out.push(DegreeClass::Low);
                out.push(DegreeClass::Medium);
                out.push(DegreeClass::High);
            }
            DegreeClass::Mixed | DegreeClass::Unknown => {
                out.push(DegreeClass::Low);
                out.push(DegreeClass::Medium);
                out.push(DegreeClass::High);
            }
        }
    }
    if mask == 0 {
        out.push(DegreeClass::Low);
        out.push(DegreeClass::Medium);
        out.push(DegreeClass::High);
    }
    out.push(DegreeClass::Mixed);
    out.push(DegreeClass::Unknown);
    out.sort_unstable();
    out.dedup();
    out
}

fn degree_directory_class_for_l0(meta: &CsrSegmentMeta) -> Option<DegreeClass> {
    if meta.level != L0
        || !meta.summary_completeness.allows_semantic_pruning()
        || meta.edge_type_partition == MIXED_EDGE_TYPE
        || !meta.degree_class_exact
        || matches!(meta.degree_class, DegreeClass::Mixed | DegreeClass::Unknown)
    {
        None
    } else {
        Some(meta.degree_class)
    }
}

const DEGREE_DIRECTORY_SIDECAR_FILE: &str = "DEGREE_DIRECTORY";
static DEGREE_DIRECTORY_SIDECAR_TMP_SEQ: AtomicU64 = AtomicU64::new(0);
const DEGREE_DIRECTORY_SIDECAR_MAGIC: &[u8; 8] = b"L0DGDIR1";
const DEGREE_DIRECTORY_VALID_MASK: DegreeClassMask =
    DEGREE_CLASS_LOW_MASK | DEGREE_CLASS_MEDIUM_MASK | DEGREE_CLASS_HIGH_MASK;

fn degree_directory_sidecar_path(store_dir: &Path) -> PathBuf {
    store_dir.join(DEGREE_DIRECTORY_SIDECAR_FILE)
}

fn degree_directory_sidecar_tmp_path(store_dir: &Path) -> PathBuf {
    let seq = DEGREE_DIRECTORY_SIDECAR_TMP_SEQ.fetch_add(1, Ordering::Relaxed);
    store_dir.join(format!(
        "{DEGREE_DIRECTORY_SIDECAR_FILE}.{}.{}.tmp",
        process::id(),
        seq
    ))
}

fn degree_directory_sidecar_file_ids(levels: &[Vec<CsrSegmentMeta>]) -> Vec<FileId> {
    let mut file_ids: Vec<_> = levels
        .get(L0 as usize)
        .into_iter()
        .flat_map(|metas| metas.iter())
        .filter(|meta| degree_directory_class_for_l0(meta).is_some())
        .map(|meta| meta.file_id)
        .collect();
    file_ids.sort_unstable();
    file_ids
}

/// Emit a single line breaking down `Engine::open` into its phases. Gated behind
/// `LSMGRAPH_LOG_OPEN_PHASES=1` so that `cargo test` (which opens many engines) stays quiet;
/// the SF30/SF100 bench runners set it to confirm whether compare timeout is dominated by
/// manifest/index/semantic-index initialization rather than by the queries themselves.
fn log_open_phase_timings(
    store_dir: &Path,
    manifest_load: Duration,
    schema_load: Duration,
    rebuild_index: Duration,
    semantic_index: Duration,
    semantic_index_skipped: bool,
    total_open: Duration,
) {
    let enabled = std::env::var("LSMGRAPH_LOG_OPEN_PHASES")
        .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
        .unwrap_or(false);
    if !enabled {
        return;
    }
    eprintln!(
        "[engine] open phases store={} manifest_load_s={:.3} schema_load_s={:.3} \
rebuild_index_s={:.3} semantic_index_s={:.3}{} total_open_s={:.3}",
        store_dir.display(),
        manifest_load.as_secs_f64(),
        schema_load.as_secs_f64(),
        rebuild_index.as_secs_f64(),
        semantic_index.as_secs_f64(),
        if semantic_index_skipped {
            " (skipped)"
        } else {
            ""
        },
        total_open.as_secs_f64(),
    );
}

fn read_degree_directory_sidecar(
    store_dir: &Path,
    expected_file_ids: &[FileId],
) -> Result<Option<HashMap<(VertexId, EdgeType), DegreeClassMask>>> {
    let path = degree_directory_sidecar_path(store_dir);
    if !path.exists() {
        return Ok(if expected_file_ids.is_empty() {
            Some(HashMap::new())
        } else {
            None
        });
    }
    match read_degree_directory_sidecar_inner(&path, expected_file_ids) {
        Ok(directory) => Ok(directory),
        Err(_) => Ok(None),
    }
}

fn read_degree_directory_sidecar_inner(
    path: &Path,
    expected_file_ids: &[FileId],
) -> Result<Option<HashMap<(VertexId, EdgeType), DegreeClassMask>>> {
    // The sidecar is read field-by-field (read_u64_le/read_i32_le/read_u8); at SF1 this is ~11M
    // entries x 3 reads. A raw File would issue one syscall per field (~33M), which dominated
    // Engine::open (measured ~75s on SF1, surfaced by the open-phase timing log). Wrap it in a
    // BufReader so the parse becomes a handful of sequential block reads. The write path already
    // uses BufWriter; this mirrors it.
    let mut reader = BufReader::new(File::open(path)?);
    let mut magic = [0u8; 8];
    reader.read_exact(&mut magic)?;
    if &magic != DEGREE_DIRECTORY_SIDECAR_MAGIC {
        return Ok(None);
    }

    let file_count = read_u64_le(&mut reader)?;
    let entry_count = read_u64_le(&mut reader)?;
    if usize::try_from(file_count).ok() != Some(expected_file_ids.len()) {
        return Ok(None);
    }
    for expected_file_id in expected_file_ids {
        if read_u64_le(&mut reader)? != *expected_file_id {
            return Ok(None);
        }
    }

    let capacity = usize::try_from(entry_count)
        .map_err(|_| anyhow::anyhow!("degree directory sidecar entry_count is too large"))?;
    let mut directory = HashMap::with_capacity(capacity);
    for _ in 0..entry_count {
        let src = read_u64_le(&mut reader)?;
        let edge_type = read_i32_le(&mut reader)?;
        let mask = read_u8(&mut reader)?;
        if mask == 0 || mask & !DEGREE_DIRECTORY_VALID_MASK != 0 {
            return Ok(None);
        }
        *directory.entry((src, edge_type)).or_default() |= mask;
    }
    Ok(Some(directory))
}

fn write_degree_directory_sidecar(
    store_dir: &Path,
    expected_file_ids: &[FileId],
    directory: &HashMap<(VertexId, EdgeType), DegreeClassMask>,
) -> Result<()> {
    fs::create_dir_all(store_dir)?;
    let tmp_path = degree_directory_sidecar_tmp_path(store_dir);
    let path = degree_directory_sidecar_path(store_dir);
    let entry_count = directory.values().filter(|mask| **mask != 0).count();

    let file = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(&tmp_path)?;
    let mut writer = BufWriter::new(file);
    writer.write_all(DEGREE_DIRECTORY_SIDECAR_MAGIC)?;
    write_u64_le(&mut writer, expected_file_ids.len() as u64)?;
    write_u64_le(&mut writer, entry_count as u64)?;
    for file_id in expected_file_ids {
        write_u64_le(&mut writer, *file_id)?;
    }
    for ((src, edge_type), mask) in directory.iter().filter(|(_, mask)| **mask != 0) {
        write_u64_le(&mut writer, *src)?;
        write_i32_le(&mut writer, *edge_type)?;
        write_u8(&mut writer, *mask)?;
    }
    let file = writer.into_inner()?;
    file.sync_all()?;
    fs::rename(tmp_path, path)?;
    Ok(())
}

fn read_u64_le(reader: &mut impl Read) -> Result<u64> {
    let mut buf = [0u8; 8];
    reader.read_exact(&mut buf)?;
    Ok(u64::from_le_bytes(buf))
}

fn read_i32_le(reader: &mut impl Read) -> Result<i32> {
    let mut buf = [0u8; 4];
    reader.read_exact(&mut buf)?;
    Ok(i32::from_le_bytes(buf))
}

fn read_u8(reader: &mut impl Read) -> Result<u8> {
    let mut buf = [0u8; 1];
    reader.read_exact(&mut buf)?;
    Ok(buf[0])
}

fn write_u64_le(writer: &mut impl Write, value: u64) -> Result<()> {
    writer.write_all(&value.to_le_bytes())?;
    Ok(())
}

fn write_i32_le(writer: &mut impl Write, value: i32) -> Result<()> {
    writer.write_all(&value.to_le_bytes())?;
    Ok(())
}

fn write_u8(writer: &mut impl Write, value: u8) -> Result<()> {
    writer.write_all(&[value])?;
    Ok(())
}

pub struct Engine {
    config: LsmGraphConfig,
    backend: Arc<AnyIoBackend>,
    metrics: Arc<Metrics>,
    manifest: Manifest,
    manifest_lock: Mutex<()>,
    version_manager: Arc<VersionManager>,
    state: Mutex<EngineState>,
    timestamp: AtomicU64,
    index: Arc<MultiLevelIndex>,
    vertex_locks: Arc<VertexLockTable>,
    metadata_cache: Arc<CsrMetadataCache>,
    degree_directory: RwLock<HashMap<(VertexId, EdgeType), DegreeClassMask>>,
    degree_directory_sidecar_persist_lock: Mutex<()>,
    k4_maintenance_lock: AsyncMutex<()>,
    semantic_l0_index: RwLock<SemanticL0Index>,
    oracle_l0_index: RwLock<OracleL0Index>,
    schema_catalog: RwLock<SchemaCatalog>,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct SchemaEvolutionReport {
    pub store_dir: PathBuf,
    pub snapshot: SnapshotId,
    pub current_schema_epoch: SchemaEpoch,
    pub catalog: SchemaCatalog,
    pub live_segment_count: usize,
    pub segment_count_by_level: Vec<SchemaEvolutionLevelCount>,
    pub segment_count_by_schema_epoch: BTreeMap<SchemaEpoch, usize>,
    pub segment_count_by_property_encoding_epoch: BTreeMap<SchemaEpoch, usize>,
    pub segment_count_by_semantic_summary: BTreeMap<String, usize>,
    pub segment_count_by_property_summary: BTreeMap<String, usize>,
    pub exact_semantic_segments: usize,
    pub mixed_edge_type_segments: usize,
    pub unknown_semantic_summary_segments: usize,
    pub unknown_property_summary_segments: usize,
    pub tombstone_segments: usize,
    pub edge_label_reports: Vec<SchemaEvolutionEdgeLabelReport>,
    pub property_reports: Vec<SchemaEvolutionPropertyReport>,
    pub alias_entries: Vec<SchemaEvolutionAliasReport>,
    pub dropped_entries: Vec<SchemaEvolutionDroppedReport>,
    pub property_encoding_migration_candidates: Vec<PropertyEncodingMigrationCandidateReport>,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct SchemaEvolutionLevelCount {
    pub level: usize,
    pub segments: usize,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct SchemaEvolutionEdgeLabelReport {
    pub edge_label_id: i32,
    pub name: String,
    pub exact_matching_segments: usize,
    pub exact_disjoint_segments: usize,
    pub conservative_segments: usize,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct SchemaEvolutionPropertyReport {
    pub property_id: u32,
    pub name: String,
    pub logical_type: String,
    pub physical_encoding: String,
    pub encoding_version: EncodingVersion,
    pub encoding_epoch: SchemaEpoch,
    pub default_or_null_rule: String,
    pub representable_in_presence_bitmap: bool,
    pub exact_absent_segments: usize,
    pub required_present_prunable_segments: usize,
    pub tombstone_blocked_absent_segments: usize,
    pub conservative_or_present_segments: usize,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct SchemaEvolutionAliasReport {
    pub entry_kind: String,
    pub id: String,
    pub name: String,
    pub canonical_id: String,
    pub valid_from_epoch: SchemaEpoch,
    pub valid_to_epoch: Option<SchemaEpoch>,
    pub dropped_at_epoch: Option<SchemaEpoch>,
    pub visible_at_current_epoch: bool,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct SchemaEvolutionDroppedReport {
    pub entry_kind: String,
    pub id: String,
    pub name: String,
    pub valid_from_epoch: SchemaEpoch,
    pub valid_to_epoch: Option<SchemaEpoch>,
    pub dropped_at_epoch: Option<SchemaEpoch>,
    pub visible_at_current_epoch: bool,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct PropertyEncodingMigrationCandidateReport {
    pub property_id: u32,
    pub name: String,
    pub current_encoding_epoch: SchemaEpoch,
    pub old_encoding_epoch: SchemaEpoch,
    pub segments: usize,
    pub segment_bytes: u64,
    pub safe_to_rewrite: bool,
    pub blocked_by_snapshot_gc: bool,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct OracleIndexStats {
    pub entry_count: usize,
    pub l0_segment_count: usize,
    pub elapsed_ms: u64,
}

impl OracleIndexStats {
    fn empty() -> Self {
        Self {
            entry_count: 0,
            l0_segment_count: 0,
            elapsed_ms: 0,
        }
    }
}

fn summary_completeness_key(value: crate::schema::SemanticSummaryCompleteness) -> String {
    format!("{value:?}")
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct L0CompactionDecision {
    pub key: L0PartitionKey,
    pub score: f64,
    pub estimated_rewrite_bytes: u64,
    pub selected_l0_segments: usize,
    pub query_count: u64,
    pub avg_candidate_segments: f64,
    pub offset_cache_miss_rate: f64,
    pub outputs: Vec<CsrSegmentMeta>,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct K4LifecycleReadReport {
    pub iteration: usize,
    pub query_index: usize,
    pub src: VertexId,
    pub edge_type: Option<EdgeType>,
    pub result_count: usize,
    pub candidate_l0_segments_delta: u64,
    pub matched_l0_segments_delta: u64,
    pub range_filtered_segments_delta: u64,
    pub bloom_filtered_segments_delta: u64,
}

#[derive(Debug, Clone, Copy, serde::Serialize)]
pub struct K4MergePolicy {
    pub fanout: usize,
    pub min_input_segments: usize,
    pub max_output_level: LevelId,
    pub semantic_partition_outputs: bool,
}

impl K4MergePolicy {
    pub fn from_config(config: &LsmGraphConfig) -> Self {
        Self {
            fanout: config.level_fanout.max(2),
            min_input_segments: 2,
            max_output_level: config.max_levels.saturating_sub(1).max(1) as LevelId,
            semantic_partition_outputs: true,
        }
    }
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct K4LevelCompactionDecision {
    pub source_level: LevelId,
    pub target_level: LevelId,
    pub input_segments: usize,
    pub output_segments: usize,
    pub input_bytes: u64,
    pub output_bytes: u64,
    pub semantic_partition_outputs: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
pub enum K4MaintenanceTrigger {
    Manual,
    Flush,
    ReadFeedback,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct K4MaintenanceReport {
    pub trigger: K4MaintenanceTrigger,
    pub skipped_reason: Option<String>,
    pub l0_segments_before: usize,
    pub l1_segments_before: usize,
    pub l0_segments_after: usize,
    pub l1_segments_after: usize,
    pub feedback_compaction: Option<L0CompactionDecision>,
    pub threshold_l0_compaction: Option<CsrSegmentMeta>,
    pub level_compactions: Vec<K4LevelCompactionDecision>,
    pub compaction_count_delta: u64,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct K4LifecycleReport {
    pub initial_snapshot: SnapshotId,
    pub final_snapshot: SnapshotId,
    pub schema_epoch_before: SchemaEpoch,
    pub schema_epoch_after: SchemaEpoch,
    pub flush_count_delta: u64,
    pub compaction_count_delta: u64,
    pub degree_directory_sidecar_persists_delta: u64,
    pub l0_segments_before: usize,
    pub l1_segments_before: usize,
    pub l0_segments_after_flush: usize,
    pub l1_segments_after_flush: usize,
    pub l0_segments_after_compaction: usize,
    pub l1_segments_after_compaction: usize,
    pub reads: Vec<K4LifecycleReadReport>,
    pub feedback_compaction: Option<L0CompactionDecision>,
    pub level_compactions: Vec<K4LevelCompactionDecision>,
}

#[derive(Debug, Clone)]
struct PickedL0Partition {
    key: L0PartitionKey,
    score: f64,
    estimated_rewrite_bytes: u64,
    selected_l0_segments: usize,
    query_count: u64,
    avg_candidate_segments: f64,
    offset_cache_miss_rate: f64,
}

#[derive(Debug, Clone, Copy)]
struct L0CompactionTarget {
    src_label: i32,
    edge_type: Option<EdgeType>,
    min_src: Option<VertexId>,
    max_src: Option<VertexId>,
}

#[derive(Debug, Clone)]
struct BudgetedEdgeCandidate {
    src_label: i32,
    edge_type: EdgeType,
    start: usize,
    end: usize,
    group_bytes: usize,
    group_edges: usize,
    group_sources: usize,
    estimated_exact_files: usize,
    configured_query_weight: f64,
    feedback_query_weight: f64,
    query_weight: f64,
    score: f64,
    selected: bool,
    reason: &'static str,
}

#[derive(Debug, Clone, Copy)]
struct BudgetedEdgeTypeDecision {
    estimated_exact_files: usize,
    configured_query_weight: f64,
    feedback_query_weight: f64,
    query_weight: f64,
    score: f64,
    selected: bool,
    reason: &'static str,
}

#[derive(Debug)]
struct L0FlushSegment {
    edges: Vec<EdgeRecord>,
    semantic_overrides: CsrSegmentSemanticOverrides,
}

impl L0FlushSegment {
    fn new(edges: Vec<EdgeRecord>, edge_type_partition_override: Option<EdgeType>) -> Self {
        Self {
            edges,
            semantic_overrides: CsrSegmentSemanticOverrides {
                edge_type_partition: edge_type_partition_override,
                ..CsrSegmentSemanticOverrides::default()
            },
        }
    }

    fn with_semantic_overrides(
        edges: Vec<EdgeRecord>,
        semantic_overrides: CsrSegmentSemanticOverrides,
    ) -> Self {
        Self {
            edges,
            semantic_overrides,
        }
    }

    fn fully_mixed(edges: Vec<EdgeRecord>) -> Self {
        Self::with_semantic_overrides(
            edges,
            CsrSegmentSemanticOverrides {
                src_label: Some(UNKNOWN_SOURCE_LABEL),
                dst_label: Some(UNKNOWN_SOURCE_LABEL),
                edge_type_partition: Some(MIXED_EDGE_TYPE),
                degree_class: Some(DegreeClass::Mixed),
                degree_class_exact: Some(false),
            },
        )
    }
}

#[derive(Debug)]
struct L0PropertyFlushSegment {
    records: Vec<EdgeRecordWithProperties>,
    semantic_overrides: CsrSegmentSemanticOverrides,
}

impl Engine {
    pub async fn create(config: LsmGraphConfig) -> Result<Arc<Self>> {
        prepare_store(&config.store_dir, true).await?;
        Self::open_inner(config, true).await
    }

    pub async fn open(config: LsmGraphConfig) -> Result<Arc<Self>> {
        prepare_store(&config.store_dir, false).await?;
        Self::open_inner(config, false).await
    }

    async fn open_inner(config: LsmGraphConfig, fresh: bool) -> Result<Arc<Self>> {
        let open_started = Instant::now();
        let metrics = Arc::new(Metrics::default());
        let backend = Arc::new(AnyIoBackend::new(
            config.io_backend,
            config.max_outstanding_io,
            metrics.clone(),
        )?);
        let manifest = Manifest::new(&config.store_dir);
        let manifest_load_started = Instant::now();
        let manifest_state = if fresh {
            crate::csr::manifest::ManifestState {
                live_files: Vec::new(),
                next_file_id: 1,
                max_ts: 0,
            }
        } else {
            manifest.load()?
        };
        let manifest_load_elapsed = manifest_load_started.elapsed();
        let schema_started = Instant::now();
        let schema_catalog =
            load_or_initialize_schema_catalog(&config.store_dir, fresh, config.schema_epoch)?;
        let schema_load_elapsed = schema_started.elapsed();

        let active = Arc::new(MemGraph::new(
            config.memgraph_capacity_bytes,
            config.inline_segment_capacity,
        ));
        let levels = split_levels(manifest_state.live_files, config.max_levels);
        let semantic_budget_used_extra_l0_files = initial_semantic_budget_used_extra_l0_files(
            &levels,
            config.semantic_budget_max_extra_l0_files,
        );
        let version = Version {
            id: 1,
            memgraphs: vec![active.clone()],
            levels,
        };
        let metadata_cache_entries = config.metadata_cache_entries;

        let engine = Arc::new(Self {
            config,
            backend,
            metrics,
            manifest,
            manifest_lock: Mutex::new(()),
            version_manager: Arc::new(VersionManager::new(version)),
            state: Mutex::new(EngineState {
                active,
                next_file_id: manifest_state.next_file_id,
                flush_tasks: Vec::new(),
                semantic_budget_used_extra_l0_files,
            }),
            timestamp: AtomicU64::new(manifest_state.max_ts),
            index: Arc::new(MultiLevelIndex::default()),
            vertex_locks: Arc::new(VertexLockTable::new(1 << 16)),
            metadata_cache: Arc::new(CsrMetadataCache::new(metadata_cache_entries)),
            degree_directory: RwLock::new(HashMap::new()),
            degree_directory_sidecar_persist_lock: Mutex::new(()),
            k4_maintenance_lock: AsyncMutex::new(()),
            semantic_l0_index: RwLock::new(SemanticL0Index::default()),
            oracle_l0_index: RwLock::new(OracleL0Index::default()),
            schema_catalog: RwLock::new(schema_catalog),
        });
        let rebuild_index_started = Instant::now();
        engine.rebuild_index().await?;
        let rebuild_index_elapsed = rebuild_index_started.elapsed();
        // rebuild_semantic_indexes is O(edges) (reads all L0 offsets -> per-(src,edge_type)
        // degree map, ~260GB at SF100). The plan-gen pass only needs scan_edges to write the
        // sample plan and never consults these indexes, so SNB_SKIP_SEM_INDEX=1 skips them to
        // avoid OOM when scan_edges + the index build would otherwise coexist. Real measurement
        // reads (--sample-plan-in, no scan) leave it unset so pruning stays correct.
        let semantic_index_started = Instant::now();
        let semantic_index_skipped = std::env::var("SNB_SKIP_SEM_INDEX")
            .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
            .unwrap_or(false);
        if semantic_index_skipped {
            eprintln!("[engine] SKIP rebuild_semantic_indexes (SNB_SKIP_SEM_INDEX set)");
        } else {
            engine.load_or_rebuild_semantic_indexes().await?;
        }
        let semantic_index_elapsed = semantic_index_started.elapsed();
        log_open_phase_timings(
            &engine.config.store_dir,
            manifest_load_elapsed,
            schema_load_elapsed,
            rebuild_index_elapsed,
            semantic_index_elapsed,
            semantic_index_skipped,
            open_started.elapsed(),
        );
        Ok(engine)
    }

    pub fn current_snapshot(&self) -> SnapshotId {
        self.timestamp.load(Ordering::SeqCst)
    }

    pub fn snapshot_gc_safe_point(&self) -> SnapshotId {
        0
    }

    pub fn metrics(&self) -> Arc<Metrics> {
        self.metrics.clone()
    }

    pub fn persist_semantic_sidecars(&self) -> Result<()> {
        self.persist_degree_directory_sidecar_for_current_version()
    }

    pub async fn run_k4_maintenance(&self) -> Result<K4MaintenanceReport> {
        Ok(self
            .run_k4_maintenance_inner(K4MaintenanceTrigger::Manual, true)
            .await?
            .expect("manual K4 maintenance always returns a report"))
    }

    async fn maybe_run_k4_maintenance(
        &self,
        trigger: K4MaintenanceTrigger,
    ) -> Result<Option<K4MaintenanceReport>> {
        if !self.config.auto_compaction {
            return Ok(None);
        }
        self.run_k4_maintenance_inner(trigger, false).await
    }

    async fn run_k4_maintenance_inner(
        &self,
        trigger: K4MaintenanceTrigger,
        force: bool,
    ) -> Result<Option<K4MaintenanceReport>> {
        let _maintenance = self.k4_maintenance_lock.lock().await;
        if !force && !self.k4_maintenance_should_run(trigger) {
            return Ok(None);
        }

        let before_levels = self.live_file_count_by_level();
        let before_compactions = self.metrics.compaction_count.load(Ordering::Relaxed);
        let feedback_compaction = self.compact_best_l0_partition_by_score().await?;
        let threshold_l0_compaction =
            if feedback_compaction.is_none() && self.l0_count_exceeds_auto_threshold() {
                self.compact_l0_to_l1_inner(false).await?
            } else {
                None
            };
        let level_compactions = self.compact_k4_levels().await?;
        if feedback_compaction.is_some()
            || threshold_l0_compaction.is_some()
            || !level_compactions.is_empty()
        {
            self.persist_semantic_sidecars()?;
        }
        let after_levels = self.live_file_count_by_level();
        let after_compactions = self.metrics.compaction_count.load(Ordering::Relaxed);
        let skipped_reason = if feedback_compaction.is_none()
            && threshold_l0_compaction.is_none()
            && level_compactions.is_empty()
        {
            Some("no eligible K4 maintenance work".to_string())
        } else {
            None
        };
        Ok(Some(K4MaintenanceReport {
            trigger,
            skipped_reason,
            l0_segments_before: level_count(&before_levels, L0),
            l1_segments_before: level_count(&before_levels, L1),
            l0_segments_after: level_count(&after_levels, L0),
            l1_segments_after: level_count(&after_levels, L1),
            feedback_compaction,
            threshold_l0_compaction,
            level_compactions,
            compaction_count_delta: after_compactions.saturating_sub(before_compactions),
        }))
    }

    fn spawn_k4_maintenance_after_flush(self: &Arc<Self>) {
        if !self.config.auto_compaction {
            return;
        }
        let engine = self.clone();
        tokio::spawn(async move {
            if let Err(err) = engine
                .maybe_run_k4_maintenance(K4MaintenanceTrigger::Flush)
                .await
            {
                eprintln!("[engine] K4 maintenance after flush failed: {err:#}");
            }
        });
    }

    fn k4_maintenance_should_run(&self, trigger: K4MaintenanceTrigger) -> bool {
        if self.l1plus_count_exceeds_k4_fanout() {
            return true;
        }
        match trigger {
            K4MaintenanceTrigger::Manual => true,
            K4MaintenanceTrigger::Flush => self.l0_count_exceeds_auto_threshold(),
            K4MaintenanceTrigger::ReadFeedback => self.pick_l0_partition_by_score().is_some(),
        }
    }

    fn l0_count_exceeds_auto_threshold(&self) -> bool {
        let threshold = self.config.l0_file_threshold.max(1);
        self.version_manager
            .pin_current()
            .version()
            .levels
            .get(L0 as usize)
            .map(|level| level.len() >= threshold)
            .unwrap_or(false)
    }

    fn l1plus_count_exceeds_k4_fanout(&self) -> bool {
        let policy = K4MergePolicy::from_config(&self.config);
        let max_output_level = policy
            .max_output_level
            .min(self.config.max_levels.saturating_sub(1).max(1) as LevelId);
        if max_output_level <= L1 {
            return false;
        }
        let guard = self.version_manager.pin_current();
        for source_level in L1..max_output_level {
            let source_count = guard
                .version()
                .levels
                .get(source_level as usize)
                .map(Vec::len)
                .unwrap_or(0);
            if source_count >= policy.min_input_segments && source_count > policy.fanout {
                return true;
            }
        }
        false
    }

    pub async fn run_k4_lifecycle(
        self: &Arc<Self>,
        signatures: &[GraphAccessSignature],
    ) -> Result<K4LifecycleReport> {
        self.run_k4_lifecycle_with_repetitions(signatures, 1).await
    }

    pub async fn run_k4_lifecycle_with_repetitions(
        self: &Arc<Self>,
        signatures: &[GraphAccessSignature],
        repetitions: usize,
    ) -> Result<K4LifecycleReport> {
        let repetitions = repetitions.max(1);
        let initial_snapshot = self.current_snapshot();
        let schema_epoch_before = self.current_schema_epoch();
        let before_levels = self.live_file_count_by_level();
        let before_flush_count = self.metrics.flush_count.load(Ordering::Relaxed);
        let before_compaction_count = self.metrics.compaction_count.load(Ordering::Relaxed);
        let before_sidecar_persists = self
            .metrics
            .degree_directory_sidecar_persists
            .load(Ordering::Relaxed);

        self.flush_active().await?;
        self.persist_semantic_sidecars()?;
        let after_flush_levels = self.live_file_count_by_level();
        let read_snapshot = self.current_snapshot();
        let mut reads = Vec::new();

        for iteration in 0..repetitions {
            for (query_index, signature) in signatures.iter().copied().enumerate() {
                let before_candidate_l0 =
                    self.metrics.candidate_l0_segments.load(Ordering::Relaxed);
                let before_matched_l0 = self.metrics.matched_l0_segments.load(Ordering::Relaxed);
                let before_range_filtered =
                    self.metrics.range_filtered_segments.load(Ordering::Relaxed);
                let before_bloom_filtered =
                    self.metrics.bloom_filtered_segments.load(Ordering::Relaxed);
                let result = self
                    .get_neighbors_by_signature(signature, read_snapshot)
                    .await?;
                reads.push(K4LifecycleReadReport {
                    iteration,
                    query_index,
                    src: signature.src,
                    edge_type: signature.edge_type,
                    result_count: result.len(),
                    candidate_l0_segments_delta: self
                        .metrics
                        .candidate_l0_segments
                        .load(Ordering::Relaxed)
                        .saturating_sub(before_candidate_l0),
                    matched_l0_segments_delta: self
                        .metrics
                        .matched_l0_segments
                        .load(Ordering::Relaxed)
                        .saturating_sub(before_matched_l0),
                    range_filtered_segments_delta: self
                        .metrics
                        .range_filtered_segments
                        .load(Ordering::Relaxed)
                        .saturating_sub(before_range_filtered),
                    bloom_filtered_segments_delta: self
                        .metrics
                        .bloom_filtered_segments
                        .load(Ordering::Relaxed)
                        .saturating_sub(before_bloom_filtered),
                });
            }
        }

        let feedback_compaction = self.compact_best_l0_partition_by_score().await?;
        let level_compactions = self.compact_k4_levels().await?;
        self.persist_semantic_sidecars()?;
        let after_compaction_levels = self.live_file_count_by_level();

        Ok(K4LifecycleReport {
            initial_snapshot,
            final_snapshot: self.current_snapshot(),
            schema_epoch_before,
            schema_epoch_after: self.current_schema_epoch(),
            flush_count_delta: self
                .metrics
                .flush_count
                .load(Ordering::Relaxed)
                .saturating_sub(before_flush_count),
            compaction_count_delta: self
                .metrics
                .compaction_count
                .load(Ordering::Relaxed)
                .saturating_sub(before_compaction_count),
            degree_directory_sidecar_persists_delta: self
                .metrics
                .degree_directory_sidecar_persists
                .load(Ordering::Relaxed)
                .saturating_sub(before_sidecar_persists),
            l0_segments_before: level_count(&before_levels, L0),
            l1_segments_before: level_count(&before_levels, L1),
            l0_segments_after_flush: level_count(&after_flush_levels, L0),
            l1_segments_after_flush: level_count(&after_flush_levels, L1),
            l0_segments_after_compaction: level_count(&after_compaction_levels, L0),
            l1_segments_after_compaction: level_count(&after_compaction_levels, L1),
            reads,
            feedback_compaction,
            level_compactions,
        })
    }

    pub fn schema_catalog_snapshot(&self) -> SchemaCatalog {
        self.schema_catalog.read().clone()
    }

    pub fn current_schema_epoch(&self) -> SchemaEpoch {
        self.schema_catalog.read().current_epoch
    }

    pub fn add_schema_vertex_label(&self, id: i32, name: impl Into<String>) -> Result<SchemaEpoch> {
        let mut catalog = self.schema_catalog.write();
        let epoch = catalog.add_vertex_label(id, name);
        catalog.save(&SchemaCatalog::path_for_store(&self.config.store_dir))?;
        Ok(epoch)
    }

    pub fn add_schema_edge_label(
        &self,
        id: i32,
        name: impl Into<String>,
        src_label_id: i32,
        dst_label_id: i32,
    ) -> Result<SchemaEpoch> {
        let mut catalog = self.schema_catalog.write();
        let epoch = catalog.add_edge_label(id, name, src_label_id, dst_label_id);
        catalog.save(&SchemaCatalog::path_for_store(&self.config.store_dir))?;
        Ok(epoch)
    }

    pub fn add_schema_property(&self, entry: NewPropertyEntry) -> Result<SchemaEpoch> {
        let mut catalog = self.schema_catalog.write();
        let epoch = catalog.add_property(entry);
        catalog.save(&SchemaCatalog::path_for_store(&self.config.store_dir))?;
        Ok(epoch)
    }

    pub fn alias_schema_vertex_label(
        &self,
        alias_id: i32,
        alias_name: impl Into<String>,
        canonical_id: i32,
    ) -> Result<SchemaEpoch> {
        let mut catalog = self.schema_catalog.write();
        let epoch = catalog.alias_vertex_label(alias_id, alias_name, canonical_id)?;
        catalog.save(&SchemaCatalog::path_for_store(&self.config.store_dir))?;
        Ok(epoch)
    }

    pub fn alias_schema_edge_label(
        &self,
        alias_id: i32,
        alias_name: impl Into<String>,
        canonical_id: i32,
    ) -> Result<SchemaEpoch> {
        let mut catalog = self.schema_catalog.write();
        let epoch = catalog.alias_edge_label(alias_id, alias_name, canonical_id)?;
        catalog.save(&SchemaCatalog::path_for_store(&self.config.store_dir))?;
        Ok(epoch)
    }

    pub fn alias_schema_property(
        &self,
        alias_id: u32,
        alias_name: impl Into<String>,
        canonical_id: u32,
    ) -> Result<SchemaEpoch> {
        let mut catalog = self.schema_catalog.write();
        let epoch = catalog.alias_property(alias_id, alias_name, canonical_id)?;
        catalog.save(&SchemaCatalog::path_for_store(&self.config.store_dir))?;
        Ok(epoch)
    }

    pub fn drop_schema_vertex_label(&self, id: i32) -> Result<SchemaEpoch> {
        let mut catalog = self.schema_catalog.write();
        let epoch = catalog.drop_vertex_label(id)?;
        catalog.save(&SchemaCatalog::path_for_store(&self.config.store_dir))?;
        Ok(epoch)
    }

    pub fn drop_schema_edge_label(&self, id: i32) -> Result<SchemaEpoch> {
        let mut catalog = self.schema_catalog.write();
        let epoch = catalog.drop_edge_label(id)?;
        catalog.save(&SchemaCatalog::path_for_store(&self.config.store_dir))?;
        Ok(epoch)
    }

    pub fn drop_schema_property(&self, id: u32) -> Result<SchemaEpoch> {
        let mut catalog = self.schema_catalog.write();
        let epoch = catalog.drop_property(id)?;
        catalog.save(&SchemaCatalog::path_for_store(&self.config.store_dir))?;
        Ok(epoch)
    }

    pub fn change_schema_property_encoding(
        &self,
        id: u32,
        logical_type: impl Into<String>,
        physical_encoding: impl Into<String>,
        encoding_version: EncodingVersion,
        default_or_null_rule: impl Into<String>,
    ) -> Result<SchemaEpoch> {
        let mut catalog = self.schema_catalog.write();
        let epoch = catalog.change_property_encoding(
            id,
            logical_type,
            physical_encoding,
            encoding_version,
            default_or_null_rule,
        )?;
        catalog.save(&SchemaCatalog::path_for_store(&self.config.store_dir))?;
        Ok(epoch)
    }

    pub fn schema_evolution_report(&self) -> SchemaEvolutionReport {
        let catalog = self.schema_catalog_snapshot();
        let guard = self.version_guard();
        let levels = &guard.version().levels;
        let segments: Vec<CsrSegmentMeta> = levels.iter().flatten().copied().collect();

        let mut segment_count_by_schema_epoch = BTreeMap::new();
        let mut segment_count_by_property_encoding_epoch = BTreeMap::new();
        let mut segment_count_by_semantic_summary = BTreeMap::new();
        let mut segment_count_by_property_summary = BTreeMap::new();
        let mut exact_semantic_segments = 0usize;
        let mut mixed_edge_type_segments = 0usize;
        let mut unknown_semantic_summary_segments = 0usize;
        let mut unknown_property_summary_segments = 0usize;
        let mut tombstone_segments = 0usize;

        for meta in &segments {
            *segment_count_by_schema_epoch
                .entry(meta.schema_epoch)
                .or_default() += 1;
            *segment_count_by_property_encoding_epoch
                .entry(meta.property_encoding_epoch)
                .or_default() += 1;
            *segment_count_by_semantic_summary
                .entry(summary_completeness_key(meta.summary_completeness))
                .or_default() += 1;
            *segment_count_by_property_summary
                .entry(summary_completeness_key(meta.property_summary_completeness))
                .or_default() += 1;
            if matches!(
                meta.summary_completeness,
                crate::schema::SemanticSummaryCompleteness::Exact
            ) {
                exact_semantic_segments += 1;
            }
            if meta.edge_type_partition == MIXED_EDGE_TYPE {
                mixed_edge_type_segments += 1;
            }
            if !meta.summary_completeness.allows_semantic_pruning() {
                unknown_semantic_summary_segments += 1;
            }
            if !meta.property_summary_completeness.allows_semantic_pruning() {
                unknown_property_summary_segments += 1;
            }
            if meta.may_contain_tombstones {
                tombstone_segments += 1;
            }
        }

        let edge_label_reports = catalog
            .edge_labels
            .values()
            .map(|entry| {
                let mut exact_matching_segments = 0usize;
                let mut exact_disjoint_segments = 0usize;
                let mut conservative_segments = 0usize;
                for meta in &segments {
                    if !meta.summary_completeness.allows_semantic_pruning()
                        || meta.edge_type_partition == MIXED_EDGE_TYPE
                    {
                        conservative_segments += 1;
                    } else if meta.edge_type_partition == entry.id {
                        exact_matching_segments += 1;
                    } else {
                        exact_disjoint_segments += 1;
                    }
                }
                SchemaEvolutionEdgeLabelReport {
                    edge_label_id: entry.id,
                    name: entry.name.clone(),
                    exact_matching_segments,
                    exact_disjoint_segments,
                    conservative_segments,
                }
            })
            .collect();

        let property_reports = catalog
            .properties
            .values()
            .map(|entry| {
                let representable_in_presence_bitmap = property_presence_bit(entry.id).is_some();
                let mut exact_absent_segments = 0usize;
                let mut required_present_prunable_segments = 0usize;
                let mut tombstone_blocked_absent_segments = 0usize;
                let mut conservative_or_present_segments = 0usize;
                for meta in &segments {
                    if !representable_in_presence_bitmap {
                        conservative_or_present_segments += 1;
                        continue;
                    }
                    if meta.definitely_lacks_property(entry.id) {
                        exact_absent_segments += 1;
                        if meta.may_contain_tombstones {
                            tombstone_blocked_absent_segments += 1;
                        } else {
                            required_present_prunable_segments += 1;
                        }
                    } else {
                        conservative_or_present_segments += 1;
                    }
                }
                SchemaEvolutionPropertyReport {
                    property_id: entry.id,
                    name: entry.name.clone(),
                    logical_type: entry.logical_type.clone(),
                    physical_encoding: entry.physical_encoding.clone(),
                    encoding_version: entry.encoding_version,
                    encoding_epoch: entry.encoding_epoch,
                    default_or_null_rule: entry.default_or_null_rule.clone(),
                    representable_in_presence_bitmap,
                    exact_absent_segments,
                    required_present_prunable_segments,
                    tombstone_blocked_absent_segments,
                    conservative_or_present_segments,
                }
            })
            .collect();

        let mut alias_entries = Vec::new();
        let mut dropped_entries = Vec::new();

        for entry in catalog.vertex_labels.values() {
            if let Some(canonical_id) = entry.alias_of {
                alias_entries.push(SchemaEvolutionAliasReport {
                    entry_kind: "vertex_label".to_string(),
                    id: entry.id.to_string(),
                    name: entry.name.clone(),
                    canonical_id: canonical_id.to_string(),
                    valid_from_epoch: entry.valid_from_epoch,
                    valid_to_epoch: entry.valid_to_epoch,
                    dropped_at_epoch: entry.dropped_at_epoch,
                    visible_at_current_epoch: entry.is_visible_at(catalog.current_epoch),
                });
            }
            if entry.dropped_at_epoch.is_some() {
                dropped_entries.push(SchemaEvolutionDroppedReport {
                    entry_kind: "vertex_label".to_string(),
                    id: entry.id.to_string(),
                    name: entry.name.clone(),
                    valid_from_epoch: entry.valid_from_epoch,
                    valid_to_epoch: entry.valid_to_epoch,
                    dropped_at_epoch: entry.dropped_at_epoch,
                    visible_at_current_epoch: entry.is_visible_at(catalog.current_epoch),
                });
            }
        }

        for entry in catalog.edge_labels.values() {
            if let Some(canonical_id) = entry.alias_of {
                alias_entries.push(SchemaEvolutionAliasReport {
                    entry_kind: "edge_label".to_string(),
                    id: entry.id.to_string(),
                    name: entry.name.clone(),
                    canonical_id: canonical_id.to_string(),
                    valid_from_epoch: entry.valid_from_epoch,
                    valid_to_epoch: entry.valid_to_epoch,
                    dropped_at_epoch: entry.dropped_at_epoch,
                    visible_at_current_epoch: entry.is_visible_at(catalog.current_epoch),
                });
            }
            if entry.dropped_at_epoch.is_some() {
                dropped_entries.push(SchemaEvolutionDroppedReport {
                    entry_kind: "edge_label".to_string(),
                    id: entry.id.to_string(),
                    name: entry.name.clone(),
                    valid_from_epoch: entry.valid_from_epoch,
                    valid_to_epoch: entry.valid_to_epoch,
                    dropped_at_epoch: entry.dropped_at_epoch,
                    visible_at_current_epoch: entry.is_visible_at(catalog.current_epoch),
                });
            }
        }

        for entry in catalog.properties.values() {
            if let Some(canonical_id) = entry.alias_of {
                alias_entries.push(SchemaEvolutionAliasReport {
                    entry_kind: "property".to_string(),
                    id: entry.id.to_string(),
                    name: entry.name.clone(),
                    canonical_id: canonical_id.to_string(),
                    valid_from_epoch: entry.valid_from_epoch,
                    valid_to_epoch: entry.valid_to_epoch,
                    dropped_at_epoch: entry.dropped_at_epoch,
                    visible_at_current_epoch: entry.is_visible_at(catalog.current_epoch),
                });
            }
            if entry.dropped_at_epoch.is_some() {
                dropped_entries.push(SchemaEvolutionDroppedReport {
                    entry_kind: "property".to_string(),
                    id: entry.id.to_string(),
                    name: entry.name.clone(),
                    valid_from_epoch: entry.valid_from_epoch,
                    valid_to_epoch: entry.valid_to_epoch,
                    dropped_at_epoch: entry.dropped_at_epoch,
                    visible_at_current_epoch: entry.is_visible_at(catalog.current_epoch),
                });
            }
        }

        let snapshot_gc_safe_point = self.snapshot_gc_safe_point();
        let property_encoding_migration_candidates = catalog
            .properties
            .values()
            .filter(|entry| entry.alias_of.is_none() && entry.is_visible_at(catalog.current_epoch))
            .flat_map(|entry| {
                let mut old_epochs: BTreeMap<SchemaEpoch, (usize, u64)> = BTreeMap::new();
                for meta in &segments {
                    if meta.property_encoding_epoch < entry.encoding_epoch {
                        let candidate = old_epochs
                            .entry(meta.property_encoding_epoch)
                            .or_insert((0, 0));
                        candidate.0 += 1;
                        candidate.1 = candidate.1.saturating_add(meta.segment_bytes);
                    }
                }
                old_epochs.into_iter().map(
                    move |(old_encoding_epoch, (segments, segment_bytes))| {
                        let safe_to_rewrite = snapshot_gc_safe_point > 0;
                        PropertyEncodingMigrationCandidateReport {
                            property_id: entry.id,
                            name: entry.name.clone(),
                            current_encoding_epoch: entry.encoding_epoch,
                            old_encoding_epoch,
                            segments,
                            segment_bytes,
                            safe_to_rewrite,
                            blocked_by_snapshot_gc: !safe_to_rewrite,
                        }
                    },
                )
            })
            .collect();

        SchemaEvolutionReport {
            store_dir: self.config.store_dir.clone(),
            snapshot: self.current_snapshot(),
            current_schema_epoch: catalog.current_epoch,
            catalog,
            live_segment_count: segments.len(),
            segment_count_by_level: levels
                .iter()
                .enumerate()
                .map(|(level, metas)| SchemaEvolutionLevelCount {
                    level,
                    segments: metas.len(),
                })
                .collect(),
            segment_count_by_schema_epoch,
            segment_count_by_property_encoding_epoch,
            segment_count_by_semantic_summary,
            segment_count_by_property_summary,
            exact_semantic_segments,
            mixed_edge_type_segments,
            unknown_semantic_summary_segments,
            unknown_property_summary_segments,
            tombstone_segments,
            edge_label_reports,
            property_reports,
            alias_entries,
            dropped_entries,
            property_encoding_migration_candidates,
        }
    }

    fn append_manifest(&self, record: &ManifestRecord) -> Result<()> {
        let _guard = self.manifest_lock.lock();
        self.manifest.append(record)
    }

    pub async fn insert_edge(
        self: &Arc<Self>,
        src: VertexId,
        dst: VertexId,
        edge_type: EdgeType,
    ) -> Result<SnapshotId> {
        let started = Instant::now();
        let ts = self.timestamp.fetch_add(1, Ordering::SeqCst) + 1;
        self.metrics.insert_ops.fetch_add(1, Ordering::Relaxed);
        let result = self
            .write_edge(EdgeRecord::insert(src, dst, edge_type, ts))
            .await;
        self.metrics.storage_insert_latency.record_since(started);
        result?;
        Ok(ts)
    }

    pub async fn insert_edge_with_properties_prototype(
        self: &Arc<Self>,
        src: VertexId,
        dst: VertexId,
        edge_type: EdgeType,
        properties: Vec<EdgePropertyValue>,
    ) -> Result<SnapshotId> {
        let started = Instant::now();
        let ts = self.timestamp.fetch_add(1, Ordering::SeqCst) + 1;
        self.metrics.insert_ops.fetch_add(1, Ordering::Relaxed);
        let result = self
            .write_edge_with_properties(EdgeRecordWithProperties {
                edge: EdgeRecord::insert(src, dst, edge_type, ts),
                properties,
            })
            .await;
        self.metrics.storage_insert_latency.record_since(started);
        result?;
        Ok(ts)
    }

    pub async fn insert_edge_with_property_values_prototype(
        self: &Arc<Self>,
        src: VertexId,
        dst: VertexId,
        edge_type: EdgeType,
        properties: Vec<(PropertyId, PropertyValue)>,
    ) -> Result<SnapshotId> {
        let encoded = self.encode_edge_property_values(edge_type, properties)?;
        self.insert_edge_with_properties_prototype(src, dst, edge_type, encoded)
            .await
    }

    pub async fn delete_edge(
        self: &Arc<Self>,
        src: VertexId,
        dst: VertexId,
        edge_type: EdgeType,
    ) -> Result<SnapshotId> {
        let started = Instant::now();
        let ts = self.timestamp.fetch_add(1, Ordering::SeqCst) + 1;
        self.metrics.delete_ops.fetch_add(1, Ordering::Relaxed);
        let result = self
            .write_edge(EdgeRecord::delete(src, dst, edge_type, ts))
            .await;
        self.metrics.storage_delete_latency.record_since(started);
        result?;
        Ok(ts)
    }

    async fn write_edge(self: &Arc<Self>, edge: EdgeRecord) -> Result<()> {
        self.write_edge_with_properties(EdgeRecordWithProperties::topology_only(edge))
            .await
    }

    async fn write_edge_with_properties(
        self: &Arc<Self>,
        record: EdgeRecordWithProperties,
    ) -> Result<()> {
        let edge = record.edge;
        let _lock = self.vertex_locks.write_lock(edge.src);
        let mut frozen = None;
        {
            let mut state = self.state.lock();
            state.active.insert_with_properties(record);
            if state.active.is_full() {
                let old_active = state.active.clone();
                let new_active = Arc::new(MemGraph::new(
                    self.config.memgraph_capacity_bytes,
                    self.config.inline_segment_capacity,
                ));
                state.active = new_active.clone();
                frozen = Some(old_active);

                let old = self.version_manager.pin_current();
                let mut memgraphs = Vec::with_capacity(old.version().memgraphs.len() + 1);
                memgraphs.push(new_active);
                memgraphs.extend(old.version().memgraphs.iter().cloned());
                self.version_manager.publish(Version {
                    id: old.version().id + 1,
                    memgraphs,
                    levels: old.version().levels.clone(),
                });
            }
        }
        if let Some(memgraph) = frozen {
            self.enqueue_flush(memgraph).await?;
        }
        Ok(())
    }

    pub async fn flush_active(self: &Arc<Self>) -> Result<()> {
        let frozen = {
            let mut state = self.state.lock();
            if state.active.is_empty() {
                None
            } else {
                let old_active = state.active.clone();
                let new_active = Arc::new(MemGraph::new(
                    self.config.memgraph_capacity_bytes,
                    self.config.inline_segment_capacity,
                ));
                state.active = new_active.clone();
                let old = self.version_manager.pin_current();
                let mut memgraphs = Vec::with_capacity(old.version().memgraphs.len() + 1);
                memgraphs.push(new_active);
                memgraphs.extend(old.version().memgraphs.iter().cloned());
                self.version_manager.publish(Version {
                    id: old.version().id + 1,
                    memgraphs,
                    levels: old.version().levels.clone(),
                });
                Some(old_active)
            }
        };
        if let Some(memgraph) = frozen {
            self.enqueue_flush(memgraph).await?;
        }
        self.wait_for_flushes().await?;
        if self.config.auto_compaction {
            self.maybe_run_k4_maintenance(K4MaintenanceTrigger::Flush)
                .await?;
        }
        Ok(())
    }

    async fn enqueue_flush(self: &Arc<Self>, memgraph: Arc<MemGraph>) -> Result<()> {
        let engine = self.clone();
        let handle = tokio::spawn(async move {
            let result = engine.clone().flush_memgraph(memgraph).await;
            if result.is_ok() {
                engine.spawn_k4_maintenance_after_flush();
            }
            result
        });
        self.state.lock().flush_tasks.push(handle);
        Ok(())
    }

    pub async fn wait_for_flushes(&self) -> Result<()> {
        loop {
            let task = self.state.lock().flush_tasks.pop();
            let Some(task) = task else {
                break;
            };
            task.await??;
        }
        Ok(())
    }

    async fn flush_memgraph(self: Arc<Self>, memgraph: Arc<MemGraph>) -> Result<()> {
        let started = Instant::now();
        let records = memgraph.all_edges_with_properties_sorted();
        if records.is_empty() {
            self.metrics.storage_flush_latency.record_since(started);
            return Ok(());
        }
        let writer = CsrWriter::new(
            self.backend.clone(),
            self.config.store_dir.clone(),
            self.current_schema_epoch(),
        );
        let mut metas = Vec::new();
        for segment in self.build_l0_property_flush_segments(records)? {
            let file_id = self.alloc_file_id();
            let meta = writer
                .write_segment_with_properties_and_semantic_overrides(
                    L0,
                    file_id,
                    segment.records,
                    segment.semantic_overrides,
                )
                .await?;
            self.append_manifest(&ManifestRecord::CreateFile { meta })?;
            metas.push(meta);
        }
        self.metrics.flush_count.fetch_add(1, Ordering::Relaxed);

        let old = self.version_manager.pin_current();
        let mut levels = old.version().levels.clone();
        ensure_level(&mut levels, L0);
        levels[L0 as usize].extend(metas.iter().copied());
        levels[L0 as usize]
            .sort_by_key(|m| (m.src_label, m.edge_type_partition, m.min_src, m.file_id));
        let memgraphs = old
            .version()
            .memgraphs
            .iter()
            .filter(|m| m.id() != memgraph.id())
            .cloned()
            .collect();
        self.version_manager.publish(Version {
            id: old.version().id + 1,
            memgraphs,
            levels,
        });
        self.rebuild_index().await?;
        self.update_semantic_indexes_with_l0_metas(&metas).await?;
        self.metrics.storage_flush_latency.record_since(started);
        Ok(())
    }

    fn alloc_file_id(&self) -> FileId {
        let mut state = self.state.lock();
        let file_id = state.next_file_id;
        state.next_file_id += 1;
        file_id
    }

    fn build_l0_flush_segments(&self, edges: Vec<EdgeRecord>) -> Result<Vec<L0FlushSegment>> {
        let segments = match self.config.l0_layout {
            L0LayoutPolicy::Naive => vec![L0FlushSegment::new(edges, None)],
            L0LayoutPolicy::Schema => self.build_schema_l0_flush_segments(edges),
            L0LayoutPolicy::LabelOnly => self.build_label_only_l0_flush_segments(edges),
            L0LayoutPolicy::EdgeTypeOnly => self.build_edge_type_only_l0_flush_segments(edges),
            L0LayoutPolicy::DegreeOnly => self.build_degree_only_l0_flush_segments(edges),
            L0LayoutPolicy::Semantic => self.build_semantic_l0_flush_segments(edges),
            L0LayoutPolicy::SemanticBudgeted => {
                self.build_budgeted_semantic_l0_flush_segments(edges)?
            }
            L0LayoutPolicy::LsmGraphStyle => vec![L0FlushSegment::new(edges, None)],
            L0LayoutPolicy::FullCompact | L0LayoutPolicy::OracleSemantic => {
                vec![L0FlushSegment::new(edges, None)]
            }
            L0LayoutPolicy::RocksDbStyle => self.build_rocksdb_style_l0_flush_segments(edges),
        };

        let segments = if segments.is_empty() {
            Vec::new()
        } else if segments.len() <= self.config.max_l0_segments_per_flush {
            segments
        } else {
            merge_l0_flush_segments_to_cap(segments, self.config.max_l0_segments_per_flush)
        };
        Ok(segments)
    }

    fn build_l0_property_flush_segments(
        &self,
        records: Vec<EdgeRecordWithProperties>,
    ) -> Result<Vec<L0PropertyFlushSegment>> {
        let mut by_key = BTreeMap::new();
        for record in records {
            by_key.insert(edge_key(&record.edge), record);
        }
        let topology_edges = by_key.values().map(|record| record.edge).collect();
        let topology_segments = self.build_l0_flush_segments(topology_edges)?;
        Ok(topology_segments
            .into_iter()
            .map(|segment| {
                let records = segment
                    .edges
                    .into_iter()
                    .map(|edge| {
                        by_key
                            .remove(&edge_key(&edge))
                            .unwrap_or_else(|| EdgeRecordWithProperties::topology_only(edge))
                    })
                    .collect();
                L0PropertyFlushSegment {
                    records,
                    semantic_overrides: segment.semantic_overrides,
                }
            })
            .collect())
    }

    fn encode_edge_property_values(
        &self,
        edge_type: EdgeType,
        properties: Vec<(PropertyId, PropertyValue)>,
    ) -> Result<Vec<EdgePropertyValue>> {
        let catalog = self.schema_catalog_snapshot();
        let epoch = catalog.current_epoch;
        let canonical_edge_type = catalog
            .resolve_edge_label(edge_type, epoch)
            .unwrap_or(edge_type);
        properties
            .into_iter()
            .map(|(property_id, value)| {
                let canonical_property_id = catalog
                    .resolve_property(property_id, epoch)
                    .ok_or_else(|| anyhow::anyhow!("property id {property_id} is not visible"))?;
                let property = catalog
                    .properties
                    .get(&canonical_property_id)
                    .ok_or_else(|| {
                        anyhow::anyhow!(
                            "resolved property id {canonical_property_id} is missing from catalog"
                        )
                    })?;
                match property.owner {
                    PropertyOwner::EdgeLabel(owner_edge_type) => {
                        let canonical_owner_edge_type = catalog
                            .resolve_edge_label(owner_edge_type, epoch)
                            .unwrap_or(owner_edge_type);
                        if canonical_owner_edge_type != canonical_edge_type {
                            anyhow::bail!(
                                "property id {property_id} belongs to edge label {}, not {}",
                                owner_edge_type,
                                edge_type
                            );
                        }
                    }
                    PropertyOwner::VertexLabel(owner_vertex_label) => {
                        anyhow::bail!(
                            "property id {property_id} belongs to vertex label {}, not edge label {}",
                            owner_vertex_label,
                            edge_type
                        );
                    }
                }
                let physical_encoding =
                    PropertyPhysicalEncoding::parse(&property.physical_encoding)?;
                Ok(EdgePropertyValue::encoded(
                    canonical_property_id,
                    property.encoding_epoch,
                    encode_with_encoding(physical_encoding, &value)?,
                ))
            })
            .collect()
    }

    fn build_schema_l0_flush_segments(&self, edges: Vec<EdgeRecord>) -> Vec<L0FlushSegment> {
        let mut exact: BTreeMap<(i32, EdgeType), Vec<EdgeRecord>> = BTreeMap::new();
        for edge in edges {
            exact
                .entry((source_label_from_vertex_id(edge.src), edge.edge_type))
                .or_default()
                .push(edge);
        }

        let mut partitions: BTreeMap<(i32, EdgeType), Vec<EdgeRecord>> = BTreeMap::new();
        for ((src_label, edge_type), group) in exact {
            if estimate_segment_bytes(&group) < self.config.min_l0_partition_bytes {
                partitions
                    .entry((src_label, MIXED_EDGE_TYPE))
                    .or_default()
                    .extend(group);
            } else {
                partitions
                    .entry((src_label, edge_type))
                    .or_default()
                    .extend(group);
            }
        }

        let mut segments = Vec::new();
        for ((_, edge_type), mut group) in partitions {
            group.sort_by_key(|e| (e.src, e.edge_type, e.dst, e.ts));
            let semantic_overrides = CsrSegmentSemanticOverrides {
                edge_type_partition: (edge_type == MIXED_EDGE_TYPE).then_some(MIXED_EDGE_TYPE),
                degree_class: Some(DegreeClass::Mixed),
                degree_class_exact: Some(false),
                ..CsrSegmentSemanticOverrides::default()
            };
            segments.extend(
                split_range_bounded_segments(group, self.config.segment_target_bytes)
                    .into_iter()
                    .map(|segment| {
                        L0FlushSegment::with_semantic_overrides(segment, semantic_overrides)
                    }),
            );
        }

        segments
    }

    fn build_label_only_l0_flush_segments(&self, edges: Vec<EdgeRecord>) -> Vec<L0FlushSegment> {
        let mut partitions: BTreeMap<i32, Vec<EdgeRecord>> = BTreeMap::new();
        for edge in edges {
            partitions
                .entry(source_label_from_vertex_id(edge.src))
                .or_default()
                .push(edge);
        }

        let overrides = CsrSegmentSemanticOverrides {
            dst_label: Some(UNKNOWN_SOURCE_LABEL),
            edge_type_partition: Some(MIXED_EDGE_TYPE),
            degree_class: Some(DegreeClass::Mixed),
            degree_class_exact: Some(false),
            ..CsrSegmentSemanticOverrides::default()
        };
        let mut segments = Vec::new();
        for (_, mut group) in partitions {
            group.sort_by_key(|e| (e.src, e.edge_type, e.dst, e.ts));
            segments.extend(
                split_range_bounded_segments(group, self.config.segment_target_bytes)
                    .into_iter()
                    .map(|segment| L0FlushSegment::with_semantic_overrides(segment, overrides)),
            );
        }
        segments
    }

    fn build_edge_type_only_l0_flush_segments(
        &self,
        edges: Vec<EdgeRecord>,
    ) -> Vec<L0FlushSegment> {
        let mut partitions: BTreeMap<EdgeType, Vec<EdgeRecord>> = BTreeMap::new();
        for edge in edges {
            partitions.entry(edge.edge_type).or_default().push(edge);
        }

        let overrides = CsrSegmentSemanticOverrides {
            src_label: Some(UNKNOWN_SOURCE_LABEL),
            dst_label: Some(UNKNOWN_SOURCE_LABEL),
            degree_class: Some(DegreeClass::Mixed),
            degree_class_exact: Some(false),
            ..CsrSegmentSemanticOverrides::default()
        };
        let mut segments = Vec::new();
        for (_, mut group) in partitions {
            group.sort_by_key(|e| (e.src, e.edge_type, e.dst, e.ts));
            segments.extend(
                split_range_bounded_segments(group, self.config.segment_target_bytes)
                    .into_iter()
                    .map(|segment| L0FlushSegment::with_semantic_overrides(segment, overrides)),
            );
        }
        segments
    }

    fn build_degree_only_l0_flush_segments(
        &self,
        mut edges: Vec<EdgeRecord>,
    ) -> Vec<L0FlushSegment> {
        edges.sort_by_key(|e| (e.src, e.edge_type, e.dst, e.ts));
        let overrides = CsrSegmentSemanticOverrides {
            src_label: Some(UNKNOWN_SOURCE_LABEL),
            dst_label: Some(UNKNOWN_SOURCE_LABEL),
            edge_type_partition: Some(MIXED_EDGE_TYPE),
            ..CsrSegmentSemanticOverrides::default()
        };
        let mut segments = Vec::new();
        for (_, mut group) in split_by_source_degree_class(edges) {
            group.sort_by_key(|e| (e.src, e.edge_type, e.dst, e.ts));
            segments.extend(
                split_range_bounded_segments(group, self.config.segment_target_bytes)
                    .into_iter()
                    .map(|segment| L0FlushSegment::with_semantic_overrides(segment, overrides)),
            );
        }
        segments
    }

    fn build_semantic_l0_flush_segments(&self, edges: Vec<EdgeRecord>) -> Vec<L0FlushSegment> {
        let mut exact: BTreeMap<(i32, EdgeType), Vec<EdgeRecord>> = BTreeMap::new();
        for edge in edges {
            exact
                .entry((source_label_from_vertex_id(edge.src), edge.edge_type))
                .or_default()
                .push(edge);
        }

        let mut partitions: BTreeMap<(i32, EdgeType, DegreeClass), Vec<EdgeRecord>> =
            BTreeMap::new();
        for ((src_label, edge_type), group) in exact {
            for (degree_class, degree_group) in split_by_source_degree_class(group) {
                partitions
                    .entry((src_label, edge_type, degree_class))
                    .or_default()
                    .extend(degree_group);
            }
        }

        let mut segments = Vec::new();
        for (_, mut group) in partitions {
            group.sort_by_key(|e| (e.src, e.edge_type, e.dst, e.ts));
            segments.extend(
                split_range_bounded_segments(group, self.config.segment_target_bytes)
                    .into_iter()
                    .map(|segment| L0FlushSegment::new(segment, None)),
            );
        }

        segments
    }

    /// Builds L0 flush segments for RocksDB-style KV-LSM baseline.
    ///
    /// This policy is designed to simulate a RocksDB-style key-value LSM store where
    /// edges are encoded as KV pairs. The key insight is:
    ///
    /// 1. **Write path**: Edges are written to CSR segments in a completely unsemantic way.
    ///    All edges go into a single mixed segment with conservative/Unknown metadata.
    ///
    /// 2. **Semantic metadata is conservative**: All semantic fields are set to Unknown/Mixed:
    ///    - `src_label = UNKNOWN_SOURCE_LABEL` (don't know source vertex label)
    ///    - `edge_type_partition = MIXED_EDGE_TYPE` (don't know edge type)
    ///    - `degree_class = DegreeClass::Mixed` (don't know degree class)
    ///    - `direction = EdgeDirection::Unknown` (don't know direction)
    ///
    /// 3. **No semantic pruning benefit**: Since `summary_completeness = Unknown`, the
    ///    `may_contain_signature()` check in `CsrSegmentMeta` always returns `true`
    ///    (conservative). This means every segment is considered a candidate for every query.
    ///
    /// 4. **This proves the point**: SemL0's contribution is making the semantic metadata
    ///    **useful** at query time. Without semantic pruning logic (i.e., in RocksDB-style),
    ///    the metadata is worthless and causes high read amplification.
    ///
    /// Note: We still partition by key range to keep file sizes reasonable, but all partitions
    /// share the same conservative semantic metadata.
    fn build_rocksdb_style_l0_flush_segments(&self, edges: Vec<EdgeRecord>) -> Vec<L0FlushSegment> {
        // RocksDB-style: sort by (src, edge_type, dst, ts) to enable range scans
        let mut sorted = edges;
        sorted.sort_by_key(|e| (e.src, e.edge_type, e.dst, e.ts));

        // Split into range-bounded segments (same as Naive, but with conservative metadata)
        let mut segments = Vec::new();
        for group in split_range_bounded_segments(sorted, self.config.segment_target_bytes) {
            segments.push(L0FlushSegment::fully_mixed(group));
        }
        segments
    }

    fn build_budgeted_semantic_l0_flush_segments(
        &self,
        mut edges: Vec<EdgeRecord>,
    ) -> Result<Vec<L0FlushSegment>> {
        edges.sort_by_key(|e| {
            (
                source_label_from_vertex_id(e.src),
                e.edge_type,
                e.src,
                e.dst,
                e.ts,
            )
        });
        let mut partitions: BTreeMap<(i32, EdgeType, DegreeClass), Vec<EdgeRecord>> =
            BTreeMap::new();
        let mut needs_sort: BTreeSet<(i32, EdgeType, DegreeClass)> = BTreeSet::new();
        let mut candidates = Vec::new();
        let feedback_edge_type_weights = if self.config.semantic_budget_disable_feedback {
            HashMap::new()
        } else {
            semantic_budget_feedback_edge_type_weights(&self.metrics.l0_partition_snapshots())
        };
        let mut group_start = 0usize;
        while group_start < edges.len() {
            let src_label = source_label_from_vertex_id(edges[group_start].src);
            let edge_type = edges[group_start].edge_type;
            let mut group_end = group_start + 1;
            while group_end < edges.len()
                && source_label_from_vertex_id(edges[group_end].src) == src_label
                && edges[group_end].edge_type == edge_type
            {
                group_end += 1;
            }
            let group = &edges[group_start..group_end];
            let group_bytes = estimate_segment_bytes(&group);
            let decision = evaluate_budgeted_semantic_edge_type(
                edge_type,
                group_bytes,
                self.config.segment_target_bytes,
                self.config.semantic_budget_min_edge_type_bytes,
                self.config.semantic_budget_min_edge_type_score,
                self.config.semantic_budget_core_edge_weight,
                self.config.semantic_budget_reverse_core_edge_weight,
                self.config.semantic_budget_other_edge_weight,
                self.config.semantic_budget_feedback_only,
                feedback_edge_type_weights
                    .get(&(src_label, edge_type))
                    .copied(),
            );
            candidates.push(BudgetedEdgeCandidate {
                src_label,
                edge_type,
                start: group_start,
                end: group_end,
                group_bytes,
                group_edges: group.len(),
                group_sources: count_sorted_group_sources(group),
                estimated_exact_files: decision.estimated_exact_files,
                configured_query_weight: decision.configured_query_weight,
                feedback_query_weight: decision.feedback_query_weight,
                query_weight: decision.query_weight,
                score: decision.score,
                selected: decision.selected,
                reason: decision.reason,
            });

            group_start = group_end;
        }

        let mut merged_bytes_by_src_label: BTreeMap<i32, usize> = BTreeMap::new();
        apply_budgeted_semantic_edge_type_allowlist(
            &mut candidates,
            &self.config.semantic_budget_edge_type_allowlist,
        );
        {
            let mut state = self.state.lock();
            apply_budgeted_semantic_edge_type_file_budget(
                &mut candidates,
                self.config.semantic_budget_max_extra_l0_files,
                &mut state.semantic_budget_used_extra_l0_files,
            );
        }
        for candidate in &candidates {
            if !candidate.selected {
                *merged_bytes_by_src_label
                    .entry(candidate.src_label)
                    .or_default() += candidate.group_bytes;
            }
        }
        let merged_file_estimates: BTreeMap<i32, usize> = merged_bytes_by_src_label
            .into_iter()
            .map(|(src_label, bytes)| {
                (
                    src_label,
                    estimate_split_segment_count(bytes, self.config.segment_target_bytes).max(1),
                )
            })
            .collect();
        self.append_budgeted_edge_candidate_diagnostics(&candidates, &merged_file_estimates)?;

        for candidate in &candidates {
            let group = &edges[candidate.start..candidate.end];
            let src_label = candidate.src_label;
            let edge_type = candidate.edge_type;
            if !candidate.selected {
                // Unselected edge types keep their real edge_type (degree merged to
                // Mixed) instead of collapsing to MIXED_EDGE_TYPE. This makes the
                // budgeted layout a smooth schema->semantic interpolation: at budget 0
                // every edge type is schema-style (src_label+edge_type, degree-mixed),
                // and raising the file budget promotes high-benefit edge types to exact
                // degree partitions. Collapsing to MIXED_EDGE_TYPE used to discard the
                // edge-type pruning schema already provides, pushing intermediate
                // budgets *below* the schema baseline (read-amp cliff). See
                // baseline/progress-note: budgeted now stays <= schema at all budgets.
                let key = (src_label, edge_type, DegreeClass::Mixed);
                needs_sort.insert(key);
                partitions.entry(key).or_default().extend_from_slice(group);
                continue;
            }

            let mut degree_bytes: BTreeMap<DegreeClass, usize> = BTreeMap::new();
            let mut degree_sources: BTreeMap<DegreeClass, usize> = BTreeMap::new();
            let mut total_sources = 0usize;
            let mut src_start = candidate.start;
            while src_start < candidate.end {
                let src = edges[src_start].src;
                let mut src_end = src_start + 1;
                while src_end < candidate.end && edges[src_end].src == src {
                    src_end += 1;
                }
                let degree_class = DegreeClass::from_max_degree((src_end - src_start) as u64);
                *degree_bytes.entry(degree_class).or_default() +=
                    estimate_segment_bytes(&edges[src_start..src_end]);
                *degree_sources.entry(degree_class).or_default() += 1;
                total_sources += 1;
                src_start = src_end;
            }

            let mut src_start = candidate.start;
            while src_start < candidate.end {
                let src = edges[src_start].src;
                let mut src_end = src_start + 1;
                while src_end < candidate.end && edges[src_end].src == src {
                    src_end += 1;
                }
                let degree_class = DegreeClass::from_max_degree((src_end - src_start) as u64);
                let keep_degree_exact = should_preserve_budgeted_semantic_degree(
                    degree_class,
                    *degree_bytes.get(&degree_class).unwrap_or(&0),
                    *degree_sources.get(&degree_class).unwrap_or(&0),
                    total_sources,
                    self.config.semantic_budget_min_exact_bytes,
                    self.config.semantic_budget_min_benefit_score,
                    self.config.semantic_budget_degree_weight,
                );
                let key = if keep_degree_exact {
                    (src_label, edge_type, degree_class)
                } else {
                    (src_label, edge_type, DegreeClass::Mixed)
                };
                partitions
                    .entry(key)
                    .or_default()
                    .extend_from_slice(&edges[src_start..src_end]);
                src_start = src_end;
            }
        }

        let mut segments = Vec::new();
        for (key, mut group) in partitions {
            if needs_sort.contains(&key) {
                group.sort_by_key(|e| (e.src, e.edge_type, e.dst, e.ts));
            }
            // Partitions whose degree was merged by policy (unselected edge types,
            // or low-benefit degree classes of selected ones) must say so
            // explicitly: segment content can be coincidentally uniform, and a
            // content-computed degree_class_exact=true would both enable degree
            // pruning the policy never paid for and be charged against the file
            // budget on reopen.
            let degree_merged = matches!(key.2, DegreeClass::Mixed | DegreeClass::Unknown);
            let semantic_overrides = CsrSegmentSemanticOverrides {
                edge_type_partition: (key.1 == MIXED_EDGE_TYPE).then_some(MIXED_EDGE_TYPE),
                degree_class: degree_merged.then_some(DegreeClass::Mixed),
                degree_class_exact: degree_merged.then_some(false),
                ..CsrSegmentSemanticOverrides::default()
            };
            segments.extend(
                split_range_bounded_segments(group, self.config.segment_target_bytes)
                    .into_iter()
                    .map(|segment| {
                        L0FlushSegment::with_semantic_overrides(segment, semantic_overrides)
                    }),
            );
        }

        Ok(segments)
    }

    fn append_budgeted_edge_candidate_diagnostics(
        &self,
        candidates: &[BudgetedEdgeCandidate],
        merged_file_estimates: &BTreeMap<i32, usize>,
    ) -> Result<()> {
        if candidates.is_empty() {
            return Ok(());
        }

        let path = self.config.store_dir.join("budgeted-edge-candidates.tsv");
        let _guard = self.manifest_lock.lock();
        let needs_header = match std::fs::metadata(&path) {
            Ok(metadata) => metadata.len() == 0,
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => true,
            Err(err) => return Err(err.into()),
        };

        let mut file = OpenOptions::new().create(true).append(true).open(&path)?;
        if needs_header {
            file.write_all(
                b"flush_snapshot\tsrc_label\tedge_type\tgroup_edges\tgroup_sources\tgroup_bytes\testimated_exact_files\testimated_merged_files\tquery_weight\tscore\tselected\treason\tconfigured_query_weight\tfeedback_query_weight\tfeedback_only\tfeedback_disabled\n",
            )?;
        }
        let snapshot = self.current_snapshot();
        for candidate in candidates {
            let estimated_merged_files = if candidate.selected {
                0
            } else {
                *merged_file_estimates
                    .get(&candidate.src_label)
                    .unwrap_or(&candidate.estimated_exact_files)
            };
            writeln!(
                file,
                "{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{:.6}\t{:.6}\t{}\t{}\t{:.6}\t{:.6}\t{}\t{}",
                snapshot,
                candidate.src_label,
                candidate.edge_type,
                candidate.group_edges,
                candidate.group_sources,
                candidate.group_bytes,
                candidate.estimated_exact_files,
                estimated_merged_files,
                candidate.query_weight,
                candidate.score,
                candidate.selected,
                candidate.reason,
                candidate.configured_query_weight,
                candidate.feedback_query_weight,
                self.config.semantic_budget_feedback_only,
                self.config.semantic_budget_disable_feedback
            )?;
        }
        file.sync_all()?;
        Ok(())
    }

    pub async fn get_neighbors(
        &self,
        src: VertexId,
        snapshot: SnapshotId,
    ) -> Result<Vec<EdgeRecord>> {
        self.get_neighbors_typed_internal(src, None, snapshot).await
    }

    pub async fn get_neighbors_typed(
        &self,
        src: VertexId,
        edge_type: EdgeType,
        snapshot: SnapshotId,
    ) -> Result<Vec<EdgeRecord>> {
        self.get_neighbors_typed_internal(src, Some(edge_type), snapshot)
            .await
    }

    pub async fn get_neighbors_by_signature(
        &self,
        signature: GraphAccessSignature,
        snapshot: SnapshotId,
    ) -> Result<Vec<EdgeRecord>> {
        self.get_neighbors_signature_internal(signature, snapshot)
            .await
    }

    pub async fn get_neighbors_matching_property_value(
        &self,
        src: VertexId,
        edge_type: EdgeType,
        snapshot: SnapshotId,
        property_id: PropertyId,
        expected: PropertyValue,
    ) -> Result<Vec<EdgeRecord>> {
        let (query_edge_type, predicate) =
            self.property_value_predicate_for_edge_query(edge_type, property_id, expected)?;
        self.get_neighbors_matching_csr_property_value_prototype(
            src,
            Some(query_edge_type),
            snapshot,
            predicate,
        )
        .await
    }

    /// Prototype Engine boundary for property value predicates.
    ///
    /// This deliberately does not extend `GraphAccessSignature`: the current
    /// signature remains a compact topology/presence descriptor. Topology-only
    /// rows still participate as snapshot blockers, and they can match only
    /// when the predicate carries an explicit schema-default absent policy.
    pub async fn get_neighbors_matching_csr_property_value_prototype(
        &self,
        src: VertexId,
        edge_type: Option<EdgeType>,
        snapshot: SnapshotId,
        predicate: CsrPropertyValuePredicate,
    ) -> Result<Vec<EdgeRecord>> {
        let started = Instant::now();
        self.metrics
            .get_neighbors_ops
            .fetch_add(1, Ordering::Relaxed);
        let _lock = self.vertex_locks.read_lock(src);
        let guard = self.version_manager.pin_current();
        let mut candidates = Vec::new();
        let registry =
            PropertyEncodingRegistry::from_schema_catalog(&self.schema_catalog_snapshot())?;

        for memgraph in &guard.version().memgraphs {
            for record in memgraph.get_edges_with_properties_for_src(src) {
                if edge_type
                    .map(|ty| record.edge.edge_type != ty)
                    .unwrap_or(false)
                {
                    continue;
                }
                let matches_predicate = edge_properties_match_value_predicate(
                    &record.properties,
                    &registry,
                    &predicate,
                )?;
                candidates.push(PropertyValueCandidateEdge {
                    edge: record.edge,
                    matches_predicate,
                });
            }
        }

        let signature = GraphAccessSignature::neighbor_scan(src, edge_type);
        let reader = CsrReader::with_metrics_and_cache(
            self.backend.clone(),
            self.config.store_dir.clone(),
            self.metrics.clone(),
            self.metadata_cache.clone(),
        );

        for (level_id, level) in guard.version().levels.iter().enumerate() {
            for meta in level {
                if !meta.may_contain_signature(&signature)
                    || src < meta.min_src
                    || src > meta.max_src
                {
                    continue;
                }
                if matches!(reader.cached_may_contain_src(meta, src), Some(false)) {
                    continue;
                }
                if level_id == L0 as usize {
                    self.metrics
                        .candidate_l0_segments
                        .fetch_add(1, Ordering::Relaxed);
                }
                if meta.has_property_value_section()
                    && !meta.definitely_lacks_property(predicate.property_id)
                {
                    let rows = reader
                        .get_neighbors_with_decoded_properties(meta, src, &registry)
                        .await?;
                    candidates.extend(rows.into_iter().map(|row| {
                        let matches_predicate =
                            predicate.matches_decoded_properties(&row.properties);
                        PropertyValueCandidateEdge {
                            edge: row.edge,
                            matches_predicate,
                        }
                    }));
                } else {
                    candidates.extend(reader.get_neighbors(meta, src).await?.into_iter().map(
                        |edge| PropertyValueCandidateEdge {
                            edge,
                            matches_predicate: predicate.absent_property_matches(),
                        },
                    ));
                }
            }
        }

        let mut out = merge_visible_property_value_candidates(candidates, snapshot);
        if let Some(edge_type) = edge_type {
            out.retain(|edge| edge.edge_type == edge_type);
        }
        self.metrics
            .storage_get_neighbors_latency
            .record_since(started);
        Ok(out)
    }

    /// Prototype Engine boundary for exact property-presence predicates.
    ///
    /// This complements `GraphAccessSignature::with_required_property`, which
    /// is a segment-level pruning hint. The method keeps non-matching newer
    /// rows as blockers during MVCC merge, then returns only the visible latest
    /// rows that actually carry `property_id`.
    pub async fn get_neighbors_with_present_property_prototype(
        &self,
        src: VertexId,
        edge_type: Option<EdgeType>,
        snapshot: SnapshotId,
        property_id: PropertyId,
    ) -> Result<Vec<EdgeRecord>> {
        let started = Instant::now();
        self.metrics
            .get_neighbors_ops
            .fetch_add(1, Ordering::Relaxed);
        let _lock = self.vertex_locks.read_lock(src);
        let guard = self.version_manager.pin_current();
        let mut candidates = Vec::new();

        for memgraph in &guard.version().memgraphs {
            for record in memgraph.get_edges_with_properties_for_src(src) {
                if edge_type
                    .map(|ty| record.edge.edge_type != ty)
                    .unwrap_or(false)
                {
                    continue;
                }
                let matches_predicate = record
                    .properties
                    .iter()
                    .any(|property| property.property_id == property_id);
                candidates.push(PropertyValueCandidateEdge {
                    edge: record.edge,
                    matches_predicate,
                });
            }
        }

        let signature = GraphAccessSignature::neighbor_scan(src, edge_type);
        let reader = CsrReader::with_metrics_and_cache(
            self.backend.clone(),
            self.config.store_dir.clone(),
            self.metrics.clone(),
            self.metadata_cache.clone(),
        );

        for (level_id, level) in guard.version().levels.iter().enumerate() {
            for meta in level {
                if !meta.may_contain_signature(&signature)
                    || src < meta.min_src
                    || src > meta.max_src
                {
                    continue;
                }
                if matches!(reader.cached_may_contain_src(meta, src), Some(false)) {
                    continue;
                }
                if level_id == L0 as usize {
                    self.metrics
                        .candidate_l0_segments
                        .fetch_add(1, Ordering::Relaxed);
                }
                if meta.has_property_value_section() && !meta.definitely_lacks_property(property_id)
                {
                    let rows = reader.get_neighbors_with_properties(meta, src).await?;
                    candidates.extend(rows.into_iter().map(|row| {
                        let matches_predicate = row
                            .properties
                            .iter()
                            .any(|property| property.property_id == property_id);
                        PropertyValueCandidateEdge {
                            edge: row.edge,
                            matches_predicate,
                        }
                    }));
                } else {
                    candidates.extend(reader.get_neighbors(meta, src).await?.into_iter().map(
                        |edge| PropertyValueCandidateEdge {
                            edge,
                            matches_predicate: false,
                        },
                    ));
                }
            }
        }

        let mut out = merge_visible_property_value_candidates(candidates, snapshot);
        if let Some(edge_type) = edge_type {
            out.retain(|edge| edge.edge_type == edge_type);
        }
        self.metrics
            .storage_get_neighbors_latency
            .record_since(started);
        Ok(out)
    }

    pub fn csr_property_value_predicate_with_current_default_prototype(
        &self,
        property_id: PropertyId,
        expected: PropertyValue,
    ) -> Result<CsrPropertyValuePredicate> {
        let catalog = self.schema_catalog_snapshot();
        let epoch = catalog.current_epoch;
        let canonical_property_id = catalog
            .resolve_property(property_id, epoch)
            .ok_or_else(|| anyhow::anyhow!("property id {property_id} is not visible"))?;
        let property = catalog
            .properties
            .get(&canonical_property_id)
            .ok_or_else(|| {
                anyhow::anyhow!(
                    "resolved property id {canonical_property_id} is missing from catalog"
                )
            })?;
        let physical_encoding = PropertyPhysicalEncoding::parse(&property.physical_encoding)?;
        let Some(default_value) =
            parse_default_or_null_rule(physical_encoding, &property.default_or_null_rule)?
        else {
            return Ok(CsrPropertyValuePredicate::equals(
                canonical_property_id,
                expected,
            ));
        };
        Ok(CsrPropertyValuePredicate::equals_with_schema_default(
            canonical_property_id,
            expected,
            default_value,
        ))
    }

    fn property_value_predicate_for_edge_query(
        &self,
        edge_type: EdgeType,
        property_id: PropertyId,
        expected: PropertyValue,
    ) -> Result<(EdgeType, CsrPropertyValuePredicate)> {
        let catalog = self.schema_catalog_snapshot();
        let epoch = catalog.current_epoch;
        let query_edge_type = catalog
            .resolve_edge_label(edge_type, epoch)
            .unwrap_or(edge_type);
        let canonical_property_id = catalog
            .resolve_property(property_id, epoch)
            .ok_or_else(|| anyhow::anyhow!("property id {property_id} is not visible"))?;
        let property = catalog
            .properties
            .get(&canonical_property_id)
            .ok_or_else(|| {
                anyhow::anyhow!(
                    "resolved property id {canonical_property_id} is missing from catalog"
                )
            })?;
        match property.owner {
            PropertyOwner::EdgeLabel(owner_edge_type) => {
                let canonical_owner_edge_type = catalog
                    .resolve_edge_label(owner_edge_type, epoch)
                    .unwrap_or(owner_edge_type);
                if canonical_owner_edge_type != query_edge_type {
                    anyhow::bail!(
                        "property id {property_id} belongs to edge label {}, not {}",
                        owner_edge_type,
                        edge_type
                    );
                }
            }
            PropertyOwner::VertexLabel(owner_vertex_label) => {
                anyhow::bail!(
                    "property id {property_id} belongs to vertex label {}, not edge label {}",
                    owner_vertex_label,
                    edge_type
                );
            }
        }
        let physical_encoding = PropertyPhysicalEncoding::parse(&property.physical_encoding)?;
        let predicate =
            match parse_default_or_null_rule(physical_encoding, &property.default_or_null_rule)? {
                Some(default_value) => CsrPropertyValuePredicate::equals_with_schema_default(
                    canonical_property_id,
                    expected,
                    default_value,
                ),
                None => CsrPropertyValuePredicate::equals(canonical_property_id, expected),
            };
        Ok((query_edge_type, predicate))
    }

    async fn get_neighbors_typed_internal(
        &self,
        src: VertexId,
        edge_type: Option<EdgeType>,
        snapshot: SnapshotId,
    ) -> Result<Vec<EdgeRecord>> {
        let signature = GraphAccessSignature::neighbor_scan(src, edge_type);
        let degree_classes = edge_type
            .and_then(|edge_type| self.degree_directory.read().get(&(src, edge_type)).copied());
        self.get_neighbors_signature_internal_with_l0_degree_classes(
            signature,
            snapshot,
            degree_classes,
        )
        .await
    }

    async fn get_neighbors_signature_internal(
        &self,
        signature: GraphAccessSignature,
        snapshot: SnapshotId,
    ) -> Result<Vec<EdgeRecord>> {
        let degree_classes = signature.edge_type.and_then(|edge_type| {
            self.degree_directory
                .read()
                .get(&(signature.src, edge_type))
                .copied()
        });
        let signature = if signature.degree_class.is_some()
            && degree_classes
                .map(|mask| degree_class_mask_count(mask) > 1)
                .unwrap_or(false)
        {
            signature.without_degree_class()
        } else {
            signature
        };
        self.get_neighbors_signature_internal_with_l0_degree_classes(
            signature,
            snapshot,
            degree_classes,
        )
        .await
    }

    async fn get_neighbors_signature_internal_with_l0_degree_classes(
        &self,
        signature: GraphAccessSignature,
        snapshot: SnapshotId,
        l0_degree_classes: Option<DegreeClassMask>,
    ) -> Result<Vec<EdgeRecord>> {
        let src = signature.src;
        let edge_type = signature.edge_type;
        let started = Instant::now();
        self.metrics
            .get_neighbors_ops
            .fetch_add(1, Ordering::Relaxed);
        let _lock = self.vertex_locks.read_lock(src);
        let guard = self.version_manager.pin_current();
        let mut updates = Vec::new();
        for memgraph in &guard.version().memgraphs {
            updates.extend(memgraph.get_edges_for_src(src));
        }

        let reader = CsrReader::with_metrics_and_cache(
            self.backend.clone(),
            self.config.store_dir.clone(),
            self.metrics.clone(),
            self.metadata_cache.clone(),
        );
        if matches!(self.config.l0_layout, L0LayoutPolicy::LsmGraphStyle) {
            if let Some(l0) = guard.version().levels.get(L0 as usize) {
                updates.extend(self.get_neighbors_lsmgraph_style(l0, src).await?);
            }
        } else if matches!(self.config.l0_layout, L0LayoutPolicy::OracleSemantic) {
            if let Some(l0) = guard.version().levels.get(L0 as usize) {
                updates.extend(
                    self.get_neighbors_oracle_semantic(l0, &reader, &signature, src)
                        .await?,
                );
            }
        } else if let Some(l0) = guard.version().levels.get(L0 as usize) {
            let use_semantic_l0_index =
                signature.edge_type.is_some() && signature.src_label != UNKNOWN_SOURCE_LABEL;
            let indexed_l0 = if use_semantic_l0_index {
                self.semantic_l0_index
                    .read()
                    .candidates_with_degree_classes(&signature, l0_degree_classes)
            } else {
                Vec::new()
            };
            let mut touched_partitions = HashSet::new();
            let fallback_l0;
            let candidate_l0: &[CsrSegmentMeta] = if use_semantic_l0_index {
                &indexed_l0
            } else {
                fallback_l0 = l0.clone();
                &fallback_l0
            };
            for meta in candidate_l0 {
                let decision = meta.signature_pruning_decision(&signature);
                self.metrics.record_signature_pruning_decision(
                    decision.reason,
                    decision.pruned,
                    meta.segment_bytes,
                );
                if decision.pruned {
                    continue;
                }
                let partition_key = self.l0_partition_key(src, edge_type, meta);
                if touched_partitions.insert(partition_key) {
                    self.metrics.record_l0_partition_query(partition_key);
                }
                let mut partition_probe = L0PartitionProbe {
                    candidate_segments: 1,
                    ..L0PartitionProbe::default()
                };
                self.metrics
                    .candidate_l0_segments
                    .fetch_add(1, Ordering::Relaxed);
                if src < meta.min_src || src > meta.max_src {
                    self.metrics
                        .range_filtered_segments
                        .fetch_add(1, Ordering::Relaxed);
                    partition_probe.range_filtered_segments = 1;
                    self.metrics
                        .record_l0_partition_probe(partition_key, partition_probe);
                    continue;
                }
                let cached_may_contain = reader.cached_may_contain_src(meta, src);
                match cached_may_contain {
                    Some(false) => {
                        self.metrics
                            .bloom_filtered_segments
                            .fetch_add(1, Ordering::Relaxed);
                        partition_probe.bloom_filtered_segments = 1;
                        partition_probe.offset_cache_hits = 1;
                        self.metrics
                            .record_l0_partition_probe(partition_key, partition_probe);
                        continue;
                    }
                    Some(true) => {
                        partition_probe.offset_cache_hits = 1;
                    }
                    None => {
                        partition_probe.offset_cache_misses = 1;
                    }
                }
                self.metrics
                    .filter_passed_segments
                    .fetch_add(1, Ordering::Relaxed);
                partition_probe.filter_passed_segments = 1;
                let edges = retain_edges_for_signature(
                    meta,
                    &signature,
                    reader.get_neighbors(meta, src).await?,
                );
                if !edges.is_empty() {
                    self.metrics
                        .matched_l0_segments
                        .fetch_add(1, Ordering::Relaxed);
                    partition_probe.matched_segments = 1;
                    partition_probe.body_reads = 1;
                } else if matches!(cached_may_contain, Some(true)) {
                    partition_probe.bloom_false_positive_probes = 1;
                }
                self.metrics
                    .record_l0_partition_probe(partition_key, partition_probe);
                updates.extend(edges);
            }
        }
        for meta in self.index.get_positions(src) {
            let decision = meta.signature_pruning_decision(&signature);
            self.metrics.record_signature_pruning_decision(
                decision.reason,
                decision.pruned,
                meta.segment_bytes,
            );
            if decision.pruned {
                continue;
            }
            updates.extend(retain_edges_for_signature(
                &meta,
                &signature,
                reader.get_neighbors(&meta, src).await?,
            ));
        }

        if let Some(edge_type) = edge_type {
            updates.retain(|edge| edge.edge_type == edge_type);
        }
        updates = retain_edges_for_dst_label(&signature, updates);
        let out = merge_visible(updates, snapshot);
        self.metrics
            .storage_get_neighbors_latency
            .record_since(started);
        drop(guard);
        drop(_lock);
        self.maybe_run_k4_maintenance(K4MaintenanceTrigger::ReadFeedback)
            .await?;
        Ok(out)
    }

    async fn get_neighbors_lsmgraph_style(
        &self,
        l0: &[CsrSegmentMeta],
        src: VertexId,
    ) -> Result<Vec<EdgeRecord>> {
        let reader = CsrReader::with_metrics_and_cache(
            self.backend.clone(),
            self.config.store_dir.clone(),
            self.metrics.clone(),
            self.metadata_cache.clone(),
        );
        let mut results = Vec::new();
        for meta in l0 {
            if src < meta.min_src || src > meta.max_src {
                continue;
            }
            let cached = reader.cached_may_contain_src(meta, src);
            if matches!(cached, Some(false)) {
                continue;
            }
            let edges: Vec<EdgeRecord> = reader.get_neighbors(meta, src).await?;
            if !edges.is_empty() {
                results.extend(edges);
            }
        }
        Ok(results)
    }

    async fn get_neighbors_oracle_semantic(
        &self,
        l0: &[CsrSegmentMeta],
        reader: &CsrReader<AnyIoBackend>,
        signature: &GraphAccessSignature,
        src: VertexId,
    ) -> Result<Vec<EdgeRecord>> {
        let edge_type = signature.edge_type;
        let oracle_file_ids = self.oracle_l0_index.read().get(src, edge_type);
        if oracle_file_ids.is_empty() {
            return Ok(Vec::new());
        }
        let oracle_meta_set: HashSet<FileId> = oracle_file_ids.iter().copied().collect();
        let mut results = Vec::new();
        for meta in l0 {
            if !oracle_meta_set.contains(&meta.file_id) {
                continue;
            }
            if src < meta.min_src || src > meta.max_src {
                continue;
            }
            if !meta.may_contain_signature(signature) {
                continue;
            }
            self.metrics
                .candidate_l0_segments
                .fetch_add(1, Ordering::Relaxed);
            let edges: Vec<EdgeRecord> = reader.get_neighbors(meta, src).await?;
            if !edges.is_empty() {
                self.metrics
                    .matched_l0_segments
                    .fetch_add(1, Ordering::Relaxed);
                results.extend(edges);
            }
        }
        Ok(results)
    }

    pub async fn scan_edges(&self, snapshot: SnapshotId) -> Result<Vec<EdgeRecord>> {
        let started = Instant::now();
        self.metrics.scan_ops.fetch_add(1, Ordering::Relaxed);
        let guard = self.version_manager.pin_current();
        let mut updates = Vec::new();
        for memgraph in &guard.version().memgraphs {
            updates.extend(memgraph.all_edges_sorted());
        }
        let reader = CsrReader::with_metrics_and_cache(
            self.backend.clone(),
            self.config.store_dir.clone(),
            self.metrics.clone(),
            self.metadata_cache.clone(),
        );
        for level in &guard.version().levels {
            for meta in level {
                updates.extend(reader.read_all_edges(meta).await?);
            }
        }
        let out = merge_visible(updates, snapshot);
        self.metrics
            .storage_scan_edges_latency
            .record_since(started);
        Ok(out)
    }

    pub async fn compact_l0_to_l1(&self) -> Result<Option<CsrSegmentMeta>> {
        self.compact_l0_to_l1_inner(true).await
    }

    pub async fn build_oracle_index(&self) -> Result<OracleIndexStats> {
        let started = Instant::now();
        let guard = self.version_manager.pin_current();
        let l0_files: Vec<_> = guard
            .version()
            .levels
            .get(L0 as usize)
            .cloned()
            .unwrap_or_default();

        if l0_files.is_empty() {
            return Ok(OracleIndexStats::empty());
        }

        let reader = CsrReader::with_metrics_and_cache(
            self.backend.clone(),
            self.config.store_dir.clone(),
            self.metrics.clone(),
            self.metadata_cache.clone(),
        );

        let oracle_index = OracleL0Index::build_from_reader(&l0_files, &reader).await?;
        let entry_count = oracle_index.len();

        *self.oracle_l0_index.write() = oracle_index;

        Ok(OracleIndexStats {
            entry_count,
            l0_segment_count: l0_files.len(),
            elapsed_ms: started.elapsed().as_millis() as u64,
        })
    }

    pub async fn compact_l0_partition_to_l1(
        &self,
        src_label: i32,
        edge_type: Option<EdgeType>,
    ) -> Result<Vec<CsrSegmentMeta>> {
        self.compact_l0_target_to_l1(L0CompactionTarget {
            src_label,
            edge_type,
            min_src: None,
            max_src: None,
        })
        .await
    }

    pub async fn compact_l0_range_to_l1(
        &self,
        src_label: i32,
        edge_type: Option<EdgeType>,
        min_src: VertexId,
        max_src: VertexId,
    ) -> Result<Vec<CsrSegmentMeta>> {
        self.compact_l0_target_to_l1(L0CompactionTarget {
            src_label,
            edge_type,
            min_src: Some(min_src),
            max_src: Some(max_src),
        })
        .await
    }

    pub async fn compact_best_l0_partition_by_score(&self) -> Result<Option<L0CompactionDecision>> {
        if !self.config.supports_feedback_compaction() {
            return Ok(None);
        }
        let Some(pick) = self.pick_l0_partition_by_score() else {
            return Ok(None);
        };
        let outputs = self
            .compact_l0_range_to_l1(
                pick.key.src_label,
                Some(pick.key.edge_type),
                pick.key.range_start,
                pick.key.range_end,
            )
            .await?;
        Ok(Some(L0CompactionDecision {
            key: pick.key,
            score: pick.score,
            estimated_rewrite_bytes: pick.estimated_rewrite_bytes,
            selected_l0_segments: pick.selected_l0_segments,
            query_count: pick.query_count,
            avg_candidate_segments: pick.avg_candidate_segments,
            offset_cache_miss_rate: pick.offset_cache_miss_rate,
            outputs,
        }))
    }

    pub async fn compact_k4_levels(&self) -> Result<Vec<K4LevelCompactionDecision>> {
        self.compact_k4_levels_with_policy(K4MergePolicy::from_config(&self.config))
            .await
    }

    pub async fn compact_k4_levels_with_policy(
        &self,
        policy: K4MergePolicy,
    ) -> Result<Vec<K4LevelCompactionDecision>> {
        let mut decisions = Vec::new();
        let max_output_level = policy
            .max_output_level
            .min(self.config.max_levels.saturating_sub(1).max(1) as LevelId);
        if max_output_level <= L1 {
            return Ok(decisions);
        }
        for source_level in L1..max_output_level {
            let source_count = self
                .version_manager
                .pin_current()
                .version()
                .levels
                .get(source_level as usize)
                .map(Vec::len)
                .unwrap_or(0);
            if source_count < policy.min_input_segments || source_count <= policy.fanout {
                continue;
            }
            if let Some(decision) = self
                .compact_k4_level_to_next(source_level, source_level + 1, policy)
                .await?
            {
                decisions.push(decision);
            }
        }
        Ok(decisions)
    }

    fn pick_l0_partition_by_score(&self) -> Option<PickedL0Partition> {
        let guard = self.version_manager.pin_current();
        let l0_files = guard.version().levels.get(L0 as usize)?;
        if l0_files.is_empty() {
            return None;
        }

        self.metrics
            .l0_partition_snapshots()
            .into_iter()
            .filter_map(|snapshot| self.score_l0_partition(snapshot, l0_files))
            .max_by(|a, b| {
                a.score
                    .partial_cmp(&b.score)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
    }

    fn score_l0_partition(
        &self,
        snapshot: L0PartitionSnapshot,
        l0_files: &[CsrSegmentMeta],
    ) -> Option<PickedL0Partition> {
        if snapshot.query_count < self.config.l0_ra_min_queries {
            return None;
        }
        let target = L0CompactionTarget {
            src_label: snapshot.key.src_label,
            edge_type: Some(snapshot.key.edge_type),
            min_src: Some(snapshot.key.range_start),
            max_src: Some(snapshot.key.range_end),
        };
        let selected: Vec<_> = l0_files
            .iter()
            .filter(|meta| target_matches_meta(meta, target))
            .collect();
        if selected.len() < self.config.l0_ra_min_l0_segments {
            return None;
        }
        let estimated_rewrite_bytes = selected.iter().map(|meta| estimated_meta_bytes(meta)).sum();
        let rewrite_mib = (estimated_rewrite_bytes as f64 / 1_048_576.0).max(1.0);
        let score = snapshot.query_count as f64
            * snapshot.avg_candidate_segments.max(1.0)
            * (1.0 + snapshot.offset_cache_miss_rate)
            / rewrite_mib;
        if score < self.config.l0_ra_min_score {
            return None;
        }
        Some(PickedL0Partition {
            key: snapshot.key,
            score,
            estimated_rewrite_bytes,
            selected_l0_segments: selected.len(),
            query_count: snapshot.query_count,
            avg_candidate_segments: snapshot.avg_candidate_segments,
            offset_cache_miss_rate: snapshot.offset_cache_miss_rate,
        })
    }

    async fn compact_l0_target_to_l1(
        &self,
        target: L0CompactionTarget,
    ) -> Result<Vec<CsrSegmentMeta>> {
        let started = Instant::now();
        self.wait_for_flushes().await?;
        let guard = self.version_manager.pin_current();
        let l0_files = guard
            .version()
            .levels
            .get(L0 as usize)
            .cloned()
            .unwrap_or_default();
        let selected_l0: Vec<_> = l0_files
            .iter()
            .copied()
            .filter(|meta| target_matches_meta(meta, target))
            .collect();
        if selected_l0.is_empty() {
            self.metrics
                .storage_compaction_latency
                .record_since(started);
            return Ok(Vec::new());
        }

        let min_src = selected_l0.iter().map(|m| m.min_src).min().unwrap_or(0);
        let max_src = selected_l0.iter().map(|m| m.max_src).max().unwrap_or(0);
        let l1_files = guard
            .version()
            .levels
            .get(L1 as usize)
            .cloned()
            .unwrap_or_default();
        let selected_l1: Vec<_> = l1_files
            .iter()
            .copied()
            .filter(|meta| {
                partition_overlaps_target(meta, target.src_label, target.edge_type)
                    && ranges_overlap(meta.min_src, meta.max_src, min_src, max_src)
            })
            .collect();

        let reader = CsrReader::with_metrics_and_cache(
            self.backend.clone(),
            self.config.store_dir.clone(),
            self.metrics.clone(),
            self.metadata_cache.clone(),
        );
        let mut updates = Vec::new();
        let mut input_bytes = 0u64;
        for meta in selected_l0.iter().chain(selected_l1.iter()) {
            input_bytes += estimated_meta_bytes(meta);
            updates.extend(
                reader
                    .read_all_edges_with_properties(meta)
                    .await?
                    .into_iter()
                    .map(edge_record_with_properties_from_csr),
            );
        }
        let snapshot = self.current_snapshot();
        let compacted = retain_property_history_for_safe_snapshot(
            updates,
            snapshot,
            self.snapshot_gc_safe_point(),
        );
        let writer = CsrWriter::new(
            self.backend.clone(),
            self.config.store_dir.clone(),
            self.current_schema_epoch(),
        );
        let mut outputs = Vec::new();
        for segment_edges in
            split_property_compaction_segments(compacted, self.config.segment_target_bytes)
        {
            let file_id = self.alloc_file_id();
            let output = writer
                .write_segment_with_properties(L1, file_id, segment_edges)
                .await?;
            self.append_manifest(&ManifestRecord::CreateFile { meta: output })?;
            outputs.push(output);
        }
        for meta in selected_l0.iter().chain(selected_l1.iter()) {
            self.append_manifest(&ManifestRecord::DeleteFile {
                file_id: meta.file_id,
            })?;
        }

        let old = self.version_manager.pin_current();
        let selected_l0_ids: HashSet<_> = selected_l0.iter().map(|m| m.file_id).collect();
        let selected_l1_ids: HashSet<_> = selected_l1.iter().map(|m| m.file_id).collect();
        let mut levels = old.version().levels.clone();
        ensure_level(&mut levels, L1);
        levels[L0 as usize].retain(|meta| !selected_l0_ids.contains(&meta.file_id));
        levels[L1 as usize].retain(|meta| !selected_l1_ids.contains(&meta.file_id));
        levels[L1 as usize].extend(outputs.iter().copied());
        sort_level(&mut levels[L0 as usize]);
        sort_level(&mut levels[L1 as usize]);
        self.version_manager.publish(Version {
            id: old.version().id + 1,
            memgraphs: old.version().memgraphs.clone(),
            levels,
        });
        self.metrics
            .compaction_count
            .fetch_add(1, Ordering::Relaxed);
        self.metrics
            .compaction_input_bytes
            .fetch_add(input_bytes, Ordering::Relaxed);
        let output_bytes = outputs.iter().map(estimated_meta_bytes).sum::<u64>();
        self.metrics
            .compaction_output_bytes
            .fetch_add(output_bytes, Ordering::Relaxed);
        self.rebuild_index().await?;
        self.rebuild_semantic_indexes().await?;
        self.metrics
            .storage_compaction_latency
            .record_since(started);
        Ok(outputs)
    }

    async fn compact_k4_level_to_next(
        &self,
        source_level: LevelId,
        target_level: LevelId,
        policy: K4MergePolicy,
    ) -> Result<Option<K4LevelCompactionDecision>> {
        let started = Instant::now();
        self.wait_for_flushes().await?;
        let guard = self.version_manager.pin_current();
        let selected_source = guard
            .version()
            .levels
            .get(source_level as usize)
            .cloned()
            .unwrap_or_default();
        if selected_source.is_empty() {
            self.metrics
                .storage_compaction_latency
                .record_since(started);
            return Ok(None);
        }
        let selected_target = guard
            .version()
            .levels
            .get(target_level as usize)
            .cloned()
            .unwrap_or_default();
        let input_segments = selected_source.len() + selected_target.len();
        let reader = CsrReader::with_metrics_and_cache(
            self.backend.clone(),
            self.config.store_dir.clone(),
            self.metrics.clone(),
            self.metadata_cache.clone(),
        );
        let mut updates = Vec::new();
        let mut input_bytes = 0u64;
        for meta in selected_source.iter().chain(selected_target.iter()) {
            input_bytes += estimated_meta_bytes(meta);
            updates.extend(
                reader
                    .read_all_edges_with_properties(meta)
                    .await?
                    .into_iter()
                    .map(edge_record_with_properties_from_csr),
            );
        }

        let snapshot = self.current_snapshot();
        let compacted = retain_property_history_for_safe_snapshot(
            updates,
            snapshot,
            self.snapshot_gc_safe_point(),
        );
        let writer = CsrWriter::new(
            self.backend.clone(),
            self.config.store_dir.clone(),
            self.current_schema_epoch(),
        );
        let segments = if policy.semantic_partition_outputs {
            split_k4_semantic_compaction_segments(compacted, self.config.segment_target_bytes)
        } else {
            split_property_compaction_segments(compacted, self.config.segment_target_bytes)
        };
        let mut outputs = Vec::new();
        for segment_edges in segments {
            let file_id = self.alloc_file_id();
            let output = writer
                .write_segment_with_properties(target_level, file_id, segment_edges)
                .await?;
            self.append_manifest(&ManifestRecord::CreateFile { meta: output })?;
            outputs.push(output);
        }
        for meta in selected_source.iter().chain(selected_target.iter()) {
            self.append_manifest(&ManifestRecord::DeleteFile {
                file_id: meta.file_id,
            })?;
        }

        let old = self.version_manager.pin_current();
        let selected_source_ids: HashSet<_> = selected_source.iter().map(|m| m.file_id).collect();
        let selected_target_ids: HashSet<_> = selected_target.iter().map(|m| m.file_id).collect();
        let mut levels = old.version().levels.clone();
        ensure_level(&mut levels, target_level);
        levels[source_level as usize].retain(|meta| !selected_source_ids.contains(&meta.file_id));
        levels[target_level as usize].retain(|meta| !selected_target_ids.contains(&meta.file_id));
        levels[target_level as usize].extend(outputs.iter().copied());
        sort_level(&mut levels[source_level as usize]);
        sort_level(&mut levels[target_level as usize]);
        self.version_manager.publish(Version {
            id: old.version().id + 1,
            memgraphs: old.version().memgraphs.clone(),
            levels,
        });
        self.metrics
            .compaction_count
            .fetch_add(1, Ordering::Relaxed);
        self.metrics
            .compaction_input_bytes
            .fetch_add(input_bytes, Ordering::Relaxed);
        let output_bytes = outputs.iter().map(estimated_meta_bytes).sum::<u64>();
        self.metrics
            .compaction_output_bytes
            .fetch_add(output_bytes, Ordering::Relaxed);
        self.rebuild_index().await?;
        self.rebuild_semantic_indexes().await?;
        self.metrics
            .storage_compaction_latency
            .record_since(started);
        Ok(Some(K4LevelCompactionDecision {
            source_level,
            target_level,
            input_segments,
            output_segments: outputs.len(),
            input_bytes,
            output_bytes,
            semantic_partition_outputs: policy.semantic_partition_outputs,
        }))
    }

    async fn compact_l0_to_l1_inner(&self, force: bool) -> Result<Option<CsrSegmentMeta>> {
        let started = Instant::now();
        self.wait_for_flushes().await?;
        let guard = self.version_manager.pin_current();
        let l0_files = guard
            .version()
            .levels
            .get(L0 as usize)
            .cloned()
            .unwrap_or_default();
        if l0_files.is_empty() {
            self.metrics
                .storage_compaction_latency
                .record_since(started);
            return Ok(None);
        }
        if !force && l0_files.len() < self.config.l0_file_threshold {
            self.metrics
                .storage_compaction_latency
                .record_since(started);
            return Ok(None);
        }
        let l1_files = guard
            .version()
            .levels
            .get(L1 as usize)
            .cloned()
            .unwrap_or_default();

        let reader = CsrReader::with_metrics_and_cache(
            self.backend.clone(),
            self.config.store_dir.clone(),
            self.metrics.clone(),
            self.metadata_cache.clone(),
        );
        let mut updates = Vec::new();
        let mut input_bytes = 0u64;
        for meta in l0_files.iter().chain(l1_files.iter()) {
            input_bytes += estimated_meta_bytes(meta);
            updates.extend(
                reader
                    .read_all_edges_with_properties(meta)
                    .await?
                    .into_iter()
                    .map(edge_record_with_properties_from_csr),
            );
        }
        let snapshot = self.current_snapshot();
        let compacted = retain_property_history_for_safe_snapshot(
            updates,
            snapshot,
            self.snapshot_gc_safe_point(),
        );
        let file_id = self.alloc_file_id();
        let writer = CsrWriter::new(
            self.backend.clone(),
            self.config.store_dir.clone(),
            self.current_schema_epoch(),
        );
        let output = writer
            .write_segment_with_properties(L1, file_id, compacted)
            .await?;
        self.append_manifest(&ManifestRecord::CreateFile { meta: output })?;
        for meta in l0_files.iter().chain(l1_files.iter()) {
            self.append_manifest(&ManifestRecord::DeleteFile {
                file_id: meta.file_id,
            })?;
        }

        let old = self.version_manager.pin_current();
        let mut levels = old.version().levels.clone();
        ensure_level(&mut levels, L1);
        levels[L0 as usize].clear();
        levels[L1 as usize].clear();
        levels[L1 as usize].push(output);
        sort_level(&mut levels[L1 as usize]);
        self.version_manager.publish(Version {
            id: old.version().id + 1,
            memgraphs: old.version().memgraphs.clone(),
            levels,
        });
        self.metrics
            .compaction_count
            .fetch_add(1, Ordering::Relaxed);
        self.metrics
            .compaction_input_bytes
            .fetch_add(input_bytes, Ordering::Relaxed);
        self.metrics
            .compaction_output_bytes
            .fetch_add(estimated_meta_bytes(&output), Ordering::Relaxed);
        self.rebuild_index().await?;
        self.rebuild_semantic_indexes().await?;
        self.metrics
            .storage_compaction_latency
            .record_since(started);
        Ok(Some(output))
    }

    async fn rebuild_index(&self) -> Result<()> {
        let started = Instant::now();
        let guard = self.version_manager.pin_current();
        let reader = CsrReader::with_metrics_and_cache(
            self.backend.clone(),
            self.config.store_dir.clone(),
            self.metrics.clone(),
            self.metadata_cache.clone(),
        );
        let mut entries: HashMap<VertexId, Vec<CsrSegmentMeta>> = HashMap::new();
        for (level_id, level) in guard.version().levels.iter().enumerate() {
            if level_id == L0 as usize {
                continue;
            }
            for meta in level {
                // This index is used as a pruning structure for L1+ lookups; an
                // incomplete rebuild can skip live segments, so offset IO errors
                // must be surfaced instead of silently treated as an empty file.
                let offsets = reader.read_offsets(meta).await?;
                for offset in offsets.iter() {
                    entries.entry(offset.src).or_default().push(*meta);
                }
            }
        }
        self.index.rebuild(entries);
        self.metrics
            .storage_rebuild_index_latency
            .record_since(started);
        Ok(())
    }

    async fn load_or_rebuild_semantic_indexes(&self) -> Result<()> {
        let guard = self.version_manager.pin_current();
        let l0_files = guard
            .version()
            .levels
            .get(L0 as usize)
            .cloned()
            .unwrap_or_default();
        let expected_file_ids = degree_directory_sidecar_file_ids(&guard.version().levels);
        if let Some(degree_directory) =
            read_degree_directory_sidecar(&self.config.store_dir, &expected_file_ids)?
        {
            *self.degree_directory.write() = degree_directory;
            *self.semantic_l0_index.write() = SemanticL0Index::rebuild(&l0_files);
            return Ok(());
        }
        drop(guard);
        self.rebuild_semantic_indexes().await
    }

    fn persist_degree_directory_sidecar_for_current_version(&self) -> Result<()> {
        let _persist_guard = self.degree_directory_sidecar_persist_lock.lock();
        let guard = self.version_manager.pin_current();
        let expected_file_ids = degree_directory_sidecar_file_ids(&guard.version().levels);
        let directory = self.degree_directory.read();
        write_degree_directory_sidecar(&self.config.store_dir, &expected_file_ids, &directory)?;
        self.metrics
            .degree_directory_sidecar_persists
            .fetch_add(1, Ordering::Relaxed);
        Ok(())
    }

    fn persist_degree_directory_sidecar_best_effort(&self) {
        if let Err(error) = self.persist_degree_directory_sidecar_for_current_version() {
            eprintln!("[engine] failed to persist degree directory sidecar: {error}");
        }
    }

    async fn rebuild_semantic_indexes(&self) -> Result<()> {
        let guard = self.version_manager.pin_current();
        let reader = CsrReader::with_metrics_and_cache(
            self.backend.clone(),
            self.config.store_dir.clone(),
            self.metrics.clone(),
            self.metadata_cache.clone(),
        );
        let mut degree_directory: HashMap<(VertexId, EdgeType), DegreeClassMask> = HashMap::new();
        if let Some(l0_files) = guard.version().levels.get(L0 as usize) {
            for meta in l0_files {
                let Some(degree_class) = degree_directory_class_for_l0(meta) else {
                    continue;
                };
                // A partially built degree directory must be a hard error: degree-hint
                // routing prunes against it, so silently skipping a tracked exact
                // segment here could turn an IO error into a missed-edge read (false
                // negative). Non-exact/Mixed-degree segments deliberately have no
                // directory entry; the semantic index still reads them conservatively.
                let offsets = reader.read_offsets(meta).await?;
                for offset in offsets.iter() {
                    insert_degree_class_mask(
                        degree_directory
                            .entry((offset.src, meta.edge_type_partition))
                            .or_default(),
                        degree_class,
                    );
                }
            }
        }
        *self.degree_directory.write() = degree_directory;

        let l0_files = guard
            .version()
            .levels
            .get(L0 as usize)
            .cloned()
            .unwrap_or_default();
        *self.semantic_l0_index.write() = SemanticL0Index::rebuild(&l0_files);
        self.persist_degree_directory_sidecar_best_effort();
        Ok(())
    }

    async fn update_semantic_indexes_with_l0_metas(&self, metas: &[CsrSegmentMeta]) -> Result<()> {
        let reader = CsrReader::with_metrics_and_cache(
            self.backend.clone(),
            self.config.store_dir.clone(),
            self.metrics.clone(),
            self.metadata_cache.clone(),
        );
        let mut updates: HashMap<(VertexId, EdgeType), DegreeClassMask> = HashMap::new();
        for meta in metas {
            let Some(degree_class) = degree_directory_class_for_l0(meta) else {
                continue;
            };
            // Same contract as rebuild_semantic_indexes: never leave the degree
            // directory silently incomplete for tracked exact segments.
            let offsets = reader.read_offsets(meta).await?;
            for offset in offsets.iter() {
                insert_degree_class_mask(
                    updates
                        .entry((offset.src, meta.edge_type_partition))
                        .or_default(),
                    degree_class,
                );
            }
        }

        let has_updates = !updates.is_empty();
        if has_updates {
            let mut directory = self.degree_directory.write();
            for (key, mask) in updates {
                *directory.entry(key).or_default() |= mask;
            }
        }

        let l0_files = self
            .version_manager
            .pin_current()
            .version()
            .levels
            .get(L0 as usize)
            .cloned()
            .unwrap_or_default();
        *self.semantic_l0_index.write() = SemanticL0Index::rebuild(&l0_files);
        Ok(())
    }

    pub fn version_guard(&self) -> VersionGuard {
        self.version_manager.pin_current()
    }

    pub fn live_file_count_by_level(&self) -> Vec<usize> {
        let guard = self.version_manager.pin_current();
        guard.version().levels.iter().map(Vec::len).collect()
    }

    fn l0_partition_key(
        &self,
        src: VertexId,
        edge_type: Option<EdgeType>,
        meta: &CsrSegmentMeta,
    ) -> L0PartitionKey {
        let src_label = match source_label_from_vertex_id(src) {
            UNKNOWN_SOURCE_LABEL => meta.src_label,
            label => label,
        };
        let bucket_size = self.config.l0_ra_range_bucket_size.max(1);
        let range_start = (src / bucket_size) * bucket_size;
        let range_end = range_start.saturating_add(bucket_size - 1);
        L0PartitionKey {
            src_label,
            edge_type: edge_type.unwrap_or(meta.edge_type_partition),
            range_start,
            range_end,
        }
    }
}

fn estimate_segment_bytes(edges: &[EdgeRecord]) -> usize {
    edges.len() * std::mem::size_of::<EdgeRecord>()
}

fn count_sorted_group_sources(edges: &[EdgeRecord]) -> usize {
    let Some(first) = edges.first() else {
        return 0;
    };
    let mut count = 1usize;
    let mut last = first.src;
    for edge in &edges[1..] {
        if edge.src != last {
            count += 1;
            last = edge.src;
        }
    }
    count
}

fn evaluate_budgeted_semantic_edge_type(
    edge_type: EdgeType,
    group_bytes: usize,
    target_segment_bytes: usize,
    min_partition_bytes: usize,
    min_edge_type_score: f64,
    core_edge_weight: f64,
    reverse_core_edge_weight: f64,
    other_edge_weight: f64,
    feedback_only: bool,
    feedback_query_weight: Option<f64>,
) -> BudgetedEdgeTypeDecision {
    let estimated_exact_files =
        estimate_split_segment_count(group_bytes, target_segment_bytes).max(1);
    let configured_query_weight = if feedback_only {
        1.0
    } else {
        semantic_budget_edge_type_weight(
            edge_type,
            core_edge_weight,
            reverse_core_edge_weight,
            other_edge_weight,
        )
    };
    let feedback_query_weight = feedback_query_weight
        .filter(|weight| weight.is_finite() && *weight >= 0.0)
        .unwrap_or(if feedback_only { 1.0 } else { 0.0 });
    let query_weight = if feedback_only {
        feedback_query_weight
    } else {
        configured_query_weight.max(feedback_query_weight)
    };
    let score = query_weight * (group_bytes as f64 / 1024.0) / estimated_exact_files as f64;

    if min_partition_bytes == 0 {
        return BudgetedEdgeTypeDecision {
            estimated_exact_files,
            configured_query_weight,
            feedback_query_weight,
            query_weight,
            score,
            selected: true,
            reason: "byte_gate_disabled",
        };
    }
    if group_bytes >= min_partition_bytes {
        return BudgetedEdgeTypeDecision {
            estimated_exact_files,
            configured_query_weight,
            feedback_query_weight,
            query_weight,
            score,
            selected: true,
            reason: "byte_gate",
        };
    }

    let score_enabled = min_edge_type_score.is_finite()
        && min_edge_type_score >= 0.0
        && min_edge_type_score < 1.0e307;
    if !score_enabled {
        return BudgetedEdgeTypeDecision {
            estimated_exact_files,
            configured_query_weight,
            feedback_query_weight,
            query_weight,
            score,
            selected: false,
            reason: "below_byte_gate_score_disabled",
        };
    }
    if score >= min_edge_type_score {
        BudgetedEdgeTypeDecision {
            estimated_exact_files,
            configured_query_weight,
            feedback_query_weight,
            query_weight,
            score,
            selected: true,
            reason: if feedback_query_weight > configured_query_weight {
                "feedback_score_gate"
            } else {
                "score_gate"
            },
        }
    } else {
        BudgetedEdgeTypeDecision {
            estimated_exact_files,
            configured_query_weight,
            feedback_query_weight,
            query_weight,
            score,
            selected: false,
            reason: "below_score_gate",
        }
    }
}

fn semantic_budget_feedback_edge_type_weights(
    snapshots: &[L0PartitionSnapshot],
) -> HashMap<(i32, EdgeType), f64> {
    let mut weights: HashMap<(i32, EdgeType), f64> = HashMap::new();
    for snapshot in snapshots {
        if snapshot.query_count == 0 || snapshot.key.edge_type == MIXED_EDGE_TYPE {
            continue;
        }
        let candidate_pressure = snapshot.avg_candidate_segments.max(1.0);
        let weight = (snapshot.query_count as f64 * candidate_pressure).min(1_000_000.0);
        *weights
            .entry((snapshot.key.src_label, snapshot.key.edge_type))
            .or_default() += weight;
    }
    weights
}

fn apply_budgeted_semantic_edge_type_file_budget(
    candidates: &mut [BudgetedEdgeCandidate],
    max_extra_l0_files: Option<usize>,
    used_extra_l0_files: &mut usize,
) {
    let Some(max_extra_l0_files) = max_extra_l0_files else {
        return;
    };

    let mut eligible: Vec<usize> = candidates
        .iter()
        .enumerate()
        .filter_map(|(idx, candidate)| candidate.selected.then_some(idx))
        .collect();
    eligible.sort_by(|left, right| {
        candidates[*right]
            .score
            .total_cmp(&candidates[*left].score)
            .then_with(|| {
                candidates[*right]
                    .group_edges
                    .cmp(&candidates[*left].group_edges)
            })
            .then_with(|| {
                candidates[*left]
                    .src_label
                    .cmp(&candidates[*right].src_label)
            })
            .then_with(|| {
                candidates[*left]
                    .edge_type
                    .cmp(&candidates[*right].edge_type)
            })
    });

    for idx in eligible {
        let cost = candidates[idx].estimated_exact_files.max(1);
        if used_extra_l0_files.saturating_add(cost) <= max_extra_l0_files {
            *used_extra_l0_files += cost;
        } else {
            candidates[idx].selected = false;
            candidates[idx].reason = "fanout_budget_exhausted";
        }
    }
}

fn apply_budgeted_semantic_edge_type_allowlist(
    candidates: &mut [BudgetedEdgeCandidate],
    edge_type_allowlist: &[EdgeType],
) {
    if edge_type_allowlist.is_empty() {
        return;
    }

    for candidate in candidates {
        if !edge_type_allowlist.contains(&candidate.edge_type) {
            candidate.selected = false;
            candidate.reason = "not_in_edge_type_allowlist";
        }
    }
}

fn initial_semantic_budget_used_extra_l0_files(
    levels: &[Vec<CsrSegmentMeta>],
    max_extra_l0_files: Option<usize>,
) -> usize {
    if max_extra_l0_files.is_none() {
        return 0;
    }
    // Flush-time accounting (apply_budgeted_semantic_edge_type_file_budget) only
    // charges the budget for groups promoted to exact degree partitions; unselected
    // groups still keep their real edge_type with degree merged to Mixed, at zero
    // cost. Reopen must mirror that: counting every non-MIXED file here (the old
    // behavior) charged thousands of schema-style files against budgets of ~64 and
    // permanently exhausted them after a restart.
    levels
        .get(L0 as usize)
        .map(|l0| {
            l0.iter()
                .filter(|meta| {
                    meta.edge_type_partition != MIXED_EDGE_TYPE
                        && meta.degree_class_exact
                        && !matches!(meta.degree_class, DegreeClass::Mixed | DegreeClass::Unknown)
                })
                .count()
        })
        .unwrap_or(0)
}

fn estimate_split_segment_count(group_bytes: usize, target_segment_bytes: usize) -> usize {
    let target = target_segment_bytes.max(1);
    group_bytes.saturating_add(target - 1) / target
}

fn semantic_budget_edge_type_weight(
    edge_type: EdgeType,
    core_edge_weight: f64,
    reverse_core_edge_weight: f64,
    other_edge_weight: f64,
) -> f64 {
    let weight = if is_core_ldbc_edge_type(edge_type) {
        core_edge_weight
    } else if is_core_ldbc_edge_type(edge_type.saturating_abs()) {
        reverse_core_edge_weight
    } else {
        other_edge_weight
    };
    if weight.is_finite() && weight >= 0.0 {
        weight
    } else {
        0.0
    }
}

fn is_core_ldbc_edge_type(edge_type: EdgeType) -> bool {
    matches!(edge_type, 1 | 2 | 3 | 7 | 8 | 9 | 10 | 11 | 12)
}

fn should_preserve_budgeted_semantic_degree(
    degree_class: DegreeClass,
    class_bytes: usize,
    class_sources: usize,
    total_sources: usize,
    min_exact_bytes: usize,
    min_benefit_score: f64,
    degree_weight: f64,
) -> bool {
    if matches!(degree_class, DegreeClass::Unknown | DegreeClass::Mixed)
        || class_bytes == 0
        || class_sources == 0
        || total_sources == 0
    {
        return false;
    }

    let metadata_cost = min_exact_bytes.max(1) as f64;
    let min_score = if min_benefit_score.is_finite() && min_benefit_score >= 0.0 {
        min_benefit_score
    } else {
        1.0
    };
    let configured_weight = if degree_weight.is_finite() && degree_weight >= 0.0 {
        degree_weight
    } else {
        1.0
    };
    let class_weight = match degree_class {
        DegreeClass::Low => 1.0,
        DegreeClass::Medium => 1.25,
        DegreeClass::High => 2.0,
        DegreeClass::Unknown | DegreeClass::Mixed => 0.0,
    };
    let competing_query_fraction = if total_sources > class_sources {
        (total_sources - class_sources) as f64 / total_sources as f64
    } else {
        1.0
    };
    let score = (class_bytes as f64 / metadata_cost)
        * competing_query_fraction
        * class_weight
        * configured_weight;
    score >= min_score
}

fn split_by_source_degree_class(mut edges: Vec<EdgeRecord>) -> Vec<(DegreeClass, Vec<EdgeRecord>)> {
    edges.sort_by_key(|e| (e.src, e.edge_type, e.dst, e.ts));
    let mut groups: BTreeMap<DegreeClass, Vec<EdgeRecord>> = BTreeMap::new();
    let mut idx = 0usize;
    while idx < edges.len() {
        let src = edges[idx].src;
        let start = idx;
        while idx < edges.len() && edges[idx].src == src {
            idx += 1;
        }
        let degree_class = DegreeClass::from_max_degree((idx - start) as u64);
        groups
            .entry(degree_class)
            .or_default()
            .extend_from_slice(&edges[start..idx]);
    }
    groups.into_iter().collect()
}

fn split_range_bounded_segments(
    edges: Vec<EdgeRecord>,
    target_segment_bytes: usize,
) -> Vec<Vec<EdgeRecord>> {
    if edges.is_empty() {
        return Vec::new();
    }
    let target_edges = (target_segment_bytes / std::mem::size_of::<EdgeRecord>()).max(1);
    let mut segments = Vec::new();
    let mut current = Vec::new();
    let mut last_src = None;

    for edge in edges {
        if !current.is_empty()
            && current.len() >= target_edges
            && last_src.is_some_and(|src| src != edge.src)
        {
            segments.push(std::mem::take(&mut current));
        }
        last_src = Some(edge.src);
        current.push(edge);
    }
    if !current.is_empty() {
        segments.push(current);
    }
    segments
}

fn merge_l0_flush_segments_to_cap(
    segments: Vec<L0FlushSegment>,
    max_segments: usize,
) -> Vec<L0FlushSegment> {
    if segments.len() <= max_segments || max_segments == 0 {
        return segments;
    }
    let chunk_size = segments.len().div_ceil(max_segments);
    let mut merged = Vec::new();
    let mut current = Vec::new();
    for (idx, segment) in segments.into_iter().enumerate() {
        current.extend(segment.edges);
        if (idx + 1) % chunk_size == 0 {
            merged.push(L0FlushSegment::fully_mixed(std::mem::take(&mut current)));
        }
    }
    if !current.is_empty() {
        merged.push(L0FlushSegment::fully_mixed(current));
    }
    merged
}

fn split_property_compaction_segments(
    mut records: Vec<EdgeRecordWithProperties>,
    target_segment_bytes: usize,
) -> Vec<Vec<EdgeRecordWithProperties>> {
    if records.is_empty() {
        return Vec::new();
    }
    records.sort_by_key(|record| {
        (
            record.edge.src,
            record.edge.edge_type,
            record.edge.dst,
            record.edge.ts,
        )
    });
    let target_edges = (target_segment_bytes / std::mem::size_of::<EdgeRecord>()).max(1);
    let mut segments = Vec::new();
    let mut current = Vec::new();
    let mut last_src = None;

    for record in records {
        if !current.is_empty()
            && current.len() >= target_edges
            && last_src.is_some_and(|src| src != record.edge.src)
        {
            segments.push(std::mem::take(&mut current));
        }
        last_src = Some(record.edge.src);
        current.push(record);
    }
    if !current.is_empty() {
        segments.push(current);
    }
    segments
}

fn split_k4_semantic_compaction_segments(
    mut records: Vec<EdgeRecordWithProperties>,
    target_segment_bytes: usize,
) -> Vec<Vec<EdgeRecordWithProperties>> {
    if records.is_empty() {
        return Vec::new();
    }
    records.sort_by_key(|record| {
        (
            source_label_from_vertex_id(record.edge.src),
            record.edge.edge_type,
            record.edge.src,
            record.edge.dst,
            record.edge.ts,
            record.edge.marker as u8,
        )
    });
    let mut groups: BTreeMap<(i32, EdgeType), Vec<EdgeRecordWithProperties>> = BTreeMap::new();
    for record in records {
        groups
            .entry((
                source_label_from_vertex_id(record.edge.src),
                record.edge.edge_type,
            ))
            .or_default()
            .push(record);
    }
    let mut segments = Vec::new();
    for (_, group) in groups {
        segments.extend(split_property_compaction_segments(
            group,
            target_segment_bytes,
        ));
    }
    segments
}

fn partition_overlaps_target(
    meta: &CsrSegmentMeta,
    src_label: i32,
    edge_type: Option<EdgeType>,
) -> bool {
    let label_matches = src_label == UNKNOWN_SOURCE_LABEL
        || meta.src_label == UNKNOWN_SOURCE_LABEL
        || meta.src_label == src_label;
    let edge_matches = match edge_type {
        Some(edge_type) => {
            meta.edge_type_partition == MIXED_EDGE_TYPE || meta.edge_type_partition == edge_type
        }
        None => true,
    };
    label_matches && edge_matches
}

fn target_matches_meta(meta: &CsrSegmentMeta, target: L0CompactionTarget) -> bool {
    if !partition_overlaps_target(meta, target.src_label, target.edge_type) {
        return false;
    }
    match (target.min_src, target.max_src) {
        (Some(min_src), Some(max_src)) => {
            ranges_overlap(meta.min_src, meta.max_src, min_src, max_src)
        }
        _ => true,
    }
}

fn ranges_overlap(
    left_min: VertexId,
    left_max: VertexId,
    right_min: VertexId,
    right_max: VertexId,
) -> bool {
    left_min <= right_max && right_min <= left_max
}

fn estimated_meta_bytes(meta: &CsrSegmentMeta) -> u64 {
    if meta.segment_bytes > 0 {
        meta.segment_bytes
    } else {
        meta.edge_count * std::mem::size_of::<EdgeRecord>() as u64
    }
}

fn sort_level(level: &mut [CsrSegmentMeta]) {
    level.sort_by_key(|m| (m.src_label, m.edge_type_partition, m.min_src, m.file_id));
}

fn merge_visible(updates: Vec<EdgeRecord>, snapshot: SnapshotId) -> Vec<EdgeRecord> {
    merge_latest_records(updates, snapshot, false)
        .into_iter()
        .filter(|e| e.marker == EdgeMarker::Insert)
        .collect()
}

fn edge_key(edge: &EdgeRecord) -> (VertexId, EdgeType, VertexId, SnapshotId) {
    (edge.src, edge.edge_type, edge.dst, edge.ts)
}

fn edge_record_with_properties_from_csr(
    row: CsrEdgeRecordWithProperties,
) -> EdgeRecordWithProperties {
    EdgeRecordWithProperties {
        edge: row.edge,
        properties: row
            .properties
            .into_iter()
            .map(|property| EdgePropertyValue {
                property_id: property.property_id,
                encoding_epoch: property.encoding_epoch,
                encoded_value: property.encoded_value,
                flags: property.flags,
            })
            .collect(),
    }
}

fn edge_properties_match_value_predicate(
    properties: &[EdgePropertyValue],
    registry: &PropertyEncodingRegistry,
    predicate: &CsrPropertyValuePredicate,
) -> Result<bool> {
    let mut saw_property = false;
    for property in properties {
        if property.property_id != predicate.property_id {
            continue;
        }
        saw_property = true;
        let value = registry.decode(
            property.property_id,
            property.encoding_epoch,
            &property.encoded_value,
        )?;
        if value == predicate.expected {
            return Ok(true);
        }
    }
    Ok(!saw_property && predicate.absent_property_matches())
}

#[derive(Debug, Clone, Copy)]
struct PropertyValueCandidateEdge {
    edge: EdgeRecord,
    matches_predicate: bool,
}

fn merge_visible_property_value_candidates(
    mut updates: Vec<PropertyValueCandidateEdge>,
    snapshot: SnapshotId,
) -> Vec<EdgeRecord> {
    updates.retain(|candidate| candidate.edge.ts <= snapshot);
    updates.sort_by(|a, b| {
        (
            a.edge.src,
            a.edge.edge_type,
            a.edge.dst,
            std::cmp::Reverse(a.edge.ts),
        )
            .cmp(&(
                b.edge.src,
                b.edge.edge_type,
                b.edge.dst,
                std::cmp::Reverse(b.edge.ts),
            ))
    });
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    for candidate in updates {
        let key = (
            candidate.edge.src,
            candidate.edge.edge_type,
            candidate.edge.dst,
        );
        if seen.insert(key)
            && candidate.edge.marker == EdgeMarker::Insert
            && candidate.matches_predicate
        {
            out.push(candidate.edge);
        }
    }
    out.sort_by_key(|e| (e.src, e.edge_type, e.dst));
    out
}

fn retain_edges_for_property_predicate(
    meta: &CsrSegmentMeta,
    signature: &GraphAccessSignature,
    edges: Vec<EdgeRecord>,
) -> Vec<EdgeRecord> {
    match signature.property_predicate {
        Some(PropertyPredicate::RequiredPresent { property_id })
            if meta.definitely_lacks_property(property_id) =>
        {
            edges
                .into_iter()
                .filter(|edge| edge.marker == EdgeMarker::Delete)
                .collect()
        }
        _ => edges,
    }
}

fn retain_edges_for_signature(
    meta: &CsrSegmentMeta,
    signature: &GraphAccessSignature,
    edges: Vec<EdgeRecord>,
) -> Vec<EdgeRecord> {
    let edges = retain_edges_for_property_predicate(meta, signature, edges);
    retain_edges_for_dst_label(signature, edges)
}

fn retain_edges_for_dst_label(
    signature: &GraphAccessSignature,
    edges: Vec<EdgeRecord>,
) -> Vec<EdgeRecord> {
    let Some(dst_label) = signature.dst_label else {
        return edges;
    };
    edges
        .into_iter()
        .filter(|edge| source_label_from_vertex_id(edge.dst) == dst_label)
        .collect()
}

fn merge_latest_records(
    mut updates: Vec<EdgeRecord>,
    snapshot: SnapshotId,
    keep_tombstones: bool,
) -> Vec<EdgeRecord> {
    updates.retain(|e| e.ts <= snapshot);
    updates.sort_by(|a, b| {
        (a.src, a.edge_type, a.dst, std::cmp::Reverse(a.ts)).cmp(&(
            b.src,
            b.edge_type,
            b.dst,
            std::cmp::Reverse(b.ts),
        ))
    });
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    for edge in updates {
        let key = (edge.src, edge.edge_type, edge.dst);
        if seen.insert(key) && (keep_tombstones || edge.marker == EdgeMarker::Insert) {
            out.push(edge);
        }
    }
    out.sort_by_key(|e| (e.src, e.edge_type, e.dst));
    out
}

#[cfg(test)]
fn retain_history_for_safe_snapshot(
    mut updates: Vec<EdgeRecord>,
    current_snapshot: SnapshotId,
    safe_snapshot: SnapshotId,
) -> Vec<EdgeRecord> {
    updates.retain(|e| e.ts <= current_snapshot);
    let safe_snapshot = safe_snapshot.min(current_snapshot);
    if safe_snapshot == 0 {
        updates.sort_by_key(|e| (e.src, e.edge_type, e.dst, e.ts));
        return updates;
    }

    let mut latest_prefix = HashMap::new();
    let mut suffix = Vec::new();
    for edge in updates {
        if edge.ts <= safe_snapshot {
            let key = (edge.src, edge.edge_type, edge.dst);
            let should_replace = latest_prefix
                .get(&key)
                .map(|existing: &EdgeRecord| edge.ts > existing.ts)
                .unwrap_or(true);
            if should_replace {
                latest_prefix.insert(key, edge);
            }
        } else {
            suffix.push(edge);
        }
    }

    let mut updates: Vec<_> = latest_prefix.into_values().chain(suffix).collect();
    updates.sort_by_key(|e| (e.src, e.edge_type, e.dst, e.ts));
    updates
}

fn retain_property_history_for_safe_snapshot(
    mut updates: Vec<EdgeRecordWithProperties>,
    current_snapshot: SnapshotId,
    safe_snapshot: SnapshotId,
) -> Vec<EdgeRecordWithProperties> {
    updates.retain(|record| record.edge.ts <= current_snapshot);
    let safe_snapshot = safe_snapshot.min(current_snapshot);
    if safe_snapshot == 0 {
        updates.sort_by_key(|record| {
            (
                record.edge.src,
                record.edge.edge_type,
                record.edge.dst,
                record.edge.ts,
            )
        });
        return updates;
    }

    let mut latest_prefix = HashMap::new();
    let mut suffix = Vec::new();
    for record in updates {
        if record.edge.ts <= safe_snapshot {
            let key = (record.edge.src, record.edge.edge_type, record.edge.dst);
            let should_replace = latest_prefix
                .get(&key)
                .map(|existing: &EdgeRecordWithProperties| record.edge.ts > existing.edge.ts)
                .unwrap_or(true);
            if should_replace {
                latest_prefix.insert(key, record);
            }
        } else {
            suffix.push(record);
        }
    }

    let mut updates: Vec<_> = latest_prefix.into_values().chain(suffix).collect();
    updates.sort_by_key(|record| {
        (
            record.edge.src,
            record.edge.edge_type,
            record.edge.dst,
            record.edge.ts,
        )
    });
    updates
}

#[cfg(test)]
mod tests {
    use super::*;

    fn insert(dst: VertexId, ts: SnapshotId) -> EdgeRecord {
        EdgeRecord::insert(1, dst, 7, ts)
    }

    fn delete(dst: VertexId, ts: SnapshotId) -> EdgeRecord {
        EdgeRecord::delete(1, dst, 7, ts)
    }

    fn value_candidate(edge: EdgeRecord, matches_predicate: bool) -> PropertyValueCandidateEdge {
        PropertyValueCandidateEdge {
            edge,
            matches_predicate,
        }
    }

    fn property_record(dst: VertexId, ts: SnapshotId, value: u8) -> EdgeRecordWithProperties {
        EdgeRecordWithProperties {
            edge: insert(dst, ts),
            properties: vec![EdgePropertyValue::encoded(5, 1, [value])],
        }
    }

    fn test_meta(
        file_id: FileId,
        edge_type_partition: EdgeType,
        degree_class: DegreeClass,
        degree_class_exact: bool,
    ) -> CsrSegmentMeta {
        CsrSegmentMeta {
            file_id,
            level: L0,
            src_label: 5,
            dst_label: UNKNOWN_SOURCE_LABEL,
            schema_epoch: 0,
            summary_completeness: crate::schema::SemanticSummaryCompleteness::Exact,
            property_summary_completeness: crate::schema::SemanticSummaryCompleteness::Exact,
            property_presence_bitmap: 0,
            property_encoding_epoch: 0,
            property_index_offset: 0,
            property_index_len: 0,
            property_values_offset: 0,
            property_values_len: 0,
            may_contain_tombstones: false,
            edge_type_partition,
            direction: crate::semantic::EdgeDirection::Out,
            degree_class,
            degree_class_exact,
            sort_key: crate::semantic::SegmentSortKey::SrcEdgeDstTs,
            min_src: 360287970189639680,
            max_src: 360287970189647634,
            min_ts: 1,
            edge_count: 1,
            unique_src_count: 1,
            avg_degree_x100: 100,
            max_degree: 1,
            segment_bytes: 1,
            source_bloom_offset: 0,
            source_bloom_len: 0,
            source_bloom_bit_count: 0,
            max_ts: 1,
        }
    }

    fn assert_degree_class_candidates(mask: DegreeClassMask, expected: &[DegreeClass]) {
        let actual: BTreeSet<_> = degree_classes_that_may_contain_query_mask(mask)
            .into_iter()
            .collect();
        let expected: BTreeSet<_> = expected.iter().copied().collect();
        assert_eq!(actual, expected);
    }

    #[test]
    fn dst_label_signature_filters_returned_edges() {
        let dst_label_3 = (3u64 << 56) | 11;
        let dst_label_4 = (4u64 << 56) | 12;
        let signature =
            GraphAccessSignature::neighbor_scan((5u64 << 56) | 1, Some(7)).with_dst_label(3);

        let filtered = retain_edges_for_dst_label(
            &signature,
            vec![insert(dst_label_3, 1), insert(dst_label_4, 1)],
        );

        assert_eq!(filtered, vec![insert(dst_label_3, 1)]);
    }

    #[test]
    fn degree_class_mask_preserves_query_candidate_semantics() {
        assert_degree_class_candidates(
            degree_class_mask(DegreeClass::Low),
            &[DegreeClass::Low, DegreeClass::Mixed, DegreeClass::Unknown],
        );
        assert_degree_class_candidates(
            degree_class_mask(DegreeClass::Medium),
            &[
                DegreeClass::Low,
                DegreeClass::Medium,
                DegreeClass::Mixed,
                DegreeClass::Unknown,
            ],
        );
        assert_degree_class_candidates(
            degree_class_mask(DegreeClass::High),
            &[
                DegreeClass::Low,
                DegreeClass::Medium,
                DegreeClass::High,
                DegreeClass::Mixed,
                DegreeClass::Unknown,
            ],
        );

        let mut mask = 0;
        insert_degree_class_mask(&mut mask, DegreeClass::Medium);
        insert_degree_class_mask(&mut mask, DegreeClass::Medium);
        assert_eq!(degree_class_mask_count(mask), 1);

        insert_degree_class_mask(&mut mask, DegreeClass::High);
        assert_eq!(degree_class_mask_count(mask), 2);
        assert_degree_class_candidates(
            mask,
            &[
                DegreeClass::Low,
                DegreeClass::Medium,
                DegreeClass::High,
                DegreeClass::Mixed,
                DegreeClass::Unknown,
            ],
        );
    }

    #[test]
    fn degree_directory_sidecar_round_trips_and_rejects_stale_files() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("target")
            .join("test-tmp");
        fs::create_dir_all(&root).unwrap();
        let tmp = tempfile::Builder::new()
            .prefix("degree-directory-sidecar-")
            .tempdir_in(root)
            .unwrap();

        let file_ids = vec![11, 12];
        let mut mask = 0;
        insert_degree_class_mask(&mut mask, DegreeClass::Low);
        insert_degree_class_mask(&mut mask, DegreeClass::High);
        let mut directory = HashMap::new();
        directory.insert((42, 7), mask);

        write_degree_directory_sidecar(tmp.path(), &file_ids, &directory).unwrap();
        let loaded = read_degree_directory_sidecar(tmp.path(), &file_ids)
            .unwrap()
            .unwrap();
        assert_eq!(loaded.get(&(42, 7)), Some(&mask));

        assert!(
            read_degree_directory_sidecar(tmp.path(), &[11])
                .unwrap()
                .is_none(),
            "sidecar must be ignored when the tracked L0 file set is stale"
        );

        fs::write(degree_directory_sidecar_path(tmp.path()), b"not-a-sidecar").unwrap();
        assert!(
            read_degree_directory_sidecar(tmp.path(), &file_ids)
                .unwrap()
                .is_none(),
            "corrupt sidecar should fall back to rebuild"
        );
    }

    #[test]
    fn semantic_l0_index_degree_override_keeps_lower_class_mixed_segments() {
        let src = 360287970189640353;
        let edge_type = -16;
        let mixed_low = test_meta(1, MIXED_EDGE_TYPE, DegreeClass::Low, true);
        let exact_medium = test_meta(2, edge_type, DegreeClass::Medium, true);
        let index = SemanticL0Index::rebuild(&[mixed_low, exact_medium]);
        let signature = GraphAccessSignature::neighbor_scan(src, Some(edge_type));

        let candidates = index.candidates_with_degree_classes(
            &signature,
            Some(degree_class_mask(DegreeClass::Medium)),
        );
        let file_ids: Vec<_> = candidates.iter().map(|meta| meta.file_id).collect();

        assert!(
            file_ids.contains(&mixed_low.file_id),
            "mixed low-degree segments can still contain edges for a medium global query"
        );
        assert!(file_ids.contains(&exact_medium.file_id));
    }

    #[test]
    fn degree_directory_tracks_only_exact_degree_l0_semantic_files() {
        let exact_low = test_meta(1, 7, DegreeClass::Low, true);
        assert_eq!(
            degree_directory_class_for_l0(&exact_low),
            Some(DegreeClass::Low)
        );

        let schema_style = test_meta(2, 7, DegreeClass::Mixed, false);
        assert_eq!(degree_directory_class_for_l0(&schema_style), None);

        let mixed_edge_type = test_meta(3, MIXED_EDGE_TYPE, DegreeClass::Low, true);
        assert_eq!(degree_directory_class_for_l0(&mixed_edge_type), None);

        let exact_flag_but_mixed_class = test_meta(4, 7, DegreeClass::Mixed, true);
        assert_eq!(
            degree_directory_class_for_l0(&exact_flag_but_mixed_class),
            None
        );

        let mut l1_exact = test_meta(5, 7, DegreeClass::Low, true);
        l1_exact.level = L1;
        assert_eq!(degree_directory_class_for_l0(&l1_exact), None);

        let mut legacy_unknown = test_meta(6, 7, DegreeClass::Low, true);
        legacy_unknown.summary_completeness = crate::schema::SemanticSummaryCompleteness::Unknown;
        assert_eq!(degree_directory_class_for_l0(&legacy_unknown), None);
    }

    #[test]
    fn missing_degree_directory_override_still_reads_schema_style_mixed_segments() {
        let src = 360287970189640353;
        let edge_type = 7;
        let schema_style = test_meta(1, edge_type, DegreeClass::Mixed, false);
        let exact_medium = test_meta(2, edge_type, DegreeClass::Medium, true);
        let index = SemanticL0Index::rebuild(&[schema_style, exact_medium]);
        let signature = GraphAccessSignature::neighbor_scan(src, Some(edge_type))
            .with_degree_class(DegreeClass::Low);

        let candidates = index.candidates_with_degree_classes(&signature, None);
        let file_ids: Vec<_> = candidates.iter().map(|meta| meta.file_id).collect();

        assert!(
            file_ids.contains(&schema_style.file_id),
            "schema-style degree-mixed segments remain conservative candidates without a directory entry"
        );
        assert!(
            !file_ids.contains(&exact_medium.file_id),
            "exact medium partitions can still be pruned for an explicit low-degree query"
        );
    }

    #[test]
    fn initial_budget_only_counts_exact_degree_partitions() {
        // schema-style files (real edge type, degree merged to Mixed) are free at
        // flush time, so reopen must not charge them against the budget.
        let exact_low = test_meta(1, 7, DegreeClass::Low, true);
        let schema_style = test_meta(2, 7, DegreeClass::Mixed, false);
        let mixed_type = test_meta(3, MIXED_EDGE_TYPE, DegreeClass::Low, true);
        let exact_flag_but_mixed_class = test_meta(4, 7, DegreeClass::Mixed, true);
        let levels = vec![vec![
            exact_low,
            schema_style,
            mixed_type,
            exact_flag_but_mixed_class,
        ]];

        assert_eq!(
            initial_semantic_budget_used_extra_l0_files(&levels, Some(64)),
            1
        );
        assert_eq!(
            initial_semantic_budget_used_extra_l0_files(&levels, None),
            0
        );
    }

    #[test]
    fn oracle_build_from_edges_requires_range_and_type_not_just_label() {
        // Regression for the old `a && b && c || d` precedence bug: a segment
        // whose src_label matched was indexed even when the edge was outside its
        // src range or edge-type partition.
        let src = 360287970189640353u64; // label 5, inside test_meta's src range
        let in_range_t7 = test_meta(1, 7, DegreeClass::Mixed, false);
        let mut out_of_range_t7 = test_meta(2, 7, DegreeClass::Mixed, false);
        out_of_range_t7.min_src = src + 100_000;
        out_of_range_t7.max_src = src + 200_000;
        let in_range_t9 = test_meta(3, 9, DegreeClass::Mixed, false);
        let in_range_mixed_type = test_meta(4, MIXED_EDGE_TYPE, DegreeClass::Mixed, false);

        let edge = EdgeRecord::insert(src, 42, 7, 1);
        let index = OracleL0Index::build_from_edges(
            &[edge],
            &[
                in_range_t7,
                out_of_range_t7,
                in_range_t9,
                in_range_mixed_type,
            ],
        );

        assert_eq!(index.get(src, Some(7)), vec![1, 4]);
        assert!(index.get(src, Some(9)).is_empty());
    }

    #[test]
    fn snapshot_retention_without_safe_point_keeps_all_history() {
        let retained = retain_history_for_safe_snapshot(vec![insert(2, 1), delete(2, 2)], 2, 0);

        assert_eq!(retained, vec![insert(2, 1), delete(2, 2)]);
        assert_eq!(merge_visible(retained.clone(), 1), vec![insert(2, 1)]);
        assert!(merge_visible(retained, 2).is_empty());
    }

    #[test]
    fn snapshot_retention_with_safe_point_keeps_latest_prefix_version() {
        let retained =
            retain_history_for_safe_snapshot(vec![insert(2, 1), insert(2, 2), delete(2, 3)], 3, 2);

        assert_eq!(retained, vec![insert(2, 2), delete(2, 3)]);
        assert_eq!(merge_visible(retained.clone(), 2), vec![insert(2, 2)]);
        assert!(merge_visible(retained, 3).is_empty());
    }

    #[test]
    fn snapshot_retention_safe_after_delete_can_keep_only_tombstone() {
        let retained = retain_history_for_safe_snapshot(vec![insert(2, 1), delete(2, 2)], 2, 2);

        assert_eq!(retained, vec![delete(2, 2)]);
        assert!(merge_visible(retained, 2).is_empty());
    }

    #[test]
    fn property_snapshot_retention_preserves_payloads() {
        let retained = retain_property_history_for_safe_snapshot(
            vec![property_record(2, 1, 7), property_record(2, 2, 42)],
            2,
            1,
        );

        assert_eq!(
            retained,
            vec![property_record(2, 1, 7), property_record(2, 2, 42)]
        );
    }

    #[test]
    fn property_snapshot_retention_safe_point_keeps_latest_payload() {
        let retained = retain_property_history_for_safe_snapshot(
            vec![
                property_record(2, 1, 7),
                property_record(2, 2, 42),
                EdgeRecordWithProperties::topology_only(delete(2, 3)),
            ],
            3,
            2,
        );

        assert_eq!(
            retained,
            vec![
                property_record(2, 2, 42),
                EdgeRecordWithProperties::topology_only(delete(2, 3))
            ]
        );
    }

    #[test]
    fn value_predicate_merge_outputs_latest_matching_insert() {
        let updates = vec![
            value_candidate(insert(2, 1), true),
            value_candidate(insert(3, 1), false),
        ];

        assert_eq!(
            merge_visible_property_value_candidates(updates, 1),
            vec![insert(2, 1)]
        );
    }

    #[test]
    fn value_predicate_merge_nonmatching_insert_hides_older_match() {
        let updates = vec![
            value_candidate(insert(2, 1), true),
            value_candidate(insert(2, 2), false),
        ];

        assert_eq!(
            merge_visible_property_value_candidates(updates.clone(), 1),
            vec![insert(2, 1)]
        );
        assert!(merge_visible_property_value_candidates(updates, 2).is_empty());
    }

    #[test]
    fn value_predicate_merge_delete_hides_older_match() {
        let updates = vec![
            value_candidate(insert(2, 1), true),
            value_candidate(delete(2, 2), false),
        ];

        assert_eq!(
            merge_visible_property_value_candidates(updates.clone(), 1),
            vec![insert(2, 1)]
        );
        assert!(merge_visible_property_value_candidates(updates, 2).is_empty());
    }
}

async fn prepare_store(path: &Path, fresh: bool) -> Result<()> {
    let path = path.to_path_buf();
    tokio::task::spawn_blocking(move || -> Result<()> {
        if fresh && path.exists() {
            std::fs::remove_dir_all(&path)?;
        }
        std::fs::create_dir_all(path.join("levels/L0"))?;
        std::fs::create_dir_all(path.join("levels/L1"))?;
        Ok(())
    })
    .await??;
    Ok(())
}

fn load_or_initialize_schema_catalog(
    store_dir: &Path,
    fresh: bool,
    bootstrap_epoch: SchemaEpoch,
) -> Result<SchemaCatalog> {
    let path = SchemaCatalog::path_for_store(store_dir);
    let mut catalog = if !fresh && path.exists() {
        SchemaCatalog::load(&path)?
    } else {
        SchemaCatalog::new()
    };
    let changed = catalog.ensure_epoch_at_least(bootstrap_epoch);
    if fresh || changed || !path.exists() {
        catalog.save(&path)?;
    }
    Ok(catalog)
}

fn ensure_level(levels: &mut Vec<Vec<CsrSegmentMeta>>, level: u8) {
    let idx = level as usize;
    if levels.len() <= idx {
        levels.resize(idx + 1, Vec::new());
    }
}

fn level_count(levels: &[usize], level: u8) -> usize {
    levels.get(level as usize).copied().unwrap_or(0)
}
