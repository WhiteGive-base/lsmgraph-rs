use serde::{Deserialize, Serialize};

use crate::schema::PropertyId;
use crate::types::{source_label_from_vertex_id, EdgeType, VertexId, UNKNOWN_SOURCE_LABEL};

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EdgeDirection {
    Unknown,
    Out,
    In,
    Both,
}

impl Default for EdgeDirection {
    fn default() -> Self {
        Self::Unknown
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DegreeClass {
    Unknown,
    Low,
    Medium,
    High,
    Mixed,
}

impl Default for DegreeClass {
    fn default() -> Self {
        Self::Unknown
    }
}

impl DegreeClass {
    pub fn from_max_degree(max_degree: u64) -> Self {
        match max_degree {
            0 => Self::Unknown,
            1..=16 => Self::Low,
            17..=1024 => Self::Medium,
            _ => Self::High,
        }
    }

    pub fn may_contain_global_query(self, query_degree_class: Self) -> bool {
        match (self, query_degree_class) {
            (Self::Mixed, _) | (Self::Unknown, _) | (_, Self::Mixed) | (_, Self::Unknown) => true,
            (Self::Low, Self::Low | Self::Medium | Self::High) => true,
            (Self::Medium, Self::Medium | Self::High) => true,
            (Self::High, Self::High) => true,
            _ => false,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SegmentSortKey {
    Unknown,
    SrcEdgeDstTs,
    SrcTsDesc,
}

impl Default for SegmentSortKey {
    fn default() -> Self {
        Self::Unknown
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct GraphAccessSignature {
    pub src: VertexId,
    pub src_label: i32,
    pub edge_type: Option<EdgeType>,
    pub direction: EdgeDirection,
    pub degree_class: Option<DegreeClass>,
    pub dst_label: Option<i32>,
    pub min_ts: Option<u64>,
    pub max_ts: Option<u64>,
    pub property_predicate: Option<PropertyPredicate>,
}

impl GraphAccessSignature {
    pub fn neighbor_scan(src: VertexId, edge_type: Option<EdgeType>) -> Self {
        Self {
            src,
            src_label: source_label_from_vertex_id(src),
            edge_type,
            direction: EdgeDirection::Out,
            degree_class: None,
            dst_label: None,
            min_ts: None,
            max_ts: None,
            property_predicate: None,
        }
    }

    pub fn with_degree_class(mut self, degree_class: DegreeClass) -> Self {
        self.degree_class = Some(degree_class);
        self
    }

    pub fn without_degree_class(mut self) -> Self {
        self.degree_class = None;
        self
    }

    pub fn with_dst_label(mut self, dst_label: i32) -> Self {
        self.dst_label = Some(dst_label);
        self
    }

    pub fn with_required_property(mut self, property_id: PropertyId) -> Self {
        self.property_predicate = Some(PropertyPredicate::RequiredPresent { property_id });
        self
    }

    pub fn with_absent_or_default_property(mut self, property_id: PropertyId) -> Self {
        self.property_predicate = Some(PropertyPredicate::AbsentOrDefault { property_id });
        self
    }

    pub fn label_matches(&self, segment_src_label: i32) -> bool {
        segment_src_label == UNKNOWN_SOURCE_LABEL
            || self.src_label == UNKNOWN_SOURCE_LABEL
            || segment_src_label == self.src_label
    }

    pub fn direction_matches(&self, segment_direction: EdgeDirection) -> bool {
        matches!(
            (self.direction, segment_direction),
            (EdgeDirection::Unknown, _)
                | (_, EdgeDirection::Unknown)
                | (EdgeDirection::Both, _)
                | (_, EdgeDirection::Both)
        ) || self.direction == segment_direction
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum PropertyPredicate {
    RequiredPresent { property_id: PropertyId },
    AbsentOrDefault { property_id: PropertyId },
}
