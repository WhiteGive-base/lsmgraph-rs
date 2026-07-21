use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::Ordering;

use anyhow::{ensure, Context, Result};
use clap::{Parser, ValueEnum};
use lsmgraph::config::{L0LayoutPolicy, LsmGraphConfig};
use lsmgraph::graph::Engine;
use lsmgraph::types::{source_label_from_vertex_id, EdgeType, VertexId};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

const FNV_OFFSET: u64 = 0xcbf29ce484222325;
const FNV_PRIME: u64 = 0x100000001b3;

#[derive(Parser, Debug)]
#[command(
    name = "p40-fixed-trace",
    about = "Deterministic P40 fixed-trace replay with per-query correctness digests"
)]
struct Cli {
    #[arg(long)]
    data_dir: PathBuf,
    #[arg(long)]
    trace_in: PathBuf,
    #[arg(long)]
    output: PathBuf,
    #[arg(long, value_enum)]
    policy: Policy,
    #[arg(long, value_enum, default_value = "create")]
    store_mode: StoreMode,
    #[arg(long, default_value_t = 4)]
    capacity_l0_files: usize,
    #[arg(long, default_value_t = 1 << 20)]
    static_range_bucket_size: u64,
    #[arg(long, default_value_t = 64 * 1024 * 1024)]
    memgraph_bytes: usize,
    #[arg(long, default_value_t = 4096)]
    metadata_cache_entries: usize,
}

#[derive(ValueEnum, Debug, Clone, Copy, PartialEq, Eq)]
enum Policy {
    None,
    CapacityNaive,
    SemanticStatic,
    SemanticFeedback,
}

impl Policy {
    fn as_str(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::CapacityNaive => "capacity-naive",
            Self::SemanticStatic => "semantic-static",
            Self::SemanticFeedback => "semantic-feedback",
        }
    }
}

#[derive(ValueEnum, Debug, Clone, Copy)]
enum StoreMode {
    Create,
    Open,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
enum TraceRecord {
    Insert {
        seq: u64,
        src: VertexId,
        dst: VertexId,
        edge_type: EdgeType,
    },
    Delete {
        seq: u64,
        src: VertexId,
        dst: VertexId,
        edge_type: EdgeType,
    },
    Flush {
        seq: u64,
    },
    Query {
        seq: u64,
        src: VertexId,
        edge_type: EdgeType,
    },
    Maintenance {
        seq: u64,
    },
}

impl TraceRecord {
    fn seq(&self) -> u64 {
        match self {
            Self::Insert { seq, .. }
            | Self::Delete { seq, .. }
            | Self::Flush { seq }
            | Self::Query { seq, .. }
            | Self::Maintenance { seq } => *seq,
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize)]
struct StaticTarget {
    src_label: i32,
    edge_type: EdgeType,
    range_start: VertexId,
    range_end: VertexId,
}

#[tokio::main(flavor = "multi_thread")]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    ensure!(
        cli.capacity_l0_files > 0,
        "--capacity-l0-files must be positive"
    );
    ensure!(
        cli.static_range_bucket_size > 0,
        "--static-range-bucket-size must be positive"
    );

    let (trace, trace_sequence_digest) = load_trace(&cli.trace_in)?;
    ensure!(!trace.is_empty(), "trace is empty");
    ensure!(
        trace
            .iter()
            .any(|record| matches!(record, TraceRecord::Query { .. })),
        "trace must contain at least one query"
    );
    ensure!(
        trace
            .iter()
            .any(|record| matches!(record, TraceRecord::Maintenance { .. })),
        "trace must contain at least one maintenance event"
    );
    let static_target = static_target_from_trace(&trace, cli.static_range_bucket_size)?;

    if let Some(parent) = cli.output.parent() {
        fs::create_dir_all(parent)
            .with_context(|| format!("create output parent {}", parent.display()))?;
    }
    let output = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&cli.output)
        .with_context(|| format!("create output {}", cli.output.display()))?;
    let mut out = BufWriter::new(output);

    let mut config = LsmGraphConfig::new(&cli.data_dir)
        .with_l0_layout(L0LayoutPolicy::Semantic)
        .with_memgraph_capacity(cli.memgraph_bytes)
        .with_metadata_cache_entries(cli.metadata_cache_entries);
    config.l0_ra_range_bucket_size = cli.static_range_bucket_size;
    config.l0_ra_min_queries = 1;
    config.l0_ra_min_l0_segments = 1;
    config.l0_ra_min_score = 0.0;
    config.auto_compaction = false;

    let engine = match cli.store_mode {
        StoreMode::Create => {
            ensure!(
                !cli.data_dir.exists(),
                "create mode refuses an existing store: {}",
                cli.data_dir.display()
            );
            Engine::create(config).await?
        }
        StoreMode::Open => {
            ensure!(
                cli.data_dir.is_dir(),
                "open mode requires an existing store: {}",
                cli.data_dir.display()
            );
            Engine::open(config).await?
        }
    };

    write_json_line(
        &mut out,
        &json!({
            "event": "start",
            "runner": "p40-fixed-trace",
            "policy": cli.policy.as_str(),
            "store_mode": format!("{:?}", cli.store_mode).to_ascii_lowercase(),
            "data_dir": cli.data_dir,
            "trace_in": cli.trace_in,
            "trace_operations": trace.len(),
            "trace_sequence_digest": format!("{trace_sequence_digest:016x}"),
            "capacity_l0_files": cli.capacity_l0_files,
            "static_target": static_target,
            "performance_eligible": false
        }),
    )?;

    let mut query_count = 0u64;
    let mut query_digest = FNV_OFFSET;
    let mut inserts = 0u64;
    let mut deletes = 0u64;
    let mut flushes = 0u64;
    let mut maintenance_events = 0u64;
    let mut maintenance_actions = 0u64;

    for record in &trace {
        match *record {
            TraceRecord::Insert {
                seq,
                src,
                dst,
                edge_type,
            } => {
                engine.insert_edge(src, dst, edge_type).await?;
                inserts += 1;
                write_json_line(
                    &mut out,
                    &json!({
                        "event": "insert",
                        "seq": seq,
                        "policy": cli.policy.as_str(),
                        "src": src,
                        "dst": dst,
                        "edge_type": edge_type
                    }),
                )?;
            }
            TraceRecord::Delete {
                seq,
                src,
                dst,
                edge_type,
            } => {
                engine.delete_edge(src, dst, edge_type).await?;
                deletes += 1;
                write_json_line(
                    &mut out,
                    &json!({
                        "event": "delete",
                        "seq": seq,
                        "policy": cli.policy.as_str(),
                        "src": src,
                        "dst": dst,
                        "edge_type": edge_type
                    }),
                )?;
            }
            TraceRecord::Flush { seq } => {
                engine.flush_active().await?;
                engine.wait_for_flushes().await?;
                flushes += 1;
                write_json_line(
                    &mut out,
                    &json!({
                        "event": "flush",
                        "seq": seq,
                        "policy": cli.policy.as_str(),
                        "levels": engine.live_file_count_by_level()
                    }),
                )?;
            }
            TraceRecord::Query {
                seq,
                src,
                edge_type,
            } => {
                let metrics = engine.metrics();
                let candidate_before = metrics.candidate_l0_segments.load(Ordering::Relaxed);
                let neighbors = engine
                    .get_neighbors_typed(src, edge_type, engine.current_snapshot())
                    .await?;
                let mut neighbor_ids: Vec<VertexId> =
                    neighbors.into_iter().map(|edge| edge.dst).collect();
                neighbor_ids.sort_unstable();
                let candidate_after = metrics.candidate_l0_segments.load(Ordering::Relaxed);
                let result_digest = digest_query_result(seq, src, edge_type, &neighbor_ids);
                query_digest = digest_query_summary(
                    query_digest,
                    seq,
                    neighbor_ids.len() as u64,
                    result_digest,
                );
                query_count += 1;
                write_json_line(
                    &mut out,
                    &json!({
                        "event": "query",
                        "seq": seq,
                        "policy": cli.policy.as_str(),
                        "query_ordinal": query_count,
                        "src": src,
                        "edge_type": edge_type,
                        "result_count": neighbor_ids.len(),
                        "result_digest": format!("{result_digest:016x}"),
                        "candidate_l0_segments": candidate_after.saturating_sub(candidate_before)
                    }),
                )?;
            }
            TraceRecord::Maintenance { seq } => {
                engine.wait_for_flushes().await?;
                maintenance_events += 1;
                let levels_before = engine.live_file_count_by_level();
                let metrics_before = engine.metrics().snapshot_json();
                let (performed, decision) =
                    apply_maintenance(cli.policy, &engine, cli.capacity_l0_files, static_target)
                        .await?;
                let levels_after = engine.live_file_count_by_level();
                let metrics_after = engine.metrics().snapshot_json();
                if performed {
                    maintenance_actions += 1;
                }
                write_json_line(
                    &mut out,
                    &json!({
                        "event": "maintenance",
                        "seq": seq,
                        "policy": cli.policy.as_str(),
                        "performed": performed,
                        "decision": decision,
                        "levels_before": levels_before,
                        "levels_after": levels_after,
                        "compaction_count_delta": metric_delta(&metrics_after, &metrics_before, &["storage", "compaction_count"]),
                        "compaction_input_bytes_delta": metric_delta(&metrics_after, &metrics_before, &["storage", "compaction_input_bytes"]),
                        "compaction_output_bytes_delta": metric_delta(&metrics_after, &metrics_before, &["storage", "compaction_output_bytes"])
                    }),
                )?;
            }
        }
    }

    engine.wait_for_flushes().await?;
    write_json_line(
        &mut out,
        &json!({
            "event": "done",
            "policy": cli.policy.as_str(),
            "trace_operations": trace.len(),
            "trace_sequence_digest": format!("{trace_sequence_digest:016x}"),
            "queries": query_count,
            "query_digest": format!("{query_digest:016x}"),
            "inserts": inserts,
            "deletes": deletes,
            "flushes": flushes,
            "maintenance_events": maintenance_events,
            "maintenance_actions": maintenance_actions,
            "levels_final": engine.live_file_count_by_level(),
            "metrics_final": engine.metrics().snapshot_json(),
            "performance_eligible": false
        }),
    )?;
    out.flush()?;
    Ok(())
}

async fn apply_maintenance(
    policy: Policy,
    engine: &std::sync::Arc<Engine>,
    capacity_l0_files: usize,
    static_target: StaticTarget,
) -> Result<(bool, Value)> {
    match policy {
        Policy::None => Ok((false, json!({"kind": "none"}))),
        Policy::CapacityNaive => {
            let l0_files = engine
                .live_file_count_by_level()
                .first()
                .copied()
                .unwrap_or_default();
            if l0_files < capacity_l0_files {
                return Ok((
                    false,
                    json!({
                        "kind": "capacity-naive",
                        "l0_files": l0_files,
                        "capacity_l0_files": capacity_l0_files,
                        "reason": "below_capacity"
                    }),
                ));
            }
            let output = engine.compact_l0_to_l1().await?;
            Ok((
                output.is_some(),
                json!({
                    "kind": "capacity-naive",
                    "l0_files": l0_files,
                    "capacity_l0_files": capacity_l0_files,
                    "output": output
                }),
            ))
        }
        Policy::SemanticStatic => {
            let outputs = engine
                .compact_l0_range_to_l1(
                    static_target.src_label,
                    Some(static_target.edge_type),
                    static_target.range_start,
                    static_target.range_end,
                )
                .await?;
            Ok((
                !outputs.is_empty(),
                json!({
                    "kind": "semantic-static",
                    "target": static_target,
                    "outputs": outputs
                }),
            ))
        }
        Policy::SemanticFeedback => {
            let decision = engine.compact_best_l0_partition_by_score().await?;
            Ok((
                decision.is_some(),
                json!({
                    "kind": "semantic-feedback",
                    "decision": decision
                }),
            ))
        }
    }
}

fn static_target_from_trace(trace: &[TraceRecord], bucket_size: u64) -> Result<StaticTarget> {
    let (src, edge_type) = trace
        .iter()
        .find_map(|record| match record {
            TraceRecord::Query { src, edge_type, .. } => Some((*src, *edge_type)),
            _ => None,
        })
        .context("trace has no query for static semantic target")?;
    let range_start = (src / bucket_size) * bucket_size;
    Ok(StaticTarget {
        src_label: source_label_from_vertex_id(src),
        edge_type,
        range_start,
        range_end: range_start.saturating_add(bucket_size - 1),
    })
}

fn load_trace(path: &Path) -> Result<(Vec<TraceRecord>, u64)> {
    let file = File::open(path).with_context(|| format!("open trace {}", path.display()))?;
    parse_trace_reader(BufReader::new(file))
}

fn parse_trace_reader(reader: impl BufRead) -> Result<(Vec<TraceRecord>, u64)> {
    let mut records = Vec::new();
    let mut digest = FNV_OFFSET;
    for (line_index, line) in reader.lines().enumerate() {
        let line = line.with_context(|| format!("read trace line {}", line_index + 1))?;
        ensure!(
            !line.trim().is_empty(),
            "blank trace line {}",
            line_index + 1
        );
        digest = fnv1a_update(digest, line.as_bytes());
        digest = fnv1a_update(digest, b"\n");
        let record: TraceRecord = serde_json::from_str(&line)
            .with_context(|| format!("parse trace line {}", line_index + 1))?;
        ensure!(
            record.seq() == records.len() as u64,
            "trace sequence gap at line {}: got seq {}, expected {}",
            line_index + 1,
            record.seq(),
            records.len()
        );
        records.push(record);
    }
    Ok((records, digest))
}

fn digest_query_result(
    seq: u64,
    src: VertexId,
    edge_type: EdgeType,
    sorted_neighbors: &[VertexId],
) -> u64 {
    let mut digest = FNV_OFFSET;
    digest = fnv1a_update(digest, &seq.to_le_bytes());
    digest = fnv1a_update(digest, &src.to_le_bytes());
    digest = fnv1a_update(digest, &edge_type.to_le_bytes());
    digest = fnv1a_update(digest, &(sorted_neighbors.len() as u64).to_le_bytes());
    for neighbor in sorted_neighbors {
        digest = fnv1a_update(digest, &neighbor.to_le_bytes());
    }
    digest
}

fn digest_query_summary(mut digest: u64, seq: u64, count: u64, result_digest: u64) -> u64 {
    digest = fnv1a_update(digest, &seq.to_le_bytes());
    digest = fnv1a_update(digest, &count.to_le_bytes());
    fnv1a_update(digest, &result_digest.to_le_bytes())
}

fn fnv1a_update(mut digest: u64, bytes: &[u8]) -> u64 {
    for byte in bytes {
        digest ^= u64::from(*byte);
        digest = digest.wrapping_mul(FNV_PRIME);
    }
    digest
}

fn metric_path(value: &Value, path: &[&str]) -> u64 {
    let mut current = value;
    for component in path {
        current = &current[*component];
    }
    current.as_u64().unwrap_or_default()
}

fn metric_delta(after: &Value, before: &Value, path: &[&str]) -> u64 {
    metric_path(after, path).saturating_sub(metric_path(before, path))
}

fn write_json_line(writer: &mut impl Write, value: &Value) -> Result<()> {
    serde_json::to_writer(&mut *writer, value)?;
    writer.write_all(b"\n")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn trace_sequence_is_strict_and_digest_is_stable() {
        let input = concat!(
            "{\"op\":\"insert\",\"seq\":0,\"src\":1,\"dst\":2,\"edge_type\":1}\n",
            "{\"op\":\"query\",\"seq\":1,\"src\":1,\"edge_type\":1}\n",
            "{\"op\":\"maintenance\",\"seq\":2}\n"
        );
        let (first, first_digest) = parse_trace_reader(Cursor::new(input)).unwrap();
        let (second, second_digest) = parse_trace_reader(Cursor::new(input)).unwrap();
        assert_eq!(first.len(), 3);
        assert_eq!(second.len(), 3);
        assert_eq!(first_digest, second_digest);
    }

    #[test]
    fn trace_sequence_gap_is_rejected() {
        let input = "{\"op\":\"query\",\"seq\":1,\"src\":1,\"edge_type\":1}\n";
        assert!(parse_trace_reader(Cursor::new(input)).is_err());
    }

    #[test]
    fn sorted_neighbor_digest_changes_with_result() {
        let a = digest_query_result(1, 10, 1, &[2, 3, 4]);
        let b = digest_query_result(1, 10, 1, &[2, 3, 5]);
        assert_ne!(a, b);
        assert_eq!(a, digest_query_result(1, 10, 1, &[2, 3, 4]));
    }
}
