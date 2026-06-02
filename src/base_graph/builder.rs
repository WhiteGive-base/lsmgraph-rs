use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};

use csv::StringRecord;
use serde::Serialize;

use crate::base_graph::catalog::{
    BaseGraphCatalog, CsrCatalogEntry, CsrPropEntry, DerivedColumnEntry, SingleColumnEntry,
    VertexCatalogEntry, VertexPropertyEntry,
};
use crate::base_graph::column::{
    write_ext_id_map, write_i64_column, write_string_column, write_u32_column, write_u8_column,
};
use crate::base_graph::csr::{write_csr, write_csr_i32_prop, write_csr_i64_prop, SortOrder};
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
    pub vertex_properties: BTreeMap<String, u64>,
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
    build_vertex_properties(csv_root, output_dir, &maps, &mut catalog, &mut stats)?;
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
        let sorted_rel = format!("vertices/{}/ext_id_to_local.sorted.idx", label.as_str());
        let mut sorted_pairs = map.ordered_pairs.clone();
        sorted_pairs.sort_unstable_by_key(|(external, _)| *external);
        write_ext_id_map(&output_dir.join(&sorted_rel), &sorted_pairs)?;
        let local_rel = format!("vertices/{}/local_to_external.col", label.as_str());
        let local_to_external: Vec<i64> = map
            .ordered_pairs
            .iter()
            .map(|(external, _)| *external)
            .collect();
        write_i64_column(&output_dir.join(&local_rel), &local_to_external)?;
        catalog.vertex_labels.insert(
            label.as_str().to_string(),
            VertexCatalogEntry {
                label: label.as_str().to_string(),
                count: map.len() as u64,
                ext_id_to_local: rel,
                ext_id_to_local_sorted: Some(sorted_rel),
                local_to_external: Some(local_rel),
            },
        );
        stats
            .vertex_counts
            .insert(label.as_str().to_string(), map.len() as u64);
    }
    Ok(maps)
}

fn build_vertex_properties(
    csv_root: &Path,
    output_dir: &Path,
    maps: &IdMaps,
    catalog: &mut BaseGraphCatalog,
    stats: &mut BaseGraphBuildStats,
) -> Result<()> {
    let dynamic = csv_root.join("dynamic");
    let static_dir = csv_root.join("static");

    let mut person_first_name = vec![String::new(); maps.person.len()];
    let mut person_last_name = vec![String::new(); maps.person.len()];
    let mut person_gender = vec![String::new(); maps.person.len()];
    let mut person_birthday = vec![0i64; maps.person.len()];
    let mut person_creation_date = vec![0i64; maps.person.len()];
    let mut person_location_ip = vec![String::new(); maps.person.len()];
    let mut person_browser_used = vec![String::new(); maps.person.len()];
    let mut person_place = vec![0i64; maps.person.len()];
    let mut person_language = vec![String::new(); maps.person.len()];
    let mut person_email = vec![String::new(); maps.person.len()];
    scan_csv(&dynamic.join("person_0_0.csv"), |header, record| {
        let local = maps.person.get(parse_i64(record, idx(header, "id")?)?)? as usize;
        person_first_name[local] = record
            .get(idx(header, "firstName")?)
            .unwrap_or_default()
            .into();
        person_last_name[local] = record
            .get(idx(header, "lastName")?)
            .unwrap_or_default()
            .into();
        person_gender[local] = record
            .get(idx(header, "gender")?)
            .unwrap_or_default()
            .into();
        person_birthday[local] = parse_i64(record, idx(header, "birthday")?)?;
        person_creation_date[local] = parse_i64(record, idx(header, "creationDate")?)?;
        person_location_ip[local] = record
            .get(idx(header, "locationIP")?)
            .unwrap_or_default()
            .into();
        person_browser_used[local] = record
            .get(idx(header, "browserUsed")?)
            .unwrap_or_default()
            .into();
        person_place[local] = parse_i64(record, idx(header, "place")?)?;
        person_language[local] = record
            .get(idx(header, "language")?)
            .unwrap_or_default()
            .into();
        person_email[local] = record.get(idx(header, "email")?).unwrap_or_default().into();
        Ok(())
    })?;
    write_vertex_string(
        output_dir,
        catalog,
        stats,
        "Person",
        "first_name",
        &person_first_name,
    )?;
    write_vertex_string(
        output_dir,
        catalog,
        stats,
        "Person",
        "last_name",
        &person_last_name,
    )?;
    write_vertex_string(
        output_dir,
        catalog,
        stats,
        "Person",
        "gender",
        &person_gender,
    )?;
    write_vertex_i64(
        output_dir,
        catalog,
        stats,
        "Person",
        "birthday",
        &person_birthday,
    )?;
    write_vertex_i64(
        output_dir,
        catalog,
        stats,
        "Person",
        "creation_date",
        &person_creation_date,
    )?;
    write_vertex_string(
        output_dir,
        catalog,
        stats,
        "Person",
        "location_ip",
        &person_location_ip,
    )?;
    write_vertex_string(
        output_dir,
        catalog,
        stats,
        "Person",
        "browser_used",
        &person_browser_used,
    )?;
    write_vertex_i64(output_dir, catalog, stats, "Person", "place", &person_place)?;
    write_vertex_string(
        output_dir,
        catalog,
        stats,
        "Person",
        "language",
        &person_language,
    )?;
    write_vertex_string(output_dir, catalog, stats, "Person", "email", &person_email)?;

    let mut forum_title = vec![String::new(); maps.forum.len()];
    let mut forum_creation_date = vec![0i64; maps.forum.len()];
    let mut forum_moderator = vec![0i64; maps.forum.len()];
    scan_csv(&dynamic.join("forum_0_0.csv"), |header, record| {
        let local = maps.forum.get(parse_i64(record, idx(header, "id")?)?)? as usize;
        forum_title[local] = record.get(idx(header, "title")?).unwrap_or_default().into();
        forum_creation_date[local] = parse_i64(record, idx(header, "creationDate")?)?;
        forum_moderator[local] = parse_i64(record, idx(header, "moderator")?)?;
        Ok(())
    })?;
    write_vertex_string(output_dir, catalog, stats, "Forum", "title", &forum_title)?;
    write_vertex_i64(
        output_dir,
        catalog,
        stats,
        "Forum",
        "creation_date",
        &forum_creation_date,
    )?;
    write_vertex_i64(
        output_dir,
        catalog,
        stats,
        "Forum",
        "moderator",
        &forum_moderator,
    )?;

    let mut post_image_file = vec![String::new(); maps.post.len()];
    let mut post_creation_date = vec![0i64; maps.post.len()];
    let mut post_location_ip = vec![String::new(); maps.post.len()];
    let mut post_browser_used = vec![String::new(); maps.post.len()];
    let mut post_language = vec![String::new(); maps.post.len()];
    let mut post_content = vec![String::new(); maps.post.len()];
    let mut post_length = vec![0i64; maps.post.len()];
    let mut post_creator = vec![0i64; maps.post.len()];
    let mut post_forum = vec![0i64; maps.post.len()];
    let mut post_place = vec![0i64; maps.post.len()];
    scan_csv(&dynamic.join("post_0_0.csv"), |header, record| {
        let local = maps.post.get(parse_i64(record, idx(header, "id")?)?)? as usize;
        post_image_file[local] = record
            .get(idx(header, "imageFile")?)
            .unwrap_or_default()
            .into();
        post_creation_date[local] = parse_i64(record, idx(header, "creationDate")?)?;
        post_location_ip[local] = record
            .get(idx(header, "locationIP")?)
            .unwrap_or_default()
            .into();
        post_browser_used[local] = record
            .get(idx(header, "browserUsed")?)
            .unwrap_or_default()
            .into();
        post_language[local] = record
            .get(idx(header, "language")?)
            .unwrap_or_default()
            .into();
        post_content[local] = record
            .get(idx(header, "content")?)
            .unwrap_or_default()
            .into();
        post_length[local] = parse_i64(record, idx(header, "length")?)?;
        post_creator[local] = parse_i64(record, idx(header, "creator")?)?;
        post_forum[local] = parse_i64(record, idx(header, "Forum.id")?)?;
        post_place[local] = parse_i64(record, idx(header, "place")?)?;
        Ok(())
    })?;
    write_vertex_string(
        output_dir,
        catalog,
        stats,
        "Post",
        "image_file",
        &post_image_file,
    )?;
    write_vertex_i64(
        output_dir,
        catalog,
        stats,
        "Post",
        "creation_date",
        &post_creation_date,
    )?;
    write_vertex_string(
        output_dir,
        catalog,
        stats,
        "Post",
        "location_ip",
        &post_location_ip,
    )?;
    write_vertex_string(
        output_dir,
        catalog,
        stats,
        "Post",
        "browser_used",
        &post_browser_used,
    )?;
    write_vertex_string(
        output_dir,
        catalog,
        stats,
        "Post",
        "language",
        &post_language,
    )?;
    write_vertex_string(output_dir, catalog, stats, "Post", "content", &post_content)?;
    write_vertex_i64(output_dir, catalog, stats, "Post", "length", &post_length)?;
    write_vertex_i64(output_dir, catalog, stats, "Post", "creator", &post_creator)?;
    write_vertex_i64(output_dir, catalog, stats, "Post", "forum_id", &post_forum)?;
    write_vertex_i64(output_dir, catalog, stats, "Post", "place", &post_place)?;

    let mut comment_creation_date = vec![0i64; maps.comment.len()];
    let mut comment_location_ip = vec![String::new(); maps.comment.len()];
    let mut comment_browser_used = vec![String::new(); maps.comment.len()];
    let mut comment_content = vec![String::new(); maps.comment.len()];
    let mut comment_length = vec![0i64; maps.comment.len()];
    let mut comment_creator = vec![0i64; maps.comment.len()];
    let mut comment_place = vec![0i64; maps.comment.len()];
    let mut comment_reply_post = vec![i64::MIN; maps.comment.len()];
    let mut comment_reply_comment = vec![i64::MIN; maps.comment.len()];
    scan_csv(&dynamic.join("comment_0_0.csv"), |header, record| {
        let local = maps.comment.get(parse_i64(record, idx(header, "id")?)?)? as usize;
        comment_creation_date[local] = parse_i64(record, idx(header, "creationDate")?)?;
        comment_location_ip[local] = record
            .get(idx(header, "locationIP")?)
            .unwrap_or_default()
            .into();
        comment_browser_used[local] = record
            .get(idx(header, "browserUsed")?)
            .unwrap_or_default()
            .into();
        comment_content[local] = record
            .get(idx(header, "content")?)
            .unwrap_or_default()
            .into();
        comment_length[local] = parse_i64(record, idx(header, "length")?)?;
        comment_creator[local] = parse_i64(record, idx(header, "creator")?)?;
        comment_place[local] = parse_i64(record, idx(header, "place")?)?;
        let reply_post = record.get(idx(header, "replyOfPost")?).unwrap_or_default();
        if !reply_post.is_empty() {
            comment_reply_post[local] = reply_post.parse::<i64>()?;
        }
        let reply_comment = record
            .get(idx(header, "replyOfComment")?)
            .unwrap_or_default();
        if !reply_comment.is_empty() {
            comment_reply_comment[local] = reply_comment.parse::<i64>()?;
        }
        Ok(())
    })?;
    write_vertex_i64(
        output_dir,
        catalog,
        stats,
        "Comment",
        "creation_date",
        &comment_creation_date,
    )?;
    write_vertex_string(
        output_dir,
        catalog,
        stats,
        "Comment",
        "location_ip",
        &comment_location_ip,
    )?;
    write_vertex_string(
        output_dir,
        catalog,
        stats,
        "Comment",
        "browser_used",
        &comment_browser_used,
    )?;
    write_vertex_string(
        output_dir,
        catalog,
        stats,
        "Comment",
        "content",
        &comment_content,
    )?;
    write_vertex_i64(
        output_dir,
        catalog,
        stats,
        "Comment",
        "length",
        &comment_length,
    )?;
    write_vertex_i64(
        output_dir,
        catalog,
        stats,
        "Comment",
        "creator",
        &comment_creator,
    )?;
    write_vertex_i64(
        output_dir,
        catalog,
        stats,
        "Comment",
        "place",
        &comment_place,
    )?;
    write_vertex_i64(
        output_dir,
        catalog,
        stats,
        "Comment",
        "reply_of_post",
        &comment_reply_post,
    )?;
    write_vertex_i64(
        output_dir,
        catalog,
        stats,
        "Comment",
        "reply_of_comment",
        &comment_reply_comment,
    )?;

    let mut place_name = vec![String::new(); maps.place.len()];
    let mut place_type = vec![String::new(); maps.place.len()];
    let mut place_parent = vec![i64::MIN; maps.place.len()];
    scan_csv(&static_dir.join("place_0_0.csv"), |header, record| {
        let local = maps.place.get(parse_i64(record, idx(header, "id")?)?)? as usize;
        place_name[local] = record.get(idx(header, "name")?).unwrap_or_default().into();
        place_type[local] = record.get(idx(header, "type")?).unwrap_or_default().into();
        let parent = record.get(idx(header, "isPartOf")?).unwrap_or_default();
        if !parent.is_empty() {
            place_parent[local] = parent.parse::<i64>()?;
        }
        Ok(())
    })?;
    write_vertex_string(output_dir, catalog, stats, "Place", "name", &place_name)?;
    write_vertex_string(
        output_dir,
        catalog,
        stats,
        "Place",
        "place_type",
        &place_type,
    )?;
    write_vertex_i64(
        output_dir,
        catalog,
        stats,
        "Place",
        "is_part_of",
        &place_parent,
    )?;

    let mut org_type = vec![String::new(); maps.organisation.len()];
    let mut org_name = vec![String::new(); maps.organisation.len()];
    let mut org_place = vec![0i64; maps.organisation.len()];
    scan_csv(
        &static_dir.join("organisation_0_0.csv"),
        |header, record| {
            let local = maps
                .organisation
                .get(parse_i64(record, idx(header, "id")?)?)? as usize;
            org_type[local] = record.get(idx(header, "type")?).unwrap_or_default().into();
            org_name[local] = record.get(idx(header, "name")?).unwrap_or_default().into();
            org_place[local] = parse_i64(record, idx(header, "place")?)?;
            Ok(())
        },
    )?;
    write_vertex_string(
        output_dir,
        catalog,
        stats,
        "Organisation",
        "org_type",
        &org_type,
    )?;
    write_vertex_string(
        output_dir,
        catalog,
        stats,
        "Organisation",
        "name",
        &org_name,
    )?;
    write_vertex_i64(
        output_dir,
        catalog,
        stats,
        "Organisation",
        "place",
        &org_place,
    )?;

    let mut tag_name = vec![String::new(); maps.tag.len()];
    let mut tag_has_type = vec![0i64; maps.tag.len()];
    scan_csv(&static_dir.join("tag_0_0.csv"), |header, record| {
        let local = maps.tag.get(parse_i64(record, idx(header, "id")?)?)? as usize;
        tag_name[local] = record.get(idx(header, "name")?).unwrap_or_default().into();
        tag_has_type[local] = parse_i64(record, idx(header, "hasType")?)?;
        Ok(())
    })?;
    write_vertex_string(output_dir, catalog, stats, "Tag", "name", &tag_name)?;
    write_vertex_i64(output_dir, catalog, stats, "Tag", "has_type", &tag_has_type)?;

    let mut tagclass_name = vec![String::new(); maps.tag_class.len()];
    let mut tagclass_parent = vec![i64::MIN; maps.tag_class.len()];
    scan_csv(&static_dir.join("tagclass_0_0.csv"), |header, record| {
        let local = maps.tag_class.get(parse_i64(record, idx(header, "id")?)?)? as usize;
        tagclass_name[local] = record.get(idx(header, "name")?).unwrap_or_default().into();
        let parent = record.get(idx(header, "isSubclassOf")?).unwrap_or_default();
        if !parent.is_empty() {
            tagclass_parent[local] = parent.parse::<i64>()?;
        }
        Ok(())
    })?;
    write_vertex_string(
        output_dir,
        catalog,
        stats,
        "TagClass",
        "name",
        &tagclass_name,
    )?;
    write_vertex_i64(
        output_dir,
        catalog,
        stats,
        "TagClass",
        "is_subclass_of",
        &tagclass_parent,
    )?;

    Ok(())
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

    let mut place_parent = vec![u32::MAX; maps.place.len()];
    let mut place_children = Vec::new();
    scan_csv(&static_dir.join("place_0_0.csv"), |header, record| {
        let place = maps.place.get(parse_i64(record, idx(header, "id")?)?)?;
        let parent = record.get(idx(header, "isPartOf")?).unwrap_or_default();
        if !parent.is_empty() {
            let parent = maps.place.get(parent.parse::<i64>()?)?;
            place_parent[place as usize] = parent;
            place_children.push((parent, place));
        }
        Ok(())
    })?;
    write_single_u32(
        output_dir,
        catalog,
        stats,
        "place_parent",
        "single_edges/IS_PART_OF/place_parent.col",
        "Place",
        "Place",
        &place_parent,
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
    let mut tagclass_parent = vec![u32::MAX; maps.tag_class.len()];
    let mut tagclass_children = Vec::new();
    scan_csv(&static_dir.join("tagclass_0_0.csv"), |header, record| {
        let tagclass = maps.tag_class.get(parse_i64(record, idx(header, "id")?)?)?;
        let parent = record.get(idx(header, "isSubclassOf")?).unwrap_or_default();
        if !parent.is_empty() {
            let parent = maps.tag_class.get(parent.parse::<i64>()?)?;
            tagclass_parent[tagclass as usize] = parent;
            tagclass_children.push((parent, tagclass));
        }
        Ok(())
    })?;
    write_single_u32(
        output_dir,
        catalog,
        stats,
        "tagclass_parent",
        "single_edges/IS_SUBCLASS_OF/tagclass_parent.col",
        "TagClass",
        "TagClass",
        &tagclass_parent,
    )?;
    write_derived_csr(
        output_dir,
        catalog,
        stats,
        "PLACE_CHILD_PLACES",
        "derived_indexes/PLACE_CHILD_PLACES",
        "Place",
        "Place",
        maps.place.len(),
        place_children,
    )?;
    write_derived_csr(
        output_dir,
        catalog,
        stats,
        "TAGCLASS_CHILD_CLASSES",
        "derived_indexes/TAGCLASS_CHILD_CLASSES",
        "TagClass",
        "TagClass",
        maps.tag_class.len(),
        tagclass_children,
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
    build_bidirectional_csr_i64_prop(
        &dynamic.join("person_knows_person_0_0.csv"),
        output_dir,
        catalog,
        stats,
        "KNOWS",
        "creation_date",
        "creation_date.bin",
        "Person",
        "Person",
        maps.person.len(),
        maps.person.len(),
        &maps.person,
        &maps.person,
        "Person.id",
        "Person.id",
        "creationDate",
    )?;
    build_bidirectional_csr_i64_prop(
        &dynamic.join("person_likes_post_0_0.csv"),
        output_dir,
        catalog,
        stats,
        "PERSON_LIKES_POST",
        "creation_date",
        "creation_date.bin",
        "Person",
        "Post",
        maps.person.len(),
        maps.post.len(),
        &maps.person,
        &maps.post,
        "Person.id",
        "Post.id",
        "creationDate",
    )?;
    build_bidirectional_csr_i64_prop(
        &dynamic.join("person_likes_comment_0_0.csv"),
        output_dir,
        catalog,
        stats,
        "PERSON_LIKES_COMMENT",
        "creation_date",
        "creation_date.bin",
        "Person",
        "Comment",
        maps.person.len(),
        maps.comment.len(),
        &maps.person,
        &maps.comment,
        "Person.id",
        "Comment.id",
        "creationDate",
    )?;
    build_bidirectional_csr_i64_prop(
        &dynamic.join("forum_hasMember_person_0_0.csv"),
        output_dir,
        catalog,
        stats,
        "FORUM_HAS_MEMBER",
        "join_date",
        "join_date.bin",
        "Forum",
        "Person",
        maps.forum.len(),
        maps.person.len(),
        &maps.forum,
        &maps.person,
        "Forum.id",
        "Person.id",
        "joinDate",
    )?;
    build_bidirectional_csr(
        &dynamic.join("person_hasInterest_tag_0_0.csv"),
        output_dir,
        catalog,
        stats,
        "PERSON_HAS_INTEREST",
        "Person",
        "Tag",
        maps.person.len(),
        maps.tag.len(),
        &maps.person,
        &maps.tag,
        "Person.id",
        "Tag.id",
    )?;
    build_bidirectional_csr(
        &dynamic.join("post_hasTag_tag_0_0.csv"),
        output_dir,
        catalog,
        stats,
        "POST_HAS_TAG",
        "Post",
        "Tag",
        maps.post.len(),
        maps.tag.len(),
        &maps.post,
        &maps.tag,
        "Post.id",
        "Tag.id",
    )?;
    build_bidirectional_csr(
        &dynamic.join("comment_hasTag_tag_0_0.csv"),
        output_dir,
        catalog,
        stats,
        "COMMENT_HAS_TAG",
        "Comment",
        "Tag",
        maps.comment.len(),
        maps.tag.len(),
        &maps.comment,
        &maps.tag,
        "Comment.id",
        "Tag.id",
    )?;
    build_bidirectional_csr(
        &dynamic.join("forum_hasTag_tag_0_0.csv"),
        output_dir,
        catalog,
        stats,
        "FORUM_HAS_TAG",
        "Forum",
        "Tag",
        maps.forum.len(),
        maps.tag.len(),
        &maps.forum,
        &maps.tag,
        "Forum.id",
        "Tag.id",
    )?;
    build_bidirectional_csr_i32_prop(
        &dynamic.join("person_workAt_organisation_0_0.csv"),
        output_dir,
        catalog,
        stats,
        "WORK_AT",
        "work_from",
        "work_from.bin",
        "Person",
        "Organisation",
        maps.person.len(),
        maps.organisation.len(),
        &maps.person,
        &maps.organisation,
        "Person.id",
        "Organisation.id",
        "workFrom",
    )?;
    build_bidirectional_csr_i32_prop(
        &dynamic.join("person_studyAt_organisation_0_0.csv"),
        output_dir,
        catalog,
        stats,
        "STUDY_AT",
        "class_year",
        "class_year.bin",
        "Person",
        "Organisation",
        maps.person.len(),
        maps.organisation.len(),
        &maps.person,
        &maps.organisation,
        "Person.id",
        "Organisation.id",
        "classYear",
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
        let (src_idx, dst_idx) = idx_pair(header, src_col, dst_col)?;
        let src = src_map.get(parse_i64(record, src_idx)?)?;
        let dst = dst_map.get(parse_i64(record, dst_idx)?)?;
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

fn build_bidirectional_csr_i64_prop(
    csv_path: &Path,
    output_dir: &Path,
    catalog: &mut BaseGraphCatalog,
    stats: &mut BaseGraphBuildStats,
    name: &str,
    prop_name: &str,
    prop_file: &str,
    src_label: &str,
    dst_label: &str,
    num_src: usize,
    num_dst: usize,
    src_map: &IdMap,
    dst_map: &IdMap,
    src_col: &str,
    dst_col: &str,
    prop_col: &str,
) -> Result<()> {
    let mut out_edges = Vec::new();
    let mut in_edges = Vec::new();
    scan_csv(csv_path, |header, record| {
        let (src_idx, dst_idx) = idx_pair(header, src_col, dst_col)?;
        let src = src_map.get(parse_i64(record, src_idx)?)?;
        let dst = dst_map.get(parse_i64(record, dst_idx)?)?;
        let prop = parse_i64(record, idx(header, prop_col)?)?;
        out_edges.push((src, dst, prop));
        in_edges.push((dst, src, prop));
        Ok(())
    })?;
    write_csr_i64_prop_entry(
        output_dir,
        catalog,
        stats,
        &format!("{name}/OUT"),
        &format!("edges/{name}/OUT"),
        src_label,
        dst_label,
        num_src,
        prop_name,
        prop_file,
        out_edges,
    )?;
    write_csr_i64_prop_entry(
        output_dir,
        catalog,
        stats,
        &format!("{name}/IN"),
        &format!("edges/{name}/IN"),
        dst_label,
        src_label,
        num_dst,
        prop_name,
        prop_file,
        in_edges,
    )?;
    Ok(())
}

fn build_bidirectional_csr_i32_prop(
    csv_path: &Path,
    output_dir: &Path,
    catalog: &mut BaseGraphCatalog,
    stats: &mut BaseGraphBuildStats,
    name: &str,
    prop_name: &str,
    prop_file: &str,
    src_label: &str,
    dst_label: &str,
    num_src: usize,
    num_dst: usize,
    src_map: &IdMap,
    dst_map: &IdMap,
    src_col: &str,
    dst_col: &str,
    prop_col: &str,
) -> Result<()> {
    let mut out_edges = Vec::new();
    let mut in_edges = Vec::new();
    scan_csv(csv_path, |header, record| {
        let (src_idx, dst_idx) = idx_pair(header, src_col, dst_col)?;
        let src = src_map.get(parse_i64(record, src_idx)?)?;
        let dst = dst_map.get(parse_i64(record, dst_idx)?)?;
        let prop = parse_i64(record, idx(header, prop_col)?)? as i32;
        out_edges.push((src, dst, prop));
        in_edges.push((dst, src, prop));
        Ok(())
    })?;
    write_csr_i32_prop_entry(
        output_dir,
        catalog,
        stats,
        &format!("{name}/OUT"),
        &format!("edges/{name}/OUT"),
        src_label,
        dst_label,
        num_src,
        prop_name,
        prop_file,
        out_edges,
    )?;
    write_csr_i32_prop_entry(
        output_dir,
        catalog,
        stats,
        &format!("{name}/IN"),
        &format!("edges/{name}/IN"),
        dst_label,
        src_label,
        num_dst,
        prop_name,
        prop_file,
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

fn write_csr_i64_prop_entry(
    output_dir: &Path,
    catalog: &mut BaseGraphCatalog,
    stats: &mut BaseGraphBuildStats,
    name: &str,
    rel: &str,
    src_label: &str,
    dst_label: &str,
    num_src: usize,
    prop_name: &str,
    prop_file: &str,
    edges: Vec<(u32, u32, i64)>,
) -> Result<()> {
    let edge_count = write_csr_i64_prop(
        &output_dir.join(rel),
        num_src,
        edges,
        SortOrder::DstId,
        prop_file,
    )?;
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
            prop_access: "aligned".to_string(),
            props: vec![CsrPropEntry {
                name: prop_name.to_string(),
                file: prop_file.to_string(),
                value_type: "i64".to_string(),
            }],
            neighbor_block_bytes: 256 * 1024,
            file_alignment: 4096,
        },
    );
    stats.csr_edges.insert(name.to_string(), edge_count);
    Ok(())
}

fn write_csr_i32_prop_entry(
    output_dir: &Path,
    catalog: &mut BaseGraphCatalog,
    stats: &mut BaseGraphBuildStats,
    name: &str,
    rel: &str,
    src_label: &str,
    dst_label: &str,
    num_src: usize,
    prop_name: &str,
    prop_file: &str,
    edges: Vec<(u32, u32, i32)>,
) -> Result<()> {
    let edge_count = write_csr_i32_prop(
        &output_dir.join(rel),
        num_src,
        edges,
        SortOrder::Unsorted,
        prop_file,
    )?;
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
            sort_order: SortOrder::Unsorted.as_str().to_string(),
            prop_access: "aligned".to_string(),
            props: vec![CsrPropEntry {
                name: prop_name.to_string(),
                file: prop_file.to_string(),
                value_type: "i32".to_string(),
            }],
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

fn write_vertex_i64(
    output_dir: &Path,
    catalog: &mut BaseGraphCatalog,
    stats: &mut BaseGraphBuildStats,
    label: &str,
    name: &str,
    values: &[i64],
) -> Result<()> {
    let rel = format!("vertices/{label}/{name}.col");
    write_i64_column(&output_dir.join(&rel), values)?;
    let key = format!("{label}.{name}");
    catalog.vertex_properties.insert(
        key.clone(),
        VertexPropertyEntry {
            label: label.to_string(),
            name: name.to_string(),
            file: rel,
            rows: values.len() as u64,
            value_type: "i64".to_string(),
            encoding: "plain".to_string(),
        },
    );
    stats.vertex_properties.insert(key, values.len() as u64);
    Ok(())
}

fn write_vertex_string(
    output_dir: &Path,
    catalog: &mut BaseGraphCatalog,
    stats: &mut BaseGraphBuildStats,
    label: &str,
    name: &str,
    values: &[String],
) -> Result<()> {
    let rel = format!("vertices/{label}/{name}");
    write_string_column(&output_dir.join(&rel), values)?;
    let key = format!("{label}.{name}");
    catalog.vertex_properties.insert(
        key.clone(),
        VertexPropertyEntry {
            label: label.to_string(),
            name: name.to_string(),
            file: rel,
            rows: values.len() as u64,
            value_type: "string".to_string(),
            encoding: "offset_blob".to_string(),
        },
    );
    stats.vertex_properties.insert(key, values.len() as u64);
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

fn idx_pair(header: &StringRecord, first: &str, second: &str) -> Result<(usize, usize)> {
    if first != second {
        return Ok((idx(header, first)?, idx(header, second)?));
    }
    let mut matches = header
        .iter()
        .enumerate()
        .filter_map(|(idx, candidate)| (candidate == first).then_some(idx));
    let first_idx = matches
        .next()
        .ok_or_else(|| anyhow::anyhow!("missing column {first} in {:?}", header))?;
    let second_idx = matches
        .next()
        .ok_or_else(|| anyhow::anyhow!("missing second column {second} in {:?}", header))?;
    Ok((first_idx, second_idx))
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
