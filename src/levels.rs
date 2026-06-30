use crate::csr::CsrSegmentMeta;
use crate::types::LevelId;

pub const L0: LevelId = 0;
pub const L1: LevelId = 1;

pub fn split_levels(files: Vec<CsrSegmentMeta>, max_levels: usize) -> Vec<Vec<CsrSegmentMeta>> {
    let mut levels = vec![Vec::new(); max_levels.max(2)];
    for file in files {
        let idx = file.level as usize;
        if idx >= levels.len() {
            levels.resize(idx + 1, Vec::new());
        }
        levels[idx].push(file);
    }
    for level in &mut levels {
        level.sort_by_key(|m| (m.min_src, m.file_id));
    }
    levels
}
