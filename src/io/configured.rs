use std::path::{Path, PathBuf};
use std::sync::Arc;

use bytes::Bytes;

use crate::config::IoBackendKind;
use crate::error::Result;
use crate::io::{BlockingPreadBackend, BoxIoFuture, IoBackend};
use crate::metrics::Metrics;

pub enum AnyIoBackend {
    Blocking(BlockingPreadBackend),
    #[cfg(all(feature = "direct-io", unix))]
    Direct(crate::io::direct_io::DirectIoBackend),
    #[cfg(all(feature = "uring", target_os = "linux"))]
    Uring(crate::io::uring::UringBackend),
}

impl AnyIoBackend {
    pub fn new(
        kind: IoBackendKind,
        max_outstanding_io: usize,
        metrics: Arc<Metrics>,
    ) -> Result<Self> {
        match kind {
            IoBackendKind::Blocking => Ok(Self::Blocking(BlockingPreadBackend::new(
                max_outstanding_io,
                metrics,
            ))),
            IoBackendKind::Direct => {
                #[cfg(all(feature = "direct-io", unix))]
                {
                    Ok(Self::Direct(crate::io::direct_io::DirectIoBackend::new(
                        max_outstanding_io,
                        metrics,
                    )))
                }
                #[cfg(not(all(feature = "direct-io", unix)))]
                {
                    let _ = max_outstanding_io;
                    let _ = metrics;
                    anyhow::bail!("direct I/O backend requires --features direct-io on Unix")
                }
            }
            IoBackendKind::Uring => {
                #[cfg(all(feature = "uring", target_os = "linux"))]
                {
                    Ok(Self::Uring(crate::io::uring::UringBackend::new(
                        max_outstanding_io,
                        metrics,
                    )))
                }
                #[cfg(not(all(feature = "uring", target_os = "linux")))]
                {
                    let _ = max_outstanding_io;
                    let _ = metrics;
                    anyhow::bail!("io_uring backend requires --features uring on Linux")
                }
            }
        }
    }
}

impl IoBackend for AnyIoBackend {
    fn read_at<'a>(&'a self, path: &'a Path, offset: u64, len: usize) -> BoxIoFuture<'a, Bytes> {
        match self {
            Self::Blocking(b) => b.read_at(path, offset, len),
            #[cfg(all(feature = "direct-io", unix))]
            Self::Direct(b) => b.read_at(path, offset, len),
            #[cfg(all(feature = "uring", target_os = "linux"))]
            Self::Uring(b) => b.read_at(path, offset, len),
        }
    }

    fn write_at<'a>(&'a self, path: &'a Path, offset: u64, buf: Bytes) -> BoxIoFuture<'a, usize> {
        match self {
            Self::Blocking(b) => b.write_at(path, offset, buf),
            #[cfg(all(feature = "direct-io", unix))]
            Self::Direct(b) => b.write_at(path, offset, buf),
            #[cfg(all(feature = "uring", target_os = "linux"))]
            Self::Uring(b) => b.write_at(path, offset, buf),
        }
    }

    fn create<'a>(&'a self, path: &'a Path) -> BoxIoFuture<'a, PathBuf> {
        match self {
            Self::Blocking(b) => b.create(path),
            #[cfg(all(feature = "direct-io", unix))]
            Self::Direct(b) => b.create(path),
            #[cfg(all(feature = "uring", target_os = "linux"))]
            Self::Uring(b) => b.create(path),
        }
    }

    fn sync<'a>(&'a self, path: &'a Path) -> BoxIoFuture<'a, ()> {
        match self {
            Self::Blocking(b) => b.sync(path),
            #[cfg(all(feature = "direct-io", unix))]
            Self::Direct(b) => b.sync(path),
            #[cfg(all(feature = "uring", target_os = "linux"))]
            Self::Uring(b) => b.sync(path),
        }
    }

    fn remove<'a>(&'a self, path: &'a Path) -> BoxIoFuture<'a, ()> {
        match self {
            Self::Blocking(b) => b.remove(path),
            #[cfg(all(feature = "direct-io", unix))]
            Self::Direct(b) => b.remove(path),
            #[cfg(all(feature = "uring", target_os = "linux"))]
            Self::Uring(b) => b.remove(path),
        }
    }
}
