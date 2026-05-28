use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::Arc;

use crate::error::Result;
use crate::graph::Engine;
use crate::snb::encode_vid;
use crate::types::{EdgeLabel, VertexId, VertexLabel};

#[derive(Debug, Clone, Copy)]
pub struct ImportStats {
    pub input_rows: u64,
    pub directed_edges: u64,
}

impl ImportStats {
    pub(crate) fn add(&mut self, other: ImportStats) {
        self.input_rows += other.input_rows;
        self.directed_edges += other.directed_edges;
    }
}

#[derive(Debug, Clone, Copy)]
pub struct ValidateStats {
    pub checked_vertices: usize,
    pub expected_directed_edges: usize,
    pub actual_directed_edges: usize,
}

pub async fn import_person_knows(engine: Arc<Engine>, csv_root: &Path) -> Result<ImportStats> {
    let path = csv_root.join("dynamic/person_knows_person_0_0.csv");
    let stats = import_index_edge_file(
        engine.clone(),
        &path,
        VertexLabel::Person,
        VertexLabel::Person,
        EdgeLabel::Knows.as_i32(),
        true,
        0,
        1,
    )
    .await?;
    engine.flush_active().await?;
    Ok(stats)
}

pub async fn import_snb_topology(engine: Arc<Engine>, csv_root: &Path) -> Result<ImportStats> {
    let mut stats = ImportStats {
        input_rows: 0,
        directed_edges: 0,
    };

    let dynamic = csv_root.join("dynamic");
    let static_dir = csv_root.join("static");

    stats.add(
        import_index_edge_file(
            engine.clone(),
            &dynamic.join("person_knows_person_0_0.csv"),
            VertexLabel::Person,
            VertexLabel::Person,
            EdgeLabel::Knows.as_i32(),
            true,
            0,
            1,
        )
        .await?,
    );
    stats.add(
        import_index_edge_file(
            engine.clone(),
            &dynamic.join("person_likes_comment_0_0.csv"),
            VertexLabel::Person,
            VertexLabel::Comment,
            EdgeLabel::LikesComment.as_i32(),
            false,
            0,
            1,
        )
        .await?,
    );
    stats.add(
        import_index_edge_file(
            engine.clone(),
            &dynamic.join("person_likes_post_0_0.csv"),
            VertexLabel::Person,
            VertexLabel::Post,
            EdgeLabel::LikesPost.as_i32(),
            false,
            0,
            1,
        )
        .await?,
    );
    stats.add(
        import_index_edge_file(
            engine.clone(),
            &dynamic.join("person_hasInterest_tag_0_0.csv"),
            VertexLabel::Person,
            VertexLabel::Tag,
            EdgeLabel::HasInterest.as_i32(),
            false,
            0,
            1,
        )
        .await?,
    );
    stats.add(
        import_index_edge_file(
            engine.clone(),
            &dynamic.join("person_studyAt_organisation_0_0.csv"),
            VertexLabel::Person,
            VertexLabel::Organisation,
            EdgeLabel::StudyAt.as_i32(),
            false,
            0,
            1,
        )
        .await?,
    );
    stats.add(
        import_index_edge_file(
            engine.clone(),
            &dynamic.join("person_workAt_organisation_0_0.csv"),
            VertexLabel::Person,
            VertexLabel::Organisation,
            EdgeLabel::WorkAt.as_i32(),
            false,
            0,
            1,
        )
        .await?,
    );
    stats.add(
        import_index_edge_file(
            engine.clone(),
            &dynamic.join("forum_hasMember_person_0_0.csv"),
            VertexLabel::Forum,
            VertexLabel::Person,
            EdgeLabel::HasMember.as_i32(),
            false,
            0,
            1,
        )
        .await?,
    );
    stats.add(
        import_index_edge_file(
            engine.clone(),
            &dynamic.join("forum_hasTag_tag_0_0.csv"),
            VertexLabel::Forum,
            VertexLabel::Tag,
            EdgeLabel::HasTag.as_i32(),
            false,
            0,
            1,
        )
        .await?,
    );
    stats.add(
        import_index_edge_file(
            engine.clone(),
            &dynamic.join("comment_hasTag_tag_0_0.csv"),
            VertexLabel::Comment,
            VertexLabel::Tag,
            EdgeLabel::HasTag.as_i32(),
            false,
            0,
            1,
        )
        .await?,
    );
    stats.add(
        import_index_edge_file(
            engine.clone(),
            &dynamic.join("post_hasTag_tag_0_0.csv"),
            VertexLabel::Post,
            VertexLabel::Tag,
            EdgeLabel::HasTag.as_i32(),
            false,
            0,
            1,
        )
        .await?,
    );

    stats.add(
        import_named_edge_file(
            engine.clone(),
            &dynamic.join("person_0_0.csv"),
            VertexLabel::Person,
            VertexLabel::Place,
            "id",
            "place",
            EdgeLabel::IsLocatedIn.as_i32(),
        )
        .await?,
    );
    stats.add(
        import_named_edge_file(
            engine.clone(),
            &dynamic.join("forum_0_0.csv"),
            VertexLabel::Forum,
            VertexLabel::Person,
            "id",
            "moderator",
            EdgeLabel::HasModerator.as_i32(),
        )
        .await?,
    );
    stats.add(
        import_named_edge_file(
            engine.clone(),
            &dynamic.join("post_0_0.csv"),
            VertexLabel::Post,
            VertexLabel::Person,
            "id",
            "creator",
            EdgeLabel::HasCreator.as_i32(),
        )
        .await?,
    );
    stats.add(
        import_reversed_named_edge_file(
            engine.clone(),
            &dynamic.join("post_0_0.csv"),
            VertexLabel::Forum,
            VertexLabel::Post,
            "Forum.id",
            "id",
            EdgeLabel::ContainerOf.as_i32(),
        )
        .await?,
    );
    stats.add(
        import_named_edge_file(
            engine.clone(),
            &dynamic.join("post_0_0.csv"),
            VertexLabel::Post,
            VertexLabel::Place,
            "id",
            "place",
            EdgeLabel::IsLocatedIn.as_i32(),
        )
        .await?,
    );
    stats.add(
        import_named_edge_file(
            engine.clone(),
            &dynamic.join("comment_0_0.csv"),
            VertexLabel::Comment,
            VertexLabel::Person,
            "id",
            "creator",
            EdgeLabel::HasCreator.as_i32(),
        )
        .await?,
    );
    stats.add(
        import_named_edge_file(
            engine.clone(),
            &dynamic.join("comment_0_0.csv"),
            VertexLabel::Comment,
            VertexLabel::Place,
            "id",
            "place",
            EdgeLabel::IsLocatedIn.as_i32(),
        )
        .await?,
    );
    stats.add(
        import_named_edge_file(
            engine.clone(),
            &dynamic.join("comment_0_0.csv"),
            VertexLabel::Comment,
            VertexLabel::Post,
            "id",
            "replyOfPost",
            EdgeLabel::ReplyOfPost.as_i32(),
        )
        .await?,
    );
    stats.add(
        import_named_edge_file(
            engine.clone(),
            &dynamic.join("comment_0_0.csv"),
            VertexLabel::Comment,
            VertexLabel::Comment,
            "id",
            "replyOfComment",
            EdgeLabel::ReplyOfComment.as_i32(),
        )
        .await?,
    );
    stats.add(
        import_named_edge_file(
            engine.clone(),
            &static_dir.join("organisation_0_0.csv"),
            VertexLabel::Organisation,
            VertexLabel::Place,
            "id",
            "place",
            EdgeLabel::IsLocatedIn.as_i32(),
        )
        .await?,
    );
    stats.add(
        import_named_edge_file(
            engine.clone(),
            &static_dir.join("place_0_0.csv"),
            VertexLabel::Place,
            VertexLabel::Place,
            "id",
            "isPartOf",
            EdgeLabel::IsPartOf.as_i32(),
        )
        .await?,
    );
    stats.add(
        import_named_edge_file(
            engine.clone(),
            &static_dir.join("tag_0_0.csv"),
            VertexLabel::Tag,
            VertexLabel::TagClass,
            "id",
            "hasType",
            EdgeLabel::HasType.as_i32(),
        )
        .await?,
    );
    stats.add(
        import_named_edge_file(
            engine.clone(),
            &static_dir.join("tagclass_0_0.csv"),
            VertexLabel::TagClass,
            VertexLabel::TagClass,
            "id",
            "isSubclassOf",
            EdgeLabel::IsSubclassOf.as_i32(),
        )
        .await?,
    );

    engine.flush_active().await?;
    Ok(stats)
}

async fn import_index_edge_file(
    engine: Arc<Engine>,
    path: &Path,
    src_label: VertexLabel,
    dst_label: VertexLabel,
    edge_type: i32,
    bidirectional: bool,
    src_idx: usize,
    dst_idx: usize,
) -> Result<ImportStats> {
    let mut rdr = csv::ReaderBuilder::new()
        .delimiter(b'|')
        .has_headers(true)
        .from_path(path)?;
    let mut input_rows = 0u64;
    let mut directed_edges = 0u64;
    for rec in rdr.records() {
        let rec = rec?;
        let src: VertexId = encode_vid(src_label, rec[src_idx].parse()?);
        let dst: VertexId = encode_vid(dst_label, rec[dst_idx].parse()?);
        engine.insert_edge(src, dst, edge_type).await?;
        if bidirectional {
            engine.insert_edge(dst, src, edge_type).await?;
            directed_edges += 2;
        } else {
            directed_edges += 1;
        }
        input_rows += 1;
    }
    Ok(ImportStats {
        input_rows,
        directed_edges,
    })
}

async fn import_named_edge_file(
    engine: Arc<Engine>,
    path: &Path,
    src_label: VertexLabel,
    dst_label: VertexLabel,
    src_col: &str,
    dst_col: &str,
    edge_type: i32,
) -> Result<ImportStats> {
    import_named_edge_file_inner(
        engine, path, src_label, dst_label, src_col, dst_col, edge_type, false,
    )
    .await
}

async fn import_reversed_named_edge_file(
    engine: Arc<Engine>,
    path: &Path,
    src_label: VertexLabel,
    dst_label: VertexLabel,
    src_col: &str,
    dst_col: &str,
    edge_type: i32,
) -> Result<ImportStats> {
    import_named_edge_file_inner(
        engine, path, src_label, dst_label, src_col, dst_col, edge_type, false,
    )
    .await
}

async fn import_named_edge_file_inner(
    engine: Arc<Engine>,
    path: &Path,
    src_label: VertexLabel,
    dst_label: VertexLabel,
    src_col: &str,
    dst_col: &str,
    edge_type: i32,
    bidirectional: bool,
) -> Result<ImportStats> {
    let mut rdr = csv::ReaderBuilder::new()
        .delimiter(b'|')
        .has_headers(true)
        .from_path(path)?;
    let headers = rdr.headers()?.clone();
    let src_idx = headers
        .iter()
        .position(|h| h == src_col)
        .ok_or_else(|| anyhow::anyhow!("missing column {src_col} in {}", path.display()))?;
    let dst_idx = headers
        .iter()
        .position(|h| h == dst_col)
        .ok_or_else(|| anyhow::anyhow!("missing column {dst_col} in {}", path.display()))?;

    let mut input_rows = 0u64;
    let mut directed_edges = 0u64;
    for rec in rdr.records() {
        let rec = rec?;
        input_rows += 1;
        let src_raw = rec.get(src_idx).unwrap_or("").trim();
        let dst_raw = rec.get(dst_idx).unwrap_or("").trim();
        if src_raw.is_empty() || dst_raw.is_empty() {
            continue;
        }
        let src: VertexId = encode_vid(src_label, src_raw.parse()?);
        let dst: VertexId = encode_vid(dst_label, dst_raw.parse()?);
        engine.insert_edge(src, dst, edge_type).await?;
        directed_edges += 1;
        if bidirectional {
            engine.insert_edge(dst, src, edge_type).await?;
            directed_edges += 1;
        }
    }

    Ok(ImportStats {
        input_rows,
        directed_edges,
    })
}

pub async fn validate_person_knows(
    engine: Arc<Engine>,
    csv_root: &Path,
    max_vertices: usize,
) -> Result<ValidateStats> {
    let expected = load_person_knows_adjacency(csv_root)?;
    let snapshot = engine.current_snapshot();
    let mut checked = 0usize;
    let mut expected_edges = 0usize;
    let mut actual_edges = 0usize;

    for (src, expected_neighbors) in expected.iter() {
        if checked >= max_vertices {
            break;
        }
        let actual: HashSet<VertexId> = engine
            .get_neighbors(*src, snapshot)
            .await?
            .into_iter()
            .filter(|e| e.edge_type == EdgeLabel::Knows.as_i32())
            .map(|e| e.dst)
            .collect();
        if actual != *expected_neighbors {
            let missing: Vec<_> = expected_neighbors
                .difference(&actual)
                .take(10)
                .copied()
                .collect();
            let extra: Vec<_> = actual
                .difference(expected_neighbors)
                .take(10)
                .copied()
                .collect();
            anyhow::bail!(
                "person_knows mismatch for src={src}: expected={}, actual={}, missing_sample={missing:?}, extra_sample={extra:?}",
                expected_neighbors.len(),
                actual.len()
            );
        }
        checked += 1;
        expected_edges += expected_neighbors.len();
        actual_edges += actual.len();
    }

    Ok(ValidateStats {
        checked_vertices: checked,
        expected_directed_edges: expected_edges,
        actual_directed_edges: actual_edges,
    })
}

fn load_person_knows_adjacency(csv_root: &Path) -> Result<HashMap<VertexId, HashSet<VertexId>>> {
    let path = csv_root.join("dynamic/person_knows_person_0_0.csv");
    let mut rdr = csv::ReaderBuilder::new()
        .delimiter(b'|')
        .has_headers(true)
        .from_path(&path)?;
    let mut map: HashMap<VertexId, HashSet<VertexId>> = HashMap::new();
    for rec in rdr.records() {
        let rec = rec?;
        let a: VertexId = encode_vid(VertexLabel::Person, rec[0].parse()?);
        let b: VertexId = encode_vid(VertexLabel::Person, rec[1].parse()?);
        map.entry(a).or_default().insert(b);
        map.entry(b).or_default().insert(a);
    }
    Ok(map)
}
