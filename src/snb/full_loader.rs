use std::fs::{self, File};
use std::io::{BufWriter, Write};
use std::path::Path;
use std::sync::Arc;

use csv::StringRecord;

use crate::error::Result;
use crate::graph::Engine;
use crate::loader::ImportStats;
use crate::snb::props::{
    encode_vid, CommentProps, EdgeProp, EdgePropRow, ForumProps, OrgProps, PersonProps, PlaceProps,
    PostProps, SnbGraph, TagClassProps, TagProps, VertexData, VertexRow,
};
use crate::types::{EdgeLabel, VertexLabel};

pub async fn import_snb_full(
    engine: Arc<Engine>,
    csv_root: &Path,
    store_dir: &Path,
) -> Result<ImportStats> {
    fs::create_dir_all(store_dir)?;
    let vertex_path = store_dir.join("snb_vertices.jsonl");
    let edge_prop_path = store_dir.join("snb_edge_props.jsonl");
    let mut vertices = BufWriter::new(File::create(&vertex_path)?);
    let mut edge_props = BufWriter::new(File::create(&edge_prop_path)?);
    let mut stats = ImportStats {
        input_rows: 0,
        directed_edges: 0,
    };

    import_vertices(csv_root, &mut vertices)?;
    vertices.flush()?;

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
            PropSpec::None,
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
    let mut rdr = csv::ReaderBuilder::new()
        .delimiter(b'|')
        .has_headers(true)
        .from_path(path)?;
    let headers = rdr.headers()?.clone();
    for rec in rdr.records() {
        let rec = rec?;
        let data = build(&headers, &rec)?;
        let vid = encode_vid(label, data.external_id());
        serde_json::to_writer(out.by_ref(), &VertexRow { vid, data })?;
        out.write_all(b"\n")?;
    }
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
    }
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
