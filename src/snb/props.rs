use std::collections::HashMap;
use std::fs::{self, File};
use std::io::{BufRead, BufReader, BufWriter, Read, Write};
use std::path::Path;
use std::sync::Arc;
use std::time::Instant;

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
        let started = Instant::now();
        let snapshot = engine.current_snapshot();
        eprintln!("[snb-cache] scanning edges snapshot={snapshot}");
        let edges = engine.scan_edges(snapshot).await?;
        eprintln!(
            "[snb-cache] scanned edges={} elapsed_s={:.1}",
            edges.len(),
            started.elapsed().as_secs_f64()
        );
        let adjacency = build_adjacency_from_edges(edges);
        let count = adjacency.len();
        eprintln!(
            "[snb-cache] writing adjacency cache groups={} path={}",
            count,
            store_dir.join("snb_adjacency.bin").display()
        );
        let write_stats =
            write_adjacency_cache_atomic(&store_dir.join("snb_adjacency.bin"), &adjacency)?;
        eprintln!(
            "[snb-cache] write complete groups={} edges={} group_bytes={} dst_bytes={} write_elapsed_s={:.1} sync_elapsed_s={:.1} total_elapsed_s={:.1}",
            write_stats.group_count,
            write_stats.edge_count,
            write_stats.encoded_group_bytes,
            write_stats.encoded_dst_bytes,
            write_stats.write_elapsed_s,
            write_stats.sync_elapsed_s,
            started.elapsed().as_secs_f64()
        );
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
const ADJ_WRITE_BUFFER_BYTES: usize = 64 * 1024 * 1024;
const ADJ_DST_CHUNK: usize = 1_000_000;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct AdjacencyBuildStats {
    pub groups: usize,
    pub directed_edges: usize,
    pub visible_edges: usize,
    pub duplicate_edges_removed: usize,
}

#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct AdjacencyCacheWriteStats {
    pub group_count: usize,
    pub edge_count: usize,
    pub encoded_group_bytes: u64,
    pub encoded_dst_bytes: u64,
    pub write_elapsed_s: f64,
    pub sync_elapsed_s: f64,
}

#[derive(Debug, Default)]
pub struct AdjacencyBuilder {
    grouped: HashMap<(VertexId, EdgeType), Vec<(VertexId, Timestamp)>>,
    directed_edges: usize,
}

impl AdjacencyBuilder {
    pub fn from_adjacency(
        adjacency: HashMap<(VertexId, EdgeType), Vec<VertexId>>,
        max_existing_ts: Timestamp,
    ) -> Self {
        let mut builder = Self::default();
        for ((src, edge_type), dsts) in adjacency {
            for (idx, dst) in dsts.into_iter().enumerate() {
                let ts = max_existing_ts.saturating_sub(idx as u64);
                builder.record_edge(src, edge_type, dst, ts);
            }
        }
        builder
    }

    pub fn record_edge(
        &mut self,
        src: VertexId,
        edge_type: EdgeType,
        dst: VertexId,
        ts: Timestamp,
    ) {
        self.grouped
            .entry((src, edge_type))
            .or_default()
            .push((dst, ts));
        self.directed_edges += 1;
    }

    pub fn directed_edges(&self) -> usize {
        self.directed_edges
    }

    pub fn build(
        self,
    ) -> (
        HashMap<(VertexId, EdgeType), Vec<VertexId>>,
        AdjacencyBuildStats,
    ) {
        let mut adjacency = HashMap::with_capacity(self.grouped.len());
        let mut visible_edges = 0usize;
        let mut duplicate_edges_removed = 0usize;

        for (key, mut dsts) in self.grouped {
            let original_len = dsts.len();
            dsts.sort_unstable_by(|a, b| a.0.cmp(&b.0).then_with(|| b.1.cmp(&a.1)));
            dsts.dedup_by_key(|(dst, _)| *dst);
            dsts.sort_unstable_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
            visible_edges += dsts.len();
            duplicate_edges_removed += original_len - dsts.len();
            adjacency.insert(key, dsts.into_iter().map(|(dst, _)| dst).collect());
        }

        let stats = AdjacencyBuildStats {
            groups: adjacency.len(),
            directed_edges: self.directed_edges,
            visible_edges,
            duplicate_edges_removed,
        };
        (adjacency, stats)
    }
}

fn build_adjacency_from_edges(
    edges: Vec<EdgeRecord>,
) -> HashMap<(VertexId, EdgeType), Vec<VertexId>> {
    let mut builder = AdjacencyBuilder::default();
    for edge in edges {
        builder.record_edge(edge.src, edge.edge_type, edge.dst, edge.ts);
    }
    let (adjacency, _) = builder.build();
    adjacency
}

pub fn write_adjacency_cache_atomic(
    path: &Path,
    adjacency: &HashMap<(VertexId, EdgeType), Vec<VertexId>>,
) -> Result<AdjacencyCacheWriteStats> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp_path = path.with_extension("bin.tmp");
    let stats = write_adjacency_cache(&tmp_path, adjacency)?;
    fs::rename(tmp_path, path)?;
    Ok(stats)
}

fn write_adjacency_cache(
    path: &Path,
    adjacency: &HashMap<(VertexId, EdgeType), Vec<VertexId>>,
) -> Result<AdjacencyCacheWriteStats> {
    let started = Instant::now();
    let mut groups: Vec<_> = adjacency.iter().collect();
    groups.sort_by_key(|((src, edge_type), _)| (*src, *edge_type));
    let total_edges: usize = groups.iter().map(|(_, dsts)| dsts.len()).sum();
    let file = File::create(path)?;
    let mut writer = BufWriter::with_capacity(ADJ_WRITE_BUFFER_BYTES, file);

    let mut header = [0u8; ADJ_HEADER_LEN];
    header[0..8].copy_from_slice(ADJ_MAGIC);
    header[8..12].copy_from_slice(&1u32.to_le_bytes());
    header[12..16].copy_from_slice(&(ADJ_HEADER_LEN as u32).to_le_bytes());
    header[16..24].copy_from_slice(&(groups.len() as u64).to_le_bytes());
    header[24..32].copy_from_slice(&(total_edges as u64).to_le_bytes());
    writer.write_all(&header)?;

    let mut first_idx = 0u64;
    for ((src, edge_type), dsts) in &groups {
        let mut group = [0u8; ADJ_GROUP_LEN];
        group[0..8].copy_from_slice(&src.to_le_bytes());
        group[8..12].copy_from_slice(&edge_type.to_le_bytes());
        group[12..16].copy_from_slice(&0u32.to_le_bytes());
        group[16..24].copy_from_slice(&first_idx.to_le_bytes());
        group[24..32].copy_from_slice(&(dsts.len() as u64).to_le_bytes());
        writer.write_all(&group)?;
        first_idx += dsts.len() as u64;
    }

    let mut dst_buf = Vec::with_capacity(ADJ_DST_CHUNK * std::mem::size_of::<VertexId>());
    for (_, dsts) in groups {
        for chunk in dsts.chunks(ADJ_DST_CHUNK) {
            dst_buf.clear();
            for dst in chunk {
                dst_buf.extend_from_slice(&dst.to_le_bytes());
            }
            writer.write_all(&dst_buf)?;
        }
    }

    writer.flush()?;
    let write_elapsed_s = started.elapsed().as_secs_f64();
    let sync_started = Instant::now();
    writer.get_ref().sync_all()?;
    let sync_elapsed_s = sync_started.elapsed().as_secs_f64();

    Ok(AdjacencyCacheWriteStats {
        group_count: adjacency.len(),
        edge_count: total_edges,
        encoded_group_bytes: (adjacency.len() * ADJ_GROUP_LEN) as u64,
        encoded_dst_bytes: (total_edges * std::mem::size_of::<VertexId>()) as u64,
        write_elapsed_s,
        sync_elapsed_s,
    })
}

pub fn load_adjacency_cache(path: &Path) -> Result<HashMap<(VertexId, EdgeType), Vec<VertexId>>> {
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

#[cfg(test)]
mod tests {
    use super::*;

    fn target_tempdir(name: &str) -> tempfile::TempDir {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("target")
            .join("test-tmp");
        std::fs::create_dir_all(&root).unwrap();
        tempfile::Builder::new()
            .prefix(name)
            .tempdir_in(root)
            .unwrap()
    }

    #[test]
    fn adjacency_cache_round_trips_current_format() {
        let dir = target_tempdir("adjacency-cache-round-trip-");
        let path = dir.path().join("snb_adjacency.bin");
        let mut adjacency = HashMap::new();
        adjacency.insert((10, 1), vec![20, 21, 22]);
        adjacency.insert((10, -1), vec![30]);
        adjacency.insert((11, 2), vec![40, 41]);

        let stats = write_adjacency_cache_atomic(&path, &adjacency).unwrap();
        assert_eq!(stats.group_count, 3);
        assert_eq!(stats.edge_count, 6);
        assert_eq!(stats.encoded_group_bytes, 3 * ADJ_GROUP_LEN as u64);
        assert_eq!(
            stats.encoded_dst_bytes,
            6 * std::mem::size_of::<VertexId>() as u64
        );

        let loaded = load_adjacency_cache(&path).unwrap();
        assert_eq!(loaded, adjacency);
    }

    #[test]
    fn adjacency_builder_matches_scan_edge_builder() {
        let edges = vec![
            EdgeRecord::insert(1, 10, 7, 1),
            EdgeRecord::insert(1, 11, 7, 2),
            EdgeRecord::insert(1, 10, 7, 3),
            EdgeRecord::insert(1, 12, -7, 4),
            EdgeRecord::insert(2, 20, 7, 5),
        ];
        let expected = build_adjacency_from_edges(edges.clone());

        let mut builder = AdjacencyBuilder::default();
        for edge in edges {
            builder.record_edge(edge.src, edge.edge_type, edge.dst, edge.ts);
        }
        let (actual, stats) = builder.build();

        assert_eq!(actual, expected);
        assert_eq!(stats.groups, 3);
        assert_eq!(stats.directed_edges, 5);
        assert_eq!(stats.visible_edges, 4);
        assert_eq!(stats.duplicate_edges_removed, 1);
        assert_eq!(actual.get(&(1, 7)).unwrap(), &vec![10, 11]);
        assert_eq!(actual.get(&(1, -7)).unwrap(), &vec![12]);
    }
}
