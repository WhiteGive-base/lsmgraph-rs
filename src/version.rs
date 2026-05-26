use std::sync::Arc;

use parking_lot::RwLock;

use crate::csr::CsrSegmentMeta;
use crate::memgraph::MemGraph;

#[derive(Debug)]
pub struct Version {
    pub id: u64,
    pub memgraphs: Vec<Arc<MemGraph>>,
    pub levels: Vec<Vec<CsrSegmentMeta>>,
}

#[derive(Debug, Clone)]
pub struct VersionGuard {
    version: Arc<Version>,
}

impl VersionGuard {
    pub fn version(&self) -> &Arc<Version> {
        &self.version
    }
}

#[derive(Debug)]
pub struct VersionManager {
    current: RwLock<Arc<Version>>,
}

impl VersionManager {
    pub fn new(version: Version) -> Self {
        Self {
            current: RwLock::new(Arc::new(version)),
        }
    }

    pub fn pin_current(&self) -> VersionGuard {
        VersionGuard {
            version: self.current.read().clone(),
        }
    }

    pub fn publish(&self, version: Version) {
        *self.current.write() = Arc::new(version);
    }
}
