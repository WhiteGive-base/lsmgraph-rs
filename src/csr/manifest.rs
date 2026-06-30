use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::csr::format::CsrSegmentMeta;
use crate::error::Result;
use crate::types::{FileId, Timestamp};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "op")]
pub enum ManifestRecord {
    CreateFile { meta: CsrSegmentMeta },
    DeleteFile { file_id: FileId },
}

#[derive(Debug, Clone)]
pub struct Manifest {
    path: PathBuf,
}

#[derive(Debug, Clone)]
pub struct ManifestState {
    pub live_files: Vec<CsrSegmentMeta>,
    pub next_file_id: FileId,
    pub max_ts: Timestamp,
}

impl Manifest {
    pub fn new(store_dir: &Path) -> Self {
        Self {
            path: store_dir.join("MANIFEST"),
        }
    }

    pub fn load(&self) -> Result<ManifestState> {
        if !self.path.exists() {
            return Ok(ManifestState {
                live_files: Vec::new(),
                next_file_id: 1,
                max_ts: 0,
            });
        }

        let file = OpenOptions::new().read(true).open(&self.path)?;
        let reader = BufReader::new(file);
        let mut live = BTreeMap::new();
        let mut max_file_id = 0;
        let mut max_ts = 0;
        for line in reader.lines() {
            let line = line?;
            if line.trim().is_empty() {
                continue;
            }
            let record: ManifestRecord = serde_json::from_str(&line)?;
            match record {
                ManifestRecord::CreateFile { meta } => {
                    max_file_id = max_file_id.max(meta.file_id);
                    max_ts = max_ts.max(meta.max_ts);
                    live.insert(meta.file_id, meta);
                }
                ManifestRecord::DeleteFile { file_id } => {
                    live.remove(&file_id);
                }
            }
        }
        Ok(ManifestState {
            live_files: live.into_values().collect(),
            next_file_id: max_file_id + 1,
            max_ts,
        })
    }

    pub fn append(&self, record: &ManifestRecord) -> Result<()> {
        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)?;
        serde_json::to_writer(&mut file, record)?;
        file.write_all(b"\n")?;
        file.sync_all()?;
        Ok(())
    }
}
