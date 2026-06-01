use std::fs::{self, OpenOptions};
use std::os::fd::AsRawFd;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use bytes::Bytes;
use io_uring::{opcode, types, IoUring};
use tokio::sync::Semaphore;

use crate::error::Result;
use crate::io::{BoxIoFuture, IoBackend};
use crate::metrics::Metrics;

#[derive(Clone)]
pub struct UringBackend {
    semaphore: Arc<Semaphore>,
    metrics: Arc<Metrics>,
}

impl UringBackend {
    pub fn new(max_outstanding_io: usize, metrics: Arc<Metrics>) -> Self {
        Self {
            semaphore: Arc::new(Semaphore::new(max_outstanding_io)),
            metrics,
        }
    }
}

impl IoBackend for UringBackend {
    fn read_at<'a>(&'a self, path: &'a Path, offset: u64, len: usize) -> BoxIoFuture<'a, Bytes> {
        let path = path.to_path_buf();
        let sem = self.semaphore.clone();
        let metrics = self.metrics.clone();
        Box::pin(async move {
            let _permit = sem.acquire_owned().await?;
            let bytes =
                tokio::task::spawn_blocking(move || uring_read_at(&path, offset, len)).await??;
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
            let _permit = sem.acquire_owned().await?;
            tokio::task::spawn_blocking(move || uring_write_at(&path, offset, &buf)).await??;
            metrics.add_write(len as u64);
            Ok(len)
        })
    }

    fn create<'a>(&'a self, path: &'a Path) -> BoxIoFuture<'a, PathBuf> {
        let path = path.to_path_buf();
        Box::pin(async move {
            tokio::task::spawn_blocking({
                let path = path.clone();
                move || -> Result<()> {
                    if let Some(parent) = path.parent() {
                        fs::create_dir_all(parent)?;
                    }
                    OpenOptions::new()
                        .create(true)
                        .truncate(true)
                        .read(true)
                        .write(true)
                        .open(&path)?;
                    Ok(())
                }
            })
            .await??;
            Ok(path)
        })
    }

    fn sync<'a>(&'a self, path: &'a Path) -> BoxIoFuture<'a, ()> {
        let path = path.to_path_buf();
        Box::pin(async move {
            tokio::task::spawn_blocking(move || uring_fsync(&path)).await??;
            Ok(())
        })
    }

    fn remove<'a>(&'a self, path: &'a Path) -> BoxIoFuture<'a, ()> {
        let path = path.to_path_buf();
        Box::pin(async move {
            tokio::task::spawn_blocking(move || -> Result<()> {
                match fs::remove_file(&path) {
                    Ok(()) => Ok(()),
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
                    Err(e) => Err(e.into()),
                }
            })
            .await??;
            Ok(())
        })
    }
}

fn uring_read_at(path: &Path, offset: u64, len: usize) -> Result<Bytes> {
    let file = OpenOptions::new().read(true).open(path)?;
    let mut buf = vec![0u8; len];
    let mut ring = IoUring::new(8)?;
    let iov = libc::iovec {
        iov_base: buf.as_mut_ptr().cast(),
        iov_len: len,
    };
    let entry = opcode::Readv::new(types::Fd(file.as_raw_fd()), &iov, 1)
        .offset(offset)
        .build()
        .user_data(1);
    unsafe {
        ring.submission()
            .push(&entry)
            .map_err(|_| anyhow::anyhow!("io_uring submission queue is full"))?;
    }
    ring.submit_and_wait(1)?;
    let cqe = ring
        .completion()
        .next()
        .ok_or_else(|| anyhow::anyhow!("missing io_uring read completion"))?;
    let ret = cqe.result();
    if ret < 0 {
        return Err(std::io::Error::from_raw_os_error(-ret).into());
    }
    buf.truncate(ret as usize);
    Ok(Bytes::from(buf))
}

fn uring_write_at(path: &Path, offset: u64, buf: &[u8]) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let file = OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .open(path)?;
    let mut ring = IoUring::new(8)?;
    let iov = libc::iovec {
        iov_base: buf.as_ptr() as *mut _,
        iov_len: buf.len(),
    };
    let entry = opcode::Writev::new(types::Fd(file.as_raw_fd()), &iov, 1)
        .offset(offset)
        .build()
        .user_data(2);
    unsafe {
        ring.submission()
            .push(&entry)
            .map_err(|_| anyhow::anyhow!("io_uring submission queue is full"))?;
    }
    ring.submit_and_wait(1)?;
    let cqe = ring
        .completion()
        .next()
        .ok_or_else(|| anyhow::anyhow!("missing io_uring write completion"))?;
    let ret = cqe.result();
    if ret < 0 {
        return Err(std::io::Error::from_raw_os_error(-ret).into());
    }
    if ret as usize != buf.len() {
        anyhow::bail!("short io_uring write: {} of {}", ret, buf.len());
    }
    Ok(())
}

fn uring_fsync(path: &Path) -> Result<()> {
    let file = OpenOptions::new().read(true).write(true).open(path)?;
    let mut ring = IoUring::new(8)?;
    let entry = opcode::Fsync::new(types::Fd(file.as_raw_fd()))
        .build()
        .user_data(3);
    unsafe {
        ring.submission()
            .push(&entry)
            .map_err(|_| anyhow::anyhow!("io_uring submission queue is full"))?;
    }
    ring.submit_and_wait(1)?;
    let cqe = ring
        .completion()
        .next()
        .ok_or_else(|| anyhow::anyhow!("missing io_uring fsync completion"))?;
    let ret = cqe.result();
    if ret < 0 {
        return Err(std::io::Error::from_raw_os_error(-ret).into());
    }
    Ok(())
}
