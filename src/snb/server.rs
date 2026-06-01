use std::sync::Arc;

use serde_json::{json, Value};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::RwLock;

use crate::error::Result;
use crate::snb::props::SnbGraph;
use crate::snb::queries::{run_dgs_http_query, run_dgs_http_update};

pub async fn start_dgs_compatible_server(snb: SnbGraph, bind: &str) -> Result<()> {
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
    let request = read_http_request(&mut stream).await?;
    let response = match request {
        HttpRequest::Get { path } if path == "/" => {
            http_response(200, "application/json", br#"{"status":"ok"}"#.to_vec())
        }
        HttpRequest::Post { path, body } => {
            let params: Value = serde_json::from_slice(&body)?;
            let result = if path.starts_with("/query/interactive_update_") {
                let mut guard = snb.write().await;
                run_dgs_http_update(&mut guard, &path, &params)
            } else {
                let guard = snb.read().await;
                run_dgs_http_query(&guard, &path, &params)
            };
            match result {
                Ok(value) => http_response(
                    200,
                    "application/json",
                    serde_json::to_vec(&value).expect("serialize query response"),
                ),
                Err(err) => http_response(
                    500,
                    "application/json",
                    serde_json::to_vec(&json!({
                        "error": err.to_string(),
                        "path": path,
                    }))
                    .expect("serialize error response"),
                ),
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
