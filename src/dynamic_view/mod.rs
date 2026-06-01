use std::path::{Path, PathBuf};
use std::sync::Arc;

use crate::base_graph::{BaseGraph, IoConfig, ReadContext};
use crate::config::IoBackendKind;
use crate::delta::DeltaGraph;
use crate::error::Result;
use crate::types::{EdgeRecord, EdgeType, SnapshotId, VertexId};

pub struct DynamicGraphView {
    store_dir: PathBuf,
    base: Option<Arc<BaseGraph>>,
    delta: Option<DeltaGraph>,
}

impl DynamicGraphView {
    pub async fn open(
        store_dir: impl AsRef<Path>,
        base_io: IoConfig,
        delta_backend: IoBackendKind,
    ) -> Result<Self> {
        let store_dir = store_dir.as_ref().to_path_buf();
        let base_dir = store_dir.join("base_graph");
        let base = if base_dir.join("catalog.json").exists() {
            Some(Arc::new(BaseGraph::open(&base_dir, base_io)?))
        } else {
            None
        };
        let delta_dir = store_dir.join("delta");
        let delta = if delta_dir.exists() {
            Some(DeltaGraph::open(&store_dir, delta_backend).await?)
        } else {
            None
        };
        Ok(Self {
            store_dir,
            base,
            delta,
        })
    }

    pub fn store_dir(&self) -> &Path {
        &self.store_dir
    }

    pub fn base(&self) -> Option<&Arc<BaseGraph>> {
        self.base.as_ref()
    }

    pub fn delta(&self) -> Option<&DeltaGraph> {
        self.delta.as_ref()
    }

    pub fn new_read_context(&self, worker_id: usize) -> Option<ReadContext> {
        self.base
            .as_ref()
            .map(|base| base.new_read_context(worker_id))
    }

    pub async fn scan_neighbors<F>(
        &self,
        base_csr: Option<&str>,
        base_src: Option<u32>,
        delta_src: VertexId,
        delta_edge_type: Option<EdgeType>,
        snapshot: SnapshotId,
        ctx: &mut Option<ReadContext>,
        mut f: F,
    ) -> Result<()>
    where
        F: FnMut(u64) -> Result<()>,
    {
        if let (Some(base), Some(csr), Some(src), Some(read_ctx)) =
            (self.base.as_ref(), base_csr, base_src, ctx.as_mut())
        {
            base.scan_csr(csr, src, read_ctx, |chunk| {
                for dst in chunk {
                    f(*dst as u64)?;
                }
                Ok(())
            })?;
        }
        if let Some(delta) = &self.delta {
            for edge in delta
                .get_neighbors(delta_src, delta_edge_type, snapshot)
                .await?
            {
                f(edge.dst)?;
            }
        }
        Ok(())
    }

    pub async fn neighbors_vec(
        &self,
        base_csr: Option<&str>,
        base_src: Option<u32>,
        delta_src: VertexId,
        delta_edge_type: Option<EdgeType>,
        snapshot: SnapshotId,
    ) -> Result<Vec<u64>> {
        let mut ctx = self.new_read_context(0);
        let mut out = Vec::new();
        self.scan_neighbors(
            base_csr,
            base_src,
            delta_src,
            delta_edge_type,
            snapshot,
            &mut ctx,
            |dst| {
                out.push(dst);
                Ok(())
            },
        )
        .await?;
        out.sort_unstable();
        out.dedup();
        Ok(out)
    }
}

pub fn merge_delta_records(records: Vec<EdgeRecord>) -> Vec<VertexId> {
    records.into_iter().map(|edge| edge.dst).collect()
}
