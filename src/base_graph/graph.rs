use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};

use crate::base_graph::catalog::{BaseGraphCatalog, SingleColumnEntry};
use crate::base_graph::column::{
    read_ext_id_map, read_i64_column, read_string_column, read_u32_column,
};
use crate::base_graph::csr::{CsrAdjacency, ReadContext};
use crate::base_graph::ids::{LabelId, MessageId};
use crate::base_graph::io::IoConfig;
use crate::error::Result;

#[derive(Debug)]
pub struct BaseGraph {
    base_dir: PathBuf,
    catalog: BaseGraphCatalog,
    io_config: IoConfig,
    csr: BTreeMap<String, CsrAdjacency>,
    single_u32: BTreeMap<String, Vec<u32>>,
    id_maps: BTreeMap<String, RuntimeIdMap>,
    comment_parent_kind: Vec<u8>,
}

#[derive(Debug)]
struct RuntimeIdMap {
    external_to_local: HashMap<i64, u32>,
    local_to_external: Vec<i64>,
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
        let mut id_maps = BTreeMap::new();
        for (label, entry) in &catalog.vertex_labels {
            let pairs = read_ext_id_map(&base_dir.join(&entry.ext_id_to_local))?;
            let mut external_to_local = HashMap::with_capacity(pairs.len());
            let mut local_to_external = vec![0i64; pairs.len()];
            for (external, local) in pairs {
                external_to_local.insert(external, local);
                if let Some(slot) = local_to_external.get_mut(local as usize) {
                    *slot = external;
                }
            }
            id_maps.insert(
                label.clone(),
                RuntimeIdMap {
                    external_to_local,
                    local_to_external,
                },
            );
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
            id_maps,
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

    pub fn local_id(&self, label: LabelId, external_id: i64) -> Option<u32> {
        self.id_maps
            .get(label.as_str())?
            .external_to_local
            .get(&external_id)
            .copied()
    }

    pub fn external_id(&self, label: LabelId, local_id: u32) -> Option<i64> {
        self.id_maps
            .get(label.as_str())?
            .local_to_external
            .get(local_id as usize)
            .copied()
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

    pub fn csr_i64_prop_vec(
        &self,
        name: &str,
        src: u32,
        prop_name: &str,
        ctx: &mut ReadContext,
    ) -> Result<Option<Vec<(u32, i64)>>> {
        let Some(csr) = self.csr.get(name) else {
            return Ok(None);
        };
        csr.neighbors_with_i64_prop_vec(src, prop_name, ctx)
    }

    pub fn csr_i32_prop_vec(
        &self,
        name: &str,
        src: u32,
        prop_name: &str,
        ctx: &mut ReadContext,
    ) -> Result<Option<Vec<(u32, i32)>>> {
        let Some(csr) = self.csr.get(name) else {
            return Ok(None);
        };
        csr.neighbors_with_i32_prop_vec(src, prop_name, ctx)
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

    pub fn post_place(&self, post: u32) -> Option<u32> {
        self.single_value("post_place", post)
    }

    pub fn comment_place(&self, comment: u32) -> Option<u32> {
        self.single_value("comment_place", comment)
    }

    pub fn organisation_place(&self, organisation: u32) -> Option<u32> {
        self.single_value("organisation_place", organisation)
    }

    pub fn forum_moderator(&self, forum: u32) -> Option<u32> {
        self.single_value("forum_moderator", forum)
    }

    pub fn tag_tagclass(&self, tag: u32) -> Option<u32> {
        self.single_value("tag_tagclass", tag)
    }

    pub fn place_parent(&self, place: u32) -> Option<u32> {
        self.single_value("place_parent", place)
    }

    pub fn tagclass_parent(&self, tagclass: u32) -> Option<u32> {
        self.single_value("tagclass_parent", tagclass)
    }

    pub fn post_root_post(&self, post: u32) -> Option<u32> {
        self.derived_value("post_root_post", post)
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

    pub fn has_vertex_properties(&self) -> bool {
        !self.catalog.vertex_properties.is_empty()
    }

    pub fn has_edge_properties(&self) -> bool {
        [
            "KNOWS/OUT",
            "KNOWS/IN",
            "PERSON_LIKES_POST/OUT",
            "PERSON_LIKES_POST/IN",
            "PERSON_LIKES_COMMENT/OUT",
            "PERSON_LIKES_COMMENT/IN",
            "FORUM_HAS_MEMBER/OUT",
            "FORUM_HAS_MEMBER/IN",
            "WORK_AT/OUT",
            "WORK_AT/IN",
            "STUDY_AT/OUT",
            "STUDY_AT/IN",
        ]
        .iter()
        .all(|name| {
            self.catalog
                .csr_adjacencies
                .get(*name)
                .map(|entry| !entry.props.is_empty())
                .unwrap_or(false)
        })
    }

    pub fn read_i64_property(&self, label: &str, name: &str) -> Result<Vec<i64>> {
        let entry = self
            .catalog
            .vertex_properties
            .get(&format!("{label}.{name}"))
            .ok_or_else(|| anyhow::anyhow!("missing vertex property {label}.{name}"))?;
        if entry.value_type != "i64" {
            anyhow::bail!(
                "vertex property {label}.{name} is {}, expected i64",
                entry.value_type
            );
        }
        read_i64_column(&self.base_dir.join(&entry.file))
    }

    pub fn read_string_property(&self, label: &str, name: &str) -> Result<Vec<String>> {
        let entry = self
            .catalog
            .vertex_properties
            .get(&format!("{label}.{name}"))
            .ok_or_else(|| anyhow::anyhow!("missing vertex property {label}.{name}"))?;
        if entry.value_type != "string" {
            anyhow::bail!(
                "vertex property {label}.{name} is {}, expected string",
                entry.value_type
            );
        }
        read_string_column(&self.base_dir.join(&entry.file))
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
