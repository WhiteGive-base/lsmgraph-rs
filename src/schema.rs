use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufReader, BufWriter};
use std::path::{Path, PathBuf};

use anyhow::bail;
use serde::{Deserialize, Serialize};

use crate::error::Result;

pub type SchemaEpoch = u64;
pub type LabelId = i32;
pub type EdgeLabelId = i32;
pub type PropertyId = u32;
pub type EncodingVersion = u16;

pub const SCHEMA_CATALOG_FILE: &str = "SCHEMA_CATALOG.json";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SchemaCatalog {
    pub format: String,
    pub current_epoch: SchemaEpoch,
    pub vertex_labels: BTreeMap<LabelId, VertexLabelEntry>,
    pub edge_labels: BTreeMap<EdgeLabelId, EdgeLabelEntry>,
    pub properties: BTreeMap<PropertyId, PropertyEntry>,
    #[serde(default)]
    pub property_encoding_history: Vec<PropertyEncodingHistoryEntry>,
}

impl Default for SchemaCatalog {
    fn default() -> Self {
        Self::new()
    }
}

impl SchemaCatalog {
    pub fn new() -> Self {
        Self {
            format: "lsmgraph.schema_catalog".to_string(),
            current_epoch: 0,
            vertex_labels: BTreeMap::new(),
            edge_labels: BTreeMap::new(),
            properties: BTreeMap::new(),
            property_encoding_history: Vec::new(),
        }
    }

    pub fn add_vertex_label(&mut self, id: LabelId, name: impl Into<String>) -> SchemaEpoch {
        let epoch = self.next_epoch();
        self.vertex_labels.insert(
            id,
            VertexLabelEntry {
                id,
                name: name.into(),
                valid_from_epoch: epoch,
                valid_to_epoch: None,
                alias_of: None,
                dropped_at_epoch: None,
            },
        );
        epoch
    }

    pub fn add_edge_label(
        &mut self,
        id: EdgeLabelId,
        name: impl Into<String>,
        src_label_id: LabelId,
        dst_label_id: LabelId,
    ) -> SchemaEpoch {
        let epoch = self.next_epoch();
        self.edge_labels.insert(
            id,
            EdgeLabelEntry {
                id,
                name: name.into(),
                src_label_id,
                dst_label_id,
                valid_from_epoch: epoch,
                valid_to_epoch: None,
                alias_of: None,
                dropped_at_epoch: None,
            },
        );
        epoch
    }

    pub fn add_property(&mut self, entry: NewPropertyEntry) -> SchemaEpoch {
        let epoch = self.next_epoch();
        let history_entry = PropertyEncodingHistoryEntry {
            property_id: entry.id,
            encoding_epoch: epoch,
            logical_type: entry.logical_type.clone(),
            physical_encoding: entry.physical_encoding.clone(),
            encoding_version: entry.encoding_version,
            default_or_null_rule: entry.default_or_null_rule.clone(),
            valid_from_epoch: epoch,
            valid_to_epoch: None,
        };
        self.properties.insert(
            entry.id,
            PropertyEntry {
                id: entry.id,
                owner: entry.owner,
                name: entry.name,
                logical_type: entry.logical_type,
                physical_encoding: entry.physical_encoding,
                encoding_version: entry.encoding_version,
                encoding_epoch: epoch,
                default_or_null_rule: entry.default_or_null_rule,
                valid_from_epoch: epoch,
                valid_to_epoch: None,
                alias_of: None,
                dropped_at_epoch: None,
            },
        );
        self.property_encoding_history.push(history_entry);
        epoch
    }

    pub fn alias_vertex_label(
        &mut self,
        alias_id: LabelId,
        alias_name: impl Into<String>,
        canonical_id: LabelId,
    ) -> Result<SchemaEpoch> {
        if self.vertex_labels.contains_key(&alias_id) {
            bail!("vertex label id {alias_id} already exists");
        }
        let canonical_id = self.require_resolved_vertex_label(canonical_id)?;
        let epoch = self.next_epoch();
        self.vertex_labels.insert(
            alias_id,
            VertexLabelEntry {
                id: alias_id,
                name: alias_name.into(),
                valid_from_epoch: epoch,
                valid_to_epoch: None,
                alias_of: Some(canonical_id),
                dropped_at_epoch: None,
            },
        );
        Ok(epoch)
    }

    pub fn alias_edge_label(
        &mut self,
        alias_id: EdgeLabelId,
        alias_name: impl Into<String>,
        canonical_id: EdgeLabelId,
    ) -> Result<SchemaEpoch> {
        if self.edge_labels.contains_key(&alias_id) {
            bail!("edge label id {alias_id} already exists");
        }
        let canonical_id = self.require_resolved_edge_label(canonical_id)?;
        let canonical = self
            .edge_labels
            .get(&canonical_id)
            .expect("resolved edge label must exist")
            .clone();
        let epoch = self.next_epoch();
        self.edge_labels.insert(
            alias_id,
            EdgeLabelEntry {
                id: alias_id,
                name: alias_name.into(),
                src_label_id: canonical.src_label_id,
                dst_label_id: canonical.dst_label_id,
                valid_from_epoch: epoch,
                valid_to_epoch: None,
                alias_of: Some(canonical_id),
                dropped_at_epoch: None,
            },
        );
        Ok(epoch)
    }

    pub fn alias_property(
        &mut self,
        alias_id: PropertyId,
        alias_name: impl Into<String>,
        canonical_id: PropertyId,
    ) -> Result<SchemaEpoch> {
        if self.properties.contains_key(&alias_id) {
            bail!("property id {alias_id} already exists");
        }
        let canonical_id = self.require_resolved_property(canonical_id)?;
        let canonical = self
            .properties
            .get(&canonical_id)
            .expect("resolved property must exist")
            .clone();
        let epoch = self.next_epoch();
        self.properties.insert(
            alias_id,
            PropertyEntry {
                id: alias_id,
                owner: canonical.owner,
                name: alias_name.into(),
                logical_type: canonical.logical_type,
                physical_encoding: canonical.physical_encoding,
                encoding_version: canonical.encoding_version,
                encoding_epoch: canonical.encoding_epoch,
                default_or_null_rule: canonical.default_or_null_rule,
                valid_from_epoch: epoch,
                valid_to_epoch: None,
                alias_of: Some(canonical_id),
                dropped_at_epoch: None,
            },
        );
        Ok(epoch)
    }

    pub fn change_property_encoding(
        &mut self,
        id: PropertyId,
        logical_type: impl Into<String>,
        physical_encoding: impl Into<String>,
        encoding_version: EncodingVersion,
        default_or_null_rule: impl Into<String>,
    ) -> Result<SchemaEpoch> {
        let previous_epoch = self.current_epoch;
        let canonical_id = self.require_resolved_property(id)?;
        let ids_to_update: Vec<_> = self
            .properties
            .keys()
            .copied()
            .filter(|entry_id| {
                self.resolve_property(*entry_id, previous_epoch) == Some(canonical_id)
            })
            .collect();
        let logical_type = logical_type.into();
        let physical_encoding = physical_encoding.into();
        let default_or_null_rule = default_or_null_rule.into();
        let epoch = self.next_epoch();
        for history in &mut self.property_encoding_history {
            if history.property_id == canonical_id && history.valid_to_epoch.is_none() {
                history.valid_to_epoch = Some(epoch);
            }
        }
        self.property_encoding_history
            .push(PropertyEncodingHistoryEntry {
                property_id: canonical_id,
                encoding_epoch: epoch,
                logical_type: logical_type.clone(),
                physical_encoding: physical_encoding.clone(),
                encoding_version,
                default_or_null_rule: default_or_null_rule.clone(),
                valid_from_epoch: epoch,
                valid_to_epoch: None,
            });
        for entry_id in ids_to_update {
            if let Some(entry) = self.properties.get_mut(&entry_id) {
                entry.logical_type = logical_type.clone();
                entry.physical_encoding = physical_encoding.clone();
                entry.encoding_version = encoding_version;
                entry.encoding_epoch = epoch;
                entry.default_or_null_rule = default_or_null_rule.clone();
            }
        }
        Ok(epoch)
    }

    pub fn property_encoding_history_for(
        &self,
        property_id: PropertyId,
    ) -> Vec<&PropertyEncodingHistoryEntry> {
        self.property_encoding_history
            .iter()
            .filter(|entry| entry.property_id == property_id)
            .collect()
    }

    pub fn drop_vertex_label(&mut self, id: LabelId) -> Result<SchemaEpoch> {
        if !self.vertex_labels.contains_key(&id) {
            bail!("vertex label id {id} does not exist");
        }
        let previous_epoch = self.current_epoch;
        let canonical_id = self.resolve_vertex_label(id, previous_epoch);
        let drop_canonical = canonical_id == Some(id);
        let ids_to_drop: Vec<_> = self
            .vertex_labels
            .keys()
            .copied()
            .filter(|entry_id| {
                *entry_id == id
                    || (drop_canonical
                        && self.resolve_vertex_label(*entry_id, previous_epoch) == canonical_id)
            })
            .collect();
        Ok(self.mark_vertex_labels_dropped(ids_to_drop))
    }

    pub fn drop_edge_label(&mut self, id: EdgeLabelId) -> Result<SchemaEpoch> {
        if !self.edge_labels.contains_key(&id) {
            bail!("edge label id {id} does not exist");
        }
        let previous_epoch = self.current_epoch;
        let canonical_id = self.resolve_edge_label(id, previous_epoch);
        let drop_canonical = canonical_id == Some(id);
        let ids_to_drop: Vec<_> = self
            .edge_labels
            .keys()
            .copied()
            .filter(|entry_id| {
                *entry_id == id
                    || (drop_canonical
                        && self.resolve_edge_label(*entry_id, previous_epoch) == canonical_id)
            })
            .collect();
        Ok(self.mark_edge_labels_dropped(ids_to_drop))
    }

    pub fn drop_property(&mut self, id: PropertyId) -> Result<SchemaEpoch> {
        if !self.properties.contains_key(&id) {
            bail!("property id {id} does not exist");
        }
        let previous_epoch = self.current_epoch;
        let canonical_id = self.resolve_property(id, previous_epoch);
        let drop_canonical = canonical_id == Some(id);
        let ids_to_drop: Vec<_> = self
            .properties
            .keys()
            .copied()
            .filter(|entry_id| {
                *entry_id == id
                    || (drop_canonical
                        && self.resolve_property(*entry_id, previous_epoch) == canonical_id)
            })
            .collect();
        Ok(self.mark_properties_dropped(ids_to_drop))
    }

    pub fn resolve_vertex_label(&self, id: LabelId, epoch: SchemaEpoch) -> Option<LabelId> {
        let mut current = id;
        let mut seen = Vec::new();
        loop {
            if seen.contains(&current) {
                return None;
            }
            seen.push(current);
            let entry = self.vertex_labels.get(&current)?;
            if !entry.is_visible_at(epoch) {
                return None;
            }
            match entry.alias_of {
                Some(next) => current = next,
                None => return Some(current),
            }
        }
    }

    pub fn resolve_edge_label(&self, id: EdgeLabelId, epoch: SchemaEpoch) -> Option<EdgeLabelId> {
        let mut current = id;
        let mut seen = Vec::new();
        loop {
            if seen.contains(&current) {
                return None;
            }
            seen.push(current);
            let entry = self.edge_labels.get(&current)?;
            if !entry.is_visible_at(epoch) {
                return None;
            }
            match entry.alias_of {
                Some(next) => current = next,
                None => return Some(current),
            }
        }
    }

    pub fn resolve_property(&self, id: PropertyId, epoch: SchemaEpoch) -> Option<PropertyId> {
        let mut current = id;
        let mut seen = Vec::new();
        loop {
            if seen.contains(&current) {
                return None;
            }
            seen.push(current);
            let entry = self.properties.get(&current)?;
            if !entry.is_visible_at(epoch) {
                return None;
            }
            match entry.alias_of {
                Some(next) => current = next,
                None => return Some(current),
            }
        }
    }

    fn require_resolved_vertex_label(&self, id: LabelId) -> Result<LabelId> {
        self.resolve_vertex_label(id, self.current_epoch)
            .ok_or_else(|| anyhow::anyhow!("vertex label id {id} is not visible"))
    }

    fn require_resolved_edge_label(&self, id: EdgeLabelId) -> Result<EdgeLabelId> {
        self.resolve_edge_label(id, self.current_epoch)
            .ok_or_else(|| anyhow::anyhow!("edge label id {id} is not visible"))
    }

    fn require_resolved_property(&self, id: PropertyId) -> Result<PropertyId> {
        self.resolve_property(id, self.current_epoch)
            .ok_or_else(|| anyhow::anyhow!("property id {id} is not visible"))
    }

    fn mark_vertex_labels_dropped(&mut self, ids: Vec<LabelId>) -> SchemaEpoch {
        let epoch = self.next_epoch();
        for id in ids {
            if let Some(entry) = self.vertex_labels.get_mut(&id) {
                entry.valid_to_epoch.get_or_insert(epoch);
                entry.dropped_at_epoch.get_or_insert(epoch);
            }
        }
        epoch
    }

    fn mark_edge_labels_dropped(&mut self, ids: Vec<EdgeLabelId>) -> SchemaEpoch {
        let epoch = self.next_epoch();
        for id in ids {
            if let Some(entry) = self.edge_labels.get_mut(&id) {
                entry.valid_to_epoch.get_or_insert(epoch);
                entry.dropped_at_epoch.get_or_insert(epoch);
            }
        }
        epoch
    }

    fn mark_properties_dropped(&mut self, ids: Vec<PropertyId>) -> SchemaEpoch {
        let epoch = self.next_epoch();
        for id in ids {
            if let Some(entry) = self.properties.get_mut(&id) {
                entry.valid_to_epoch.get_or_insert(epoch);
                entry.dropped_at_epoch.get_or_insert(epoch);
            }
        }
        epoch
    }

    fn next_epoch(&mut self) -> SchemaEpoch {
        self.current_epoch = self.current_epoch.saturating_add(1);
        self.current_epoch
    }

    pub fn ensure_epoch_at_least(&mut self, epoch: SchemaEpoch) -> bool {
        if epoch > self.current_epoch {
            self.current_epoch = epoch;
            true
        } else {
            false
        }
    }

    pub fn path_for_store(store_dir: &Path) -> PathBuf {
        store_dir.join(SCHEMA_CATALOG_FILE)
    }

    pub fn load(path: &Path) -> Result<Self> {
        let file = File::open(path)?;
        Ok(serde_json::from_reader(BufReader::new(file))?)
    }

    pub fn save(&self, path: &Path) -> Result<()> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let file = File::create(path)?;
        serde_json::to_writer_pretty(BufWriter::new(file), self)?;
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VertexLabelEntry {
    pub id: LabelId,
    pub name: String,
    pub valid_from_epoch: SchemaEpoch,
    pub valid_to_epoch: Option<SchemaEpoch>,
    pub alias_of: Option<LabelId>,
    pub dropped_at_epoch: Option<SchemaEpoch>,
}

impl VertexLabelEntry {
    pub fn is_visible_at(&self, epoch: SchemaEpoch) -> bool {
        entry_is_visible_at(
            self.valid_from_epoch,
            self.valid_to_epoch,
            self.dropped_at_epoch,
            epoch,
        )
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EdgeLabelEntry {
    pub id: EdgeLabelId,
    pub name: String,
    pub src_label_id: LabelId,
    pub dst_label_id: LabelId,
    pub valid_from_epoch: SchemaEpoch,
    pub valid_to_epoch: Option<SchemaEpoch>,
    pub alias_of: Option<EdgeLabelId>,
    pub dropped_at_epoch: Option<SchemaEpoch>,
}

impl EdgeLabelEntry {
    pub fn is_visible_at(&self, epoch: SchemaEpoch) -> bool {
        entry_is_visible_at(
            self.valid_from_epoch,
            self.valid_to_epoch,
            self.dropped_at_epoch,
            epoch,
        )
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PropertyEntry {
    pub id: PropertyId,
    pub owner: PropertyOwner,
    pub name: String,
    pub logical_type: String,
    pub physical_encoding: String,
    pub encoding_version: EncodingVersion,
    #[serde(default)]
    pub encoding_epoch: SchemaEpoch,
    pub default_or_null_rule: String,
    pub valid_from_epoch: SchemaEpoch,
    pub valid_to_epoch: Option<SchemaEpoch>,
    pub alias_of: Option<PropertyId>,
    pub dropped_at_epoch: Option<SchemaEpoch>,
}

impl PropertyEntry {
    pub fn is_visible_at(&self, epoch: SchemaEpoch) -> bool {
        entry_is_visible_at(
            self.valid_from_epoch,
            self.valid_to_epoch,
            self.dropped_at_epoch,
            epoch,
        )
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NewPropertyEntry {
    pub id: PropertyId,
    pub owner: PropertyOwner,
    pub name: String,
    pub logical_type: String,
    pub physical_encoding: String,
    pub encoding_version: EncodingVersion,
    pub default_or_null_rule: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PropertyEncodingHistoryEntry {
    pub property_id: PropertyId,
    pub encoding_epoch: SchemaEpoch,
    pub logical_type: String,
    pub physical_encoding: String,
    pub encoding_version: EncodingVersion,
    pub default_or_null_rule: String,
    pub valid_from_epoch: SchemaEpoch,
    pub valid_to_epoch: Option<SchemaEpoch>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PropertyOwner {
    VertexLabel(LabelId),
    EdgeLabel(EdgeLabelId),
}

/// Strength of the set-valued evidence stored in a semantic summary.
///
/// Both `Exact` and `Conservative` can prove disjointness. `Unknown` cannot.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SemanticSummaryCompleteness {
    /// The recorded set equals the values represented by the segment.
    Exact,
    /// The recorded set is a safe over-approximation. It may contain false
    /// positives, but it omits no represented value; disjointness is safe.
    Conservative,
    /// No set-containment guarantee is available; semantic pruning is disabled.
    Unknown,
}

fn entry_is_visible_at(
    valid_from_epoch: SchemaEpoch,
    valid_to_epoch: Option<SchemaEpoch>,
    dropped_at_epoch: Option<SchemaEpoch>,
    epoch: SchemaEpoch,
) -> bool {
    if epoch < valid_from_epoch {
        return false;
    }
    if valid_to_epoch
        .map(|valid_to| epoch >= valid_to)
        .unwrap_or(false)
    {
        return false;
    }
    if dropped_at_epoch
        .map(|dropped_at| epoch >= dropped_at)
        .unwrap_or(false)
    {
        return false;
    }
    true
}

impl Default for SemanticSummaryCompleteness {
    fn default() -> Self {
        Self::Unknown
    }
}

impl SemanticSummaryCompleteness {
    /// Whether this summary can prove safe disjointness. This does not mean a
    /// conservative summary can prove membership or absence within its set.
    pub fn allows_semantic_pruning(self) -> bool {
        matches!(self, Self::Exact | Self::Conservative)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn schema_catalog_advances_epochs_for_schema_changes() {
        let mut catalog = SchemaCatalog::new();
        let person_epoch = catalog.add_vertex_label(1, "Person");
        let comment_epoch = catalog.add_vertex_label(2, "Comment");
        let edge_epoch = catalog.add_edge_label(1, "KNOWS", 1, 1);
        let property_epoch = catalog.add_property(NewPropertyEntry {
            id: 1,
            owner: PropertyOwner::VertexLabel(1),
            name: "reputation".to_string(),
            logical_type: "int64".to_string(),
            physical_encoding: "plain_i64".to_string(),
            encoding_version: 1,
            default_or_null_rule: "null".to_string(),
        });

        assert_eq!(person_epoch, 1);
        assert_eq!(comment_epoch, 2);
        assert_eq!(edge_epoch, 3);
        assert_eq!(property_epoch, 4);
        assert_eq!(catalog.current_epoch, 4);
        assert_eq!(catalog.edge_labels.get(&1).unwrap().valid_from_epoch, 3);
        assert_eq!(catalog.properties.get(&1).unwrap().valid_from_epoch, 4);
        assert_eq!(catalog.properties.get(&1).unwrap().encoding_epoch, 4);
        assert_eq!(catalog.property_encoding_history.len(), 1);
        assert_eq!(catalog.property_encoding_history[0].property_id, 1);
        assert_eq!(catalog.property_encoding_history[0].encoding_epoch, 4);
    }

    #[test]
    fn schema_catalog_can_be_bootstrapped_to_external_epoch() {
        let mut catalog = SchemaCatalog::new();
        assert!(catalog.ensure_epoch_at_least(7));
        assert_eq!(catalog.current_epoch, 7);
        assert!(!catalog.ensure_epoch_at_least(3));
        assert_eq!(catalog.current_epoch, 7);
    }

    #[test]
    fn schema_catalog_aliases_resolve_by_epoch() {
        let mut catalog = SchemaCatalog::new();
        let canonical_epoch = catalog.add_edge_label(1, "KNOWS", 10, 10);
        let alias_epoch = catalog
            .alias_edge_label(2, "FOLLOWS", 1)
            .expect("alias should resolve canonical edge label");

        assert_eq!(canonical_epoch, 1);
        assert_eq!(alias_epoch, 2);
        assert_eq!(catalog.resolve_edge_label(1, 1), Some(1));
        assert_eq!(catalog.resolve_edge_label(2, 1), None);
        assert_eq!(catalog.resolve_edge_label(2, 2), Some(1));
        assert_eq!(catalog.edge_labels.get(&2).unwrap().alias_of, Some(1));
    }

    #[test]
    fn schema_catalog_drop_alias_preserves_canonical() {
        let mut catalog = SchemaCatalog::new();
        catalog.add_property(NewPropertyEntry {
            id: 1,
            owner: PropertyOwner::EdgeLabel(7),
            name: "strength".to_string(),
            logical_type: "int64".to_string(),
            physical_encoding: "plain_i64".to_string(),
            encoding_version: 1,
            default_or_null_rule: "null".to_string(),
        });
        catalog
            .alias_property(2, "weight", 1)
            .expect("property alias should resolve");
        let drop_epoch = catalog
            .drop_property(2)
            .expect("dropping alias should succeed");

        assert_eq!(drop_epoch, 3);
        assert_eq!(catalog.resolve_property(2, 2), Some(1));
        assert_eq!(catalog.resolve_property(2, 3), None);
        assert_eq!(catalog.resolve_property(1, 3), Some(1));
        assert_eq!(
            catalog.properties.get(&2).unwrap().dropped_at_epoch,
            Some(3)
        );
    }

    #[test]
    fn schema_catalog_drop_canonical_hides_aliases() {
        let mut catalog = SchemaCatalog::new();
        catalog.add_vertex_label(1, "Person");
        catalog
            .alias_vertex_label(2, "Creator", 1)
            .expect("vertex alias should resolve");
        let drop_epoch = catalog
            .drop_vertex_label(1)
            .expect("dropping canonical should succeed");

        assert_eq!(drop_epoch, 3);
        assert_eq!(catalog.resolve_vertex_label(1, 2), Some(1));
        assert_eq!(catalog.resolve_vertex_label(2, 2), Some(1));
        assert_eq!(catalog.resolve_vertex_label(1, 3), None);
        assert_eq!(catalog.resolve_vertex_label(2, 3), None);
        assert_eq!(
            catalog.vertex_labels.get(&2).unwrap().dropped_at_epoch,
            Some(3)
        );
    }

    #[test]
    fn schema_catalog_property_encoding_change_updates_visible_aliases() {
        let mut catalog = SchemaCatalog::new();
        catalog.add_property(NewPropertyEntry {
            id: 1,
            owner: PropertyOwner::EdgeLabel(7),
            name: "strength".to_string(),
            logical_type: "int64".to_string(),
            physical_encoding: "plain_i64".to_string(),
            encoding_version: 1,
            default_or_null_rule: "null".to_string(),
        });
        catalog
            .alias_property(2, "weight", 1)
            .expect("property alias should resolve");

        let encoding_epoch = catalog
            .change_property_encoding(2, "float64", "plain_f64", 2, "null")
            .expect("encoding change through alias should update canonical property");

        assert_eq!(encoding_epoch, 3);
        assert_eq!(catalog.resolve_property(2, 3), Some(1));
        assert_eq!(catalog.properties.get(&1).unwrap().logical_type, "float64");
        assert_eq!(
            catalog.properties.get(&1).unwrap().physical_encoding,
            "plain_f64"
        );
        assert_eq!(catalog.properties.get(&1).unwrap().encoding_version, 2);
        assert_eq!(catalog.properties.get(&1).unwrap().encoding_epoch, 3);
        assert_eq!(
            catalog.properties.get(&2).unwrap().physical_encoding,
            "plain_f64"
        );
        assert_eq!(catalog.properties.get(&2).unwrap().encoding_epoch, 3);
        let history = catalog.property_encoding_history_for(1);
        assert_eq!(history.len(), 2);
        assert_eq!(history[0].encoding_epoch, 1);
        assert_eq!(history[0].valid_to_epoch, Some(3));
        assert_eq!(history[1].encoding_epoch, 3);
        assert_eq!(history[1].valid_to_epoch, None);
    }
}
