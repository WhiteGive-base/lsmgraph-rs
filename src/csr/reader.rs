use std::path::{Path, PathBuf};
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::Instant;

use crate::csr::cache::{CachedCsrMetadata, CsrMetadataCache};
use crate::csr::format::{
    CsrHeader, CsrSegmentMeta, DiskEdgeBody, EdgeOffset, CSR_HEADER_LEN, DISK_EDGE_BODY_LEN,
    EDGE_OFFSET_LEN,
};
use crate::error::Result;
use crate::io::IoBackend;
use crate::metrics::Metrics;
use crate::types::{EdgeRecord, VertexId};

pub struct CsrReader<B: IoBackend> {
    backend: Arc<B>,
    store_dir: PathBuf,
    metrics: Option<Arc<Metrics>>,
    cache: Option<Arc<CsrMetadataCache>>,
}

impl<B: IoBackend> CsrReader<B> {
    pub fn new(backend: Arc<B>, store_dir: impl Into<PathBuf>) -> Self {
        Self {
            backend,
            store_dir: store_dir.into(),
            metrics: None,
            cache: None,
        }
    }

    pub fn with_metrics(
        backend: Arc<B>,
        store_dir: impl Into<PathBuf>,
        metrics: Arc<Metrics>,
    ) -> Self {
        Self {
            backend,
            store_dir: store_dir.into(),
            metrics: Some(metrics),
            cache: None,
        }
    }

    pub fn with_metrics_and_cache(
        backend: Arc<B>,
        store_dir: impl Into<PathBuf>,
        metrics: Arc<Metrics>,
        cache: Arc<CsrMetadataCache>,
    ) -> Self {
        Self {
            backend,
            store_dir: store_dir.into(),
            metrics: Some(metrics),
            cache: Some(cache),
        }
    }

    pub async fn read_header(&self, meta: &CsrSegmentMeta) -> Result<CsrHeader> {
        Ok(self.metadata_for(meta).await?.header)
    }

    pub fn cached_may_contain_src(&self, meta: &CsrSegmentMeta, src: VertexId) -> Option<bool> {
        self.cache
            .as_ref()
            .and_then(|cache| cache.cached_may_contain_src(meta.file_id, src))
    }

    pub async fn get_neighbors(
        &self,
        meta: &CsrSegmentMeta,
        src: VertexId,
    ) -> Result<Vec<EdgeRecord>> {
        let started = Instant::now();
        if src < meta.min_src || src > meta.max_src || meta.edge_count == 0 {
            self.record_csr_get_neighbors(started);
            return Ok(Vec::new());
        }
        let metadata = self.metadata_for(meta).await?;
        let offset = find_offset_in_offsets(&metadata.offsets, src);
        let Some(offset) = offset else {
            if metadata.may_contain_src(src) {
                if let Some(metrics) = &self.metrics {
                    metrics
                        .l0_bloom_false_positive_probes
                        .fetch_add(1, Ordering::Relaxed);
                }
            } else if let Some(metrics) = &self.metrics {
                metrics
                    .bloom_filtered_segments
                    .fetch_add(1, Ordering::Relaxed);
            }
            self.record_csr_get_neighbors(started);
            return Ok(Vec::new());
        };

        let path = self.path_for(meta);
        let body_offset =
            metadata.header.bodies_offset + offset.first_edge_idx * DISK_EDGE_BODY_LEN as u64;
        let body_len = offset.edge_count as usize * DISK_EDGE_BODY_LEN;
        let bodies = self.backend.read_at(&path, body_offset, body_len).await?;
        self.record_body_read(bodies.len());
        let mut out = Vec::with_capacity(offset.edge_count as usize);
        for chunk in bodies.chunks_exact(DISK_EDGE_BODY_LEN) {
            out.push(DiskEdgeBody::decode(chunk).to_edge_record(src));
        }
        self.record_csr_get_neighbors(started);
        Ok(out)
    }

    pub async fn read_all_edges(&self, meta: &CsrSegmentMeta) -> Result<Vec<EdgeRecord>> {
        let started = Instant::now();
        if meta.edge_count == 0 {
            self.record_csr_read_all_edges(started);
            return Ok(Vec::new());
        }
        let path = self.path_for(meta);
        let metadata = Arc::new(self.read_metadata_from_disk(meta).await?);
        let bodies = self
            .backend
            .read_at(
                &path,
                metadata.header.bodies_offset,
                metadata.header.bodies_len as usize,
            )
            .await?;
        self.record_body_read(bodies.len());
        if let Some(metrics) = &self.metrics {
            metrics.csr_full_scan_reads.fetch_add(1, Ordering::Relaxed);
        }
        let mut out = Vec::with_capacity(metadata.header.edge_body_count as usize);
        for offset in &metadata.offsets {
            let start = offset.first_edge_idx as usize * DISK_EDGE_BODY_LEN;
            let end = start + offset.edge_count as usize * DISK_EDGE_BODY_LEN;
            for body_chunk in bodies[start..end].chunks_exact(DISK_EDGE_BODY_LEN) {
                out.push(DiskEdgeBody::decode(body_chunk).to_edge_record(offset.src));
            }
        }
        self.record_csr_read_all_edges(started);
        Ok(out)
    }

    pub async fn read_offsets(&self, meta: &CsrSegmentMeta) -> Result<Vec<EdgeOffset>> {
        let started = Instant::now();
        let metadata = self.metadata_for(meta).await?;
        self.record_csr_read_offsets(started);
        Ok(metadata.offsets.clone())
    }

    fn path_for(&self, meta: &CsrSegmentMeta) -> PathBuf {
        self.store_dir.join(meta.relative_path())
    }

    async fn metadata_for(&self, meta: &CsrSegmentMeta) -> Result<Arc<CachedCsrMetadata>> {
        if let Some(cache) = &self.cache {
            if let Some(metadata) = cache.get(meta.file_id) {
                if let Some(metrics) = &self.metrics {
                    metrics
                        .csr_offset_cache_hits
                        .fetch_add(1, Ordering::Relaxed);
                }
                return Ok(metadata);
            }
            if let Some(metrics) = &self.metrics {
                metrics
                    .csr_offset_cache_misses
                    .fetch_add(1, Ordering::Relaxed);
            }
            let metadata = self.read_metadata_from_disk(meta).await?;
            return Ok(cache.insert(meta.file_id, metadata));
        }

        Ok(Arc::new(self.read_metadata_from_disk(meta).await?))
    }

    async fn read_metadata_from_disk(&self, meta: &CsrSegmentMeta) -> Result<CachedCsrMetadata> {
        let path = self.path_for(meta);
        let header_bytes = self.backend.read_at(&path, 0, CSR_HEADER_LEN).await?;
        self.record_header_read(header_bytes.len());
        let header = CsrHeader::decode(&header_bytes)?;
        let offsets_bytes = self
            .backend
            .read_at(&path, header.offsets_offset, header.offsets_len as usize)
            .await?;
        self.record_offset_read(offsets_bytes.len());
        let offsets = offsets_bytes
            .chunks_exact(EDGE_OFFSET_LEN)
            .map(EdgeOffset::decode)
            .collect();
        Ok(CachedCsrMetadata::new(header, offsets))
    }

    fn record_header_read(&self, bytes: usize) {
        if let Some(metrics) = &self.metrics {
            metrics.csr_header_reads.fetch_add(1, Ordering::Relaxed);
            metrics
                .csr_header_bytes
                .fetch_add(bytes as u64, Ordering::Relaxed);
        }
    }

    fn record_offset_read(&self, bytes: usize) {
        if let Some(metrics) = &self.metrics {
            metrics.csr_offset_reads.fetch_add(1, Ordering::Relaxed);
            metrics
                .csr_offset_bytes
                .fetch_add(bytes as u64, Ordering::Relaxed);
        }
    }

    fn record_body_read(&self, bytes: usize) {
        if let Some(metrics) = &self.metrics {
            metrics.csr_body_reads.fetch_add(1, Ordering::Relaxed);
            metrics
                .csr_body_bytes
                .fetch_add(bytes as u64, Ordering::Relaxed);
        }
    }

    fn record_csr_get_neighbors(&self, started: Instant) {
        if let Some(metrics) = &self.metrics {
            metrics.csr_get_neighbors_latency.record_since(started);
        }
    }

    fn record_csr_read_all_edges(&self, started: Instant) {
        if let Some(metrics) = &self.metrics {
            metrics.csr_read_all_edges_latency.record_since(started);
        }
    }

    fn record_csr_read_offsets(&self, started: Instant) {
        if let Some(metrics) = &self.metrics {
            metrics.csr_read_offsets_latency.record_since(started);
        }
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

pub fn find_offset_in_offsets(offsets: &[EdgeOffset], src: VertexId) -> Option<EdgeOffset> {
    offsets
        .binary_search_by_key(&src, |offset| offset.src)
        .ok()
        .map(|idx| offsets[idx])
}

#[allow(dead_code)]
fn _assert_path_send_sync(_: &Path) {}
