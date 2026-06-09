use std::path::{Path, PathBuf};
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::Instant;

use crate::csr::cache::{CachedCsrMetadata, CsrMetadataCache};
use crate::csr::format::{
    CsrHeader, CsrSegmentMeta, DiskEdgeBody, DiskPropertyListHeader, DiskPropertyValueIndexEntry,
    EdgeOffset, CSR_HEADER_LEN, DISK_EDGE_BODY_LEN, DISK_PROPERTY_LIST_HEADER_LEN,
    DISK_PROPERTY_VALUE_INDEX_ENTRY_LEN, EDGE_OFFSET_LEN,
};
use crate::error::Result;
use crate::io::IoBackend;
use crate::metrics::Metrics;
use crate::property_encoding::{PropertyEncodingRegistry, PropertyValue};
use crate::schema::{PropertyId, SchemaEpoch};
use crate::types::{EdgeRecord, VertexId};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CsrEncodedPropertyValue {
    pub property_id: PropertyId,
    pub encoding_epoch: SchemaEpoch,
    pub encoded_value: Vec<u8>,
    pub flags: u32,
}

impl CsrEncodedPropertyValue {
    pub fn decode_with(
        &self,
        registry: &PropertyEncodingRegistry,
    ) -> Result<CsrDecodedPropertyValue> {
        Ok(CsrDecodedPropertyValue {
            property_id: self.property_id,
            encoding_epoch: self.encoding_epoch,
            value: registry.decode(self.property_id, self.encoding_epoch, &self.encoded_value)?,
            flags: self.flags,
        })
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct CsrDecodedPropertyValue {
    pub property_id: PropertyId,
    pub encoding_epoch: SchemaEpoch,
    pub value: PropertyValue,
    pub flags: u32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CsrEdgeRecordWithProperties {
    pub edge: EdgeRecord,
    pub properties: Vec<CsrEncodedPropertyValue>,
}

impl CsrEdgeRecordWithProperties {
    pub fn property_values(
        &self,
        property_id: PropertyId,
    ) -> impl Iterator<Item = &CsrEncodedPropertyValue> {
        self.properties
            .iter()
            .filter(move |property| property.property_id == property_id)
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct CsrEdgeRecordWithDecodedProperties {
    pub edge: EdgeRecord,
    pub properties: Vec<CsrDecodedPropertyValue>,
}

impl CsrEdgeRecordWithDecodedProperties {
    pub fn property_values(
        &self,
        property_id: PropertyId,
    ) -> impl Iterator<Item = &CsrDecodedPropertyValue> {
        self.properties
            .iter()
            .filter(move |property| property.property_id == property_id)
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct CsrPropertyValuePredicate {
    pub property_id: PropertyId,
    pub expected: PropertyValue,
}

impl CsrPropertyValuePredicate {
    pub fn equals(property_id: PropertyId, expected: PropertyValue) -> Self {
        Self {
            property_id,
            expected,
        }
    }

    pub fn matches(&self, property: &CsrDecodedPropertyValue) -> bool {
        property.property_id == self.property_id && property.value == self.expected
    }
}

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

    pub async fn get_neighbors_with_properties(
        &self,
        meta: &CsrSegmentMeta,
        src: VertexId,
    ) -> Result<Vec<CsrEdgeRecordWithProperties>> {
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
        let property_sections = if meta.has_property_value_section() {
            Some(self.read_property_sections(meta, &path).await?)
        } else {
            None
        };

        let mut out = Vec::with_capacity(offset.edge_count as usize);
        for chunk in bodies.chunks_exact(DISK_EDGE_BODY_LEN) {
            let body = DiskEdgeBody::decode(chunk);
            let properties = match &property_sections {
                Some(sections) => decode_property_list(body.prop_offset, sections)?,
                None if body.prop_offset == 0 => Vec::new(),
                None => anyhow::bail!(
                    "CSR edge has prop_offset={} but segment metadata has no property section",
                    body.prop_offset
                ),
            };
            out.push(CsrEdgeRecordWithProperties {
                edge: body.to_edge_record(src),
                properties,
            });
        }
        self.record_csr_get_neighbors(started);
        Ok(out)
    }

    pub async fn get_neighbors_with_decoded_properties(
        &self,
        meta: &CsrSegmentMeta,
        src: VertexId,
        registry: &PropertyEncodingRegistry,
    ) -> Result<Vec<CsrEdgeRecordWithDecodedProperties>> {
        let rows = self.get_neighbors_with_properties(meta, src).await?;
        rows.into_iter()
            .map(|row| {
                let properties = row
                    .properties
                    .iter()
                    .map(|property| property.decode_with(registry))
                    .collect::<Result<Vec<_>>>()?;
                Ok(CsrEdgeRecordWithDecodedProperties {
                    edge: row.edge,
                    properties,
                })
            })
            .collect()
    }

    pub async fn get_neighbors_matching_property_value(
        &self,
        meta: &CsrSegmentMeta,
        src: VertexId,
        registry: &PropertyEncodingRegistry,
        predicate: &CsrPropertyValuePredicate,
    ) -> Result<Vec<CsrEdgeRecordWithDecodedProperties>> {
        let rows = self
            .get_neighbors_with_decoded_properties(meta, src, registry)
            .await?;
        Ok(rows
            .into_iter()
            .filter(|row| {
                row.properties
                    .iter()
                    .any(|property| predicate.matches(property))
            })
            .collect())
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

    pub async fn read_all_edges_with_properties(
        &self,
        meta: &CsrSegmentMeta,
    ) -> Result<Vec<CsrEdgeRecordWithProperties>> {
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
        let property_sections = if meta.has_property_value_section() {
            Some(self.read_property_sections(meta, &path).await?)
        } else {
            None
        };
        if let Some(metrics) = &self.metrics {
            metrics.csr_full_scan_reads.fetch_add(1, Ordering::Relaxed);
        }
        let mut out = Vec::with_capacity(metadata.header.edge_body_count as usize);
        for offset in &metadata.offsets {
            let start = offset.first_edge_idx as usize * DISK_EDGE_BODY_LEN;
            let end = start + offset.edge_count as usize * DISK_EDGE_BODY_LEN;
            for body_chunk in bodies[start..end].chunks_exact(DISK_EDGE_BODY_LEN) {
                let body = DiskEdgeBody::decode(body_chunk);
                let properties = match &property_sections {
                    Some(sections) => decode_property_list(body.prop_offset, sections)?,
                    None if body.prop_offset == 0 => Vec::new(),
                    None => anyhow::bail!(
                        "CSR edge has prop_offset={} but segment metadata has no property section",
                        body.prop_offset
                    ),
                };
                out.push(CsrEdgeRecordWithProperties {
                    edge: body.to_edge_record(offset.src),
                    properties,
                });
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

    async fn read_property_sections(
        &self,
        meta: &CsrSegmentMeta,
        path: &Path,
    ) -> Result<CsrPropertySections> {
        if meta.property_index_offset == 0 {
            anyhow::bail!("CSR property index length is nonzero but index offset is zero");
        }
        let index_len = checked_usize(meta.property_index_len, "property_index_len")?;
        let values_len = checked_usize(meta.property_values_len, "property_values_len")?;
        let index = self
            .backend
            .read_at(path, meta.property_index_offset, index_len)
            .await?;
        if index.len() != index_len {
            anyhow::bail!(
                "CSR property index read was short: expected {} bytes, got {}",
                index_len,
                index.len()
            );
        }
        let values = if values_len == 0 {
            bytes::Bytes::new()
        } else {
            if meta.property_values_offset == 0 {
                anyhow::bail!("CSR property values length is nonzero but values offset is zero");
            }
            let values = self
                .backend
                .read_at(path, meta.property_values_offset, values_len)
                .await?;
            if values.len() != values_len {
                anyhow::bail!(
                    "CSR property values read was short: expected {} bytes, got {}",
                    values_len,
                    values.len()
                );
            }
            values
        };
        Ok(CsrPropertySections { index, values })
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

struct CsrPropertySections {
    index: bytes::Bytes,
    values: bytes::Bytes,
}

fn decode_property_list(
    prop_offset: u64,
    sections: &CsrPropertySections,
) -> Result<Vec<CsrEncodedPropertyValue>> {
    if prop_offset == 0 {
        return Ok(Vec::new());
    }
    let relative_offset = prop_offset.checked_sub(1).expect("prop_offset is nonzero");
    let list_start = checked_usize(relative_offset, "prop_offset")?;
    let header_range = checked_range(
        list_start,
        DISK_PROPERTY_LIST_HEADER_LEN,
        sections.index.len(),
        "property list header",
    )?;
    let header = DiskPropertyListHeader::decode(&sections.index[header_range]);
    let entry_count = usize::from(header.count);
    let entries_len = entry_count
        .checked_mul(DISK_PROPERTY_VALUE_INDEX_ENTRY_LEN)
        .ok_or_else(|| anyhow::anyhow!("CSR property list entry byte length overflow"))?;
    let entries_start = list_start
        .checked_add(DISK_PROPERTY_LIST_HEADER_LEN)
        .ok_or_else(|| anyhow::anyhow!("CSR property list entry start overflow"))?;
    let entries_range = checked_range(
        entries_start,
        entries_len,
        sections.index.len(),
        "property list entries",
    )?;

    let mut properties = Vec::with_capacity(entry_count);
    for entry_chunk in
        sections.index[entries_range].chunks_exact(DISK_PROPERTY_VALUE_INDEX_ENTRY_LEN)
    {
        let entry = DiskPropertyValueIndexEntry::decode(entry_chunk);
        let value_start = checked_usize(entry.value_offset, "property value offset")?;
        let value_range = checked_range(
            value_start,
            entry.value_len as usize,
            sections.values.len(),
            "property value bytes",
        )?;
        properties.push(CsrEncodedPropertyValue {
            property_id: entry.property_id,
            encoding_epoch: entry.encoding_epoch,
            encoded_value: sections.values[value_range].to_vec(),
            flags: entry.flags,
        });
    }
    Ok(properties)
}

fn checked_usize(value: u64, name: &str) -> Result<usize> {
    value
        .try_into()
        .map_err(|_| anyhow::anyhow!("{name} does not fit in usize: {value}"))
}

fn checked_range(
    start: usize,
    len: usize,
    total_len: usize,
    context: &str,
) -> Result<std::ops::Range<usize>> {
    let end = start
        .checked_add(len)
        .ok_or_else(|| anyhow::anyhow!("CSR {context} range overflow"))?;
    if end > total_len {
        anyhow::bail!(
            "CSR {context} range {}..{} exceeds section length {}",
            start,
            end,
            total_len
        );
    }
    Ok(start..end)
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

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use super::*;
    use crate::csr::writer::{
        CsrWriter, EdgePropertyValue, EdgeRecordWithProperties as WriterEdgeRecordWithProperties,
    };
    use crate::io::BlockingPreadBackend;
    use crate::metrics::Metrics;
    use crate::property_encoding::{
        PropertyEncodingRegistry, PropertyEncodingSpec, PropertyPhysicalEncoding, PropertyValue,
    };

    fn person_vid(local: u64) -> VertexId {
        (1u64 << 56) | local
    }

    fn backend() -> Arc<BlockingPreadBackend> {
        Arc::new(BlockingPreadBackend::new(8, Arc::new(Metrics::default())))
    }

    #[tokio::test]
    async fn get_neighbors_with_properties_reads_writer_property_sections() -> Result<()> {
        let tmp = tempfile::tempdir()?;
        let backend = backend();
        let writer = CsrWriter::new(backend.clone(), tmp.path(), 77);
        let src = person_vid(1);
        let dst_a = person_vid(2);
        let dst_b = person_vid(3);

        let meta = writer
            .write_segment_with_properties(
                0,
                21,
                vec![
                    WriterEdgeRecordWithProperties {
                        edge: EdgeRecord::insert(src, dst_b, 2, 20),
                        properties: Vec::new(),
                    },
                    WriterEdgeRecordWithProperties {
                        edge: EdgeRecord::insert(src, dst_a, 2, 10),
                        properties: vec![
                            EdgePropertyValue::encoded(5, 42, [1u8, 2, 3]),
                            EdgePropertyValue {
                                property_id: 7,
                                encoding_epoch: 41,
                                encoded_value: vec![4, 5],
                                flags: 2,
                            },
                        ],
                    },
                ],
            )
            .await?;

        let reader = CsrReader::new(backend, tmp.path());
        let rows = reader.get_neighbors_with_properties(&meta, src).await?;
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].edge.dst, dst_a);
        assert_eq!(rows[0].properties.len(), 2);
        let prop5 = rows[0]
            .property_values(5)
            .next()
            .expect("property 5 should be present");
        assert_eq!(prop5.encoding_epoch, 42);
        assert_eq!(prop5.encoded_value, vec![1, 2, 3]);
        assert_eq!(prop5.flags, 0);
        let prop7 = rows[0]
            .property_values(7)
            .next()
            .expect("property 7 should be present");
        assert_eq!(prop7.encoding_epoch, 41);
        assert_eq!(prop7.encoded_value, vec![4, 5]);
        assert_eq!(prop7.flags, 2);

        assert_eq!(rows[1].edge.dst, dst_b);
        assert!(rows[1].properties.is_empty());
        Ok(())
    }

    #[tokio::test]
    async fn get_neighbors_with_properties_keeps_topology_only_segments_empty() -> Result<()> {
        let tmp = tempfile::tempdir()?;
        let backend = backend();
        let writer = CsrWriter::new(backend.clone(), tmp.path(), 12);
        let src = person_vid(1);
        let dst = person_vid(2);
        let meta = writer
            .write_segment(0, 22, vec![EdgeRecord::insert(src, dst, 2, 10)])
            .await?;
        assert!(!meta.has_property_value_section());

        let reader = CsrReader::new(backend, tmp.path());
        let rows = reader.get_neighbors_with_properties(&meta, src).await?;
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].edge.dst, dst);
        assert!(rows[0].properties.is_empty());
        Ok(())
    }

    #[tokio::test]
    async fn get_neighbors_with_decoded_properties_dispatches_by_entry_epoch() -> Result<()> {
        let tmp = tempfile::tempdir()?;
        let backend = backend();
        let writer = CsrWriter::new(backend.clone(), tmp.path(), 88);
        let src = person_vid(1);
        let dst = person_vid(2);
        let mut registry = PropertyEncodingRegistry::new();
        registry.register(PropertyEncodingSpec {
            property_id: 5,
            encoding_epoch: 1,
            physical_encoding: PropertyPhysicalEncoding::PlainI64,
            encoding_version: 1,
        });
        registry.register(PropertyEncodingSpec {
            property_id: 5,
            encoding_epoch: 2,
            physical_encoding: PropertyPhysicalEncoding::PlainF64,
            encoding_version: 2,
        });

        let meta = writer
            .write_segment_with_properties(
                0,
                23,
                vec![WriterEdgeRecordWithProperties {
                    edge: EdgeRecord::insert(src, dst, 2, 10),
                    properties: vec![
                        EdgePropertyValue::encoded(5, 1, 42_i64.to_le_bytes()),
                        EdgePropertyValue::encoded(5, 2, 42.5_f64.to_le_bytes()),
                    ],
                }],
            )
            .await?;

        let reader = CsrReader::new(backend, tmp.path());
        let rows = reader
            .get_neighbors_with_decoded_properties(&meta, src, &registry)
            .await?;
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].edge.dst, dst);
        let values: Vec<_> = rows[0].property_values(5).collect();
        assert_eq!(values.len(), 2);
        assert_eq!(values[0].encoding_epoch, 1);
        assert_eq!(values[0].value, PropertyValue::I64(42));
        assert_eq!(values[1].encoding_epoch, 2);
        assert_eq!(values[1].value, PropertyValue::F64(42.5));
        Ok(())
    }

    #[tokio::test]
    async fn get_neighbors_with_decoded_properties_rejects_missing_decoder() -> Result<()> {
        let tmp = tempfile::tempdir()?;
        let backend = backend();
        let writer = CsrWriter::new(backend.clone(), tmp.path(), 88);
        let src = person_vid(1);
        let dst = person_vid(2);
        let meta = writer
            .write_segment_with_properties(
                0,
                24,
                vec![WriterEdgeRecordWithProperties {
                    edge: EdgeRecord::insert(src, dst, 2, 10),
                    properties: vec![EdgePropertyValue::encoded(5, 99, 42_i64.to_le_bytes())],
                }],
            )
            .await?;

        let reader = CsrReader::new(backend, tmp.path());
        let err = reader
            .get_neighbors_with_decoded_properties(&meta, src, &PropertyEncodingRegistry::new())
            .await
            .expect_err("missing decoder should fail");
        assert!(
            err.to_string().contains("missing decoder"),
            "unexpected error: {err:#}"
        );
        Ok(())
    }

    #[tokio::test]
    async fn get_neighbors_matching_property_value_filters_decoded_rows() -> Result<()> {
        let tmp = tempfile::tempdir()?;
        let backend = backend();
        let writer = CsrWriter::new(backend.clone(), tmp.path(), 88);
        let src = person_vid(1);
        let dst_a = person_vid(2);
        let dst_b = person_vid(3);
        let mut registry = PropertyEncodingRegistry::new();
        registry.register(PropertyEncodingSpec {
            property_id: 5,
            encoding_epoch: 1,
            physical_encoding: PropertyPhysicalEncoding::PlainI64,
            encoding_version: 1,
        });

        let meta = writer
            .write_segment_with_properties(
                0,
                25,
                vec![
                    WriterEdgeRecordWithProperties {
                        edge: EdgeRecord::insert(src, dst_b, 2, 20),
                        properties: vec![EdgePropertyValue::encoded(5, 1, 7_i64.to_le_bytes())],
                    },
                    WriterEdgeRecordWithProperties {
                        edge: EdgeRecord::insert(src, dst_a, 2, 10),
                        properties: vec![EdgePropertyValue::encoded(5, 1, 42_i64.to_le_bytes())],
                    },
                ],
            )
            .await?;

        let reader = CsrReader::new(backend, tmp.path());
        let matching = reader
            .get_neighbors_matching_property_value(
                &meta,
                src,
                &registry,
                &CsrPropertyValuePredicate::equals(5, PropertyValue::I64(42)),
            )
            .await?;
        assert_eq!(matching.len(), 1);
        assert_eq!(matching[0].edge.dst, dst_a);
        assert_eq!(
            matching[0]
                .property_values(5)
                .next()
                .map(|value| &value.value),
            Some(&PropertyValue::I64(42))
        );

        let absent = reader
            .get_neighbors_matching_property_value(
                &meta,
                src,
                &registry,
                &CsrPropertyValuePredicate::equals(5, PropertyValue::I64(100)),
            )
            .await?;
        assert!(absent.is_empty());
        Ok(())
    }
}
