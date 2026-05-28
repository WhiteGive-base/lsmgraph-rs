use std::path::{Path, PathBuf};
use std::sync::Arc;

use bytes::Bytes;

use crate::csr::format::{
    CsrHeader, CsrSegmentMeta, DiskEdgeBody, EdgeOffset, CSR_HEADER_LEN, CSR_MAGIC, CSR_VERSION,
    DISK_EDGE_BODY_LEN,
};
use crate::error::Result;
use crate::io::IoBackend;
use crate::types::{
    source_label_from_vertex_id, EdgeRecord, EdgeType, FileId, LevelId, MIXED_EDGE_TYPE,
    UNKNOWN_SOURCE_LABEL,
};

pub struct CsrWriter<B: IoBackend> {
    backend: Arc<B>,
    store_dir: PathBuf,
}

impl<B: IoBackend> CsrWriter<B> {
    pub fn new(backend: Arc<B>, store_dir: impl Into<PathBuf>) -> Self {
        Self {
            backend,
            store_dir: store_dir.into(),
        }
    }

    pub async fn write_segment(
        &self,
        level: LevelId,
        file_id: FileId,
        mut edges: Vec<EdgeRecord>,
    ) -> Result<CsrSegmentMeta> {
        edges.sort_by_key(|e| (e.src, e.edge_type, e.dst, e.ts));
        let min_ts = edges.iter().map(|e| e.ts).min().unwrap_or(0);
        let max_ts = edges.iter().map(|e| e.ts).max().unwrap_or(0);
        let min_src = edges.first().map(|e| e.src).unwrap_or(0);
        let max_src = edges.last().map(|e| e.src).unwrap_or(0);
        let unique_src_count = count_unique_sources(&edges);
        let src_label = segment_src_label(&edges);
        let edge_type_partition = segment_edge_type(&edges);

        let mut offsets = Vec::new();
        let mut bodies = Vec::with_capacity(edges.len() * DISK_EDGE_BODY_LEN);
        let mut idx = 0usize;
        while idx < edges.len() {
            let src = edges[idx].src;
            let first = idx as u64;
            let mut count = 0u64;
            while idx < edges.len() && edges[idx].src == src {
                DiskEdgeBody::from(edges[idx]).encode(&mut bodies);
                count += 1;
                idx += 1;
            }
            EdgeOffset {
                src,
                first_edge_idx: first,
                edge_count: count,
            }
            .encode(&mut offsets);
        }

        let offsets_offset = CSR_HEADER_LEN as u64;
        let bodies_offset = offsets_offset + offsets.len() as u64;
        let header = CsrHeader {
            magic: CSR_MAGIC,
            version: CSR_VERSION,
            header_len: CSR_HEADER_LEN as u16,
            level,
            flags: 0,
            file_id,
            create_ts: max_ts,
            min_ts,
            max_ts,
            src_label,
            edge_type_partition,
            min_src,
            max_src,
            edge_offset_count: (offsets.len() / crate::csr::format::EDGE_OFFSET_LEN) as u64,
            edge_body_count: edges.len() as u64,
            offsets_offset,
            offsets_len: offsets.len() as u64,
            bodies_offset,
            bodies_len: bodies.len() as u64,
            checksum: 0,
        };

        let final_path = self.segment_path(level, file_id);
        let tmp_path = final_path.with_extension("edge.tmp");
        let mut bytes = Vec::with_capacity(CSR_HEADER_LEN + offsets.len() + bodies.len());
        bytes.extend_from_slice(&header.encode());
        bytes.extend_from_slice(&offsets);
        bytes.extend_from_slice(&bodies);
        let segment_bytes = bytes.len() as u64;

        self.backend.create(&tmp_path).await?;
        self.backend
            .write_at(&tmp_path, 0, Bytes::from(bytes))
            .await?;
        self.backend.sync(&tmp_path).await?;
        rename_blocking(&tmp_path, &final_path).await?;

        Ok(CsrSegmentMeta {
            file_id,
            level,
            src_label,
            edge_type_partition,
            min_src,
            max_src,
            min_ts,
            edge_count: edges.len() as u64,
            unique_src_count,
            segment_bytes,
            max_ts,
        })
    }

    fn segment_path(&self, level: LevelId, file_id: FileId) -> PathBuf {
        self.store_dir
            .join("levels")
            .join(format!("L{}", level))
            .join(format!("{:012}.edge", file_id))
    }
}

fn count_unique_sources(edges: &[EdgeRecord]) -> u64 {
    let mut count = 0u64;
    let mut last = None;
    for edge in edges {
        if last != Some(edge.src) {
            count += 1;
            last = Some(edge.src);
        }
    }
    count
}

fn segment_src_label(edges: &[EdgeRecord]) -> i32 {
    let mut label = None;
    for edge in edges {
        let current = source_label_from_vertex_id(edge.src);
        match label {
            None => label = Some(current),
            Some(existing) if existing == current => {}
            Some(_) => return UNKNOWN_SOURCE_LABEL,
        }
    }
    label.unwrap_or(UNKNOWN_SOURCE_LABEL)
}

fn segment_edge_type(edges: &[EdgeRecord]) -> EdgeType {
    let mut edge_type = None;
    for edge in edges {
        match edge_type {
            None => edge_type = Some(edge.edge_type),
            Some(existing) if existing == edge.edge_type => {}
            Some(_) => return MIXED_EDGE_TYPE,
        }
    }
    edge_type.unwrap_or(MIXED_EDGE_TYPE)
}

async fn rename_blocking(from: &Path, to: &Path) -> Result<()> {
    let from = from.to_path_buf();
    let to = to.to_path_buf();
    tokio::task::spawn_blocking(move || -> Result<()> {
        if let Some(parent) = to.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::rename(from, to)?;
        Ok(())
    })
    .await??;
    Ok(())
}
