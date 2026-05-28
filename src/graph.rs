use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Instant;

use parking_lot::Mutex;
use tokio::task::JoinHandle;

use crate::config::LsmGraphConfig;
use crate::csr::{CsrReader, CsrSegmentMeta, CsrWriter, Manifest, ManifestRecord};
use crate::error::Result;
use crate::index::{MultiLevelIndex, VertexLockTable};
use crate::io::AnyIoBackend;
use crate::levels::{split_levels, L0, L1};
use crate::memgraph::MemGraph;
use crate::metrics::Metrics;
use crate::types::{EdgeMarker, EdgeRecord, EdgeType, FileId, SnapshotId, VertexId};
use crate::version::{Version, VersionGuard, VersionManager};

struct EngineState {
    active: Arc<MemGraph>,
    next_file_id: FileId,
    flush_tasks: Vec<JoinHandle<Result<()>>>,
}

pub struct Engine {
    config: LsmGraphConfig,
    backend: Arc<AnyIoBackend>,
    metrics: Arc<Metrics>,
    manifest: Manifest,
    version_manager: Arc<VersionManager>,
    state: Mutex<EngineState>,
    timestamp: AtomicU64,
    index: Arc<MultiLevelIndex>,
    vertex_locks: Arc<VertexLockTable>,
}

impl Engine {
    pub async fn create(config: LsmGraphConfig) -> Result<Arc<Self>> {
        prepare_store(&config.store_dir, true).await?;
        Self::open_inner(config, true).await
    }

    pub async fn open(config: LsmGraphConfig) -> Result<Arc<Self>> {
        prepare_store(&config.store_dir, false).await?;
        Self::open_inner(config, false).await
    }

    async fn open_inner(config: LsmGraphConfig, fresh: bool) -> Result<Arc<Self>> {
        let metrics = Arc::new(Metrics::default());
        let backend = Arc::new(AnyIoBackend::new(
            config.io_backend,
            config.max_outstanding_io,
            metrics.clone(),
        )?);
        let manifest = Manifest::new(&config.store_dir);
        let manifest_state = if fresh {
            crate::csr::manifest::ManifestState {
                live_files: Vec::new(),
                next_file_id: 1,
                max_ts: 0,
            }
        } else {
            manifest.load()?
        };

        let active = Arc::new(MemGraph::new(
            config.memgraph_capacity_bytes,
            config.inline_segment_capacity,
        ));
        let levels = split_levels(manifest_state.live_files, config.max_levels);
        let version = Version {
            id: 1,
            memgraphs: vec![active.clone()],
            levels,
        };

        let engine = Arc::new(Self {
            config,
            backend,
            metrics,
            manifest,
            version_manager: Arc::new(VersionManager::new(version)),
            state: Mutex::new(EngineState {
                active,
                next_file_id: manifest_state.next_file_id,
                flush_tasks: Vec::new(),
            }),
            timestamp: AtomicU64::new(manifest_state.max_ts),
            index: Arc::new(MultiLevelIndex::default()),
            vertex_locks: Arc::new(VertexLockTable::new(1 << 16)),
        });
        engine.rebuild_index().await?;
        Ok(engine)
    }

    pub fn current_snapshot(&self) -> SnapshotId {
        self.timestamp.load(Ordering::SeqCst)
    }

    pub fn metrics(&self) -> Arc<Metrics> {
        self.metrics.clone()
    }

    pub async fn insert_edge(
        self: &Arc<Self>,
        src: VertexId,
        dst: VertexId,
        edge_type: EdgeType,
    ) -> Result<SnapshotId> {
        let started = Instant::now();
        let ts = self.timestamp.fetch_add(1, Ordering::SeqCst) + 1;
        self.metrics.insert_ops.fetch_add(1, Ordering::Relaxed);
        let result = self
            .write_edge(EdgeRecord::insert(src, dst, edge_type, ts))
            .await;
        self.metrics.storage_insert_latency.record_since(started);
        result?;
        Ok(ts)
    }

    pub async fn delete_edge(
        self: &Arc<Self>,
        src: VertexId,
        dst: VertexId,
        edge_type: EdgeType,
    ) -> Result<SnapshotId> {
        let started = Instant::now();
        let ts = self.timestamp.fetch_add(1, Ordering::SeqCst) + 1;
        self.metrics.delete_ops.fetch_add(1, Ordering::Relaxed);
        let result = self
            .write_edge(EdgeRecord::delete(src, dst, edge_type, ts))
            .await;
        self.metrics.storage_delete_latency.record_since(started);
        result?;
        Ok(ts)
    }

    async fn write_edge(self: &Arc<Self>, edge: EdgeRecord) -> Result<()> {
        let _lock = self.vertex_locks.write_lock(edge.src);
        let mut frozen = None;
        {
            let mut state = self.state.lock();
            state.active.insert(edge);
            if state.active.is_full() {
                let old_active = state.active.clone();
                let new_active = Arc::new(MemGraph::new(
                    self.config.memgraph_capacity_bytes,
                    self.config.inline_segment_capacity,
                ));
                state.active = new_active.clone();
                frozen = Some(old_active);

                let old = self.version_manager.pin_current();
                let mut memgraphs = Vec::with_capacity(old.version().memgraphs.len() + 1);
                memgraphs.push(new_active);
                memgraphs.extend(old.version().memgraphs.iter().cloned());
                self.version_manager.publish(Version {
                    id: old.version().id + 1,
                    memgraphs,
                    levels: old.version().levels.clone(),
                });
            }
        }
        if let Some(memgraph) = frozen {
            self.enqueue_flush(memgraph).await?;
        }
        Ok(())
    }

    pub async fn flush_active(self: &Arc<Self>) -> Result<()> {
        let frozen = {
            let mut state = self.state.lock();
            if state.active.is_empty() {
                None
            } else {
                let old_active = state.active.clone();
                let new_active = Arc::new(MemGraph::new(
                    self.config.memgraph_capacity_bytes,
                    self.config.inline_segment_capacity,
                ));
                state.active = new_active.clone();
                let old = self.version_manager.pin_current();
                let mut memgraphs = Vec::with_capacity(old.version().memgraphs.len() + 1);
                memgraphs.push(new_active);
                memgraphs.extend(old.version().memgraphs.iter().cloned());
                self.version_manager.publish(Version {
                    id: old.version().id + 1,
                    memgraphs,
                    levels: old.version().levels.clone(),
                });
                Some(old_active)
            }
        };
        if let Some(memgraph) = frozen {
            self.enqueue_flush(memgraph).await?;
        }
        self.wait_for_flushes().await
    }

    async fn enqueue_flush(self: &Arc<Self>, memgraph: Arc<MemGraph>) -> Result<()> {
        let engine = self.clone();
        let handle = tokio::spawn(async move { engine.flush_memgraph(memgraph).await });
        self.state.lock().flush_tasks.push(handle);
        Ok(())
    }

    pub async fn wait_for_flushes(&self) -> Result<()> {
        loop {
            let task = self.state.lock().flush_tasks.pop();
            let Some(task) = task else {
                break;
            };
            task.await??;
        }
        Ok(())
    }

    async fn flush_memgraph(self: Arc<Self>, memgraph: Arc<MemGraph>) -> Result<()> {
        let started = Instant::now();
        let edges = memgraph.all_edges_sorted();
        if edges.is_empty() {
            self.metrics.storage_flush_latency.record_since(started);
            return Ok(());
        }
        let file_id = self.alloc_file_id();
        let writer = CsrWriter::new(self.backend.clone(), self.config.store_dir.clone());
        let meta = writer.write_segment(L0, file_id, edges).await?;
        self.manifest.append(&ManifestRecord::CreateFile { meta })?;
        self.metrics.flush_count.fetch_add(1, Ordering::Relaxed);

        let old = self.version_manager.pin_current();
        let mut levels = old.version().levels.clone();
        ensure_level(&mut levels, L0);
        levels[L0 as usize].push(meta);
        levels[L0 as usize].sort_by_key(|m| (m.file_id, m.min_src));
        let memgraphs = old
            .version()
            .memgraphs
            .iter()
            .filter(|m| m.id() != memgraph.id())
            .cloned()
            .collect();
        self.version_manager.publish(Version {
            id: old.version().id + 1,
            memgraphs,
            levels,
        });
        self.rebuild_index().await?;
        self.metrics.storage_flush_latency.record_since(started);
        Ok(())
    }

    fn alloc_file_id(&self) -> FileId {
        let mut state = self.state.lock();
        let file_id = state.next_file_id;
        state.next_file_id += 1;
        file_id
    }

    pub async fn get_neighbors(
        &self,
        src: VertexId,
        snapshot: SnapshotId,
    ) -> Result<Vec<EdgeRecord>> {
        let started = Instant::now();
        self.metrics
            .get_neighbors_ops
            .fetch_add(1, Ordering::Relaxed);
        let _lock = self.vertex_locks.read_lock(src);
        let guard = self.version_manager.pin_current();
        let mut updates = Vec::new();
        for memgraph in &guard.version().memgraphs {
            updates.extend(memgraph.get_edges_for_src(src));
        }

        let reader = CsrReader::with_metrics(
            self.backend.clone(),
            self.config.store_dir.clone(),
            self.metrics.clone(),
        );
        if let Some(l0) = guard.version().levels.get(L0 as usize) {
            for meta in l0 {
                updates.extend(reader.get_neighbors(meta, src).await?);
            }
        }
        for meta in self.index.get_positions(src) {
            updates.extend(reader.get_neighbors(&meta, src).await?);
        }

        let out = merge_visible(updates, snapshot);
        self.metrics
            .storage_get_neighbors_latency
            .record_since(started);
        Ok(out)
    }

    pub async fn scan_edges(&self, snapshot: SnapshotId) -> Result<Vec<EdgeRecord>> {
        let started = Instant::now();
        self.metrics.scan_ops.fetch_add(1, Ordering::Relaxed);
        let guard = self.version_manager.pin_current();
        let mut updates = Vec::new();
        for memgraph in &guard.version().memgraphs {
            updates.extend(memgraph.all_edges_sorted());
        }
        let reader = CsrReader::with_metrics(
            self.backend.clone(),
            self.config.store_dir.clone(),
            self.metrics.clone(),
        );
        for level in &guard.version().levels {
            for meta in level {
                updates.extend(reader.read_all_edges(meta).await?);
            }
        }
        let out = merge_visible(updates, snapshot);
        self.metrics
            .storage_scan_edges_latency
            .record_since(started);
        Ok(out)
    }

    pub async fn compact_l0_to_l1(&self) -> Result<Option<CsrSegmentMeta>> {
        let started = Instant::now();
        self.wait_for_flushes().await?;
        let guard = self.version_manager.pin_current();
        let l0_files = guard
            .version()
            .levels
            .get(L0 as usize)
            .cloned()
            .unwrap_or_default();
        if l0_files.is_empty() {
            self.metrics
                .storage_compaction_latency
                .record_since(started);
            return Ok(None);
        }
        let l1_files = guard
            .version()
            .levels
            .get(L1 as usize)
            .cloned()
            .unwrap_or_default();

        let reader = CsrReader::with_metrics(
            self.backend.clone(),
            self.config.store_dir.clone(),
            self.metrics.clone(),
        );
        let mut updates = Vec::new();
        let mut input_bytes = 0u64;
        for meta in l0_files.iter().chain(l1_files.iter()) {
            input_bytes += meta.edge_count * std::mem::size_of::<EdgeRecord>() as u64;
            updates.extend(reader.read_all_edges(meta).await?);
        }
        let snapshot = self.current_snapshot();
        let compacted = merge_latest_records(updates, snapshot, true);
        let file_id = self.alloc_file_id();
        let writer = CsrWriter::new(self.backend.clone(), self.config.store_dir.clone());
        let output = writer.write_segment(L1, file_id, compacted).await?;
        self.manifest
            .append(&ManifestRecord::CreateFile { meta: output })?;
        for meta in l0_files.iter().chain(l1_files.iter()) {
            self.manifest.append(&ManifestRecord::DeleteFile {
                file_id: meta.file_id,
            })?;
        }

        let old = self.version_manager.pin_current();
        let mut levels = old.version().levels.clone();
        ensure_level(&mut levels, L1);
        levels[L0 as usize].clear();
        levels[L1 as usize].clear();
        levels[L1 as usize].push(output);
        self.version_manager.publish(Version {
            id: old.version().id + 1,
            memgraphs: old.version().memgraphs.clone(),
            levels,
        });
        self.metrics
            .compaction_count
            .fetch_add(1, Ordering::Relaxed);
        self.metrics
            .compaction_input_bytes
            .fetch_add(input_bytes, Ordering::Relaxed);
        self.metrics.compaction_output_bytes.fetch_add(
            output.edge_count * std::mem::size_of::<EdgeRecord>() as u64,
            Ordering::Relaxed,
        );
        self.rebuild_index().await?;
        self.metrics
            .storage_compaction_latency
            .record_since(started);
        Ok(Some(output))
    }

    async fn rebuild_index(&self) -> Result<()> {
        let started = Instant::now();
        let guard = self.version_manager.pin_current();
        let reader = CsrReader::with_metrics(
            self.backend.clone(),
            self.config.store_dir.clone(),
            self.metrics.clone(),
        );
        let mut entries: HashMap<VertexId, Vec<CsrSegmentMeta>> = HashMap::new();
        for (level_id, level) in guard.version().levels.iter().enumerate() {
            if level_id == L0 as usize {
                continue;
            }
            for meta in level {
                for offset in reader.read_offsets(meta).await.unwrap_or_default() {
                    entries.entry(offset.src).or_default().push(*meta);
                }
            }
        }
        self.index.rebuild(entries);
        self.metrics
            .storage_rebuild_index_latency
            .record_since(started);
        Ok(())
    }

    pub fn version_guard(&self) -> VersionGuard {
        self.version_manager.pin_current()
    }

    pub fn live_file_count_by_level(&self) -> Vec<usize> {
        let guard = self.version_manager.pin_current();
        guard.version().levels.iter().map(Vec::len).collect()
    }
}

fn merge_visible(updates: Vec<EdgeRecord>, snapshot: SnapshotId) -> Vec<EdgeRecord> {
    merge_latest_records(updates, snapshot, false)
        .into_iter()
        .filter(|e| e.marker == EdgeMarker::Insert)
        .collect()
}

fn merge_latest_records(
    mut updates: Vec<EdgeRecord>,
    snapshot: SnapshotId,
    keep_tombstones: bool,
) -> Vec<EdgeRecord> {
    updates.retain(|e| e.ts <= snapshot);
    updates.sort_by(|a, b| {
        (a.src, a.edge_type, a.dst, std::cmp::Reverse(a.ts)).cmp(&(
            b.src,
            b.edge_type,
            b.dst,
            std::cmp::Reverse(b.ts),
        ))
    });
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    for edge in updates {
        let key = (edge.src, edge.edge_type, edge.dst);
        if seen.insert(key) && (keep_tombstones || edge.marker == EdgeMarker::Insert) {
            out.push(edge);
        }
    }
    out.sort_by_key(|e| (e.src, e.edge_type, e.dst));
    out
}

async fn prepare_store(path: &Path, fresh: bool) -> Result<()> {
    let path = path.to_path_buf();
    tokio::task::spawn_blocking(move || -> Result<()> {
        if fresh && path.exists() {
            std::fs::remove_dir_all(&path)?;
        }
        std::fs::create_dir_all(path.join("levels/L0"))?;
        std::fs::create_dir_all(path.join("levels/L1"))?;
        Ok(())
    })
    .await??;
    Ok(())
}

fn ensure_level(levels: &mut Vec<Vec<CsrSegmentMeta>>, level: u8) {
    let idx = level as usize;
    if levels.len() <= idx {
        levels.resize(idx + 1, Vec::new());
    }
}
