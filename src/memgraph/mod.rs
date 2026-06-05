use std::collections::{BTreeMap, HashMap};
use std::sync::atomic::{AtomicU64, Ordering};

use parking_lot::Mutex;

use crate::csr::writer::EdgeRecordWithProperties;
use crate::types::{EdgeRecord, EdgeType, VertexId};

#[derive(Debug, Clone, Copy)]
enum AdjRef {
    Inline { segment_id: usize },
    Overflow,
}

#[derive(Debug)]
struct MemGraphInner {
    vertex_map: HashMap<VertexId, AdjRef>,
    low_degree_segments: Vec<Vec<EdgeRecordWithProperties>>,
    high_degree: HashMap<VertexId, BTreeMap<(EdgeType, VertexId, u64), EdgeRecordWithProperties>>,
    bytes: usize,
    edge_count: usize,
}

#[derive(Debug)]
pub struct MemGraph {
    id: u64,
    inline_segment_capacity: usize,
    capacity_bytes: usize,
    inner: Mutex<MemGraphInner>,
}

static NEXT_MEMGRAPH_ID: AtomicU64 = AtomicU64::new(1);

impl MemGraph {
    pub fn new(capacity_bytes: usize, inline_segment_capacity: usize) -> Self {
        Self {
            id: NEXT_MEMGRAPH_ID.fetch_add(1, Ordering::Relaxed),
            inline_segment_capacity,
            capacity_bytes,
            inner: Mutex::new(MemGraphInner {
                vertex_map: HashMap::new(),
                low_degree_segments: Vec::new(),
                high_degree: HashMap::new(),
                bytes: 0,
                edge_count: 0,
            }),
        }
    }

    pub fn id(&self) -> u64 {
        self.id
    }

    pub fn insert(&self, edge: EdgeRecord) {
        self.insert_with_properties(EdgeRecordWithProperties::topology_only(edge));
    }

    pub fn insert_with_properties(&self, record: EdgeRecordWithProperties) {
        let mut inner = self.inner.lock();
        inner.bytes += estimated_record_bytes(&record);
        inner.edge_count += 1;
        let edge = record.edge;

        match inner.vertex_map.get(&edge.src).copied() {
            None => {
                let segment_id = inner.low_degree_segments.len();
                inner.low_degree_segments.push(vec![record]);
                inner
                    .vertex_map
                    .insert(edge.src, AdjRef::Inline { segment_id });
            }
            Some(AdjRef::Inline { segment_id }) => {
                if inner.low_degree_segments[segment_id].len() < self.inline_segment_capacity {
                    inner.low_degree_segments[segment_id].push(record);
                } else {
                    let existing = std::mem::take(&mut inner.low_degree_segments[segment_id]);
                    let tree = inner.high_degree.entry(edge.src).or_default();
                    for old in existing {
                        tree.insert((old.edge.edge_type, old.edge.dst, old.edge.ts), old);
                    }
                    tree.insert((edge.edge_type, edge.dst, edge.ts), record);
                    inner.vertex_map.insert(edge.src, AdjRef::Overflow);
                }
            }
            Some(AdjRef::Overflow) => {
                inner
                    .high_degree
                    .entry(edge.src)
                    .or_default()
                    .insert((edge.edge_type, edge.dst, edge.ts), record);
            }
        }
    }

    pub fn is_full(&self) -> bool {
        self.inner.lock().bytes >= self.capacity_bytes
    }

    pub fn is_empty(&self) -> bool {
        self.inner.lock().edge_count == 0
    }

    pub fn edge_count(&self) -> usize {
        self.inner.lock().edge_count
    }

    pub fn get_edges_for_src(&self, src: VertexId) -> Vec<EdgeRecord> {
        self.get_edges_with_properties_for_src(src)
            .into_iter()
            .map(|record| record.edge)
            .collect()
    }

    pub fn get_edges_with_properties_for_src(
        &self,
        src: VertexId,
    ) -> Vec<EdgeRecordWithProperties> {
        let inner = self.inner.lock();
        match inner.vertex_map.get(&src).copied() {
            None => Vec::new(),
            Some(AdjRef::Inline { segment_id }) => inner.low_degree_segments[segment_id].clone(),
            Some(AdjRef::Overflow) => inner
                .high_degree
                .get(&src)
                .map(|tree| tree.values().cloned().collect())
                .unwrap_or_default(),
        }
    }

    pub fn all_edges_sorted(&self) -> Vec<EdgeRecord> {
        self.all_edges_with_properties_sorted()
            .into_iter()
            .map(|record| record.edge)
            .collect()
    }

    pub fn all_edges_with_properties_sorted(&self) -> Vec<EdgeRecordWithProperties> {
        let inner = self.inner.lock();
        let mut edges = Vec::with_capacity(inner.edge_count);
        for segment in &inner.low_degree_segments {
            edges.extend(segment.iter().cloned());
        }
        for tree in inner.high_degree.values() {
            edges.extend(tree.values().cloned());
        }
        edges.sort_by_key(|record| {
            (
                record.edge.src,
                record.edge.edge_type,
                record.edge.dst,
                record.edge.ts,
            )
        });
        edges
    }
}

fn estimated_record_bytes(record: &EdgeRecordWithProperties) -> usize {
    std::mem::size_of::<EdgeRecord>()
        + record
            .properties
            .iter()
            .map(|property| std::mem::size_of_val(property) + property.encoded_value.len())
            .sum::<usize>()
}
