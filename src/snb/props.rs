use std::collections::{BTreeSet, HashMap};
use std::fs::{self, File};
use std::future::Future;
use std::io::{BufRead, BufReader, BufWriter, Read, Write};
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::Instant;

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::base_graph::column::StringColumnReader;
use crate::base_graph::ids::LabelId;
use crate::base_graph::{BaseGraph, IoConfig};
use crate::config::IoBackendKind;
use crate::delta::DeltaGraph;
use crate::dynamic_view::DynamicGraphView;
use crate::error::Result;
use crate::graph::Engine;
use crate::types::{EdgeLabel, EdgeMarker, EdgeRecord, EdgeType, Timestamp, VertexId, VertexLabel};

const LABEL_SHIFT: u64 = 56;
const EXTERNAL_MASK: u64 = (1u64 << LABEL_SHIFT) - 1;

pub fn encode_vid(label: VertexLabel, external_id: i64) -> VertexId {
    ((label as u64) << LABEL_SHIFT) | (external_id as u64 & EXTERNAL_MASK)
}

pub fn external_id(vid: VertexId) -> i64 {
    (vid & EXTERNAL_MASK) as i64
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PersonProps {
    pub id: i64,
    pub first_name: String,
    pub last_name: String,
    pub gender: String,
    pub birthday: i64,
    pub creation_date: i64,
    pub location_ip: String,
    pub browser_used: String,
    pub place: i64,
    pub language: String,
    pub email: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PlaceProps {
    pub id: i64,
    pub name: String,
    pub place_type: String,
    pub is_part_of: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrgProps {
    pub id: i64,
    pub org_type: String,
    pub name: String,
    pub place: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PostProps {
    pub id: i64,
    pub image_file: String,
    pub creation_date: i64,
    pub location_ip: String,
    pub browser_used: String,
    pub language: String,
    pub content: String,
    pub length: i64,
    pub creator: i64,
    pub forum_id: i64,
    pub place: i64,
}

impl PostProps {
    pub fn content_or_image(&self) -> &str {
        if self.content.is_empty() {
            &self.image_file
        } else {
            &self.content
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CommentProps {
    pub id: i64,
    pub creation_date: i64,
    pub location_ip: String,
    pub browser_used: String,
    pub content: String,
    pub length: i64,
    pub creator: i64,
    pub place: i64,
    pub reply_of_post: Option<i64>,
    pub reply_of_comment: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ForumProps {
    pub id: i64,
    pub title: String,
    pub creation_date: i64,
    pub moderator: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TagProps {
    pub id: i64,
    pub name: String,
    pub has_type: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TagClassProps {
    pub id: i64,
    pub name: String,
    pub is_subclass_of: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind")]
pub enum VertexData {
    Person(PersonProps),
    Place(PlaceProps),
    Organisation(OrgProps),
    Post(PostProps),
    Comment(CommentProps),
    Forum(ForumProps),
    Tag(TagProps),
    TagClass(TagClassProps),
}

impl VertexData {
    pub fn label(&self) -> VertexLabel {
        match self {
            Self::Person(_) => VertexLabel::Person,
            Self::Place(_) => VertexLabel::Place,
            Self::Organisation(_) => VertexLabel::Organisation,
            Self::Post(_) => VertexLabel::Post,
            Self::Comment(_) => VertexLabel::Comment,
            Self::Forum(_) => VertexLabel::Forum,
            Self::Tag(_) => VertexLabel::Tag,
            Self::TagClass(_) => VertexLabel::TagClass,
        }
    }

    pub fn external_id(&self) -> i64 {
        match self {
            Self::Person(p) => p.id,
            Self::Place(p) => p.id,
            Self::Organisation(p) => p.id,
            Self::Post(p) => p.id,
            Self::Comment(p) => p.id,
            Self::Forum(p) => p.id,
            Self::Tag(p) => p.id,
            Self::TagClass(p) => p.id,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VertexRow {
    pub vid: VertexId,
    pub data: VertexData,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub enum EdgeProp {
    Empty,
    I32(i32),
    I64(i64),
}

impl EdgeProp {
    pub fn as_i32(self) -> i32 {
        match self {
            Self::I32(v) => v,
            Self::I64(v) => v as i32,
            Self::Empty => 0,
        }
    }

    pub fn as_i64(self) -> i64 {
        match self {
            Self::I32(v) => v as i64,
            Self::I64(v) => v,
            Self::Empty => 0,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MessageRef {
    pub vid: VertexId,
    pub id: i64,
    pub creation_date: i64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ReplyRef {
    pub reply_vid: VertexId,
    pub comment_id: i64,
    pub creation_date: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EdgePropRow {
    pub src: VertexId,
    pub dst: VertexId,
    pub edge_type: EdgeType,
    pub prop: EdgeProp,
}

pub struct LegacySnbGraph {
    pub engine: Arc<Engine>,
    pub snapshot: u64,
    vertices: HashMap<VertexId, VertexData>,
    edge_props: HashMap<(VertexId, EdgeType, VertexId), EdgeProp>,
    adjacency: HashMap<(VertexId, EdgeType), Vec<VertexId>>,
}

impl LegacySnbGraph {
    pub async fn open(engine: Arc<Engine>, store_dir: &Path) -> Result<Self> {
        let vertices = load_vertices(&store_dir.join("snb_vertices.jsonl"))?;
        let edge_props = load_edge_props(&store_dir.join("snb_edge_props.jsonl"))?;
        let snapshot = engine.current_snapshot();
        let cache_path = store_dir.join("snb_adjacency.bin");
        let adjacency = if cache_path.exists() {
            load_adjacency_cache(&cache_path)?
        } else {
            let adjacency = build_adjacency_from_edges(engine.scan_edges(snapshot).await?);
            write_adjacency_cache_atomic(&cache_path, &adjacency)?;
            adjacency
        };
        Ok(Self {
            engine,
            snapshot,
            vertices,
            edge_props,
            adjacency,
        })
    }

    pub fn lookup(&self, label: VertexLabel, external_id: i64) -> VertexId {
        encode_vid(label, external_id)
    }

    pub fn vertex(&self, vid: VertexId) -> Option<&VertexData> {
        self.vertices.get(&vid)
    }

    pub fn insert_vertex(&mut self, data: VertexData) {
        let vid = encode_vid(data.label(), data.external_id());
        self.vertices.insert(vid, data);
    }

    pub fn insert_edge_cached(
        &mut self,
        src: VertexId,
        dst: VertexId,
        edge_label: EdgeLabel,
        prop: EdgeProp,
        include_reverse: bool,
    ) {
        let edge_type = edge_label.as_i32();
        self.insert_edge_by_type(src, dst, edge_type, prop);
        if include_reverse {
            self.insert_edge_by_type(dst, src, -edge_type, prop);
        }
    }

    pub fn insert_edge_by_type(
        &mut self,
        src: VertexId,
        dst: VertexId,
        edge_type: EdgeType,
        prop: EdgeProp,
    ) {
        let dsts = self.adjacency.entry((src, edge_type)).or_default();
        if !dsts.contains(&dst) {
            dsts.push(dst);
        }
        if !matches!(prop, EdgeProp::Empty) {
            self.edge_props.insert((src, edge_type, dst), prop);
        }
    }

    pub fn person(&self, vid: VertexId) -> Option<&PersonProps> {
        match self.vertices.get(&vid)? {
            VertexData::Person(p) => Some(p),
            _ => None,
        }
    }

    pub fn place(&self, vid: VertexId) -> Option<&PlaceProps> {
        match self.vertices.get(&vid)? {
            VertexData::Place(p) => Some(p),
            _ => None,
        }
    }

    pub fn organisation(&self, vid: VertexId) -> Option<&OrgProps> {
        match self.vertices.get(&vid)? {
            VertexData::Organisation(p) => Some(p),
            _ => None,
        }
    }

    pub fn post(&self, vid: VertexId) -> Option<&PostProps> {
        match self.vertices.get(&vid)? {
            VertexData::Post(p) => Some(p),
            _ => None,
        }
    }

    pub fn comment(&self, vid: VertexId) -> Option<&CommentProps> {
        match self.vertices.get(&vid)? {
            VertexData::Comment(p) => Some(p),
            _ => None,
        }
    }

    pub fn forum(&self, vid: VertexId) -> Option<&ForumProps> {
        match self.vertices.get(&vid)? {
            VertexData::Forum(p) => Some(p),
            _ => None,
        }
    }

    pub fn tag(&self, vid: VertexId) -> Option<&TagProps> {
        match self.vertices.get(&vid)? {
            VertexData::Tag(p) => Some(p),
            _ => None,
        }
    }

    pub fn tag_class(&self, vid: VertexId) -> Option<&TagClassProps> {
        match self.vertices.get(&vid)? {
            VertexData::TagClass(p) => Some(p),
            _ => None,
        }
    }

    pub fn vertex_label(&self, vid: VertexId) -> Option<VertexLabel> {
        self.vertex(vid).map(VertexData::label)
    }

    pub fn vertices_by_label(&self, label: VertexLabel) -> impl Iterator<Item = VertexId> + '_ {
        self.vertices.iter().filter_map(move |(&vid, data)| {
            if data.label() == label {
                Some(vid)
            } else {
                None
            }
        })
    }

    pub fn place_name(&self, vid: VertexId) -> String {
        self.place(vid).map(|p| p.name.clone()).unwrap_or_default()
    }

    pub fn post_content_or_image(&self, vid: VertexId) -> Option<String> {
        self.post(vid)
            .map(|post| post.content_or_image().to_string())
    }

    pub fn comment_content(&self, vid: VertexId) -> Option<String> {
        self.comment(vid).map(|comment| comment.content.clone())
    }

    pub fn message_content(&self, vid: VertexId) -> Option<String> {
        self.comment_content(vid)
            .or_else(|| self.post_content_or_image(vid))
    }

    pub fn out_neighbors_cached(&self, vid: VertexId, edge_label: EdgeLabel) -> Vec<VertexId> {
        self.adjacency
            .get(&(vid, edge_label.as_i32()))
            .cloned()
            .unwrap_or_default()
    }

    pub fn in_neighbors_cached(&self, vid: VertexId, edge_label: EdgeLabel) -> Vec<VertexId> {
        self.adjacency
            .get(&(vid, -edge_label.as_i32()))
            .cloned()
            .unwrap_or_default()
    }

    pub fn for_each_out_neighbor<F>(&self, vid: VertexId, edge_label: EdgeLabel, mut f: F)
    where
        F: FnMut(VertexId),
    {
        if let Some(dsts) = self.adjacency.get(&(vid, edge_label.as_i32())) {
            for &dst in dsts {
                f(dst);
            }
        }
    }

    pub fn for_each_in_neighbor<F>(&self, vid: VertexId, edge_label: EdgeLabel, mut f: F)
    where
        F: FnMut(VertexId),
    {
        if let Some(srcs) = self.adjacency.get(&(vid, -edge_label.as_i32())) {
            for &src in srcs {
                f(src);
            }
        }
    }

    pub fn for_each_message_ref_by_creator_date<F>(&self, person_vid: VertexId, mut f: F)
    where
        F: FnMut(MessageRef),
    {
        let mut messages = Vec::new();
        self.for_each_in_neighbor(person_vid, EdgeLabel::HasCreator, |vid| {
            if matches!(
                self.vertex_label(vid),
                Some(VertexLabel::Post | VertexLabel::Comment)
            ) {
                let message = match self.vertex(vid) {
                    Some(VertexData::Post(post)) => Some(MessageRef {
                        vid,
                        id: post.id,
                        creation_date: post.creation_date,
                    }),
                    Some(VertexData::Comment(comment)) => Some(MessageRef {
                        vid,
                        id: comment.id,
                        creation_date: comment.creation_date,
                    }),
                    _ => None,
                };
                if let Some(message) = message {
                    messages.push(message);
                }
            }
        });
        sort_message_refs(&mut messages);
        for message in messages {
            f(message);
        }
    }

    pub fn for_each_message_ref_by_creator_date_range<F>(
        &self,
        person_vid: VertexId,
        start_date: i64,
        end_date: i64,
        mut f: F,
    ) where
        F: FnMut(MessageRef),
    {
        self.for_each_message_ref_by_creator_date(person_vid, |message| {
            if message.creation_date >= start_date && message.creation_date < end_date {
                f(message);
            }
        });
    }

    pub fn for_each_reply_ref_by_parent_creator_date<F>(&self, person_vid: VertexId, mut f: F)
    where
        F: FnMut(ReplyRef),
    {
        let mut replies = Vec::new();
        self.for_each_message_ref_by_creator_date(person_vid, |message| {
            self.for_each_in_neighbor(message.vid, EdgeLabel::ReplyOfPost, |reply_vid| {
                if let Some(comment) = self.comment(reply_vid) {
                    replies.push(ReplyRef {
                        reply_vid,
                        comment_id: comment.id,
                        creation_date: comment.creation_date,
                    });
                }
            });
            self.for_each_in_neighbor(message.vid, EdgeLabel::ReplyOfComment, |reply_vid| {
                if let Some(comment) = self.comment(reply_vid) {
                    replies.push(ReplyRef {
                        reply_vid,
                        comment_id: comment.id,
                        creation_date: comment.creation_date,
                    });
                }
            });
        });
        sort_reply_refs(&mut replies);
        for reply in replies {
            f(reply);
        }
    }

    pub fn for_each_top_reply_ref_by_parent_creator_date<F>(
        &self,
        person_vid: VertexId,
        limit: usize,
        mut f: F,
    ) where
        F: FnMut(ReplyRef),
    {
        let mut emitted = 0usize;
        self.for_each_reply_ref_by_parent_creator_date(person_vid, |reply| {
            if emitted < limit {
                emitted += 1;
                f(reply);
            }
        });
    }

    pub fn for_each_tag_with_type<F>(&self, tag_class_vid: VertexId, mut f: F)
    where
        F: FnMut(VertexId),
    {
        let mut emitted = false;
        self.for_each_in_neighbor(tag_class_vid, EdgeLabel::HasType, |tag_vid| {
            if self.vertex_label(tag_vid) == Some(VertexLabel::Tag) {
                emitted = true;
                f(tag_vid);
            }
        });
        if emitted {
            return;
        }
        let tag_class_id = external_id(tag_class_vid);
        for vid in self.vertices_by_label(VertexLabel::Tag) {
            if self.tag(vid).map(|tag| tag.has_type) == Some(tag_class_id) {
                f(vid);
            }
        }
    }

    pub async fn out_neighbors(
        &self,
        vid: VertexId,
        edge_label: EdgeLabel,
    ) -> Result<Vec<VertexId>> {
        Ok(self.out_neighbors_cached(vid, edge_label))
    }

    pub async fn in_neighbors(
        &self,
        vid: VertexId,
        edge_label: EdgeLabel,
    ) -> Result<Vec<VertexId>> {
        Ok(self.in_neighbors_cached(vid, edge_label))
    }

    pub async fn knows_neighbors(&self, vid: VertexId) -> Result<Vec<VertexId>> {
        self.out_neighbors(vid, EdgeLabel::Knows).await
    }

    pub async fn edge_prop(&self, src: VertexId, edge_label: EdgeLabel, dst: VertexId) -> EdgeProp {
        self.edge_prop_by_type(src, edge_label.as_i32(), dst)
    }

    pub fn edge_prop_by_type(&self, src: VertexId, edge_type: EdgeType, dst: VertexId) -> EdgeProp {
        self.edge_props
            .get(&(src, edge_type, dst))
            .copied()
            .unwrap_or(EdgeProp::Empty)
    }

    pub fn out_edges_with_prop(
        &self,
        vid: VertexId,
        edge_label: EdgeLabel,
    ) -> Vec<(VertexId, EdgeProp)> {
        let edge_type = edge_label.as_i32();
        self.out_neighbors_cached(vid, edge_label)
            .into_iter()
            .map(|dst| (dst, self.edge_prop_by_type(vid, edge_type, dst)))
            .collect()
    }

    pub fn in_edges_with_prop(
        &self,
        vid: VertexId,
        edge_label: EdgeLabel,
    ) -> Vec<(VertexId, EdgeProp)> {
        let edge_type = -edge_label.as_i32();
        self.in_neighbors_cached(vid, edge_label)
            .into_iter()
            .map(|src| (src, self.edge_prop_by_type(vid, edge_type, src)))
            .collect()
    }

    pub async fn build_adjacency_cache(engine: Arc<Engine>, store_dir: &Path) -> Result<usize> {
        let started = Instant::now();
        let snapshot = engine.current_snapshot();
        eprintln!("[snb-cache] scanning edges snapshot={snapshot}");
        let edges = engine.scan_edges(snapshot).await?;
        eprintln!(
            "[snb-cache] scanned edges={} elapsed_s={:.1}",
            edges.len(),
            started.elapsed().as_secs_f64()
        );
        let adjacency = build_adjacency_from_edges(edges);
        let count = adjacency.len();
        eprintln!(
            "[snb-cache] writing adjacency cache groups={} path={}",
            count,
            store_dir.join("snb_adjacency.bin").display()
        );
        let write_stats =
            write_adjacency_cache_atomic(&store_dir.join("snb_adjacency.bin"), &adjacency)?;
        eprintln!(
            "[snb-cache] write complete groups={} edges={} group_bytes={} dst_bytes={} write_elapsed_s={:.1} sync_elapsed_s={:.1} total_elapsed_s={:.1}",
            write_stats.group_count,
            write_stats.edge_count,
            write_stats.encoded_group_bytes,
            write_stats.encoded_dst_bytes,
            write_stats.write_elapsed_s,
            write_stats.sync_elapsed_s,
            started.elapsed().as_secs_f64()
        );
        Ok(count)
    }
}

pub struct DynamicSnbGraph {
    view: DynamicGraphView,
    base_vertices: Option<BaseSnbProperties>,
    vertices: HashMap<VertexId, VertexData>,
    edge_props: HashMap<(VertexId, EdgeType, VertexId), EdgeProp>,
    lookup_indexes: SnbLookupIndexes,
    delta_messages_by_creator: HashMap<VertexId, Vec<MessageRef>>,
    delta_replies_by_parent_creator: HashMap<VertexId, Vec<ReplyRef>>,
    /// Instrumentation: counts neighbor lookups not served by BaseGraph CSR
    /// routing (`base_neighbors_by_type` returned `None`), keyed by
    /// (src_label, edge_type). Resolved as empty + DeltaGraph merge.
    fallback_hits: Mutex<HashMap<(i32, EdgeType), u64>>,
}

struct BaseSnbProperties {
    persons: Vec<PersonProps>,
    places: Vec<PlaceProps>,
    organisations: Vec<OrgProps>,
    // Posts/Comments are kept as compact columns (SoA) rather than AoS
    // `Vec<PostProps>`/`Vec<CommentProps>`. Their string fields (image_file,
    // content, location_ip, browser_used, language) are always served lazily
    // from `message_strings`, so an AoS row only stored empty `String` stubs
    // (~24B/field/row of pure waste at SF30+ message counts). `post()`/
    // `comment()` reconstruct the public struct on demand with empty strings.
    post_creation_date: Vec<i64>,
    post_length: Vec<i64>,
    post_creator: Vec<i64>,
    post_forum: Vec<i64>,
    post_place: Vec<i64>,
    comment_creation_date: Vec<i64>,
    comment_length: Vec<i64>,
    comment_creator: Vec<i64>,
    comment_place: Vec<i64>,
    // Raw reply-parent ids with `i64::MIN` sentinel for "none" (see optional_i64).
    comment_reply_post: Vec<i64>,
    comment_reply_comment: Vec<i64>,
    forums: Vec<ForumProps>,
    tags: Vec<TagProps>,
    tag_classes: Vec<TagClassProps>,
    message_strings: BaseMessageStringColumns,
    messages_by_creator: Vec<Vec<MessageRef>>,
    replies_by_parent_creator: Vec<Vec<ReplyRef>>,
    persons_by_place: Vec<Vec<VertexId>>,
    posts_by_place: Vec<Vec<VertexId>>,
    comments_by_place: Vec<Vec<VertexId>>,
    organisations_by_place: Vec<Vec<VertexId>>,
    tags_by_tag_class: Vec<Vec<VertexId>>,
}

struct BaseMessageStringColumns {
    post_image_file: StringColumnReader,
    post_content: StringColumnReader,
    comment_content: StringColumnReader,
}

#[derive(Default)]
struct SnbLookupIndexes {
    country_by_name: HashMap<String, VertexId>,
    tag_by_name: HashMap<String, VertexId>,
    tag_class_by_name: HashMap<String, VertexId>,
}

impl DynamicSnbGraph {
    pub async fn open(
        store_dir: &Path,
        base_io: IoConfig,
        io_backend: IoBackendKind,
    ) -> Result<Self> {
        let started = Instant::now();
        eprintln!(
            "[dynamic-snb] opening base/delta view store_dir={}",
            store_dir.display()
        );
        let view_started = Instant::now();
        let view = DynamicGraphView::open_or_create_delta(
            store_dir,
            base_io,
            io_backend,
            256 * 1024 * 1024,
        )
        .await?;
        eprintln!(
            "[dynamic-snb] base/delta view ready elapsed_s={:.1}",
            view_started.elapsed().as_secs_f64()
        );
        let vertices_started = Instant::now();
        let (base_vertices, vertices) =
            if let Some(base) = view.base().filter(|base| base.has_vertex_properties()) {
                eprintln!("[dynamic-snb] loading vertex properties from BaseGraph columns");
                let base_vertices = load_vertices_from_base_graph(base)?;
                eprintln!(
                    "[dynamic-snb] BaseGraph vertex properties ready vertices={} elapsed_s={:.1}",
                    base_vertices.len(),
                    vertices_started.elapsed().as_secs_f64()
                );
                (Some(base_vertices), HashMap::new())
            } else {
                eprintln!("[dynamic-snb] loading vertex property sidecar");
                let vertices = load_vertices(&store_dir.join("snb_vertices.jsonl"))?;
                eprintln!(
                    "[dynamic-snb] vertex property sidecar ready vertices={} elapsed_s={:.1}",
                    vertices.len(),
                    vertices_started.elapsed().as_secs_f64()
                );
                (None, vertices)
            };
        let edge_props_started = Instant::now();
        let edge_props = if view
            .base()
            .map(|base| base.has_edge_properties())
            .unwrap_or(false)
        {
            eprintln!("[dynamic-snb] using BaseGraph aligned edge property columns");
            HashMap::new()
        } else {
            eprintln!("[dynamic-snb] loading edge property sidecar");
            load_edge_props(&store_dir.join("snb_edge_props.jsonl"))?
        };
        eprintln!(
            "[dynamic-snb] edge properties ready sidecar_props={} elapsed_s={:.1} total_elapsed_s={:.1}",
            edge_props.len(),
            edge_props_started.elapsed().as_secs_f64(),
            started.elapsed().as_secs_f64()
        );
        Ok(Self {
            view,
            base_vertices,
            vertices,
            edge_props,
            lookup_indexes: SnbLookupIndexes::default(),
            delta_messages_by_creator: HashMap::new(),
            delta_replies_by_parent_creator: HashMap::new(),
            fallback_hits: Mutex::new(HashMap::new()),
        })
        .map(|mut graph| {
            graph.rebuild_lookup_indexes();
            graph
        })
    }

    pub fn current_snapshot(&self) -> u64 {
        self.view
            .delta()
            .map(DeltaGraph::current_snapshot)
            .unwrap_or(0)
    }

    pub fn lookup(&self, label: VertexLabel, external_id: i64) -> VertexId {
        encode_vid(label, external_id)
    }

    pub fn vertex(&self, vid: VertexId) -> Option<&VertexData> {
        self.vertices.get(&vid)
    }

    pub fn insert_vertex(&mut self, data: VertexData) {
        let vid = encode_vid(data.label(), data.external_id());
        self.index_vertex(vid, &data);
        self.index_delta_message_vertex(vid, &data);
        self.vertices.insert(vid, data);
    }

    pub fn insert_edge_cached(
        &mut self,
        src: VertexId,
        dst: VertexId,
        edge_label: EdgeLabel,
        prop: EdgeProp,
        include_reverse: bool,
    ) {
        let edge_type = edge_label.as_i32();
        self.insert_edge_by_type(src, dst, edge_type, prop);
        if include_reverse {
            self.insert_edge_by_type(dst, src, -edge_type, prop);
        }
    }

    pub fn insert_edge_by_type(
        &mut self,
        src: VertexId,
        dst: VertexId,
        edge_type: EdgeType,
        prop: EdgeProp,
    ) {
        if !matches!(prop, EdgeProp::Empty) {
            self.edge_props.insert((src, edge_type, dst), prop);
        }
        let Some(delta) = self.view.delta().cloned() else {
            return;
        };
        if let Err(err) = block_on_runtime(delta.insert_edge(src, dst, edge_type)) {
            eprintln!(
                "[dynamic-snb] failed to write delta edge src={src} dst={dst} edge_type={edge_type}: {err:#}"
            );
        }
    }

    pub fn person(&self, vid: VertexId) -> Option<&PersonProps> {
        if let Some(data) = self.vertices.get(&vid) {
            return match data {
                VertexData::Person(p) => Some(p),
                _ => None,
            };
        }
        self.base_vertices
            .as_ref()?
            .person(self.base_local(LabelId::Person, vid)?)
    }

    pub fn place(&self, vid: VertexId) -> Option<&PlaceProps> {
        if let Some(data) = self.vertices.get(&vid) {
            return match data {
                VertexData::Place(p) => Some(p),
                _ => None,
            };
        }
        self.base_vertices
            .as_ref()?
            .place(self.base_local(LabelId::Place, vid)?)
    }

    pub fn organisation(&self, vid: VertexId) -> Option<&OrgProps> {
        if let Some(data) = self.vertices.get(&vid) {
            return match data {
                VertexData::Organisation(p) => Some(p),
                _ => None,
            };
        }
        self.base_vertices
            .as_ref()?
            .organisation(self.base_local(LabelId::Organisation, vid)?)
    }

    pub fn post(&self, vid: VertexId) -> Option<PostProps> {
        if let Some(data) = self.vertices.get(&vid) {
            return match data {
                VertexData::Post(p) => Some(p.clone()),
                _ => None,
            };
        }
        let local = self.base_local(LabelId::Post, vid)?;
        self.base_vertices.as_ref()?.post(local, external_id(vid))
    }

    pub fn comment(&self, vid: VertexId) -> Option<CommentProps> {
        if let Some(data) = self.vertices.get(&vid) {
            return match data {
                VertexData::Comment(p) => Some(p.clone()),
                _ => None,
            };
        }
        let local = self.base_local(LabelId::Comment, vid)?;
        self.base_vertices
            .as_ref()?
            .comment(local, external_id(vid))
    }

    pub fn forum(&self, vid: VertexId) -> Option<&ForumProps> {
        if let Some(data) = self.vertices.get(&vid) {
            return match data {
                VertexData::Forum(p) => Some(p),
                _ => None,
            };
        }
        self.base_vertices
            .as_ref()?
            .forum(self.base_local(LabelId::Forum, vid)?)
    }

    pub fn tag(&self, vid: VertexId) -> Option<&TagProps> {
        if let Some(data) = self.vertices.get(&vid) {
            return match data {
                VertexData::Tag(p) => Some(p),
                _ => None,
            };
        }
        self.base_vertices
            .as_ref()?
            .tag(self.base_local(LabelId::Tag, vid)?)
    }

    pub fn tag_class(&self, vid: VertexId) -> Option<&TagClassProps> {
        if let Some(data) = self.vertices.get(&vid) {
            return match data {
                VertexData::TagClass(p) => Some(p),
                _ => None,
            };
        }
        self.base_vertices
            .as_ref()?
            .tag_class(self.base_local(LabelId::TagClass, vid)?)
    }

    pub fn vertex_label(&self, vid: VertexId) -> Option<VertexLabel> {
        if let Some(data) = self.vertices.get(&vid) {
            return Some(data.label());
        }
        let label = label_from_vid(vid)?;
        let local = self.base_local(vertex_label_to_base(label), vid)?;
        self.base_vertices
            .as_ref()
            .filter(|base| base.contains(vertex_label_to_base(label), local))
            .map(|_| label)
    }

    pub fn vertices_by_label(&self, label: VertexLabel) -> Box<dyn Iterator<Item = VertexId> + '_> {
        let mut out = self
            .base_vertices
            .as_ref()
            .and_then(|base_props| self.base_vertices_by_label(base_props, label))
            .unwrap_or_default();
        out.extend(
            self.vertices
                .iter()
                .filter_map(move |(&vid, data)| (data.label() == label).then_some(vid)),
        );
        Box::new(out.into_iter())
    }

    pub fn place_name(&self, vid: VertexId) -> String {
        self.place(vid).map(|p| p.name.clone()).unwrap_or_default()
    }

    pub fn post_content_or_image(&self, vid: VertexId) -> Option<String> {
        if let Some(VertexData::Post(post)) = self.vertices.get(&vid) {
            return Some(post.content_or_image().to_string());
        }
        let local = self.base_local(LabelId::Post, vid)?;
        self.base_vertices
            .as_ref()?
            .post_content_or_image(local)
            .ok()
    }

    pub fn comment_content(&self, vid: VertexId) -> Option<String> {
        if let Some(VertexData::Comment(comment)) = self.vertices.get(&vid) {
            return Some(comment.content.clone());
        }
        let local = self.base_local(LabelId::Comment, vid)?;
        self.base_vertices.as_ref()?.comment_content(local).ok()
    }

    pub fn message_content(&self, vid: VertexId) -> Option<String> {
        self.comment_content(vid)
            .or_else(|| self.post_content_or_image(vid))
    }

    pub fn message_refs_by_creator_date(&self, person_vid: VertexId) -> Option<Vec<MessageRef>> {
        let mut out = self
            .base_message_refs_by_creator_date(person_vid)
            .unwrap_or_default();
        if let Some(delta) = self.delta_messages_by_creator.get(&person_vid) {
            out.extend_from_slice(delta);
        }
        if out.is_empty() {
            return Some(out);
        }
        out.sort_by(|a, b| {
            b.creation_date
                .cmp(&a.creation_date)
                .then_with(|| a.id.cmp(&b.id))
        });
        out.dedup_by_key(|msg| msg.vid);
        Some(out)
    }

    pub fn reply_refs_by_parent_creator_date(&self, person_vid: VertexId) -> Option<Vec<ReplyRef>> {
        let mut out = self
            .base_reply_refs_by_parent_creator_date(person_vid)
            .unwrap_or_default();
        if let Some(delta) = self.delta_replies_by_parent_creator.get(&person_vid) {
            out.extend_from_slice(delta);
        }
        if out.is_empty() {
            return Some(out);
        }
        out.sort_by(|a, b| {
            b.creation_date
                .cmp(&a.creation_date)
                .then_with(|| a.comment_id.cmp(&b.comment_id))
        });
        out.dedup_by_key(|reply| reply.reply_vid);
        Some(out)
    }

    pub fn country_by_name(&self, name: &str) -> Option<VertexId> {
        self.lookup_indexes.country_by_name.get(name).copied()
    }

    pub fn tag_by_name(&self, name: &str) -> Option<VertexId> {
        self.lookup_indexes.tag_by_name.get(name).copied()
    }

    pub fn tag_class_by_name(&self, name: &str) -> Option<VertexId> {
        self.lookup_indexes.tag_class_by_name.get(name).copied()
    }

    pub fn out_neighbors_cached(&self, vid: VertexId, edge_label: EdgeLabel) -> Vec<VertexId> {
        self.neighbors_by_type(vid, edge_label.as_i32())
    }

    pub fn in_neighbors_cached(&self, vid: VertexId, edge_label: EdgeLabel) -> Vec<VertexId> {
        self.neighbors_by_type(vid, -edge_label.as_i32())
    }

    pub fn for_each_out_neighbor<F>(&self, vid: VertexId, edge_label: EdgeLabel, mut f: F)
    where
        F: FnMut(VertexId),
    {
        self.for_each_neighbor_by_type(vid, edge_label.as_i32(), &mut f);
    }

    pub fn for_each_in_neighbor<F>(&self, vid: VertexId, edge_label: EdgeLabel, mut f: F)
    where
        F: FnMut(VertexId),
    {
        self.for_each_neighbor_by_type(vid, -edge_label.as_i32(), &mut f);
    }

    pub fn for_each_message_ref_by_creator_date<F>(&self, person_vid: VertexId, f: F)
    where
        F: FnMut(MessageRef),
    {
        let base = self
            .base_message_refs_slice_by_creator_date(person_vid)
            .unwrap_or(&[]);
        let delta = self
            .delta_messages_by_creator
            .get(&person_vid)
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        for_each_merged_message_ref(base, delta, f);
    }

    pub fn for_each_message_ref_by_creator_date_range<F>(
        &self,
        person_vid: VertexId,
        start_date: i64,
        end_date: i64,
        f: F,
    ) where
        F: FnMut(MessageRef),
    {
        let base = self
            .base_message_refs_slice_by_creator_date(person_vid)
            .unwrap_or(&[]);
        let delta = self
            .delta_messages_by_creator
            .get(&person_vid)
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        for_each_merged_message_ref_range(base, delta, start_date, end_date, f);
    }

    pub fn for_each_reply_ref_by_parent_creator_date<F>(&self, person_vid: VertexId, f: F)
    where
        F: FnMut(ReplyRef),
    {
        let base = self
            .base_reply_refs_slice_by_parent_creator_date(person_vid)
            .unwrap_or(&[]);
        let delta = self
            .delta_replies_by_parent_creator
            .get(&person_vid)
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        for_each_merged_reply_ref(base, delta, f);
    }

    pub fn for_each_top_reply_ref_by_parent_creator_date<F>(
        &self,
        person_vid: VertexId,
        limit: usize,
        f: F,
    ) where
        F: FnMut(ReplyRef),
    {
        let base = self
            .base_reply_refs_slice_by_parent_creator_date(person_vid)
            .unwrap_or(&[]);
        let delta = self
            .delta_replies_by_parent_creator
            .get(&person_vid)
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        for_each_merged_reply_ref_limited(base, delta, limit, f);
    }

    pub fn for_each_tag_with_type<F>(&self, tag_class_vid: VertexId, mut f: F)
    where
        F: FnMut(VertexId),
    {
        let mut seen = BTreeSet::new();
        if let (Some(base), Some(base_props)) = (self.view.base(), self.base_vertices.as_ref()) {
            if let Some(local) = base.local_id(LabelId::TagClass, external_id(tag_class_vid)) {
                if let Some(tags) = base_props.tags_by_tag_class.get(local as usize) {
                    for &tag_vid in tags {
                        if seen.insert(tag_vid) {
                            f(tag_vid);
                        }
                    }
                }
            }
        }
        let tag_class_id = external_id(tag_class_vid);
        for (&vid, data) in &self.vertices {
            if let VertexData::Tag(tag) = data {
                if tag.has_type == tag_class_id && seen.insert(vid) {
                    f(vid);
                }
            }
        }
    }

    pub async fn out_neighbors(
        &self,
        vid: VertexId,
        edge_label: EdgeLabel,
    ) -> Result<Vec<VertexId>> {
        Ok(self.out_neighbors_cached(vid, edge_label))
    }

    pub async fn in_neighbors(
        &self,
        vid: VertexId,
        edge_label: EdgeLabel,
    ) -> Result<Vec<VertexId>> {
        Ok(self.in_neighbors_cached(vid, edge_label))
    }

    pub async fn knows_neighbors(&self, vid: VertexId) -> Result<Vec<VertexId>> {
        self.out_neighbors(vid, EdgeLabel::Knows).await
    }

    pub async fn edge_prop(&self, src: VertexId, edge_label: EdgeLabel, dst: VertexId) -> EdgeProp {
        self.edge_prop_by_type(src, edge_label.as_i32(), dst)
    }

    pub fn edge_prop_by_type(&self, src: VertexId, edge_type: EdgeType, dst: VertexId) -> EdgeProp {
        if let Some(prop) = self.base_edge_prop_by_type(src, edge_type, dst) {
            return prop;
        }
        self.edge_props
            .get(&(src, edge_type, dst))
            .copied()
            .unwrap_or(EdgeProp::Empty)
    }

    pub fn out_edges_with_prop(
        &self,
        vid: VertexId,
        edge_label: EdgeLabel,
    ) -> Vec<(VertexId, EdgeProp)> {
        let edge_type = edge_label.as_i32();
        if let Some(mut edges) = self.base_edges_with_prop_by_type(vid, edge_type) {
            edges.extend(self.delta_edges_with_prop_by_type(vid, edge_type));
            return dedup_edges_with_prop(edges);
        }
        self.out_neighbors_cached(vid, edge_label)
            .into_iter()
            .map(|dst| (dst, self.edge_prop_by_type(vid, edge_type, dst)))
            .collect()
    }

    pub fn in_edges_with_prop(
        &self,
        vid: VertexId,
        edge_label: EdgeLabel,
    ) -> Vec<(VertexId, EdgeProp)> {
        let edge_type = -edge_label.as_i32();
        if let Some(mut edges) = self.base_edges_with_prop_by_type(vid, edge_type) {
            edges.extend(self.delta_edges_with_prop_by_type(vid, edge_type));
            return dedup_edges_with_prop(edges);
        }
        self.in_neighbors_cached(vid, edge_label)
            .into_iter()
            .map(|src| (src, self.edge_prop_by_type(vid, edge_type, src)))
            .collect()
    }

    fn base_local(&self, label: LabelId, vid: VertexId) -> Option<u32> {
        let external = external_id(vid);
        self.view.base()?.local_id(label, external)
    }

    fn base_vertices_by_label(
        &self,
        base_props: &BaseSnbProperties,
        label: VertexLabel,
    ) -> Option<Vec<VertexId>> {
        let base = self.view.base()?;
        let label_id = vertex_label_to_base(label);
        let count = base_props.len_for_label(label_id);
        let mut out = Vec::with_capacity(count);
        for local in 0..count {
            if let Some(external) = base.external_id(label_id, local as u32) {
                out.push(encode_vid(label, external));
            }
        }
        Some(out)
    }

    fn base_message_refs_by_creator_date(&self, person_vid: VertexId) -> Option<Vec<MessageRef>> {
        Some(
            self.base_message_refs_slice_by_creator_date(person_vid)?
                .to_vec(),
        )
    }

    fn base_reply_refs_by_parent_creator_date(
        &self,
        person_vid: VertexId,
    ) -> Option<Vec<ReplyRef>> {
        Some(
            self.base_reply_refs_slice_by_parent_creator_date(person_vid)?
                .to_vec(),
        )
    }

    fn base_message_refs_slice_by_creator_date(
        &self,
        person_vid: VertexId,
    ) -> Option<&[MessageRef]> {
        let base = self.view.base()?;
        let local = base.local_id(LabelId::Person, external_id(person_vid))? as usize;
        Some(
            self.base_vertices
                .as_ref()?
                .messages_by_creator
                .get(local)?
                .as_slice(),
        )
    }

    fn base_reply_refs_slice_by_parent_creator_date(
        &self,
        person_vid: VertexId,
    ) -> Option<&[ReplyRef]> {
        let base = self.view.base()?;
        let local = base.local_id(LabelId::Person, external_id(person_vid))? as usize;
        Some(
            self.base_vertices
                .as_ref()?
                .replies_by_parent_creator
                .get(local)?
                .as_slice(),
        )
    }

    fn base_reverse_located_in(&self, place_vid: VertexId) -> Option<Vec<VertexId>> {
        let base = self.view.base()?;
        let local = base.local_id(LabelId::Place, external_id(place_vid))? as usize;
        let base_props = self.base_vertices.as_ref()?;
        let mut out = Vec::new();
        if let Some(values) = base_props.persons_by_place.get(local) {
            out.extend_from_slice(values);
        }
        if let Some(values) = base_props.posts_by_place.get(local) {
            out.extend_from_slice(values);
        }
        if let Some(values) = base_props.comments_by_place.get(local) {
            out.extend_from_slice(values);
        }
        if let Some(values) = base_props.organisations_by_place.get(local) {
            out.extend_from_slice(values);
        }
        Some(out)
    }

    fn rebuild_lookup_indexes(&mut self) {
        self.lookup_indexes = SnbLookupIndexes::default();
        if let Some(base_props) = &self.base_vertices {
            if let Some(base) = self.view.base() {
                let mut pending = Vec::new();
                for (local, place) in base_props.places.iter().enumerate() {
                    if place.place_type == "country" {
                        if let Some(external) = base.external_id(LabelId::Place, local as u32) {
                            pending.push((
                                place.name.clone(),
                                encode_vid(VertexLabel::Place, external),
                            ));
                        }
                    }
                }
                for (name, vid) in pending {
                    self.lookup_indexes.country_by_name.insert(name, vid);
                }
                let mut pending = Vec::new();
                for (local, tag) in base_props.tags.iter().enumerate() {
                    if let Some(external) = base.external_id(LabelId::Tag, local as u32) {
                        pending.push((tag.name.clone(), encode_vid(VertexLabel::Tag, external)));
                    }
                }
                for (name, vid) in pending {
                    self.lookup_indexes.tag_by_name.insert(name, vid);
                }
                let mut pending = Vec::new();
                for (local, tag_class) in base_props.tag_classes.iter().enumerate() {
                    if let Some(external) = base.external_id(LabelId::TagClass, local as u32) {
                        pending.push((
                            tag_class.name.clone(),
                            encode_vid(VertexLabel::TagClass, external),
                        ));
                    }
                }
                for (name, vid) in pending {
                    self.lookup_indexes.tag_class_by_name.insert(name, vid);
                }
            }
        }
        let indexed: Vec<_> = self
            .vertices
            .iter()
            .map(|(&vid, data)| (vid, data.clone()))
            .collect();
        for (vid, data) in indexed {
            self.index_vertex(vid, &data);
        }
    }

    fn index_vertex(&mut self, vid: VertexId, data: &VertexData) {
        match data {
            VertexData::Place(place) if place.place_type == "country" => {
                self.lookup_indexes
                    .country_by_name
                    .insert(place.name.clone(), vid);
            }
            VertexData::Tag(tag) => {
                self.lookup_indexes
                    .tag_by_name
                    .insert(tag.name.clone(), vid);
            }
            VertexData::TagClass(tag_class) => {
                self.lookup_indexes
                    .tag_class_by_name
                    .insert(tag_class.name.clone(), vid);
            }
            _ => {}
        }
    }

    fn index_delta_message_vertex(&mut self, vid: VertexId, data: &VertexData) {
        match data {
            VertexData::Post(post) => {
                let creator = encode_vid(VertexLabel::Person, post.creator);
                self.delta_messages_by_creator
                    .entry(creator)
                    .or_default()
                    .push(MessageRef {
                        vid,
                        id: post.id,
                        creation_date: post.creation_date,
                    });
                sort_message_refs(self.delta_messages_by_creator.get_mut(&creator).unwrap());
            }
            VertexData::Comment(comment) => {
                let creator = encode_vid(VertexLabel::Person, comment.creator);
                self.delta_messages_by_creator
                    .entry(creator)
                    .or_default()
                    .push(MessageRef {
                        vid,
                        id: comment.id,
                        creation_date: comment.creation_date,
                    });
                sort_message_refs(self.delta_messages_by_creator.get_mut(&creator).unwrap());

                if let Some(parent_creator) = self.comment_parent_creator(comment) {
                    self.delta_replies_by_parent_creator
                        .entry(parent_creator)
                        .or_default()
                        .push(ReplyRef {
                            reply_vid: vid,
                            comment_id: comment.id,
                            creation_date: comment.creation_date,
                        });
                    sort_reply_refs(
                        self.delta_replies_by_parent_creator
                            .get_mut(&parent_creator)
                            .unwrap(),
                    );
                }
            }
            _ => {}
        }
    }

    fn comment_parent_creator(&self, comment: &CommentProps) -> Option<VertexId> {
        if let Some(post_id) = comment.reply_of_post {
            let post = self.post(encode_vid(VertexLabel::Post, post_id))?;
            return Some(encode_vid(VertexLabel::Person, post.creator));
        }
        if let Some(comment_id) = comment.reply_of_comment {
            let parent = self.comment(encode_vid(VertexLabel::Comment, comment_id))?;
            return Some(encode_vid(VertexLabel::Person, parent.creator));
        }
        None
    }

    fn for_each_neighbor_by_type<F>(&self, src: VertexId, edge_type: EdgeType, f: &mut F)
    where
        F: FnMut(VertexId),
    {
        let mut seen = BTreeSet::new();
        let base_handled = self.for_each_base_neighbor_by_type(src, edge_type, &mut |dst| {
            if seen.insert(dst) {
                f(dst);
            }
        });
        if base_handled.is_none() {
            self.record_base_miss(src, edge_type);
        }
        for dst in self.delta_neighbors(src, edge_type) {
            if seen.insert(dst) {
                f(dst);
            }
        }
    }

    fn for_each_base_neighbor_by_type<F>(
        &self,
        src: VertexId,
        edge_type: EdgeType,
        f: &mut F,
    ) -> Option<()>
    where
        F: FnMut(VertexId),
    {
        let base = self.view.base()?.as_ref();
        let src_label = label_from_vid(src)?;
        let external = external_id(src);
        match (src_label, edge_type) {
            (VertexLabel::Person, x)
                if x == EdgeLabel::Knows.as_i32() || x == -EdgeLabel::Knows.as_i32() =>
            {
                self.for_each_base_csr(
                    base,
                    "KNOWS/OUT",
                    LabelId::Person,
                    external,
                    LabelId::Person,
                    f,
                );
                self.for_each_base_csr(
                    base,
                    "KNOWS/IN",
                    LabelId::Person,
                    external,
                    LabelId::Person,
                    f,
                );
                Some(())
            }
            (VertexLabel::Person, x) if x == EdgeLabel::LikesPost.as_i32() => self
                .for_each_base_csr(
                    base,
                    "PERSON_LIKES_POST/OUT",
                    LabelId::Person,
                    external,
                    LabelId::Post,
                    f,
                ),
            (VertexLabel::Post, x) if x == -EdgeLabel::LikesPost.as_i32() => self
                .for_each_base_csr(
                    base,
                    "PERSON_LIKES_POST/IN",
                    LabelId::Post,
                    external,
                    LabelId::Person,
                    f,
                ),
            (VertexLabel::Person, x) if x == EdgeLabel::LikesComment.as_i32() => self
                .for_each_base_csr(
                    base,
                    "PERSON_LIKES_COMMENT/OUT",
                    LabelId::Person,
                    external,
                    LabelId::Comment,
                    f,
                ),
            (VertexLabel::Comment, x) if x == -EdgeLabel::LikesComment.as_i32() => self
                .for_each_base_csr(
                    base,
                    "PERSON_LIKES_COMMENT/IN",
                    LabelId::Comment,
                    external,
                    LabelId::Person,
                    f,
                ),
            (VertexLabel::Forum, x) if x == EdgeLabel::HasMember.as_i32() => self
                .for_each_base_csr(
                    base,
                    "FORUM_HAS_MEMBER/OUT",
                    LabelId::Forum,
                    external,
                    LabelId::Person,
                    f,
                ),
            (VertexLabel::Person, x) if x == -EdgeLabel::HasMember.as_i32() => self
                .for_each_base_csr(
                    base,
                    "FORUM_HAS_MEMBER/IN",
                    LabelId::Person,
                    external,
                    LabelId::Forum,
                    f,
                ),
            (VertexLabel::Person, x) if x == EdgeLabel::HasCreator.as_i32() => Some(()),
            (VertexLabel::Person, x) if x == -EdgeLabel::HasCreator.as_i32() => {
                self.for_each_base_csr(
                    base,
                    "PERSON_CREATED_POST",
                    LabelId::Person,
                    external,
                    LabelId::Post,
                    f,
                );
                self.for_each_base_csr(
                    base,
                    "PERSON_CREATED_COMMENT",
                    LabelId::Person,
                    external,
                    LabelId::Comment,
                    f,
                );
                Some(())
            }
            (VertexLabel::Post, x) if x == EdgeLabel::HasCreator.as_i32() => self
                .for_each_base_single_neighbor(
                    base,
                    LabelId::Post,
                    external,
                    LabelId::Person,
                    |base, local| base.post_creator(local),
                    f,
                ),
            (VertexLabel::Comment, x) if x == EdgeLabel::HasCreator.as_i32() => self
                .for_each_base_single_neighbor(
                    base,
                    LabelId::Comment,
                    external,
                    LabelId::Person,
                    |base, local| base.comment_creator(local),
                    f,
                ),
            (VertexLabel::Comment, x)
                if x == EdgeLabel::ReplyOfPost.as_i32()
                    || x == EdgeLabel::ReplyOfComment.as_i32() =>
            {
                let local = base.local_id(LabelId::Comment, external)?;
                match (edge_type, base.comment_reply_of(local)?) {
                    (x, crate::base_graph::ids::MessageId::Post(post))
                        if x == EdgeLabel::ReplyOfPost.as_i32() =>
                    {
                        f(self.base_vid(base, LabelId::Post, post)?);
                    }
                    (x, crate::base_graph::ids::MessageId::Comment(comment))
                        if x == EdgeLabel::ReplyOfComment.as_i32() =>
                    {
                        f(self.base_vid(base, LabelId::Comment, comment)?);
                    }
                    _ => {}
                }
                Some(())
            }
            (VertexLabel::Post, x) if x == -EdgeLabel::ReplyOfPost.as_i32() => self
                .for_each_base_csr(
                    base,
                    "POST_CHILD_COMMENTS",
                    LabelId::Post,
                    external,
                    LabelId::Comment,
                    f,
                ),
            (VertexLabel::Comment, x) if x == -EdgeLabel::ReplyOfComment.as_i32() => self
                .for_each_base_csr(
                    base,
                    "COMMENT_CHILD_COMMENTS",
                    LabelId::Comment,
                    external,
                    LabelId::Comment,
                    f,
                ),
            (VertexLabel::Post, x) if x == -EdgeLabel::ContainerOf.as_i32() => self
                .for_each_base_single_neighbor(
                    base,
                    LabelId::Post,
                    external,
                    LabelId::Forum,
                    |base, local| base.post_forum(local),
                    f,
                ),
            (VertexLabel::Forum, x) if x == EdgeLabel::ContainerOf.as_i32() => self
                .for_each_base_csr(
                    base,
                    "FORUM_CONTAINER_OF_POST",
                    LabelId::Forum,
                    external,
                    LabelId::Post,
                    f,
                ),
            (VertexLabel::Person, x) if x == EdgeLabel::IsLocatedIn.as_i32() => self
                .for_each_base_single_neighbor(
                    base,
                    LabelId::Person,
                    external,
                    LabelId::Place,
                    |base, local| base.person_place(local),
                    f,
                ),
            (VertexLabel::Post, x) if x == EdgeLabel::IsLocatedIn.as_i32() => self
                .for_each_base_single_neighbor(
                    base,
                    LabelId::Post,
                    external,
                    LabelId::Place,
                    |base, local| base.post_place(local),
                    f,
                ),
            (VertexLabel::Comment, x) if x == EdgeLabel::IsLocatedIn.as_i32() => self
                .for_each_base_single_neighbor(
                    base,
                    LabelId::Comment,
                    external,
                    LabelId::Place,
                    |base, local| base.comment_place(local),
                    f,
                ),
            (VertexLabel::Organisation, x) if x == EdgeLabel::IsLocatedIn.as_i32() => self
                .for_each_base_single_neighbor(
                    base,
                    LabelId::Organisation,
                    external,
                    LabelId::Place,
                    |base, local| base.organisation_place(local),
                    f,
                ),
            (VertexLabel::Place, x) if x == -EdgeLabel::IsLocatedIn.as_i32() => {
                for dst in self.base_reverse_located_in(src)? {
                    f(dst);
                }
                Some(())
            }
            (VertexLabel::Forum, x) if x == EdgeLabel::HasModerator.as_i32() => self
                .for_each_base_single_neighbor(
                    base,
                    LabelId::Forum,
                    external,
                    LabelId::Person,
                    |base, local| base.forum_moderator(local),
                    f,
                ),
            (VertexLabel::Tag, x) if x == EdgeLabel::HasType.as_i32() => self
                .for_each_base_single_neighbor(
                    base,
                    LabelId::Tag,
                    external,
                    LabelId::TagClass,
                    |base, local| base.tag_tagclass(local),
                    f,
                ),
            (VertexLabel::Place, x) if x == EdgeLabel::IsPartOf.as_i32() => self
                .for_each_base_single_neighbor(
                    base,
                    LabelId::Place,
                    external,
                    LabelId::Place,
                    |base, local| base.place_parent(local),
                    f,
                ),
            (VertexLabel::Place, x) if x == -EdgeLabel::IsPartOf.as_i32() => self
                .for_each_base_csr(
                    base,
                    "PLACE_CHILD_PLACES",
                    LabelId::Place,
                    external,
                    LabelId::Place,
                    f,
                ),
            (VertexLabel::TagClass, x) if x == EdgeLabel::IsSubclassOf.as_i32() => self
                .for_each_base_single_neighbor(
                    base,
                    LabelId::TagClass,
                    external,
                    LabelId::TagClass,
                    |base, local| base.tagclass_parent(local),
                    f,
                ),
            (VertexLabel::TagClass, x) if x == -EdgeLabel::IsSubclassOf.as_i32() => self
                .for_each_base_csr(
                    base,
                    "TAGCLASS_CHILD_CLASSES",
                    LabelId::TagClass,
                    external,
                    LabelId::TagClass,
                    f,
                ),
            (VertexLabel::Person, x) if x == EdgeLabel::HasInterest.as_i32() => self
                .for_each_base_csr(
                    base,
                    "PERSON_HAS_INTEREST/OUT",
                    LabelId::Person,
                    external,
                    LabelId::Tag,
                    f,
                ),
            (VertexLabel::Tag, x) if x == -EdgeLabel::HasInterest.as_i32() => self
                .for_each_base_csr(
                    base,
                    "PERSON_HAS_INTEREST/IN",
                    LabelId::Tag,
                    external,
                    LabelId::Person,
                    f,
                ),
            (VertexLabel::Post, x) if x == EdgeLabel::HasTag.as_i32() => self.for_each_base_csr(
                base,
                "POST_HAS_TAG/OUT",
                LabelId::Post,
                external,
                LabelId::Tag,
                f,
            ),
            (VertexLabel::Comment, x) if x == EdgeLabel::HasTag.as_i32() => self.for_each_base_csr(
                base,
                "COMMENT_HAS_TAG/OUT",
                LabelId::Comment,
                external,
                LabelId::Tag,
                f,
            ),
            (VertexLabel::Forum, x) if x == EdgeLabel::HasTag.as_i32() => self.for_each_base_csr(
                base,
                "FORUM_HAS_TAG/OUT",
                LabelId::Forum,
                external,
                LabelId::Tag,
                f,
            ),
            (VertexLabel::Tag, x) if x == -EdgeLabel::HasTag.as_i32() => {
                let mut handled = false;
                for (csr, src_label) in [
                    ("POST_HAS_TAG/IN", LabelId::Post),
                    ("COMMENT_HAS_TAG/IN", LabelId::Comment),
                    ("FORUM_HAS_TAG/IN", LabelId::Forum),
                ] {
                    if self
                        .for_each_base_csr(base, csr, LabelId::Tag, external, src_label, f)
                        .is_some()
                    {
                        handled = true;
                    }
                }
                handled.then_some(())
            }
            (VertexLabel::Person, x) if x == EdgeLabel::WorkAt.as_i32() => self.for_each_base_csr(
                base,
                "WORK_AT/OUT",
                LabelId::Person,
                external,
                LabelId::Organisation,
                f,
            ),
            (VertexLabel::Organisation, x) if x == -EdgeLabel::WorkAt.as_i32() => self
                .for_each_base_csr(
                    base,
                    "WORK_AT/IN",
                    LabelId::Organisation,
                    external,
                    LabelId::Person,
                    f,
                ),
            (VertexLabel::Person, x) if x == EdgeLabel::StudyAt.as_i32() => self.for_each_base_csr(
                base,
                "STUDY_AT/OUT",
                LabelId::Person,
                external,
                LabelId::Organisation,
                f,
            ),
            (VertexLabel::Organisation, x) if x == -EdgeLabel::StudyAt.as_i32() => self
                .for_each_base_csr(
                    base,
                    "STUDY_AT/IN",
                    LabelId::Organisation,
                    external,
                    LabelId::Person,
                    f,
                ),
            _ => None,
        }
    }

    fn neighbors_by_type(&self, src: VertexId, edge_type: EdgeType) -> Vec<VertexId> {
        let mut out = match self.base_neighbors_by_type(src, edge_type) {
            Some(base_neighbors) => base_neighbors,
            None => {
                self.record_base_miss(src, edge_type);
                Vec::new()
            }
        };
        out.extend(self.delta_neighbors(src, edge_type));
        dedup_vertices(out)
    }

    fn base_neighbors_by_type(&self, src: VertexId, edge_type: EdgeType) -> Option<Vec<VertexId>> {
        let base = self.view.base()?.as_ref();
        let src_label = label_from_vid(src)?;
        let external = external_id(src);
        match (src_label, edge_type) {
            (VertexLabel::Person, x)
                if x == EdgeLabel::Knows.as_i32() || x == -EdgeLabel::Knows.as_i32() =>
            {
                Some(self.scan_base_undirected_knows(base, external))
            }
            (VertexLabel::Person, x) if x == EdgeLabel::LikesPost.as_i32() => self.scan_base_csr(
                base,
                "PERSON_LIKES_POST/OUT",
                LabelId::Person,
                external,
                LabelId::Post,
            ),
            (VertexLabel::Post, x) if x == -EdgeLabel::LikesPost.as_i32() => self.scan_base_csr(
                base,
                "PERSON_LIKES_POST/IN",
                LabelId::Post,
                external,
                LabelId::Person,
            ),
            (VertexLabel::Person, x) if x == EdgeLabel::LikesComment.as_i32() => self
                .scan_base_csr(
                    base,
                    "PERSON_LIKES_COMMENT/OUT",
                    LabelId::Person,
                    external,
                    LabelId::Comment,
                ),
            (VertexLabel::Comment, x) if x == -EdgeLabel::LikesComment.as_i32() => self
                .scan_base_csr(
                    base,
                    "PERSON_LIKES_COMMENT/IN",
                    LabelId::Comment,
                    external,
                    LabelId::Person,
                ),
            (VertexLabel::Forum, x) if x == EdgeLabel::HasMember.as_i32() => self.scan_base_csr(
                base,
                "FORUM_HAS_MEMBER/OUT",
                LabelId::Forum,
                external,
                LabelId::Person,
            ),
            (VertexLabel::Person, x) if x == -EdgeLabel::HasMember.as_i32() => self.scan_base_csr(
                base,
                "FORUM_HAS_MEMBER/IN",
                LabelId::Person,
                external,
                LabelId::Forum,
            ),
            (VertexLabel::Person, x) if x == EdgeLabel::HasCreator.as_i32() => Some(Vec::new()),
            (VertexLabel::Person, x) if x == -EdgeLabel::HasCreator.as_i32() => {
                let mut out = self
                    .scan_base_csr(
                        base,
                        "PERSON_CREATED_POST",
                        LabelId::Person,
                        external,
                        LabelId::Post,
                    )
                    .unwrap_or_default();
                out.extend(
                    self.scan_base_csr(
                        base,
                        "PERSON_CREATED_COMMENT",
                        LabelId::Person,
                        external,
                        LabelId::Comment,
                    )
                    .unwrap_or_default(),
                );
                Some(out)
            }
            (VertexLabel::Post, x) if x == EdgeLabel::HasCreator.as_i32() => self
                .base_single_neighbor(
                    base,
                    LabelId::Post,
                    external,
                    LabelId::Person,
                    |base, local| base.post_creator(local),
                ),
            (VertexLabel::Comment, x) if x == EdgeLabel::HasCreator.as_i32() => self
                .base_single_neighbor(
                    base,
                    LabelId::Comment,
                    external,
                    LabelId::Person,
                    |base, local| base.comment_creator(local),
                ),
            (VertexLabel::Comment, x)
                if x == EdgeLabel::ReplyOfPost.as_i32()
                    || x == EdgeLabel::ReplyOfComment.as_i32() =>
            {
                let local = base.local_id(LabelId::Comment, external)?;
                match (edge_type, base.comment_reply_of(local)?) {
                    (x, crate::base_graph::ids::MessageId::Post(post))
                        if x == EdgeLabel::ReplyOfPost.as_i32() =>
                    {
                        Some(vec![self.base_vid(base, LabelId::Post, post)?])
                    }
                    (x, crate::base_graph::ids::MessageId::Comment(comment))
                        if x == EdgeLabel::ReplyOfComment.as_i32() =>
                    {
                        Some(vec![self.base_vid(base, LabelId::Comment, comment)?])
                    }
                    _ => Some(Vec::new()),
                }
            }
            (VertexLabel::Post, x) if x == -EdgeLabel::ReplyOfPost.as_i32() => self.scan_base_csr(
                base,
                "POST_CHILD_COMMENTS",
                LabelId::Post,
                external,
                LabelId::Comment,
            ),
            (VertexLabel::Comment, x) if x == -EdgeLabel::ReplyOfComment.as_i32() => self
                .scan_base_csr(
                    base,
                    "COMMENT_CHILD_COMMENTS",
                    LabelId::Comment,
                    external,
                    LabelId::Comment,
                ),
            (VertexLabel::Post, x) if x == -EdgeLabel::ContainerOf.as_i32() => self
                .base_single_neighbor(
                    base,
                    LabelId::Post,
                    external,
                    LabelId::Forum,
                    |base, local| base.post_forum(local),
                ),
            (VertexLabel::Forum, x) if x == EdgeLabel::ContainerOf.as_i32() => self.scan_base_csr(
                base,
                "FORUM_CONTAINER_OF_POST",
                LabelId::Forum,
                external,
                LabelId::Post,
            ),
            (VertexLabel::Person, x) if x == EdgeLabel::IsLocatedIn.as_i32() => self
                .base_single_neighbor(
                    base,
                    LabelId::Person,
                    external,
                    LabelId::Place,
                    |base, local| base.person_place(local),
                ),
            (VertexLabel::Post, x) if x == EdgeLabel::IsLocatedIn.as_i32() => self
                .base_single_neighbor(
                    base,
                    LabelId::Post,
                    external,
                    LabelId::Place,
                    |base, local| base.post_place(local),
                ),
            (VertexLabel::Comment, x) if x == EdgeLabel::IsLocatedIn.as_i32() => self
                .base_single_neighbor(
                    base,
                    LabelId::Comment,
                    external,
                    LabelId::Place,
                    |base, local| base.comment_place(local),
                ),
            (VertexLabel::Organisation, x) if x == EdgeLabel::IsLocatedIn.as_i32() => self
                .base_single_neighbor(
                    base,
                    LabelId::Organisation,
                    external,
                    LabelId::Place,
                    |base, local| base.organisation_place(local),
                ),
            (VertexLabel::Place, x) if x == -EdgeLabel::IsLocatedIn.as_i32() => {
                self.base_reverse_located_in(src)
            }
            (VertexLabel::Forum, x) if x == EdgeLabel::HasModerator.as_i32() => self
                .base_single_neighbor(
                    base,
                    LabelId::Forum,
                    external,
                    LabelId::Person,
                    |base, local| base.forum_moderator(local),
                ),
            (VertexLabel::Tag, x) if x == EdgeLabel::HasType.as_i32() => self.base_single_neighbor(
                base,
                LabelId::Tag,
                external,
                LabelId::TagClass,
                |base, local| base.tag_tagclass(local),
            ),
            (VertexLabel::Place, x) if x == EdgeLabel::IsPartOf.as_i32() => self
                .base_single_neighbor(
                    base,
                    LabelId::Place,
                    external,
                    LabelId::Place,
                    |base, local| base.place_parent(local),
                ),
            (VertexLabel::Place, x) if x == -EdgeLabel::IsPartOf.as_i32() => self.scan_base_csr(
                base,
                "PLACE_CHILD_PLACES",
                LabelId::Place,
                external,
                LabelId::Place,
            ),
            (VertexLabel::TagClass, x) if x == EdgeLabel::IsSubclassOf.as_i32() => self
                .base_single_neighbor(
                    base,
                    LabelId::TagClass,
                    external,
                    LabelId::TagClass,
                    |base, local| base.tagclass_parent(local),
                ),
            (VertexLabel::TagClass, x) if x == -EdgeLabel::IsSubclassOf.as_i32() => self
                .scan_base_csr(
                    base,
                    "TAGCLASS_CHILD_CLASSES",
                    LabelId::TagClass,
                    external,
                    LabelId::TagClass,
                ),
            (VertexLabel::Person, x) if x == EdgeLabel::HasInterest.as_i32() => self.scan_base_csr(
                base,
                "PERSON_HAS_INTEREST/OUT",
                LabelId::Person,
                external,
                LabelId::Tag,
            ),
            (VertexLabel::Tag, x) if x == -EdgeLabel::HasInterest.as_i32() => self.scan_base_csr(
                base,
                "PERSON_HAS_INTEREST/IN",
                LabelId::Tag,
                external,
                LabelId::Person,
            ),
            (VertexLabel::Post, x) if x == EdgeLabel::HasTag.as_i32() => self.scan_base_csr(
                base,
                "POST_HAS_TAG/OUT",
                LabelId::Post,
                external,
                LabelId::Tag,
            ),
            (VertexLabel::Comment, x) if x == EdgeLabel::HasTag.as_i32() => self.scan_base_csr(
                base,
                "COMMENT_HAS_TAG/OUT",
                LabelId::Comment,
                external,
                LabelId::Tag,
            ),
            (VertexLabel::Forum, x) if x == EdgeLabel::HasTag.as_i32() => self.scan_base_csr(
                base,
                "FORUM_HAS_TAG/OUT",
                LabelId::Forum,
                external,
                LabelId::Tag,
            ),
            (VertexLabel::Tag, x) if x == -EdgeLabel::HasTag.as_i32() => {
                let mut handled = false;
                let mut out = Vec::new();
                for (csr, src_label) in [
                    ("POST_HAS_TAG/IN", LabelId::Post),
                    ("COMMENT_HAS_TAG/IN", LabelId::Comment),
                    ("FORUM_HAS_TAG/IN", LabelId::Forum),
                ] {
                    if let Some(mut values) =
                        self.scan_base_csr(base, csr, LabelId::Tag, external, src_label)
                    {
                        handled = true;
                        out.append(&mut values);
                    }
                }
                handled.then_some(out)
            }
            (VertexLabel::Person, x) if x == EdgeLabel::WorkAt.as_i32() => self.scan_base_csr(
                base,
                "WORK_AT/OUT",
                LabelId::Person,
                external,
                LabelId::Organisation,
            ),
            (VertexLabel::Organisation, x) if x == -EdgeLabel::WorkAt.as_i32() => self
                .scan_base_csr(
                    base,
                    "WORK_AT/IN",
                    LabelId::Organisation,
                    external,
                    LabelId::Person,
                ),
            (VertexLabel::Person, x) if x == EdgeLabel::StudyAt.as_i32() => self.scan_base_csr(
                base,
                "STUDY_AT/OUT",
                LabelId::Person,
                external,
                LabelId::Organisation,
            ),
            (VertexLabel::Organisation, x) if x == -EdgeLabel::StudyAt.as_i32() => self
                .scan_base_csr(
                    base,
                    "STUDY_AT/IN",
                    LabelId::Organisation,
                    external,
                    LabelId::Person,
                ),
            _ => None,
        }
    }

    fn base_edges_with_prop_by_type(
        &self,
        src: VertexId,
        edge_type: EdgeType,
    ) -> Option<Vec<(VertexId, EdgeProp)>> {
        let base = self.view.base()?.as_ref();
        let src_label = label_from_vid(src)?;
        let external = external_id(src);
        match (src_label, edge_type) {
            (VertexLabel::Person, x)
                if x == EdgeLabel::Knows.as_i32() || x == -EdgeLabel::Knows.as_i32() =>
            {
                Some(self.scan_base_undirected_knows_with_prop(base, external))
            }
            (VertexLabel::Person, x) if x == EdgeLabel::LikesPost.as_i32() => self
                .scan_base_csr_i64_prop(
                    base,
                    "PERSON_LIKES_POST/OUT",
                    LabelId::Person,
                    external,
                    LabelId::Post,
                    "creation_date",
                ),
            (VertexLabel::Post, x) if x == -EdgeLabel::LikesPost.as_i32() => self
                .scan_base_csr_i64_prop(
                    base,
                    "PERSON_LIKES_POST/IN",
                    LabelId::Post,
                    external,
                    LabelId::Person,
                    "creation_date",
                ),
            (VertexLabel::Person, x) if x == EdgeLabel::LikesComment.as_i32() => self
                .scan_base_csr_i64_prop(
                    base,
                    "PERSON_LIKES_COMMENT/OUT",
                    LabelId::Person,
                    external,
                    LabelId::Comment,
                    "creation_date",
                ),
            (VertexLabel::Comment, x) if x == -EdgeLabel::LikesComment.as_i32() => self
                .scan_base_csr_i64_prop(
                    base,
                    "PERSON_LIKES_COMMENT/IN",
                    LabelId::Comment,
                    external,
                    LabelId::Person,
                    "creation_date",
                ),
            (VertexLabel::Forum, x) if x == EdgeLabel::HasMember.as_i32() => self
                .scan_base_csr_i64_prop(
                    base,
                    "FORUM_HAS_MEMBER/OUT",
                    LabelId::Forum,
                    external,
                    LabelId::Person,
                    "join_date",
                ),
            (VertexLabel::Person, x) if x == -EdgeLabel::HasMember.as_i32() => self
                .scan_base_csr_i64_prop(
                    base,
                    "FORUM_HAS_MEMBER/IN",
                    LabelId::Person,
                    external,
                    LabelId::Forum,
                    "join_date",
                ),
            (VertexLabel::Person, x) if x == EdgeLabel::WorkAt.as_i32() => self
                .scan_base_csr_i32_prop(
                    base,
                    "WORK_AT/OUT",
                    LabelId::Person,
                    external,
                    LabelId::Organisation,
                    "work_from",
                ),
            (VertexLabel::Organisation, x) if x == -EdgeLabel::WorkAt.as_i32() => self
                .scan_base_csr_i32_prop(
                    base,
                    "WORK_AT/IN",
                    LabelId::Organisation,
                    external,
                    LabelId::Person,
                    "work_from",
                ),
            (VertexLabel::Person, x) if x == EdgeLabel::StudyAt.as_i32() => self
                .scan_base_csr_i32_prop(
                    base,
                    "STUDY_AT/OUT",
                    LabelId::Person,
                    external,
                    LabelId::Organisation,
                    "class_year",
                ),
            (VertexLabel::Organisation, x) if x == -EdgeLabel::StudyAt.as_i32() => self
                .scan_base_csr_i32_prop(
                    base,
                    "STUDY_AT/IN",
                    LabelId::Organisation,
                    external,
                    LabelId::Person,
                    "class_year",
                ),
            _ => None,
        }
    }

    fn base_edge_prop_by_type(
        &self,
        src: VertexId,
        edge_type: EdgeType,
        dst: VertexId,
    ) -> Option<EdgeProp> {
        self.base_edges_with_prop_by_type(src, edge_type)?
            .into_iter()
            .find_map(|(candidate, prop)| (candidate == dst).then_some(prop))
    }

    fn scan_base_csr(
        &self,
        base: &BaseGraph,
        csr: &str,
        src_label: LabelId,
        src_external: i64,
        dst_label: LabelId,
    ) -> Option<Vec<VertexId>> {
        if base.csr(csr).is_none() {
            return None;
        }
        let src_local = base.local_id(src_label, src_external)?;
        let mut ctx = base.new_read_context(0);
        let mut out = Vec::new();
        base.scan_csr(csr, src_local, &mut ctx, |chunk| {
            for dst_local in chunk {
                if let Some(vid) = self.base_vid(base, dst_label, *dst_local) {
                    out.push(vid);
                }
            }
            Ok(())
        })
        .ok()?;
        Some(out)
    }

    fn for_each_base_csr<F>(
        &self,
        base: &BaseGraph,
        csr: &str,
        src_label: LabelId,
        src_external: i64,
        dst_label: LabelId,
        f: &mut F,
    ) -> Option<()>
    where
        F: FnMut(VertexId),
    {
        base.csr(csr)?;
        let src_local = base.local_id(src_label, src_external)?;
        let mut ctx = base.new_read_context(0);
        base.scan_csr(csr, src_local, &mut ctx, |chunk| {
            for dst_local in chunk {
                if let Some(vid) = self.base_vid(base, dst_label, *dst_local) {
                    f(vid);
                }
            }
            Ok(())
        })
        .ok()?;
        Some(())
    }

    fn for_each_base_single_neighbor<F, R>(
        &self,
        base: &BaseGraph,
        src_label: LabelId,
        src_external: i64,
        dst_label: LabelId,
        read: R,
        f: &mut F,
    ) -> Option<()>
    where
        F: FnMut(VertexId),
        R: FnOnce(&BaseGraph, u32) -> Option<u32>,
    {
        let src_local = base.local_id(src_label, src_external)?;
        let dst_local = read(base, src_local)?;
        f(self.base_vid(base, dst_label, dst_local)?);
        Some(())
    }

    fn scan_base_csr_i64_prop(
        &self,
        base: &BaseGraph,
        csr: &str,
        src_label: LabelId,
        src_external: i64,
        dst_label: LabelId,
        prop_name: &str,
    ) -> Option<Vec<(VertexId, EdgeProp)>> {
        let src_local = base.local_id(src_label, src_external)?;
        let mut ctx = base.new_read_context(0);
        let pairs = base
            .csr_i64_prop_vec(csr, src_local, prop_name, &mut ctx)
            .ok()??;
        let mut out = Vec::with_capacity(pairs.len());
        for (dst_local, prop) in pairs {
            if let Some(vid) = self.base_vid(base, dst_label, dst_local) {
                out.push((vid, EdgeProp::I64(prop)));
            }
        }
        Some(out)
    }

    fn scan_base_csr_i32_prop(
        &self,
        base: &BaseGraph,
        csr: &str,
        src_label: LabelId,
        src_external: i64,
        dst_label: LabelId,
        prop_name: &str,
    ) -> Option<Vec<(VertexId, EdgeProp)>> {
        let src_local = base.local_id(src_label, src_external)?;
        let mut ctx = base.new_read_context(0);
        let pairs = base
            .csr_i32_prop_vec(csr, src_local, prop_name, &mut ctx)
            .ok()??;
        let mut out = Vec::with_capacity(pairs.len());
        for (dst_local, prop) in pairs {
            if let Some(vid) = self.base_vid(base, dst_label, dst_local) {
                out.push((vid, EdgeProp::I32(prop)));
            }
        }
        Some(out)
    }

    fn scan_base_undirected_knows(&self, base: &BaseGraph, person_external: i64) -> Vec<VertexId> {
        let mut out = self
            .scan_base_csr(
                base,
                "KNOWS/OUT",
                LabelId::Person,
                person_external,
                LabelId::Person,
            )
            .unwrap_or_default();
        out.extend(
            self.scan_base_csr(
                base,
                "KNOWS/IN",
                LabelId::Person,
                person_external,
                LabelId::Person,
            )
            .unwrap_or_default(),
        );
        dedup_vertices(out)
    }

    fn scan_base_undirected_knows_with_prop(
        &self,
        base: &BaseGraph,
        person_external: i64,
    ) -> Vec<(VertexId, EdgeProp)> {
        let mut out = self
            .scan_base_csr_i64_prop(
                base,
                "KNOWS/OUT",
                LabelId::Person,
                person_external,
                LabelId::Person,
                "creation_date",
            )
            .unwrap_or_default();
        out.extend(
            self.scan_base_csr_i64_prop(
                base,
                "KNOWS/IN",
                LabelId::Person,
                person_external,
                LabelId::Person,
                "creation_date",
            )
            .unwrap_or_default(),
        );
        dedup_edges_with_prop(out)
    }

    fn base_single_neighbor<F>(
        &self,
        base: &BaseGraph,
        src_label: LabelId,
        src_external: i64,
        dst_label: LabelId,
        read: F,
    ) -> Option<Vec<VertexId>>
    where
        F: FnOnce(&BaseGraph, u32) -> Option<u32>,
    {
        let src_local = base.local_id(src_label, src_external)?;
        let dst_local = read(base, src_local)?;
        Some(vec![self.base_vid(base, dst_label, dst_local)?])
    }

    fn base_vid(&self, base: &BaseGraph, label: LabelId, local: u32) -> Option<VertexId> {
        let external = base.external_id(label, local)?;
        Some(encode_vid(vertex_label_from_base(label), external))
    }

    fn delta_neighbors(&self, src: VertexId, edge_type: EdgeType) -> Vec<VertexId> {
        let Some(delta) = self.view.delta().cloned() else {
            return Vec::new();
        };
        let snapshot = delta.current_snapshot();
        if snapshot == 0 {
            return Vec::new();
        }
        block_on_runtime(delta.get_neighbors(src, Some(edge_type), snapshot))
            .map(|edges| {
                edges
                    .into_iter()
                    .filter(|edge| edge.marker == EdgeMarker::Insert)
                    .map(|edge| edge.dst)
                    .collect()
            })
            .unwrap_or_default()
    }

    fn delta_edges_with_prop_by_type(
        &self,
        src: VertexId,
        edge_type: EdgeType,
    ) -> Vec<(VertexId, EdgeProp)> {
        self.delta_neighbors(src, edge_type)
            .into_iter()
            .map(|dst| {
                (
                    dst,
                    self.edge_props
                        .get(&(src, edge_type, dst))
                        .copied()
                        .unwrap_or(EdgeProp::Empty),
                )
            })
            .collect()
    }

    /// Records a neighbor lookup whose `(src_label, edge_type)` was not served
    /// by BaseGraph CSR routing (`base_neighbors_by_type` returned `None`).
    /// These are now resolved as empty + DeltaGraph merge instead of probing
    /// the removed legacy LSM `fallback_engine`; the counter lets validation
    /// confirm no such lookup actually needed base topology.
    fn record_base_miss(&self, src: VertexId, edge_type: EdgeType) {
        let label = label_from_vid(src).map(|l| l as i32).unwrap_or(0);
        if let Ok(mut hits) = self.fallback_hits.lock() {
            *hits.entry((label, edge_type)).or_insert(0) += 1;
        }
    }

    /// Snapshot of base-CSR misses accumulated so far, sorted by descending
    /// count: `(src_label, edge_type, count)`.
    pub fn fallback_hits_snapshot(&self) -> Vec<(i32, EdgeType, u64)> {
        let mut out: Vec<(i32, EdgeType, u64)> = self
            .fallback_hits
            .lock()
            .map(|hits| {
                hits.iter()
                    .map(|(&(label, edge_type), &count)| (label, edge_type, count))
                    .collect()
            })
            .unwrap_or_default();
        out.sort_by(|a, b| b.2.cmp(&a.2).then(a.0.cmp(&b.0)).then(a.1.cmp(&b.1)));
        out
    }

    pub fn reset_fallback_hits(&self) {
        if let Ok(mut hits) = self.fallback_hits.lock() {
            hits.clear();
        }
    }
}

pub enum SnbGraph {
    Legacy(LegacySnbGraph),
    Dynamic(DynamicSnbGraph),
}

impl SnbGraph {
    pub async fn open(engine: Arc<Engine>, store_dir: &Path) -> Result<Self> {
        Ok(Self::Legacy(LegacySnbGraph::open(engine, store_dir).await?))
    }

    pub async fn open_dynamic(
        store_dir: &Path,
        base_io: IoConfig,
        io_backend: IoBackendKind,
    ) -> Result<Self> {
        Ok(Self::Dynamic(
            DynamicSnbGraph::open(store_dir, base_io, io_backend).await?,
        ))
    }

    pub async fn build_adjacency_cache(engine: Arc<Engine>, store_dir: &Path) -> Result<usize> {
        LegacySnbGraph::build_adjacency_cache(engine, store_dir).await
    }

    /// Snapshot of legacy fallback-engine hits, `(src_label, edge_type, count)`.
    /// Empty for the legacy variant (which does not route through BaseGraph CSR).
    pub fn fallback_hits_snapshot(&self) -> Vec<(i32, EdgeType, u64)> {
        match self {
            Self::Legacy(_) => Vec::new(),
            Self::Dynamic(graph) => graph.fallback_hits_snapshot(),
        }
    }

    pub fn reset_fallback_hits(&self) {
        if let Self::Dynamic(graph) = self {
            graph.reset_fallback_hits();
        }
    }

    pub fn reset_storage_metrics(&self) {
        match self {
            Self::Legacy(graph) => graph.engine.metrics().reset(),
            Self::Dynamic(graph) => {
                if let Some(engine) = &graph.fallback_engine {
                    engine.metrics().reset();
                }
                if let Some(delta) = graph.view.delta() {
                    delta.engine().metrics().reset();
                }
            }
        }
    }

    pub fn storage_metrics_snapshot_json(&self) -> Value {
        match self {
            Self::Legacy(graph) => json!({
                "engine": graph.engine.metrics().snapshot_json(),
            }),
            Self::Dynamic(graph) => json!({
                "fallback": graph
                    .fallback_engine
                    .as_ref()
                    .map(|engine| engine.metrics().snapshot_json()),
                "delta": graph
                    .view
                    .delta()
                    .map(|delta| delta.engine().metrics().snapshot_json()),
            }),
        }
    }

    pub fn lookup(&self, label: VertexLabel, external_id: i64) -> VertexId {
        match self {
            Self::Legacy(graph) => graph.lookup(label, external_id),
            Self::Dynamic(graph) => graph.lookup(label, external_id),
        }
    }

    pub fn vertex(&self, vid: VertexId) -> Option<&VertexData> {
        match self {
            Self::Legacy(graph) => graph.vertex(vid),
            Self::Dynamic(graph) => graph.vertex(vid),
        }
    }

    pub fn insert_vertex(&mut self, data: VertexData) {
        match self {
            Self::Legacy(graph) => graph.insert_vertex(data),
            Self::Dynamic(graph) => graph.insert_vertex(data),
        }
    }

    pub fn insert_edge_cached(
        &mut self,
        src: VertexId,
        dst: VertexId,
        edge_label: EdgeLabel,
        prop: EdgeProp,
        include_reverse: bool,
    ) {
        match self {
            Self::Legacy(graph) => {
                graph.insert_edge_cached(src, dst, edge_label, prop, include_reverse)
            }
            Self::Dynamic(graph) => {
                graph.insert_edge_cached(src, dst, edge_label, prop, include_reverse)
            }
        }
    }

    pub fn insert_edge_by_type(
        &mut self,
        src: VertexId,
        dst: VertexId,
        edge_type: EdgeType,
        prop: EdgeProp,
    ) {
        match self {
            Self::Legacy(graph) => graph.insert_edge_by_type(src, dst, edge_type, prop),
            Self::Dynamic(graph) => graph.insert_edge_by_type(src, dst, edge_type, prop),
        }
    }

    pub fn person(&self, vid: VertexId) -> Option<&PersonProps> {
        match self {
            Self::Legacy(graph) => graph.person(vid),
            Self::Dynamic(graph) => graph.person(vid),
        }
    }

    pub fn place(&self, vid: VertexId) -> Option<&PlaceProps> {
        match self {
            Self::Legacy(graph) => graph.place(vid),
            Self::Dynamic(graph) => graph.place(vid),
        }
    }

    pub fn organisation(&self, vid: VertexId) -> Option<&OrgProps> {
        match self {
            Self::Legacy(graph) => graph.organisation(vid),
            Self::Dynamic(graph) => graph.organisation(vid),
        }
    }

    pub fn post(&self, vid: VertexId) -> Option<PostProps> {
        match self {
            Self::Legacy(graph) => graph.post(vid).cloned(),
            Self::Dynamic(graph) => graph.post(vid),
        }
    }

    pub fn comment(&self, vid: VertexId) -> Option<CommentProps> {
        match self {
            Self::Legacy(graph) => graph.comment(vid).cloned(),
            Self::Dynamic(graph) => graph.comment(vid),
        }
    }

    pub fn forum(&self, vid: VertexId) -> Option<&ForumProps> {
        match self {
            Self::Legacy(graph) => graph.forum(vid),
            Self::Dynamic(graph) => graph.forum(vid),
        }
    }

    pub fn tag(&self, vid: VertexId) -> Option<&TagProps> {
        match self {
            Self::Legacy(graph) => graph.tag(vid),
            Self::Dynamic(graph) => graph.tag(vid),
        }
    }

    pub fn tag_class(&self, vid: VertexId) -> Option<&TagClassProps> {
        match self {
            Self::Legacy(graph) => graph.tag_class(vid),
            Self::Dynamic(graph) => graph.tag_class(vid),
        }
    }

    pub fn vertex_label(&self, vid: VertexId) -> Option<VertexLabel> {
        match self {
            Self::Legacy(graph) => graph.vertex_label(vid),
            Self::Dynamic(graph) => graph.vertex_label(vid),
        }
    }

    pub fn vertices_by_label(&self, label: VertexLabel) -> Box<dyn Iterator<Item = VertexId> + '_> {
        match self {
            Self::Legacy(graph) => Box::new(graph.vertices_by_label(label)),
            Self::Dynamic(graph) => graph.vertices_by_label(label),
        }
    }

    pub fn place_name(&self, vid: VertexId) -> String {
        match self {
            Self::Legacy(graph) => graph.place_name(vid),
            Self::Dynamic(graph) => graph.place_name(vid),
        }
    }

    pub fn post_content_or_image(&self, vid: VertexId) -> Option<String> {
        match self {
            Self::Legacy(graph) => graph.post_content_or_image(vid),
            Self::Dynamic(graph) => graph.post_content_or_image(vid),
        }
    }

    pub fn comment_content(&self, vid: VertexId) -> Option<String> {
        match self {
            Self::Legacy(graph) => graph.comment_content(vid),
            Self::Dynamic(graph) => graph.comment_content(vid),
        }
    }

    pub fn message_content(&self, vid: VertexId) -> Option<String> {
        match self {
            Self::Legacy(graph) => graph.message_content(vid),
            Self::Dynamic(graph) => graph.message_content(vid),
        }
    }

    pub fn message_refs_by_creator_date(&self, person_vid: VertexId) -> Option<Vec<MessageRef>> {
        match self {
            Self::Dynamic(graph) => graph.message_refs_by_creator_date(person_vid),
            Self::Legacy(_) => None,
        }
    }

    pub fn reply_refs_by_parent_creator_date(&self, person_vid: VertexId) -> Option<Vec<ReplyRef>> {
        match self {
            Self::Dynamic(graph) => graph.reply_refs_by_parent_creator_date(person_vid),
            Self::Legacy(_) => None,
        }
    }

    pub fn country_by_name(&self, name: &str) -> Option<VertexId> {
        match self {
            Self::Dynamic(graph) => graph.country_by_name(name),
            Self::Legacy(graph) => graph.vertices_by_label(VertexLabel::Place).find(|&vid| {
                graph
                    .place(vid)
                    .map(|place| place.name == name && place.place_type == "country")
                    .unwrap_or(false)
            }),
        }
    }

    pub fn tag_by_name(&self, name: &str) -> Option<VertexId> {
        match self {
            Self::Dynamic(graph) => graph.tag_by_name(name),
            Self::Legacy(graph) => graph
                .vertices_by_label(VertexLabel::Tag)
                .find(|&vid| graph.tag(vid).map(|tag| tag.name == name).unwrap_or(false)),
        }
    }

    pub fn tag_class_by_name(&self, name: &str) -> Option<VertexId> {
        match self {
            Self::Dynamic(graph) => graph.tag_class_by_name(name),
            Self::Legacy(graph) => graph.vertices_by_label(VertexLabel::TagClass).find(|&vid| {
                graph
                    .tag_class(vid)
                    .map(|tag_class| tag_class.name == name)
                    .unwrap_or(false)
            }),
        }
    }

    pub fn out_neighbors_cached(&self, vid: VertexId, edge_label: EdgeLabel) -> Vec<VertexId> {
        match self {
            Self::Legacy(graph) => graph.out_neighbors_cached(vid, edge_label),
            Self::Dynamic(graph) => graph.out_neighbors_cached(vid, edge_label),
        }
    }

    pub fn in_neighbors_cached(&self, vid: VertexId, edge_label: EdgeLabel) -> Vec<VertexId> {
        match self {
            Self::Legacy(graph) => graph.in_neighbors_cached(vid, edge_label),
            Self::Dynamic(graph) => graph.in_neighbors_cached(vid, edge_label),
        }
    }

    pub fn for_each_out_neighbor<F>(&self, vid: VertexId, edge_label: EdgeLabel, mut f: F)
    where
        F: FnMut(VertexId),
    {
        match self {
            Self::Legacy(graph) => graph.for_each_out_neighbor(vid, edge_label, &mut f),
            Self::Dynamic(graph) => graph.for_each_out_neighbor(vid, edge_label, &mut f),
        }
    }

    pub fn for_each_in_neighbor<F>(&self, vid: VertexId, edge_label: EdgeLabel, mut f: F)
    where
        F: FnMut(VertexId),
    {
        match self {
            Self::Legacy(graph) => graph.for_each_in_neighbor(vid, edge_label, &mut f),
            Self::Dynamic(graph) => graph.for_each_in_neighbor(vid, edge_label, &mut f),
        }
    }

    pub fn for_each_message_ref_by_creator_date<F>(&self, person_vid: VertexId, mut f: F)
    where
        F: FnMut(MessageRef),
    {
        match self {
            Self::Legacy(graph) => graph.for_each_message_ref_by_creator_date(person_vid, &mut f),
            Self::Dynamic(graph) => graph.for_each_message_ref_by_creator_date(person_vid, &mut f),
        }
    }

    pub fn for_each_message_ref_by_creator_date_range<F>(
        &self,
        person_vid: VertexId,
        start_date: i64,
        end_date: i64,
        mut f: F,
    ) where
        F: FnMut(MessageRef),
    {
        match self {
            Self::Legacy(graph) => graph.for_each_message_ref_by_creator_date_range(
                person_vid, start_date, end_date, &mut f,
            ),
            Self::Dynamic(graph) => graph.for_each_message_ref_by_creator_date_range(
                person_vid, start_date, end_date, &mut f,
            ),
        }
    }

    pub fn for_each_reply_ref_by_parent_creator_date<F>(&self, person_vid: VertexId, mut f: F)
    where
        F: FnMut(ReplyRef),
    {
        match self {
            Self::Legacy(graph) => {
                graph.for_each_reply_ref_by_parent_creator_date(person_vid, &mut f)
            }
            Self::Dynamic(graph) => {
                graph.for_each_reply_ref_by_parent_creator_date(person_vid, &mut f)
            }
        }
    }

    pub fn for_each_top_reply_ref_by_parent_creator_date<F>(
        &self,
        person_vid: VertexId,
        limit: usize,
        mut f: F,
    ) where
        F: FnMut(ReplyRef),
    {
        match self {
            Self::Legacy(graph) => {
                graph.for_each_top_reply_ref_by_parent_creator_date(person_vid, limit, &mut f)
            }
            Self::Dynamic(graph) => {
                graph.for_each_top_reply_ref_by_parent_creator_date(person_vid, limit, &mut f)
            }
        }
    }

    pub fn for_each_tag_with_type<F>(&self, tag_class_vid: VertexId, mut f: F)
    where
        F: FnMut(VertexId),
    {
        match self {
            Self::Legacy(graph) => graph.for_each_tag_with_type(tag_class_vid, &mut f),
            Self::Dynamic(graph) => graph.for_each_tag_with_type(tag_class_vid, &mut f),
        }
    }

    pub async fn out_neighbors(
        &self,
        vid: VertexId,
        edge_label: EdgeLabel,
    ) -> Result<Vec<VertexId>> {
        match self {
            Self::Legacy(graph) => graph.out_neighbors(vid, edge_label).await,
            Self::Dynamic(graph) => graph.out_neighbors(vid, edge_label).await,
        }
    }

    pub async fn in_neighbors(
        &self,
        vid: VertexId,
        edge_label: EdgeLabel,
    ) -> Result<Vec<VertexId>> {
        match self {
            Self::Legacy(graph) => graph.in_neighbors(vid, edge_label).await,
            Self::Dynamic(graph) => graph.in_neighbors(vid, edge_label).await,
        }
    }

    pub async fn knows_neighbors(&self, vid: VertexId) -> Result<Vec<VertexId>> {
        match self {
            Self::Legacy(graph) => graph.knows_neighbors(vid).await,
            Self::Dynamic(graph) => graph.knows_neighbors(vid).await,
        }
    }

    pub async fn edge_prop(&self, src: VertexId, edge_label: EdgeLabel, dst: VertexId) -> EdgeProp {
        match self {
            Self::Legacy(graph) => graph.edge_prop(src, edge_label, dst).await,
            Self::Dynamic(graph) => graph.edge_prop(src, edge_label, dst).await,
        }
    }

    pub fn edge_prop_by_type(&self, src: VertexId, edge_type: EdgeType, dst: VertexId) -> EdgeProp {
        match self {
            Self::Legacy(graph) => graph.edge_prop_by_type(src, edge_type, dst),
            Self::Dynamic(graph) => graph.edge_prop_by_type(src, edge_type, dst),
        }
    }

    pub fn out_edges_with_prop(
        &self,
        vid: VertexId,
        edge_label: EdgeLabel,
    ) -> Vec<(VertexId, EdgeProp)> {
        match self {
            Self::Legacy(graph) => graph.out_edges_with_prop(vid, edge_label),
            Self::Dynamic(graph) => graph.out_edges_with_prop(vid, edge_label),
        }
    }

    pub fn in_edges_with_prop(
        &self,
        vid: VertexId,
        edge_label: EdgeLabel,
    ) -> Vec<(VertexId, EdgeProp)> {
        match self {
            Self::Legacy(graph) => graph.in_edges_with_prop(vid, edge_label),
            Self::Dynamic(graph) => graph.in_edges_with_prop(vid, edge_label),
        }
    }
}

fn block_on_runtime<T>(future: impl Future<Output = Result<T>>) -> Result<T> {
    match tokio::runtime::Handle::try_current() {
        Ok(handle) => tokio::task::block_in_place(|| handle.block_on(future)),
        Err(_) => {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()?;
            runtime.block_on(future)
        }
    }
}

fn dedup_vertices(vertices: Vec<VertexId>) -> Vec<VertexId> {
    let mut seen = BTreeSet::new();
    vertices
        .into_iter()
        .filter(|vid| seen.insert(*vid))
        .collect()
}

fn dedup_edges_with_prop(edges: Vec<(VertexId, EdgeProp)>) -> Vec<(VertexId, EdgeProp)> {
    let mut seen = BTreeSet::new();
    edges
        .into_iter()
        .filter(|(vid, _)| seen.insert(*vid))
        .collect()
}

fn for_each_merged_message_ref<F>(base: &[MessageRef], delta: &[MessageRef], mut f: F)
where
    F: FnMut(MessageRef),
{
    if delta.is_empty() {
        for &message in base {
            f(message);
        }
        return;
    }
    if base.is_empty() {
        for &message in delta {
            f(message);
        }
        return;
    }
    let mut base_idx = 0usize;
    let mut delta_idx = 0usize;
    let mut seen = BTreeSet::new();
    while base_idx < base.len() || delta_idx < delta.len() {
        let next = match (base.get(base_idx), delta.get(delta_idx)) {
            (Some(base_ref), Some(delta_ref)) => {
                if message_ref_precedes_or_ties(*base_ref, *delta_ref) {
                    base_idx += 1;
                    *base_ref
                } else {
                    delta_idx += 1;
                    *delta_ref
                }
            }
            (Some(base_ref), None) => {
                base_idx += 1;
                *base_ref
            }
            (None, Some(delta_ref)) => {
                delta_idx += 1;
                *delta_ref
            }
            (None, None) => break,
        };
        if seen.insert(next.vid) {
            f(next);
        }
    }
}

fn for_each_merged_message_ref_range<F>(
    base: &[MessageRef],
    delta: &[MessageRef],
    start_date: i64,
    end_date: i64,
    mut f: F,
) where
    F: FnMut(MessageRef),
{
    if start_date >= end_date {
        return;
    }
    if delta.is_empty() {
        for &message in base {
            if message.creation_date >= end_date {
                continue;
            }
            if message.creation_date < start_date {
                break;
            }
            f(message);
        }
        return;
    }
    if base.is_empty() {
        for &message in delta {
            if message.creation_date >= end_date {
                continue;
            }
            if message.creation_date < start_date {
                break;
            }
            f(message);
        }
        return;
    }

    let mut base_idx = 0usize;
    let mut delta_idx = 0usize;
    let mut seen = BTreeSet::new();
    while base_idx < base.len() || delta_idx < delta.len() {
        let next = match (base.get(base_idx), delta.get(delta_idx)) {
            (Some(base_ref), Some(delta_ref)) => {
                if message_ref_precedes_or_ties(*base_ref, *delta_ref) {
                    base_idx += 1;
                    *base_ref
                } else {
                    delta_idx += 1;
                    *delta_ref
                }
            }
            (Some(base_ref), None) => {
                base_idx += 1;
                *base_ref
            }
            (None, Some(delta_ref)) => {
                delta_idx += 1;
                *delta_ref
            }
            (None, None) => break,
        };
        if next.creation_date < start_date {
            break;
        }
        if seen.insert(next.vid) && next.creation_date < end_date {
            f(next);
        }
    }
}

fn for_each_merged_reply_ref<F>(base: &[ReplyRef], delta: &[ReplyRef], mut f: F)
where
    F: FnMut(ReplyRef),
{
    if delta.is_empty() {
        for &reply in base {
            f(reply);
        }
        return;
    }
    if base.is_empty() {
        for &reply in delta {
            f(reply);
        }
        return;
    }
    let mut base_idx = 0usize;
    let mut delta_idx = 0usize;
    let mut seen = BTreeSet::new();
    while base_idx < base.len() || delta_idx < delta.len() {
        let next = match (base.get(base_idx), delta.get(delta_idx)) {
            (Some(base_ref), Some(delta_ref)) => {
                if reply_ref_precedes_or_ties(*base_ref, *delta_ref) {
                    base_idx += 1;
                    *base_ref
                } else {
                    delta_idx += 1;
                    *delta_ref
                }
            }
            (Some(base_ref), None) => {
                base_idx += 1;
                *base_ref
            }
            (None, Some(delta_ref)) => {
                delta_idx += 1;
                *delta_ref
            }
            (None, None) => break,
        };
        if seen.insert(next.reply_vid) {
            f(next);
        }
    }
}

fn for_each_merged_reply_ref_limited<F>(
    base: &[ReplyRef],
    delta: &[ReplyRef],
    limit: usize,
    mut f: F,
) where
    F: FnMut(ReplyRef),
{
    if limit == 0 {
        return;
    }
    if delta.is_empty() {
        for &reply in base.iter().take(limit) {
            f(reply);
        }
        return;
    }
    if base.is_empty() {
        for &reply in delta.iter().take(limit) {
            f(reply);
        }
        return;
    }

    let mut base_idx = 0usize;
    let mut delta_idx = 0usize;
    let mut emitted = 0usize;
    let mut seen = BTreeSet::new();
    while emitted < limit && (base_idx < base.len() || delta_idx < delta.len()) {
        let next = match (base.get(base_idx), delta.get(delta_idx)) {
            (Some(base_ref), Some(delta_ref)) => {
                if reply_ref_precedes_or_ties(*base_ref, *delta_ref) {
                    base_idx += 1;
                    *base_ref
                } else {
                    delta_idx += 1;
                    *delta_ref
                }
            }
            (Some(base_ref), None) => {
                base_idx += 1;
                *base_ref
            }
            (None, Some(delta_ref)) => {
                delta_idx += 1;
                *delta_ref
            }
            (None, None) => break,
        };
        if seen.insert(next.reply_vid) {
            emitted += 1;
            f(next);
        }
    }
}

fn message_ref_precedes_or_ties(left: MessageRef, right: MessageRef) -> bool {
    left.creation_date > right.creation_date
        || (left.creation_date == right.creation_date && left.id <= right.id)
}

fn reply_ref_precedes_or_ties(left: ReplyRef, right: ReplyRef) -> bool {
    left.creation_date > right.creation_date
        || (left.creation_date == right.creation_date && left.comment_id <= right.comment_id)
}

fn label_from_vid(vid: VertexId) -> Option<VertexLabel> {
    match (vid >> LABEL_SHIFT) as u8 {
        1 => Some(VertexLabel::Person),
        2 => Some(VertexLabel::Comment),
        3 => Some(VertexLabel::Post),
        4 => Some(VertexLabel::Forum),
        5 => Some(VertexLabel::Organisation),
        6 => Some(VertexLabel::Place),
        7 => Some(VertexLabel::Tag),
        8 => Some(VertexLabel::TagClass),
        _ => None,
    }
}

fn vertex_label_from_base(label: LabelId) -> VertexLabel {
    match label {
        LabelId::Person => VertexLabel::Person,
        LabelId::Comment => VertexLabel::Comment,
        LabelId::Post => VertexLabel::Post,
        LabelId::Forum => VertexLabel::Forum,
        LabelId::Organisation => VertexLabel::Organisation,
        LabelId::Place => VertexLabel::Place,
        LabelId::Tag => VertexLabel::Tag,
        LabelId::TagClass => VertexLabel::TagClass,
    }
}

fn vertex_label_to_base(label: VertexLabel) -> LabelId {
    match label {
        VertexLabel::Person => LabelId::Person,
        VertexLabel::Comment => LabelId::Comment,
        VertexLabel::Post => LabelId::Post,
        VertexLabel::Forum => LabelId::Forum,
        VertexLabel::Organisation => LabelId::Organisation,
        VertexLabel::Place => LabelId::Place,
        VertexLabel::Tag => LabelId::Tag,
        VertexLabel::TagClass => LabelId::TagClass,
    }
}

impl BaseSnbProperties {
    fn len(&self) -> usize {
        self.persons.len()
            + self.places.len()
            + self.organisations.len()
            + self.post_creation_date.len()
            + self.comment_creation_date.len()
            + self.forums.len()
            + self.tags.len()
            + self.tag_classes.len()
    }

    fn len_for_label(&self, label: LabelId) -> usize {
        match label {
            LabelId::Person => self.persons.len(),
            LabelId::Comment => self.comment_creation_date.len(),
            LabelId::Post => self.post_creation_date.len(),
            LabelId::Forum => self.forums.len(),
            LabelId::Organisation => self.organisations.len(),
            LabelId::Place => self.places.len(),
            LabelId::Tag => self.tags.len(),
            LabelId::TagClass => self.tag_classes.len(),
        }
    }

    fn contains(&self, label: LabelId, local: u32) -> bool {
        (local as usize) < self.len_for_label(label)
    }

    fn person(&self, local: u32) -> Option<&PersonProps> {
        self.persons.get(local as usize)
    }

    fn place(&self, local: u32) -> Option<&PlaceProps> {
        self.places.get(local as usize)
    }

    fn organisation(&self, local: u32) -> Option<&OrgProps> {
        self.organisations.get(local as usize)
    }

    /// Reconstruct a `PostProps` on demand from the compact columns. `id` is the
    /// external id (carried in the caller's `VertexId`), so it is not duplicated
    /// in a column. String fields are served lazily via `post_content_or_image`.
    fn post(&self, local: u32, id: i64) -> Option<PostProps> {
        let i = local as usize;
        let creation_date = *self.post_creation_date.get(i)?;
        Some(PostProps {
            id,
            image_file: String::new(),
            creation_date,
            location_ip: String::new(),
            browser_used: String::new(),
            language: String::new(),
            content: String::new(),
            length: self.post_length[i],
            creator: self.post_creator[i],
            forum_id: self.post_forum[i],
            place: self.post_place[i],
        })
    }

    /// Reconstruct a `CommentProps` on demand from the compact columns. See `post`.
    fn comment(&self, local: u32, id: i64) -> Option<CommentProps> {
        let i = local as usize;
        let creation_date = *self.comment_creation_date.get(i)?;
        Some(CommentProps {
            id,
            creation_date,
            location_ip: String::new(),
            browser_used: String::new(),
            content: String::new(),
            length: self.comment_length[i],
            creator: self.comment_creator[i],
            place: self.comment_place[i],
            reply_of_post: optional_i64(self.comment_reply_post[i]),
            reply_of_comment: optional_i64(self.comment_reply_comment[i]),
        })
    }

    fn forum(&self, local: u32) -> Option<&ForumProps> {
        self.forums.get(local as usize)
    }

    fn tag(&self, local: u32) -> Option<&TagProps> {
        self.tags.get(local as usize)
    }

    fn tag_class(&self, local: u32) -> Option<&TagClassProps> {
        self.tag_classes.get(local as usize)
    }

    fn post_content_or_image(&self, local: u32) -> Result<String> {
        let idx = local as usize;
        let content = self.message_strings.post_content.get(idx)?;
        if content.is_empty() {
            self.message_strings.post_image_file.get(idx)
        } else {
            Ok(content)
        }
    }

    fn comment_content(&self, local: u32) -> Result<String> {
        self.message_strings.comment_content.get(local as usize)
    }
}

fn load_vertices_from_base_graph(base: &BaseGraph) -> Result<BaseSnbProperties> {
    let person_first_name = base.read_string_property("Person", "first_name")?;
    let person_last_name = base.read_string_property("Person", "last_name")?;
    let person_gender = base.read_string_property("Person", "gender")?;
    let person_birthday = base.read_i64_property("Person", "birthday")?;
    let person_creation_date = base.read_i64_property("Person", "creation_date")?;
    let person_location_ip = base.read_string_property("Person", "location_ip")?;
    let person_browser_used = base.read_string_property("Person", "browser_used")?;
    let person_place = base.read_i64_property("Person", "place")?;
    let person_language = base.read_string_property("Person", "language")?;
    let person_email = base.read_string_property("Person", "email")?;
    let mut persons = Vec::with_capacity(person_first_name.len());
    for local in 0..person_first_name.len() {
        let id = required_external_id(base, LabelId::Person, local)?;
        persons.push(PersonProps {
            id,
            first_name: person_first_name[local].clone(),
            last_name: person_last_name[local].clone(),
            gender: person_gender[local].clone(),
            birthday: person_birthday[local],
            creation_date: person_creation_date[local],
            location_ip: person_location_ip[local].clone(),
            browser_used: person_browser_used[local].clone(),
            place: person_place[local],
            language: person_language[local].clone(),
            email: person_email[local].clone(),
        });
    }

    let forum_title = base.read_string_property("Forum", "title")?;
    let forum_creation_date = base.read_i64_property("Forum", "creation_date")?;
    let forum_moderator = base.read_i64_property("Forum", "moderator")?;
    let mut forums = Vec::with_capacity(forum_title.len());
    for local in 0..forum_title.len() {
        let id = required_external_id(base, LabelId::Forum, local)?;
        forums.push(ForumProps {
            id,
            title: forum_title[local].clone(),
            creation_date: forum_creation_date[local],
            moderator: forum_moderator[local],
        });
    }

    let message_strings = BaseMessageStringColumns {
        post_image_file: base.open_string_property("Post", "image_file")?,
        post_content: base.open_string_property("Post", "content")?,
        comment_content: base.open_string_property("Comment", "content")?,
    };

    let post_creation_date = base.read_i64_property("Post", "creation_date")?;
    let post_length = base.read_i64_property("Post", "length")?;
    let post_creator = base.read_i64_property("Post", "creator")?;
    let post_forum = base.read_i64_property("Post", "forum_id")?;
    let post_place = base.read_i64_property("Post", "place")?;
    if message_strings.post_content.len() != post_creation_date.len()
        || message_strings.post_image_file.len() != post_creation_date.len()
    {
        anyhow::bail!(
            "Post lazy string column length mismatch content={} image={} rows={}",
            message_strings.post_content.len(),
            message_strings.post_image_file.len(),
            post_creation_date.len()
        );
    }
    let mut posts = Vec::with_capacity(post_creation_date.len());
    for local in 0..post_creation_date.len() {
        let id = required_external_id(base, LabelId::Post, local)?;
        posts.push(PostProps {
            id,
            image_file: String::new(),
            creation_date: post_creation_date[local],
            location_ip: String::new(),
            browser_used: String::new(),
            language: String::new(),
            content: String::new(),
            length: post_length[local],
            creator: post_creator[local],
            forum_id: post_forum[local],
            place: post_place[local],
        });
    }

    let comment_creation_date = base.read_i64_property("Comment", "creation_date")?;
    let comment_length = base.read_i64_property("Comment", "length")?;
    let comment_creator = base.read_i64_property("Comment", "creator")?;
    let comment_place = base.read_i64_property("Comment", "place")?;
    let comment_reply_post = base.read_i64_property("Comment", "reply_of_post")?;
    let comment_reply_comment = base.read_i64_property("Comment", "reply_of_comment")?;
    if message_strings.comment_content.len() != comment_creation_date.len() {
        anyhow::bail!(
            "Comment lazy string column length mismatch content={} rows={}",
            message_strings.comment_content.len(),
            comment_creation_date.len()
        );
    }
    let mut comments = Vec::with_capacity(comment_creation_date.len());
    for local in 0..comment_creation_date.len() {
        let id = required_external_id(base, LabelId::Comment, local)?;
        comments.push(CommentProps {
            id,
            creation_date: comment_creation_date[local],
            location_ip: String::new(),
            browser_used: String::new(),
            content: String::new(),
            length: comment_length[local],
            creator: comment_creator[local],
            place: comment_place[local],
            reply_of_post: optional_i64(comment_reply_post[local]),
            reply_of_comment: optional_i64(comment_reply_comment[local]),
        });
    }
    let (messages_by_creator, replies_by_parent_creator) =
        build_base_message_indexes(base, persons.len(), &posts, &comments)?;

    let place_name = base.read_string_property("Place", "name")?;
    let place_type = base.read_string_property("Place", "place_type")?;
    let place_parent = base.read_i64_property("Place", "is_part_of")?;
    let mut places = Vec::with_capacity(place_name.len());
    for local in 0..place_name.len() {
        let id = required_external_id(base, LabelId::Place, local)?;
        places.push(PlaceProps {
            id,
            name: place_name[local].clone(),
            place_type: place_type[local].clone(),
            is_part_of: optional_i64(place_parent[local]),
        });
    }

    let org_type = base.read_string_property("Organisation", "org_type")?;
    let org_name = base.read_string_property("Organisation", "name")?;
    let org_place = base.read_i64_property("Organisation", "place")?;
    let mut organisations = Vec::with_capacity(org_name.len());
    for local in 0..org_name.len() {
        let id = required_external_id(base, LabelId::Organisation, local)?;
        organisations.push(OrgProps {
            id,
            org_type: org_type[local].clone(),
            name: org_name[local].clone(),
            place: org_place[local],
        });
    }

    let tag_name = base.read_string_property("Tag", "name")?;
    let tag_has_type = base.read_i64_property("Tag", "has_type")?;
    let mut tags = Vec::with_capacity(tag_name.len());
    for local in 0..tag_name.len() {
        let id = required_external_id(base, LabelId::Tag, local)?;
        tags.push(TagProps {
            id,
            name: tag_name[local].clone(),
            has_type: tag_has_type[local],
        });
    }

    let tagclass_name = base.read_string_property("TagClass", "name")?;
    let tagclass_parent = base.read_i64_property("TagClass", "is_subclass_of")?;
    let mut tag_classes = Vec::with_capacity(tagclass_name.len());
    for local in 0..tagclass_name.len() {
        let id = required_external_id(base, LabelId::TagClass, local)?;
        tag_classes.push(TagClassProps {
            id,
            name: tagclass_name[local].clone(),
            is_subclass_of: optional_i64(tagclass_parent[local]),
        });
    }

    let (persons_by_place, posts_by_place, comments_by_place, organisations_by_place) =
        build_base_place_indexes(
            base,
            places.len(),
            &persons,
            &posts,
            &comments,
            &organisations,
        );
    let tags_by_tag_class = build_base_tag_class_index(base, tag_classes.len(), &tags);

    // The compact columns read above (post_*/comment_*) are what we keep
    // resident; the AoS temporaries were only needed to feed the index builders.
    drop(posts);
    drop(comments);

    Ok(BaseSnbProperties {
        persons,
        places,
        organisations,
        post_creation_date,
        post_length,
        post_creator,
        post_forum,
        post_place,
        comment_creation_date,
        comment_length,
        comment_creator,
        comment_place,
        comment_reply_post,
        comment_reply_comment,
        forums,
        tags,
        tag_classes,
        message_strings,
        messages_by_creator,
        replies_by_parent_creator,
        persons_by_place,
        posts_by_place,
        comments_by_place,
        organisations_by_place,
        tags_by_tag_class,
    })
}

type BasePlaceIndexes = (
    Vec<Vec<VertexId>>,
    Vec<Vec<VertexId>>,
    Vec<Vec<VertexId>>,
    Vec<Vec<VertexId>>,
);

fn build_base_place_indexes(
    base: &BaseGraph,
    place_count: usize,
    persons: &[PersonProps],
    posts: &[PostProps],
    comments: &[CommentProps],
    organisations: &[OrgProps],
) -> BasePlaceIndexes {
    let mut persons_by_place = vec![Vec::new(); place_count];
    let mut posts_by_place = vec![Vec::new(); place_count];
    let mut comments_by_place = vec![Vec::new(); place_count];
    let mut organisations_by_place = vec![Vec::new(); place_count];

    for person in persons {
        if let Some(place_local) = base.local_id(LabelId::Place, person.place) {
            persons_by_place[place_local as usize].push(encode_vid(VertexLabel::Person, person.id));
        }
    }
    for post in posts {
        if let Some(place_local) = base.local_id(LabelId::Place, post.place) {
            posts_by_place[place_local as usize].push(encode_vid(VertexLabel::Post, post.id));
        }
    }
    for comment in comments {
        if let Some(place_local) = base.local_id(LabelId::Place, comment.place) {
            comments_by_place[place_local as usize]
                .push(encode_vid(VertexLabel::Comment, comment.id));
        }
    }
    for organisation in organisations {
        if let Some(place_local) = base.local_id(LabelId::Place, organisation.place) {
            organisations_by_place[place_local as usize]
                .push(encode_vid(VertexLabel::Organisation, organisation.id));
        }
    }

    (
        persons_by_place,
        posts_by_place,
        comments_by_place,
        organisations_by_place,
    )
}

fn build_base_tag_class_index(
    base: &BaseGraph,
    tag_class_count: usize,
    tags: &[TagProps],
) -> Vec<Vec<VertexId>> {
    let mut tags_by_tag_class = vec![Vec::new(); tag_class_count];
    for tag in tags {
        if let Some(tag_class_local) = base.local_id(LabelId::TagClass, tag.has_type) {
            tags_by_tag_class[tag_class_local as usize].push(encode_vid(VertexLabel::Tag, tag.id));
        }
    }
    tags_by_tag_class
}

fn build_base_message_indexes(
    base: &BaseGraph,
    person_count: usize,
    posts: &[PostProps],
    comments: &[CommentProps],
) -> Result<(Vec<Vec<MessageRef>>, Vec<Vec<ReplyRef>>)> {
    let mut messages_by_creator = vec![Vec::new(); person_count];
    let mut replies_by_parent_creator = vec![Vec::new(); person_count];

    for post in posts {
        if let Some(person_local) = base.local_id(LabelId::Person, post.creator) {
            messages_by_creator[person_local as usize].push(MessageRef {
                vid: encode_vid(VertexLabel::Post, post.id),
                id: post.id,
                creation_date: post.creation_date,
            });
        }
    }

    for comment in comments {
        if let Some(person_local) = base.local_id(LabelId::Person, comment.creator) {
            messages_by_creator[person_local as usize].push(MessageRef {
                vid: encode_vid(VertexLabel::Comment, comment.id),
                id: comment.id,
                creation_date: comment.creation_date,
            });
        }
        let parent_creator = if let Some(post_id) = comment.reply_of_post {
            base.local_id(LabelId::Post, post_id)
                .and_then(|post_local| posts.get(post_local as usize))
                .and_then(|post| base.local_id(LabelId::Person, post.creator))
        } else if let Some(comment_id) = comment.reply_of_comment {
            base.local_id(LabelId::Comment, comment_id)
                .and_then(|comment_local| comments.get(comment_local as usize))
                .and_then(|parent| base.local_id(LabelId::Person, parent.creator))
        } else {
            None
        };
        if let Some(person_local) = parent_creator {
            replies_by_parent_creator[person_local as usize].push(ReplyRef {
                reply_vid: encode_vid(VertexLabel::Comment, comment.id),
                comment_id: comment.id,
                creation_date: comment.creation_date,
            });
        }
    }

    for messages in &mut messages_by_creator {
        sort_message_refs(messages);
    }
    for replies in &mut replies_by_parent_creator {
        sort_reply_refs(replies);
    }
    Ok((messages_by_creator, replies_by_parent_creator))
}

fn sort_message_refs(messages: &mut Vec<MessageRef>) {
    messages.sort_by(|a, b| {
        b.creation_date
            .cmp(&a.creation_date)
            .then_with(|| a.id.cmp(&b.id))
    });
    messages.dedup_by_key(|msg| msg.vid);
}

fn sort_reply_refs(replies: &mut Vec<ReplyRef>) {
    replies.sort_by(|a, b| {
        b.creation_date
            .cmp(&a.creation_date)
            .then_with(|| a.comment_id.cmp(&b.comment_id))
    });
    replies.dedup_by_key(|reply| reply.reply_vid);
}

fn required_external_id(base: &BaseGraph, label: LabelId, local: usize) -> Result<i64> {
    base.external_id(label, local as u32)
        .ok_or_else(|| anyhow::anyhow!("missing external id for {:?} local {}", label, local))
}

fn optional_i64(value: i64) -> Option<i64> {
    (value != i64::MIN).then_some(value)
}

fn load_vertices(path: &Path) -> Result<HashMap<VertexId, VertexData>> {
    let file = File::open(path)?;
    let reader = BufReader::new(file);
    let mut out = HashMap::new();
    for line in reader.lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let row: VertexRow = serde_json::from_str(&line)?;
        out.insert(row.vid, row.data);
    }
    Ok(out)
}

fn load_edge_props(path: &Path) -> Result<HashMap<(VertexId, EdgeType, VertexId), EdgeProp>> {
    if !path.exists() {
        return Ok(HashMap::new());
    }
    let file = File::open(path)?;
    let reader = BufReader::new(file);
    let mut out = HashMap::new();
    for line in reader.lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let row: EdgePropRow = serde_json::from_str(&line)?;
        out.insert((row.src, row.edge_type, row.dst), row.prop);
    }
    Ok(out)
}

const ADJ_MAGIC: &[u8; 8] = b"LSMADJ01";
const ADJ_HEADER_LEN: usize = 32;
const ADJ_GROUP_LEN: usize = 32;
const ADJ_WRITE_BUFFER_BYTES: usize = 64 * 1024 * 1024;
const ADJ_DST_CHUNK: usize = 1_000_000;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct AdjacencyBuildStats {
    pub groups: usize,
    pub directed_edges: usize,
    pub visible_edges: usize,
    pub duplicate_edges_removed: usize,
}

#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct AdjacencyCacheWriteStats {
    pub group_count: usize,
    pub edge_count: usize,
    pub encoded_group_bytes: u64,
    pub encoded_dst_bytes: u64,
    pub write_elapsed_s: f64,
    pub sync_elapsed_s: f64,
}

#[derive(Debug, Default)]
pub struct AdjacencyBuilder {
    grouped: HashMap<(VertexId, EdgeType), Vec<(VertexId, Timestamp)>>,
    directed_edges: usize,
}

impl AdjacencyBuilder {
    pub fn from_adjacency(
        adjacency: HashMap<(VertexId, EdgeType), Vec<VertexId>>,
        max_existing_ts: Timestamp,
    ) -> Self {
        let mut builder = Self::default();
        for ((src, edge_type), dsts) in adjacency {
            for (idx, dst) in dsts.into_iter().enumerate() {
                let ts = max_existing_ts.saturating_sub(idx as u64);
                builder.record_edge(src, edge_type, dst, ts);
            }
        }
        builder
    }

    pub fn record_edge(
        &mut self,
        src: VertexId,
        edge_type: EdgeType,
        dst: VertexId,
        ts: Timestamp,
    ) {
        self.grouped
            .entry((src, edge_type))
            .or_default()
            .push((dst, ts));
        self.directed_edges += 1;
    }

    pub fn directed_edges(&self) -> usize {
        self.directed_edges
    }

    pub fn build(
        self,
    ) -> (
        HashMap<(VertexId, EdgeType), Vec<VertexId>>,
        AdjacencyBuildStats,
    ) {
        let mut adjacency = HashMap::with_capacity(self.grouped.len());
        let mut visible_edges = 0usize;
        let mut duplicate_edges_removed = 0usize;

        for (key, mut dsts) in self.grouped {
            let original_len = dsts.len();
            dsts.sort_unstable_by(|a, b| a.0.cmp(&b.0).then_with(|| b.1.cmp(&a.1)));
            dsts.dedup_by_key(|(dst, _)| *dst);
            dsts.sort_unstable_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
            visible_edges += dsts.len();
            duplicate_edges_removed += original_len - dsts.len();
            adjacency.insert(key, dsts.into_iter().map(|(dst, _)| dst).collect());
        }

        let stats = AdjacencyBuildStats {
            groups: adjacency.len(),
            directed_edges: self.directed_edges,
            visible_edges,
            duplicate_edges_removed,
        };
        (adjacency, stats)
    }
}

fn build_adjacency_from_edges(
    edges: Vec<EdgeRecord>,
) -> HashMap<(VertexId, EdgeType), Vec<VertexId>> {
    let mut builder = AdjacencyBuilder::default();
    for edge in edges {
        builder.record_edge(edge.src, edge.edge_type, edge.dst, edge.ts);
    }
    let (adjacency, _) = builder.build();
    adjacency
}

pub fn write_adjacency_cache_atomic(
    path: &Path,
    adjacency: &HashMap<(VertexId, EdgeType), Vec<VertexId>>,
) -> Result<AdjacencyCacheWriteStats> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp_path = path.with_extension("bin.tmp");
    let stats = write_adjacency_cache(&tmp_path, adjacency)?;
    fs::rename(tmp_path, path)?;
    Ok(stats)
}

fn write_adjacency_cache(
    path: &Path,
    adjacency: &HashMap<(VertexId, EdgeType), Vec<VertexId>>,
) -> Result<AdjacencyCacheWriteStats> {
    let started = Instant::now();
    let mut groups: Vec<_> = adjacency.iter().collect();
    groups.sort_by_key(|((src, edge_type), _)| (*src, *edge_type));
    let total_edges: usize = groups.iter().map(|(_, dsts)| dsts.len()).sum();
    let file = File::create(path)?;
    let mut writer = BufWriter::with_capacity(ADJ_WRITE_BUFFER_BYTES, file);

    let mut header = [0u8; ADJ_HEADER_LEN];
    header[0..8].copy_from_slice(ADJ_MAGIC);
    header[8..12].copy_from_slice(&1u32.to_le_bytes());
    header[12..16].copy_from_slice(&(ADJ_HEADER_LEN as u32).to_le_bytes());
    header[16..24].copy_from_slice(&(groups.len() as u64).to_le_bytes());
    header[24..32].copy_from_slice(&(total_edges as u64).to_le_bytes());
    writer.write_all(&header)?;

    let mut first_idx = 0u64;
    for ((src, edge_type), dsts) in &groups {
        let mut group = [0u8; ADJ_GROUP_LEN];
        group[0..8].copy_from_slice(&src.to_le_bytes());
        group[8..12].copy_from_slice(&edge_type.to_le_bytes());
        group[12..16].copy_from_slice(&0u32.to_le_bytes());
        group[16..24].copy_from_slice(&first_idx.to_le_bytes());
        group[24..32].copy_from_slice(&(dsts.len() as u64).to_le_bytes());
        writer.write_all(&group)?;
        first_idx += dsts.len() as u64;
    }

    let mut dst_buf = Vec::with_capacity(ADJ_DST_CHUNK * std::mem::size_of::<VertexId>());
    for (_, dsts) in groups {
        for chunk in dsts.chunks(ADJ_DST_CHUNK) {
            dst_buf.clear();
            for dst in chunk {
                dst_buf.extend_from_slice(&dst.to_le_bytes());
            }
            writer.write_all(&dst_buf)?;
        }
    }

    writer.flush()?;
    let write_elapsed_s = started.elapsed().as_secs_f64();
    let sync_started = Instant::now();
    writer.get_ref().sync_all()?;
    let sync_elapsed_s = sync_started.elapsed().as_secs_f64();

    Ok(AdjacencyCacheWriteStats {
        group_count: adjacency.len(),
        edge_count: total_edges,
        encoded_group_bytes: (adjacency.len() * ADJ_GROUP_LEN) as u64,
        encoded_dst_bytes: (total_edges * std::mem::size_of::<VertexId>()) as u64,
        write_elapsed_s,
        sync_elapsed_s,
    })
}

pub fn load_adjacency_cache(path: &Path) -> Result<HashMap<(VertexId, EdgeType), Vec<VertexId>>> {
    let mut file = File::open(path)?;
    let mut header = [0u8; ADJ_HEADER_LEN];
    file.read_exact(&mut header)?;
    if &header[0..8] != ADJ_MAGIC {
        anyhow::bail!("bad adjacency cache magic in {}", path.display());
    }
    let version = u32::from_le_bytes(header[8..12].try_into().unwrap());
    if version != 1 {
        anyhow::bail!("unsupported adjacency cache version {version}");
    }
    let group_count = u64::from_le_bytes(header[16..24].try_into().unwrap()) as usize;
    let edge_count = u64::from_le_bytes(header[24..32].try_into().unwrap()) as usize;

    let mut group_bytes = vec![0u8; group_count * ADJ_GROUP_LEN];
    file.read_exact(&mut group_bytes)?;
    let mut dsts = vec![0u8; edge_count * 8];
    file.read_exact(&mut dsts)?;

    let mut out = HashMap::with_capacity(group_count);
    for chunk in group_bytes.chunks_exact(ADJ_GROUP_LEN) {
        let src = u64::from_le_bytes(chunk[0..8].try_into().unwrap());
        let edge_type = i32::from_le_bytes(chunk[8..12].try_into().unwrap());
        let first = u64::from_le_bytes(chunk[16..24].try_into().unwrap()) as usize;
        let count = u64::from_le_bytes(chunk[24..32].try_into().unwrap()) as usize;
        let mut list = Vec::with_capacity(count);
        let start = first * 8;
        let end = start + count * 8;
        for dst_chunk in dsts[start..end].chunks_exact(8) {
            list.push(u64::from_le_bytes(dst_chunk.try_into().unwrap()));
        }
        out.insert((src, edge_type), list);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn target_tempdir(name: &str) -> tempfile::TempDir {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("target")
            .join("test-tmp");
        std::fs::create_dir_all(&root).unwrap();
        tempfile::Builder::new()
            .prefix(name)
            .tempdir_in(root)
            .unwrap()
    }

    #[test]
    fn adjacency_cache_round_trips_current_format() {
        let dir = target_tempdir("adjacency-cache-round-trip-");
        let path = dir.path().join("snb_adjacency.bin");
        let mut adjacency = HashMap::new();
        adjacency.insert((10, 1), vec![20, 21, 22]);
        adjacency.insert((10, -1), vec![30]);
        adjacency.insert((11, 2), vec![40, 41]);

        let stats = write_adjacency_cache_atomic(&path, &adjacency).unwrap();
        assert_eq!(stats.group_count, 3);
        assert_eq!(stats.edge_count, 6);
        assert_eq!(stats.encoded_group_bytes, 3 * ADJ_GROUP_LEN as u64);
        assert_eq!(
            stats.encoded_dst_bytes,
            6 * std::mem::size_of::<VertexId>() as u64
        );

        let loaded = load_adjacency_cache(&path).unwrap();
        assert_eq!(loaded, adjacency);
    }

    #[test]
    fn adjacency_builder_matches_scan_edge_builder() {
        let edges = vec![
            EdgeRecord::insert(1, 10, 7, 1),
            EdgeRecord::insert(1, 11, 7, 2),
            EdgeRecord::insert(1, 10, 7, 3),
            EdgeRecord::insert(1, 12, -7, 4),
            EdgeRecord::insert(2, 20, 7, 5),
        ];
        let expected = build_adjacency_from_edges(edges.clone());

        let mut builder = AdjacencyBuilder::default();
        for edge in edges {
            builder.record_edge(edge.src, edge.edge_type, edge.dst, edge.ts);
        }
        let (actual, stats) = builder.build();

        assert_eq!(actual, expected);
        assert_eq!(stats.groups, 3);
        assert_eq!(stats.directed_edges, 5);
        assert_eq!(stats.visible_edges, 4);
        assert_eq!(stats.duplicate_edges_removed, 1);
        assert_eq!(actual.get(&(1, 7)).unwrap(), &vec![10, 11]);
        assert_eq!(actual.get(&(1, -7)).unwrap(), &vec![12]);
    }
}
