use std::path::PathBuf;
use std::str::FromStr;

use anyhow::Result;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IoBackendKind {
    Blocking,
    Direct,
    Uring,
}

impl FromStr for IoBackendKind {
    type Err = anyhow::Error;

    fn from_str(s: &str) -> Result<Self> {
        match s.to_ascii_lowercase().as_str() {
            "blocking" | "pread" | "default" => Ok(Self::Blocking),
            "direct" | "direct-io" | "odirect" => Ok(Self::Direct),
            "uring" | "io-uring" | "io_uring" => Ok(Self::Uring),
            _ => anyhow::bail!("unknown io backend: {s}"),
        }
    }
}

#[derive(Debug, Clone)]
pub struct LsmGraphConfig {
    pub store_dir: PathBuf,
    pub memgraph_capacity_bytes: usize,
    pub inline_segment_capacity: usize,
    pub level_fanout: usize,
    pub max_levels: usize,
    pub l0_file_threshold: usize,
    pub segment_target_bytes: usize,
    pub max_background_flushes: usize,
    pub max_outstanding_io: usize,
    pub io_backend: IoBackendKind,
}

impl LsmGraphConfig {
    pub fn new(store_dir: impl Into<PathBuf>) -> Self {
        Self {
            store_dir: store_dir.into(),
            memgraph_capacity_bytes: 64 * 1024 * 1024,
            inline_segment_capacity: 8,
            level_fanout: 10,
            max_levels: 5,
            l0_file_threshold: 4,
            segment_target_bytes: 64 * 1024 * 1024,
            max_background_flushes: 2,
            max_outstanding_io: 128,
            io_backend: IoBackendKind::Blocking,
        }
    }

    pub fn with_memgraph_capacity(mut self, bytes: usize) -> Self {
        self.memgraph_capacity_bytes = bytes;
        self
    }

    pub fn with_io_backend(mut self, backend: IoBackendKind) -> Self {
        self.io_backend = backend;
        self
    }
}
