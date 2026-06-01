use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use crate::base_graph::catalog::{BaseGraphCatalog, SingleColumnEntry};
use crate::base_graph::column::read_u32_column;
use crate::base_graph::csr::{CsrAdjacency, ReadContext};
use crate::base_graph::ids::MessageId;
use crate::base_graph::io::IoConfig;
use crate::error::Result;

#[derive(Debug)]
pub struct BaseGraph {
    base_dir: PathBuf,
    catalog: BaseGraphCatalog,
    io_config: IoConfig,
    csr: BTreeMap<String, CsrAdjacency>,
    single_u32: BTreeMap<String, Vec<u32>>,
    comment_parent_kind: Vec<u8>,
}

impl BaseGraph {
    pub fn open(base_dir: impl AsRef<Path>, mut io_config: IoConfig) -> Result<Self> {
        let base_dir = base_dir.as_ref().to_path_buf();
        io_config.base_dir = Some(base_dir.clone());
        let catalog = BaseGraphCatalog::load(&base_dir.join("catalog.json"))?;
        let mut csr = BTreeMap::new();
        for (name, entry) in &catalog.csr_adjacencies {
            csr.insert(name.clone(), CsrAdjacency::open(&base_dir, entry)?);
        }
        let mut single_u32 = BTreeMap::new();
        for (name, entry) in &catalog.single_columns {
            if entry.value_type == "u32" {
                single_u32.insert(name.clone(), read_u32_column(&base_dir.join(&entry.file))?);
            }
        }
        let comment_parent_kind = read_optional_u8_column(
            &base_dir.join("single_edges/REPLY_OF/comment_parent_kind.col"),
        )?;
        Ok(Self {
            base_dir,
            catalog,
            io_config,
            csr,
            single_u32,
            comment_parent_kind,
        })
    }

    pub fn catalog(&self) -> &BaseGraphCatalog {
        &self.catalog
    }

    pub fn base_dir(&self) -> &Path {
        &self.base_dir
    }

    pub fn new_read_context(&self, worker_id: usize) -> ReadContext {
        ReadContext::new(worker_id, self.io_config.clone())
    }

    pub fn csr(&self, name: &str) -> Option<&CsrAdjacency> {
        self.csr.get(name)
    }

    pub fn scan_csr<F>(&self, name: &str, src: u32, ctx: &mut ReadContext, f: F) -> Result<()>
    where
        F: FnMut(&[u32]) -> Result<()>,
    {
        let Some(csr) = self.csr.get(name) else {
            return Ok(());
        };
        csr.scan_chunks(src, ctx, f)
    }

    pub fn post_creator(&self, post: u32) -> Option<u32> {
        self.single_value("post_creator", post)
    }

    pub fn comment_creator(&self, comment: u32) -> Option<u32> {
        self.single_value("comment_creator", comment)
    }

    pub fn post_forum(&self, post: u32) -> Option<u32> {
        self.single_value("post_forum", post)
    }

    pub fn person_place(&self, person: u32) -> Option<u32> {
        self.single_value("person_place", person)
    }

    pub fn comment_reply_of(&self, comment: u32) -> Option<MessageId> {
        let kind = *self.comment_parent_kind.get(comment as usize)?;
        let id = self.single_value("comment_parent_id", comment)?;
        match kind {
            0 => Some(MessageId::Post(id)),
            1 => Some(MessageId::Comment(id)),
            _ => None,
        }
    }

    pub fn comment_root_post(&self, comment: u32) -> Option<u32> {
        self.derived_value("comment_root_post", comment)
    }

    pub fn single_column_entry(&self, name: &str) -> Option<&SingleColumnEntry> {
        self.catalog.single_columns.get(name)
    }

    fn single_value(&self, name: &str, idx: u32) -> Option<u32> {
        let value = *self.single_u32.get(name)?.get(idx as usize)?;
        (value != u32::MAX).then_some(value)
    }

    fn derived_value(&self, name: &str, idx: u32) -> Option<u32> {
        let entry = self.catalog.derived_columns.get(name)?;
        let values = read_u32_column(&self.base_dir.join(&entry.file)).ok()?;
        let value = *values.get(idx as usize)?;
        (value != u32::MAX).then_some(value)
    }
}

fn read_optional_u8_column(path: &Path) -> Result<Vec<u8>> {
    match std::fs::read(path) {
        Ok(bytes) => Ok(bytes),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(Vec::new()),
        Err(err) => Err(err.into()),
    }
}
