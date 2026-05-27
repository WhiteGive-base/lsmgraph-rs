use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::path::Path;
use std::sync::Arc;
use std::time::Instant;

use csv::StringRecord;

use crate::error::Result;
use crate::graph::Engine;
use crate::loader::ImportStats;
use crate::snb::props::{
    encode_vid, CommentProps, EdgeProp, EdgePropRow, ForumProps, OrgProps, PersonProps, PlaceProps,
    PostProps, SnbGraph, TagClassProps, TagProps, VertexData, VertexRow,
};
use crate::types::{EdgeLabel, VertexLabel};

const IMPORT_PROGRESS_ROWS: u64 = 1_000_000;

pub async fn import_snb_full(
    engine: Arc<Engine>,
    csv_root: &Path,
    store_dir: &Path,
) -> Result<ImportStats> {
    let import_started = Instant::now();
    fs::create_dir_all(store_dir)?;
    let vertex_path = store_dir.join("snb_vertices.jsonl");
    let edge_prop_path = store_dir.join("snb_edge_props.jsonl");
    let mut vertices = BufWriter::new(File::create(&vertex_path)?);
    let mut edge_props = BufWriter::new(File::create(&edge_prop_path)?);
    let mut stats = ImportStats {
        input_rows: 0,
        directed_edges: 0,
    };

    eprintln!("[snb-full] importing vertices from {}", csv_root.display());
    import_vertices(csv_root, &mut vertices)?;
    vertices.flush()?;
    eprintln!(
        "[snb-full] vertices written to {} elapsed_s={:.1}",
        vertex_path.display(),
        import_started.elapsed().as_secs_f64()
    );

    let dynamic = csv_root.join("dynamic");
    let static_dir = csv_root.join("static");

    stats.add(
        import_edge_file(
            engine.clone(),
            &mut edge_props,
            &dynamic.join("person_knows_person_0_0.csv"),
            VertexLabel::Person,
            VertexLabel::Person,
            EdgeLabel::Knows,
            true,
            ColRef::Index(0),
            ColRef::Index(1),
            PropSpec::I64Col(2),
        )
        .await?,
    );
    stats.add(
        import_edge_file(
            engine.clone(),
            &mut edge_props,
            &dynamic.join("person_likes_comment_0_0.csv"),
            VertexLabel::Person,
            VertexLabel::Comment,
            EdgeLabel::LikesComment,
            false,
            ColRef::Index(0),
            ColRef::Index(1),
            PropSpec::I64Col(2),
        )
        .await?,
    );
    stats.add(
        import_edge_file(
            engine.clone(),
            &mut edge_props,
            &dynamic.join("person_likes_post_0_0.csv"),
            VertexLabel::Person,
            VertexLabel::Post,
            EdgeLabel::LikesPost,
            false,
            ColRef::Index(0),
            ColRef::Index(1),
            PropSpec::I64Col(2),
        )
        .await?,
    );
    stats.add(
        import_edge_file(
            engine.clone(),
            &mut edge_props,
            &dynamic.join("person_hasInterest_tag_0_0.csv"),
            VertexLabel::Person,
            VertexLabel::Tag,
            EdgeLabel::HasInterest,
            false,
            ColRef::Index(0),
            ColRef::Index(1),
            PropSpec::None,
        )
        .await?,
    );
    stats.add(
        import_edge_file(
            engine.clone(),
            &mut edge_props,
            &dynamic.join("person_studyAt_organisation_0_0.csv"),
            VertexLabel::Person,
            VertexLabel::Organisation,
            EdgeLabel::StudyAt,
            false,
            ColRef::Index(0),
            ColRef::Index(1),
            PropSpec::I32Col(2),
        )
        .await?,
    );
    stats.add(
        import_edge_file(
            engine.clone(),
            &mut edge_props,
            &dynamic.join("person_workAt_organisation_0_0.csv"),
            VertexLabel::Person,
            VertexLabel::Organisation,
            EdgeLabel::WorkAt,
            false,
            ColRef::Index(0),
            ColRef::Index(1),
            PropSpec::I32Col(2),
        )
        .await?,
    );
    stats.add(
        import_edge_file(
            engine.clone(),
            &mut edge_props,
            &dynamic.join("forum_hasMember_person_0_0.csv"),
            VertexLabel::Forum,
            VertexLabel::Person,
            EdgeLabel::HasMember,
            false,
            ColRef::Index(0),
            ColRef::Index(1),
            PropSpec::I64Col(2),
        )
        .await?,
    );

    for (path, src, dst) in [
        (
            dynamic.join("forum_hasTag_tag_0_0.csv"),
            VertexLabel::Forum,
            VertexLabel::Tag,
        ),
        (
            dynamic.join("comment_hasTag_tag_0_0.csv"),
            VertexLabel::Comment,
            VertexLabel::Tag,
        ),
        (
            dynamic.join("post_hasTag_tag_0_0.csv"),
            VertexLabel::Post,
            VertexLabel::Tag,
        ),
    ] {
        stats.add(
            import_edge_file(
                engine.clone(),
                &mut edge_props,
                &path,
                src,
                dst,
                EdgeLabel::HasTag,
                false,
                ColRef::Index(0),
                ColRef::Index(1),
                PropSpec::None,
            )
            .await?,
        );
    }

    for spec in [
        NamedEdgeSpec::new(
            dynamic.join("person_0_0.csv"),
            VertexLabel::Person,
            VertexLabel::Place,
            EdgeLabel::IsLocatedIn,
            "id",
            "place",
        ),
        NamedEdgeSpec::new(
            dynamic.join("forum_0_0.csv"),
            VertexLabel::Forum,
            VertexLabel::Person,
            EdgeLabel::HasModerator,
            "id",
            "moderator",
        ),
        NamedEdgeSpec::new(
            dynamic.join("post_0_0.csv"),
            VertexLabel::Post,
            VertexLabel::Person,
            EdgeLabel::HasCreator,
            "id",
            "creator",
        ),
        NamedEdgeSpec::new(
            dynamic.join("post_0_0.csv"),
            VertexLabel::Forum,
            VertexLabel::Post,
            EdgeLabel::ContainerOf,
            "Forum.id",
            "id",
        ),
        NamedEdgeSpec::new(
            dynamic.join("post_0_0.csv"),
            VertexLabel::Post,
            VertexLabel::Place,
            EdgeLabel::IsLocatedIn,
            "id",
            "place",
        ),
        NamedEdgeSpec::new(
            dynamic.join("comment_0_0.csv"),
            VertexLabel::Comment,
            VertexLabel::Person,
            EdgeLabel::HasCreator,
            "id",
            "creator",
        ),
        NamedEdgeSpec::new(
            dynamic.join("comment_0_0.csv"),
            VertexLabel::Comment,
            VertexLabel::Place,
            EdgeLabel::IsLocatedIn,
            "id",
            "place",
        ),
        NamedEdgeSpec::new(
            dynamic.join("comment_0_0.csv"),
            VertexLabel::Comment,
            VertexLabel::Post,
            EdgeLabel::ReplyOfPost,
            "id",
            "replyOfPost",
        ),
        NamedEdgeSpec::new(
            dynamic.join("comment_0_0.csv"),
            VertexLabel::Comment,
            VertexLabel::Comment,
            EdgeLabel::ReplyOfComment,
            "id",
            "replyOfComment",
        ),
        NamedEdgeSpec::new(
            static_dir.join("organisation_0_0.csv"),
            VertexLabel::Organisation,
            VertexLabel::Place,
            EdgeLabel::IsLocatedIn,
            "id",
            "place",
        ),
        NamedEdgeSpec::new(
            static_dir.join("place_0_0.csv"),
            VertexLabel::Place,
            VertexLabel::Place,
            EdgeLabel::IsPartOf,
            "id",
            "isPartOf",
        ),
        NamedEdgeSpec::new(
            static_dir.join("tag_0_0.csv"),
            VertexLabel::Tag,
            VertexLabel::TagClass,
            EdgeLabel::HasType,
            "id",
            "hasType",
        ),
        NamedEdgeSpec::new(
            static_dir.join("tagclass_0_0.csv"),
            VertexLabel::TagClass,
            VertexLabel::TagClass,
            EdgeLabel::IsSubclassOf,
            "id",
            "isSubclassOf",
        ),
    ] {
        stats.add(import_named_edge(engine.clone(), &mut edge_props, spec).await?);
    }

    edge_props.flush()?;
    eprintln!("[snb-full] edge import complete input_rows={} directed_edges={} edge_props={} elapsed_s={:.1}", stats.input_rows, stats.directed_edges, edge_prop_path.display(), import_started.elapsed().as_secs_f64());
    let flush_started = Instant::now();
    eprintln!("[snb-full] flushing active MemGraph to CSR");
    engine.flush_active().await?;
    eprintln!(
        "[snb-full] flush complete elapsed_s={:.1}",
        flush_started.elapsed().as_secs_f64()
    );
    let cache_started = Instant::now();
    eprintln!("[snb-full] building adjacency cache");
    let groups = SnbGraph::build_adjacency_cache(engine, store_dir).await?;
    eprintln!(
        "[snb-full] adjacency cache complete groups={} elapsed_s={:.1} total_elapsed_s={:.1}",
        groups,
        cache_started.elapsed().as_secs_f64(),
        import_started.elapsed().as_secs_f64()
    );
    Ok(stats)
}

pub fn rebuild_snb_edge_props(csv_root: &Path, store_dir: &Path) -> Result<u64> {
    fs::create_dir_all(store_dir)?;
    let path = store_dir.join("snb_edge_props.jsonl");
    let tmp_path = path.with_extension("jsonl.tmp");
    let mut edge_props = BufWriter::new(File::create(&tmp_path)?);
    let dynamic = csv_root.join("dynamic");
    let mut rows = 0u64;

    rows += write_edge_prop_file(
        &mut edge_props,
        &dynamic.join("person_knows_person_0_0.csv"),
        VertexLabel::Person,
        VertexLabel::Person,
        EdgeLabel::Knows,
        true,
        ColRef::Index(0),
        ColRef::Index(1),
        PropSpec::I64Col(2),
    )?;
    rows += write_edge_prop_file(
        &mut edge_props,
        &dynamic.join("person_likes_comment_0_0.csv"),
        VertexLabel::Person,
        VertexLabel::Comment,
        EdgeLabel::LikesComment,
        false,
        ColRef::Index(0),
        ColRef::Index(1),
        PropSpec::I64Col(2),
    )?;
    rows += write_edge_prop_file(
        &mut edge_props,
        &dynamic.join("person_likes_post_0_0.csv"),
        VertexLabel::Person,
        VertexLabel::Post,
        EdgeLabel::LikesPost,
        false,
        ColRef::Index(0),
        ColRef::Index(1),
        PropSpec::I64Col(2),
    )?;
    rows += write_edge_prop_file(
        &mut edge_props,
        &dynamic.join("person_studyAt_organisation_0_0.csv"),
        VertexLabel::Person,
        VertexLabel::Organisation,
        EdgeLabel::StudyAt,
        false,
        ColRef::Index(0),
        ColRef::Index(1),
        PropSpec::I32Col(2),
    )?;
    rows += write_edge_prop_file(
        &mut edge_props,
        &dynamic.join("person_workAt_organisation_0_0.csv"),
        VertexLabel::Person,
        VertexLabel::Organisation,
        EdgeLabel::WorkAt,
        false,
        ColRef::Index(0),
        ColRef::Index(1),
        PropSpec::I32Col(2),
    )?;
    rows += write_edge_prop_file(
        &mut edge_props,
        &dynamic.join("forum_hasMember_person_0_0.csv"),
        VertexLabel::Forum,
        VertexLabel::Person,
        EdgeLabel::HasMember,
        false,
        ColRef::Index(0),
        ColRef::Index(1),
        PropSpec::I64Col(2),
    )?;

    edge_props.flush()?;
    fs::rename(tmp_path, path)?;
    Ok(rows)
}

pub async fn import_snb_updates(
    engine: Arc<Engine>,
    csv_root: &Path,
    store_dir: &Path,
) -> Result<ImportStats> {
    fs::create_dir_all(store_dir)?;
    let mut vertices = BufWriter::new(
        OpenOptions::new()
            .create(true)
            .append(true)
            .open(store_dir.join("snb_vertices.jsonl"))?,
    );
    let mut edge_props = BufWriter::new(
        OpenOptions::new()
            .create(true)
            .append(true)
            .open(store_dir.join("snb_edge_props.jsonl"))?,
    );
    let mut stats = ImportStats {
        input_rows: 0,
        directed_edges: 0,
    };
    let mut files = update_stream_files(csv_root)?;
    files.sort();
    for path in files {
        import_update_stream_file(
            engine.clone(),
            &mut vertices,
            &mut edge_props,
            &path,
            &mut stats,
        )
        .await?;
    }
    vertices.flush()?;
    edge_props.flush()?;
    engine.flush_active().await?;
    SnbGraph::build_adjacency_cache(engine, store_dir).await?;
    Ok(stats)
}

fn import_vertices(csv_root: &Path, out: &mut BufWriter<File>) -> Result<()> {
    let dynamic = csv_root.join("dynamic");
    let static_dir = csv_root.join("static");
    import_vertex_file(
        &static_dir.join("place_0_0.csv"),
        out,
        VertexLabel::Place,
        |headers, rec| {
            Ok(VertexData::Place(PlaceProps {
                id: get_i64(headers, rec, "id")?,
                name: get(headers, rec, "name").to_string(),
                place_type: get(headers, rec, "type").to_string(),
                is_part_of: parse_optional_i64(get(headers, rec, "isPartOf"))?,
            }))
        },
    )?;
    import_vertex_file(
        &static_dir.join("organisation_0_0.csv"),
        out,
        VertexLabel::Organisation,
        |headers, rec| {
            Ok(VertexData::Organisation(OrgProps {
                id: get_i64(headers, rec, "id")?,
                org_type: get(headers, rec, "type").to_string(),
                name: get(headers, rec, "name").to_string(),
                place: get_i64(headers, rec, "place")?,
            }))
        },
    )?;
    import_vertex_file(
        &static_dir.join("tag_0_0.csv"),
        out,
        VertexLabel::Tag,
        |headers, rec| {
            Ok(VertexData::Tag(TagProps {
                id: get_i64(headers, rec, "id")?,
                name: get(headers, rec, "name").to_string(),
                has_type: get_i64(headers, rec, "hasType")?,
            }))
        },
    )?;
    import_vertex_file(
        &static_dir.join("tagclass_0_0.csv"),
        out,
        VertexLabel::TagClass,
        |headers, rec| {
            Ok(VertexData::TagClass(TagClassProps {
                id: get_i64(headers, rec, "id")?,
                name: get(headers, rec, "name").to_string(),
                is_subclass_of: parse_optional_i64(get(headers, rec, "isSubclassOf"))?,
            }))
        },
    )?;
    import_vertex_file(
        &dynamic.join("person_0_0.csv"),
        out,
        VertexLabel::Person,
        |headers, rec| {
            Ok(VertexData::Person(PersonProps {
                id: get_i64(headers, rec, "id")?,
                first_name: get(headers, rec, "firstName").to_string(),
                last_name: get(headers, rec, "lastName").to_string(),
                gender: get(headers, rec, "gender").to_string(),
                birthday: get_i64(headers, rec, "birthday")?,
                creation_date: get_i64(headers, rec, "creationDate")?,
                location_ip: get(headers, rec, "locationIP").to_string(),
                browser_used: get(headers, rec, "browserUsed").to_string(),
                place: get_i64(headers, rec, "place")?,
                language: get(headers, rec, "language").to_string(),
                email: get(headers, rec, "email").to_string(),
            }))
        },
    )?;
    import_vertex_file(
        &dynamic.join("forum_0_0.csv"),
        out,
        VertexLabel::Forum,
        |headers, rec| {
            Ok(VertexData::Forum(ForumProps {
                id: get_i64(headers, rec, "id")?,
                title: get(headers, rec, "title").to_string(),
                creation_date: get_i64(headers, rec, "creationDate")?,
                moderator: get_i64(headers, rec, "moderator")?,
            }))
        },
    )?;
    import_vertex_file(
        &dynamic.join("post_0_0.csv"),
        out,
        VertexLabel::Post,
        |headers, rec| {
            Ok(VertexData::Post(PostProps {
                id: get_i64(headers, rec, "id")?,
                image_file: get(headers, rec, "imageFile").to_string(),
                creation_date: get_i64(headers, rec, "creationDate")?,
                location_ip: get(headers, rec, "locationIP").to_string(),
                browser_used: get(headers, rec, "browserUsed").to_string(),
                language: get(headers, rec, "language").to_string(),
                content: get(headers, rec, "content").to_string(),
                length: get_i64(headers, rec, "length")?,
                creator: get_i64(headers, rec, "creator")?,
                forum_id: get_i64(headers, rec, "Forum.id")?,
                place: get_i64(headers, rec, "place")?,
            }))
        },
    )?;
    import_vertex_file(
        &dynamic.join("comment_0_0.csv"),
        out,
        VertexLabel::Comment,
        |headers, rec| {
            Ok(VertexData::Comment(CommentProps {
                id: get_i64(headers, rec, "id")?,
                creation_date: get_i64(headers, rec, "creationDate")?,
                location_ip: get(headers, rec, "locationIP").to_string(),
                browser_used: get(headers, rec, "browserUsed").to_string(),
                content: get(headers, rec, "content").to_string(),
                length: get_i64(headers, rec, "length")?,
                creator: get_i64(headers, rec, "creator")?,
                place: get_i64(headers, rec, "place")?,
                reply_of_post: parse_optional_i64(get(headers, rec, "replyOfPost"))?,
                reply_of_comment: parse_optional_i64(get(headers, rec, "replyOfComment"))?,
            }))
        },
    )?;
    Ok(())
}

fn import_vertex_file<F>(
    path: &Path,
    out: &mut BufWriter<File>,
    label: VertexLabel,
    mut build: F,
) -> Result<()>
where
    F: FnMut(&StringRecord, &StringRecord) -> Result<VertexData>,
{
    let started = Instant::now();
    eprintln!(
        "[snb-full][vertex] start file={} label={:?}",
        path.display(),
        label
    );
    let mut rdr = csv::ReaderBuilder::new()
        .delimiter(b'|')
        .has_headers(true)
        .from_path(path)?;
    let headers = rdr.headers()?.clone();
    let mut rows = 0u64;
    for rec in rdr.records() {
        let rec = rec?;
        rows += 1;
        let data = build(&headers, &rec)?;
        let vid = encode_vid(label, data.external_id());
        serde_json::to_writer(out.by_ref(), &VertexRow { vid, data })?;
        out.write_all(b"\n")?;
        if rows % IMPORT_PROGRESS_ROWS == 0 {
            eprintln!(
                "[snb-full][vertex] progress file={} rows={} elapsed_s={:.1}",
                path.display(),
                rows,
                started.elapsed().as_secs_f64()
            );
        }
    }
    eprintln!(
        "[snb-full][vertex] done file={} rows={} elapsed_s={:.1}",
        path.display(),
        rows,
        started.elapsed().as_secs_f64()
    );
    Ok(())
}

async fn import_update_stream_file(
    engine: Arc<Engine>,
    vertices: &mut BufWriter<File>,
    edge_props: &mut BufWriter<File>,
    path: &Path,
    stats: &mut ImportStats,
) -> Result<()> {
    let started = Instant::now();
    eprintln!("[snb-full][update] start file={}", path.display());
    let file = File::open(path)?;
    let reader = BufReader::new(file);
    let mut file_rows = 0u64;
    for line in reader.lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let parts: Vec<&str> = line.split('|').collect();
        if parts.len() < 3 {
            continue;
        }
        stats.input_rows += 1;
        file_rows += 1;
        if file_rows % IMPORT_PROGRESS_ROWS == 0 {
            eprintln!("[snb-full][update] progress file={} rows={} total_rows={} directed_edges={} elapsed_s={:.1}", path.display(), file_rows, stats.input_rows, stats.directed_edges, started.elapsed().as_secs_f64());
        }
        match parts[2] {
            "1" => apply_add_person(engine.clone(), vertices, edge_props, &parts, stats).await?,
            "2" => {
                apply_binary_update_edge(
                    engine.clone(),
                    edge_props,
                    &parts,
                    VertexLabel::Person,
                    VertexLabel::Post,
                    EdgeLabel::LikesPost,
                    false,
                )
                .await?;
                stats.directed_edges += 2;
            }
            "3" => {
                apply_binary_update_edge(
                    engine.clone(),
                    edge_props,
                    &parts,
                    VertexLabel::Person,
                    VertexLabel::Comment,
                    EdgeLabel::LikesComment,
                    false,
                )
                .await?;
                stats.directed_edges += 2;
            }
            "4" => apply_add_forum(engine.clone(), vertices, edge_props, &parts, stats).await?,
            "5" => {
                apply_binary_update_edge(
                    engine.clone(),
                    edge_props,
                    &parts,
                    VertexLabel::Forum,
                    VertexLabel::Person,
                    EdgeLabel::HasMember,
                    false,
                )
                .await?;
                stats.directed_edges += 2;
            }
            "6" => apply_add_post(engine.clone(), vertices, edge_props, &parts, stats).await?,
            "7" => apply_add_comment(engine.clone(), vertices, edge_props, &parts, stats).await?,
            "8" => {
                apply_binary_update_edge(
                    engine.clone(),
                    edge_props,
                    &parts,
                    VertexLabel::Person,
                    VertexLabel::Person,
                    EdgeLabel::Knows,
                    true,
                )
                .await?;
                stats.directed_edges += 3;
            }
            _ => {}
        }
    }
    eprintln!(
        "[snb-full][update] done file={} rows={} total_rows={} directed_edges={} elapsed_s={:.1}",
        path.display(),
        file_rows,
        stats.input_rows,
        stats.directed_edges,
        started.elapsed().as_secs_f64()
    );
    Ok(())
}

async fn import_edge_file(
    engine: Arc<Engine>,
    edge_props: &mut BufWriter<File>,
    path: &Path,
    src_label: VertexLabel,
    dst_label: VertexLabel,
    edge_label: EdgeLabel,
    bidirectional_positive: bool,
    src_col: ColRef,
    dst_col: ColRef,
    prop: PropSpec,
) -> Result<ImportStats> {
    let started = Instant::now();
    eprintln!(
        "[snb-full][edge] start file={} edge={:?} src={:?} dst={:?}",
        path.display(),
        edge_label,
        src_label,
        dst_label
    );
    let mut rdr = csv::ReaderBuilder::new()
        .delimiter(b'|')
        .has_headers(true)
        .from_path(path)?;
    let headers = rdr.headers()?.clone();
    let mut input_rows = 0u64;
    let mut directed_edges = 0u64;
    for rec in rdr.records() {
        let rec = rec?;
        input_rows += 1;
        let Some(src_ext) = src_col.get(&headers, &rec)? else {
            continue;
        };
        let Some(dst_ext) = dst_col.get(&headers, &rec)? else {
            continue;
        };
        let src = encode_vid(src_label, src_ext);
        let dst = encode_vid(dst_label, dst_ext);
        let prop_value = prop.read(&headers, &rec)?;
        insert_forward_reverse(
            engine.clone(),
            edge_props,
            src,
            dst,
            edge_label,
            prop_value,
            true,
        )
        .await?;
        directed_edges += 2;
        if bidirectional_positive {
            insert_forward_reverse(
                engine.clone(),
                edge_props,
                dst,
                src,
                edge_label,
                prop_value,
                false,
            )
            .await?;
            directed_edges += 1;
        }
        if input_rows % IMPORT_PROGRESS_ROWS == 0 {
            eprintln!(
                "[snb-full][edge] progress file={} rows={} directed_edges={} elapsed_s={:.1}",
                path.display(),
                input_rows,
                directed_edges,
                started.elapsed().as_secs_f64()
            );
        }
    }
    eprintln!(
        "[snb-full][edge] done file={} rows={} directed_edges={} elapsed_s={:.1}",
        path.display(),
        input_rows,
        directed_edges,
        started.elapsed().as_secs_f64()
    );
    Ok(ImportStats {
        input_rows,
        directed_edges,
    })
}

async fn import_named_edge(
    engine: Arc<Engine>,
    edge_props: &mut BufWriter<File>,
    spec: NamedEdgeSpec,
) -> Result<ImportStats> {
    import_edge_file(
        engine,
        edge_props,
        &spec.path,
        spec.src_label,
        spec.dst_label,
        spec.edge_label,
        false,
        ColRef::Name(spec.src_col),
        ColRef::Name(spec.dst_col),
        PropSpec::None,
    )
    .await
}

fn write_edge_prop_file(
    edge_props: &mut BufWriter<File>,
    path: &Path,
    src_label: VertexLabel,
    dst_label: VertexLabel,
    edge_label: EdgeLabel,
    bidirectional_positive: bool,
    src_col: ColRef,
    dst_col: ColRef,
    prop: PropSpec,
) -> Result<u64> {
    let mut rdr = csv::ReaderBuilder::new()
        .delimiter(b'|')
        .has_headers(true)
        .from_path(path)?;
    let headers = rdr.headers()?.clone();
    let mut input_rows = 0u64;
    for rec in rdr.records() {
        let rec = rec?;
        input_rows += 1;
        let Some(src_ext) = src_col.get(&headers, &rec)? else {
            continue;
        };
        let Some(dst_ext) = dst_col.get(&headers, &rec)? else {
            continue;
        };
        let src = encode_vid(src_label, src_ext);
        let dst = encode_vid(dst_label, dst_ext);
        let prop_value = prop.read(&headers, &rec)?;
        let label = edge_label.as_i32();
        write_edge_prop(edge_props, src, dst, label, prop_value)?;
        write_edge_prop(edge_props, dst, src, -label, prop_value)?;
        if bidirectional_positive {
            write_edge_prop(edge_props, dst, src, label, prop_value)?;
        }
    }
    Ok(input_rows)
}

async fn apply_add_person(
    engine: Arc<Engine>,
    vertices: &mut BufWriter<File>,
    edge_props: &mut BufWriter<File>,
    p: &[&str],
    stats: &mut ImportStats,
) -> Result<()> {
    let id = parse_field_i64(p, 3)?;
    let vid = encode_vid(VertexLabel::Person, id);
    write_vertex(
        vertices,
        vid,
        VertexData::Person(PersonProps {
            id,
            first_name: field(p, 4).to_string(),
            last_name: field(p, 5).to_string(),
            gender: field(p, 6).to_string(),
            birthday: parse_field_i64(p, 7)?,
            creation_date: parse_field_i64(p, 8)?,
            location_ip: field(p, 9).to_string(),
            browser_used: field(p, 10).to_string(),
            place: parse_field_i64(p, 11)?,
            language: field(p, 12).to_string(),
            email: field(p, 13).to_string(),
        }),
    )?;
    insert_forward_reverse(
        engine.clone(),
        edge_props,
        vid,
        encode_vid(VertexLabel::Place, parse_field_i64(p, 11)?),
        EdgeLabel::IsLocatedIn,
        EdgeProp::Empty,
        true,
    )
    .await?;
    stats.directed_edges += 2;
    for tag in parse_id_list(field(p, 14)) {
        insert_forward_reverse(
            engine.clone(),
            edge_props,
            vid,
            encode_vid(VertexLabel::Tag, tag),
            EdgeLabel::HasInterest,
            EdgeProp::Empty,
            true,
        )
        .await?;
        stats.directed_edges += 2;
    }
    for (org, year) in parse_org_year_list(field(p, 15)) {
        insert_forward_reverse(
            engine.clone(),
            edge_props,
            vid,
            encode_vid(VertexLabel::Organisation, org),
            EdgeLabel::StudyAt,
            EdgeProp::I32(year),
            true,
        )
        .await?;
        stats.directed_edges += 2;
    }
    for (org, year) in parse_org_year_list(field(p, 16)) {
        insert_forward_reverse(
            engine.clone(),
            edge_props,
            vid,
            encode_vid(VertexLabel::Organisation, org),
            EdgeLabel::WorkAt,
            EdgeProp::I32(year),
            true,
        )
        .await?;
        stats.directed_edges += 2;
    }
    Ok(())
}

async fn apply_add_forum(
    engine: Arc<Engine>,
    vertices: &mut BufWriter<File>,
    edge_props: &mut BufWriter<File>,
    p: &[&str],
    stats: &mut ImportStats,
) -> Result<()> {
    let id = parse_field_i64(p, 3)?;
    let vid = encode_vid(VertexLabel::Forum, id);
    write_vertex(
        vertices,
        vid,
        VertexData::Forum(ForumProps {
            id,
            title: field(p, 4).to_string(),
            creation_date: parse_field_i64(p, 5)?,
            moderator: parse_field_i64(p, 6)?,
        }),
    )?;
    insert_forward_reverse(
        engine.clone(),
        edge_props,
        vid,
        encode_vid(VertexLabel::Person, parse_field_i64(p, 6)?),
        EdgeLabel::HasModerator,
        EdgeProp::Empty,
        true,
    )
    .await?;
    stats.directed_edges += 2;
    for tag in parse_id_list(field(p, 7)) {
        insert_forward_reverse(
            engine.clone(),
            edge_props,
            vid,
            encode_vid(VertexLabel::Tag, tag),
            EdgeLabel::HasTag,
            EdgeProp::Empty,
            true,
        )
        .await?;
        stats.directed_edges += 2;
    }
    Ok(())
}

async fn apply_add_post(
    engine: Arc<Engine>,
    vertices: &mut BufWriter<File>,
    edge_props: &mut BufWriter<File>,
    p: &[&str],
    stats: &mut ImportStats,
) -> Result<()> {
    let id = parse_field_i64(p, 3)?;
    let vid = encode_vid(VertexLabel::Post, id);
    write_vertex(
        vertices,
        vid,
        VertexData::Post(PostProps {
            id,
            image_file: field(p, 4).to_string(),
            creation_date: parse_field_i64(p, 5)?,
            location_ip: field(p, 6).to_string(),
            browser_used: field(p, 7).to_string(),
            language: field(p, 8).to_string(),
            content: field(p, 9).to_string(),
            length: parse_field_i64(p, 10)?,
            creator: parse_field_i64(p, 11)?,
            forum_id: parse_field_i64(p, 12)?,
            place: parse_field_i64(p, 13)?,
        }),
    )?;
    insert_forward_reverse(
        engine.clone(),
        edge_props,
        vid,
        encode_vid(VertexLabel::Person, parse_field_i64(p, 11)?),
        EdgeLabel::HasCreator,
        EdgeProp::Empty,
        true,
    )
    .await?;
    stats.directed_edges += 2;
    insert_forward_reverse(
        engine.clone(),
        edge_props,
        encode_vid(VertexLabel::Forum, parse_field_i64(p, 12)?),
        vid,
        EdgeLabel::ContainerOf,
        EdgeProp::Empty,
        true,
    )
    .await?;
    stats.directed_edges += 2;
    insert_forward_reverse(
        engine.clone(),
        edge_props,
        vid,
        encode_vid(VertexLabel::Place, parse_field_i64(p, 13)?),
        EdgeLabel::IsLocatedIn,
        EdgeProp::Empty,
        true,
    )
    .await?;
    stats.directed_edges += 2;
    for tag in parse_id_list(field(p, 14)) {
        insert_forward_reverse(
            engine.clone(),
            edge_props,
            vid,
            encode_vid(VertexLabel::Tag, tag),
            EdgeLabel::HasTag,
            EdgeProp::Empty,
            true,
        )
        .await?;
        stats.directed_edges += 2;
    }
    Ok(())
}

async fn apply_add_comment(
    engine: Arc<Engine>,
    vertices: &mut BufWriter<File>,
    edge_props: &mut BufWriter<File>,
    p: &[&str],
    stats: &mut ImportStats,
) -> Result<()> {
    let id = parse_field_i64(p, 3)?;
    let vid = encode_vid(VertexLabel::Comment, id);
    let reply_of_post = parse_optional_i64(field(p, 11))?;
    let reply_of_comment = parse_optional_i64(field(p, 12))?;
    write_vertex(
        vertices,
        vid,
        VertexData::Comment(CommentProps {
            id,
            creation_date: parse_field_i64(p, 4)?,
            location_ip: field(p, 5).to_string(),
            browser_used: field(p, 6).to_string(),
            content: field(p, 7).to_string(),
            length: parse_field_i64(p, 8)?,
            creator: parse_field_i64(p, 9)?,
            place: parse_field_i64(p, 10)?,
            reply_of_post,
            reply_of_comment,
        }),
    )?;
    insert_forward_reverse(
        engine.clone(),
        edge_props,
        vid,
        encode_vid(VertexLabel::Person, parse_field_i64(p, 9)?),
        EdgeLabel::HasCreator,
        EdgeProp::Empty,
        true,
    )
    .await?;
    stats.directed_edges += 2;
    insert_forward_reverse(
        engine.clone(),
        edge_props,
        vid,
        encode_vid(VertexLabel::Place, parse_field_i64(p, 10)?),
        EdgeLabel::IsLocatedIn,
        EdgeProp::Empty,
        true,
    )
    .await?;
    stats.directed_edges += 2;
    if let Some(post) = reply_of_post {
        insert_forward_reverse(
            engine.clone(),
            edge_props,
            vid,
            encode_vid(VertexLabel::Post, post),
            EdgeLabel::ReplyOfPost,
            EdgeProp::Empty,
            true,
        )
        .await?;
        stats.directed_edges += 2;
    }
    if let Some(comment) = reply_of_comment {
        insert_forward_reverse(
            engine.clone(),
            edge_props,
            vid,
            encode_vid(VertexLabel::Comment, comment),
            EdgeLabel::ReplyOfComment,
            EdgeProp::Empty,
            true,
        )
        .await?;
        stats.directed_edges += 2;
    }
    for tag in parse_id_list(field(p, 13)) {
        insert_forward_reverse(
            engine.clone(),
            edge_props,
            vid,
            encode_vid(VertexLabel::Tag, tag),
            EdgeLabel::HasTag,
            EdgeProp::Empty,
            true,
        )
        .await?;
        stats.directed_edges += 2;
    }
    Ok(())
}

async fn apply_binary_update_edge(
    engine: Arc<Engine>,
    edge_props: &mut BufWriter<File>,
    p: &[&str],
    src_label: VertexLabel,
    dst_label: VertexLabel,
    edge_label: EdgeLabel,
    bidirectional_positive: bool,
) -> Result<()> {
    let src = encode_vid(src_label, parse_field_i64(p, 3)?);
    let dst = encode_vid(dst_label, parse_field_i64(p, 4)?);
    let prop = EdgeProp::I64(parse_field_i64(p, 5)?);
    insert_forward_reverse(engine.clone(), edge_props, src, dst, edge_label, prop, true).await?;
    if bidirectional_positive {
        insert_forward_reverse(engine, edge_props, dst, src, edge_label, prop, false).await?;
    }
    Ok(())
}

async fn insert_forward_reverse(
    engine: Arc<Engine>,
    edge_props: &mut BufWriter<File>,
    src: u64,
    dst: u64,
    edge_label: EdgeLabel,
    prop: EdgeProp,
    include_reverse: bool,
) -> Result<()> {
    let label = edge_label.as_i32();
    engine.insert_edge(src, dst, label).await?;
    write_edge_prop(edge_props, src, dst, label, prop)?;
    if include_reverse {
        engine.insert_edge(dst, src, -label).await?;
        write_edge_prop(edge_props, dst, src, -label, prop)?;
    }
    Ok(())
}

fn write_edge_prop(
    edge_props: &mut BufWriter<File>,
    src: u64,
    dst: u64,
    edge_type: i32,
    prop: EdgeProp,
) -> Result<()> {
    if matches!(prop, EdgeProp::Empty) {
        return Ok(());
    }
    serde_json::to_writer(
        edge_props.by_ref(),
        &EdgePropRow {
            src,
            dst,
            edge_type,
            prop,
        },
    )?;
    edge_props.write_all(b"\n")?;
    Ok(())
}

struct NamedEdgeSpec {
    path: std::path::PathBuf,
    src_label: VertexLabel,
    dst_label: VertexLabel,
    edge_label: EdgeLabel,
    src_col: &'static str,
    dst_col: &'static str,
}

impl NamedEdgeSpec {
    fn new(
        path: std::path::PathBuf,
        src_label: VertexLabel,
        dst_label: VertexLabel,
        edge_label: EdgeLabel,
        src_col: &'static str,
        dst_col: &'static str,
    ) -> Self {
        Self {
            path,
            src_label,
            dst_label,
            edge_label,
            src_col,
            dst_col,
        }
    }
}

#[derive(Clone, Copy)]
enum ColRef {
    Index(usize),
    Name(&'static str),
}

impl ColRef {
    fn get(&self, headers: &StringRecord, rec: &StringRecord) -> Result<Option<i64>> {
        let raw = match self {
            Self::Index(i) => rec.get(*i).unwrap_or(""),
            Self::Name(name) => get(headers, rec, name),
        };
        parse_optional_i64(raw)
    }
}

#[derive(Clone, Copy)]
enum PropSpec {
    None,
    I32Col(usize),
    I64Col(usize),
}

impl PropSpec {
    fn read(&self, _headers: &StringRecord, rec: &StringRecord) -> Result<EdgeProp> {
        Ok(match self {
            Self::None => EdgeProp::Empty,
            Self::I32Col(i) => EdgeProp::I32(rec[*i].parse()?),
            Self::I64Col(i) => EdgeProp::I64(rec[*i].parse()?),
        })
    }
}

fn get<'a>(headers: &StringRecord, rec: &'a StringRecord, name: &str) -> &'a str {
    headers
        .iter()
        .position(|h| h == name)
        .and_then(|idx| rec.get(idx))
        .unwrap_or("")
}

fn get_i64(headers: &StringRecord, rec: &StringRecord, name: &str) -> Result<i64> {
    Ok(get(headers, rec, name).parse()?)
}

fn parse_optional_i64(raw: &str) -> Result<Option<i64>> {
    let raw = raw.trim();
    if raw.is_empty() || raw == "-1" {
        Ok(None)
    } else {
        Ok(Some(raw.parse()?))
    }
}

fn update_stream_files(csv_root: &Path) -> Result<Vec<std::path::PathBuf>> {
    let mut out = Vec::new();
    for entry in fs::read_dir(csv_root)? {
        let entry = entry?;
        let path = entry.path();
        let Some(name) = path.file_name().and_then(|n| n.to_str()) else {
            continue;
        };
        if name.starts_with("updateStream_0_")
            && (name.ends_with("_person.csv") || name.ends_with("_forum.csv"))
        {
            out.push(path);
        }
    }
    Ok(out)
}

fn write_vertex(out: &mut BufWriter<File>, vid: u64, data: VertexData) -> Result<()> {
    serde_json::to_writer(out.by_ref(), &VertexRow { vid, data })?;
    out.write_all(b"\n")?;
    Ok(())
}

fn field<'a>(parts: &'a [&str], idx: usize) -> &'a str {
    parts.get(idx).copied().unwrap_or("")
}

fn parse_field_i64(parts: &[&str], idx: usize) -> Result<i64> {
    Ok(field(parts, idx).parse()?)
}

fn parse_id_list(raw: &str) -> Vec<i64> {
    raw.split(';')
        .filter_map(|s| {
            let s = s.trim();
            (!s.is_empty() && s != "-1")
                .then(|| s.parse::<i64>().ok())
                .flatten()
        })
        .collect()
}

fn parse_org_year_list(raw: &str) -> Vec<(i64, i32)> {
    raw.split(';')
        .filter_map(|item| {
            let (org, year) = item.trim().split_once(',')?;
            Some((org.parse().ok()?, year.parse().ok()?))
        })
        .collect()
}
