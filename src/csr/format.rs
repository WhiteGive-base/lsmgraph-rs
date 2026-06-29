use std::path::PathBuf;

use serde::{Deserialize, Serialize};

use crate::schema::{PropertyId, SchemaEpoch, SemanticSummaryCompleteness};
use crate::semantic::{
    DegreeClass, EdgeDirection, GraphAccessSignature, PropertyPredicate, SegmentSortKey,
};
use crate::types::{
    EdgeMarker, EdgeRecord, EdgeType, FileId, LevelId, Timestamp, VertexId, MIXED_EDGE_TYPE,
    UNKNOWN_SOURCE_LABEL,
};

pub const CSR_MAGIC: u32 = 0x4753_4d4c;
pub const CSR_VERSION: u16 = 1;
pub const CSR_HEADER_LEN: usize = 128;
pub const EDGE_OFFSET_LEN: usize = 24;
pub const DISK_EDGE_BODY_LEN: usize = 32;
pub const DISK_PROPERTY_LIST_HEADER_LEN: usize = 4;
pub const DISK_PROPERTY_VALUE_INDEX_ENTRY_LEN: usize = 28;

#[derive(Debug, Clone, Copy)]
pub struct CsrHeader {
    pub magic: u32,
    pub version: u16,
    pub header_len: u16,
    pub level: LevelId,
    pub flags: u8,
    pub file_id: FileId,
    pub create_ts: Timestamp,
    pub min_ts: Timestamp,
    pub max_ts: Timestamp,
    pub src_label: i32,
    pub edge_type_partition: EdgeType,
    pub min_src: VertexId,
    pub max_src: VertexId,
    pub edge_offset_count: u64,
    pub edge_body_count: u64,
    pub offsets_offset: u64,
    pub offsets_len: u64,
    pub bodies_offset: u64,
    pub bodies_len: u64,
    pub checksum: u64,
}

impl CsrHeader {
    pub fn encode(&self) -> [u8; CSR_HEADER_LEN] {
        let mut out = [0u8; CSR_HEADER_LEN];
        put_u32(&mut out, 0, self.magic);
        put_u16(&mut out, 4, self.version);
        put_u16(&mut out, 6, self.header_len);
        out[8] = self.level;
        out[9] = self.flags;
        put_u64(&mut out, 16, self.file_id);
        put_u64(&mut out, 24, self.create_ts);
        put_u64(&mut out, 32, self.max_ts);
        put_u64(&mut out, 112, self.min_ts);
        out[120..124].copy_from_slice(&self.src_label.to_le_bytes());
        out[124..128].copy_from_slice(&self.edge_type_partition.to_le_bytes());
        put_u64(&mut out, 40, self.min_src);
        put_u64(&mut out, 48, self.max_src);
        put_u64(&mut out, 56, self.edge_offset_count);
        put_u64(&mut out, 64, self.edge_body_count);
        put_u64(&mut out, 72, self.offsets_offset);
        put_u64(&mut out, 80, self.offsets_len);
        put_u64(&mut out, 88, self.bodies_offset);
        put_u64(&mut out, 96, self.bodies_len);
        put_u64(&mut out, 104, self.checksum);
        out
    }

    pub fn decode(buf: &[u8]) -> anyhow::Result<Self> {
        if buf.len() < CSR_HEADER_LEN {
            anyhow::bail!("CSR header too short: {}", buf.len());
        }
        let header = Self {
            magic: get_u32(buf, 0),
            version: get_u16(buf, 4),
            header_len: get_u16(buf, 6),
            level: buf[8],
            flags: buf[9],
            file_id: get_u64(buf, 16),
            create_ts: get_u64(buf, 24),
            min_ts: get_u64(buf, 112),
            max_ts: get_u64(buf, 32),
            src_label: i32::from_le_bytes(buf[120..124].try_into().unwrap()),
            edge_type_partition: i32::from_le_bytes(buf[124..128].try_into().unwrap()),
            min_src: get_u64(buf, 40),
            max_src: get_u64(buf, 48),
            edge_offset_count: get_u64(buf, 56),
            edge_body_count: get_u64(buf, 64),
            offsets_offset: get_u64(buf, 72),
            offsets_len: get_u64(buf, 80),
            bodies_offset: get_u64(buf, 88),
            bodies_len: get_u64(buf, 96),
            checksum: get_u64(buf, 104),
        };
        if header.magic != CSR_MAGIC {
            anyhow::bail!("bad CSR magic: {:#x}", header.magic);
        }
        if header.version != CSR_VERSION {
            anyhow::bail!("unsupported CSR version: {}", header.version);
        }
        Ok(header)
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub struct CsrSegmentMeta {
    pub file_id: FileId,
    pub level: LevelId,
    #[serde(default = "default_unknown_source_label")]
    pub src_label: i32,
    #[serde(default = "default_unknown_source_label")]
    pub dst_label: i32,
    #[serde(default)]
    pub schema_epoch: SchemaEpoch,
    #[serde(default)]
    pub summary_completeness: SemanticSummaryCompleteness,
    #[serde(default)]
    pub property_summary_completeness: SemanticSummaryCompleteness,
    #[serde(default)]
    pub property_presence_bitmap: u64,
    #[serde(default)]
    pub property_encoding_epoch: SchemaEpoch,
    #[serde(default)]
    pub property_index_offset: u64,
    #[serde(default)]
    pub property_index_len: u64,
    #[serde(default)]
    pub property_values_offset: u64,
    #[serde(default)]
    pub property_values_len: u64,
    #[serde(default)]
    pub source_bloom_offset: u64,
    #[serde(default)]
    pub source_bloom_len: u64,
    #[serde(default)]
    pub source_bloom_bit_count: u64,
    #[serde(default = "default_may_contain_tombstones")]
    pub may_contain_tombstones: bool,
    #[serde(default = "default_mixed_edge_type")]
    pub edge_type_partition: EdgeType,
    #[serde(default)]
    pub direction: EdgeDirection,
    #[serde(default)]
    pub degree_class: DegreeClass,
    #[serde(default)]
    pub degree_class_exact: bool,
    #[serde(default)]
    pub sort_key: SegmentSortKey,
    pub min_src: VertexId,
    pub max_src: VertexId,
    #[serde(default)]
    pub min_ts: Timestamp,
    pub edge_count: u64,
    #[serde(default)]
    pub unique_src_count: u64,
    #[serde(default)]
    pub avg_degree_x100: u64,
    #[serde(default)]
    pub max_degree: u64,
    #[serde(default)]
    pub segment_bytes: u64,
    pub max_ts: Timestamp,
}

impl CsrSegmentMeta {
    pub fn relative_path(&self) -> PathBuf {
        PathBuf::from("levels")
            .join(format!("L{}", self.level))
            .join(format!("{:012}.edge", self.file_id))
    }

    pub fn may_contain_partition(&self, src: VertexId, edge_type: Option<EdgeType>) -> bool {
        self.may_contain_signature(&GraphAccessSignature::neighbor_scan(src, edge_type))
    }

    pub fn has_property_value_section(&self) -> bool {
        self.property_index_len > 0
    }

    pub fn has_source_bloom_section(&self) -> bool {
        self.source_bloom_offset > 0 && self.source_bloom_len > 0 && self.source_bloom_bit_count > 0
    }

    pub fn signature_pruning_decision(
        &self,
        signature: &GraphAccessSignature,
    ) -> SignaturePruningDecision {
        let time_matches = !signature
            .min_ts
            .map(|min_ts| self.max_ts < min_ts)
            .unwrap_or(false)
            && !signature
                .max_ts
                .map(|max_ts| self.min_ts > max_ts)
                .unwrap_or(false);
        if !time_matches {
            return SignaturePruningDecision::pruned("time");
        }
        if !self.summary_completeness.allows_semantic_pruning() {
            return SignaturePruningDecision::kept("mixed_unknown_fallback");
        }

        if !signature.label_matches(self.src_label) {
            return SignaturePruningDecision::pruned("src_label");
        }
        if let Some(edge_type) = signature.edge_type {
            if self.edge_type_partition != MIXED_EDGE_TYPE && self.edge_type_partition != edge_type
            {
                return SignaturePruningDecision::pruned("edge_type");
            }
        }
        if !signature.direction_matches(self.direction) {
            return SignaturePruningDecision::pruned("direction");
        }
        if let Some(degree_class) = signature.degree_class {
            if self.degree_class_exact {
                if !self.degree_class.may_contain_global_query(degree_class) {
                    return SignaturePruningDecision::pruned("degree");
                }
            } else {
                return SignaturePruningDecision::kept("budgeted_not_materialized");
            }
        }
        if let Some(dst_label) = signature.dst_label {
            if self.dst_label != UNKNOWN_SOURCE_LABEL && self.dst_label != dst_label {
                return SignaturePruningDecision::pruned("dst_label");
            }
        }
        if let Some(PropertyPredicate::RequiredPresent { property_id }) =
            signature.property_predicate
        {
            if self.definitely_lacks_property(property_id) {
                if self.may_contain_tombstones {
                    return SignaturePruningDecision::kept("schema_tombstone_fallback");
                }
                return SignaturePruningDecision::pruned("property_absence");
            }
            if !self.property_summary_completeness.allows_semantic_pruning() {
                return SignaturePruningDecision::kept("mixed_unknown_fallback");
            }
        }

        SignaturePruningDecision::kept("kept_candidate")
    }

    pub fn may_contain_signature(&self, signature: &GraphAccessSignature) -> bool {
        !self.signature_pruning_decision(signature).pruned
    }

    pub fn may_contain_property(&self, property_id: PropertyId) -> bool {
        !self.definitely_lacks_property(property_id)
    }

    pub fn definitely_lacks_property(&self, property_id: PropertyId) -> bool {
        let Some(bit) = property_presence_bit(property_id) else {
            return false;
        };
        self.property_summary_completeness.allows_semantic_pruning()
            && self.property_presence_bitmap & bit == 0
    }

    pub fn may_satisfy_property_predicate(&self, predicate: PropertyPredicate) -> bool {
        match predicate {
            PropertyPredicate::RequiredPresent { property_id } => {
                self.may_contain_tombstones || self.may_contain_property(property_id)
            }
            PropertyPredicate::AbsentOrDefault { property_id: _ } => true,
        }
    }

    /// Derive the orthogonal, multi-dimensional semantic state of this segment.
    ///
    /// This is the C2 measurement primitive. Topology is **key-based**: a segment
    /// whose `src_label`/`edge_type_partition` collapsed to the
    /// `UNKNOWN_SOURCE_LABEL`/`MIXED_EDGE_TYPE` sentinels (e.g. the output of a
    /// naive, non-semantic merge over multiple labels/edge types) is `Mixed`,
    /// because the CSR writer always records `summary_completeness == Exact` for
    /// freshly written segments — so the key sentinels, not the completeness flag,
    /// are what signals a degraded pruning surface.
    pub fn semantic_state(&self, current_epoch: SchemaEpoch) -> SegmentSemanticState {
        let topology = if !self.summary_completeness.allows_semantic_pruning() {
            TopologySummaryState::Unknown
        } else if self.src_label == UNKNOWN_SOURCE_LABEL
            || self.edge_type_partition == MIXED_EDGE_TYPE
        {
            TopologySummaryState::Mixed
        } else {
            TopologySummaryState::Exact
        };
        let degree = if !self.summary_completeness.allows_semantic_pruning()
            || matches!(self.degree_class, DegreeClass::Unknown)
        {
            DegreeSummaryState::Unknown
        } else if self.degree_class_exact
            && matches!(
                self.degree_class,
                DegreeClass::Low | DegreeClass::Medium | DegreeClass::High
            )
        {
            DegreeSummaryState::Exact
        } else {
            DegreeSummaryState::Mixed
        };
        let tombstone = if self.may_contain_tombstones {
            TombstoneState::Sensitive
        } else {
            TombstoneState::Clean
        };
        let schema = if self.schema_epoch == current_epoch {
            SchemaState::Current
        } else if self.schema_epoch < current_epoch {
            SchemaState::OlderEpoch
        } else {
            SchemaState::Uncertain
        };
        SegmentSemanticState {
            topology,
            degree,
            property: self.property_summary_completeness,
            tombstone,
            schema,
        }
    }
}

/// Orthogonal, multi-dimensional semantic state of a CSR segment (C2).
///
/// Kept as a struct of independent dimensions rather than one flat enum: a
/// segment can be e.g. topology-`Exact` yet tombstone-`Sensitive` and
/// schema-`OlderEpoch` simultaneously, which a flat enum could not express.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct SegmentSemanticState {
    pub topology: TopologySummaryState,
    pub degree: DegreeSummaryState,
    /// Reuses the catalog completeness enum (Exact / Conservative / Unknown).
    pub property: SemanticSummaryCompleteness,
    pub tombstone: TombstoneState,
    pub schema: SchemaState,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TopologySummaryState {
    /// Summary allows pruning and both (src_label, edge_type) keys are concrete.
    Exact,
    /// Summary allows pruning but a partition key collapsed to a sentinel
    /// (UNKNOWN_SOURCE_LABEL or MIXED_EDGE_TYPE): pruning surface degraded.
    Mixed,
    /// Summary does not allow semantic pruning at all.
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DegreeSummaryState {
    Exact,
    Mixed,
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TombstoneState {
    Clean,
    Sensitive,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SchemaState {
    Current,
    OlderEpoch,
    Uncertain,
}

/// Edge-weighted + segment-count summary of the pruning surface over a set of
/// segments (e.g. the inputs vs the outputs of one compaction). This is the
/// core C2 retention measurement; `exact_surface_ratio` is the headline metric.
#[derive(Debug, Clone, Copy, Default, PartialEq, Serialize, Deserialize)]
pub struct PruningSurfaceSummary {
    pub segment_count: usize,
    pub edge_count: u64,
    pub topology_exact_edges: u64,
    pub topology_mixed_edges: u64,
    pub topology_unknown_edges: u64,
    pub topology_exact_segments: usize,
    pub topology_mixed_segments: usize,
    pub topology_unknown_segments: usize,
    pub degree_exact_segments: usize,
    pub property_exact_segments: usize,
    pub tombstone_sensitive_segments: usize,
    pub older_epoch_segments: usize,
}

impl PruningSurfaceSummary {
    /// Aggregate the semantic state of every segment, edge-weighted by
    /// `edge_count`. Pass the schema epoch the read path would resolve against.
    pub fn from_segments(metas: &[CsrSegmentMeta], current_epoch: SchemaEpoch) -> Self {
        let mut summary = PruningSurfaceSummary::default();
        for meta in metas {
            let state = meta.semantic_state(current_epoch);
            let edges = meta.edge_count;
            summary.segment_count += 1;
            summary.edge_count += edges;
            match state.topology {
                TopologySummaryState::Exact => {
                    summary.topology_exact_edges += edges;
                    summary.topology_exact_segments += 1;
                }
                TopologySummaryState::Mixed => {
                    summary.topology_mixed_edges += edges;
                    summary.topology_mixed_segments += 1;
                }
                TopologySummaryState::Unknown => {
                    summary.topology_unknown_edges += edges;
                    summary.topology_unknown_segments += 1;
                }
            }
            if matches!(state.degree, DegreeSummaryState::Exact) {
                summary.degree_exact_segments += 1;
            }
            if matches!(state.property, SemanticSummaryCompleteness::Exact) {
                summary.property_exact_segments += 1;
            }
            if matches!(state.tombstone, TombstoneState::Sensitive) {
                summary.tombstone_sensitive_segments += 1;
            }
            if matches!(state.schema, SchemaState::OlderEpoch | SchemaState::Uncertain) {
                summary.older_epoch_segments += 1;
            }
        }
        summary
    }

    /// Fraction of edges living in a topology-`Exact` segment (headline metric).
    pub fn exact_surface_ratio(&self) -> f64 {
        if self.edge_count == 0 {
            0.0
        } else {
            self.topology_exact_edges as f64 / self.edge_count as f64
        }
    }

    /// Fraction of edges living in a topology-`Mixed` segment.
    pub fn mixed_ratio(&self) -> f64 {
        if self.edge_count == 0 {
            0.0
        } else {
            self.topology_mixed_edges as f64 / self.edge_count as f64
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SignaturePruningDecision {
    pub pruned: bool,
    pub reason: &'static str,
}

impl SignaturePruningDecision {
    fn pruned(reason: &'static str) -> Self {
        Self {
            pruned: true,
            reason,
        }
    }

    fn kept(reason: &'static str) -> Self {
        Self {
            pruned: false,
            reason,
        }
    }
}

fn default_unknown_source_label() -> i32 {
    UNKNOWN_SOURCE_LABEL
}

fn default_mixed_edge_type() -> EdgeType {
    MIXED_EDGE_TYPE
}

fn default_may_contain_tombstones() -> bool {
    true
}

pub fn property_presence_bit(property_id: PropertyId) -> Option<u64> {
    if property_id < 64 {
        Some(1u64 << property_id)
    } else {
        None
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EdgeOffset {
    pub src: VertexId,
    pub first_edge_idx: u64,
    pub edge_count: u64,
}

impl EdgeOffset {
    pub fn encode(&self, out: &mut Vec<u8>) {
        out.extend_from_slice(&self.src.to_le_bytes());
        out.extend_from_slice(&self.first_edge_idx.to_le_bytes());
        out.extend_from_slice(&self.edge_count.to_le_bytes());
    }

    pub fn decode(buf: &[u8]) -> Self {
        Self {
            src: get_u64(buf, 0),
            first_edge_idx: get_u64(buf, 8),
            edge_count: get_u64(buf, 16),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DiskEdgeBody {
    pub dst: VertexId,
    pub ts: Timestamp,
    pub prop_offset: u64,
    pub edge_type: EdgeType,
    pub marker: EdgeMarker,
}

impl From<EdgeRecord> for DiskEdgeBody {
    fn from(edge: EdgeRecord) -> Self {
        Self {
            dst: edge.dst,
            ts: edge.ts,
            prop_offset: 0,
            edge_type: edge.edge_type,
            marker: edge.marker,
        }
    }
}

impl DiskEdgeBody {
    pub fn to_edge_record(self, src: VertexId) -> EdgeRecord {
        EdgeRecord {
            src,
            dst: self.dst,
            edge_type: self.edge_type,
            ts: self.ts,
            marker: self.marker,
        }
    }

    pub fn encode(&self, out: &mut Vec<u8>) {
        out.extend_from_slice(&self.dst.to_le_bytes());
        out.extend_from_slice(&self.ts.to_le_bytes());
        out.extend_from_slice(&self.prop_offset.to_le_bytes());
        out.extend_from_slice(&self.edge_type.to_le_bytes());
        out.push(self.marker as u8);
        out.extend_from_slice(&[0u8; 3]);
    }

    pub fn decode(buf: &[u8]) -> Self {
        Self {
            dst: get_u64(buf, 0),
            ts: get_u64(buf, 8),
            prop_offset: get_u64(buf, 16),
            edge_type: i32::from_le_bytes(buf[24..28].try_into().unwrap()),
            marker: EdgeMarker::from_u8(buf[28]),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DiskPropertyListHeader {
    pub count: u16,
    pub reserved: u16,
}

impl DiskPropertyListHeader {
    pub fn encode(&self, out: &mut Vec<u8>) {
        out.extend_from_slice(&self.count.to_le_bytes());
        out.extend_from_slice(&self.reserved.to_le_bytes());
    }

    pub fn decode(buf: &[u8]) -> Self {
        Self {
            count: get_u16(buf, 0),
            reserved: get_u16(buf, 2),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DiskPropertyValueIndexEntry {
    pub property_id: PropertyId,
    pub encoding_epoch: SchemaEpoch,
    pub value_offset: u64,
    pub value_len: u32,
    pub flags: u32,
}

impl DiskPropertyValueIndexEntry {
    pub fn encode(&self, out: &mut Vec<u8>) {
        out.extend_from_slice(&self.property_id.to_le_bytes());
        out.extend_from_slice(&self.encoding_epoch.to_le_bytes());
        out.extend_from_slice(&self.value_offset.to_le_bytes());
        out.extend_from_slice(&self.value_len.to_le_bytes());
        out.extend_from_slice(&self.flags.to_le_bytes());
    }

    pub fn decode(buf: &[u8]) -> Self {
        Self {
            property_id: get_u32(buf, 0),
            encoding_epoch: get_u64(buf, 4),
            value_offset: get_u64(buf, 12),
            value_len: get_u32(buf, 20),
            flags: get_u32(buf, 24),
        }
    }
}

fn put_u16(out: &mut [u8], off: usize, v: u16) {
    out[off..off + 2].copy_from_slice(&v.to_le_bytes());
}

fn put_u32(out: &mut [u8], off: usize, v: u32) {
    out[off..off + 4].copy_from_slice(&v.to_le_bytes());
}

fn put_u64(out: &mut [u8], off: usize, v: u64) {
    out[off..off + 8].copy_from_slice(&v.to_le_bytes());
}

fn get_u16(buf: &[u8], off: usize) -> u16 {
    u16::from_le_bytes(buf[off..off + 2].try_into().unwrap())
}

fn get_u32(buf: &[u8], off: usize) -> u32 {
    u32::from_le_bytes(buf[off..off + 4].try_into().unwrap())
}

fn get_u64(buf: &[u8], off: usize) -> u64 {
    u64::from_le_bytes(buf[off..off + 8].try_into().unwrap())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn person_vid(local: u64) -> VertexId {
        (1u64 << 56) | local
    }

    fn semantic_meta(edge_type: EdgeType) -> CsrSegmentMeta {
        CsrSegmentMeta {
            file_id: 1,
            level: 0,
            src_label: 1,
            dst_label: 1,
            schema_epoch: 0,
            summary_completeness: SemanticSummaryCompleteness::Exact,
            property_summary_completeness: SemanticSummaryCompleteness::Exact,
            property_presence_bitmap: 0,
            property_encoding_epoch: 0,
            property_index_offset: 0,
            property_index_len: 0,
            property_values_offset: 0,
            property_values_len: 0,
            source_bloom_offset: 0,
            source_bloom_len: 0,
            source_bloom_bit_count: 0,
            may_contain_tombstones: false,
            edge_type_partition: edge_type,
            direction: EdgeDirection::Out,
            degree_class: DegreeClass::Low,
            degree_class_exact: true,
            sort_key: SegmentSortKey::SrcEdgeDstTs,
            min_src: person_vid(1),
            max_src: person_vid(100),
            min_ts: 10,
            edge_count: 10,
            unique_src_count: 2,
            avg_degree_x100: 500,
            max_degree: 8,
            segment_bytes: 1024,
            max_ts: 20,
        }
    }

    #[test]
    fn semantic_signature_prunes_edge_type_but_keeps_legacy_unknowns() {
        let meta = semantic_meta(1);
        assert!(meta
            .may_contain_signature(&GraphAccessSignature::neighbor_scan(person_vid(7), Some(1))));
        assert!(!meta
            .may_contain_signature(&GraphAccessSignature::neighbor_scan(person_vid(7), Some(2))));

        let mut legacy_meta = meta;
        legacy_meta.direction = EdgeDirection::Unknown;
        legacy_meta.src_label = UNKNOWN_SOURCE_LABEL;
        assert!(legacy_meta
            .may_contain_signature(&GraphAccessSignature::neighbor_scan(person_vid(7), Some(1))));
    }

    #[test]
    fn signature_pruning_decision_reports_semantic_reason() {
        let meta = semantic_meta(1);
        let edge_decision = meta.signature_pruning_decision(&GraphAccessSignature::neighbor_scan(
            person_vid(7),
            Some(2),
        ));
        assert!(edge_decision.pruned);
        assert_eq!(edge_decision.reason, "edge_type");

        let mut src_meta = meta;
        src_meta.src_label = 2;
        let label_decision = src_meta.signature_pruning_decision(
            &GraphAccessSignature::neighbor_scan(person_vid(7), Some(1)),
        );
        assert!(label_decision.pruned);
        assert_eq!(label_decision.reason, "src_label");

        let property_decision = meta.signature_pruning_decision(
            &GraphAccessSignature::neighbor_scan(person_vid(7), Some(1)).with_required_property(2),
        );
        assert!(property_decision.pruned);
        assert_eq!(property_decision.reason, "property_absence");

        let mut tombstone_meta = meta;
        tombstone_meta.may_contain_tombstones = true;
        let tombstone_decision = tombstone_meta.signature_pruning_decision(
            &GraphAccessSignature::neighbor_scan(person_vid(7), Some(1)).with_required_property(2),
        );
        assert!(!tombstone_decision.pruned);
        assert_eq!(tombstone_decision.reason, "schema_tombstone_fallback");
    }

    #[test]
    fn semantic_degree_signature_treats_segment_degree_as_local_bound() {
        let low_meta = semantic_meta(1);
        assert!(low_meta.may_contain_signature(
            &GraphAccessSignature::neighbor_scan(person_vid(7), Some(1))
                .with_degree_class(DegreeClass::High)
        ));

        let mut high_meta = low_meta;
        high_meta.degree_class = DegreeClass::High;
        assert!(!high_meta.may_contain_signature(
            &GraphAccessSignature::neighbor_scan(person_vid(7), Some(1))
                .with_degree_class(DegreeClass::Low)
        ));
    }

    #[test]
    fn legacy_manifest_meta_defaults_to_conservative_schema_summary() {
        let json = r#"{
            "file_id":1,
            "level":0,
            "src_label":1,
            "dst_label":1,
            "edge_type_partition":1,
            "direction":"out",
            "degree_class":"low",
            "degree_class_exact":true,
            "sort_key":"src_edge_dst_ts",
            "min_src":72057594037927937,
            "max_src":72057594037928036,
            "min_ts":10,
            "edge_count":10,
            "unique_src_count":2,
            "avg_degree_x100":500,
            "max_degree":8,
            "segment_bytes":1024,
            "max_ts":20
        }"#;
        let meta: CsrSegmentMeta = serde_json::from_str(json).unwrap();
        assert_eq!(meta.schema_epoch, 0);
        assert_eq!(
            meta.summary_completeness,
            SemanticSummaryCompleteness::Unknown
        );
        assert_eq!(
            meta.property_summary_completeness,
            SemanticSummaryCompleteness::Unknown
        );
        assert_eq!(meta.property_presence_bitmap, 0);
        assert_eq!(meta.property_encoding_epoch, 0);
        assert_eq!(meta.property_index_offset, 0);
        assert_eq!(meta.property_index_len, 0);
        assert_eq!(meta.property_values_offset, 0);
        assert_eq!(meta.property_values_len, 0);
        assert!(!meta.has_property_value_section());
        assert!(meta.may_contain_tombstones);
        assert!(meta
            .may_contain_signature(&GraphAccessSignature::neighbor_scan(person_vid(7), Some(2))));
        assert!(meta.may_contain_property(1));
        assert!(!meta.definitely_lacks_property(1));
    }

    #[test]
    fn exact_property_presence_allows_safe_absence_reasoning() {
        let mut meta = semantic_meta(1);
        meta.property_presence_bitmap =
            property_presence_bit(1).unwrap() | property_presence_bit(3).unwrap();

        assert!(meta.may_contain_property(1));
        assert!(!meta.definitely_lacks_property(1));
        assert!(!meta.may_contain_property(2));
        assert!(meta.definitely_lacks_property(2));

        meta.property_summary_completeness = SemanticSummaryCompleteness::Unknown;
        assert!(meta.may_contain_property(2));
        assert!(!meta.definitely_lacks_property(2));
        assert!(meta.may_contain_property(65));
        assert!(!meta.definitely_lacks_property(65));
    }

    #[test]
    fn property_predicate_pruning_respects_default_and_unknown_summaries() {
        let mut meta = semantic_meta(1);
        let src = person_vid(7);

        assert!(!meta.may_contain_signature(
            &GraphAccessSignature::neighbor_scan(src, Some(1)).with_required_property(2)
        ));
        assert!(meta.may_contain_signature(
            &GraphAccessSignature::neighbor_scan(src, Some(1)).with_absent_or_default_property(2)
        ));

        meta.property_presence_bitmap = property_presence_bit(2).unwrap();
        assert!(meta.may_contain_signature(
            &GraphAccessSignature::neighbor_scan(src, Some(1)).with_required_property(2)
        ));

        meta.property_summary_completeness = SemanticSummaryCompleteness::Unknown;
        meta.property_presence_bitmap = 0;
        assert!(meta.may_contain_signature(
            &GraphAccessSignature::neighbor_scan(src, Some(1)).with_required_property(2)
        ));
        assert!(meta.may_contain_signature(
            &GraphAccessSignature::neighbor_scan(src, Some(1)).with_required_property(65)
        ));
    }

    #[test]
    fn required_property_keeps_exact_absent_tombstone_segments() {
        let mut meta = semantic_meta(1);
        let src = person_vid(7);

        assert!(!meta.may_contain_signature(
            &GraphAccessSignature::neighbor_scan(src, Some(1)).with_required_property(2)
        ));

        meta.may_contain_tombstones = true;
        assert!(meta.may_contain_signature(
            &GraphAccessSignature::neighbor_scan(src, Some(1)).with_required_property(2)
        ));
    }

    #[test]
    fn property_section_metadata_tracks_presence_without_breaking_defaults() {
        let mut meta = semantic_meta(1);
        assert!(!meta.has_property_value_section());

        meta.property_index_offset = 512;
        meta.property_index_len = 64;
        meta.property_values_offset = 576;
        meta.property_values_len = 128;
        assert!(meta.has_property_value_section());
    }

    #[test]
    fn disk_property_list_header_round_trips() {
        let header = DiskPropertyListHeader {
            count: 3,
            reserved: 0,
        };
        let mut bytes = Vec::new();
        header.encode(&mut bytes);

        assert_eq!(bytes.len(), DISK_PROPERTY_LIST_HEADER_LEN);
        assert_eq!(DiskPropertyListHeader::decode(&bytes), header);
    }

    #[test]
    fn disk_property_value_index_entry_round_trips() {
        let entry = DiskPropertyValueIndexEntry {
            property_id: 7,
            encoding_epoch: 42,
            value_offset: 4096,
            value_len: 16,
            flags: 1,
        };
        let mut bytes = Vec::new();
        entry.encode(&mut bytes);

        assert_eq!(bytes.len(), DISK_PROPERTY_VALUE_INDEX_ENTRY_LEN);
        assert_eq!(DiskPropertyValueIndexEntry::decode(&bytes), entry);
    }

    // ---- C2: SegmentSemanticState / PruningSurfaceSummary ----

    #[allow(clippy::too_many_arguments)]
    fn surface_meta(
        src_label: i32,
        edge_type_partition: EdgeType,
        edge_count: u64,
        summary: SemanticSummaryCompleteness,
        degree_class: DegreeClass,
        degree_class_exact: bool,
        may_contain_tombstones: bool,
        schema_epoch: SchemaEpoch,
    ) -> CsrSegmentMeta {
        CsrSegmentMeta {
            file_id: 1,
            level: 0,
            src_label,
            dst_label: UNKNOWN_SOURCE_LABEL,
            schema_epoch,
            summary_completeness: summary,
            property_summary_completeness: SemanticSummaryCompleteness::Exact,
            property_presence_bitmap: 0,
            property_encoding_epoch: 0,
            property_index_offset: 0,
            property_index_len: 0,
            property_values_offset: 0,
            property_values_len: 0,
            source_bloom_offset: 0,
            source_bloom_len: 0,
            source_bloom_bit_count: 0,
            may_contain_tombstones,
            edge_type_partition,
            direction: EdgeDirection::default(),
            degree_class,
            degree_class_exact,
            sort_key: SegmentSortKey::default(),
            min_src: 0,
            max_src: 0,
            min_ts: 0,
            edge_count,
            unique_src_count: 0,
            avg_degree_x100: 0,
            max_degree: 0,
            segment_bytes: 0,
            max_ts: 0,
        }
    }

    #[test]
    fn semantic_state_topology_exact_for_concrete_keys() {
        let m = surface_meta(
            5,
            7,
            100,
            SemanticSummaryCompleteness::Exact,
            DegreeClass::Low,
            true,
            false,
            1,
        );
        assert_eq!(m.semantic_state(1).topology, TopologySummaryState::Exact);
    }

    #[test]
    fn semantic_state_topology_mixed_when_keys_collapse() {
        // After a naive (non-semantic) merge over multiple labels/edge types the
        // writer records UNKNOWN_SOURCE_LABEL / MIXED_EDGE_TYPE while keeping
        // summary_completeness == Exact.
        let mixed_label = surface_meta(
            UNKNOWN_SOURCE_LABEL,
            7,
            100,
            SemanticSummaryCompleteness::Exact,
            DegreeClass::Mixed,
            false,
            false,
            1,
        );
        let mixed_type = surface_meta(
            5,
            MIXED_EDGE_TYPE,
            100,
            SemanticSummaryCompleteness::Exact,
            DegreeClass::Mixed,
            false,
            false,
            1,
        );
        assert_eq!(
            mixed_label.semantic_state(1).topology,
            TopologySummaryState::Mixed
        );
        assert_eq!(
            mixed_type.semantic_state(1).topology,
            TopologySummaryState::Mixed
        );
    }

    #[test]
    fn semantic_state_dimensions() {
        let m = surface_meta(
            5,
            7,
            100,
            SemanticSummaryCompleteness::Unknown,
            DegreeClass::High,
            true,
            true,
            0,
        );
        let s = m.semantic_state(2);
        assert_eq!(s.topology, TopologySummaryState::Unknown);
        assert_eq!(s.degree, DegreeSummaryState::Unknown); // gated by Unknown summary
        assert_eq!(s.tombstone, TombstoneState::Sensitive);
        assert_eq!(s.schema, SchemaState::OlderEpoch);

        let exact_degree = surface_meta(
            5,
            7,
            100,
            SemanticSummaryCompleteness::Exact,
            DegreeClass::High,
            true,
            false,
            3,
        );
        assert_eq!(exact_degree.semantic_state(3).degree, DegreeSummaryState::Exact);
        assert_eq!(exact_degree.semantic_state(3).schema, SchemaState::Current);
    }

    #[test]
    fn pruning_surface_summary_is_edge_weighted() {
        let exact = surface_meta(
            5,
            7,
            100,
            SemanticSummaryCompleteness::Exact,
            DegreeClass::Low,
            true,
            false,
            1,
        );
        let mixed = surface_meta(
            UNKNOWN_SOURCE_LABEL,
            MIXED_EDGE_TYPE,
            900,
            SemanticSummaryCompleteness::Exact,
            DegreeClass::Mixed,
            false,
            false,
            1,
        );
        let s = PruningSurfaceSummary::from_segments(&[exact, mixed], 1);
        assert_eq!(s.segment_count, 2);
        assert_eq!(s.edge_count, 1000);
        assert!((s.exact_surface_ratio() - 0.1).abs() < 1e-9);
        assert!((s.mixed_ratio() - 0.9).abs() < 1e-9);
        assert_eq!(s.topology_exact_segments, 1);
        assert_eq!(s.topology_mixed_segments, 1);
    }

    #[test]
    fn naive_merge_degrades_surface_semantic_merge_rebuilds_it() {
        // before: one big mixed input segment (e.g. produced by a prior naive merge)
        let before = PruningSurfaceSummary::from_segments(
            &[surface_meta(
                UNKNOWN_SOURCE_LABEL,
                MIXED_EDGE_TYPE,
                1000,
                SemanticSummaryCompleteness::Exact,
                DegreeClass::Mixed,
                false,
                false,
                1,
            )],
            1,
        );
        // naive merge output: still a mixed segment
        let naive_after = PruningSurfaceSummary::from_segments(
            &[surface_meta(
                UNKNOWN_SOURCE_LABEL,
                MIXED_EDGE_TYPE,
                1000,
                SemanticSummaryCompleteness::Exact,
                DegreeClass::Mixed,
                false,
                false,
                1,
            )],
            1,
        );
        // semantic merge output: split into concrete (label, edge_type) segments
        let semantic_after = PruningSurfaceSummary::from_segments(
            &[
                surface_meta(
                    5,
                    7,
                    400,
                    SemanticSummaryCompleteness::Exact,
                    DegreeClass::Low,
                    true,
                    false,
                    1,
                ),
                surface_meta(
                    5,
                    8,
                    600,
                    SemanticSummaryCompleteness::Exact,
                    DegreeClass::Low,
                    true,
                    false,
                    1,
                ),
            ],
            1,
        );
        assert!(before.exact_surface_ratio() < 0.01);
        assert!(naive_after.exact_surface_ratio() < 0.01);
        assert!((semantic_after.exact_surface_ratio() - 1.0).abs() < 1e-9);
    }
}
