use std::path::{Path, PathBuf};
use std::sync::Arc;

use bytes::Bytes;

use crate::csr::format::{
    property_presence_bit, CsrHeader, CsrSegmentMeta, DiskEdgeBody, DiskPropertyListHeader,
    DiskPropertyValueIndexEntry, EdgeOffset, CSR_HEADER_LEN, CSR_MAGIC, CSR_VERSION,
    DISK_EDGE_BODY_LEN,
};
use crate::error::Result;
use crate::io::IoBackend;
use crate::schema::{PropertyId, SchemaEpoch, SemanticSummaryCompleteness};
use crate::semantic::{DegreeClass, EdgeDirection, SegmentSortKey};
use crate::types::{
    source_label_from_vertex_id, EdgeMarker, EdgeRecord, EdgeType, FileId, LevelId,
    MIXED_EDGE_TYPE, UNKNOWN_SOURCE_LABEL,
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EdgePropertyValue {
    pub property_id: PropertyId,
    pub encoding_epoch: SchemaEpoch,
    pub encoded_value: Vec<u8>,
    pub flags: u32,
}

impl EdgePropertyValue {
    pub fn encoded(
        property_id: PropertyId,
        encoding_epoch: SchemaEpoch,
        encoded_value: impl Into<Vec<u8>>,
    ) -> Self {
        Self {
            property_id,
            encoding_epoch,
            encoded_value: encoded_value.into(),
            flags: 0,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EdgeRecordWithProperties {
    pub edge: EdgeRecord,
    pub properties: Vec<EdgePropertyValue>,
}

impl EdgeRecordWithProperties {
    pub fn topology_only(edge: EdgeRecord) -> Self {
        Self {
            edge,
            properties: Vec::new(),
        }
    }
}

#[derive(Debug, Clone, Copy, Default)]
pub struct CsrSegmentSemanticOverrides {
    pub src_label: Option<i32>,
    pub dst_label: Option<i32>,
    pub edge_type_partition: Option<EdgeType>,
    pub degree_class: Option<DegreeClass>,
    pub degree_class_exact: Option<bool>,
}

pub struct CsrWriter<B: IoBackend> {
    backend: Arc<B>,
    store_dir: PathBuf,
    schema_epoch: SchemaEpoch,
}

impl<B: IoBackend> CsrWriter<B> {
    pub fn new(backend: Arc<B>, store_dir: impl Into<PathBuf>, schema_epoch: SchemaEpoch) -> Self {
        Self {
            backend,
            store_dir: store_dir.into(),
            schema_epoch,
        }
    }

    pub async fn write_segment(
        &self,
        level: LevelId,
        file_id: FileId,
        edges: Vec<EdgeRecord>,
    ) -> Result<CsrSegmentMeta> {
        let records = edges
            .into_iter()
            .map(EdgeRecordWithProperties::topology_only)
            .collect();
        self.write_property_records_with_semantic_overrides(
            level,
            file_id,
            records,
            CsrSegmentSemanticOverrides::default(),
        )
        .await
    }

    pub async fn write_segment_with_edge_type_partition(
        &self,
        level: LevelId,
        file_id: FileId,
        edges: Vec<EdgeRecord>,
        edge_type_partition_override: Option<EdgeType>,
    ) -> Result<CsrSegmentMeta> {
        let records = edges
            .into_iter()
            .map(EdgeRecordWithProperties::topology_only)
            .collect();
        self.write_property_records_with_semantic_overrides(
            level,
            file_id,
            records,
            CsrSegmentSemanticOverrides {
                edge_type_partition: edge_type_partition_override,
                ..CsrSegmentSemanticOverrides::default()
            },
        )
        .await
    }

    pub async fn write_segment_with_properties(
        &self,
        level: LevelId,
        file_id: FileId,
        records: Vec<EdgeRecordWithProperties>,
    ) -> Result<CsrSegmentMeta> {
        self.write_property_records_with_semantic_overrides(
            level,
            file_id,
            records,
            CsrSegmentSemanticOverrides::default(),
        )
        .await
    }

    pub async fn write_segment_with_properties_and_edge_type_partition(
        &self,
        level: LevelId,
        file_id: FileId,
        records: Vec<EdgeRecordWithProperties>,
        edge_type_partition_override: Option<EdgeType>,
    ) -> Result<CsrSegmentMeta> {
        self.write_property_records_with_semantic_overrides(
            level,
            file_id,
            records,
            CsrSegmentSemanticOverrides {
                edge_type_partition: edge_type_partition_override,
                ..CsrSegmentSemanticOverrides::default()
            },
        )
        .await
    }

    pub async fn write_segment_with_properties_and_semantic_overrides(
        &self,
        level: LevelId,
        file_id: FileId,
        records: Vec<EdgeRecordWithProperties>,
        semantic_overrides: CsrSegmentSemanticOverrides,
    ) -> Result<CsrSegmentMeta> {
        self.write_property_records_with_semantic_overrides(
            level,
            file_id,
            records,
            semantic_overrides,
        )
        .await
    }

    async fn write_property_records_with_semantic_overrides(
        &self,
        level: LevelId,
        file_id: FileId,
        mut records: Vec<EdgeRecordWithProperties>,
        semantic_overrides: CsrSegmentSemanticOverrides,
    ) -> Result<CsrSegmentMeta> {
        records.sort_by_key(|record| {
            (
                record.edge.src,
                record.edge.edge_type,
                record.edge.dst,
                record.edge.ts,
            )
        });
        let edges: Vec<EdgeRecord> = records.iter().map(|record| record.edge).collect();
        let min_ts = edges.iter().map(|e| e.ts).min().unwrap_or(0);
        let max_ts = edges.iter().map(|e| e.ts).max().unwrap_or(0);
        let min_src = edges.first().map(|e| e.src).unwrap_or(0);
        let max_src = edges.last().map(|e| e.src).unwrap_or(0);
        let degree_stats = source_degree_stats(&edges);
        let unique_src_count = degree_stats.unique_src_count;
        let src_label = semantic_overrides
            .src_label
            .unwrap_or_else(|| segment_src_label(&edges));
        let dst_label = semantic_overrides
            .dst_label
            .unwrap_or_else(|| segment_dst_label(&edges));
        let edge_type_partition = semantic_overrides
            .edge_type_partition
            .unwrap_or_else(|| segment_edge_type(&edges));

        let mut offsets = Vec::new();
        let mut bodies = Vec::with_capacity(edges.len() * DISK_EDGE_BODY_LEN);
        let mut property_index = Vec::new();
        let mut property_values = Vec::new();
        let mut property_presence_bitmap = 0u64;
        let mut all_property_ids_representable = true;
        let mut min_property_encoding_epoch = None;
        let mut idx = 0usize;
        while idx < records.len() {
            let src = records[idx].edge.src;
            let first = idx as u64;
            let mut count = 0u64;
            while idx < records.len() && records[idx].edge.src == src {
                let record = &records[idx];
                let mut body = DiskEdgeBody::from(record.edge);
                if !record.properties.is_empty() {
                    body.prop_offset = encode_property_list(
                        &record.properties,
                        &mut property_index,
                        &mut property_values,
                        &mut property_presence_bitmap,
                        &mut all_property_ids_representable,
                        &mut min_property_encoding_epoch,
                    )?;
                }
                body.encode(&mut bodies);
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
        let property_index_offset = if property_index.is_empty() {
            0
        } else {
            bodies_offset + bodies.len() as u64
        };
        let property_values_offset = if property_index.is_empty() {
            0
        } else {
            property_index_offset + property_index.len() as u64
        };
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
        let mut bytes = Vec::with_capacity(
            CSR_HEADER_LEN
                + offsets.len()
                + bodies.len()
                + property_index.len()
                + property_values.len(),
        );
        bytes.extend_from_slice(&header.encode());
        bytes.extend_from_slice(&offsets);
        bytes.extend_from_slice(&bodies);
        bytes.extend_from_slice(&property_index);
        bytes.extend_from_slice(&property_values);
        let segment_bytes = bytes.len() as u64;
        let property_summary_completeness = if all_property_ids_representable {
            SemanticSummaryCompleteness::Exact
        } else {
            SemanticSummaryCompleteness::Conservative
        };
        let property_encoding_epoch = min_property_encoding_epoch.unwrap_or(self.schema_epoch);

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
            dst_label,
            schema_epoch: self.schema_epoch,
            summary_completeness: SemanticSummaryCompleteness::Exact,
            property_summary_completeness,
            property_presence_bitmap,
            property_encoding_epoch,
            property_index_offset,
            property_index_len: property_index.len() as u64,
            property_values_offset,
            property_values_len: property_values.len() as u64,
            may_contain_tombstones: edges.iter().any(|edge| edge.marker == EdgeMarker::Delete),
            edge_type_partition,
            direction: EdgeDirection::Out,
            degree_class: semantic_overrides
                .degree_class
                .unwrap_or(degree_stats.degree_class),
            degree_class_exact: semantic_overrides
                .degree_class_exact
                .unwrap_or(degree_stats.degree_class_exact),
            sort_key: SegmentSortKey::SrcEdgeDstTs,
            min_src,
            max_src,
            min_ts,
            edge_count: edges.len() as u64,
            unique_src_count,
            avg_degree_x100: degree_stats.avg_degree_x100,
            max_degree: degree_stats.max_degree,
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

fn encode_property_list(
    properties: &[EdgePropertyValue],
    property_index: &mut Vec<u8>,
    property_values: &mut Vec<u8>,
    property_presence_bitmap: &mut u64,
    all_property_ids_representable: &mut bool,
    min_property_encoding_epoch: &mut Option<SchemaEpoch>,
) -> Result<u64> {
    if properties.len() > u16::MAX as usize {
        anyhow::bail!(
            "edge property list has {} entries, max supported is {}",
            properties.len(),
            u16::MAX
        );
    }
    let prop_offset = property_index.len() as u64 + 1;
    DiskPropertyListHeader {
        count: properties.len() as u16,
        reserved: 0,
    }
    .encode(property_index);

    let mut sorted_properties: Vec<&EdgePropertyValue> = properties.iter().collect();
    sorted_properties.sort_by_key(|property| (property.property_id, property.encoding_epoch));
    for property in sorted_properties {
        let value_len = property.encoded_value.len();
        if value_len > u32::MAX as usize {
            anyhow::bail!(
                "encoded property value for property_id={} is {} bytes, max supported is {}",
                property.property_id,
                value_len,
                u32::MAX
            );
        }
        let value_offset = property_values.len() as u64;
        DiskPropertyValueIndexEntry {
            property_id: property.property_id,
            encoding_epoch: property.encoding_epoch,
            value_offset,
            value_len: value_len as u32,
            flags: property.flags,
        }
        .encode(property_index);
        property_values.extend_from_slice(&property.encoded_value);

        if let Some(bit) = property_presence_bit(property.property_id) {
            *property_presence_bitmap |= bit;
        } else {
            *all_property_ids_representable = false;
        }
        *min_property_encoding_epoch = Some(
            min_property_encoding_epoch
                .map(|epoch| epoch.min(property.encoding_epoch))
                .unwrap_or(property.encoding_epoch),
        );
    }
    Ok(prop_offset)
}

struct SourceDegreeStats {
    unique_src_count: u64,
    avg_degree_x100: u64,
    max_degree: u64,
    degree_class: DegreeClass,
    degree_class_exact: bool,
}

fn source_degree_stats(edges: &[EdgeRecord]) -> SourceDegreeStats {
    let mut unique_src_count = 0u64;
    let mut max_degree = 0u64;
    let mut class = None;
    let mut exact = true;
    let mut idx = 0usize;
    while idx < edges.len() {
        let src = edges[idx].src;
        let mut degree = 0u64;
        while idx < edges.len() && edges[idx].src == src {
            degree += 1;
            idx += 1;
        }
        unique_src_count += 1;
        max_degree = max_degree.max(degree);
        let degree_class = DegreeClass::from_max_degree(degree);
        match class {
            None => class = Some(degree_class),
            Some(existing) if existing == degree_class => {}
            Some(_) => exact = false,
        }
    }
    let avg_degree_x100 = if unique_src_count == 0 {
        0
    } else {
        (edges.len() as u64).saturating_mul(100) / unique_src_count
    };
    SourceDegreeStats {
        unique_src_count,
        avg_degree_x100,
        max_degree,
        degree_class: if exact {
            class.unwrap_or(DegreeClass::Unknown)
        } else {
            DegreeClass::Mixed
        },
        degree_class_exact: exact,
    }
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

fn segment_dst_label(edges: &[EdgeRecord]) -> i32 {
    let mut label = None;
    for edge in edges {
        let current = source_label_from_vertex_id(edge.dst);
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

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use super::*;
    use crate::csr::format::{
        property_presence_bit, CsrHeader, DiskPropertyListHeader, DiskPropertyValueIndexEntry,
        DISK_EDGE_BODY_LEN, DISK_PROPERTY_LIST_HEADER_LEN, DISK_PROPERTY_VALUE_INDEX_ENTRY_LEN,
    };
    use crate::csr::reader::CsrReader;
    use crate::io::BlockingPreadBackend;
    use crate::metrics::Metrics;

    fn person_vid(local: u64) -> u64 {
        (1u64 << 56) | local
    }

    fn backend() -> Arc<BlockingPreadBackend> {
        Arc::new(BlockingPreadBackend::new(8, Arc::new(Metrics::default())))
    }

    #[tokio::test]
    async fn property_aware_writer_appends_index_and_value_sections() -> Result<()> {
        let tmp = tempfile::tempdir()?;
        let backend = backend();
        let writer = CsrWriter::new(backend.clone(), tmp.path(), 99);
        let src = person_vid(1);
        let dst_a = person_vid(2);
        let dst_b = person_vid(3);

        let meta = writer
            .write_segment_with_properties(
                0,
                7,
                vec![
                    EdgeRecordWithProperties {
                        edge: EdgeRecord::insert(src, dst_b, 2, 20),
                        properties: Vec::new(),
                    },
                    EdgeRecordWithProperties {
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

        assert!(meta.has_property_value_section());
        assert_eq!(
            meta.property_presence_bitmap,
            property_presence_bit(5).unwrap() | property_presence_bit(7).unwrap()
        );
        assert_eq!(
            meta.property_summary_completeness,
            SemanticSummaryCompleteness::Exact
        );
        assert_eq!(meta.property_encoding_epoch, 41);
        assert_eq!(
            meta.property_index_len,
            (DISK_PROPERTY_LIST_HEADER_LEN + 2 * DISK_PROPERTY_VALUE_INDEX_ENTRY_LEN) as u64
        );
        assert_eq!(meta.property_values_len, 5);
        assert_eq!(
            meta.property_values_offset,
            meta.property_index_offset + meta.property_index_len
        );

        let path = tmp.path().join(meta.relative_path());
        let bytes = std::fs::read(&path)?;
        assert_eq!(bytes.len() as u64, meta.segment_bytes);
        let header = CsrHeader::decode(&bytes[..CSR_HEADER_LEN])?;
        assert_eq!(header.edge_body_count, 2);
        assert_eq!(header.bodies_len, (2 * DISK_EDGE_BODY_LEN) as u64);
        assert_eq!(
            meta.property_index_offset,
            header.bodies_offset + header.bodies_len
        );

        let first_body_start = header.bodies_offset as usize;
        let first_body =
            DiskEdgeBody::decode(&bytes[first_body_start..first_body_start + DISK_EDGE_BODY_LEN]);
        let second_body_start = first_body_start + DISK_EDGE_BODY_LEN;
        let second_body =
            DiskEdgeBody::decode(&bytes[second_body_start..second_body_start + DISK_EDGE_BODY_LEN]);
        assert_eq!(first_body.dst, dst_a);
        assert_eq!(first_body.prop_offset, 1);
        assert_eq!(second_body.dst, dst_b);
        assert_eq!(second_body.prop_offset, 0);

        let index_start = meta.property_index_offset as usize;
        let list_header = DiskPropertyListHeader::decode(
            &bytes[index_start..index_start + DISK_PROPERTY_LIST_HEADER_LEN],
        );
        assert_eq!(list_header.count, 2);

        let first_entry_start = index_start + DISK_PROPERTY_LIST_HEADER_LEN;
        let first_entry = DiskPropertyValueIndexEntry::decode(
            &bytes[first_entry_start..first_entry_start + DISK_PROPERTY_VALUE_INDEX_ENTRY_LEN],
        );
        let second_entry_start = first_entry_start + DISK_PROPERTY_VALUE_INDEX_ENTRY_LEN;
        let second_entry = DiskPropertyValueIndexEntry::decode(
            &bytes[second_entry_start..second_entry_start + DISK_PROPERTY_VALUE_INDEX_ENTRY_LEN],
        );
        assert_eq!(first_entry.property_id, 5);
        assert_eq!(first_entry.encoding_epoch, 42);
        assert_eq!(first_entry.value_offset, 0);
        assert_eq!(first_entry.value_len, 3);
        assert_eq!(first_entry.flags, 0);
        assert_eq!(second_entry.property_id, 7);
        assert_eq!(second_entry.encoding_epoch, 41);
        assert_eq!(second_entry.value_offset, 3);
        assert_eq!(second_entry.value_len, 2);
        assert_eq!(second_entry.flags, 2);

        let values_start = meta.property_values_offset as usize;
        assert_eq!(
            &bytes[values_start..values_start + meta.property_values_len as usize],
            &[1, 2, 3, 4, 5]
        );

        let reader = CsrReader::new(backend, tmp.path());
        let neighbors = reader.get_neighbors(&meta, src).await?;
        assert_eq!(neighbors.len(), 2);
        assert_eq!(neighbors[0].dst, dst_a);
        assert_eq!(neighbors[1].dst, dst_b);
        Ok(())
    }

    #[tokio::test]
    async fn property_aware_writer_marks_wide_property_bitmap_conservative() -> Result<()> {
        let tmp = tempfile::tempdir()?;
        let writer = CsrWriter::new(backend(), tmp.path(), 10);
        let meta = writer
            .write_segment_with_properties(
                0,
                8,
                vec![EdgeRecordWithProperties {
                    edge: EdgeRecord::insert(person_vid(1), person_vid(2), 2, 10),
                    properties: vec![EdgePropertyValue::encoded(65, 9, [9u8])],
                }],
            )
            .await?;

        assert!(meta.has_property_value_section());
        assert_eq!(meta.property_presence_bitmap, 0);
        assert_eq!(
            meta.property_summary_completeness,
            SemanticSummaryCompleteness::Conservative
        );
        assert!(!meta.definitely_lacks_property(65));
        assert!(meta.definitely_lacks_property(5));
        assert_eq!(meta.property_encoding_epoch, 9);
        Ok(())
    }
}
