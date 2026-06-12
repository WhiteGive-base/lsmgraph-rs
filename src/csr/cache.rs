use std::collections::{HashMap, VecDeque};
use std::sync::Arc;

use parking_lot::Mutex;

use crate::csr::format::{CsrHeader, EdgeOffset};
use crate::types::{FileId, VertexId};

#[derive(Debug)]
pub struct CachedCsrMetadata {
    pub header: CsrHeader,
    pub offsets: Arc<[EdgeOffset]>,
    pub src_filter: SourceBloom,
}

impl CachedCsrMetadata {
    pub fn new(header: CsrHeader, offsets: Vec<EdgeOffset>) -> Self {
        let src_filter = SourceBloom::from_sources(offsets.iter().map(|offset| offset.src));
        let offsets = Arc::from(offsets.into_boxed_slice());
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
    map: HashMap<FileId, CacheEntry>,
    // LRU order with lazy invalidation: every touch pushes (file_id, gen) and
    // bumps the entry's gen; queue records whose gen no longer matches the map
    // are stale and skipped at eviction time. This keeps get/insert O(1) without
    // the unbounded queue growth and hot-entry eviction the previous
    // push-without-dedup implementation suffered from.
    queue: VecDeque<(FileId, u64)>,
    next_gen: u64,
}

#[derive(Debug)]
struct CacheEntry {
    metadata: Arc<CachedCsrMetadata>,
    gen: u64,
}

impl CacheState {
    fn touch(&mut self, file_id: FileId) -> u64 {
        self.next_gen += 1;
        self.queue.push_back((file_id, self.next_gen));
        self.next_gen
    }

    fn compact_queue(&mut self, max_entries: usize) {
        if self.queue.len() <= (max_entries * 8).max(64) {
            return;
        }
        let map = &self.map;
        self.queue
            .retain(|(file_id, gen)| map.get(file_id).is_some_and(|entry| entry.gen == *gen));
    }
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
        let metadata = state.map.get(&file_id)?.metadata.clone();
        let gen = state.touch(file_id);
        if let Some(entry) = state.map.get_mut(&file_id) {
            entry.gen = gen;
        }
        state.compact_queue(self.max_entries);
        Some(metadata)
    }

    pub fn insert(&self, file_id: FileId, metadata: CachedCsrMetadata) -> Arc<CachedCsrMetadata> {
        let metadata = Arc::new(metadata);
        let mut state = self.state.lock();
        let gen = state.touch(file_id);
        state.map.insert(
            file_id,
            CacheEntry {
                metadata: metadata.clone(),
                gen,
            },
        );
        while state.map.len() > self.max_entries {
            let Some((victim, victim_gen)) = state.queue.pop_front() else {
                break;
            };
            if state
                .map
                .get(&victim)
                .is_some_and(|entry| entry.gen == victim_gen)
            {
                state.map.remove(&victim);
            }
        }
        state.compact_queue(self.max_entries);
        metadata
    }

    pub fn cached_may_contain_src(&self, file_id: FileId, src: VertexId) -> Option<bool> {
        self.get(file_id)
            .map(|metadata| metadata.may_contain_src(src))
    }

    #[cfg(test)]
    fn debug_sizes(&self) -> (usize, usize) {
        let state = self.state.lock();
        (state.map.len(), state.queue.len())
    }
}

#[derive(Debug)]
pub struct SourceBloom {
    bits: Vec<u64>,
    bit_mask: u64,
}

impl SourceBloom {
    pub fn from_sources(sources: impl Iterator<Item = VertexId>) -> Self {
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

    pub fn from_words(bit_count: u64, bytes: &[u8]) -> anyhow::Result<Self> {
        if bit_count == 0 || !bit_count.is_power_of_two() {
            anyhow::bail!("invalid SourceBloom bit_count={bit_count}");
        }
        let expected_words = ((bit_count + 63) / 64) as usize;
        let expected_bytes = expected_words
            .checked_mul(8)
            .ok_or_else(|| anyhow::anyhow!("SourceBloom byte length overflow"))?;
        if bytes.len() != expected_bytes {
            anyhow::bail!(
                "SourceBloom byte length mismatch: expected {}, got {}",
                expected_bytes,
                bytes.len()
            );
        }
        let mut bits = Vec::with_capacity(expected_words);
        for chunk in bytes.chunks_exact(8) {
            bits.push(u64::from_le_bytes(chunk.try_into().unwrap()));
        }
        Ok(Self {
            bits,
            bit_mask: bit_count - 1,
        })
    }

    pub fn bit_count(&self) -> u64 {
        self.bit_mask + 1
    }

    pub fn encode_words(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(self.bits.len() * 8);
        for word in &self.bits {
            out.extend_from_slice(&word.to_le_bytes());
        }
        out
    }

    pub fn may_contain(&self, src: VertexId) -> bool {
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

#[cfg(test)]
mod tests {
    use super::*;

    fn metadata(file_id: FileId) -> CachedCsrMetadata {
        let header = CsrHeader {
            magic: 0,
            version: 1,
            header_len: 0,
            level: 0,
            flags: 0,
            file_id,
            create_ts: 0,
            min_ts: 0,
            max_ts: 0,
            src_label: 0,
            edge_type_partition: 0,
            min_src: 0,
            max_src: 0,
            edge_offset_count: 0,
            edge_body_count: 0,
            offsets_offset: 0,
            offsets_len: 0,
            bodies_offset: 0,
            bodies_len: 0,
            checksum: 0,
        };
        CachedCsrMetadata::new(header, Vec::new())
    }

    #[test]
    fn enforces_capacity_and_evicts_oldest() {
        let cache = CsrMetadataCache::new(2);
        cache.insert(1, metadata(1));
        cache.insert(2, metadata(2));
        cache.insert(3, metadata(3));

        let (map_len, _) = cache.debug_sizes();
        assert_eq!(map_len, 2);
        assert!(cache.get(1).is_none(), "oldest entry must be evicted");
        assert!(cache.get(2).is_some());
        assert!(cache.get(3).is_some());
    }

    #[test]
    fn recently_used_entry_survives_eviction() {
        let cache = CsrMetadataCache::new(2);
        cache.insert(1, metadata(1));
        cache.insert(2, metadata(2));
        assert!(cache.get(1).is_some(), "touch 1 so 2 becomes LRU");
        cache.insert(3, metadata(3));

        assert!(
            cache.get(1).is_some(),
            "hot entry must not be evicted by stale queue records"
        );
        assert!(cache.get(2).is_none());
        assert!(cache.get(3).is_some());
    }

    #[test]
    fn reinserting_same_file_does_not_evict_others() {
        let cache = CsrMetadataCache::new(2);
        cache.insert(1, metadata(1));
        cache.insert(2, metadata(2));
        cache.insert(2, metadata(2));
        cache.insert(2, metadata(2));

        assert!(cache.get(1).is_some());
        assert!(cache.get(2).is_some());
    }

    #[test]
    fn queue_stays_bounded_under_repeated_gets() {
        let cache = CsrMetadataCache::new(4);
        for id in 0..4u64 {
            cache.insert(id, metadata(id));
        }
        for _ in 0..10_000 {
            for id in 0..4u64 {
                assert!(cache.get(id).is_some());
            }
        }
        let (map_len, queue_len) = cache.debug_sizes();
        assert_eq!(map_len, 4);
        assert!(
            queue_len <= 64 + 4,
            "queue must be compacted, got {queue_len}"
        );
    }
}
