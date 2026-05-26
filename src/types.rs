use serde::{Deserialize, Serialize};

pub type VertexId = u64;
pub type Timestamp = u64;
pub type SnapshotId = u64;
pub type FileId = u64;
pub type LevelId = u8;
pub type EdgeType = i32;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[repr(u8)]
pub enum EdgeMarker {
    Insert = 0,
    Delete = 1,
}

impl EdgeMarker {
    pub fn from_u8(v: u8) -> Self {
        match v {
            1 => Self::Delete,
            _ => Self::Insert,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct EdgeRecord {
    pub src: VertexId,
    pub dst: VertexId,
    pub edge_type: EdgeType,
    pub ts: Timestamp,
    pub marker: EdgeMarker,
}

impl EdgeRecord {
    pub fn insert(src: VertexId, dst: VertexId, edge_type: EdgeType, ts: Timestamp) -> Self {
        Self {
            src,
            dst,
            edge_type,
            ts,
            marker: EdgeMarker::Insert,
        }
    }

    pub fn delete(src: VertexId, dst: VertexId, edge_type: EdgeType, ts: Timestamp) -> Self {
        Self {
            src,
            dst,
            edge_type,
            ts,
            marker: EdgeMarker::Delete,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[repr(i32)]
pub enum VertexLabel {
    Person = 1,
    Comment = 2,
    Post = 3,
    Forum = 4,
    Organisation = 5,
    Place = 6,
    Tag = 7,
    TagClass = 8,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[repr(i32)]
pub enum EdgeLabel {
    Knows = 1,
    HasCreator = 2,
    HasTag = 3,
    HasType = 4,
    IsLocatedIn = 5,
    IsPartOf = 6,
    LikesComment = 7,
    LikesPost = 8,
    ReplyOfComment = 9,
    ReplyOfPost = 10,
    ContainerOf = 11,
    HasMember = 12,
    HasModerator = 13,
    HasInterest = 14,
    StudyAt = 15,
    WorkAt = 16,
    IsSubclassOf = 17,
}

impl EdgeLabel {
    pub fn as_i32(self) -> i32 {
        self as i32
    }
}
