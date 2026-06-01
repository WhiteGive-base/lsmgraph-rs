use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufWriter, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::base_graph::catalog::CsrCatalogEntry;
use crate::base_graph::column::read_u64_column;
use crate::base_graph::io::IoConfig;
use crate::error::Result;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum CsrId {
    KnowsOut,
    KnowsIn,
    PersonLikesPostOut,
    PersonLikesPostIn,
    PersonLikesCommentOut,
    PersonLikesCommentIn,
    ForumHasMemberOut,
    ForumHasMemberIn,
    PersonHasInterestOut,
    PersonHasInterestIn,
    PostHasTagOut,
    PostHasTagIn,
    CommentHasTagOut,
    CommentHasTagIn,
    ForumHasTagOut,
    ForumHasTagIn,
    WorkAtOut,
    WorkAtIn,
    StudyAtOut,
    StudyAtIn,
    PersonCreatedPost,
    PersonCreatedComment,
    PostChildComments,
    CommentChildComments,
    ForumContainerOfPost,
}

impl CsrId {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::KnowsOut => "KNOWS/OUT",
            Self::KnowsIn => "KNOWS/IN",
            Self::PersonLikesPostOut => "PERSON_LIKES_POST/OUT",
            Self::PersonLikesPostIn => "PERSON_LIKES_POST/IN",
            Self::PersonLikesCommentOut => "PERSON_LIKES_COMMENT/OUT",
            Self::PersonLikesCommentIn => "PERSON_LIKES_COMMENT/IN",
            Self::ForumHasMemberOut => "FORUM_HAS_MEMBER/OUT",
            Self::ForumHasMemberIn => "FORUM_HAS_MEMBER/IN",
            Self::PersonHasInterestOut => "PERSON_HAS_INTEREST/OUT",
            Self::PersonHasInterestIn => "PERSON_HAS_INTEREST/IN",
            Self::PostHasTagOut => "POST_HAS_TAG/OUT",
            Self::PostHasTagIn => "POST_HAS_TAG/IN",
            Self::CommentHasTagOut => "COMMENT_HAS_TAG/OUT",
            Self::CommentHasTagIn => "COMMENT_HAS_TAG/IN",
            Self::ForumHasTagOut => "FORUM_HAS_TAG/OUT",
            Self::ForumHasTagIn => "FORUM_HAS_TAG/IN",
            Self::WorkAtOut => "WORK_AT/OUT",
            Self::WorkAtIn => "WORK_AT/IN",
            Self::StudyAtOut => "STUDY_AT/OUT",
            Self::StudyAtIn => "STUDY_AT/IN",
            Self::PersonCreatedPost => "PERSON_CREATED_POST",
            Self::PersonCreatedComment => "PERSON_CREATED_COMMENT",
            Self::PostChildComments => "POST_CHILD_COMMENTS",
            Self::CommentChildComments => "COMMENT_CHILD_COMMENTS",
            Self::ForumContainerOfPost => "FORUM_CONTAINER_OF_POST",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum SortOrder {
    DstId,
    CreationDateDesc,
    JoinDateDesc,
    PersonId,
    TagId,
    Unsorted,
}

impl SortOrder {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::DstId => "dst_id",
            Self::CreationDateDesc => "creation_date_desc",
            Self::JoinDateDesc => "join_date_desc",
            Self::PersonId => "person_id",
            Self::TagId => "tag_id",
            Self::Unsorted => "unsorted",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum CachePolicy {
    Cache,
    Bypass,
    CacheFirstNBlocks(usize),
}

#[derive(Debug)]
pub struct ReadContext {
    pub worker_id: usize,
    pub scratch_u32: Vec<u32>,
    pub read_chunks: u64,
    pub read_items: u64,
    pub config: IoConfig,
}

impl ReadContext {
    pub fn new(worker_id: usize, config: IoConfig) -> Self {
        Self {
            worker_id,
            scratch_u32: Vec::new(),
            read_chunks: 0,
            read_items: 0,
            config,
        }
    }
}

#[derive(Debug, Clone)]
pub struct CsrAdjacency {
    pub id: String,
    pub offsets: Vec<u64>,
    pub neighbors_path: PathBuf,
    pub prop_paths: BTreeMap<String, (PathBuf, String)>,
    pub sort_order: SortOrder,
}

impl CsrAdjacency {
    pub fn open(base_dir: &Path, entry: &CsrCatalogEntry) -> Result<Self> {
        let path = base_dir.join(&entry.path);
        let offsets = read_u64_column(&path.join("offsets.bin"))?;
        Ok(Self {
            id: entry.name.clone(),
            offsets,
            neighbors_path: path.join("neighbors.bin"),
            prop_paths: entry
                .props
                .iter()
                .map(|prop| {
                    (
                        prop.name.clone(),
                        (path.join(&prop.file), prop.value_type.clone()),
                    )
                })
                .collect(),
            sort_order: match entry.sort_order.as_str() {
                "creation_date_desc" => SortOrder::CreationDateDesc,
                "join_date_desc" => SortOrder::JoinDateDesc,
                "person_id" => SortOrder::PersonId,
                "tag_id" => SortOrder::TagId,
                "unsorted" => SortOrder::Unsorted,
                _ => SortOrder::DstId,
            },
        })
    }

    pub fn range(&self, src: u32) -> Option<(u64, u64)> {
        let idx = src as usize;
        (idx + 1 < self.offsets.len()).then(|| (self.offsets[idx], self.offsets[idx + 1]))
    }

    pub fn scan_chunks<F>(&self, src: u32, ctx: &mut ReadContext, mut f: F) -> Result<()>
    where
        F: FnMut(&[u32]) -> Result<()>,
    {
        let Some((start, end)) = self.range(src) else {
            return Ok(());
        };
        if start == end {
            return Ok(());
        }
        let degree = end - start;
        let chunk_items = choose_chunk_items(degree, &ctx.config);
        let mut file = File::open(&self.neighbors_path)?;
        let mut pos = start;
        while pos < end {
            let n = chunk_items.min((end - pos) as usize);
            ctx.scratch_u32.clear();
            ctx.scratch_u32.resize(n, 0);
            let byte_offset = pos * 4;
            file.seek(SeekFrom::Start(byte_offset))?;
            let mut bytes = vec![0u8; n * 4];
            file.read_exact(&mut bytes)?;
            for (slot, chunk) in ctx.scratch_u32.iter_mut().zip(bytes.chunks_exact(4)) {
                *slot = u32::from_le_bytes(chunk.try_into().unwrap());
            }
            ctx.read_chunks += 1;
            ctx.read_items += n as u64;
            f(&ctx.scratch_u32)?;
            pos += n as u64;
        }
        Ok(())
    }

    pub fn neighbors_vec(&self, src: u32, ctx: &mut ReadContext) -> Result<Vec<u32>> {
        let mut out = Vec::new();
        self.scan_chunks(src, ctx, |chunk| {
            out.extend_from_slice(chunk);
            Ok(())
        })?;
        Ok(out)
    }

    pub fn neighbors_with_i64_prop_vec(
        &self,
        src: u32,
        prop_name: &str,
        _ctx: &mut ReadContext,
    ) -> Result<Option<Vec<(u32, i64)>>> {
        let Some((prop_path, value_type)) = self.prop_paths.get(prop_name) else {
            return Ok(None);
        };
        if value_type != "i64" {
            return Ok(None);
        }
        let Some((start, end)) = self.range(src) else {
            return Ok(Some(Vec::new()));
        };
        let len = (end - start) as usize;
        let mut neighbors = vec![0u8; len * 4];
        let mut props = vec![0u8; len * 8];
        let mut neighbor_file = File::open(&self.neighbors_path)?;
        neighbor_file.seek(SeekFrom::Start(start * 4))?;
        neighbor_file.read_exact(&mut neighbors)?;
        let mut prop_file = File::open(prop_path)?;
        prop_file.seek(SeekFrom::Start(start * 8))?;
        prop_file.read_exact(&mut props)?;
        let out = neighbors
            .chunks_exact(4)
            .zip(props.chunks_exact(8))
            .map(|(neighbor, prop)| {
                (
                    u32::from_le_bytes(neighbor.try_into().unwrap()),
                    i64::from_le_bytes(prop.try_into().unwrap()),
                )
            })
            .collect();
        Ok(Some(out))
    }

    pub fn neighbors_with_i32_prop_vec(
        &self,
        src: u32,
        prop_name: &str,
        _ctx: &mut ReadContext,
    ) -> Result<Option<Vec<(u32, i32)>>> {
        let Some((prop_path, value_type)) = self.prop_paths.get(prop_name) else {
            return Ok(None);
        };
        if value_type != "i32" {
            return Ok(None);
        }
        let Some((start, end)) = self.range(src) else {
            return Ok(Some(Vec::new()));
        };
        let len = (end - start) as usize;
        let mut neighbors = vec![0u8; len * 4];
        let mut props = vec![0u8; len * 4];
        let mut neighbor_file = File::open(&self.neighbors_path)?;
        neighbor_file.seek(SeekFrom::Start(start * 4))?;
        neighbor_file.read_exact(&mut neighbors)?;
        let mut prop_file = File::open(prop_path)?;
        prop_file.seek(SeekFrom::Start(start * 4))?;
        prop_file.read_exact(&mut props)?;
        let out = neighbors
            .chunks_exact(4)
            .zip(props.chunks_exact(4))
            .map(|(neighbor, prop)| {
                (
                    u32::from_le_bytes(neighbor.try_into().unwrap()),
                    i32::from_le_bytes(prop.try_into().unwrap()),
                )
            })
            .collect();
        Ok(Some(out))
    }
}

pub fn write_csr(
    dir: &Path,
    num_src_vertices: usize,
    mut edges: Vec<(u32, u32)>,
    sort_order: SortOrder,
) -> Result<u64> {
    std::fs::create_dir_all(dir)?;
    edges.sort_unstable_by_key(|(src, dst)| (*src, *dst));
    let mut offsets = vec![0u64; num_src_vertices + 1];
    for (src, _) in &edges {
        let idx = *src as usize;
        if idx < num_src_vertices {
            offsets[idx + 1] += 1;
        }
    }
    for idx in 1..offsets.len() {
        offsets[idx] += offsets[idx - 1];
    }
    write_u64s(&dir.join("offsets.bin"), &offsets)?;
    let neighbors: Vec<u32> = edges.into_iter().map(|(_, dst)| dst).collect();
    write_u32s(&dir.join("neighbors.bin"), &neighbors)?;
    let meta = serde_json::json!({
        "num_src_vertices": num_src_vertices,
        "num_edges": neighbors.len(),
        "sort_order": sort_order.as_str(),
    });
    let mut writer = BufWriter::new(File::create(dir.join("meta.json"))?);
    serde_json::to_writer_pretty(&mut writer, &meta)?;
    writer.flush()?;
    Ok(neighbors.len() as u64)
}

pub fn write_csr_i64_prop(
    dir: &Path,
    num_src_vertices: usize,
    mut edges: Vec<(u32, u32, i64)>,
    sort_order: SortOrder,
    prop_file: &str,
) -> Result<u64> {
    std::fs::create_dir_all(dir)?;
    edges.sort_unstable_by_key(|(src, dst, _)| (*src, *dst));
    let mut offsets = vec![0u64; num_src_vertices + 1];
    for (src, _, _) in &edges {
        let idx = *src as usize;
        if idx < num_src_vertices {
            offsets[idx + 1] += 1;
        }
    }
    for idx in 1..offsets.len() {
        offsets[idx] += offsets[idx - 1];
    }
    write_u64s(&dir.join("offsets.bin"), &offsets)?;
    let mut neighbors = Vec::with_capacity(edges.len());
    let mut props = Vec::with_capacity(edges.len());
    for (_, dst, prop) in edges {
        neighbors.push(dst);
        props.push(prop);
    }
    write_u32s(&dir.join("neighbors.bin"), &neighbors)?;
    write_i64s(&dir.join(prop_file), &props)?;
    let meta = serde_json::json!({
        "num_src_vertices": num_src_vertices,
        "num_edges": neighbors.len(),
        "sort_order": sort_order.as_str(),
        "props": [{"file": prop_file, "type": "i64"}],
    });
    let mut writer = BufWriter::new(File::create(dir.join("meta.json"))?);
    serde_json::to_writer_pretty(&mut writer, &meta)?;
    writer.flush()?;
    Ok(neighbors.len() as u64)
}

pub fn write_csr_i32_prop(
    dir: &Path,
    num_src_vertices: usize,
    mut edges: Vec<(u32, u32, i32)>,
    sort_order: SortOrder,
    prop_file: &str,
) -> Result<u64> {
    std::fs::create_dir_all(dir)?;
    edges.sort_unstable_by_key(|(src, dst, _)| (*src, *dst));
    let mut offsets = vec![0u64; num_src_vertices + 1];
    for (src, _, _) in &edges {
        let idx = *src as usize;
        if idx < num_src_vertices {
            offsets[idx + 1] += 1;
        }
    }
    for idx in 1..offsets.len() {
        offsets[idx] += offsets[idx - 1];
    }
    write_u64s(&dir.join("offsets.bin"), &offsets)?;
    let mut neighbors = Vec::with_capacity(edges.len());
    let mut props = Vec::with_capacity(edges.len());
    for (_, dst, prop) in edges {
        neighbors.push(dst);
        props.push(prop);
    }
    write_u32s(&dir.join("neighbors.bin"), &neighbors)?;
    write_i32s(&dir.join(prop_file), &props)?;
    let meta = serde_json::json!({
        "num_src_vertices": num_src_vertices,
        "num_edges": neighbors.len(),
        "sort_order": sort_order.as_str(),
        "props": [{"file": prop_file, "type": "i32"}],
    });
    let mut writer = BufWriter::new(File::create(dir.join("meta.json"))?);
    serde_json::to_writer_pretty(&mut writer, &meta)?;
    writer.flush()?;
    Ok(neighbors.len() as u64)
}

fn write_u64s(path: &Path, values: &[u64]) -> Result<()> {
    let mut writer = BufWriter::new(File::create(path)?);
    for value in values {
        writer.write_all(&value.to_le_bytes())?;
    }
    writer.flush()?;
    Ok(())
}

fn write_u32s(path: &Path, values: &[u32]) -> Result<()> {
    let mut writer = BufWriter::new(File::create(path)?);
    for value in values {
        writer.write_all(&value.to_le_bytes())?;
    }
    writer.flush()?;
    Ok(())
}

fn write_i32s(path: &Path, values: &[i32]) -> Result<()> {
    let mut writer = BufWriter::new(File::create(path)?);
    for value in values {
        writer.write_all(&value.to_le_bytes())?;
    }
    writer.flush()?;
    Ok(())
}

fn write_i64s(path: &Path, values: &[i64]) -> Result<()> {
    let mut writer = BufWriter::new(File::create(path)?);
    for value in values {
        writer.write_all(&value.to_le_bytes())?;
    }
    writer.flush()?;
    Ok(())
}

fn choose_chunk_items(degree: u64, config: &IoConfig) -> usize {
    if degree <= config.small_degree_threshold {
        degree.max(1) as usize
    } else {
        (config.neighbor_block_bytes / std::mem::size_of::<u32>()).max(1)
    }
}
