use std::fs::{self, OpenOptions};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Instant;

use bytes::Bytes;
use tokio::sync::Semaphore;

use crate::error::Result;
use crate::io::backend::{BoxIoFuture, IoBackend};
use crate::metrics::Metrics;

#[derive(Clone)]
pub struct BlockingPreadBackend {
    semaphore: Arc<Semaphore>,
    metrics: Arc<Metrics>,
}

impl BlockingPreadBackend {
    pub fn new(max_outstanding_io: usize, metrics: Arc<Metrics>) -> Self {
        Self {
            semaphore: Arc::new(Semaphore::new(max_outstanding_io)),
            metrics,
        }
    }
}

impl IoBackend for BlockingPreadBackend {
    fn read_at<'a>(&'a self, path: &'a Path, offset: u64, len: usize) -> BoxIoFuture<'a, Bytes> {
        let path = path.to_path_buf();
        let sem = self.semaphore.clone();
        let metrics = self.metrics.clone();
        Box::pin(async move {
            let wait_started = Instant::now();
            let _permit = sem.acquire_owned().await?;
            metrics.io_semaphore_wait_latency.record_since(wait_started);
            let blocking_started = Instant::now();
            let bytes = tokio::task::spawn_blocking(move || -> Result<Bytes> {
                let file = OpenOptions::new().read(true).open(&path)?;
                let mut buf = vec![0u8; len];
                let n = read_at_exactish(&file, &mut buf, offset)?;
                buf.truncate(n);
                Ok(Bytes::from(buf))
            })
            .await??;
            metrics
                .io_read_blocking_latency
                .record_since(blocking_started);
            metrics.add_read(bytes.len() as u64);
            Ok(bytes)
        })
    }

    fn write_at<'a>(&'a self, path: &'a Path, offset: u64, buf: Bytes) -> BoxIoFuture<'a, usize> {
        let path = path.to_path_buf();
        let sem = self.semaphore.clone();
        let metrics = self.metrics.clone();
        Box::pin(async move {
            let len = buf.len();
            let wait_started = Instant::now();
            let _permit = sem.acquire_owned().await?;
            metrics.io_semaphore_wait_latency.record_since(wait_started);
            let blocking_started = Instant::now();
            let written = tokio::task::spawn_blocking(move || -> Result<usize> {
                if let Some(parent) = path.parent() {
                    fs::create_dir_all(parent)?;
                }
                let file = OpenOptions::new()
                    .create(true)
                    .write(true)
                    .read(true)
                    .open(&path)?;
                write_all_at(&file, &buf, offset)?;
                Ok(len)
            })
            .await??;
            metrics
                .io_write_blocking_latency
                .record_since(blocking_started);
            metrics.add_write(written as u64);
            Ok(written)
        })
    }

    fn create<'a>(&'a self, path: &'a Path) -> BoxIoFuture<'a, PathBuf> {
        let path = path.to_path_buf();
        let metrics = self.metrics.clone();
        Box::pin(async move {
            let blocking_started = Instant::now();
            tokio::task::spawn_blocking({
                let path = path.clone();
                move || -> Result<()> {
                    if let Some(parent) = path.parent() {
                        fs::create_dir_all(parent)?;
                    }
                    OpenOptions::new()
                        .create(true)
                        .truncate(true)
                        .write(true)
                        .read(true)
                        .open(&path)?;
                    Ok(())
                }
            })
            .await??;
            metrics
                .io_create_blocking_latency
                .record_since(blocking_started);
            Ok(path)
        })
    }

    fn sync<'a>(&'a self, path: &'a Path) -> BoxIoFuture<'a, ()> {
        let path = path.to_path_buf();
        let metrics = self.metrics.clone();
        Box::pin(async move {
            let blocking_started = Instant::now();
            tokio::task::spawn_blocking(move || -> Result<()> {
                let file = OpenOptions::new().read(true).write(true).open(&path)?;
                file.sync_all()?;
                Ok(())
            })
            .await??;
            metrics
                .io_sync_blocking_latency
                .record_since(blocking_started);
            Ok(())
        })
    }

    fn remove<'a>(&'a self, path: &'a Path) -> BoxIoFuture<'a, ()> {
        let path = path.to_path_buf();
        let metrics = self.metrics.clone();
        Box::pin(async move {
            let blocking_started = Instant::now();
            tokio::task::spawn_blocking(move || -> Result<()> {
                match fs::remove_file(&path) {
                    Ok(()) => Ok(()),
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
                    Err(e) => Err(e.into()),
                }
            })
            .await??;
            metrics
                .io_remove_blocking_latency
                .record_since(blocking_started);
            Ok(())
        })
    }
}

#[cfg(unix)]
fn read_at_exactish(file: &std::fs::File, buf: &mut [u8], offset: u64) -> std::io::Result<usize> {
    use std::os::unix::fs::FileExt;
    let mut pos = 0usize;
    while pos < buf.len() {
        let n = file.read_at(&mut buf[pos..], offset + pos as u64)?;
        if n == 0 {
            break;
        }
        pos += n;
    }
    Ok(pos)
}

#[cfg(unix)]
fn write_all_at(file: &std::fs::File, buf: &[u8], offset: u64) -> std::io::Result<()> {
    use std::os::unix::fs::FileExt;
    let mut pos = 0usize;
    while pos < buf.len() {
        let n = file.write_at(&buf[pos..], offset + pos as u64)?;
        if n == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::WriteZero,
                "write_at returned zero",
            ));
        }
        pos += n;
    }
    Ok(())
}

#[cfg(windows)]
fn read_at_exactish(file: &std::fs::File, buf: &mut [u8], offset: u64) -> std::io::Result<usize> {
    use std::os::windows::fs::FileExt;
    let mut pos = 0usize;
    while pos < buf.len() {
        let n = file.seek_read(&mut buf[pos..], offset + pos as u64)?;
        if n == 0 {
            break;
        }
        pos += n;
    }
    Ok(pos)
}

#[cfg(windows)]
fn write_all_at(file: &std::fs::File, buf: &[u8], offset: u64) -> std::io::Result<()> {
    use std::os::windows::fs::FileExt;
    let mut pos = 0usize;
    while pos < buf.len() {
        let n = file.seek_write(&buf[pos..], offset + pos as u64)?;
        if n == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::WriteZero,
                "seek_write returned zero",
            ));
        }
        pos += n;
    }
    Ok(())
}
