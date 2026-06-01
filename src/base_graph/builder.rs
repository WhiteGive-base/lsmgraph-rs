use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};

use csv::StringRecord;
use serde::Serialize;

use crate::base_graph::catalog::{
    BaseGraphCatalog, CsrCatalogEntry, DerivedColumnEntry, SingleColumnEntry, VertexCatalogEntry,
};
use crate::base_graph::column::{write_ext_id_map, write_u32_column, write_u8_column};
use crate::base_graph::csr::{write_csr, SortOrder};
use crate::base_graph::ids::LabelId;
use crate::error::Result;

#[derive(Debug, Clone)]
pub struct BuildConfig {
    pub vertices_per_bucket: u64,
    pub max_bucket_bytes: usize,
    pub max_parallel_buckets: usize,
    pub temp_dir: PathBuf,
    pub temp_disk_budget_gb: usize,
    pub sort_memory_limit_mb: usize,
}

impl BuildConfig {
    pub fn for_output(output_dir: &Path) -> Self {
        Self {
            vertices_per_bucket: 10_000_000,
            max_bucket_bytes: 512 * 1024 * 1024,
            max_parallel_buckets: 1,
            temp_dir: output_dir.join("build-tmp"),
            temp_disk_budget_gb: 512,
            sort_memory_limit_mb: 1024,
        }
    }
}

#[derive(Debug, Default, Clone, Serialize)]
pub struct BaseGraphBuildStats {
    pub source: String,
    pub output_dir: String,
    pub vertex_counts: BTreeMap<String, u64>,
    pub csr_edges: BTreeMap<String, u64>,
    pub single_columns: BTreeMap<String, u64>,
    pub derived_columns: BTreeMap<String, u64>,
}

#[derive(Debug, Clone)]
struct IdMap {
    external_to_local: HashMap<i64, u32>,
    ordered_pairs: Vec<(i64, u32)>,
}

impl IdMap {
    fn get(&self, external: i64) -> Result<u32> {
        self.external_to_local
            .get(&external)
            .copied()
            .ok_or_else(|| anyhow::anyhow!("missing external id {external} in id map"))
    }

    fn len(&self) -> usize {
        self.ordered_pairs.len()
    }
}

#[derive(Default)]
struct IdMaps {
    person: IdMap,
    post: IdMap,
    comment: IdMap,
    forum: IdMap,
    place: IdMap,
    organisation: IdMap,
    tag: IdMap,
    tag_class: IdMap,
}

impl Default for IdMap {
    fn default() -> Self {
        Self {
            external_to_local: HashMap::new(),
            ordered_pairs: Vec::new(),
        }
    }
}

pub fn build_from_snb(
    csv_root: &Path,
    output_dir: &Path,
    config: &BuildConfig,
) -> Result<BaseGraphBuildStats> {
    std::fs::create_dir_all(output_dir)?;
    std::fs::create_dir_all(&config.temp_dir)?;

    let mut stats = BaseGraphBuildStats {
        source: csv_root.display().to_string(),
        output_dir: output_dir.display().to_string(),
        ..BaseGraphBuildStats::default()
    };
    let mut catalog = BaseGraphCatalog::new(csv_root.display().to_string());
    let maps = build_id_maps(csv_root, output_dir, &mut catalog, &mut stats)?;
    build_single_edges(csv_root, output_dir, &maps, &mut catalog, &mut stats)?;
    build_multi_edges(csv_root, output_dir, &maps, &mut catalog, &mut stats)?;
    build_derived_indexes(csv_root, output_dir, &maps, &mut catalog, &mut stats)?;
    catalog.save(&output_dir.join("catalog.json"))?;
    Ok(stats)
}

fn build_id_maps(
    csv_root: &Path,
    output_dir: &Path,
    catalog: &mut BaseGraphCatalog,
    stats: &mut BaseGraphBuildStats,
) -> Result<IdMaps> {
    let dynamic = csv_root.join("dynamic");
    let static_dir = csv_root.join("static");
    let mut maps = IdMaps::default();
    maps.person = load_id_map(&dynamic.join("person_0_0.csv"), "id")?;
    maps.post = load_id_map(&dynamic.join("post_0_0.csv"), "id")?;
    maps.comment = load_id_map(&dynamic.join("comment_0_0.csv"), "id")?;
    maps.forum = load_id_map(&dynamic.join("forum_0_0.csv"), "id")?;
    maps.place = load_id_map(&static_dir.join("place_0_0.csv"), "id")?;
    maps.organisation = load_id_map(&static_dir.join("organisation_0_0.csv"), "id")?;
    maps.tag = load_id_map(&static_dir.join("tag_0_0.csv"), "id")?;
    maps.tag_class = load_id_map(&static_dir.join("tagclass_0_0.csv"), "id")?;

    for (label, map) in [
        (LabelId::Person, &maps.person),
        (LabelId::Post, &maps.post),
        (LabelId::Comment, &maps.comment),
        (LabelId::Forum, &maps.forum),
        (LabelId::Place, &maps.place),
        (LabelId::Organisation, &maps.organisation),
        (LabelId::Tag, &maps.tag),
        (LabelId::TagClass, &maps.tag_class),
    ] {
        let rel = format!("vertices/{}/ext_id_to_local.idx", label.as_str());
        write_ext_id_map(&output_dir.join(&rel), &map.ordered_pairs)?;
        catalog.vertex_labels.insert(
            label.as_str().to_string(),
            VertexCatalogEntry {
                label: label.as_str().to_string(),
                count: map.len() as u64,
                ext_id_to_local: rel,
            },
        );
        stats
            .vertex_counts
            .insert(label.as_str().to_string(), map.len() as u64);
    }
    Ok(maps)
}

fn build_single_edges(
    csv_root: &Path,
    output_dir: &Path,
    maps: &IdMaps,
    catalog: &mut BaseGraphCatalog,
    stats: &mut BaseGraphBuildStats,
) -> Result<()> {
    let dynamic = csv_root.join("dynamic");
    let static_dir = csv_root.join("static");

    let mut person_place = vec![u32::MAX; maps.person.len()];
    scan_csv(&dynamic.join("person_0_0.csv"), |header, record| {
        let person = maps.person.get(parse_i64(record, idx(header, "id")?)?)?;
        let place = maps.place.get(parse_i64(record, idx(header, "place")?)?)?;
        person_place[person as usize] = place;
        Ok(())
    })?;
    write_single_u32(
        output_dir,
        catalog,
        stats,
        "person_place",
        "single_edges/LOCATED_IN/person_place.col",
        "Person",
        "Place",
        &person_place,
    )?;

    let mut post_creator = vec![u32::MAX; maps.post.len()];
    let mut post_forum = vec![u32::MAX; maps.post.len()];
    let mut post_place = vec![u32::MAX; maps.post.len()];
    scan_csv(&dynamic.join("post_0_0.csv"), |header, record| {
        let post = maps.post.get(parse_i64(record, idx(header, "id")?)?)?;
        post_creator[post as usize] = maps
            .person
            .get(parse_i64(record, idx(header, "creator")?)?)?;
        post_forum[post as usize] = maps
            .forum
            .get(parse_i64(record, idx(header, "Forum.id")?)?)?;
        post_place[post as usize] = maps.place.get(parse_i64(record, idx(header, "place")?)?)?;
        Ok(())
    })?;
    write_single_u32(
        output_dir,
        catalog,
        stats,
        "post_creator",
        "single_edges/HAS_CREATOR/post_creator.col",
        "Post",
        "Person",
        &post_creator,
    )?;
    write_single_u32(
        output_dir,
        catalog,
        stats,
        "post_forum",
        "single_edges/CONTAINER_OF/post_forum.col",
        "Post",
        "Forum",
        &post_forum,
    )?;
    write_single_u32(
        output_dir,
        catalog,
        stats,
        "post_place",
        "single_edges/LOCATED_IN/post_place.col",
        "Post",
        "Place",
        &post_place,
    )?;

    let mut comment_creator = vec![u32::MAX; maps.comment.len()];
    let mut comment_place = vec![u32::MAX; maps.comment.len()];
    let mut parent_kind = vec![u8::MAX; maps.comment.len()];
    let mut parent_id = vec![u32::MAX; maps.comment.len()];
    scan_csv(&dynamic.join("comment_0_0.csv"), |header, record| {
        let comment = maps.comment.get(parse_i64(record, idx(header, "id")?)?)?;
        comment_creator[comment as usize] = maps
            .person
            .get(parse_i64(record, idx(header, "creator")?)?)?;
        comment_place[comment as usize] =
            maps.place.get(parse_i64(record, idx(header, "place")?)?)?;
        let reply_post = record.get(idx(header, "replyOfPost")?).unwrap_or_default();
        let reply_comment = record
            .get(idx(header, "replyOfComment")?)
            .unwrap_or_default();
        if !reply_post.is_empty() {
            parent_kind[comment as usize] = 0;
            parent_id[comment as usize] = maps.post.get(reply_post.parse::<i64>()?)?;
        } else if !reply_comment.is_empty() {
            parent_kind[comment as usize] = 1;
            parent_id[comment as usize] = maps.comment.get(reply_comment.parse::<i64>()?)?;
        }
        Ok(())
    })?;
    write_single_u32(
        output_dir,
        catalog,
        stats,
        "comment_creator",
        "single_edges/HAS_CREATOR/comment_creator.col",
        "Comment",
        "Person",
        &comment_creator,
    )?;
    write_single_u32(
        output_dir,
        catalog,
        stats,
        "comment_place",
        "single_edges/LOCATED_IN/comment_place.col",
        "Comment",
        "Place",
        &comment_place,
    )?;
    write_u8_column(
        &output_dir.join("single_edges/REPLY_OF/comment_parent_kind.col"),
        &parent_kind,
    )?;
    write_single_u32(
        output_dir,
        catalog,
        stats,
        "comment_parent_id",
        "single_edges/REPLY_OF/comment_parent_id.col",
        "Comment",
        "Message",
        &parent_id,
    )?;

    let mut forum_moderator = vec![u32::MAX; maps.forum.len()];
    scan_csv(&dynamic.join("forum_0_0.csv"), |header, record| {
        let forum = maps.forum.get(parse_i64(record, idx(header, "id")?)?)?;
        forum_moderator[forum as usize] = maps
            .person
            .get(parse_i64(record, idx(header, "moderator")?)?)?;
        Ok(())
    })?;
    write_single_u32(
        output_dir,
        catalog,
        stats,
        "forum_moderator",
        "single_edges/HAS_MODERATOR/forum_moderator.col",
        "Forum",
        "Person",
        &forum_moderator,
    )?;

    let mut organisation_place = vec![u32::MAX; maps.organisation.len()];
    scan_csv(
        &static_dir.join("organisation_0_0.csv"),
        |header, record| {
            let org = maps
                .organisation
                .get(parse_i64(record, idx(header, "id")?)?)?;
            organisation_place[org as usize] =
                maps.place.get(parse_i64(record, idx(header, "place")?)?)?;
            Ok(())
        },
    )?;
    write_single_u32(
        output_dir,
        catalog,
        stats,
        "organisation_place",
        "single_edges/LOCATED_IN/organisation_place.col",
        "Organisation",
        "Place",
        &organisation_place,
    )?;

    let mut tag_tagclass = vec![u32::MAX; maps.tag.len()];
    scan_csv(&static_dir.join("tag_0_0.csv"), |header, record| {
        let tag = maps.tag.get(parse_i64(record, idx(header, "id")?)?)?;
        tag_tagclass[tag as usize] = maps
            .tag_class
            .get(parse_i64(record, idx(header, "hasType")?)?)?;
        Ok(())
    })?;
    write_single_u32(
        output_dir,
        catalog,
        stats,
        "tag_tagclass",
        "single_edges/HAS_TYPE/tag_tagclass.col",
        "Tag",
        "TagClass",
        &tag_tagclass,
    )?;
    Ok(())
}

fn build_multi_edges(
    csv_root: &Path,
    output_dir: &Path,
    maps: &IdMaps,
    catalog: &mut BaseGraphCatalog,
    stats: &mut BaseGraphBuildStats,
) -> Result<()> {
    let dynamic = csv_root.join("dynamic");
    build_bidirectional_csr(
        &dynamic.join("person_knows_person_0_0.csv"),
        output_dir,
        catalog,
        stats,
        "KNOWS",
        "Person",
        "Person",
        maps.person.len(),
        maps.person.len(),
        &maps.person,
        &maps.person,
        "Person.id",
        "Person.id",
    )?;
    build_bidirectional_csr(
        &dynamic.join("person_likes_post_0_0.csv"),
        output_dir,
        catalog,
        stats,
        "PERSON_LIKES_POST",
        "Person",
        "Post",
        maps.person.len(),
        maps.post.len(),
        &maps.person,
        &maps.post,
        "Person.id",
        "Post.id",
    )?;
    build_bidirectional_csr(
        &dynamic.join("person_likes_comment_0_0.csv"),
        output_dir,
        catalog,
        stats,
        "PERSON_LIKES_COMMENT",
        "Person",
        "Comment",
        maps.person.len(),
        maps.comment.len(),
        &maps.person,
        &maps.comment,
        "Person.id",
        "Comment.id",
    )?;
    build_bidirectional_csr(
        &dynamic.join("forum_hasMember_person_0_0.csv"),
        output_dir,
        catalog,
        stats,
        "FORUM_HAS_MEMBER",
        "Forum",
        "Person",
        maps.forum.len(),
        maps.person.len(),
        &maps.forum,
        &maps.person,
        "Forum.id",
        "Person.id",
    )?;
    Ok(())
}

fn build_derived_indexes(
    csv_root: &Path,
    output_dir: &Path,
    maps: &IdMaps,
    catalog: &mut BaseGraphCatalog,
    stats: &mut BaseGraphBuildStats,
) -> Result<()> {
    let dynamic = csv_root.join("dynamic");
    let mut person_posts = Vec::new();
    let mut forum_posts = Vec::new();
    let mut post_root = vec![u32::MAX; maps.post.len()];
    scan_csv(&dynamic.join("post_0_0.csv"), |header, record| {
        let post = maps.post.get(parse_i64(record, idx(header, "id")?)?)?;
        let creator = maps
            .person
            .get(parse_i64(record, idx(header, "creator")?)?)?;
        let forum = maps
            .forum
            .get(parse_i64(record, idx(header, "Forum.id")?)?)?;
        person_posts.push((creator, post));
        forum_posts.push((forum, post));
        post_root[post as usize] = post;
        Ok(())
    })?;
    write_derived_csr(
        output_dir,
        catalog,
        stats,
        "PERSON_CREATED_POST",
        "derived_indexes/PERSON_CREATED_POST",
        "Person",
        "Post",
        maps.person.len(),
        person_posts,
    )?;
    write_derived_csr(
        output_dir,
        catalog,
        stats,
        "FORUM_CONTAINER_OF_POST",
        "derived_indexes/FORUM_CONTAINER_OF_POST",
        "Forum",
        "Post",
        maps.forum.len(),
        forum_posts,
    )?;
    write_u32_column(
        &output_dir.join("derived_indexes/MESSAGE_ROOT_POST/post_root_post.col"),
        &post_root,
    )?;
    catalog.derived_columns.insert(
        "post_root_post".to_string(),
        DerivedColumnEntry {
            name: "post_root_post".to_string(),
            file: "derived_indexes/MESSAGE_ROOT_POST/post_root_post.col".to_string(),
            rows: post_root.len() as u64,
            value_type: "u32".to_string(),
        },
    );
    stats
        .derived_columns
        .insert("post_root_post".to_string(), post_root.len() as u64);

    let mut person_comments = Vec::new();
    let mut post_children = Vec::new();
    let mut comment_children = Vec::new();
    let mut parent_kind = vec![u8::MAX; maps.comment.len()];
    let mut parent_id = vec![u32::MAX; maps.comment.len()];
    scan_csv(&dynamic.join("comment_0_0.csv"), |header, record| {
        let comment = maps.comment.get(parse_i64(record, idx(header, "id")?)?)?;
        let creator = maps
            .person
            .get(parse_i64(record, idx(header, "creator")?)?)?;
        person_comments.push((creator, comment));
        let reply_post = record.get(idx(header, "replyOfPost")?).unwrap_or_default();
        let reply_comment = record
            .get(idx(header, "replyOfComment")?)
            .unwrap_or_default();
        if !reply_post.is_empty() {
            let post = maps.post.get(reply_post.parse::<i64>()?)?;
            parent_kind[comment as usize] = 0;
            parent_id[comment as usize] = post;
            post_children.push((post, comment));
        } else if !reply_comment.is_empty() {
            let parent = maps.comment.get(reply_comment.parse::<i64>()?)?;
            parent_kind[comment as usize] = 1;
            parent_id[comment as usize] = parent;
            comment_children.push((parent, comment));
        }
        Ok(())
    })?;
    write_derived_csr(
        output_dir,
        catalog,
        stats,
        "PERSON_CREATED_COMMENT",
        "derived_indexes/PERSON_CREATED_COMMENT",
        "Person",
        "Comment",
        maps.person.len(),
        person_comments,
    )?;
    write_derived_csr(
        output_dir,
        catalog,
        stats,
        "POST_CHILD_COMMENTS",
        "derived_indexes/POST_CHILD_COMMENTS",
        "Post",
        "Comment",
        maps.post.len(),
        post_children,
    )?;
    write_derived_csr(
        output_dir,
        catalog,
        stats,
        "COMMENT_CHILD_COMMENTS",
        "derived_indexes/COMMENT_CHILD_COMMENTS",
        "Comment",
        "Comment",
        maps.comment.len(),
        comment_children,
    )?;
    let mut memo = vec![u32::MAX; maps.comment.len()];
    for idx in 0..maps.comment.len() {
        resolve_comment_root(idx as u32, &parent_kind, &parent_id, &mut memo);
    }
    write_u32_column(
        &output_dir.join("derived_indexes/MESSAGE_ROOT_POST/comment_root_post.col"),
        &memo,
    )?;
    catalog.derived_columns.insert(
        "comment_root_post".to_string(),
        DerivedColumnEntry {
            name: "comment_root_post".to_string(),
            file: "derived_indexes/MESSAGE_ROOT_POST/comment_root_post.col".to_string(),
            rows: memo.len() as u64,
            value_type: "u32".to_string(),
        },
    );
    stats
        .derived_columns
        .insert("comment_root_post".to_string(), memo.len() as u64);
    Ok(())
}

fn build_bidirectional_csr(
    csv_path: &Path,
    output_dir: &Path,
    catalog: &mut BaseGraphCatalog,
    stats: &mut BaseGraphBuildStats,
    name: &str,
    src_label: &str,
    dst_label: &str,
    num_src: usize,
    num_dst: usize,
    src_map: &IdMap,
    dst_map: &IdMap,
    src_col: &str,
    dst_col: &str,
) -> Result<()> {
    let mut out_edges = Vec::new();
    let mut in_edges = Vec::new();
    scan_csv(csv_path, |header, record| {
        let src = src_map.get(parse_i64(record, idx(header, src_col)?)?)?;
        let dst = dst_map.get(parse_i64(record, idx(header, dst_col)?)?)?;
        out_edges.push((src, dst));
        in_edges.push((dst, src));
        Ok(())
    })?;
    write_csr_entry(
        output_dir,
        catalog,
        stats,
        &format!("{name}/OUT"),
        &format!("edges/{name}/OUT"),
        src_label,
        dst_label,
        num_src,
        out_edges,
    )?;
    write_csr_entry(
        output_dir,
        catalog,
        stats,
        &format!("{name}/IN"),
        &format!("edges/{name}/IN"),
        dst_label,
        src_label,
        num_dst,
        in_edges,
    )?;
    Ok(())
}

fn write_derived_csr(
    output_dir: &Path,
    catalog: &mut BaseGraphCatalog,
    stats: &mut BaseGraphBuildStats,
    name: &str,
    rel: &str,
    src_label: &str,
    dst_label: &str,
    num_src: usize,
    edges: Vec<(u32, u32)>,
) -> Result<()> {
    write_csr_entry(
        output_dir, catalog, stats, name, rel, src_label, dst_label, num_src, edges,
    )
}

fn write_csr_entry(
    output_dir: &Path,
    catalog: &mut BaseGraphCatalog,
    stats: &mut BaseGraphBuildStats,
    name: &str,
    rel: &str,
    src_label: &str,
    dst_label: &str,
    num_src: usize,
    edges: Vec<(u32, u32)>,
) -> Result<()> {
    let edge_count = write_csr(&output_dir.join(rel), num_src, edges, SortOrder::DstId)?;
    catalog.csr_adjacencies.insert(
        name.to_string(),
        CsrCatalogEntry {
            name: name.to_string(),
            path: rel.to_string(),
            src_label: src_label.to_string(),
            dst_label: dst_label.to_string(),
            num_src_vertices: num_src as u64,
            num_edges: edge_count,
            offset_type: "u64".to_string(),
            neighbor_type: "u32".to_string(),
            sort_order: "dst_id".to_string(),
            prop_access: "none".to_string(),
            props: Vec::new(),
            neighbor_block_bytes: 256 * 1024,
            file_alignment: 4096,
        },
    );
    stats.csr_edges.insert(name.to_string(), edge_count);
    Ok(())
}

fn write_single_u32(
    output_dir: &Path,
    catalog: &mut BaseGraphCatalog,
    stats: &mut BaseGraphBuildStats,
    name: &str,
    rel: &str,
    src_label: &str,
    dst_label: &str,
    values: &[u32],
) -> Result<()> {
    write_u32_column(&output_dir.join(rel), values)?;
    catalog.single_columns.insert(
        name.to_string(),
        SingleColumnEntry {
            name: name.to_string(),
            file: rel.to_string(),
            src_label: src_label.to_string(),
            dst_label: dst_label.to_string(),
            rows: values.len() as u64,
            value_type: "u32".to_string(),
        },
    );
    stats
        .single_columns
        .insert(name.to_string(), values.len() as u64);
    Ok(())
}

fn load_id_map(path: &Path, id_col: &str) -> Result<IdMap> {
    let mut out = IdMap::default();
    scan_csv(path, |header, record| {
        let external = parse_i64(record, idx(header, id_col)?)?;
        let local = out.ordered_pairs.len() as u32;
        out.external_to_local.insert(external, local);
        out.ordered_pairs.push((external, local));
        Ok(())
    })?;
    Ok(out)
}

fn scan_csv<F>(path: &Path, mut f: F) -> Result<()>
where
    F: FnMut(&StringRecord, &StringRecord) -> Result<()>,
{
    let mut reader = csv::ReaderBuilder::new()
        .delimiter(b'|')
        .has_headers(true)
        .from_path(path)?;
    let header = reader.headers()?.clone();
    for record in reader.records() {
        let record = record?;
        f(&header, &record)?;
    }
    Ok(())
}

fn idx(header: &StringRecord, name: &str) -> Result<usize> {
    header
        .iter()
        .position(|candidate| candidate == name)
        .ok_or_else(|| anyhow::anyhow!("missing column {name} in {:?}", header))
}

fn parse_i64(record: &StringRecord, idx: usize) -> Result<i64> {
    let value = record.get(idx).unwrap_or_default();
    if value.is_empty() {
        anyhow::bail!("empty i64 value at column {idx}");
    }
    Ok(value.parse::<i64>()?)
}

fn resolve_comment_root(
    comment: u32,
    parent_kind: &[u8],
    parent_id: &[u32],
    memo: &mut [u32],
) -> u32 {
    let idx = comment as usize;
    if memo[idx] != u32::MAX {
        return memo[idx];
    }
    let root = match parent_kind[idx] {
        0 => parent_id[idx],
        1 => resolve_comment_root(parent_id[idx], parent_kind, parent_id, memo),
        _ => u32::MAX,
    };
    memo[idx] = root;
    root
}
