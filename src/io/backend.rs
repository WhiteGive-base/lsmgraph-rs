use std::future::Future;
use std::path::{Path, PathBuf};
use std::pin::Pin;

use bytes::Bytes;

use crate::error::Result;

pub type BoxIoFuture<'a, T> = Pin<Box<dyn Future<Output = Result<T>> + Send + 'a>>;

pub trait IoBackend: Send + Sync + 'static {
    fn read_at<'a>(&'a self, path: &'a Path, offset: u64, len: usize) -> BoxIoFuture<'a, Bytes>;
    fn write_at<'a>(&'a self, path: &'a Path, offset: u64, buf: Bytes) -> BoxIoFuture<'a, usize>;
    fn create<'a>(&'a self, path: &'a Path) -> BoxIoFuture<'a, PathBuf>;
    fn sync<'a>(&'a self, path: &'a Path) -> BoxIoFuture<'a, ()>;
    fn remove<'a>(&'a self, path: &'a Path) -> BoxIoFuture<'a, ()>;
}
