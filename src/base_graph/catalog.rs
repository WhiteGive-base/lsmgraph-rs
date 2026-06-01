use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufReader, BufWriter};
use std::path::Path;

use serde::{Deserialize, Serialize};

use crate::error::Result;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BaseGraphCatalog {
    pub format: String,
    pub version: u32,
    pub source: String,
    pub vertex_labels: BTreeMap<String, VertexCatalogEntry>,
    pub single_columns: BTreeMap<String, SingleColumnEntry>,
    pub csr_adjacencies: BTreeMap<String, CsrCatalogEntry>,
    pub derived_columns: BTreeMap<String, DerivedColumnEntry>,
}

impl BaseGraphCatalog {
    pub fn new(source: impl Into<String>) -> Self {
        Self {
            format: "lsmgraph.base_graph".to_string(),
            version: 1,
            source: source.into(),
            vertex_labels: BTreeMap::new(),
            single_columns: BTreeMap::new(),
            csr_adjacencies: BTreeMap::new(),
            derived_columns: BTreeMap::new(),
        }
    }

    pub fn load(path: &Path) -> Result<Self> {
        let file = File::open(path)?;
        Ok(serde_json::from_reader(BufReader::new(file))?)
    }

    pub fn save(&self, path: &Path) -> Result<()> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let file = File::create(path)?;
        serde_json::to_writer_pretty(BufWriter::new(file), self)?;
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VertexCatalogEntry {
    pub label: String,
    pub count: u64,
    pub ext_id_to_local: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SingleColumnEntry {
    pub name: String,
    pub file: String,
    pub src_label: String,
    pub dst_label: String,
    pub rows: u64,
    pub value_type: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CsrCatalogEntry {
    pub name: String,
    pub path: String,
    pub src_label: String,
    pub dst_label: String,
    pub num_src_vertices: u64,
    pub num_edges: u64,
    pub offset_type: String,
    pub neighbor_type: String,
    pub sort_order: String,
    pub prop_access: String,
    pub props: Vec<CsrPropEntry>,
    pub neighbor_block_bytes: usize,
    pub file_alignment: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CsrPropEntry {
    pub name: String,
    pub file: String,
    pub value_type: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DerivedColumnEntry {
    pub name: String,
    pub file: String,
    pub rows: u64,
    pub value_type: String,
}
