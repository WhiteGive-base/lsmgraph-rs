use std::collections::HashMap;

use parking_lot::{RwLock, RwLockReadGuard, RwLockWriteGuard};

use crate::csr::CsrSegmentMeta;
use crate::types::VertexId;

#[derive(Debug, Default)]
pub struct MultiLevelIndex {
    by_vertex: RwLock<HashMap<VertexId, Vec<CsrSegmentMeta>>>,
}

impl MultiLevelIndex {
    pub fn rebuild(&self, entries: HashMap<VertexId, Vec<CsrSegmentMeta>>) {
        *self.by_vertex.write() = entries;
    }

    pub fn get_positions(&self, src: VertexId) -> Vec<CsrSegmentMeta> {
        self.by_vertex.read().get(&src).cloned().unwrap_or_default()
    }
}

#[derive(Debug)]
pub struct VertexLockTable {
    locks: Vec<RwLock<()>>,
}

impl VertexLockTable {
    pub fn new(shards: usize) -> Self {
        let size = shards.next_power_of_two();
        Self {
            locks: (0..size).map(|_| RwLock::new(())).collect(),
        }
    }

    pub fn read_lock(&self, src: VertexId) -> RwLockReadGuard<'_, ()> {
        self.locks[(src as usize) & (self.locks.len() - 1)].read()
    }

    pub fn write_lock(&self, src: VertexId) -> RwLockWriteGuard<'_, ()> {
        self.locks[(src as usize) & (self.locks.len() - 1)].write()
    }
}
