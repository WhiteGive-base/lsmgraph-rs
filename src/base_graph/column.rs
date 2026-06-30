use std::collections::{HashMap, VecDeque};
use std::fs::File;
use std::io::{BufReader, BufWriter, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use parking_lot::Mutex;

use crate::error::Result;

pub fn write_u32_column(path: &Path, values: &[u32]) -> Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut writer = BufWriter::new(File::create(path)?);
    for value in values {
        writer.write_all(&value.to_le_bytes())?;
    }
    writer.flush()?;
    Ok(())
}

pub fn write_u8_column(path: &Path, values: &[u8]) -> Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut writer = BufWriter::new(File::create(path)?);
    writer.write_all(values)?;
    writer.flush()?;
    Ok(())
}

pub fn write_i32_column(path: &Path, values: &[i32]) -> Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut writer = BufWriter::new(File::create(path)?);
    for value in values {
        writer.write_all(&value.to_le_bytes())?;
    }
    writer.flush()?;
    Ok(())
}

pub fn write_i64_column(path: &Path, values: &[i64]) -> Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut writer = BufWriter::new(File::create(path)?);
    for value in values {
        writer.write_all(&value.to_le_bytes())?;
    }
    writer.flush()?;
    Ok(())
}

pub fn write_string_column(path_prefix: &Path, values: &[String]) -> Result<()> {
    if let Some(parent) = path_prefix.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let offsets_path = path_prefix.with_extension("offsets");
    let blob_path = path_prefix.with_extension("blob");
    let mut offsets = Vec::with_capacity(values.len() + 1);
    let mut blob = Vec::new();
    offsets.push(0u64);
    for value in values {
        blob.extend_from_slice(value.as_bytes());
        offsets.push(blob.len() as u64);
    }
    let mut offset_writer = BufWriter::new(File::create(offsets_path)?);
    for offset in offsets {
        offset_writer.write_all(&offset.to_le_bytes())?;
    }
    offset_writer.flush()?;
    let mut blob_writer = BufWriter::new(File::create(blob_path)?);
    blob_writer.write_all(&blob)?;
    blob_writer.flush()?;
    Ok(())
}

pub fn write_ext_id_map(path: &Path, external_to_local: &[(i64, u32)]) -> Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut writer = BufWriter::new(File::create(path)?);
    for (external, local) in external_to_local {
        writer.write_all(&external.to_le_bytes())?;
        writer.write_all(&local.to_le_bytes())?;
    }
    writer.flush()?;
    Ok(())
}

pub fn read_u32_column(path: &Path) -> Result<Vec<u32>> {
    let mut reader = BufReader::new(File::open(path)?);
    let mut bytes = Vec::new();
    reader.read_to_end(&mut bytes)?;
    if bytes.len() % 4 != 0 {
        anyhow::bail!(
            "u32 column {} has non-u32 length {}",
            path.display(),
            bytes.len()
        );
    }
    Ok(bytes
        .chunks_exact(4)
        .map(|chunk| u32::from_le_bytes(chunk.try_into().unwrap()))
        .collect())
}

pub fn read_i32_column(path: &Path) -> Result<Vec<i32>> {
    let mut reader = BufReader::new(File::open(path)?);
    let mut bytes = Vec::new();
    reader.read_to_end(&mut bytes)?;
    if bytes.len() % 4 != 0 {
        anyhow::bail!(
            "i32 column {} has non-i32 length {}",
            path.display(),
            bytes.len()
        );
    }
    Ok(bytes
        .chunks_exact(4)
        .map(|chunk| i32::from_le_bytes(chunk.try_into().unwrap()))
        .collect())
}

pub fn read_i64_column(path: &Path) -> Result<Vec<i64>> {
    let mut reader = BufReader::new(File::open(path)?);
    let mut bytes = Vec::new();
    reader.read_to_end(&mut bytes)?;
    if bytes.len() % 8 != 0 {
        anyhow::bail!(
            "i64 column {} has non-i64 length {}",
            path.display(),
            bytes.len()
        );
    }
    Ok(bytes
        .chunks_exact(8)
        .map(|chunk| i64::from_le_bytes(chunk.try_into().unwrap()))
        .collect())
}

pub fn read_u64_column(path: &Path) -> Result<Vec<u64>> {
    let mut reader = BufReader::new(File::open(path)?);
    let mut bytes = Vec::new();
    reader.read_to_end(&mut bytes)?;
    if bytes.len() % 8 != 0 {
        anyhow::bail!(
            "u64 column {} has non-u64 length {}",
            path.display(),
            bytes.len()
        );
    }
    Ok(bytes
        .chunks_exact(8)
        .map(|chunk| u64::from_le_bytes(chunk.try_into().unwrap()))
        .collect())
}

pub fn read_string_column(path_prefix: &Path) -> Result<Vec<String>> {
    let offsets = read_u64_column(&path_prefix.with_extension("offsets"))?;
    let mut blob = Vec::new();
    BufReader::new(File::open(path_prefix.with_extension("blob"))?).read_to_end(&mut blob)?;
    let mut out = Vec::with_capacity(offsets.len().saturating_sub(1));
    for window in offsets.windows(2) {
        let start = window[0] as usize;
        let end = window[1] as usize;
        if start > end || end > blob.len() {
            anyhow::bail!(
                "string column {} has invalid offset range {start}..{end} for blob length {}",
                path_prefix.display(),
                blob.len()
            );
        }
        out.push(String::from_utf8(blob[start..end].to_vec())?);
    }
    Ok(out)
}

#[derive(Debug, Clone)]
pub struct StringColumnReader {
    offsets: Vec<u64>,
    blob_path: PathBuf,
    file: Arc<Mutex<File>>,
    cache: Arc<Mutex<StringColumnCache>>,
}

#[derive(Debug)]
struct StringColumnCache {
    max_items: usize,
    values: HashMap<usize, String>,
    order: VecDeque<usize>,
}

impl StringColumnCache {
    fn new(max_items: usize) -> Self {
        Self {
            max_items,
            values: HashMap::new(),
            order: VecDeque::new(),
        }
    }

    fn get(&self, idx: usize) -> Option<String> {
        self.values.get(&idx).cloned()
    }

    fn insert(&mut self, idx: usize, value: String) {
        if self.values.contains_key(&idx) {
            self.values.insert(idx, value);
            return;
        }
        self.values.insert(idx, value);
        self.order.push_back(idx);
        while self.values.len() > self.max_items {
            if let Some(evict) = self.order.pop_front() {
                self.values.remove(&evict);
            } else {
                break;
            }
        }
    }
}

impl StringColumnReader {
    pub fn open(path_prefix: &Path) -> Result<Self> {
        let blob_path = path_prefix.with_extension("blob");
        Ok(Self {
            offsets: read_u64_column(&path_prefix.with_extension("offsets"))?,
            file: Arc::new(Mutex::new(File::open(&blob_path)?)),
            blob_path,
            cache: Arc::new(Mutex::new(StringColumnCache::new(8192))),
        })
    }

    pub fn len(&self) -> usize {
        self.offsets.len().saturating_sub(1)
    }

    pub fn get(&self, idx: usize) -> Result<String> {
        if let Some(value) = self.cache.lock().get(idx) {
            return Ok(value);
        }
        if idx + 1 >= self.offsets.len() {
            anyhow::bail!(
                "string column {} index {} out of bounds len {}",
                self.blob_path.display(),
                idx,
                self.len()
            );
        }
        let start = self.offsets[idx];
        let end = self.offsets[idx + 1];
        if end < start {
            anyhow::bail!(
                "string column {} has invalid offset range {}..{}",
                self.blob_path.display(),
                start,
                end
            );
        }
        let len = (end - start) as usize;
        let mut file = self.file.lock();
        file.seek(SeekFrom::Start(start))?;
        let mut bytes = vec![0u8; len];
        file.read_exact(&mut bytes)?;
        let value = String::from_utf8(bytes)?;
        self.cache.lock().insert(idx, value.clone());
        Ok(value)
    }
}

#[derive(Debug, Clone)]
pub struct I64ColumnReader {
    path: PathBuf,
    len: usize,
    file: Arc<Mutex<File>>,
    cache: Arc<Mutex<I64ColumnCache>>,
}

#[derive(Debug)]
struct I64ColumnCache {
    block_items: usize,
    max_blocks: usize,
    blocks: HashMap<usize, Arc<Vec<i64>>>,
    order: VecDeque<usize>,
}

impl I64ColumnCache {
    fn new(block_items: usize, max_blocks: usize) -> Self {
        Self {
            block_items,
            max_blocks,
            blocks: HashMap::new(),
            order: VecDeque::new(),
        }
    }

    fn get(&self, block_idx: usize) -> Option<Arc<Vec<i64>>> {
        self.blocks.get(&block_idx).cloned()
    }

    fn insert(&mut self, block_idx: usize, block: Arc<Vec<i64>>) {
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

impl I64ColumnReader {
    pub fn open(path: &Path) -> Result<Self> {
        let bytes = std::fs::metadata(path)?.len();
        if bytes % 8 != 0 {
            anyhow::bail!("i64 column {} has non-i64 length {}", path.display(), bytes);
        }
        Ok(Self {
            path: path.to_path_buf(),
            len: (bytes / 8) as usize,
            file: Arc::new(Mutex::new(File::open(path)?)),
            cache: Arc::new(Mutex::new(I64ColumnCache::new(4096, 1024))),
        })
    }

    pub fn len(&self) -> usize {
        self.len
    }

    pub fn get(&self, idx: usize) -> Result<i64> {
        if idx >= self.len {
            anyhow::bail!(
                "i64 column {} index {} out of bounds len {}",
                self.path.display(),
                idx,
                self.len
            );
        }
        let block_items = self.cache.lock().block_items;
        let block_idx = idx / block_items;
        let item_idx = idx % block_items;
        if let Some(block) = self.cache.lock().get(block_idx) {
            return Ok(block[item_idx]);
        }
        let block_start = block_idx * block_items;
        let items = (self.len - block_start).min(block_items);
        let mut bytes = vec![0u8; items * 8];
        {
            let mut file = self.file.lock();
            file.seek(SeekFrom::Start((block_start * 8) as u64))?;
            file.read_exact(&mut bytes)?;
        }
        let values: Vec<i64> = bytes
            .chunks_exact(8)
            .map(|chunk| i64::from_le_bytes(chunk.try_into().unwrap()))
            .collect();
        let values = Arc::new(values);
        let value = values[item_idx];
        self.cache.lock().insert(block_idx, values);
        Ok(value)
    }
}

pub fn read_ext_id_map(path: &Path) -> Result<Vec<(i64, u32)>> {
    let mut reader = BufReader::new(File::open(path)?);
    let mut bytes = Vec::new();
    reader.read_to_end(&mut bytes)?;
    if bytes.len() % 12 != 0 {
        anyhow::bail!(
            "external id map {} has invalid length {}",
            path.display(),
            bytes.len()
        );
    }
    Ok(bytes
        .chunks_exact(12)
        .map(|chunk| {
            let external = i64::from_le_bytes(chunk[0..8].try_into().unwrap());
            let local = u32::from_le_bytes(chunk[8..12].try_into().unwrap());
            (external, local)
        })
        .collect())
}
