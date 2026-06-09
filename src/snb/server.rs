use std::collections::{BTreeMap, HashMap};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Instant;

use parking_lot::Mutex;
use serde_json::{json, Value};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::RwLock;

use crate::error::Result;
use crate::metrics::LatencyMetric;
use crate::snb::props::SnbGraph;
use crate::snb::queries::{fallback_hits_value, run_dgs_http_query, run_dgs_http_update};

const SLOW_LOCK_WAIT_US: u64 = 5_000;
const SLOW_EXEC_US: u64 = 50_000;

pub async fn start_dgs_compatible_server(snb: SnbGraph, bind: &str) -> Result<()> {
    let snb = Arc::new(RwLock::new(snb));
    let metrics = Arc::new(ServerMetrics::default());
    let listener = TcpListener::bind(bind).await?;
    println!("{{\"server\":\"lsmgraph-snb\",\"bind\":\"{bind}\"}}");
    loop {
        let (stream, _) = listener.accept().await?;
        let snb = snb.clone();
        let metrics = metrics.clone();
        tokio::spawn(async move {
            if let Err(err) = handle_connection(stream, snb, metrics).await {
                eprintln!("snb-server connection error: {err:#}");
            }
        });
    }
}

#[derive(Default)]
struct ServerMetrics {
    requests: AtomicU64,
    queries: AtomicU64,
    updates: AtomicU64,
    errors: AtomicU64,
    slow_requests: AtomicU64,
    read_lock_wait_us: AtomicU64,
    write_lock_wait_us: AtomicU64,
    max_read_lock_wait_us: AtomicU64,
    max_write_lock_wait_us: AtomicU64,
    query_exec_us: AtomicU64,
    update_exec_us: AtomicU64,
    max_query_exec_us: AtomicU64,
    max_update_exec_us: AtomicU64,
    // LatencyMetric fields for percentiles
    read_lock_wait_latency: LatencyMetric,
    write_lock_wait_latency: LatencyMetric,
    query_exec_latency: LatencyMetric,
    update_exec_latency: LatencyMetric,
    endpoints: Mutex<HashMap<String, Arc<EndpointMetrics>>>,
}

#[derive(Default)]
struct EndpointMetrics {
    requests: AtomicU64,
    queries: AtomicU64,
    updates: AtomicU64,
    errors: AtomicU64,
    slow_requests: AtomicU64,
    lock_wait_us: AtomicU64,
    max_lock_wait_us: AtomicU64,
    exec_us: AtomicU64,
    max_exec_us: AtomicU64,
}

impl ServerMetrics {
    fn reset(&self) {
        for counter in [
            &self.requests,
            &self.queries,
            &self.updates,
            &self.errors,
            &self.slow_requests,
            &self.read_lock_wait_us,
            &self.write_lock_wait_us,
            &self.max_read_lock_wait_us,
            &self.max_write_lock_wait_us,
            &self.query_exec_us,
            &self.update_exec_us,
            &self.max_query_exec_us,
            &self.max_update_exec_us,
        ] {
            counter.store(0, Ordering::Relaxed);
        }
        self.read_lock_wait_latency.reset();
        self.write_lock_wait_latency.reset();
        self.query_exec_latency.reset();
        self.update_exec_latency.reset();
        self.endpoints.lock().clear();
    }

    fn snapshot(&self) -> Value {
        let queries = self.queries.load(Ordering::Relaxed);
        let updates = self.updates.load(Ordering::Relaxed);
        let read_lock_snapshot = self.read_lock_wait_latency.snapshot();
        let write_lock_snapshot = self.write_lock_wait_latency.snapshot();
        let query_exec_snapshot = self.query_exec_latency.snapshot();
        let update_exec_snapshot = self.update_exec_latency.snapshot();
        let endpoints = self.endpoint_snapshots();
        json!({
            "requests": self.requests.load(Ordering::Relaxed),
            "queries": queries,
            "updates": updates,
            "errors": self.errors.load(Ordering::Relaxed),
            "slow_requests": self.slow_requests.load(Ordering::Relaxed),
            "read_lock_wait_us_total": self.read_lock_wait_us.load(Ordering::Relaxed),
            "write_lock_wait_us_total": self.write_lock_wait_us.load(Ordering::Relaxed),
            "read_lock_wait_us_avg": avg(self.read_lock_wait_us.load(Ordering::Relaxed), queries),
            "write_lock_wait_us_avg": avg(self.write_lock_wait_us.load(Ordering::Relaxed), updates),
            "read_lock_wait_us_max": self.max_read_lock_wait_us.load(Ordering::Relaxed),
            "write_lock_wait_us_max": self.max_write_lock_wait_us.load(Ordering::Relaxed),
            "query_exec_us_avg": avg(self.query_exec_us.load(Ordering::Relaxed), queries),
            "update_exec_us_avg": avg(self.update_exec_us.load(Ordering::Relaxed), updates),
            "query_exec_us_max": self.max_query_exec_us.load(Ordering::Relaxed),
            "update_exec_us_max": self.max_update_exec_us.load(Ordering::Relaxed),
            "read_lock_wait_p50_us": read_lock_snapshot.p50_us,
            "read_lock_wait_p90_us": read_lock_snapshot.p90_us,
            "read_lock_wait_p99_us": read_lock_snapshot.p99_us,
            "write_lock_wait_p50_us": write_lock_snapshot.p50_us,
            "write_lock_wait_p90_us": write_lock_snapshot.p90_us,
            "write_lock_wait_p99_us": write_lock_snapshot.p99_us,
            "query_exec_p50_us": query_exec_snapshot.p50_us,
            "query_exec_p95_us": query_exec_snapshot.p90_us,
            "query_exec_p99_us": query_exec_snapshot.p99_us,
            "update_exec_p50_us": update_exec_snapshot.p50_us,
            "update_exec_p95_us": update_exec_snapshot.p90_us,
            "update_exec_p99_us": update_exec_snapshot.p99_us,
            "endpoints": endpoints,
        })
    }

    fn record_query(&self, path: &str, lock_wait_us: u64, exec_us: u64, is_update: bool, ok: bool) {
        self.requests.fetch_add(1, Ordering::Relaxed);
        if !ok {
            self.errors.fetch_add(1, Ordering::Relaxed);
        }
        let is_slow = is_slow_request(lock_wait_us, exec_us);
        if is_slow {
            self.slow_requests.fetch_add(1, Ordering::Relaxed);
        }
        if is_update {
            self.updates.fetch_add(1, Ordering::Relaxed);
            self.write_lock_wait_us
                .fetch_add(lock_wait_us, Ordering::Relaxed);
            self.update_exec_us.fetch_add(exec_us, Ordering::Relaxed);
            update_max(&self.max_write_lock_wait_us, lock_wait_us);
            update_max(&self.max_update_exec_us, exec_us);
            self.write_lock_wait_latency.record_us(lock_wait_us);
            self.update_exec_latency.record_us(exec_us);
        } else {
            self.queries.fetch_add(1, Ordering::Relaxed);
            self.read_lock_wait_us
                .fetch_add(lock_wait_us, Ordering::Relaxed);
            self.query_exec_us.fetch_add(exec_us, Ordering::Relaxed);
            update_max(&self.max_read_lock_wait_us, lock_wait_us);
            update_max(&self.max_query_exec_us, exec_us);
            self.read_lock_wait_latency.record_us(lock_wait_us);
            self.query_exec_latency.record_us(exec_us);
        }
        let endpoint = {
            let mut endpoints = self.endpoints.lock();
            endpoints
                .entry(path.to_string())
                .or_insert_with(|| Arc::new(EndpointMetrics::default()))
                .clone()
        };
        endpoint.record(lock_wait_us, exec_us, is_update, ok, is_slow);
    }

    fn endpoint_snapshots(&self) -> BTreeMap<String, Value> {
        let endpoints = self.endpoints.lock();
        endpoints
            .iter()
            .map(|(path, metric)| (path.clone(), metric.snapshot()))
            .collect()
    }
}

impl EndpointMetrics {
    fn record(&self, lock_wait_us: u64, exec_us: u64, is_update: bool, ok: bool, is_slow: bool) {
        self.requests.fetch_add(1, Ordering::Relaxed);
        if is_update {
            self.updates.fetch_add(1, Ordering::Relaxed);
        } else {
            self.queries.fetch_add(1, Ordering::Relaxed);
        }
        if !ok {
            self.errors.fetch_add(1, Ordering::Relaxed);
        }
        if is_slow {
            self.slow_requests.fetch_add(1, Ordering::Relaxed);
        }
        self.lock_wait_us.fetch_add(lock_wait_us, Ordering::Relaxed);
        self.exec_us.fetch_add(exec_us, Ordering::Relaxed);
        update_max(&self.max_lock_wait_us, lock_wait_us);
        update_max(&self.max_exec_us, exec_us);
    }

    fn snapshot(&self) -> Value {
        let requests = self.requests.load(Ordering::Relaxed);
        let queries = self.queries.load(Ordering::Relaxed);
        let updates = self.updates.load(Ordering::Relaxed);
        let kind = match (queries > 0, updates > 0) {
            (true, false) => "query",
            (false, true) => "update",
            (true, true) => "mixed",
            (false, false) => "unknown",
        };
        json!({
            "kind": kind,
            "count": requests,
            "queries": queries,
            "updates": updates,
            "errors": self.errors.load(Ordering::Relaxed),
            "slow_requests": self.slow_requests.load(Ordering::Relaxed),
            "lock_wait_us_total": self.lock_wait_us.load(Ordering::Relaxed),
            "lock_wait_us_avg": avg(self.lock_wait_us.load(Ordering::Relaxed), requests),
            "lock_wait_us_max": self.max_lock_wait_us.load(Ordering::Relaxed),
            "exec_us_total": self.exec_us.load(Ordering::Relaxed),
            "exec_us_avg": avg(self.exec_us.load(Ordering::Relaxed), requests),
            "exec_us_max": self.max_exec_us.load(Ordering::Relaxed),
        })
    }
}

fn avg(total: u64, count: u64) -> u64 {
    if count == 0 {
        0
    } else {
        total / count
    }
}

fn update_max(max: &AtomicU64, value: u64) {
    let mut current = max.load(Ordering::Relaxed);
    while value > current {
        match max.compare_exchange_weak(current, value, Ordering::Relaxed, Ordering::Relaxed) {
            Ok(_) => break,
            Err(next) => current = next,
        }
    }
}

fn is_slow_request(lock_wait_us: u64, exec_us: u64) -> bool {
    lock_wait_us >= SLOW_LOCK_WAIT_US || exec_us >= SLOW_EXEC_US
}

async fn handle_connection(
    mut stream: TcpStream,
    snb: Arc<RwLock<SnbGraph>>,
    metrics: Arc<ServerMetrics>,
) -> Result<()> {
    let request = read_http_request(&mut stream).await?;
    let response = match request {
        HttpRequest::Get { path } if path == "/" => {
            http_response(200, "application/json", br#"{"status":"ok"}"#.to_vec())
        }
        HttpRequest::Get { path } if path == "/metrics" => {
            let mut snapshot =
                serde_json::to_value(metrics.snapshot()).expect("serialize metrics");
            let fallback = {
                let guard = snb.read().await;
                fallback_hits_value(&guard)
            };
            if let Value::Object(ref mut map) = snapshot {
                map.insert("fallback_hits".to_string(), fallback);
            }
            http_response(
                200,
                "application/json",
                serde_json::to_vec(&snapshot).expect("serialize metrics"),
            )
        }
        HttpRequest::Post { path, .. } if path == "/metrics/reset" => {
            metrics.reset();
            snb.read().await.reset_fallback_hits();
            http_response(200, "application/json", br#"{"status":"ok"}"#.to_vec())
        }
        HttpRequest::Post { path, body } => {
            let params: Value = serde_json::from_slice(&body)?;
            let is_update = path.starts_with("/query/interactive_update_");
            if std::env::var("LSMGRAPH_TRACE_REQUESTS").is_ok() {
                eprintln!(
                    "{}",
                    json!({"event":"req_begin","path":path,"params":params})
                );
            }
            let lock_started = Instant::now();
            let (result, lock_wait_us, exec_us) = if is_update {
                let mut guard = snb.write().await;
                let lock_wait_us = lock_started.elapsed().as_micros() as u64;
                let exec_started = Instant::now();
                let result = run_dgs_http_update(&mut guard, &path, &params);
                let exec_us = exec_started.elapsed().as_micros() as u64;
                (result, lock_wait_us, exec_us)
            } else {
                let guard = snb.read().await;
                let lock_wait_us = lock_started.elapsed().as_micros() as u64;
                let exec_started = Instant::now();
                let result = run_dgs_http_query(&guard, &path, &params);
                let exec_us = exec_started.elapsed().as_micros() as u64;
                (result, lock_wait_us, exec_us)
            };
            metrics.record_query(&path, lock_wait_us, exec_us, is_update, result.is_ok());
            log_slow_request(&path, is_update, lock_wait_us, exec_us);
            match result {
                Ok(value) => http_response(
                    200,
                    "application/json",
                    serde_json::to_vec(&value).expect("serialize query response"),
                ),
                Err(err) => {
                    eprintln!(
                        "{}",
                        json!({
                            "event": "snb_request_error",
                            "path": path,
                            "kind": if is_update { "update" } else { "query" },
                            "error": err.to_string(),
                        })
                    );
                    http_response(
                        500,
                        "application/json",
                        serde_json::to_vec(&json!({
                            "error": err.to_string(),
                            "path": path,
                        }))
                        .expect("serialize error response"),
                    )
                }
            }
        }
        HttpRequest::Get { path } => http_response(
            404,
            "application/json",
            serde_json::to_vec(&json!({"error": "not found", "path": path}))
                .expect("serialize not found response"),
        ),
    };
    stream.write_all(&response).await?;
    stream.shutdown().await?;
    Ok(())
}

fn log_slow_request(path: &str, is_update: bool, lock_wait_us: u64, exec_us: u64) {
    if is_slow_request(lock_wait_us, exec_us) {
        eprintln!(
            "{}",
            json!({
                "event": "snb_request_slow",
                "path": path,
                "kind": if is_update { "update" } else { "query" },
                "lock_wait_us": lock_wait_us,
                "exec_us": exec_us,
            })
        );
    }
}

enum HttpRequest {
    Get { path: String },
    Post { path: String, body: Vec<u8> },
}

async fn read_http_request(stream: &mut TcpStream) -> Result<HttpRequest> {
    let mut buf = Vec::with_capacity(8192);
    let header_end;
    loop {
        let mut chunk = [0u8; 4096];
        let n = stream.read(&mut chunk).await?;
        if n == 0 {
            anyhow::bail!("connection closed before complete HTTP headers");
        }
        buf.extend_from_slice(&chunk[..n]);
        if let Some(pos) = find_header_end(&buf) {
            header_end = pos;
            break;
        }
        if buf.len() > 1024 * 1024 {
            anyhow::bail!("HTTP headers too large");
        }
    }

    let (method, path, content_len) = {
        let header = String::from_utf8_lossy(&buf[..header_end]);
        let mut lines = header.lines();
        let Some(request_line) = lines.next() else {
            anyhow::bail!("empty HTTP request");
        };
        let mut parts = request_line.split_whitespace();
        let method = parts.next().unwrap_or_default().to_string();
        let path = parts.next().unwrap_or_default().to_string();
        let content_len = lines
            .filter_map(|line| line.split_once(':'))
            .find_map(|(name, value)| {
                name.eq_ignore_ascii_case("content-length")
                    .then(|| value.trim().parse::<usize>().ok())
                    .flatten()
            })
            .unwrap_or(0);
        (method, path, content_len)
    };

    let body_start = header_end + 4;
    while buf.len() < body_start + content_len {
        let mut chunk = [0u8; 4096];
        let n = stream.read(&mut chunk).await?;
        if n == 0 {
            anyhow::bail!("connection closed before complete HTTP body");
        }
        buf.extend_from_slice(&chunk[..n]);
    }
    let body = buf[body_start..body_start + content_len].to_vec();

    match method.as_str() {
        "GET" => Ok(HttpRequest::Get { path }),
        "POST" => Ok(HttpRequest::Post { path, body }),
        _ => anyhow::bail!("unsupported HTTP method: {method}"),
    }
}

fn find_header_end(buf: &[u8]) -> Option<usize> {
    buf.windows(4).position(|w| w == b"\r\n\r\n")
}

fn http_response(status: u16, content_type: &str, body: Vec<u8>) -> Vec<u8> {
    let reason = match status {
        200 => "OK",
        404 => "Not Found",
        500 => "Internal Server Error",
        _ => "OK",
    };
    let mut response = format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    )
    .into_bytes();
    response.extend_from_slice(&body);
    response
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn server_metrics_snapshot_includes_endpoint_aggregates() {
        let metrics = ServerMetrics::default();
        metrics.record_query(
            "/query/interactive_complex_read_3",
            100,
            SLOW_EXEC_US,
            false,
            true,
        );
        metrics.record_query(
            "/query/interactive_update_5",
            SLOW_LOCK_WAIT_US,
            42,
            true,
            false,
        );

        let snapshot = metrics.snapshot();
        assert_eq!(snapshot["requests"], 2);
        assert_eq!(snapshot["slow_requests"], 2);
        assert_eq!(
            snapshot["endpoints"]["/query/interactive_complex_read_3"]["kind"],
            "query"
        );
        assert_eq!(
            snapshot["endpoints"]["/query/interactive_complex_read_3"]["exec_us_max"],
            SLOW_EXEC_US
        );
        assert_eq!(
            snapshot["endpoints"]["/query/interactive_update_5"]["kind"],
            "update"
        );
        assert_eq!(
            snapshot["endpoints"]["/query/interactive_update_5"]["errors"],
            1
        );
        assert_eq!(
            snapshot["endpoints"]["/query/interactive_update_5"]["lock_wait_us_max"],
            SLOW_LOCK_WAIT_US
        );

        metrics.reset();
        let snapshot = metrics.snapshot();
        assert_eq!(snapshot["requests"], 0);
        assert_eq!(snapshot["endpoints"].as_object().unwrap().len(), 0);
    }
}
