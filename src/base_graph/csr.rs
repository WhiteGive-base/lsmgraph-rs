use std::collections::{BTreeMap, HashMap, VecDeque};
use std::fs::{File, OpenOptions};
use std::io::{BufWriter, Read, Seek, SeekFrom, Write};
#[cfg(all(feature = "direct-io", unix))]
use std::os::fd::AsRawFd;
#[cfg(all(feature = "direct-io", unix))]
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use parking_lot::Mutex;
use serde::{Deserialize, Serialize};

use crate::base_graph::catalog::CsrCatalogEntry;
use crate::base_graph::column::read_u64_column;
use crate::base_graph::io::{IoConfig, ReaderBackendKind};
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
    pub scratch_bytes: Vec<u8>,
    direct_scratch: DirectReadBuffer,
    pub read_chunks: u64,
    pub read_items: u64,
    pub cache_hits: u64,
    pub cache_misses: u64,
    pub direct_reads: u64,
    pub direct_bytes: u64,
    pub blocking_reads: u64,
    pub blocking_bytes: u64,
    pub config: IoConfig,
}

impl ReadContext {
    pub fn new(worker_id: usize, config: IoConfig) -> Self {
        Self {
            worker_id,
            scratch_u32: Vec::new(),
            scratch_bytes: Vec::new(),
            direct_scratch: DirectReadBuffer::new(
                config
                    .neighbor_block_bytes
                    .max(config.prop_block_bytes)
                    .max(4096),
            ),
            read_chunks: 0,
            read_items: 0,
            cache_hits: 0,
            cache_misses: 0,
            direct_reads: 0,
            direct_bytes: 0,
            blocking_reads: 0,
            blocking_bytes: 0,
            config,
        }
    }
}

#[derive(Debug, Clone)]
pub struct CsrAdjacency {
    pub id: String,
    pub offsets: Vec<u64>,
    neighbors_file: CsrDataFile,
    prop_files: BTreeMap<String, (CsrDataFile, String)>,
    pub sort_order: SortOrder,
    block_bytes: usize,
    block_caches: Arc<Mutex<HashMap<usize, CsrBlockCache>>>,
    prop_block_caches: Arc<Mutex<HashMap<(usize, String), CsrBlockCache>>>,
}

#[derive(Debug, Clone)]
struct CsrDataFile {
    file: Arc<Mutex<File>>,
    len: u64,
}

impl CsrDataFile {
    fn open(path: PathBuf, backend: ReaderBackendKind) -> Result<Self> {
        let len = std::fs::metadata(&path)?.len();
        Ok(Self {
            file: Arc::new(Mutex::new(open_data_file(&path, backend)?)),
            len,
        })
    }
}

#[derive(Debug)]
struct CsrBlockCache {
    max_blocks: usize,
    blocks: HashMap<u64, Arc<Vec<u8>>>,
    order: VecDeque<u64>,
}

impl CsrBlockCache {
    fn new(max_blocks: usize) -> Self {
        Self {
            max_blocks,
            blocks: HashMap::new(),
            order: VecDeque::new(),
        }
    }

    fn get(&mut self, block_idx: u64) -> Option<Arc<Vec<u8>>> {
        self.blocks.get(&block_idx).cloned()
    }

    fn insert(&mut self, block_idx: u64, block: Arc<Vec<u8>>) {
        if self.blocks.contains_key(&block_idx) {
            return;
        }
        self.blocks.insert(block_idx, block);
        self.order.push_back(block_idx);
        while self.blocks.len() > self.max_blocks {
            if let Some(evict) = self.order.pop_front() {
                self.blocks.remove(&evict);
            } else {
                break;
            }
        }
    }
}

impl CsrAdjacency {
    pub fn open(
        base_dir: &Path,
        entry: &CsrCatalogEntry,
        backend: ReaderBackendKind,
    ) -> Result<Self> {
        let path = base_dir.join(&entry.path);
        let offsets = read_u64_column(&path.join("offsets.bin"))?;
        let neighbors_path = path.join("neighbors.bin");
        Ok(Self {
            id: entry.name.clone(),
            offsets,
            neighbors_file: CsrDataFile::open(neighbors_path, backend)?,
            prop_files: entry
                .props
                .iter()
                .map(|prop| {
                    Ok((
                        prop.name.clone(),
                        (
                            CsrDataFile::open(path.join(&prop.file), backend)?,
                            prop.value_type.clone(),
                        ),
                    ))
                })
                .collect::<Result<BTreeMap<_, _>>>()?,
            sort_order: match entry.sort_order.as_str() {
                "creation_date_desc" => SortOrder::CreationDateDesc,
                "join_date_desc" => SortOrder::JoinDateDesc,
                "person_id" => SortOrder::PersonId,
                "tag_id" => SortOrder::TagId,
                "unsorted" => SortOrder::Unsorted,
                _ => SortOrder::DstId,
            },
            block_bytes: entry.neighbor_block_bytes.max(4096),
            block_caches: Arc::new(Mutex::new(HashMap::new())),
            prop_block_caches: Arc::new(Mutex::new(HashMap::new())),
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
        let mut pos = start;
        while pos < end {
            let n = choose_chunk_items(end - pos, &ctx.config).min((end - pos) as usize);
            self.read_neighbors_cached(pos, n, ctx)?;
            ctx.read_chunks += 1;
            ctx.read_items += n as u64;
            f(&ctx.scratch_u32)?;
            pos += n as u64;
        }
        Ok(())
    }

    fn read_neighbors_cached(&self, start: u64, len: usize, ctx: &mut ReadContext) -> Result<()> {
        ctx.scratch_u32.clear();
        if len == 0 {
            return Ok(());
        }
        let byte_start = start * 4;
        let byte_end = byte_start + (len as u64 * 4);
        let block_bytes = self.block_bytes as u64;
        let mut block_idx = byte_start / block_bytes;
        while block_idx * block_bytes < byte_end {
            let block_start = block_idx * block_bytes;
            let block = self.read_neighbor_block(block_idx, ctx)?;
            let slice_start = byte_start.saturating_sub(block_start) as usize;
            let slice_end = (byte_end.min(block_start + block.len() as u64) - block_start) as usize;
            if slice_start < slice_end {
                for chunk in block[slice_start..slice_end].chunks_exact(4) {
                    ctx.scratch_u32
                        .push(u32::from_le_bytes(chunk.try_into().unwrap()));
                }
            }
            block_idx += 1;
        }
        Ok(())
    }

    fn read_neighbor_block(&self, block_idx: u64, ctx: &mut ReadContext) -> Result<Arc<Vec<u8>>> {
        {
            let mut caches = self.block_caches.lock();
            if let Some(block) = caches
                .get_mut(&ctx.worker_id)
                .and_then(|cache| cache.get(block_idx))
            {
                ctx.cache_hits += 1;
                return Ok(block);
            }
        }
        ctx.cache_misses += 1;
        let offset = block_idx * self.block_bytes as u64;
        if offset >= self.neighbors_file.len {
            return Ok(Arc::new(Vec::new()));
        }
        let len = ((self.neighbors_file.len - offset) as usize).min(self.block_bytes);
        let block = read_block_at(&self.neighbors_file, offset, len, ctx)?;
        let block = Arc::new(block);
        self.block_caches
            .lock()
            .entry(ctx.worker_id)
            .or_insert_with(|| CsrBlockCache::new(64))
            .insert(block_idx, block.clone());
        Ok(block)
    }

    fn neighbor_range_bytes_cached(
        &self,
        start: u64,
        len: usize,
        ctx: &mut ReadContext,
    ) -> Result<Vec<u8>> {
        self.range_bytes_from_blocks(start * 4, len * 4, ctx, |block_idx, ctx| {
            self.read_neighbor_block(block_idx, ctx)
        })
    }

    fn prop_range_bytes_cached(
        &self,
        prop_name: &str,
        prop_file: &CsrDataFile,
        item_bytes: usize,
        start: u64,
        len: usize,
        ctx: &mut ReadContext,
    ) -> Result<Vec<u8>> {
        let byte_start = start * item_bytes as u64;
        self.range_bytes_from_blocks(byte_start, len * item_bytes, ctx, |block_idx, ctx| {
            self.read_prop_block(prop_name, prop_file, block_idx, ctx)
        })
    }

    fn range_bytes_from_blocks<F>(
        &self,
        byte_start: u64,
        len: usize,
        ctx: &mut ReadContext,
        mut read_block: F,
    ) -> Result<Vec<u8>>
    where
        F: FnMut(u64, &mut ReadContext) -> Result<Arc<Vec<u8>>>,
    {
        let byte_end = byte_start + len as u64;
        let block_bytes = self.block_bytes as u64;
        let mut block_idx = byte_start / block_bytes;
        let mut out = Vec::with_capacity(len);
        while block_idx * block_bytes < byte_end {
            let block_start = block_idx * block_bytes;
            let block = read_block(block_idx, ctx)?;
            let slice_start = byte_start.saturating_sub(block_start) as usize;
            let slice_end = (byte_end.min(block_start + block.len() as u64) - block_start) as usize;
            if slice_start < slice_end {
                out.extend_from_slice(&block[slice_start..slice_end]);
            }
            block_idx += 1;
        }
        Ok(out)
    }

    fn read_prop_block(
        &self,
        prop_name: &str,
        prop_file: &CsrDataFile,
        block_idx: u64,
        ctx: &mut ReadContext,
    ) -> Result<Arc<Vec<u8>>> {
        let cache_key = (ctx.worker_id, prop_name.to_string());
        {
            let mut caches = self.prop_block_caches.lock();
            if let Some(block) = caches
                .get_mut(&cache_key)
                .and_then(|cache| cache.get(block_idx))
            {
                ctx.cache_hits += 1;
                return Ok(block);
            }
        }
        ctx.cache_misses += 1;
        let offset = block_idx * self.block_bytes as u64;
        if offset >= prop_file.len {
            return Ok(Arc::new(Vec::new()));
        }
        let len = ((prop_file.len - offset) as usize).min(self.block_bytes);
        let block = read_block_at(prop_file, offset, len, ctx)?;
        let block = Arc::new(block);
        self.prop_block_caches
            .lock()
            .entry(cache_key)
            .or_insert_with(|| CsrBlockCache::new(64))
            .insert(block_idx, block.clone());
        Ok(block)
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
        let Some((prop_file, value_type)) = self.prop_files.get(prop_name) else {
            return Ok(None);
        };
        if value_type != "i64" {
            return Ok(None);
        }
        let Some((start, end)) = self.range(src) else {
            return Ok(Some(Vec::new()));
        };
        let len = (end - start) as usize;
        let neighbors = self.neighbor_range_bytes_cached(start, len, _ctx)?;
        let props = self.prop_range_bytes_cached(prop_name, prop_file, 8, start, len, _ctx)?;
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
        let Some((prop_file, value_type)) = self.prop_files.get(prop_name) else {
            return Ok(None);
        };
        if value_type != "i32" {
            return Ok(None);
        }
        let Some((start, end)) = self.range(src) else {
            return Ok(Some(Vec::new()));
        };
        let len = (end - start) as usize;
        let neighbors = self.neighbor_range_bytes_cached(start, len, _ctx)?;
        let props = self.prop_range_bytes_cached(prop_name, prop_file, 4, start, len, _ctx)?;
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
    match sort_order {
        SortOrder::Unsorted => {
            edges.reverse();
            edges.sort_by_key(|(src, _, _)| *src);
        }
        _ => edges.sort_unstable_by_key(|(src, dst, _)| (*src, *dst)),
    }
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

fn read_block_at(
    file: &CsrDataFile,
    offset: u64,
    len: usize,
    ctx: &mut ReadContext,
) -> Result<Vec<u8>> {
    match ctx.config.backend {
        ReaderBackendKind::Direct => {
            let out = ctx.direct_scratch.read_direct_at(file, offset, len)?;
            ctx.direct_reads += 1;
            ctx.direct_bytes += out.len() as u64;
            Ok(out)
        }
        ReaderBackendKind::BufferedPread | ReaderBackendKind::MmapDebug => {
            let out = read_buffered_at(file, offset, len)?;
            ctx.blocking_reads += 1;
            ctx.blocking_bytes += out.len() as u64;
            Ok(out)
        }
    }
}

fn read_buffered_at(data_file: &CsrDataFile, offset: u64, len: usize) -> Result<Vec<u8>> {
    let mut file = data_file.file.lock();
    let mut block = vec![0u8; len];
    file.seek(SeekFrom::Start(offset))?;
    file.read_exact(&mut block)?;
    Ok(block)
}

fn open_data_file(path: &Path, backend: ReaderBackendKind) -> Result<File> {
    match backend {
        ReaderBackendKind::Direct => open_direct_file(path),
        ReaderBackendKind::BufferedPread | ReaderBackendKind::MmapDebug => Ok(File::open(path)?),
    }
}

#[cfg(all(feature = "direct-io", unix))]
fn open_direct_file(path: &Path) -> Result<File> {
    Ok(OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECT)
        .open(path)?)
}

#[cfg(not(all(feature = "direct-io", unix)))]
fn open_direct_file(path: &Path) -> Result<File> {
    Ok(File::open(path)?)
}

#[derive(Debug)]
struct DirectReadBuffer {
    #[cfg(all(feature = "direct-io", unix))]
    aligned: AlignedBuf,
}

impl DirectReadBuffer {
    fn new(capacity: usize) -> Self {
        Self {
            #[cfg(all(feature = "direct-io", unix))]
            aligned: AlignedBuf::new(capacity.max(4096), 4096)
                .expect("aligned direct I/O buffer allocation failed"),
        }
    }

    fn read_direct_at(&mut self, file: &CsrDataFile, offset: u64, len: usize) -> Result<Vec<u8>> {
        #[cfg(all(feature = "direct-io", unix))]
        {
            return self.read_direct_at_impl(file, offset, len);
        }
        #[cfg(not(all(feature = "direct-io", unix)))]
        {
            read_buffered_at(file, offset, len)
        }
    }

    #[cfg(all(feature = "direct-io", unix))]
    fn read_direct_at_impl(
        &mut self,
        file: &CsrDataFile,
        offset: u64,
        len: usize,
    ) -> Result<Vec<u8>> {
        const ALIGN: usize = 4096;
        if len == 0 {
            return Ok(Vec::new());
        }
        let aligned_offset = align_down(offset as usize, ALIGN) as u64;
        let delta = (offset - aligned_offset) as usize;
        let aligned_len = align_up(delta + len, ALIGN);
        self.aligned.ensure_len(aligned_len, ALIGN)?;
        let file = file.file.lock();
        let n = unsafe {
            libc::pread(
                file.as_raw_fd(),
                self.aligned.as_mut_ptr().cast(),
                aligned_len,
                aligned_offset as libc::off_t,
            )
        };
        if n < 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        let n = n as usize;
        if n <= delta {
            return Ok(Vec::new());
        }
        let available = (n - delta).min(len);
        Ok(self.aligned.as_slice()[delta..delta + available].to_vec())
    }
}

#[cfg(all(feature = "direct-io", unix))]
#[derive(Debug)]
struct AlignedBuf {
    ptr: *mut u8,
    len: usize,
    capacity: usize,
}

#[cfg(all(feature = "direct-io", unix))]
impl AlignedBuf {
    fn new(capacity: usize, align: usize) -> Result<Self> {
        let mut ptr = std::ptr::null_mut();
        let capacity = align_up(capacity, align);
        let rc = unsafe { libc::posix_memalign(&mut ptr, align, capacity) };
        if rc != 0 {
            return Err(std::io::Error::from_raw_os_error(rc).into());
        }
        unsafe {
            std::ptr::write_bytes(ptr, 0, capacity);
        }
        Ok(Self {
            ptr: ptr.cast(),
            len: capacity,
            capacity,
        })
    }

    fn ensure_len(&mut self, len: usize, align: usize) -> Result<()> {
        if len <= self.capacity {
            self.len = len;
            return Ok(());
        }
        unsafe {
            libc::free(self.ptr.cast());
        }
        let mut ptr = std::ptr::null_mut();
        let capacity = align_up(len, align);
        let rc = unsafe { libc::posix_memalign(&mut ptr, align, capacity) };
        if rc != 0 {
            self.ptr = std::ptr::null_mut();
            self.capacity = 0;
            self.len = 0;
            return Err(std::io::Error::from_raw_os_error(rc).into());
        }
        unsafe {
            std::ptr::write_bytes(ptr, 0, capacity);
        }
        self.ptr = ptr.cast();
        self.capacity = capacity;
        self.len = len;
        Ok(())
    }

    fn as_mut_ptr(&mut self) -> *mut u8 {
        self.ptr
    }

    fn as_slice(&self) -> &[u8] {
        unsafe { std::slice::from_raw_parts(self.ptr, self.len) }
    }
}

#[cfg(all(feature = "direct-io", unix))]
impl Drop for AlignedBuf {
    fn drop(&mut self) {
        unsafe {
            libc::free(self.ptr.cast());
        }
    }
}

#[cfg(all(feature = "direct-io", unix))]
fn align_down(v: usize, align: usize) -> usize {
    v & !(align - 1)
}

#[cfg(all(feature = "direct-io", unix))]
fn align_up(v: usize, align: usize) -> usize {
    (v + align - 1) & !(align - 1)
}
