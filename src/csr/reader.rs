use std::path::{Path, PathBuf};
use std::sync::Arc;

use crate::csr::format::{
    CsrHeader, CsrSegmentMeta, DiskEdgeBody, EdgeOffset, CSR_HEADER_LEN, DISK_EDGE_BODY_LEN,
    EDGE_OFFSET_LEN,
};
use crate::error::Result;
use crate::io::IoBackend;
use crate::types::{EdgeRecord, VertexId};

pub struct CsrReader<B: IoBackend> {
    backend: Arc<B>,
    store_dir: PathBuf,
}

impl<B: IoBackend> CsrReader<B> {
    pub fn new(backend: Arc<B>, store_dir: impl Into<PathBuf>) -> Self {
        Self {
            backend,
            store_dir: store_dir.into(),
        }
    }

    pub async fn read_header(&self, meta: &CsrSegmentMeta) -> Result<CsrHeader> {
        let path = self.path_for(meta);
        let bytes = self.backend.read_at(&path, 0, CSR_HEADER_LEN).await?;
        CsrHeader::decode(&bytes)
    }

    pub async fn get_neighbors(
        &self,
        meta: &CsrSegmentMeta,
        src: VertexId,
    ) -> Result<Vec<EdgeRecord>> {
        if src < meta.min_src || src > meta.max_src || meta.edge_count == 0 {
            return Ok(Vec::new());
        }
        let path = self.path_for(meta);
        let header_bytes = self.backend.read_at(&path, 0, CSR_HEADER_LEN).await?;
        let header = CsrHeader::decode(&header_bytes)?;
        let offsets_bytes = self
            .backend
            .read_at(&path, header.offsets_offset, header.offsets_len as usize)
            .await?;
        let offset = find_offset(&offsets_bytes, src);
        let Some(offset) = offset else {
            return Ok(Vec::new());
        };

        let body_offset = header.bodies_offset + offset.first_edge_idx * DISK_EDGE_BODY_LEN as u64;
        let body_len = offset.edge_count as usize * DISK_EDGE_BODY_LEN;
        let bodies = self.backend.read_at(&path, body_offset, body_len).await?;
        let mut out = Vec::with_capacity(offset.edge_count as usize);
        for chunk in bodies.chunks_exact(DISK_EDGE_BODY_LEN) {
            out.push(DiskEdgeBody::decode(chunk).to_edge_record(src));
        }
        Ok(out)
    }

    pub async fn read_all_edges(&self, meta: &CsrSegmentMeta) -> Result<Vec<EdgeRecord>> {
        if meta.edge_count == 0 {
            return Ok(Vec::new());
        }
        let path = self.path_for(meta);
        let header_bytes = self.backend.read_at(&path, 0, CSR_HEADER_LEN).await?;
        let header = CsrHeader::decode(&header_bytes)?;
        let offsets_bytes = self
            .backend
            .read_at(&path, header.offsets_offset, header.offsets_len as usize)
            .await?;
        let bodies = self
            .backend
            .read_at(&path, header.bodies_offset, header.bodies_len as usize)
            .await?;
        let mut out = Vec::with_capacity(header.edge_body_count as usize);
        for off_chunk in offsets_bytes.chunks_exact(EDGE_OFFSET_LEN) {
            let offset = EdgeOffset::decode(off_chunk);
            let start = offset.first_edge_idx as usize * DISK_EDGE_BODY_LEN;
            let end = start + offset.edge_count as usize * DISK_EDGE_BODY_LEN;
            for body_chunk in bodies[start..end].chunks_exact(DISK_EDGE_BODY_LEN) {
                out.push(DiskEdgeBody::decode(body_chunk).to_edge_record(offset.src));
            }
        }
        Ok(out)
    }

    pub async fn read_offsets(&self, meta: &CsrSegmentMeta) -> Result<Vec<EdgeOffset>> {
        let path = self.path_for(meta);
        let header_bytes = self.backend.read_at(&path, 0, CSR_HEADER_LEN).await?;
        let header = CsrHeader::decode(&header_bytes)?;
        let offsets_bytes = self
            .backend
            .read_at(&path, header.offsets_offset, header.offsets_len as usize)
            .await?;
        Ok(offsets_bytes
            .chunks_exact(EDGE_OFFSET_LEN)
            .map(EdgeOffset::decode)
            .collect())
    }

    fn path_for(&self, meta: &CsrSegmentMeta) -> PathBuf {
        self.store_dir.join(meta.relative_path())
    }
}

pub fn find_offset(offsets_bytes: &[u8], src: VertexId) -> Option<EdgeOffset> {
    let count = offsets_bytes.len() / EDGE_OFFSET_LEN;
    let mut lo = 0usize;
    let mut hi = count;
    while lo < hi {
        let mid = (lo + hi) / 2;
        let chunk = &offsets_bytes[mid * EDGE_OFFSET_LEN..(mid + 1) * EDGE_OFFSET_LEN];
        let offset = EdgeOffset::decode(chunk);
        if offset.src == src {
            return Some(offset);
        }
        if offset.src < src {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }
    None
}

#[allow(dead_code)]
fn _assert_path_send_sync(_: &Path) {}
