use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use parking_lot::Mutex;
use serde::Serialize;
use serde_json::{json, Value};

use crate::types::{EdgeType, VertexId};

const LATENCY_BUCKET_US: [u64; 32] = [
    25,
    50,
    75,
    100,
    150,
    200,
    250,
    350,
    500,
    750,
    1_000,
    1_500,
    2_000,
    3_000,
    5_000,
    7_500,
    10_000,
    15_000,
    20_000,
    30_000,
    50_000,
    75_000,
    100_000,
    150_000,
    250_000,
    500_000,
    1_000_000,
    2_000_000,
    5_000_000,
    10_000_000,
    30_000_000,
    u64::MAX,
];

#[derive(Debug)]
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
    pub degree_directory_sidecar_persists: AtomicU64,

    pub http_requests: AtomicU64,
    pub http_2xx: AtomicU64,
    pub http_4xx: AtomicU64,
    pub http_5xx: AtomicU64,
    pub http_request_read_latency: LatencyMetric,
    pub http_json_parse_latency: LatencyMetric,
    pub http_handler_latency: LatencyMetric,
    pub http_serialize_latency: LatencyMetric,
    pub http_response_write_latency: LatencyMetric,

    pub snb_read_lock_wait_latency: LatencyMetric,
    pub snb_read_lock_hold_latency: LatencyMetric,
    pub snb_write_lock_wait_latency: LatencyMetric,
    pub snb_write_lock_hold_latency: LatencyMetric,

    pub snb_vertices_load_latency: LatencyMetric,
    pub snb_edge_props_load_latency: LatencyMetric,
    pub snb_adjacency_load_latency: LatencyMetric,
    pub snb_adjacency_scan_latency: LatencyMetric,
    pub snb_adjacency_group_latency: LatencyMetric,
    pub snb_adjacency_write_latency: LatencyMetric,
    pub snb_vertices_count: AtomicU64,
    pub snb_edge_props_count: AtomicU64,
    pub snb_adjacency_groups: AtomicU64,
    pub snb_adjacency_edges: AtomicU64,
    pub snb_adjacency_bytes: AtomicU64,
    pub snb_adjacency_cache_hits: AtomicU64,
    pub snb_adjacency_cache_misses: AtomicU64,
    pub snb_adjacency_cache_write_syscalls: AtomicU64,
    pub snb_adjacency_cache_write_bytes: AtomicU64,

    pub vertex_lookups: AtomicU64,
    pub edge_prop_lookups: AtomicU64,
    pub adjacency_lookups: AtomicU64,
    pub neighbor_clone_items: AtomicU64,

    pub storage_insert_latency: LatencyMetric,
    pub storage_delete_latency: LatencyMetric,
    pub storage_get_neighbors_latency: LatencyMetric,
    pub storage_scan_edges_latency: LatencyMetric,
    pub storage_flush_latency: LatencyMetric,
    pub storage_compaction_latency: LatencyMetric,
    pub storage_rebuild_index_latency: LatencyMetric,

    pub csr_get_neighbors_latency: LatencyMetric,
    pub csr_read_all_edges_latency: LatencyMetric,
    pub csr_read_offsets_latency: LatencyMetric,
    pub csr_header_reads: AtomicU64,
    pub csr_offset_reads: AtomicU64,
    pub csr_body_reads: AtomicU64,
    pub csr_full_scan_reads: AtomicU64,
    pub csr_header_bytes: AtomicU64,
    pub csr_offset_bytes: AtomicU64,
    pub csr_body_bytes: AtomicU64,
    pub candidate_l0_segments: AtomicU64,
    pub range_filtered_segments: AtomicU64,
    pub bloom_filtered_segments: AtomicU64,
    pub filter_passed_segments: AtomicU64,
    pub matched_l0_segments: AtomicU64,
    pub csr_offset_cache_hits: AtomicU64,
    pub csr_offset_cache_misses: AtomicU64,
    pub l0_bloom_false_positive_probes: AtomicU64,

    pub io_semaphore_wait_latency: LatencyMetric,
    pub io_read_blocking_latency: LatencyMetric,
    pub io_write_blocking_latency: LatencyMetric,
    pub io_create_blocking_latency: LatencyMetric,
    pub io_sync_blocking_latency: LatencyMetric,
    pub io_remove_blocking_latency: LatencyMetric,

    endpoint_metrics: Mutex<HashMap<String, Arc<EndpointMetric>>>,
    l0_partition_metrics: Mutex<HashMap<L0PartitionKey, L0PartitionMetric>>,
}

impl Default for Metrics {
    fn default() -> Self {
        Self {
            insert_ops: AtomicU64::new(0),
            delete_ops: AtomicU64::new(0),
            get_neighbors_ops: AtomicU64::new(0),
            scan_ops: AtomicU64::new(0),
            read_syscalls: AtomicU64::new(0),
            write_syscalls: AtomicU64::new(0),
            read_bytes: AtomicU64::new(0),
            write_bytes: AtomicU64::new(0),
            flush_count: AtomicU64::new(0),
            compaction_count: AtomicU64::new(0),
            compaction_input_bytes: AtomicU64::new(0),
            compaction_output_bytes: AtomicU64::new(0),
            degree_directory_sidecar_persists: AtomicU64::new(0),
            http_requests: AtomicU64::new(0),
            http_2xx: AtomicU64::new(0),
            http_4xx: AtomicU64::new(0),
            http_5xx: AtomicU64::new(0),
            http_request_read_latency: LatencyMetric::default(),
            http_json_parse_latency: LatencyMetric::default(),
            http_handler_latency: LatencyMetric::default(),
            http_serialize_latency: LatencyMetric::default(),
            http_response_write_latency: LatencyMetric::default(),
            snb_read_lock_wait_latency: LatencyMetric::default(),
            snb_read_lock_hold_latency: LatencyMetric::default(),
            snb_write_lock_wait_latency: LatencyMetric::default(),
            snb_write_lock_hold_latency: LatencyMetric::default(),
            snb_vertices_load_latency: LatencyMetric::default(),
            snb_edge_props_load_latency: LatencyMetric::default(),
            snb_adjacency_load_latency: LatencyMetric::default(),
            snb_adjacency_scan_latency: LatencyMetric::default(),
            snb_adjacency_group_latency: LatencyMetric::default(),
            snb_adjacency_write_latency: LatencyMetric::default(),
            snb_vertices_count: AtomicU64::new(0),
            snb_edge_props_count: AtomicU64::new(0),
            snb_adjacency_groups: AtomicU64::new(0),
            snb_adjacency_edges: AtomicU64::new(0),
            snb_adjacency_bytes: AtomicU64::new(0),
            snb_adjacency_cache_hits: AtomicU64::new(0),
            snb_adjacency_cache_misses: AtomicU64::new(0),
            snb_adjacency_cache_write_syscalls: AtomicU64::new(0),
            snb_adjacency_cache_write_bytes: AtomicU64::new(0),
            vertex_lookups: AtomicU64::new(0),
            edge_prop_lookups: AtomicU64::new(0),
            adjacency_lookups: AtomicU64::new(0),
            neighbor_clone_items: AtomicU64::new(0),
            storage_insert_latency: LatencyMetric::default(),
            storage_delete_latency: LatencyMetric::default(),
            storage_get_neighbors_latency: LatencyMetric::default(),
            storage_scan_edges_latency: LatencyMetric::default(),
            storage_flush_latency: LatencyMetric::default(),
            storage_compaction_latency: LatencyMetric::default(),
            storage_rebuild_index_latency: LatencyMetric::default(),
            csr_get_neighbors_latency: LatencyMetric::default(),
            csr_read_all_edges_latency: LatencyMetric::default(),
            csr_read_offsets_latency: LatencyMetric::default(),
            csr_header_reads: AtomicU64::new(0),
            csr_offset_reads: AtomicU64::new(0),
            csr_body_reads: AtomicU64::new(0),
            csr_full_scan_reads: AtomicU64::new(0),
            csr_header_bytes: AtomicU64::new(0),
            csr_offset_bytes: AtomicU64::new(0),
            csr_body_bytes: AtomicU64::new(0),
            candidate_l0_segments: AtomicU64::new(0),
            range_filtered_segments: AtomicU64::new(0),
            bloom_filtered_segments: AtomicU64::new(0),
            filter_passed_segments: AtomicU64::new(0),
            matched_l0_segments: AtomicU64::new(0),
            csr_offset_cache_hits: AtomicU64::new(0),
            csr_offset_cache_misses: AtomicU64::new(0),
            l0_bloom_false_positive_probes: AtomicU64::new(0),
            io_semaphore_wait_latency: LatencyMetric::default(),
            io_read_blocking_latency: LatencyMetric::default(),
            io_write_blocking_latency: LatencyMetric::default(),
            io_create_blocking_latency: LatencyMetric::default(),
            io_sync_blocking_latency: LatencyMetric::default(),
            io_remove_blocking_latency: LatencyMetric::default(),
            endpoint_metrics: Mutex::new(HashMap::new()),
            l0_partition_metrics: Mutex::new(HashMap::new()),
        }
    }
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

    pub fn record_http_status(&self, status: u16) {
        self.http_requests.fetch_add(1, Ordering::Relaxed);
        match status {
            200..=299 => self.http_2xx.fetch_add(1, Ordering::Relaxed),
            400..=499 => self.http_4xx.fetch_add(1, Ordering::Relaxed),
            500..=599 => self.http_5xx.fetch_add(1, Ordering::Relaxed),
            _ => 0,
        };
    }

    pub fn record_endpoint(&self, endpoint: &str, elapsed: Duration, ok: bool) {
        let metric = {
            let mut endpoints = self.endpoint_metrics.lock();
            endpoints
                .entry(endpoint.to_string())
                .or_insert_with(|| Arc::new(EndpointMetric::default()))
                .clone()
        };
        metric.record(elapsed, ok);
    }

    pub fn record_l0_partition_query(&self, key: L0PartitionKey) {
        let mut partitions = self.l0_partition_metrics.lock();
        partitions.entry(key).or_default().query_count += 1;
    }

    pub fn record_l0_partition_probe(&self, key: L0PartitionKey, probe: L0PartitionProbe) {
        let mut partitions = self.l0_partition_metrics.lock();
        let metric = partitions.entry(key).or_default();
        metric.candidate_segments += probe.candidate_segments;
        metric.range_filtered_segments += probe.range_filtered_segments;
        metric.bloom_filtered_segments += probe.bloom_filtered_segments;
        metric.filter_passed_segments += probe.filter_passed_segments;
        metric.matched_segments += probe.matched_segments;
        metric.offset_cache_hits += probe.offset_cache_hits;
        metric.offset_cache_misses += probe.offset_cache_misses;
        metric.body_reads += probe.body_reads;
        metric.bloom_false_positive_probes += probe.bloom_false_positive_probes;
    }

    pub fn l0_partition_snapshots(&self) -> Vec<L0PartitionSnapshot> {
        let mut snapshots: Vec<_> = self
            .l0_partition_metrics
            .lock()
            .iter()
            .map(|(key, metric)| metric.snapshot(*key))
            .collect();
        snapshots.sort_by_key(|snapshot| {
            (
                snapshot.key.src_label,
                snapshot.key.edge_type,
                snapshot.key.range_start,
            )
        });
        snapshots
    }

    pub fn reset(&self) {
        self.reset_counters();
        self.reset_latencies();
        for metric in self.endpoint_metrics.lock().values() {
            metric.reset();
        }
        self.l0_partition_metrics.lock().clear();
    }

    pub fn snapshot_json(&self) -> Value {
        let endpoints: serde_json::Map<String, Value> = self
            .endpoint_metrics
            .lock()
            .iter()
            .map(|(name, metric)| (name.clone(), metric.snapshot_json()))
            .collect();

        json!({
            "storage": {
                "insert_ops": self.load(&self.insert_ops),
                "delete_ops": self.load(&self.delete_ops),
                "get_neighbors_ops": self.load(&self.get_neighbors_ops),
                "scan_ops": self.load(&self.scan_ops),
                "insert_latency": self.storage_insert_latency.snapshot(),
                "delete_latency": self.storage_delete_latency.snapshot(),
                "get_neighbors_latency": self.storage_get_neighbors_latency.snapshot(),
                "scan_edges_latency": self.storage_scan_edges_latency.snapshot(),
                "flush_count": self.load(&self.flush_count),
                "flush_latency": self.storage_flush_latency.snapshot(),
                "compaction_count": self.load(&self.compaction_count),
                "compaction_latency": self.storage_compaction_latency.snapshot(),
                "compaction_input_bytes": self.load(&self.compaction_input_bytes),
                "compaction_output_bytes": self.load(&self.compaction_output_bytes),
                "degree_directory_sidecar_persists": self.load(&self.degree_directory_sidecar_persists),
                "rebuild_index_latency": self.storage_rebuild_index_latency.snapshot(),
            },
            "io": {
                "read_syscalls": self.load(&self.read_syscalls),
                "write_syscalls": self.load(&self.write_syscalls),
                "read_bytes": self.load(&self.read_bytes),
                "write_bytes": self.load(&self.write_bytes),
                "semaphore_wait_latency": self.io_semaphore_wait_latency.snapshot(),
                "read_blocking_latency": self.io_read_blocking_latency.snapshot(),
                "write_blocking_latency": self.io_write_blocking_latency.snapshot(),
                "create_blocking_latency": self.io_create_blocking_latency.snapshot(),
                "sync_blocking_latency": self.io_sync_blocking_latency.snapshot(),
                "remove_blocking_latency": self.io_remove_blocking_latency.snapshot(),
            },
            "csr": {
                "get_neighbors_latency": self.csr_get_neighbors_latency.snapshot(),
                "read_all_edges_latency": self.csr_read_all_edges_latency.snapshot(),
                "read_offsets_latency": self.csr_read_offsets_latency.snapshot(),
                "header_reads": self.load(&self.csr_header_reads),
                "offset_reads": self.load(&self.csr_offset_reads),
                "body_reads": self.load(&self.csr_body_reads),
                "full_scan_reads": self.load(&self.csr_full_scan_reads),
                "header_bytes": self.load(&self.csr_header_bytes),
                "offset_bytes": self.load(&self.csr_offset_bytes),
                "body_bytes": self.load(&self.csr_body_bytes),
                "candidate_l0_segments": self.load(&self.candidate_l0_segments),
                "range_filtered_segments": self.load(&self.range_filtered_segments),
                "bloom_filtered_segments": self.load(&self.bloom_filtered_segments),
                "filter_passed_segments": self.load(&self.filter_passed_segments),
                "matched_l0_segments": self.load(&self.matched_l0_segments),
                "offset_cache_hits": self.load(&self.csr_offset_cache_hits),
                "offset_cache_misses": self.load(&self.csr_offset_cache_misses),
                "bloom_false_positive_probes": self.load(&self.l0_bloom_false_positive_probes),
                "l0_partitions": self.l0_partition_snapshots(),
            },
            "http": {
                "requests": self.load(&self.http_requests),
                "status_2xx": self.load(&self.http_2xx),
                "status_4xx": self.load(&self.http_4xx),
                "status_5xx": self.load(&self.http_5xx),
                "request_read_latency": self.http_request_read_latency.snapshot(),
                "json_parse_latency": self.http_json_parse_latency.snapshot(),
                "handler_latency": self.http_handler_latency.snapshot(),
                "serialize_latency": self.http_serialize_latency.snapshot(),
                "response_write_latency": self.http_response_write_latency.snapshot(),
                "endpoints": endpoints,
            },
            "snb": {
                "locks": {
                    "read_wait_latency": self.snb_read_lock_wait_latency.snapshot(),
                    "read_hold_latency": self.snb_read_lock_hold_latency.snapshot(),
                    "write_wait_latency": self.snb_write_lock_wait_latency.snapshot(),
                    "write_hold_latency": self.snb_write_lock_hold_latency.snapshot(),
                },
                "cache": {
                    "vertices": self.load(&self.snb_vertices_count),
                    "edge_props": self.load(&self.snb_edge_props_count),
                    "adjacency_groups": self.load(&self.snb_adjacency_groups),
                    "adjacency_edges": self.load(&self.snb_adjacency_edges),
                    "adjacency_bytes": self.load(&self.snb_adjacency_bytes),
                    "adjacency_cache_hits": self.load(&self.snb_adjacency_cache_hits),
                    "adjacency_cache_misses": self.load(&self.snb_adjacency_cache_misses),
                    "adjacency_cache_write_syscalls": self.load(&self.snb_adjacency_cache_write_syscalls),
                    "adjacency_cache_write_bytes": self.load(&self.snb_adjacency_cache_write_bytes),
                    "vertices_load_latency": self.snb_vertices_load_latency.snapshot(),
                    "edge_props_load_latency": self.snb_edge_props_load_latency.snapshot(),
                    "adjacency_load_latency": self.snb_adjacency_load_latency.snapshot(),
                    "adjacency_scan_latency": self.snb_adjacency_scan_latency.snapshot(),
                    "adjacency_group_latency": self.snb_adjacency_group_latency.snapshot(),
                    "adjacency_write_latency": self.snb_adjacency_write_latency.snapshot(),
                },
                "helpers": {
                    "vertex_lookups": self.load(&self.vertex_lookups),
                    "edge_prop_lookups": self.load(&self.edge_prop_lookups),
                    "adjacency_lookups": self.load(&self.adjacency_lookups),
                    "neighbor_clone_items": self.load(&self.neighbor_clone_items),
                }
            }
        })
    }

    pub fn summary_json(&self) -> Value {
        json!({
            "http_requests": self.load(&self.http_requests),
            "http_5xx": self.load(&self.http_5xx),
            "endpoint_count": self.endpoint_metrics.lock().len(),
            "read_bytes": self.load(&self.read_bytes),
            "write_bytes": self.load(&self.write_bytes),
            "storage_get_neighbors_p99_us": self.storage_get_neighbors_latency.snapshot().p99_us,
            "http_handler_p99_us": self.http_handler_latency.snapshot().p99_us,
            "snb_read_lock_wait_p99_us": self.snb_read_lock_wait_latency.snapshot().p99_us,
        })
    }

    fn load(&self, counter: &AtomicU64) -> u64 {
        counter.load(Ordering::Relaxed)
    }

    fn reset_counters(&self) {
        for counter in [
            &self.insert_ops,
            &self.delete_ops,
            &self.get_neighbors_ops,
            &self.scan_ops,
            &self.read_syscalls,
            &self.write_syscalls,
            &self.read_bytes,
            &self.write_bytes,
            &self.flush_count,
            &self.compaction_count,
            &self.compaction_input_bytes,
            &self.compaction_output_bytes,
            &self.degree_directory_sidecar_persists,
            &self.http_requests,
            &self.http_2xx,
            &self.http_4xx,
            &self.http_5xx,
            &self.snb_vertices_count,
            &self.snb_edge_props_count,
            &self.snb_adjacency_groups,
            &self.snb_adjacency_edges,
            &self.snb_adjacency_bytes,
            &self.snb_adjacency_cache_hits,
            &self.snb_adjacency_cache_misses,
            &self.snb_adjacency_cache_write_syscalls,
            &self.snb_adjacency_cache_write_bytes,
            &self.vertex_lookups,
            &self.edge_prop_lookups,
            &self.adjacency_lookups,
            &self.neighbor_clone_items,
            &self.csr_header_reads,
            &self.csr_offset_reads,
            &self.csr_body_reads,
            &self.csr_full_scan_reads,
            &self.csr_header_bytes,
            &self.csr_offset_bytes,
            &self.csr_body_bytes,
            &self.candidate_l0_segments,
            &self.range_filtered_segments,
            &self.bloom_filtered_segments,
            &self.filter_passed_segments,
            &self.matched_l0_segments,
            &self.csr_offset_cache_hits,
            &self.csr_offset_cache_misses,
            &self.l0_bloom_false_positive_probes,
        ] {
            counter.store(0, Ordering::Relaxed);
        }
    }

    fn reset_latencies(&self) {
        for latency in [
            &self.http_request_read_latency,
            &self.http_json_parse_latency,
            &self.http_handler_latency,
            &self.http_serialize_latency,
            &self.http_response_write_latency,
            &self.snb_read_lock_wait_latency,
            &self.snb_read_lock_hold_latency,
            &self.snb_write_lock_wait_latency,
            &self.snb_write_lock_hold_latency,
            &self.snb_vertices_load_latency,
            &self.snb_edge_props_load_latency,
            &self.snb_adjacency_load_latency,
            &self.snb_adjacency_scan_latency,
            &self.snb_adjacency_group_latency,
            &self.snb_adjacency_write_latency,
            &self.storage_insert_latency,
            &self.storage_delete_latency,
            &self.storage_get_neighbors_latency,
            &self.storage_scan_edges_latency,
            &self.storage_flush_latency,
            &self.storage_compaction_latency,
            &self.storage_rebuild_index_latency,
            &self.csr_get_neighbors_latency,
            &self.csr_read_all_edges_latency,
            &self.csr_read_offsets_latency,
            &self.io_semaphore_wait_latency,
            &self.io_read_blocking_latency,
            &self.io_write_blocking_latency,
            &self.io_create_blocking_latency,
            &self.io_sync_blocking_latency,
            &self.io_remove_blocking_latency,
        ] {
            latency.reset();
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
pub struct L0PartitionKey {
    pub src_label: i32,
    pub edge_type: EdgeType,
    pub range_start: VertexId,
    pub range_end: VertexId,
}

#[derive(Debug, Clone, Copy, Default)]
pub struct L0PartitionProbe {
    pub candidate_segments: u64,
    pub range_filtered_segments: u64,
    pub bloom_filtered_segments: u64,
    pub filter_passed_segments: u64,
    pub matched_segments: u64,
    pub offset_cache_hits: u64,
    pub offset_cache_misses: u64,
    pub body_reads: u64,
    pub bloom_false_positive_probes: u64,
}

#[derive(Debug, Clone, Serialize)]
pub struct L0PartitionSnapshot {
    pub key: L0PartitionKey,
    pub query_count: u64,
    pub candidate_segments: u64,
    pub range_filtered_segments: u64,
    pub bloom_filtered_segments: u64,
    pub filter_passed_segments: u64,
    pub matched_segments: u64,
    pub offset_cache_hits: u64,
    pub offset_cache_misses: u64,
    pub body_reads: u64,
    pub bloom_false_positive_probes: u64,
    pub avg_candidate_segments: f64,
    pub offset_cache_miss_rate: f64,
}

#[derive(Debug, Default)]
struct L0PartitionMetric {
    query_count: u64,
    candidate_segments: u64,
    range_filtered_segments: u64,
    bloom_filtered_segments: u64,
    filter_passed_segments: u64,
    matched_segments: u64,
    offset_cache_hits: u64,
    offset_cache_misses: u64,
    body_reads: u64,
    bloom_false_positive_probes: u64,
}

impl L0PartitionMetric {
    fn snapshot(&self, key: L0PartitionKey) -> L0PartitionSnapshot {
        let cache_probes = self.offset_cache_hits + self.offset_cache_misses;
        L0PartitionSnapshot {
            key,
            query_count: self.query_count,
            candidate_segments: self.candidate_segments,
            range_filtered_segments: self.range_filtered_segments,
            bloom_filtered_segments: self.bloom_filtered_segments,
            filter_passed_segments: self.filter_passed_segments,
            matched_segments: self.matched_segments,
            offset_cache_hits: self.offset_cache_hits,
            offset_cache_misses: self.offset_cache_misses,
            body_reads: self.body_reads,
            bloom_false_positive_probes: self.bloom_false_positive_probes,
            avg_candidate_segments: if self.query_count == 0 {
                0.0
            } else {
                self.candidate_segments as f64 / self.query_count as f64
            },
            offset_cache_miss_rate: if cache_probes == 0 {
                0.0
            } else {
                self.offset_cache_misses as f64 / cache_probes as f64
            },
        }
    }
}

#[derive(Debug)]
pub struct LatencyMetric {
    count: AtomicU64,
    sum_us: AtomicU64,
    min_us: AtomicU64,
    max_us: AtomicU64,
    buckets: [AtomicU64; LATENCY_BUCKET_US.len()],
}

impl Default for LatencyMetric {
    fn default() -> Self {
        Self {
            count: AtomicU64::new(0),
            sum_us: AtomicU64::new(0),
            min_us: AtomicU64::new(u64::MAX),
            max_us: AtomicU64::new(0),
            buckets: std::array::from_fn(|_| AtomicU64::new(0)),
        }
    }
}

impl LatencyMetric {
    pub fn record_since(&self, started: Instant) {
        self.record(started.elapsed());
    }

    pub fn record(&self, elapsed: Duration) {
        self.record_us(duration_to_us(elapsed));
    }

    pub fn record_us(&self, elapsed_us: u64) {
        self.count.fetch_add(1, Ordering::Relaxed);
        self.sum_us.fetch_add(elapsed_us, Ordering::Relaxed);
        fetch_min(&self.min_us, elapsed_us);
        fetch_max(&self.max_us, elapsed_us);
        if let Some(idx) = LATENCY_BUCKET_US
            .iter()
            .position(|upper| elapsed_us <= *upper)
        {
            self.buckets[idx].fetch_add(1, Ordering::Relaxed);
        }
    }

    pub fn reset(&self) {
        self.count.store(0, Ordering::Relaxed);
        self.sum_us.store(0, Ordering::Relaxed);
        self.min_us.store(u64::MAX, Ordering::Relaxed);
        self.max_us.store(0, Ordering::Relaxed);
        for bucket in &self.buckets {
            bucket.store(0, Ordering::Relaxed);
        }
    }

    pub fn snapshot(&self) -> LatencySnapshot {
        let count = self.count.load(Ordering::Relaxed);
        let sum_us = self.sum_us.load(Ordering::Relaxed);
        let min_us = self.min_us.load(Ordering::Relaxed);
        let max_us = self.max_us.load(Ordering::Relaxed);
        let buckets = self.bucket_snapshots();
        LatencySnapshot {
            count,
            sum_us,
            min_us: if count == 0 { 0 } else { min_us },
            max_us: if count == 0 { 0 } else { max_us },
            avg_us: if count == 0 { 0 } else { sum_us / count },
            p50_us: percentile_from_buckets(&buckets, count, 0.50),
            p90_us: percentile_from_buckets(&buckets, count, 0.90),
            p99_us: percentile_from_buckets(&buckets, count, 0.99),
            buckets,
        }
    }

    fn bucket_snapshots(&self) -> Vec<BucketSnapshot> {
        LATENCY_BUCKET_US
            .iter()
            .zip(self.buckets.iter())
            .map(|(upper_bound_us, count)| BucketSnapshot {
                upper_bound_us: *upper_bound_us,
                count: count.load(Ordering::Relaxed),
            })
            .collect()
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct LatencySnapshot {
    pub count: u64,
    pub sum_us: u64,
    pub min_us: u64,
    pub max_us: u64,
    pub avg_us: u64,
    pub p50_us: u64,
    pub p90_us: u64,
    pub p99_us: u64,
    pub buckets: Vec<BucketSnapshot>,
}

#[derive(Debug, Clone, Serialize)]
pub struct BucketSnapshot {
    pub upper_bound_us: u64,
    pub count: u64,
}

#[derive(Debug, Default)]
struct EndpointMetric {
    count: AtomicU64,
    errors: AtomicU64,
    latency: LatencyMetric,
}

impl EndpointMetric {
    fn record(&self, elapsed: Duration, ok: bool) {
        self.count.fetch_add(1, Ordering::Relaxed);
        if !ok {
            self.errors.fetch_add(1, Ordering::Relaxed);
        }
        self.latency.record(elapsed);
    }

    fn reset(&self) {
        self.count.store(0, Ordering::Relaxed);
        self.errors.store(0, Ordering::Relaxed);
        self.latency.reset();
    }

    fn snapshot_json(&self) -> Value {
        json!({
            "count": self.count.load(Ordering::Relaxed),
            "errors": self.errors.load(Ordering::Relaxed),
            "latency": self.latency.snapshot(),
        })
    }
}

fn duration_to_us(duration: Duration) -> u64 {
    u64::try_from(duration.as_micros()).unwrap_or(u64::MAX)
}

fn percentile_from_buckets(buckets: &[BucketSnapshot], count: u64, percentile: f64) -> u64 {
    if count == 0 {
        return 0;
    }
    let target = ((count as f64) * percentile).ceil() as u64;
    let target = target.max(1);
    let mut seen = 0u64;
    for bucket in buckets {
        seen += bucket.count;
        if seen >= target {
            return bucket.upper_bound_us;
        }
    }
    buckets.last().map(|b| b.upper_bound_us).unwrap_or(0)
}

fn fetch_min(target: &AtomicU64, value: u64) {
    let mut current = target.load(Ordering::Relaxed);
    while value < current {
        match target.compare_exchange_weak(current, value, Ordering::Relaxed, Ordering::Relaxed) {
            Ok(_) => return,
            Err(next) => current = next,
        }
    }
}

fn fetch_max(target: &AtomicU64, value: u64) {
    let mut current = target.load(Ordering::Relaxed);
    while value > current {
        match target.compare_exchange_weak(current, value, Ordering::Relaxed, Ordering::Relaxed) {
            Ok(_) => return,
            Err(next) => current = next,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn latency_snapshot_uses_buckets() {
        let metric = LatencyMetric::default();
        metric.record_us(10);
        metric.record_us(2_500);
        metric.record_us(80_000);

        let snapshot = metric.snapshot();
        assert_eq!(snapshot.count, 3);
        assert_eq!(snapshot.min_us, 10);
        assert_eq!(snapshot.max_us, 80_000);
        assert_eq!(snapshot.p50_us, 3_000);
        assert_eq!(snapshot.p90_us, 100_000);
        assert_eq!(snapshot.p99_us, 100_000);
    }

    #[test]
    fn metrics_reset_clears_counters_and_endpoints() {
        let metrics = Metrics::default();
        metrics.add_read(128);
        metrics.record_http_status(200);
        metrics.record_endpoint(
            "/query/interactive_complex_1",
            Duration::from_micros(400),
            true,
        );
        metrics.storage_get_neighbors_latency.record_us(900);

        let before = metrics.snapshot_json();
        assert_eq!(before["io"]["read_bytes"], 128);
        assert_eq!(before["http"]["requests"], 1);
        assert_eq!(
            before["http"]["endpoints"]["/query/interactive_complex_1"]["count"],
            1
        );

        metrics.reset();
        let after = metrics.snapshot_json();
        assert_eq!(after["io"]["read_bytes"], 0);
        assert_eq!(after["http"]["requests"], 0);
        assert_eq!(after["storage"]["get_neighbors_latency"]["count"], 0);
        assert_eq!(
            after["http"]["endpoints"]["/query/interactive_complex_1"]["count"],
            0
        );
    }
}
