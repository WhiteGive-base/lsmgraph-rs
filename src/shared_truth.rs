//! Shared typed-neighbor truth and dense/original ID bridge.
//!
//! The external baselines use dense vertex IDs while SemL0 stores the original
//! encoded IDs.  This module loads the converter's bidirectional map, validates
//! its stable mapping hash, consumes the common truth TSV in query order, and
//! computes the same count/sum_hash/xor_hash digest as the C++ baseline drivers.

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};

use crate::{DegreeClass, Engine, GraphAccessSignature};

const MAPPING_HASH_OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
const MAPPING_HASH_PRIME: u64 = 0x0000_0100_0000_01b3;
pub const MAPPING_HASH_ALGORITHM: &str = "fnv1a64-le-dense-original-v1";

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct DenseDigest {
    pub count: u64,
    pub sum_hash: u64,
    pub xor_hash: u64,
}

impl DenseDigest {
    pub fn add(&mut self, dense_dst: u64) {
        let h1 = mix64(dense_dst);
        let h2 = mix64(dense_dst ^ 0xd6e8_feb8_6659_fd93);
        self.count = self.count.wrapping_add(1);
        self.sum_hash = self.sum_hash.wrapping_add(h1);
        self.xor_hash ^= h2;
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TruthRow {
    pub query_index: usize,
    pub edge_type: i32,
    pub src: u64,
    pub count: u64,
    pub sum_hash: u64,
    pub xor_hash: u64,
}

impl TruthRow {
    pub fn digest(&self) -> DenseDigest {
        DenseDigest {
            count: self.count,
            sum_hash: self.sum_hash,
            xor_hash: self.xor_hash,
        }
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
struct IdMapFileManifest {
    path: String,
    sha256: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
struct IdMapManifest {
    format: String,
    format_version: u32,
    vertex_count: usize,
    mapping_hash_algorithm: String,
    mapping_hash: String,
    original_to_dense: IdMapFileManifest,
    dense_to_original: IdMapFileManifest,
}

#[derive(Debug, Clone)]
pub struct SharedIdMap {
    dense_to_original: Vec<u64>,
    original_to_dense: HashMap<u64, u64>,
    mapping_hash: String,
    dense_to_original_sha256: Option<String>,
    original_to_dense_sha256: Option<String>,
}

#[derive(Debug, Deserialize)]
struct DenseToOriginalRow {
    dense_id: u64,
    original_id: u64,
}

#[derive(Debug, Deserialize)]
struct OriginalToDenseRow {
    original_id: u64,
    dense_id: u64,
}

impl SharedIdMap {
    pub fn load(id_map_dir: impl AsRef<Path>) -> Result<Self> {
        let id_map_dir = id_map_dir.as_ref();
        let manifest_path = id_map_dir.join("id-map-manifest.json");
        let manifest: IdMapManifest = serde_json::from_slice(
            &fs::read(&manifest_path)
                .with_context(|| format!("read {}", manifest_path.display()))?,
        )
        .with_context(|| format!("parse {}", manifest_path.display()))?;
        if manifest.format != "seml0-shared-id-map" || manifest.format_version != 1 {
            bail!(
                "unsupported ID-map manifest format {:?} version {}",
                manifest.format,
                manifest.format_version
            );
        }
        if manifest.mapping_hash_algorithm != MAPPING_HASH_ALGORITHM {
            bail!(
                "unsupported mapping hash algorithm {:?}",
                manifest.mapping_hash_algorithm
            );
        }

        let dense_path = safe_manifest_path(id_map_dir, &manifest.dense_to_original.path)?;
        let original_path = safe_manifest_path(id_map_dir, &manifest.original_to_dense.path)?;
        let mut dense_reader = csv::ReaderBuilder::new()
            .delimiter(b'\t')
            .from_path(&dense_path)
            .with_context(|| format!("open {}", dense_path.display()))?;
        let mut dense_to_original = Vec::with_capacity(manifest.vertex_count);
        for row in dense_reader.deserialize::<DenseToOriginalRow>() {
            let row = row.with_context(|| format!("parse {}", dense_path.display()))?;
            if row.dense_id != dense_to_original.len() as u64 {
                bail!(
                    "{}: dense IDs must be contiguous; expected {}, found {}",
                    dense_path.display(),
                    dense_to_original.len(),
                    row.dense_id
                );
            }
            dense_to_original.push(row.original_id);
        }
        if dense_to_original.len() != manifest.vertex_count {
            bail!(
                "ID-map vertex_count mismatch: manifest={} dense_rows={}",
                manifest.vertex_count,
                dense_to_original.len()
            );
        }

        let mut original_reader = csv::ReaderBuilder::new()
            .delimiter(b'\t')
            .from_path(&original_path)
            .with_context(|| format!("open {}", original_path.display()))?;
        let mut original_to_dense = HashMap::with_capacity(manifest.vertex_count);
        for row in original_reader.deserialize::<OriginalToDenseRow>() {
            let row = row.with_context(|| format!("parse {}", original_path.display()))?;
            if original_to_dense
                .insert(row.original_id, row.dense_id)
                .is_some()
            {
                bail!(
                    "{}: duplicate original ID {}",
                    original_path.display(),
                    row.original_id
                );
            }
        }
        validate_bijection(&dense_to_original, &original_to_dense)?;
        let computed_hash = mapping_hash(&dense_to_original);
        if computed_hash != manifest.mapping_hash {
            bail!(
                "mapping hash mismatch: manifest={} computed={}",
                manifest.mapping_hash,
                computed_hash
            );
        }

        Ok(Self {
            dense_to_original,
            original_to_dense,
            mapping_hash: computed_hash,
            dense_to_original_sha256: Some(manifest.dense_to_original.sha256),
            original_to_dense_sha256: Some(manifest.original_to_dense.sha256),
        })
    }

    pub fn from_dense_to_original(dense_to_original: Vec<u64>) -> Result<Self> {
        let mut original_to_dense = HashMap::with_capacity(dense_to_original.len());
        for (dense_id, original_id) in dense_to_original.iter().copied().enumerate() {
            if original_to_dense
                .insert(original_id, dense_id as u64)
                .is_some()
            {
                bail!("duplicate original ID {original_id}");
            }
        }
        validate_bijection(&dense_to_original, &original_to_dense)?;
        Ok(Self {
            mapping_hash: mapping_hash(&dense_to_original),
            dense_to_original,
            original_to_dense,
            dense_to_original_sha256: None,
            original_to_dense_sha256: None,
        })
    }

    pub fn original_for_dense(&self, dense_id: u64) -> Option<u64> {
        self.dense_to_original.get(dense_id as usize).copied()
    }

    pub fn dense_for_original(&self, original_id: u64) -> Option<u64> {
        self.original_to_dense.get(&original_id).copied()
    }

    pub fn vertex_count(&self) -> usize {
        self.dense_to_original.len()
    }

    pub fn mapping_hash(&self) -> &str {
        &self.mapping_hash
    }

    pub fn dense_to_original_sha256(&self) -> Option<&str> {
        self.dense_to_original_sha256.as_deref()
    }

    pub fn original_to_dense_sha256(&self) -> Option<&str> {
        self.original_to_dense_sha256.as_deref()
    }
}

fn safe_manifest_path(base: &Path, relative: &str) -> Result<PathBuf> {
    let path = Path::new(relative);
    if path.is_absolute() || path.components().count() != 1 {
        bail!("ID-map manifest path must be a basename, got {relative:?}");
    }
    Ok(base.join(path))
}

fn validate_bijection(
    dense_to_original: &[u64],
    original_to_dense: &HashMap<u64, u64>,
) -> Result<()> {
    if dense_to_original.len() != original_to_dense.len() {
        bail!(
            "ID-map directions have different sizes: dense={} original={}",
            dense_to_original.len(),
            original_to_dense.len()
        );
    }
    for (dense_id, original_id) in dense_to_original.iter().copied().enumerate() {
        if original_to_dense.get(&original_id) != Some(&(dense_id as u64)) {
            bail!(
                "ID-map directions disagree at dense={} original={}",
                dense_id,
                original_id
            );
        }
    }
    Ok(())
}

pub fn mapping_hash(dense_to_original: &[u64]) -> String {
    let mut value = MAPPING_HASH_OFFSET;
    for (dense_id, original_id) in dense_to_original.iter().copied().enumerate() {
        for byte in (dense_id as u64)
            .to_le_bytes()
            .into_iter()
            .chain(original_id.to_le_bytes())
        {
            value ^= byte as u64;
            value = value.wrapping_mul(MAPPING_HASH_PRIME);
        }
    }
    format!("{value:016x}")
}

pub fn read_truth_tsv(path: impl AsRef<Path>) -> Result<Vec<TruthRow>> {
    let path = path.as_ref();
    let mut reader = csv::ReaderBuilder::new()
        .delimiter(b'\t')
        .from_path(path)
        .with_context(|| format!("open truth TSV {}", path.display()))?;
    let expected = [
        "query_index",
        "edge_type",
        "src",
        "count",
        "sum_hash",
        "xor_hash",
    ];
    let headers = reader
        .headers()
        .with_context(|| format!("read truth header {}", path.display()))?;
    if headers.iter().collect::<Vec<_>>() != expected {
        bail!(
            "{}: expected truth header {:?}, found {:?}",
            path.display(),
            expected,
            headers
        );
    }
    let mut rows = Vec::new();
    for row in reader.deserialize::<TruthRow>() {
        let row = row.with_context(|| format!("parse truth TSV {}", path.display()))?;
        if row.query_index != rows.len() {
            bail!(
                "{}: query_index must be contiguous; expected {}, found {}",
                path.display(),
                rows.len(),
                row.query_index
            );
        }
        rows.push(row);
    }
    if rows.is_empty() {
        bail!("{}: truth TSV has no queries", path.display());
    }
    Ok(rows)
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SharedTruthSamplePlan {
    pub version: u32,
    pub source: String,
    pub samples_per_edge_type: usize,
    pub semantic_degree_hint: bool,
    pub force_signature: bool,
    pub src_label: Option<i32>,
    pub dst_label: Option<i32>,
    pub entries: Vec<SharedTruthSampleEntry>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SharedTruthSampleEntry {
    pub edge_type: Option<i32>,
    pub src_label: Option<i32>,
    pub dst_label: Option<i32>,
    pub candidate_edges_for_sampling: usize,
    pub candidate_sources_for_sampling: usize,
    pub samples: Vec<SharedTruthSample>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SharedTruthSample {
    pub src: u64,
    pub degree: u64,
}

pub fn build_storage_sample_plan(
    rows: &[TruthRow],
    ids: &SharedIdMap,
    semantic_degree_hint: bool,
    force_signature: bool,
) -> Result<SharedTruthSamplePlan> {
    let mut entries: Vec<SharedTruthSampleEntry> = Vec::new();
    for row in rows {
        let original_src = ids.original_for_dense(row.src).with_context(|| {
            format!(
                "truth query {} references unknown dense src {}",
                row.query_index, row.src
            )
        })?;
        if entries
            .last()
            .map(|entry| entry.edge_type != Some(row.edge_type))
            .unwrap_or(true)
        {
            entries.push(SharedTruthSampleEntry {
                edge_type: Some(row.edge_type),
                src_label: None,
                dst_label: None,
                candidate_edges_for_sampling: 0,
                candidate_sources_for_sampling: 0,
                samples: Vec::new(),
            });
        }
        let entry = entries.last_mut().expect("entry was just created");
        entry.candidate_edges_for_sampling = entry
            .candidate_edges_for_sampling
            .saturating_add(row.count as usize);
        entry.candidate_sources_for_sampling += 1;
        entry.samples.push(SharedTruthSample {
            src: original_src,
            degree: row.count,
        });
    }
    let samples_per_edge_type = entries
        .iter()
        .map(|entry| entry.samples.len())
        .max()
        .unwrap_or(0);
    Ok(SharedTruthSamplePlan {
        version: 1,
        source: "shared-truth-tsv".to_string(),
        samples_per_edge_type,
        semantic_degree_hint,
        force_signature,
        src_label: None,
        dst_label: None,
        entries,
    })
}

#[derive(Debug, Clone, Serialize)]
pub struct TruthMismatch {
    pub query_index: usize,
    pub edge_type: i32,
    pub dense_src: u64,
    pub original_src: Option<u64>,
    pub expected: DenseDigest,
    pub observed: Option<DenseDigest>,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct SharedTruthVerification {
    pub status: String,
    pub checked: usize,
    pub mismatches: usize,
    pub total_neighbors: u64,
    pub mapping_hash_algorithm: &'static str,
    pub mapping_hash: String,
    pub id_map_vertex_count: usize,
    pub sample_mismatches: Vec<TruthMismatch>,
}

pub async fn verify_engine_truth(
    engine: &Engine,
    rows: &[TruthRow],
    ids: &SharedIdMap,
    semantic_degree_hint: bool,
    force_signature: bool,
    max_mismatch_details: usize,
) -> Result<SharedTruthVerification> {
    let snapshot = engine.current_snapshot();
    let mut mismatches = 0usize;
    let mut total_neighbors = 0u64;
    let mut sample_mismatches = Vec::new();
    for row in rows {
        let expected = row.digest();
        let Some(original_src) = ids.original_for_dense(row.src) else {
            mismatches += 1;
            if sample_mismatches.len() < max_mismatch_details {
                sample_mismatches.push(TruthMismatch {
                    query_index: row.query_index,
                    edge_type: row.edge_type,
                    dense_src: row.src,
                    original_src: None,
                    expected,
                    observed: None,
                    error: Some("dense source is absent from ID map".to_string()),
                });
            }
            continue;
        };
        let edges = if semantic_degree_hint || force_signature {
            let mut signature =
                GraphAccessSignature::neighbor_scan(original_src, Some(row.edge_type));
            if semantic_degree_hint {
                signature = signature.with_degree_class(DegreeClass::from_max_degree(row.count));
            }
            engine
                .get_neighbors_by_signature(signature, snapshot)
                .await?
        } else {
            engine
                .get_neighbors_typed(original_src, row.edge_type, snapshot)
                .await?
        };
        let mut observed = DenseDigest::default();
        let mut missing_dst = None;
        for edge in edges {
            observed.count = observed.count.wrapping_add(1);
            if let Some(dense_dst) = ids.dense_for_original(edge.dst) {
                let h1 = mix64(dense_dst);
                let h2 = mix64(dense_dst ^ 0xd6e8_feb8_6659_fd93);
                observed.sum_hash = observed.sum_hash.wrapping_add(h1);
                observed.xor_hash ^= h2;
            } else if missing_dst.is_none() {
                missing_dst = Some(edge.dst);
            }
        }
        total_neighbors = total_neighbors.wrapping_add(observed.count);
        if observed != expected || missing_dst.is_some() {
            mismatches += 1;
            if sample_mismatches.len() < max_mismatch_details {
                sample_mismatches.push(TruthMismatch {
                    query_index: row.query_index,
                    edge_type: row.edge_type,
                    dense_src: row.src,
                    original_src: Some(original_src),
                    expected,
                    observed: Some(observed),
                    error: missing_dst
                        .map(|dst| format!("original destination {dst} is absent from ID map")),
                });
            }
        }
    }
    Ok(SharedTruthVerification {
        status: if mismatches == 0 { "PASS" } else { "FAIL" }.to_string(),
        checked: rows.len(),
        mismatches,
        total_neighbors,
        mapping_hash_algorithm: MAPPING_HASH_ALGORITHM,
        mapping_hash: ids.mapping_hash().to_string(),
        id_map_vertex_count: ids.vertex_count(),
        sample_mismatches,
    })
}

fn mix64(mut value: u64) -> u64 {
    value = value.wrapping_add(0x9e37_79b9_7f4a_7c15);
    value = (value ^ (value >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    value = (value ^ (value >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    value ^ (value >> 31)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::LsmGraphConfig;

    #[test]
    fn dense_digest_matches_cpp_reference() {
        let mut digest = DenseDigest::default();
        digest.add(1);
        digest.add(2);
        assert_eq!(digest.count, 2);
        assert_eq!(digest.sum_hash, 2_909_998_031_247_618_959);
        assert_eq!(digest.xor_hash, 6_475_611_011_048_258_314);
    }

    #[test]
    fn mapping_hash_and_plan_preserve_truth_order() -> Result<()> {
        let ids = SharedIdMap::from_dense_to_original(vec![100, 400, 900])?;
        assert_eq!(ids.mapping_hash(), "e859143c2d2eeedc");
        let rows = vec![
            TruthRow {
                query_index: 0,
                edge_type: -7,
                src: 1,
                count: 2,
                sum_hash: 10,
                xor_hash: 11,
            },
            TruthRow {
                query_index: 1,
                edge_type: 7,
                src: 0,
                count: 1,
                sum_hash: 12,
                xor_hash: 13,
            },
        ];
        let plan = build_storage_sample_plan(&rows, &ids, true, false)?;
        assert_eq!(plan.entries.len(), 2);
        assert_eq!(plan.entries[0].edge_type, Some(-7));
        assert_eq!(plan.entries[0].samples[0].src, 400);
        assert_eq!(plan.entries[1].edge_type, Some(7));
        assert_eq!(plan.entries[1].samples[0].src, 100);
        Ok(())
    }

    #[test]
    fn truth_parser_requires_contiguous_indices() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let path = temp.path().join("truth.tsv");
        fs::write(
            &path,
            "query_index\tedge_type\tsrc\tcount\tsum_hash\txor_hash\n0\t7\t0\t2\t3\t4\n",
        )?;
        let rows = read_truth_tsv(&path)?;
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].edge_type, 7);
        Ok(())
    }

    #[test]
    fn id_map_loader_validates_both_directions_and_mapping_hash() -> Result<()> {
        let temp = tempfile::tempdir()?;
        fs::write(
            temp.path().join("dense-to-original.tsv"),
            "dense_id\toriginal_id\n0\t100\n1\t400\n2\t900\n",
        )?;
        fs::write(
            temp.path().join("original-to-dense.tsv"),
            "original_id\tdense_id\n100\t0\n400\t1\n900\t2\n",
        )?;
        fs::write(
            temp.path().join("id-map-manifest.json"),
            serde_json::to_vec_pretty(&serde_json::json!({
                "format": "seml0-shared-id-map",
                "format_version": 1,
                "vertex_count": 3,
                "mapping_hash_algorithm": MAPPING_HASH_ALGORITHM,
                "mapping_hash": "e859143c2d2eeedc",
                "original_to_dense": {
                    "path": "original-to-dense.tsv",
                    "sha256": "fixture-original"
                },
                "dense_to_original": {
                    "path": "dense-to-original.tsv",
                    "sha256": "fixture-dense"
                }
            }))?,
        )?;
        let ids = SharedIdMap::load(temp.path())?;
        assert_eq!(ids.original_for_dense(2), Some(900));
        assert_eq!(ids.dense_for_original(400), Some(1));
        assert_eq!(ids.mapping_hash(), "e859143c2d2eeedc");
        Ok(())
    }

    #[tokio::test]
    async fn seml0_consumer_validates_dense_truth() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let engine = Engine::create(LsmGraphConfig::new(temp.path())).await?;
        engine.insert_edge(100, 400, 7).await?;
        engine.insert_edge(100, 900, 7).await?;
        let ids = SharedIdMap::from_dense_to_original(vec![100, 400, 900])?;
        let mut expected = DenseDigest::default();
        expected.add(1);
        expected.add(2);
        let rows = vec![TruthRow {
            query_index: 0,
            edge_type: 7,
            src: 0,
            count: expected.count,
            sum_hash: expected.sum_hash,
            xor_hash: expected.xor_hash,
        }];
        let report = verify_engine_truth(&engine, &rows, &ids, true, false, 5).await?;
        assert_eq!(report.status, "PASS");
        assert_eq!(report.checked, 1);
        assert_eq!(report.mismatches, 0);
        Ok(())
    }
}
