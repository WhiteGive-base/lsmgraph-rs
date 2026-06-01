use std::fs::File;
use std::io::{BufReader, BufWriter, Read, Write};
use std::path::Path;

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
