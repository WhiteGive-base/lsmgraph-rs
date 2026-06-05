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

    pub fn may_contain_signature(&self, signature: &GraphAccessSignature) -> bool {
        let time_matches = !signature
            .min_ts
            .map(|min_ts| self.max_ts < min_ts)
            .unwrap_or(false)
            && !signature
                .max_ts
                .map(|max_ts| self.min_ts > max_ts)
                .unwrap_or(false);
        if !time_matches {
            return false;
        }
        if !self.summary_completeness.allows_semantic_pruning() {
            return true;
        }

        let label_matches = signature.label_matches(self.src_label);
        let edge_matches = match signature.edge_type {
            Some(edge_type) => {
                self.edge_type_partition == MIXED_EDGE_TYPE || self.edge_type_partition == edge_type
            }
            None => true,
        };
        let direction_matches = signature.direction_matches(self.direction);
        let degree_matches = match signature.degree_class {
            Some(degree_class) if self.degree_class_exact => {
                self.degree_class.may_contain_global_query(degree_class)
            }
            _ => true,
        };
        let dst_matches = match signature.dst_label {
            Some(dst_label) => {
                self.dst_label == UNKNOWN_SOURCE_LABEL || self.dst_label == dst_label
            }
            None => true,
        };
        let property_matches = match signature.property_predicate {
            Some(predicate) => self.may_satisfy_property_predicate(predicate),
            None => true,
        };
        label_matches
            && edge_matches
            && direction_matches
            && degree_matches
            && dst_matches
            && property_matches
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
}
