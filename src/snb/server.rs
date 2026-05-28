use std::env;
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::RwLock;
use tokio::time::sleep;

use crate::error::Result;
use crate::metrics::Metrics;
use crate::snb::props::SnbGraph;
use crate::snb::queries::{run_dgs_http_query, run_dgs_http_update};

pub async fn start_dgs_compatible_server(snb: SnbGraph, bind: &str) -> Result<()> {
    let metrics = snb.engine.metrics();
    start_periodic_metrics_logger(metrics);

    let snb = Arc::new(RwLock::new(snb));
    let listener = TcpListener::bind(bind).await?;
    println!("{{\"server\":\"lsmgraph-snb\",\"bind\":\"{bind}\"}}");
    loop {
        let (stream, _) = listener.accept().await?;
        let snb = snb.clone();
        tokio::spawn(async move {
            if let Err(err) = handle_connection(stream, snb).await {
                eprintln!("snb-server connection error: {err:#}");
            }
        });
    }
}

async fn handle_connection(mut stream: TcpStream, snb: Arc<RwLock<SnbGraph>>) -> Result<()> {
    let read_started = Instant::now();
    let request = read_http_request(&mut stream).await?;
    let metrics = metrics_for(&snb).await;
    metrics.http_request_read_latency.record_since(read_started);

    let mut record_response_metrics = true;
    let response = match request {
        HttpRequest::Get { path } if path == "/" => {
            http_response(200, "application/json", br#"{"status":"ok"}"#.to_vec())
        }
        HttpRequest::Get { path } if path.starts_with("/metrics") => {
            let snapshot = metrics.snapshot_json();
            json_response(&metrics, 200, &snapshot)
        }
        HttpRequest::Post { path, body: _ } if path == "/metrics/reset" => {
            record_response_metrics = false;
            let response =
                http_response(200, "application/json", br#"{"status":"reset"}"#.to_vec());
            metrics.reset();
            response
        }
        HttpRequest::Post { path, body } => {
            let parse_started = Instant::now();
            let params: Value = serde_json::from_slice(&body)?;
            metrics.http_json_parse_latency.record_since(parse_started);

            let handler_started = Instant::now();
            let result = if path.starts_with("/query/interactive_update_") {
                let lock_wait_started = Instant::now();
                let mut guard = snb.write().await;
                metrics
                    .snb_write_lock_wait_latency
                    .record_since(lock_wait_started);
                let lock_hold_started = Instant::now();
                let result = run_dgs_http_update(&mut guard, &path, &params);
                metrics
                    .snb_write_lock_hold_latency
                    .record_since(lock_hold_started);
                result
            } else {
                let lock_wait_started = Instant::now();
                let guard = snb.read().await;
                metrics
                    .snb_read_lock_wait_latency
                    .record_since(lock_wait_started);
                let lock_hold_started = Instant::now();
                let result = run_dgs_http_query(&guard, &path, &params);
                metrics
                    .snb_read_lock_hold_latency
                    .record_since(lock_hold_started);
                result
            };
            metrics.http_handler_latency.record_since(handler_started);
            metrics.record_endpoint(&path, handler_started.elapsed(), result.is_ok());

            match result {
                Ok(value) => json_response(&metrics, 200, &value),
                Err(err) => json_response(
                    &metrics,
                    500,
                    &json!({
                        "error": err.to_string(),
                        "path": path,
                    }),
                ),
            }
        }
        HttpRequest::Get { path } => {
            json_response(&metrics, 404, &json!({"error": "not found", "path": path}))
        }
    };

    if record_response_metrics {
        metrics.record_http_status(response.status);
    }
    let write_started = Instant::now();
    stream.write_all(&response.bytes).await?;
    stream.shutdown().await?;
    if record_response_metrics {
        metrics
            .http_response_write_latency
            .record_since(write_started);
    }
    Ok(())
}

async fn metrics_for(snb: &Arc<RwLock<SnbGraph>>) -> Arc<Metrics> {
    let guard = snb.read().await;
    guard.engine.metrics()
}

fn start_periodic_metrics_logger(metrics: Arc<Metrics>) {
    let interval_secs = env::var("LSMGRAPH_METRICS_INTERVAL_SECS")
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
        .unwrap_or(0);
    if interval_secs == 0 {
        return;
    }
    tokio::spawn(async move {
        let interval = Duration::from_secs(interval_secs);
        loop {
            sleep(interval).await;
            eprintln!("[metrics] {}", metrics.summary_json());
        }
    });
}

enum HttpRequest {
    Get { path: String },
    Post { path: String, body: Vec<u8> },
}

struct HttpResponse {
    status: u16,
    bytes: Vec<u8>,
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

fn json_response(metrics: &Metrics, status: u16, value: &Value) -> HttpResponse {
    let started = Instant::now();
    let body = serde_json::to_vec(value).expect("serialize json response");
    metrics.http_serialize_latency.record_since(started);
    http_response(status, "application/json", body)
}

fn http_response(status: u16, content_type: &str, body: Vec<u8>) -> HttpResponse {
    let reason = match status {
        200 => "OK",
        404 => "Not Found",
        500 => "Internal Server Error",
        _ => "OK",
    };
    let mut bytes = format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    )
    .into_bytes();
    bytes.extend_from_slice(&body);
    HttpResponse { status, bytes }
}
