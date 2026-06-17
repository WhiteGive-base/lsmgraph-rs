use std::path::{Path, PathBuf};
use std::sync::Arc;

use crate::config::{IoBackendKind, L0LayoutPolicy, LsmGraphConfig};
use crate::error::Result;
use crate::graph::Engine;
use crate::types::{EdgeRecord, EdgeType, SnapshotId, VertexId};

#[derive(Clone)]
pub struct DeltaGraph {
    dir: PathBuf,
    engine: Arc<Engine>,
}

impl DeltaGraph {
    pub async fn create(
        store_dir: impl AsRef<Path>,
        io_backend: IoBackendKind,
        memgraph_bytes: usize,
    ) -> Result<Self> {
        let dir = store_dir.as_ref().join("delta");
        let engine = Engine::create(
            LsmGraphConfig::new(&dir)
                .with_io_backend(io_backend)
                .with_memgraph_capacity(memgraph_bytes),
        )
        .await?;
        Ok(Self { dir, engine })
    }

    pub async fn create_with_k4_auto_maintenance(
        store_dir: impl AsRef<Path>,
        io_backend: IoBackendKind,
        memgraph_bytes: usize,
    ) -> Result<Self> {
        let dir = store_dir.as_ref().join("delta");
        let engine = Engine::create(k4_delta_config(&dir, io_backend, memgraph_bytes)).await?;
        Ok(Self { dir, engine })
    }

    pub async fn open(store_dir: impl AsRef<Path>, io_backend: IoBackendKind) -> Result<Self> {
        let dir = store_dir.as_ref().join("delta");
        let engine = Engine::open(LsmGraphConfig::new(&dir).with_io_backend(io_backend)).await?;
        Ok(Self { dir, engine })
    }

    pub async fn open_with_k4_auto_maintenance(
        store_dir: impl AsRef<Path>,
        io_backend: IoBackendKind,
        memgraph_bytes: usize,
    ) -> Result<Self> {
        let dir = store_dir.as_ref().join("delta");
        let engine = Engine::open(k4_delta_config(&dir, io_backend, memgraph_bytes)).await?;
        Ok(Self { dir, engine })
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }

    pub fn engine(&self) -> Arc<Engine> {
        self.engine.clone()
    }

    pub fn current_snapshot(&self) -> SnapshotId {
        self.engine.current_snapshot()
    }

    pub async fn insert_edge(
        &self,
        src: VertexId,
        dst: VertexId,
        edge_type: EdgeType,
    ) -> Result<SnapshotId> {
        self.engine.insert_edge(src, dst, edge_type).await
    }

    pub async fn delete_edge(
        &self,
        src: VertexId,
        dst: VertexId,
        edge_type: EdgeType,
    ) -> Result<SnapshotId> {
        self.engine.delete_edge(src, dst, edge_type).await
    }

    pub async fn get_neighbors(
        &self,
        src: VertexId,
        edge_type: Option<EdgeType>,
        snapshot: SnapshotId,
    ) -> Result<Vec<EdgeRecord>> {
        match edge_type {
            Some(edge_type) => {
                self.engine
                    .get_neighbors_typed(src, edge_type, snapshot)
                    .await
            }
            None => self.engine.get_neighbors(src, snapshot).await,
        }
    }

    pub async fn flush(&self) -> Result<()> {
        self.engine.flush_active().await
    }
}

fn k4_delta_config(dir: &Path, io_backend: IoBackendKind, memgraph_bytes: usize) -> LsmGraphConfig {
    let mut config = LsmGraphConfig::new(dir)
        .with_io_backend(io_backend)
        .with_memgraph_capacity(memgraph_bytes)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(1)
        .with_semantic_budget_min_exact_bytes(1)
        .with_semantic_budget_min_benefit_score(0.0)
        .with_k4_auto_maintenance(true);
    config.l0_ra_min_queries = 2;
    config.l0_ra_min_l0_segments = 2;
    config.l0_ra_min_score = 0.0;
    config
}
