use serde::{Deserialize, Serialize};

pub type PersonId = u32;
pub type PostId = u32;
pub type CommentId = u32;
pub type ForumId = u32;
pub type TagId = u32;
pub type PlaceId = u32;
pub type OrgId = u32;
pub type TagClassId = u32;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[repr(u16)]
pub enum LabelId {
    Person = 1,
    Comment = 2,
    Post = 3,
    Forum = 4,
    Organisation = 5,
    Place = 6,
    Tag = 7,
    TagClass = 8,
}

impl LabelId {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Person => "Person",
            Self::Comment => "Comment",
            Self::Post => "Post",
            Self::Forum => "Forum",
            Self::Organisation => "Organisation",
            Self::Place => "Place",
            Self::Tag => "Tag",
            Self::TagClass => "TagClass",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum MessageId {
    Post(PostId),
    Comment(CommentId),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct GlobalId {
    pub label: u16,
    pub local: u32,
}

impl GlobalId {
    pub fn new(label: LabelId, local: u32) -> Self {
        Self {
            label: label as u16,
            local,
        }
    }
}
