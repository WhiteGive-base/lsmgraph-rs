use std::collections::HashMap;
use std::fs::{self, File};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::Path;
use std::sync::Arc;

use serde::{Deserialize, Serialize};

use crate::error::Result;
use crate::graph::Engine;
use crate::types::{EdgeLabel, EdgeRecord, EdgeType, Timestamp, VertexId, VertexLabel};

const LABEL_SHIFT: u64 = 56;
const EXTERNAL_MASK: u64 = (1u64 << LABEL_SHIFT) - 1;

pub fn encode_vid(label: VertexLabel, external_id: i64) -> VertexId {
    ((label as u64) << LABEL_SHIFT) | (external_id as u64 & EXTERNAL_MASK)
}

pub fn external_id(vid: VertexId) -> i64 {
    (vid & EXTERNAL_MASK) as i64
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PersonProps {
    pub id: i64,
    pub first_name: String,
    pub last_name: String,
    pub gender: String,
    pub birthday: i64,
    pub creation_date: i64,
    pub location_ip: String,
    pub browser_used: String,
    pub place: i64,
    pub language: String,
    pub email: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PlaceProps {
    pub id: i64,
    pub name: String,
    pub place_type: String,
    pub is_part_of: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrgProps {
    pub id: i64,
    pub org_type: String,
    pub name: String,
    pub place: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PostProps {
    pub id: i64,
    pub image_file: String,
    pub creation_date: i64,
    pub location_ip: String,
    pub browser_used: String,
    pub language: String,
    pub content: String,
    pub length: i64,
    pub creator: i64,
    pub forum_id: i64,
    pub place: i64,
}

impl PostProps {
    pub fn content_or_image(&self) -> &str {
        if self.content.is_empty() {
            &self.image_file
        } else {
            &self.content
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CommentProps {
    pub id: i64,
    pub creation_date: i64,
    pub location_ip: String,
    pub browser_used: String,
    pub content: String,
    pub length: i64,
    pub creator: i64,
    pub place: i64,
    pub reply_of_post: Option<i64>,
    pub reply_of_comment: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ForumProps {
    pub id: i64,
    pub title: String,
    pub creation_date: i64,
    pub moderator: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TagProps {
    pub id: i64,
    pub name: String,
    pub has_type: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TagClassProps {
    pub id: i64,
    pub name: String,
    pub is_subclass_of: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind")]
pub enum VertexData {
    Person(PersonProps),
    Place(PlaceProps),
    Organisation(OrgProps),
    Post(PostProps),
    Comment(CommentProps),
    Forum(ForumProps),
    Tag(TagProps),
    TagClass(TagClassProps),
}

impl VertexData {
    pub fn label(&self) -> VertexLabel {
        match self {
            Self::Person(_) => VertexLabel::Person,
            Self::Place(_) => VertexLabel::Place,
            Self::Organisation(_) => VertexLabel::Organisation,
            Self::Post(_) => VertexLabel::Post,
            Self::Comment(_) => VertexLabel::Comment,
            Self::Forum(_) => VertexLabel::Forum,
            Self::Tag(_) => VertexLabel::Tag,
            Self::TagClass(_) => VertexLabel::TagClass,
        }
    }

    pub fn external_id(&self) -> i64 {
        match self {
            Self::Person(p) => p.id,
            Self::Place(p) => p.id,
            Self::Organisation(p) => p.id,
            Self::Post(p) => p.id,
            Self::Comment(p) => p.id,
            Self::Forum(p) => p.id,
            Self::Tag(p) => p.id,
            Self::TagClass(p) => p.id,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VertexRow {
    pub vid: VertexId,
    pub data: VertexData,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub enum EdgeProp {
    Empty,
    I32(i32),
    I64(i64),
}

impl EdgeProp {
    pub fn as_i32(self) -> i32 {
        match self {
            Self::I32(v) => v,
            Self::I64(v) => v as i32,
            Self::Empty => 0,
        }
    }

    pub fn as_i64(self) -> i64 {
        match self {
            Self::I32(v) => v as i64,
            Self::I64(v) => v,
            Self::Empty => 0,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EdgePropRow {
    pub src: VertexId,
    pub dst: VertexId,
    pub edge_type: EdgeType,
    pub prop: EdgeProp,
}

pub struct SnbGraph {
    pub engine: Arc<Engine>,
    pub snapshot: u64,
    vertices: HashMap<VertexId, VertexData>,
    edge_props: HashMap<(VertexId, EdgeType, VertexId), EdgeProp>,
    adjacency: HashMap<(VertexId, EdgeType), Vec<VertexId>>,
}

impl SnbGraph {
    pub async fn open(engine: Arc<Engine>, store_dir: &Path) -> Result<Self> {
        let vertices = load_vertices(&store_dir.join("snb_vertices.jsonl"))?;
        let edge_props = load_edge_props(&store_dir.join("snb_edge_props.jsonl"))?;
        let snapshot = engine.current_snapshot();
        let cache_path = store_dir.join("snb_adjacency.bin");
        let adjacency = if cache_path.exists() {
            load_adjacency_cache(&cache_path)?
        } else {
            let adjacency = build_adjacency_from_edges(engine.scan_edges(snapshot).await?);
            write_adjacency_cache_atomic(&cache_path, &adjacency)?;
            adjacency
        };
        Ok(Self {
            engine,
            snapshot,
            vertices,
            edge_props,
            adjacency,
        })
    }

    pub fn lookup(&self, label: VertexLabel, external_id: i64) -> VertexId {
        encode_vid(label, external_id)
    }

    pub fn vertex(&self, vid: VertexId) -> Option<&VertexData> {
        self.vertices.get(&vid)
    }

    pub fn insert_vertex(&mut self, data: VertexData) {
        let vid = encode_vid(data.label(), data.external_id());
        self.vertices.insert(vid, data);
    }

    pub fn insert_edge_cached(
        &mut self,
        src: VertexId,
        dst: VertexId,
        edge_label: EdgeLabel,
        prop: EdgeProp,
        include_reverse: bool,
    ) {
        let edge_type = edge_label.as_i32();
        self.insert_edge_by_type(src, dst, edge_type, prop);
        if include_reverse {
            self.insert_edge_by_type(dst, src, -edge_type, prop);
        }
    }

    pub fn insert_edge_by_type(
        &mut self,
        src: VertexId,
        dst: VertexId,
        edge_type: EdgeType,
        prop: EdgeProp,
    ) {
        let dsts = self.adjacency.entry((src, edge_type)).or_default();
        if !dsts.contains(&dst) {
            dsts.push(dst);
        }
        if !matches!(prop, EdgeProp::Empty) {
            self.edge_props.insert((src, edge_type, dst), prop);
        }
    }

    pub fn person(&self, vid: VertexId) -> Option<&PersonProps> {
        match self.vertices.get(&vid)? {
            VertexData::Person(p) => Some(p),
            _ => None,
        }
    }

    pub fn place(&self, vid: VertexId) -> Option<&PlaceProps> {
        match self.vertices.get(&vid)? {
            VertexData::Place(p) => Some(p),
            _ => None,
        }
    }

    pub fn organisation(&self, vid: VertexId) -> Option<&OrgProps> {
        match self.vertices.get(&vid)? {
            VertexData::Organisation(p) => Some(p),
            _ => None,
        }
    }

    pub fn post(&self, vid: VertexId) -> Option<&PostProps> {
        match self.vertices.get(&vid)? {
            VertexData::Post(p) => Some(p),
            _ => None,
        }
    }

    pub fn comment(&self, vid: VertexId) -> Option<&CommentProps> {
        match self.vertices.get(&vid)? {
            VertexData::Comment(p) => Some(p),
            _ => None,
        }
    }

    pub fn forum(&self, vid: VertexId) -> Option<&ForumProps> {
        match self.vertices.get(&vid)? {
            VertexData::Forum(p) => Some(p),
            _ => None,
        }
    }

    pub fn tag(&self, vid: VertexId) -> Option<&TagProps> {
        match self.vertices.get(&vid)? {
            VertexData::Tag(p) => Some(p),
            _ => None,
        }
    }

    pub fn tag_class(&self, vid: VertexId) -> Option<&TagClassProps> {
        match self.vertices.get(&vid)? {
            VertexData::TagClass(p) => Some(p),
            _ => None,
        }
    }

    pub fn vertex_label(&self, vid: VertexId) -> Option<VertexLabel> {
        self.vertex(vid).map(VertexData::label)
    }

    pub fn vertices_by_label(&self, label: VertexLabel) -> impl Iterator<Item = VertexId> + '_ {
        self.vertices.iter().filter_map(move |(&vid, data)| {
            if data.label() == label {
                Some(vid)
            } else {
                None
            }
        })
    }

    pub fn place_name(&self, vid: VertexId) -> String {
        self.place(vid).map(|p| p.name.clone()).unwrap_or_default()
    }

    pub fn out_neighbors_cached(&self, vid: VertexId, edge_label: EdgeLabel) -> Vec<VertexId> {
        self.adjacency
            .get(&(vid, edge_label.as_i32()))
            .cloned()
            .unwrap_or_default()
    }

    pub fn in_neighbors_cached(&self, vid: VertexId, edge_label: EdgeLabel) -> Vec<VertexId> {
        self.adjacency
            .get(&(vid, -edge_label.as_i32()))
            .cloned()
            .unwrap_or_default()
    }

    pub async fn out_neighbors(
        &self,
        vid: VertexId,
        edge_label: EdgeLabel,
    ) -> Result<Vec<VertexId>> {
        Ok(self.out_neighbors_cached(vid, edge_label))
    }

    pub async fn in_neighbors(
        &self,
        vid: VertexId,
        edge_label: EdgeLabel,
    ) -> Result<Vec<VertexId>> {
        Ok(self.in_neighbors_cached(vid, edge_label))
    }

    pub async fn knows_neighbors(&self, vid: VertexId) -> Result<Vec<VertexId>> {
        self.out_neighbors(vid, EdgeLabel::Knows).await
    }

    pub async fn edge_prop(&self, src: VertexId, edge_label: EdgeLabel, dst: VertexId) -> EdgeProp {
        self.edge_prop_by_type(src, edge_label.as_i32(), dst)
    }

    pub fn edge_prop_by_type(&self, src: VertexId, edge_type: EdgeType, dst: VertexId) -> EdgeProp {
        self.edge_props
            .get(&(src, edge_type, dst))
            .copied()
            .unwrap_or(EdgeProp::Empty)
    }

    pub fn out_edges_with_prop(
        &self,
        vid: VertexId,
        edge_label: EdgeLabel,
    ) -> Vec<(VertexId, EdgeProp)> {
        let edge_type = edge_label.as_i32();
        self.out_neighbors_cached(vid, edge_label)
            .into_iter()
            .map(|dst| (dst, self.edge_prop_by_type(vid, edge_type, dst)))
            .collect()
    }

    pub fn in_edges_with_prop(
        &self,
        vid: VertexId,
        edge_label: EdgeLabel,
    ) -> Vec<(VertexId, EdgeProp)> {
        let edge_type = -edge_label.as_i32();
        self.in_neighbors_cached(vid, edge_label)
            .into_iter()
            .map(|src| (src, self.edge_prop_by_type(vid, edge_type, src)))
            .collect()
    }

    pub async fn build_adjacency_cache(engine: Arc<Engine>, store_dir: &Path) -> Result<usize> {
        let snapshot = engine.current_snapshot();
        let adjacency = build_adjacency_from_edges(engine.scan_edges(snapshot).await?);
        let count = adjacency.len();
        write_adjacency_cache_atomic(&store_dir.join("snb_adjacency.bin"), &adjacency)?;
        Ok(count)
    }
}

fn load_vertices(path: &Path) -> Result<HashMap<VertexId, VertexData>> {
    let file = File::open(path)?;
    let reader = BufReader::new(file);
    let mut out = HashMap::new();
    for line in reader.lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let row: VertexRow = serde_json::from_str(&line)?;
        out.insert(row.vid, row.data);
    }
    Ok(out)
}

fn load_edge_props(path: &Path) -> Result<HashMap<(VertexId, EdgeType, VertexId), EdgeProp>> {
    if !path.exists() {
        return Ok(HashMap::new());
    }
    let file = File::open(path)?;
    let reader = BufReader::new(file);
    let mut out = HashMap::new();
    for line in reader.lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let row: EdgePropRow = serde_json::from_str(&line)?;
        out.insert((row.src, row.edge_type, row.dst), row.prop);
    }
    Ok(out)
}

const ADJ_MAGIC: &[u8; 8] = b"LSMADJ01";
const ADJ_HEADER_LEN: usize = 32;
const ADJ_GROUP_LEN: usize = 32;

fn build_adjacency_from_edges(
    edges: Vec<EdgeRecord>,
) -> HashMap<(VertexId, EdgeType), Vec<VertexId>> {
    let mut grouped: HashMap<(VertexId, EdgeType), Vec<(VertexId, Timestamp)>> = HashMap::new();
    for edge in edges {
        grouped
            .entry((edge.src, edge.edge_type))
            .or_default()
            .push((edge.dst, edge.ts));
    }
    let mut adjacency = HashMap::with_capacity(grouped.len());
    for (key, mut dsts) in grouped {
        dsts.sort_unstable_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
        dsts.dedup_by_key(|(dst, _)| *dst);
        adjacency.insert(key, dsts.into_iter().map(|(dst, _)| dst).collect());
    }
    adjacency
}

fn write_adjacency_cache_atomic(
    path: &Path,
    adjacency: &HashMap<(VertexId, EdgeType), Vec<VertexId>>,
) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp_path = path.with_extension("bin.tmp");
    write_adjacency_cache(&tmp_path, adjacency)?;
    fs::rename(tmp_path, path)?;
    Ok(())
}

fn write_adjacency_cache(
    path: &Path,
    adjacency: &HashMap<(VertexId, EdgeType), Vec<VertexId>>,
) -> Result<()> {
    let mut groups: Vec<_> = adjacency.iter().collect();
    groups.sort_by_key(|((src, edge_type), _)| (*src, *edge_type));
    let total_edges: usize = groups.iter().map(|(_, dsts)| dsts.len()).sum();
    let mut file = File::create(path)?;
    file.write_all(ADJ_MAGIC)?;
    file.write_all(&(1u32).to_le_bytes())?;
    file.write_all(&(ADJ_HEADER_LEN as u32).to_le_bytes())?;
    file.write_all(&(groups.len() as u64).to_le_bytes())?;
    file.write_all(&(total_edges as u64).to_le_bytes())?;

    let mut first_idx = 0u64;
    for ((src, edge_type), dsts) in &groups {
        file.write_all(&src.to_le_bytes())?;
        file.write_all(&edge_type.to_le_bytes())?;
        file.write_all(&0u32.to_le_bytes())?;
        file.write_all(&first_idx.to_le_bytes())?;
        file.write_all(&(dsts.len() as u64).to_le_bytes())?;
        first_idx += dsts.len() as u64;
    }
    for (_, dsts) in groups {
        for dst in dsts {
            file.write_all(&dst.to_le_bytes())?;
        }
    }
    file.sync_all()?;
    Ok(())
}

fn load_adjacency_cache(path: &Path) -> Result<HashMap<(VertexId, EdgeType), Vec<VertexId>>> {
    let mut file = File::open(path)?;
    let mut header = [0u8; ADJ_HEADER_LEN];
    file.read_exact(&mut header)?;
    if &header[0..8] != ADJ_MAGIC {
        anyhow::bail!("bad adjacency cache magic in {}", path.display());
    }
    let version = u32::from_le_bytes(header[8..12].try_into().unwrap());
    if version != 1 {
        anyhow::bail!("unsupported adjacency cache version {version}");
    }
    let group_count = u64::from_le_bytes(header[16..24].try_into().unwrap()) as usize;
    let edge_count = u64::from_le_bytes(header[24..32].try_into().unwrap()) as usize;

    let mut group_bytes = vec![0u8; group_count * ADJ_GROUP_LEN];
    file.read_exact(&mut group_bytes)?;
    let mut dsts = vec![0u8; edge_count * 8];
    file.read_exact(&mut dsts)?;

    let mut out = HashMap::with_capacity(group_count);
    for chunk in group_bytes.chunks_exact(ADJ_GROUP_LEN) {
        let src = u64::from_le_bytes(chunk[0..8].try_into().unwrap());
        let edge_type = i32::from_le_bytes(chunk[8..12].try_into().unwrap());
        let first = u64::from_le_bytes(chunk[16..24].try_into().unwrap()) as usize;
        let count = u64::from_le_bytes(chunk[24..32].try_into().unwrap()) as usize;
        let mut list = Vec::with_capacity(count);
        let start = first * 8;
        let end = start + count * 8;
        for dst_chunk in dsts[start..end].chunks_exact(8) {
            list.push(u64::from_le_bytes(dst_chunk.try_into().unwrap()));
        }
        out.insert((src, edge_type), list);
    }
    Ok(out)
}
