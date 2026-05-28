use std::collections::{HashMap, VecDeque};
use std::sync::Arc;

use parking_lot::Mutex;

use crate::csr::format::{CsrHeader, EdgeOffset};
use crate::types::{FileId, VertexId};

#[derive(Debug)]
pub struct CachedCsrMetadata {
    pub header: CsrHeader,
    pub offsets: Vec<EdgeOffset>,
    pub src_filter: SourceBloom,
}

impl CachedCsrMetadata {
    pub fn new(header: CsrHeader, offsets: Vec<EdgeOffset>) -> Self {
        let src_filter = SourceBloom::from_sources(offsets.iter().map(|offset| offset.src));
        Self {
            header,
            offsets,
            src_filter,
        }
    }

    pub fn may_contain_src(&self, src: VertexId) -> bool {
        self.src_filter.may_contain(src)
    }
}

#[derive(Debug)]
pub struct CsrMetadataCache {
    max_entries: usize,
    state: Mutex<CacheState>,
}

#[derive(Debug, Default)]
struct CacheState {
    map: HashMap<FileId, Arc<CachedCsrMetadata>>,
    lru: VecDeque<FileId>,
}

impl CsrMetadataCache {
    pub fn new(max_entries: usize) -> Self {
        Self {
            max_entries: max_entries.max(1),
            state: Mutex::new(CacheState::default()),
        }
    }

    pub fn get(&self, file_id: FileId) -> Option<Arc<CachedCsrMetadata>> {
        let mut state = self.state.lock();
        let value = state.map.get(&file_id).cloned();
        if value.is_some() {
            state.lru.push_back(file_id);
        }
        value
    }

    pub fn insert(&self, file_id: FileId, metadata: CachedCsrMetadata) -> Arc<CachedCsrMetadata> {
        let metadata = Arc::new(metadata);
        let mut state = self.state.lock();
        if !state.map.contains_key(&file_id) {
            while state.map.len() >= self.max_entries {
                let Some(victim) = state.lru.pop_front() else {
                    break;
                };
                if victim != file_id {
                    state.map.remove(&victim);
                }
            }
        }
        state.lru.push_back(file_id);
        state.map.insert(file_id, metadata.clone());
        metadata
    }

    pub fn cached_may_contain_src(&self, file_id: FileId, src: VertexId) -> Option<bool> {
        self.get(file_id)
            .map(|metadata| metadata.may_contain_src(src))
    }
}

#[derive(Debug)]
pub struct SourceBloom {
    bits: Vec<u64>,
    bit_mask: u64,
}

impl SourceBloom {
    fn from_sources(sources: impl Iterator<Item = VertexId>) -> Self {
        let sources: Vec<_> = sources.collect();
        let bit_count = (sources.len().max(4) * 16).next_power_of_two();
        let word_count = (bit_count + 63) / 64;
        let mut out = Self {
            bits: vec![0; word_count],
            bit_mask: bit_count as u64 - 1,
        };
        for src in sources {
            out.insert(src);
        }
        out
    }

    fn insert(&mut self, src: VertexId) {
        for bit in self.bits_for(src) {
            let word = (bit / 64) as usize;
            let offset = bit % 64;
            self.bits[word] |= 1u64 << offset;
        }
    }

    fn may_contain(&self, src: VertexId) -> bool {
        if self.bits.is_empty() {
            return false;
        }
        self.bits_for(src).into_iter().all(|bit| {
            let word = (bit / 64) as usize;
            let offset = bit % 64;
            (self.bits[word] & (1u64 << offset)) != 0
        })
    }

    fn bits_for(&self, src: VertexId) -> [u64; 3] {
        let h1 = mix64(src);
        let h2 = mix64(src ^ 0x9e37_79b9_7f4a_7c15);
        [
            h1 & self.bit_mask,
            h1.wrapping_add(h2) & self.bit_mask,
            h1.wrapping_add(h2.wrapping_mul(2)) & self.bit_mask,
        ]
    }
}

fn mix64(mut value: u64) -> u64 {
    value ^= value >> 30;
    value = value.wrapping_mul(0xbf58_476d_1ce4_e5b9);
    value ^= value >> 27;
    value = value.wrapping_mul(0x94d0_49bb_1331_11eb);
    value ^ (value >> 31)
}
