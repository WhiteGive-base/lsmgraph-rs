use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Instant;

use parking_lot::Mutex;
use tokio::task::JoinHandle;

use crate::config::LsmGraphConfig;
use crate::csr::{
    CsrMetadataCache, CsrReader, CsrSegmentMeta, CsrWriter, Manifest, ManifestRecord,
};
use crate::error::Result;
use crate::index::{MultiLevelIndex, VertexLockTable};
use crate::io::AnyIoBackend;
use crate::levels::{split_levels, L0, L1};
use crate::memgraph::MemGraph;
use crate::metrics::{L0PartitionKey, L0PartitionProbe, L0PartitionSnapshot, Metrics};
use crate::types::{
    source_label_from_vertex_id, EdgeMarker, EdgeRecord, EdgeType, FileId, SnapshotId, VertexId,
    MIXED_EDGE_TYPE, UNKNOWN_SOURCE_LABEL,
};
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
    metadata_cache: Arc<CsrMetadataCache>,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct L0CompactionDecision {
    pub key: L0PartitionKey,
    pub score: f64,
    pub estimated_rewrite_bytes: u64,
    pub selected_l0_segments: usize,
    pub query_count: u64,
    pub avg_candidate_segments: f64,
    pub offset_cache_miss_rate: f64,
    pub outputs: Vec<CsrSegmentMeta>,
}

#[derive(Debug, Clone)]
struct PickedL0Partition {
    key: L0PartitionKey,
    score: f64,
    estimated_rewrite_bytes: u64,
    selected_l0_segments: usize,
    query_count: u64,
    avg_candidate_segments: f64,
    offset_cache_miss_rate: f64,
}

#[derive(Debug, Clone, Copy)]
struct L0CompactionTarget {
    src_label: i32,
    edge_type: Option<EdgeType>,
    min_src: Option<VertexId>,
    max_src: Option<VertexId>,
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
            metadata_cache: Arc::new(CsrMetadataCache::new(4096)),
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
        self.wait_for_flushes().await?;
        if self.config.auto_compaction {
            self.compact_l0_to_l1_inner(false).await?;
        }
        Ok(())
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
        let writer = CsrWriter::new(self.backend.clone(), self.config.store_dir.clone());
        let mut metas = Vec::new();
        for segment_edges in self.build_l0_flush_segments(edges) {
            let file_id = self.alloc_file_id();
            let meta = writer.write_segment(L0, file_id, segment_edges).await?;
            self.manifest.append(&ManifestRecord::CreateFile { meta })?;
            metas.push(meta);
        }
        self.metrics.flush_count.fetch_add(1, Ordering::Relaxed);

        let old = self.version_manager.pin_current();
        let mut levels = old.version().levels.clone();
        ensure_level(&mut levels, L0);
        levels[L0 as usize].extend(metas);
        levels[L0 as usize]
            .sort_by_key(|m| (m.src_label, m.edge_type_partition, m.min_src, m.file_id));
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

    fn build_l0_flush_segments(&self, edges: Vec<EdgeRecord>) -> Vec<Vec<EdgeRecord>> {
        if !self.config.graph_aware_l0 {
            return vec![edges];
        }

        let mut exact: BTreeMap<(i32, EdgeType), Vec<EdgeRecord>> = BTreeMap::new();
        for edge in edges {
            exact
                .entry((source_label_from_vertex_id(edge.src), edge.edge_type))
                .or_default()
                .push(edge);
        }

        let mut partitions: BTreeMap<(i32, EdgeType), Vec<EdgeRecord>> = BTreeMap::new();
        for ((src_label, edge_type), group) in exact {
            if estimate_segment_bytes(&group) < self.config.min_l0_partition_bytes {
                partitions
                    .entry((src_label, MIXED_EDGE_TYPE))
                    .or_default()
                    .extend(group);
            } else {
                partitions
                    .entry((src_label, edge_type))
                    .or_default()
                    .extend(group);
            }
        }

        let mut segments = Vec::new();
        for (_, mut group) in partitions {
            group.sort_by_key(|e| (e.src, e.edge_type, e.dst, e.ts));
            segments.extend(split_range_bounded_segments(
                group,
                self.config.segment_target_bytes,
            ));
        }

        if segments.is_empty() {
            Vec::new()
        } else if segments.len() <= self.config.max_l0_segments_per_flush {
            segments
        } else {
            merge_segments_to_cap(segments, self.config.max_l0_segments_per_flush)
        }
    }

    pub async fn get_neighbors(
        &self,
        src: VertexId,
        snapshot: SnapshotId,
    ) -> Result<Vec<EdgeRecord>> {
        self.get_neighbors_typed_internal(src, None, snapshot).await
    }

    pub async fn get_neighbors_typed(
        &self,
        src: VertexId,
        edge_type: EdgeType,
        snapshot: SnapshotId,
    ) -> Result<Vec<EdgeRecord>> {
        self.get_neighbors_typed_internal(src, Some(edge_type), snapshot)
            .await
    }

    async fn get_neighbors_typed_internal(
        &self,
        src: VertexId,
        edge_type: Option<EdgeType>,
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

        let reader = CsrReader::with_metrics_and_cache(
            self.backend.clone(),
            self.config.store_dir.clone(),
            self.metrics.clone(),
            self.metadata_cache.clone(),
        );
        if let Some(l0) = guard.version().levels.get(L0 as usize) {
            let mut touched_partitions = HashSet::new();
            for meta in l0 {
                if !meta.may_contain_partition(src, edge_type) {
                    continue;
                }
                let partition_key = self.l0_partition_key(src, edge_type, meta);
                if touched_partitions.insert(partition_key) {
                    self.metrics.record_l0_partition_query(partition_key);
                }
                let mut partition_probe = L0PartitionProbe {
                    candidate_segments: 1,
                    ..L0PartitionProbe::default()
                };
                self.metrics
                    .candidate_l0_segments
                    .fetch_add(1, Ordering::Relaxed);
                if src < meta.min_src || src > meta.max_src {
                    self.metrics
                        .range_filtered_segments
                        .fetch_add(1, Ordering::Relaxed);
                    partition_probe.range_filtered_segments = 1;
                    self.metrics
                        .record_l0_partition_probe(partition_key, partition_probe);
                    continue;
                }
                let cached_may_contain = reader.cached_may_contain_src(meta, src);
                match cached_may_contain {
                    Some(false) => {
                        self.metrics
                            .bloom_filtered_segments
                            .fetch_add(1, Ordering::Relaxed);
                        partition_probe.bloom_filtered_segments = 1;
                        partition_probe.offset_cache_hits = 1;
                        self.metrics
                            .record_l0_partition_probe(partition_key, partition_probe);
                        continue;
                    }
                    Some(true) => {
                        partition_probe.offset_cache_hits = 1;
                    }
                    None => {
                        partition_probe.offset_cache_misses = 1;
                    }
                }
                self.metrics
                    .filter_passed_segments
                    .fetch_add(1, Ordering::Relaxed);
                partition_probe.filter_passed_segments = 1;
                let edges = reader.get_neighbors(meta, src).await?;
                if !edges.is_empty() {
                    self.metrics
                        .matched_l0_segments
                        .fetch_add(1, Ordering::Relaxed);
                    partition_probe.matched_segments = 1;
                    partition_probe.body_reads = 1;
                } else if matches!(cached_may_contain, Some(true)) {
                    partition_probe.bloom_false_positive_probes = 1;
                }
                self.metrics
                    .record_l0_partition_probe(partition_key, partition_probe);
                updates.extend(edges);
            }
        }
        for meta in self.index.get_positions(src) {
            updates.extend(reader.get_neighbors(&meta, src).await?);
        }

        if let Some(edge_type) = edge_type {
            updates.retain(|edge| edge.edge_type == edge_type);
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
        let reader = CsrReader::with_metrics_and_cache(
            self.backend.clone(),
            self.config.store_dir.clone(),
            self.metrics.clone(),
            self.metadata_cache.clone(),
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
        self.compact_l0_to_l1_inner(true).await
    }

    pub async fn compact_l0_partition_to_l1(
        &self,
        src_label: i32,
        edge_type: Option<EdgeType>,
    ) -> Result<Vec<CsrSegmentMeta>> {
        self.compact_l0_target_to_l1(L0CompactionTarget {
            src_label,
            edge_type,
            min_src: None,
            max_src: None,
        })
        .await
    }

    pub async fn compact_l0_range_to_l1(
        &self,
        src_label: i32,
        edge_type: Option<EdgeType>,
        min_src: VertexId,
        max_src: VertexId,
    ) -> Result<Vec<CsrSegmentMeta>> {
        self.compact_l0_target_to_l1(L0CompactionTarget {
            src_label,
            edge_type,
            min_src: Some(min_src),
            max_src: Some(max_src),
        })
        .await
    }

    pub async fn compact_best_l0_partition_by_score(&self) -> Result<Option<L0CompactionDecision>> {
        let Some(pick) = self.pick_l0_partition_by_score() else {
            return Ok(None);
        };
        let outputs = self
            .compact_l0_range_to_l1(
                pick.key.src_label,
                Some(pick.key.edge_type),
                pick.key.range_start,
                pick.key.range_end,
            )
            .await?;
        Ok(Some(L0CompactionDecision {
            key: pick.key,
            score: pick.score,
            estimated_rewrite_bytes: pick.estimated_rewrite_bytes,
            selected_l0_segments: pick.selected_l0_segments,
            query_count: pick.query_count,
            avg_candidate_segments: pick.avg_candidate_segments,
            offset_cache_miss_rate: pick.offset_cache_miss_rate,
            outputs,
        }))
    }

    fn pick_l0_partition_by_score(&self) -> Option<PickedL0Partition> {
        let guard = self.version_manager.pin_current();
        let l0_files = guard.version().levels.get(L0 as usize)?;
        if l0_files.is_empty() {
            return None;
        }

        self.metrics
            .l0_partition_snapshots()
            .into_iter()
            .filter_map(|snapshot| self.score_l0_partition(snapshot, l0_files))
            .max_by(|a, b| {
                a.score
                    .partial_cmp(&b.score)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
    }

    fn score_l0_partition(
        &self,
        snapshot: L0PartitionSnapshot,
        l0_files: &[CsrSegmentMeta],
    ) -> Option<PickedL0Partition> {
        if snapshot.query_count < self.config.l0_ra_min_queries {
            return None;
        }
        let target = L0CompactionTarget {
            src_label: snapshot.key.src_label,
            edge_type: Some(snapshot.key.edge_type),
            min_src: Some(snapshot.key.range_start),
            max_src: Some(snapshot.key.range_end),
        };
        let selected: Vec<_> = l0_files
            .iter()
            .filter(|meta| target_matches_meta(meta, target))
            .collect();
        if selected.len() < self.config.l0_ra_min_l0_segments {
            return None;
        }
        let estimated_rewrite_bytes = selected.iter().map(|meta| estimated_meta_bytes(meta)).sum();
        let rewrite_mib = (estimated_rewrite_bytes as f64 / 1_048_576.0).max(1.0);
        let score = snapshot.query_count as f64
            * snapshot.avg_candidate_segments.max(1.0)
            * (1.0 + snapshot.offset_cache_miss_rate)
            / rewrite_mib;
        if score < self.config.l0_ra_min_score {
            return None;
        }
        Some(PickedL0Partition {
            key: snapshot.key,
            score,
            estimated_rewrite_bytes,
            selected_l0_segments: selected.len(),
            query_count: snapshot.query_count,
            avg_candidate_segments: snapshot.avg_candidate_segments,
            offset_cache_miss_rate: snapshot.offset_cache_miss_rate,
        })
    }

    async fn compact_l0_target_to_l1(
        &self,
        target: L0CompactionTarget,
    ) -> Result<Vec<CsrSegmentMeta>> {
        let started = Instant::now();
        self.wait_for_flushes().await?;
        let guard = self.version_manager.pin_current();
        let l0_files = guard
            .version()
            .levels
            .get(L0 as usize)
            .cloned()
            .unwrap_or_default();
        let selected_l0: Vec<_> = l0_files
            .iter()
            .copied()
            .filter(|meta| target_matches_meta(meta, target))
            .collect();
        if selected_l0.is_empty() {
            self.metrics
                .storage_compaction_latency
                .record_since(started);
            return Ok(Vec::new());
        }

        let min_src = selected_l0.iter().map(|m| m.min_src).min().unwrap_or(0);
        let max_src = selected_l0.iter().map(|m| m.max_src).max().unwrap_or(0);
        let l1_files = guard
            .version()
            .levels
            .get(L1 as usize)
            .cloned()
            .unwrap_or_default();
        let selected_l1: Vec<_> = l1_files
            .iter()
            .copied()
            .filter(|meta| {
                partition_overlaps_target(meta, target.src_label, target.edge_type)
                    && ranges_overlap(meta.min_src, meta.max_src, min_src, max_src)
            })
            .collect();

        let reader = CsrReader::with_metrics_and_cache(
            self.backend.clone(),
            self.config.store_dir.clone(),
            self.metrics.clone(),
            self.metadata_cache.clone(),
        );
        let mut updates = Vec::new();
        let mut input_bytes = 0u64;
        for meta in selected_l0.iter().chain(selected_l1.iter()) {
            input_bytes += estimated_meta_bytes(meta);
            updates.extend(reader.read_all_edges(meta).await?);
        }
        let snapshot = self.current_snapshot();
        let compacted = merge_latest_records(updates, snapshot, true);
        let writer = CsrWriter::new(self.backend.clone(), self.config.store_dir.clone());
        let mut outputs = Vec::new();
        for segment_edges in split_compaction_segments(compacted, self.config.segment_target_bytes)
        {
            let file_id = self.alloc_file_id();
            let output = writer.write_segment(L1, file_id, segment_edges).await?;
            self.manifest
                .append(&ManifestRecord::CreateFile { meta: output })?;
            outputs.push(output);
        }
        for meta in selected_l0.iter().chain(selected_l1.iter()) {
            self.manifest.append(&ManifestRecord::DeleteFile {
                file_id: meta.file_id,
            })?;
        }

        let old = self.version_manager.pin_current();
        let selected_l0_ids: HashSet<_> = selected_l0.iter().map(|m| m.file_id).collect();
        let selected_l1_ids: HashSet<_> = selected_l1.iter().map(|m| m.file_id).collect();
        let mut levels = old.version().levels.clone();
        ensure_level(&mut levels, L1);
        levels[L0 as usize].retain(|meta| !selected_l0_ids.contains(&meta.file_id));
        levels[L1 as usize].retain(|meta| !selected_l1_ids.contains(&meta.file_id));
        levels[L1 as usize].extend(outputs.iter().copied());
        sort_level(&mut levels[L0 as usize]);
        sort_level(&mut levels[L1 as usize]);
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
        let output_bytes = outputs.iter().map(estimated_meta_bytes).sum::<u64>();
        self.metrics
            .compaction_output_bytes
            .fetch_add(output_bytes, Ordering::Relaxed);
        self.rebuild_index().await?;
        self.metrics
            .storage_compaction_latency
            .record_since(started);
        Ok(outputs)
    }

    async fn compact_l0_to_l1_inner(&self, force: bool) -> Result<Option<CsrSegmentMeta>> {
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
        if !force && l0_files.len() < self.config.l0_file_threshold {
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

        let reader = CsrReader::with_metrics_and_cache(
            self.backend.clone(),
            self.config.store_dir.clone(),
            self.metrics.clone(),
            self.metadata_cache.clone(),
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
        sort_level(&mut levels[L1 as usize]);
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
        let reader = CsrReader::with_metrics_and_cache(
            self.backend.clone(),
            self.config.store_dir.clone(),
            self.metrics.clone(),
            self.metadata_cache.clone(),
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

    fn l0_partition_key(
        &self,
        src: VertexId,
        edge_type: Option<EdgeType>,
        meta: &CsrSegmentMeta,
    ) -> L0PartitionKey {
        let src_label = match source_label_from_vertex_id(src) {
            UNKNOWN_SOURCE_LABEL => meta.src_label,
            label => label,
        };
        let bucket_size = self.config.l0_ra_range_bucket_size.max(1);
        let range_start = (src / bucket_size) * bucket_size;
        let range_end = range_start.saturating_add(bucket_size - 1);
        L0PartitionKey {
            src_label,
            edge_type: edge_type.unwrap_or(meta.edge_type_partition),
            range_start,
            range_end,
        }
    }
}

fn estimate_segment_bytes(edges: &[EdgeRecord]) -> usize {
    edges.len() * std::mem::size_of::<EdgeRecord>()
}

fn split_range_bounded_segments(
    edges: Vec<EdgeRecord>,
    target_segment_bytes: usize,
) -> Vec<Vec<EdgeRecord>> {
    if edges.is_empty() {
        return Vec::new();
    }
    let target_edges = (target_segment_bytes / std::mem::size_of::<EdgeRecord>()).max(1);
    let mut segments = Vec::new();
    let mut current = Vec::new();
    let mut last_src = None;

    for edge in edges {
        if !current.is_empty()
            && current.len() >= target_edges
            && last_src.is_some_and(|src| src != edge.src)
        {
            segments.push(std::mem::take(&mut current));
        }
        last_src = Some(edge.src);
        current.push(edge);
    }
    if !current.is_empty() {
        segments.push(current);
    }
    segments
}

fn merge_segments_to_cap(
    segments: Vec<Vec<EdgeRecord>>,
    max_segments: usize,
) -> Vec<Vec<EdgeRecord>> {
    if segments.len() <= max_segments || max_segments == 0 {
        return segments;
    }
    let chunk_size = segments.len().div_ceil(max_segments);
    let mut merged = Vec::new();
    let mut current = Vec::new();
    for (idx, segment) in segments.into_iter().enumerate() {
        current.extend(segment);
        if (idx + 1) % chunk_size == 0 {
            merged.push(std::mem::take(&mut current));
        }
    }
    if !current.is_empty() {
        merged.push(current);
    }
    merged
}

fn split_compaction_segments(
    mut edges: Vec<EdgeRecord>,
    target_segment_bytes: usize,
) -> Vec<Vec<EdgeRecord>> {
    edges.sort_by_key(|e| (e.src, e.edge_type, e.dst, e.ts));
    split_range_bounded_segments(edges, target_segment_bytes)
}

fn partition_overlaps_target(
    meta: &CsrSegmentMeta,
    src_label: i32,
    edge_type: Option<EdgeType>,
) -> bool {
    let label_matches = src_label == UNKNOWN_SOURCE_LABEL
        || meta.src_label == UNKNOWN_SOURCE_LABEL
        || meta.src_label == src_label;
    let edge_matches = match edge_type {
        Some(edge_type) => {
            meta.edge_type_partition == MIXED_EDGE_TYPE || meta.edge_type_partition == edge_type
        }
        None => true,
    };
    label_matches && edge_matches
}

fn target_matches_meta(meta: &CsrSegmentMeta, target: L0CompactionTarget) -> bool {
    if !partition_overlaps_target(meta, target.src_label, target.edge_type) {
        return false;
    }
    match (target.min_src, target.max_src) {
        (Some(min_src), Some(max_src)) => {
            ranges_overlap(meta.min_src, meta.max_src, min_src, max_src)
        }
        _ => true,
    }
}

fn ranges_overlap(
    left_min: VertexId,
    left_max: VertexId,
    right_min: VertexId,
    right_max: VertexId,
) -> bool {
    left_min <= right_max && right_min <= left_max
}

fn estimated_meta_bytes(meta: &CsrSegmentMeta) -> u64 {
    if meta.segment_bytes > 0 {
        meta.segment_bytes
    } else {
        meta.edge_count * std::mem::size_of::<EdgeRecord>() as u64
    }
}

fn sort_level(level: &mut [CsrSegmentMeta]) {
    level.sort_by_key(|m| (m.src_label, m.edge_type_partition, m.min_src, m.file_id));
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
