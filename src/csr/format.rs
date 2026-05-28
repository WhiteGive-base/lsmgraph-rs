use std::path::PathBuf;

use serde::{Deserialize, Serialize};

use crate::types::{
    source_label_from_vertex_id, EdgeMarker, EdgeRecord, EdgeType, FileId, LevelId, Timestamp,
    VertexId, MIXED_EDGE_TYPE, UNKNOWN_SOURCE_LABEL,
};

pub const CSR_MAGIC: u32 = 0x4753_4d4c;
pub const CSR_VERSION: u16 = 1;
pub const CSR_HEADER_LEN: usize = 128;
pub const EDGE_OFFSET_LEN: usize = 24;
pub const DISK_EDGE_BODY_LEN: usize = 32;

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
    #[serde(default = "default_mixed_edge_type")]
    pub edge_type_partition: EdgeType,
    pub min_src: VertexId,
    pub max_src: VertexId,
    #[serde(default)]
    pub min_ts: Timestamp,
    pub edge_count: u64,
    #[serde(default)]
    pub unique_src_count: u64,
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
        let src_label = source_label_from_vertex_id(src);
        let label_matches = self.src_label == UNKNOWN_SOURCE_LABEL
            || src_label == UNKNOWN_SOURCE_LABEL
            || self.src_label == src_label;
        let edge_matches = match edge_type {
            Some(edge_type) => {
                self.edge_type_partition == MIXED_EDGE_TYPE || self.edge_type_partition == edge_type
            }
            None => true,
        };
        label_matches && edge_matches
    }
}

fn default_unknown_source_label() -> i32 {
    UNKNOWN_SOURCE_LABEL
}

fn default_mixed_edge_type() -> EdgeType {
    MIXED_EDGE_TYPE
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
