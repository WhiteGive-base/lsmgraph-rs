use std::path::PathBuf;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ReaderBackendKind {
    Direct,
    BufferedPread,
    MmapDebug,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum OffsetStrategy {
    Preload,
    Cache,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IoConfig {
    pub backend: ReaderBackendKind,
    pub offset_strategy: OffsetStrategy,
    pub neighbor_block_bytes: usize,
    pub prop_block_bytes: usize,
    pub cache_mb_per_worker: usize,
    pub small_degree_threshold: u64,
    pub huge_degree_threshold: u64,
    pub base_dir: Option<PathBuf>,
}

impl Default for IoConfig {
    fn default() -> Self {
        Self {
            backend: ReaderBackendKind::BufferedPread,
            offset_strategy: OffsetStrategy::Preload,
            neighbor_block_bytes: 256 * 1024,
            prop_block_bytes: 64 * 1024,
            cache_mb_per_worker: 256,
            small_degree_threshold: 128,
            huge_degree_threshold: 65_536,
            base_dir: None,
        }
    }
}

impl IoConfig {
    pub fn linux_direct() -> Self {
        Self {
            backend: ReaderBackendKind::Direct,
            ..Self::default()
        }
    }
}
