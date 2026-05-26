pub mod backend;
pub mod blocking_pread;
pub mod configured;
#[cfg(all(feature = "direct-io", unix))]
pub mod direct_io;
#[cfg(all(feature = "uring", target_os = "linux"))]
pub mod uring;

pub use backend::{BoxIoFuture, IoBackend};
pub use blocking_pread::BlockingPreadBackend;
pub use configured::AnyIoBackend;
