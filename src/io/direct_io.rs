use std::fs::{self, OpenOptions};
use std::os::fd::AsRawFd;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use bytes::Bytes;
use tokio::sync::Semaphore;

use crate::error::Result;
use crate::io::{BoxIoFuture, IoBackend};
use crate::metrics::Metrics;

const ALIGN: usize = 4096;

#[derive(Clone)]
pub struct DirectIoBackend {
    semaphore: Arc<Semaphore>,
    metrics: Arc<Metrics>,
}

impl DirectIoBackend {
    pub fn new(max_outstanding_io: usize, metrics: Arc<Metrics>) -> Self {
        Self {
            semaphore: Arc::new(Semaphore::new(max_outstanding_io)),
            metrics,
        }
    }
}

impl IoBackend for DirectIoBackend {
    fn read_at<'a>(&'a self, path: &'a Path, offset: u64, len: usize) -> BoxIoFuture<'a, Bytes> {
        let path = path.to_path_buf();
        let sem = self.semaphore.clone();
        let metrics = self.metrics.clone();
        Box::pin(async move {
            let _permit = sem.acquire_owned().await?;
            let bytes =
                tokio::task::spawn_blocking(move || direct_read_at(&path, offset, len)).await??;
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
            tokio::task::spawn_blocking(move || direct_write_at(&path, offset, &buf)).await??;
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
            tokio::task::spawn_blocking(move || -> Result<()> {
                let file = OpenOptions::new().read(true).write(true).open(&path)?;
                file.sync_all()?;
                Ok(())
            })
            .await??;
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

fn direct_read_at(path: &Path, offset: u64, len: usize) -> Result<Bytes> {
    if len == 0 {
        return Ok(Bytes::new());
    }
    let aligned_offset = align_down(offset as usize, ALIGN) as u64;
    let delta = (offset - aligned_offset) as usize;
    let aligned_len = align_up(delta + len, ALIGN);
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECT)
        .open(path)?;
    let mut buf = AlignedBuf::new(aligned_len, ALIGN)?;
    let n = unsafe {
        libc::pread(
            file.as_raw_fd(),
            buf.as_mut_ptr().cast(),
            aligned_len,
            aligned_offset as libc::off_t,
        )
    };
    if n < 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    let n = n as usize;
    if n <= delta {
        return Ok(Bytes::new());
    }
    let available = (n - delta).min(len);
    Ok(Bytes::copy_from_slice(
        &buf.as_slice()[delta..delta + available],
    ))
}

fn direct_write_at(path: &Path, offset: u64, buf: &[u8]) -> Result<()> {
    if offset as usize % ALIGN != 0 {
        anyhow::bail!("O_DIRECT write offset must be {ALIGN}-byte aligned: {offset}");
    }
    let aligned_len = align_up(buf.len(), ALIGN);
    let file = OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .custom_flags(libc::O_DIRECT)
        .open(path)?;
    let mut aligned = AlignedBuf::new(aligned_len, ALIGN)?;
    aligned.as_mut_slice()[..buf.len()].copy_from_slice(buf);
    let mut written = 0usize;
    while written < aligned_len {
        let n = unsafe {
            libc::pwrite(
                file.as_raw_fd(),
                aligned.as_ptr().add(written).cast(),
                aligned_len - written,
                (offset as usize + written) as libc::off_t,
            )
        };
        if n < 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        if n == 0 {
            anyhow::bail!("O_DIRECT pwrite returned zero");
        }
        written += n as usize;
    }
    file.set_len(offset + buf.len() as u64)?;
    Ok(())
}

struct AlignedBuf {
    ptr: *mut u8,
    len: usize,
}

impl AlignedBuf {
    fn new(len: usize, align: usize) -> Result<Self> {
        let mut ptr = std::ptr::null_mut();
        let rc = unsafe { libc::posix_memalign(&mut ptr, align, len) };
        if rc != 0 {
            return Err(std::io::Error::from_raw_os_error(rc).into());
        }
        unsafe {
            std::ptr::write_bytes(ptr, 0, len);
        }
        Ok(Self {
            ptr: ptr.cast(),
            len,
        })
    }

    fn as_ptr(&self) -> *const u8 {
        self.ptr
    }

    fn as_mut_ptr(&mut self) -> *mut u8 {
        self.ptr
    }

    fn as_slice(&self) -> &[u8] {
        unsafe { std::slice::from_raw_parts(self.ptr, self.len) }
    }

    fn as_mut_slice(&mut self) -> &mut [u8] {
        unsafe { std::slice::from_raw_parts_mut(self.ptr, self.len) }
    }
}

impl Drop for AlignedBuf {
    fn drop(&mut self) {
        unsafe {
            libc::free(self.ptr.cast());
        }
    }
}

fn align_down(v: usize, align: usize) -> usize {
    v & !(align - 1)
}

fn align_up(v: usize, align: usize) -> usize {
    (v + align - 1) & !(align - 1)
}
