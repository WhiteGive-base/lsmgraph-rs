use std::sync::atomic::{AtomicU64, Ordering};

#[derive(Debug, Default)]
pub struct Metrics {
    pub insert_ops: AtomicU64,
    pub delete_ops: AtomicU64,
    pub get_neighbors_ops: AtomicU64,
    pub scan_ops: AtomicU64,
    pub read_syscalls: AtomicU64,
    pub write_syscalls: AtomicU64,
    pub read_bytes: AtomicU64,
    pub write_bytes: AtomicU64,
    pub flush_count: AtomicU64,
    pub compaction_count: AtomicU64,
    pub compaction_input_bytes: AtomicU64,
    pub compaction_output_bytes: AtomicU64,
}

impl Metrics {
    pub fn add_read(&self, bytes: u64) {
        self.read_syscalls.fetch_add(1, Ordering::Relaxed);
        self.read_bytes.fetch_add(bytes, Ordering::Relaxed);
    }

    pub fn add_write(&self, bytes: u64) {
        self.write_syscalls.fetch_add(1, Ordering::Relaxed);
        self.write_bytes.fetch_add(bytes, Ordering::Relaxed);
    }
}
