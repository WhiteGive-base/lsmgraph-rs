use std::sync::Arc;

use lsmgraph::config::{L0LayoutPolicy, LsmGraphConfig};
use lsmgraph::csr::{
    CsrPropertyValuePredicate, CsrWriter, EdgePropertyValue, EdgeRecordWithProperties, Manifest,
    ManifestRecord,
};
use lsmgraph::graph::Engine;
use lsmgraph::io::BlockingPreadBackend;
use lsmgraph::metrics::Metrics;
use lsmgraph::property_encoding::PropertyValue;
use lsmgraph::types::{EdgeLabel, EdgeRecord, VertexId, MIXED_EDGE_TYPE, UNKNOWN_SOURCE_LABEL};
use lsmgraph::{
    DegreeClass, GraphAccessSignature, NewPropertyEntry, PropertyOwner, SchemaCatalog,
    SemanticSummaryCompleteness, VertexLabel,
};
use serde_json::Value;

fn encoded(label: VertexLabel, local: u64) -> VertexId {
    ((label as u64) << 56) | local
}

fn target_tempdir(name: &str) -> anyhow::Result<tempfile::TempDir> {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("target")
        .join("test-tmp");
    std::fs::create_dir_all(&root)?;
    Ok(tempfile::Builder::new().prefix(name).tempdir_in(root)?)
}

fn strip_manifest_property_summary_fields(store_dir: &std::path::Path) -> anyhow::Result<()> {
    let path = store_dir.join("MANIFEST");
    let manifest = std::fs::read_to_string(&path)?;
    let mut rewritten = String::new();
    for line in manifest.lines() {
        if line.trim().is_empty() {
            continue;
        }
        let mut value: Value = serde_json::from_str(line)?;
        if value.get("op").and_then(Value::as_str) == Some("CreateFile") {
            if let Some(meta) = value.get_mut("meta").and_then(Value::as_object_mut) {
                meta.remove("property_summary_completeness");
                meta.remove("property_presence_bitmap");
            }
        }
        rewritten.push_str(&serde_json::to_string(&value)?);
        rewritten.push('\n');
    }
    std::fs::write(path, rewritten)?;
    Ok(())
}

async fn record_feedback_queries(
    engine: &Engine,
    src: VertexId,
    edge_type: i32,
    expected_neighbors: usize,
    repeats: usize,
) -> anyhow::Result<u64> {
    engine.metrics().reset();
    for _ in 0..repeats {
        let neighbors = engine
            .get_neighbors_typed(src, edge_type, engine.current_snapshot())
            .await?;
        assert_eq!(
            neighbors.len(),
            expected_neighbors,
            "test workload should keep a stable result set while collecting feedback"
        );
    }
    Ok(
        engine.metrics().snapshot_json()["csr"]["candidate_l0_segments"]
            .as_u64()
            .unwrap_or_default(),
    )
}

async fn assert_property_snapshot_state(
    engine: &Engine,
    src: VertexId,
    edge_type: i32,
    property_id: u32,
    snapshot: u64,
    expected_visible_edges: usize,
    context: &str,
) -> anyhow::Result<()> {
    let typed = engine.get_neighbors_typed(src, edge_type, snapshot).await?;
    assert_eq!(
        typed.len(),
        expected_visible_edges,
        "{context}: typed read should match expected visible edges"
    );

    let required = engine
        .get_neighbors_by_signature(
            GraphAccessSignature::neighbor_scan(src, Some(edge_type))
                .with_required_property(property_id),
            snapshot,
        )
        .await?;
    assert!(
        required.is_empty(),
        "{context}: required-property predicate should reject exact-absent records"
    );

    let absent_or_default = engine
        .get_neighbors_by_signature(
            GraphAccessSignature::neighbor_scan(src, Some(edge_type))
                .with_absent_or_default_property(property_id),
            snapshot,
        )
        .await?;
    assert_eq!(
        absent_or_default.len(),
        expected_visible_edges,
        "{context}: absent/default predicate should keep exact-absent records"
    );

    assert_eq!(
        engine.scan_edges(snapshot).await?.len(),
        expected_visible_edges,
        "{context}: scan_edges should agree with typed visibility"
    );
    Ok(())
}

fn feedback_compaction_config(store_dir: &std::path::Path) -> LsmGraphConfig {
    let mut config = LsmGraphConfig::new(store_dir)
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(1)
        .with_semantic_budget_min_exact_bytes(1)
        .with_semantic_budget_min_benefit_score(0.0);
    config.l0_ra_range_bucket_size = 256;
    config.l0_ra_min_queries = 2;
    config.l0_ra_min_l0_segments = 2;
    config.l0_ra_min_score = 0.0;
    config
}

#[tokio::test]
async fn insert_flush_reopen_and_scan() -> anyhow::Result<()> {
    let tmp = target_tempdir("insert-flush-reopen-and-scan-")?;
    let config = LsmGraphConfig::new(tmp.path()).with_memgraph_capacity(256);
    let engine = Engine::create(config.clone()).await?;
    engine.insert_edge(1, 2, EdgeLabel::Knows.as_i32()).await?;
    engine.insert_edge(1, 3, EdgeLabel::Knows.as_i32()).await?;
    engine.insert_edge(2, 1, EdgeLabel::Knows.as_i32()).await?;
    engine.flush_active().await?;

    let reopened = Engine::open(config).await?;
    let snapshot = reopened.current_snapshot();
    let n = reopened.get_neighbors(1, snapshot).await?;
    let dsts: Vec<_> = n.into_iter().map(|e| e.dst).collect();
    assert_eq!(dsts, vec![2, 3]);
    assert_eq!(reopened.scan_edges(snapshot).await?.len(), 3);
    Ok(())
}

#[tokio::test]
async fn delete_tombstone_hides_latest_edge() -> anyhow::Result<()> {
    let tmp = target_tempdir("delete-tombstone-hides-latest-edge-")?;
    let engine =
        Engine::create(LsmGraphConfig::new(tmp.path()).with_memgraph_capacity(256)).await?;
    engine.insert_edge(1, 2, EdgeLabel::Knows.as_i32()).await?;
    engine.delete_edge(1, 2, EdgeLabel::Knows.as_i32()).await?;
    engine.flush_active().await?;
    let n = engine.get_neighbors(1, engine.current_snapshot()).await?;
    assert!(n.is_empty());
    Ok(())
}

#[tokio::test]
async fn snapshot_read_sees_insert_before_later_delete_across_reopen() -> anyhow::Result<()> {
    let tmp = target_tempdir("snapshot-read-before-delete-reopen-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(1)
        .with_semantic_budget_min_exact_bytes(1);
    let engine = Engine::create(config.clone()).await?;
    let src = encoded(VertexLabel::Person, 1_900);
    let dst = encoded(VertexLabel::Person, 1_901);
    let edge_type = EdgeLabel::Knows.as_i32();

    let insert_snapshot = engine.insert_edge(src, dst, edge_type).await?;
    engine.flush_active().await?;
    let delete_snapshot = engine.delete_edge(src, dst, edge_type).await?;
    engine.flush_active().await?;

    let before_delete = engine
        .get_neighbors_typed(src, edge_type, insert_snapshot)
        .await?;
    assert_eq!(
        before_delete.len(),
        1,
        "the insert should remain visible at the snapshot before the delete"
    );
    let after_delete = engine
        .get_neighbors_typed(src, edge_type, delete_snapshot)
        .await?;
    assert!(
        after_delete.is_empty(),
        "the tombstone should hide the edge at the delete snapshot"
    );
    assert_eq!(engine.scan_edges(insert_snapshot).await?.len(), 1);
    assert!(engine.scan_edges(delete_snapshot).await?.is_empty());
    drop(engine);

    let reopened = Engine::open(config).await?;
    let reopened_before_delete = reopened
        .get_neighbors_typed(src, edge_type, insert_snapshot)
        .await?;
    assert_eq!(reopened_before_delete.len(), 1);
    let reopened_after_delete = reopened
        .get_neighbors_typed(src, edge_type, reopened.current_snapshot())
        .await?;
    assert!(reopened_after_delete.is_empty());
    Ok(())
}

#[tokio::test]
async fn compaction_preserves_old_snapshot_after_tombstone() -> anyhow::Result<()> {
    let tmp = target_tempdir("compaction-preserves-old-snapshot-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(1)
        .with_semantic_budget_min_exact_bytes(1);
    let engine = Engine::create(config.clone()).await?;
    let src = encoded(VertexLabel::Person, 1_920);
    let dst = encoded(VertexLabel::Person, 1_921);
    let edge_type = EdgeLabel::Knows.as_i32();

    let insert_snapshot = engine.insert_edge(src, dst, edge_type).await?;
    engine.flush_active().await?;
    let delete_snapshot = engine.delete_edge(src, dst, edge_type).await?;
    engine.flush_active().await?;

    let output = engine
        .compact_l0_to_l1()
        .await?
        .expect("forced compaction should produce an L1 segment");
    assert_eq!(
        output.edge_count, 2,
        "snapshot-correct compaction must retain both insert and tombstone versions"
    );

    let before_delete = engine
        .get_neighbors_typed(src, edge_type, insert_snapshot)
        .await?;
    assert_eq!(
        before_delete.len(),
        1,
        "old snapshot should remain readable after compaction"
    );
    let after_delete = engine
        .get_neighbors_typed(src, edge_type, delete_snapshot)
        .await?;
    assert!(
        after_delete.is_empty(),
        "current snapshot should still apply the tombstone after compaction"
    );
    assert_eq!(engine.scan_edges(insert_snapshot).await?.len(), 1);
    assert!(engine.scan_edges(delete_snapshot).await?.is_empty());
    drop(engine);

    let reopened = Engine::open(config).await?;
    let reopened_before_delete = reopened
        .get_neighbors_typed(src, edge_type, insert_snapshot)
        .await?;
    assert_eq!(
        reopened_before_delete.len(),
        1,
        "old snapshot should survive manifest replay after compaction"
    );
    let reopened_after_delete = reopened
        .get_neighbors_typed(src, edge_type, reopened.current_snapshot())
        .await?;
    assert!(reopened_after_delete.is_empty());
    Ok(())
}

#[tokio::test]
async fn degree_change_tombstones_keep_explicit_low_degree_query_conservative() -> anyhow::Result<()>
{
    let tmp = target_tempdir("degree-change-tombstones-keep-low-query-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(1)
        .with_semantic_budget_min_exact_bytes(1);
    let engine = Engine::create(config.clone()).await?;
    let src = encoded(VertexLabel::Person, 1_940);
    let edge_type = EdgeLabel::Knows.as_i32();

    for i in 0..17u64 {
        engine
            .insert_edge(src, encoded(VertexLabel::Person, 2_000 + i), edge_type)
            .await?;
    }
    let medium_snapshot = engine.current_snapshot();
    engine.flush_active().await?;

    for i in 0..16u64 {
        engine
            .delete_edge(src, encoded(VertexLabel::Person, 2_000 + i), edge_type)
            .await?;
    }
    let low_snapshot = engine.current_snapshot();
    engine.flush_active().await?;

    let guard = engine.version_guard();
    let l0 = &guard.version().levels[0];
    assert!(
        l0.iter()
            .any(|meta| meta.degree_class == DegreeClass::Medium && meta.degree_class_exact),
        "test setup should create a Medium insert segment"
    );
    assert!(
        l0.iter()
            .any(|meta| meta.degree_class == DegreeClass::Low && meta.degree_class_exact),
        "test setup should create a Low tombstone segment"
    );
    drop(guard);

    let medium_result = engine
        .get_neighbors_by_signature(
            GraphAccessSignature::neighbor_scan(src, Some(edge_type))
                .with_degree_class(DegreeClass::Medium),
            medium_snapshot,
        )
        .await?;
    assert_eq!(
        medium_result.len(),
        17,
        "medium snapshot should see all inserted edges before tombstones"
    );

    engine.metrics().reset();
    let low_result = engine
        .get_neighbors_by_signature(
            GraphAccessSignature::neighbor_scan(src, Some(edge_type))
                .with_degree_class(DegreeClass::Low),
            low_snapshot,
        )
        .await?;
    assert_eq!(
        low_result.len(),
        1,
        "degree-changing tombstones must not let a Low hint prune the older Medium insert segment"
    );
    let low_candidates = engine.metrics().snapshot_json()["csr"]["candidate_l0_segments"]
        .as_u64()
        .unwrap_or_default();
    assert!(
        low_candidates >= 2,
        "degree invalidation should conservatively probe both insert and tombstone L0 segments"
    );

    let scan = engine.scan_edges(low_snapshot).await?;
    assert_eq!(scan.len(), 1);
    assert_eq!(scan[0].dst, encoded(VertexLabel::Person, 2_016));
    drop(engine);

    let reopened = Engine::open(config).await?;
    let reopened_low = reopened
        .get_neighbors_by_signature(
            GraphAccessSignature::neighbor_scan(src, Some(edge_type))
                .with_degree_class(DegreeClass::Low),
            reopened.current_snapshot(),
        )
        .await?;
    assert_eq!(
        reopened_low.len(),
        1,
        "rebuild-time semantic degree directory should preserve conservative degree behavior"
    );
    Ok(())
}

#[tokio::test]
async fn schema_epoch_snapshot_mixed_delta_survives_compaction_and_reopen() -> anyhow::Result<()> {
    let tmp = target_tempdir("schema-epoch-snapshot-mixed-delta-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(1)
        .with_semantic_budget_min_exact_bytes(1);
    let engine = Engine::create(config.clone()).await?;
    let src = encoded(VertexLabel::Person, 1_960);
    let old_dst = encoded(VertexLabel::Person, 1_961);
    let new_dst = encoded(VertexLabel::Comment, 1_962);
    let old_edge_type = EdgeLabel::Knows.as_i32();
    let new_edge_type = 101;

    assert_eq!(engine.current_schema_epoch(), 0);
    let old_snapshot = engine.insert_edge(src, old_dst, old_edge_type).await?;
    engine.flush_active().await?;

    let new_edge_epoch = engine.add_schema_edge_label(
        new_edge_type,
        "P4_REACTS_TO",
        VertexLabel::Person as i32,
        VertexLabel::Comment as i32,
    )?;
    assert_eq!(new_edge_epoch, 1);
    assert_eq!(engine.current_schema_epoch(), 1);

    let new_snapshot = engine.insert_edge(src, new_dst, new_edge_type).await?;
    engine.flush_active().await?;
    let delete_snapshot = engine.delete_edge(src, old_dst, old_edge_type).await?;
    engine.flush_active().await?;

    {
        let guard = engine.version_guard();
        let l0 = &guard.version().levels[0];
        assert!(
            l0.iter()
                .any(|meta| meta.schema_epoch == 0 && meta.edge_type_partition == old_edge_type),
            "the old-label segment should keep schema epoch 0"
        );
        assert!(
            l0.iter()
                .any(|meta| meta.schema_epoch == 1 && meta.edge_type_partition == new_edge_type),
            "the new-label segment should use schema epoch 1"
        );
        assert!(
            l0.iter()
                .any(|meta| meta.schema_epoch == 1 && meta.edge_type_partition == old_edge_type),
            "the tombstone segment should be written under the current schema epoch"
        );
    }

    let old_at_old = engine
        .get_neighbors_typed(src, old_edge_type, old_snapshot)
        .await?;
    let new_at_old = engine
        .get_neighbors_typed(src, new_edge_type, old_snapshot)
        .await?;
    assert_eq!(old_at_old.len(), 1);
    assert!(new_at_old.is_empty());
    assert_eq!(engine.scan_edges(old_snapshot).await?.len(), 1);

    let old_at_new = engine
        .get_neighbors_typed(src, old_edge_type, new_snapshot)
        .await?;
    let new_at_new = engine
        .get_neighbors_typed(src, new_edge_type, new_snapshot)
        .await?;
    assert_eq!(old_at_new.len(), 1);
    assert_eq!(new_at_new.len(), 1);
    assert_eq!(engine.scan_edges(new_snapshot).await?.len(), 2);

    let old_after_delete = engine
        .get_neighbors_typed(src, old_edge_type, delete_snapshot)
        .await?;
    let new_after_delete = engine
        .get_neighbors_typed(src, new_edge_type, delete_snapshot)
        .await?;
    assert!(old_after_delete.is_empty());
    assert_eq!(new_after_delete.len(), 1);
    assert_eq!(new_after_delete[0].dst, new_dst);
    assert_eq!(engine.scan_edges(delete_snapshot).await?.len(), 1);

    let output = engine
        .compact_l0_to_l1()
        .await?
        .expect("forced compaction should produce an L1 segment");
    assert_eq!(
        output.edge_count, 3,
        "schema/snapshot compaction must retain old insert, new insert, and old tombstone"
    );

    let old_at_old_after_compaction = engine
        .get_neighbors_typed(src, old_edge_type, old_snapshot)
        .await?;
    let new_at_old_after_compaction = engine
        .get_neighbors_typed(src, new_edge_type, old_snapshot)
        .await?;
    assert_eq!(old_at_old_after_compaction.len(), 1);
    assert!(new_at_old_after_compaction.is_empty());
    assert_eq!(engine.scan_edges(old_snapshot).await?.len(), 1);

    let old_at_new_after_compaction = engine
        .get_neighbors_typed(src, old_edge_type, new_snapshot)
        .await?;
    let new_at_new_after_compaction = engine
        .get_neighbors_typed(src, new_edge_type, new_snapshot)
        .await?;
    assert_eq!(old_at_new_after_compaction.len(), 1);
    assert_eq!(new_at_new_after_compaction.len(), 1);
    assert_eq!(engine.scan_edges(new_snapshot).await?.len(), 2);

    let old_after_delete_compacted = engine
        .get_neighbors_typed(src, old_edge_type, delete_snapshot)
        .await?;
    let new_after_delete_compacted = engine
        .get_neighbors_typed(src, new_edge_type, delete_snapshot)
        .await?;
    assert!(old_after_delete_compacted.is_empty());
    assert_eq!(new_after_delete_compacted.len(), 1);
    assert_eq!(new_after_delete_compacted[0].dst, new_dst);
    assert_eq!(engine.scan_edges(delete_snapshot).await?.len(), 1);
    drop(engine);

    let reopened = Engine::open(config).await?;
    assert_eq!(reopened.current_schema_epoch(), 1);
    assert_eq!(
        reopened
            .schema_catalog_snapshot()
            .edge_labels
            .get(&new_edge_type)
            .map(|entry| entry.name.as_str()),
        Some("P4_REACTS_TO")
    );

    let reopened_old_at_old = reopened
        .get_neighbors_typed(src, old_edge_type, old_snapshot)
        .await?;
    let reopened_new_at_old = reopened
        .get_neighbors_typed(src, new_edge_type, old_snapshot)
        .await?;
    assert_eq!(reopened_old_at_old.len(), 1);
    assert!(reopened_new_at_old.is_empty());
    assert_eq!(reopened.scan_edges(old_snapshot).await?.len(), 1);

    let reopened_old_at_new = reopened
        .get_neighbors_typed(src, old_edge_type, new_snapshot)
        .await?;
    let reopened_new_at_new = reopened
        .get_neighbors_typed(src, new_edge_type, new_snapshot)
        .await?;
    assert_eq!(reopened_old_at_new.len(), 1);
    assert_eq!(reopened_new_at_new.len(), 1);
    assert_eq!(reopened.scan_edges(new_snapshot).await?.len(), 2);

    let reopened_old_after_delete = reopened
        .get_neighbors_typed(src, old_edge_type, delete_snapshot)
        .await?;
    let reopened_new_after_delete = reopened
        .get_neighbors_typed(src, new_edge_type, delete_snapshot)
        .await?;
    assert!(reopened_old_after_delete.is_empty());
    assert_eq!(reopened_new_after_delete.len(), 1);
    assert_eq!(reopened_new_after_delete[0].dst, new_dst);
    assert_eq!(reopened.scan_edges(delete_snapshot).await?.len(), 1);
    Ok(())
}

#[tokio::test]
async fn property_schema_snapshot_mixed_delta_survives_compaction_and_reopen() -> anyhow::Result<()>
{
    let tmp = target_tempdir("property-schema-snapshot-mixed-delta-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(1)
        .with_semantic_budget_min_exact_bytes(1);
    let engine = Engine::create(config.clone()).await?;
    let src = encoded(VertexLabel::Person, 1_980);
    let old_dst = encoded(VertexLabel::Person, 1_981);
    let new_dst = encoded(VertexLabel::Person, 1_982);
    let edge_type = EdgeLabel::Knows.as_i32();
    let property_id = 9;

    let old_snapshot = engine.insert_edge(src, old_dst, edge_type).await?;
    engine.flush_active().await?;

    let property_epoch = engine.add_schema_property(NewPropertyEntry {
        id: property_id,
        owner: PropertyOwner::EdgeLabel(edge_type),
        name: "p4_strength".to_string(),
        logical_type: "int64".to_string(),
        physical_encoding: "plain_i64".to_string(),
        encoding_version: 1,
        default_or_null_rule: "null".to_string(),
    })?;
    assert_eq!(property_epoch, 1);
    assert_eq!(engine.current_schema_epoch(), 1);

    let new_snapshot = engine.insert_edge(src, new_dst, edge_type).await?;
    engine.flush_active().await?;
    let delete_snapshot = engine.delete_edge(src, old_dst, edge_type).await?;
    engine.flush_active().await?;

    {
        let guard = engine.version_guard();
        let l0 = &guard.version().levels[0];
        assert!(
            l0.iter().any(|meta| meta.schema_epoch == 0
                && meta.property_summary_completeness == SemanticSummaryCompleteness::Exact
                && meta.definitely_lacks_property(property_id)
                && !meta.may_contain_tombstones),
            "pre-property insert segment should remain exact-absent and non-tombstone"
        );
        assert!(
            l0.iter().any(|meta| meta.schema_epoch == 1
                && meta.property_summary_completeness == SemanticSummaryCompleteness::Exact
                && meta.definitely_lacks_property(property_id)
                && !meta.may_contain_tombstones),
            "post-property insert without inline values should remain exact-absent"
        );
        assert!(
            l0.iter().any(|meta| meta.schema_epoch == 1
                && meta.property_summary_completeness == SemanticSummaryCompleteness::Exact
                && meta.definitely_lacks_property(property_id)
                && meta.may_contain_tombstones),
            "post-property tombstone segment should be exact-absent but retained for delta safety"
        );
    }

    assert_property_snapshot_state(
        &engine,
        src,
        edge_type,
        property_id,
        old_snapshot,
        1,
        "before compaction old_snapshot",
    )
    .await?;
    assert_property_snapshot_state(
        &engine,
        src,
        edge_type,
        property_id,
        new_snapshot,
        2,
        "before compaction new_snapshot",
    )
    .await?;

    engine.metrics().reset();
    let required_after_delete = engine
        .get_neighbors_by_signature(
            GraphAccessSignature::neighbor_scan(src, Some(edge_type))
                .with_required_property(property_id),
            delete_snapshot,
        )
        .await?;
    assert!(required_after_delete.is_empty());
    let required_candidates = engine.metrics().snapshot_json()["csr"]["candidate_l0_segments"]
        .as_u64()
        .unwrap_or_default();
    assert!(
        required_candidates >= 1,
        "required-property pruning should still read the exact-absent tombstone segment"
    );
    assert_property_snapshot_state(
        &engine,
        src,
        edge_type,
        property_id,
        delete_snapshot,
        1,
        "before compaction delete_snapshot",
    )
    .await?;

    let output = engine
        .compact_l0_to_l1()
        .await?
        .expect("forced compaction should produce an L1 segment");
    assert_eq!(
        output.edge_count, 3,
        "property/schema compaction must retain old insert, new insert, and old tombstone"
    );
    assert!(
        output.may_contain_tombstones,
        "compacted history should remember that it contains tombstones"
    );
    assert!(
        output.definitely_lacks_property(property_id),
        "current prototype stores no inline property values, so the compacted segment is exact-absent"
    );

    assert_property_snapshot_state(
        &engine,
        src,
        edge_type,
        property_id,
        old_snapshot,
        1,
        "after compaction old_snapshot",
    )
    .await?;
    assert_property_snapshot_state(
        &engine,
        src,
        edge_type,
        property_id,
        new_snapshot,
        2,
        "after compaction new_snapshot",
    )
    .await?;
    assert_property_snapshot_state(
        &engine,
        src,
        edge_type,
        property_id,
        delete_snapshot,
        1,
        "after compaction delete_snapshot",
    )
    .await?;
    drop(engine);

    let reopened = Engine::open(config).await?;
    assert_eq!(reopened.current_schema_epoch(), 1);
    assert_eq!(
        reopened
            .schema_catalog_snapshot()
            .properties
            .get(&property_id)
            .map(|entry| entry.default_or_null_rule.as_str()),
        Some("null")
    );

    assert_property_snapshot_state(
        &reopened,
        src,
        edge_type,
        property_id,
        old_snapshot,
        1,
        "after reopen old_snapshot",
    )
    .await?;
    assert_property_snapshot_state(
        &reopened,
        src,
        edge_type,
        property_id,
        new_snapshot,
        2,
        "after reopen new_snapshot",
    )
    .await?;
    assert_property_snapshot_state(
        &reopened,
        src,
        edge_type,
        property_id,
        delete_snapshot,
        1,
        "after reopen delete_snapshot",
    )
    .await?;
    Ok(())
}

#[tokio::test]
async fn compact_l0_to_l1_preserves_visible_edges() -> anyhow::Result<()> {
    let tmp = target_tempdir("compact-l0-to-l1-preserves-visible-edges-")?;
    let config = LsmGraphConfig::new(tmp.path()).with_memgraph_capacity(128);
    let engine = Engine::create(config).await?;
    for i in 0..20u64 {
        engine.insert_edge(7, i, EdgeLabel::Knows.as_i32()).await?;
    }
    engine.flush_active().await?;
    let before = engine
        .get_neighbors(7, engine.current_snapshot())
        .await?
        .len();
    engine.compact_l0_to_l1().await?;
    let after = engine
        .get_neighbors(7, engine.current_snapshot())
        .await?
        .len();
    assert_eq!(before, after);
    assert_eq!(after, 20);
    Ok(())
}

#[tokio::test]
async fn semantic_l0_splits_high_degree_sources() -> anyhow::Result<()> {
    let tmp = target_tempdir("semantic-l0-splits-high-degree-sources-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::Semantic);
    let engine = Engine::create(config).await?;
    let high_src = encoded(VertexLabel::Person, 1);
    let low_src = encoded(VertexLabel::Person, 2);
    for i in 0..1030u64 {
        engine
            .insert_edge(
                high_src,
                encoded(VertexLabel::Person, 10_000 + i),
                EdgeLabel::Knows.as_i32(),
            )
            .await?;
    }
    engine
        .insert_edge(
            low_src,
            encoded(VertexLabel::Person, 20_000),
            EdgeLabel::Knows.as_i32(),
        )
        .await?;
    engine
        .insert_edge(
            low_src,
            encoded(VertexLabel::Tag, 30_000),
            EdgeLabel::HasInterest.as_i32(),
        )
        .await?;
    engine.flush_active().await?;

    let guard = engine.version_guard();
    let l0 = &guard.version().levels[0];
    assert!(
        l0.iter().any(|meta| meta.degree_class == DegreeClass::High),
        "semantic L0 should isolate high-degree sources"
    );
    assert!(
        l0.iter().any(|meta| meta.degree_class == DegreeClass::Low),
        "semantic L0 should keep low-degree sources in a separate segment"
    );
    assert!(
        l0.iter().any(|meta| meta.edge_type_partition == EdgeLabel::Knows.as_i32()
            && meta.degree_class == DegreeClass::Low
            && meta.degree_class_exact),
        "semantic L0 should keep low-degree Knows exact instead of demoting it to a mixed edge segment"
    );
    assert!(
        l0.iter().any(|meta| meta.edge_type_partition == EdgeLabel::HasInterest.as_i32()
            && meta.degree_class == DegreeClass::Low
            && meta.degree_class_exact),
        "semantic L0 should keep low-degree HasInterest exact instead of demoting it to a mixed edge segment"
    );
    assert!(
        !l0.iter()
            .any(|meta| meta.edge_type_partition == MIXED_EDGE_TYPE
                && meta.degree_class == DegreeClass::Low
                && meta.degree_class_exact),
        "semantic L0 should not merge exact low-degree query signatures into a mixed edge segment"
    );

    engine.metrics().reset();
    let neighbors = engine
        .get_neighbors_typed(
            low_src,
            EdgeLabel::Knows.as_i32(),
            engine.current_snapshot(),
        )
        .await?;
    assert_eq!(neighbors.len(), 1);
    let metrics = engine.metrics().snapshot_json();
    let candidates = metrics["csr"]["candidate_l0_segments"]
        .as_u64()
        .unwrap_or_default();
    assert_eq!(
        candidates, 1,
        "degree directory and semantic L0 index should route low-degree lookup to one L0 segment"
    );
    Ok(())
}

#[tokio::test]
async fn semantic_l0_degree_pruning_keeps_edges_across_flushes() -> anyhow::Result<()> {
    let tmp = target_tempdir("semantic-l0-degree-pruning-keeps-edges-across-flushes-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::Semantic);
    let engine = Engine::create(config).await?;
    let src = encoded(VertexLabel::Person, 42);
    for i in 0..20u64 {
        engine
            .insert_edge(
                src,
                encoded(VertexLabel::Person, 40_000 + i),
                EdgeLabel::Knows.as_i32(),
            )
            .await?;
        engine.flush_active().await?;
    }

    engine.metrics().reset();
    let neighbors = engine
        .get_neighbors_typed(src, EdgeLabel::Knows.as_i32(), engine.current_snapshot())
        .await?;
    assert_eq!(
        neighbors.len(),
        20,
        "global degree hints must not prune low-degree L0 fragments from earlier flushes"
    );
    let metrics = engine.metrics().snapshot_json();
    let candidates = metrics["csr"]["candidate_l0_segments"]
        .as_u64()
        .unwrap_or_default();
    assert_eq!(
        candidates, 20,
        "medium global degree should include all low-degree local L0 fragments"
    );
    Ok(())
}

#[tokio::test]
async fn semantic_l0_degree_directory_sidecar_survives_reopen() -> anyhow::Result<()> {
    let tmp = target_tempdir("semantic-l0-degree-directory-sidecar-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::Semantic);
    let engine = Engine::create(config.clone()).await?;
    let src = encoded(VertexLabel::Person, 420);
    let dst = encoded(VertexLabel::Person, 421);
    let edge_type = EdgeLabel::Knows.as_i32();

    engine.insert_edge(src, dst, edge_type).await?;
    engine.flush_active().await?;
    let sidecar = tmp.path().join("DEGREE_DIRECTORY");
    assert!(
        sidecar.exists() && std::fs::metadata(&sidecar)?.len() > 0,
        "flush should persist the packed degree directory sidecar"
    );

    drop(engine);
    let reopened = Engine::open(config).await?;
    reopened.metrics().reset();
    let neighbors = reopened
        .get_neighbors_typed(src, edge_type, reopened.current_snapshot())
        .await?;
    assert_eq!(neighbors.len(), 1);
    let metrics = reopened.metrics().snapshot_json();
    let candidates = metrics["csr"]["candidate_l0_segments"]
        .as_u64()
        .unwrap_or_default();
    assert_eq!(candidates, 1);
    Ok(())
}

#[tokio::test]
async fn label_only_l0_exposes_only_source_label_baseline() -> anyhow::Result<()> {
    let tmp = target_tempdir("label-only-l0-baseline-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::LabelOnly);
    let engine = Engine::create(config).await?;
    let person_knows = encoded(VertexLabel::Person, 10);
    let person_interest = encoded(VertexLabel::Person, 20);
    let comment_knows = encoded(VertexLabel::Comment, 30);

    engine
        .insert_edge(
            person_knows,
            encoded(VertexLabel::Person, 11),
            EdgeLabel::Knows.as_i32(),
        )
        .await?;
    engine
        .insert_edge(
            person_interest,
            encoded(VertexLabel::Tag, 21),
            EdgeLabel::HasInterest.as_i32(),
        )
        .await?;
    engine
        .insert_edge(
            comment_knows,
            encoded(VertexLabel::Person, 31),
            EdgeLabel::Knows.as_i32(),
        )
        .await?;
    engine.flush_active().await?;

    let guard = engine.version_guard();
    let l0 = &guard.version().levels[0];
    let person_meta = l0
        .iter()
        .find(|meta| meta.src_label == VertexLabel::Person as i32)
        .expect("label-only layout should keep a Person-label segment");
    assert_eq!(person_meta.edge_type_partition, MIXED_EDGE_TYPE);
    assert_eq!(person_meta.dst_label, UNKNOWN_SOURCE_LABEL);
    assert_eq!(person_meta.degree_class, DegreeClass::Mixed);
    assert!(!person_meta.degree_class_exact);

    engine.metrics().reset();
    let knows = engine
        .get_neighbors_typed(
            person_knows,
            EdgeLabel::Knows.as_i32(),
            engine.current_snapshot(),
        )
        .await?;
    assert_eq!(knows.len(), 1);
    let knows_candidates = engine.metrics().snapshot_json()["csr"]["candidate_l0_segments"]
        .as_u64()
        .unwrap_or_default();

    engine.metrics().reset();
    let interests = engine
        .get_neighbors_typed(
            person_knows,
            EdgeLabel::HasInterest.as_i32(),
            engine.current_snapshot(),
        )
        .await?;
    assert!(interests.is_empty());
    let interest_candidates = engine.metrics().snapshot_json()["csr"]["candidate_l0_segments"]
        .as_u64()
        .unwrap_or_default();
    assert_eq!(knows_candidates, 1);
    assert_eq!(
        interest_candidates, 1,
        "label-only baseline should not prune by edge type inside a matching label segment"
    );
    Ok(())
}

#[tokio::test]
async fn edge_type_only_l0_exposes_only_edge_type_baseline() -> anyhow::Result<()> {
    let tmp = target_tempdir("edge-type-only-l0-baseline-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::EdgeTypeOnly);
    let engine = Engine::create(config).await?;
    let person_knows = encoded(VertexLabel::Person, 10);
    let person_without_knows = encoded(VertexLabel::Person, 20);
    let comment_knows = encoded(VertexLabel::Comment, 30);

    engine
        .insert_edge(
            person_knows,
            encoded(VertexLabel::Person, 11),
            EdgeLabel::Knows.as_i32(),
        )
        .await?;
    engine
        .insert_edge(
            person_without_knows,
            encoded(VertexLabel::Tag, 21),
            EdgeLabel::HasInterest.as_i32(),
        )
        .await?;
    engine
        .insert_edge(
            comment_knows,
            encoded(VertexLabel::Person, 31),
            EdgeLabel::Knows.as_i32(),
        )
        .await?;
    engine.flush_active().await?;

    let guard = engine.version_guard();
    let l0 = &guard.version().levels[0];
    let knows_meta = l0
        .iter()
        .find(|meta| meta.edge_type_partition == EdgeLabel::Knows.as_i32())
        .expect("edge-type-only layout should keep a Knows segment");
    assert_eq!(knows_meta.src_label, UNKNOWN_SOURCE_LABEL);
    assert_eq!(knows_meta.dst_label, UNKNOWN_SOURCE_LABEL);
    assert_eq!(knows_meta.degree_class, DegreeClass::Mixed);
    assert!(!knows_meta.degree_class_exact);

    engine.metrics().reset();
    let knows = engine
        .get_neighbors_typed(
            person_without_knows,
            EdgeLabel::Knows.as_i32(),
            engine.current_snapshot(),
        )
        .await?;
    assert!(knows.is_empty());
    let candidates = engine.metrics().snapshot_json()["csr"]["candidate_l0_segments"]
        .as_u64()
        .unwrap_or_default();
    assert_eq!(
        candidates, 1,
        "edge-type-only baseline should keep the Knows segment as a candidate even when the source label differs"
    );
    Ok(())
}

#[tokio::test]
async fn degree_only_l0_exposes_only_degree_baseline() -> anyhow::Result<()> {
    let tmp = target_tempdir("degree-only-l0-baseline-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::DegreeOnly);
    let engine = Engine::create(config).await?;
    let low_src = encoded(VertexLabel::Person, 10);
    let medium_src = encoded(VertexLabel::Comment, 20);

    engine
        .insert_edge(
            low_src,
            encoded(VertexLabel::Person, 11),
            EdgeLabel::Knows.as_i32(),
        )
        .await?;
    for i in 0..20u64 {
        engine
            .insert_edge(
                medium_src,
                encoded(VertexLabel::Tag, 1_000 + i),
                EdgeLabel::HasTag.as_i32(),
            )
            .await?;
    }
    engine.flush_active().await?;

    let guard = engine.version_guard();
    let l0 = &guard.version().levels[0];
    let low_meta = l0
        .iter()
        .find(|meta| meta.degree_class == DegreeClass::Low)
        .expect("degree-only layout should keep a low-degree segment");
    assert_eq!(low_meta.src_label, UNKNOWN_SOURCE_LABEL);
    assert_eq!(low_meta.dst_label, UNKNOWN_SOURCE_LABEL);
    assert_eq!(low_meta.edge_type_partition, MIXED_EDGE_TYPE);
    assert!(low_meta.degree_class_exact);
    assert!(l0
        .iter()
        .any(|meta| meta.degree_class == DegreeClass::Medium && meta.degree_class_exact));

    engine.metrics().reset();
    let signature = GraphAccessSignature::neighbor_scan(low_src, Some(EdgeLabel::Knows.as_i32()))
        .with_degree_class(DegreeClass::Low);
    let neighbors = engine
        .get_neighbors_by_signature(signature, engine.current_snapshot())
        .await?;
    assert_eq!(neighbors.len(), 1);
    let candidates = engine.metrics().snapshot_json()["csr"]["candidate_l0_segments"]
        .as_u64()
        .unwrap_or_default();
    assert_eq!(
        candidates, 1,
        "degree-only baseline should prune the medium-degree L0 segment but not use label or edge-type metadata"
    );
    Ok(())
}

#[tokio::test]
async fn budgeted_semantic_l0_keeps_edge_type_for_unselected_small_partitions() -> anyhow::Result<()>
{
    let tmp = target_tempdir("budgeted-semantic-l0-merges-low-value-small-partitions-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_exact_bytes(128 * 1024);
    let engine = Engine::create(config).await?;
    let src = encoded(VertexLabel::Person, 500);
    engine
        .insert_edge(
            src,
            encoded(VertexLabel::Person, 501),
            EdgeLabel::Knows.as_i32(),
        )
        .await?;
    engine
        .insert_edge(src, encoded(VertexLabel::TagClass, 600), 4)
        .await?;
    engine
        .insert_edge(src, encoded(VertexLabel::Place, 700), 6)
        .await?;
    engine.flush_active().await?;

    let guard = engine.version_guard();
    let l0 = &guard.version().levels[0];
    for edge_type in [EdgeLabel::Knows.as_i32(), 4, 6] {
        let meta = l0
            .iter()
            .find(|meta| {
                meta.edge_type_partition == edge_type && meta.min_src == src && meta.max_src == src
            })
            .expect("unselected small edge types should keep schema-style edge-type partitions");
        assert_eq!(meta.degree_class, DegreeClass::Mixed);
        assert!(!meta.degree_class_exact);
    }
    assert!(
        !l0.iter()
            .any(|meta| meta.edge_type_partition == MIXED_EDGE_TYPE
                && meta.edge_count == 3
                && meta.min_src == src
                && meta.max_src == src),
        "budgeted semantic L0 should not collapse unselected edge types below schema-style pruning"
    );

    let neighbors = engine
        .get_neighbors_typed(src, EdgeLabel::Knows.as_i32(), engine.current_snapshot())
        .await?;
    assert_eq!(neighbors.len(), 1);
    let merged_neighbors = engine
        .get_neighbors_typed(src, 4, engine.current_snapshot())
        .await?;
    assert_eq!(merged_neighbors.len(), 1);
    Ok(())
}

#[tokio::test]
async fn budgeted_semantic_edge_type_score_preserves_hot_core_edge() -> anyhow::Result<()> {
    let tmp = target_tempdir("budgeted-semantic-edge-type-score-preserves-hot-core-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(128 * 1024)
        .with_semantic_budget_min_edge_type_score(0.1)
        .with_semantic_budget_core_edge_weight(4.0)
        .with_semantic_budget_other_edge_weight(0.1)
        .with_semantic_budget_min_exact_bytes(0)
        .with_semantic_budget_min_benefit_score(0.0);
    let engine = Engine::create(config).await?;
    let src = encoded(VertexLabel::Person, 700);
    engine
        .insert_edge(
            src,
            encoded(VertexLabel::Person, 701),
            EdgeLabel::Knows.as_i32(),
        )
        .await?;
    engine
        .insert_edge(src, encoded(VertexLabel::TagClass, 702), 4)
        .await?;
    engine
        .insert_edge(src, encoded(VertexLabel::Place, 703), 6)
        .await?;
    engine.flush_active().await?;

    let guard = engine.version_guard();
    let l0 = &guard.version().levels[0];
    assert!(
        l0.iter()
            .any(|meta| meta.edge_type_partition == EdgeLabel::Knows.as_i32()),
        "benefit score should preserve a hot core edge type even below the byte gate"
    );
    for edge_type in [4, 6] {
        let meta = l0
            .iter()
            .find(|meta| {
                meta.edge_type_partition == edge_type && meta.min_src == src && meta.max_src == src
            })
            .expect("low-weight cold edge types should keep schema-style partitions");
        assert_eq!(meta.degree_class, DegreeClass::Mixed);
        assert!(!meta.degree_class_exact);
    }

    let diagnostics = std::fs::read_to_string(tmp.path().join("budgeted-edge-candidates.tsv"))?;
    let rows: Vec<Vec<&str>> = diagnostics
        .lines()
        .skip(1)
        .map(|line| line.split('\t').collect())
        .collect();
    let knows_edge_type = EdgeLabel::Knows.as_i32().to_string();
    let knows_row = rows
        .iter()
        .find(|cols| cols.get(2).copied() == Some(knows_edge_type.as_str()))
        .expect("diagnostics should include the Knows edge-type candidate");
    assert_eq!(knows_row.get(10).copied(), Some("true"));
    assert_eq!(knows_row.get(11).copied(), Some("score_gate"));

    let cold_row = rows
        .iter()
        .find(|cols| cols.get(2).copied() == Some("4"))
        .expect("diagnostics should include cold edge type 4");
    assert_eq!(cold_row.get(10).copied(), Some("false"));
    assert_eq!(cold_row.get(11).copied(), Some("below_score_gate"));

    let hot_neighbors = engine
        .get_neighbors_typed(src, EdgeLabel::Knows.as_i32(), engine.current_snapshot())
        .await?;
    assert_eq!(hot_neighbors.len(), 1);
    let cold_neighbors = engine
        .get_neighbors_typed(src, 4, engine.current_snapshot())
        .await?;
    assert_eq!(cold_neighbors.len(), 1);
    Ok(())
}

#[tokio::test]
async fn budgeted_semantic_feedback_promotes_hot_non_core_edge_type() -> anyhow::Result<()> {
    let tmp = target_tempdir("budgeted-semantic-feedback-promotes-hot-non-core-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(128 * 1024)
        .with_semantic_budget_min_edge_type_score(0.05)
        .with_semantic_budget_core_edge_weight(0.0)
        .with_semantic_budget_reverse_core_edge_weight(0.0)
        .with_semantic_budget_other_edge_weight(0.0)
        .with_semantic_budget_min_exact_bytes(1)
        .with_semantic_budget_min_benefit_score(0.0);
    let engine = Engine::create(config).await?;
    let src = encoded(VertexLabel::Person, 780);
    let edge_type = 4;

    engine
        .insert_edge(src, encoded(VertexLabel::TagClass, 781), edge_type)
        .await?;
    engine.flush_active().await?;
    {
        let guard = engine.version_guard();
        let first = guard.version().levels[0]
            .iter()
            .find(|meta| {
                meta.edge_type_partition == edge_type && meta.min_src == src && meta.max_src == src
            })
            .expect("cold first flush should create a schema-style partition");
        assert_eq!(first.degree_class, DegreeClass::Mixed);
        assert!(!first.degree_class_exact);
    }

    for _ in 0..4 {
        let neighbors = engine
            .get_neighbors_typed(src, edge_type, engine.current_snapshot())
            .await?;
        assert_eq!(neighbors.len(), 1);
    }

    engine
        .insert_edge(src, encoded(VertexLabel::TagClass, 782), edge_type)
        .await?;
    engine.flush_active().await?;
    {
        let guard = engine.version_guard();
        assert!(
            guard.version().levels[0].iter().any(|meta| {
                meta.edge_type_partition == edge_type
                    && meta.min_src == src
                    && meta.max_src == src
                    && meta.degree_class == DegreeClass::Low
                    && meta.degree_class_exact
            }),
            "observed query feedback should promote the next hot non-core edge-type flush to exact degree"
        );
    }

    let diagnostics = std::fs::read_to_string(tmp.path().join("budgeted-edge-candidates.tsv"))?;
    let rows: Vec<Vec<&str>> = diagnostics
        .lines()
        .skip(1)
        .map(|line| line.split('\t').collect())
        .collect();
    let last_edge_type_row = rows
        .iter()
        .filter(|cols| cols.get(2).copied() == Some("4"))
        .last()
        .expect("diagnostics should include the feedback-promoted edge type");
    assert_eq!(last_edge_type_row.get(10).copied(), Some("true"));
    assert_eq!(
        last_edge_type_row.get(11).copied(),
        Some("feedback_score_gate")
    );
    Ok(())
}

#[tokio::test]
async fn budgeted_semantic_edge_type_allowlist_limits_exact_materialization() -> anyhow::Result<()>
{
    let tmp = target_tempdir("budgeted-semantic-edge-type-allowlist-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(128 * 1024)
        .with_semantic_budget_min_edge_type_score(0.1)
        .with_semantic_budget_core_edge_weight(4.0)
        .with_semantic_budget_edge_type_allowlist(vec![EdgeLabel::Knows.as_i32()])
        .with_semantic_budget_min_exact_bytes(0)
        .with_semantic_budget_min_benefit_score(0.0);
    let engine = Engine::create(config).await?;
    let src = encoded(VertexLabel::Person, 760);
    engine
        .insert_edge(
            src,
            encoded(VertexLabel::Person, 761),
            EdgeLabel::Knows.as_i32(),
        )
        .await?;
    engine
        .insert_edge(
            src,
            encoded(VertexLabel::Comment, 762),
            EdgeLabel::HasCreator.as_i32(),
        )
        .await?;
    engine.flush_active().await?;

    let guard = engine.version_guard();
    let l0 = &guard.version().levels[0];
    assert!(
        l0.iter()
            .any(|meta| meta.edge_type_partition == EdgeLabel::Knows.as_i32()),
        "the allowlisted edge type should be exact-materialized"
    );
    let non_allowlisted = l0
        .iter()
        .find(|meta| {
            meta.edge_type_partition == EdgeLabel::HasCreator.as_i32()
                && meta.min_src == src
                && meta.max_src == src
        })
        .expect(
            "a score-eligible but non-allowlisted edge type should keep a schema-style partition",
        );
    assert_eq!(non_allowlisted.degree_class, DegreeClass::Mixed);
    assert!(!non_allowlisted.degree_class_exact);

    let diagnostics = std::fs::read_to_string(tmp.path().join("budgeted-edge-candidates.tsv"))?;
    let rows: Vec<Vec<&str>> = diagnostics
        .lines()
        .skip(1)
        .map(|line| line.split('\t').collect())
        .collect();
    let knows_edge_type = EdgeLabel::Knows.as_i32().to_string();
    let knows_row = rows
        .iter()
        .find(|cols| cols.get(2).copied() == Some(knows_edge_type.as_str()))
        .expect("diagnostics should include the allowlisted Knows candidate");
    assert_eq!(knows_row.get(10).copied(), Some("true"));

    let has_creator_edge_type = EdgeLabel::HasCreator.as_i32().to_string();
    let has_creator_row = rows
        .iter()
        .find(|cols| cols.get(2).copied() == Some(has_creator_edge_type.as_str()))
        .expect("diagnostics should include the non-allowlisted HasCreator candidate");
    assert_eq!(has_creator_row.get(10).copied(), Some("false"));
    assert_eq!(
        has_creator_row.get(11).copied(),
        Some("not_in_edge_type_allowlist")
    );

    let knows_neighbors = engine
        .get_neighbors_typed(src, EdgeLabel::Knows.as_i32(), engine.current_snapshot())
        .await?;
    assert_eq!(knows_neighbors.len(), 1);
    let has_creator_neighbors = engine
        .get_neighbors_typed(
            src,
            EdgeLabel::HasCreator.as_i32(),
            engine.current_snapshot(),
        )
        .await?;
    assert_eq!(has_creator_neighbors.len(), 1);
    Ok(())
}

#[tokio::test]
async fn budgeted_semantic_hard_file_budget_keeps_highest_score_candidate() -> anyhow::Result<()> {
    let tmp = target_tempdir("budgeted-semantic-hard-file-budget-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(128 * 1024)
        .with_semantic_budget_min_edge_type_score(0.1)
        .with_semantic_budget_core_edge_weight(4.0)
        .with_semantic_budget_other_edge_weight(0.1)
        .with_semantic_budget_max_extra_l0_files(Some(1))
        .with_semantic_budget_min_exact_bytes(0)
        .with_semantic_budget_min_benefit_score(0.0);
    let engine = Engine::create(config).await?;
    let src = encoded(VertexLabel::Person, 720);
    engine
        .insert_edge(
            src,
            encoded(VertexLabel::Person, 721),
            EdgeLabel::Knows.as_i32(),
        )
        .await?;
    engine
        .insert_edge(
            src,
            encoded(VertexLabel::Person, 722),
            EdgeLabel::Knows.as_i32(),
        )
        .await?;
    engine
        .insert_edge(
            src,
            encoded(VertexLabel::Comment, 723),
            EdgeLabel::HasCreator.as_i32(),
        )
        .await?;
    engine
        .insert_edge(src, encoded(VertexLabel::Place, 724), 6)
        .await?;
    engine.flush_active().await?;

    let guard = engine.version_guard();
    let l0 = &guard.version().levels[0];
    assert!(
        l0.iter()
            .any(|meta| meta.edge_type_partition == EdgeLabel::Knows.as_i32()),
        "the highest score core edge type should consume the one-file budget"
    );
    let budget_rejected = l0
        .iter()
        .find(|meta| {
            meta.edge_type_partition == EdgeLabel::HasCreator.as_i32()
                && meta.min_src == src
                && meta.max_src == src
        })
        .expect("the second score-eligible core edge type should keep a schema-style partition");
    assert_eq!(budget_rejected.degree_class, DegreeClass::Mixed);
    assert!(!budget_rejected.degree_class_exact);
    let cold = l0
        .iter()
        .find(|meta| meta.edge_type_partition == 6 && meta.min_src == src && meta.max_src == src)
        .expect("cold edge types should remain readable through schema-style partitions");
    assert_eq!(cold.degree_class, DegreeClass::Mixed);
    assert!(!cold.degree_class_exact);

    let diagnostics = std::fs::read_to_string(tmp.path().join("budgeted-edge-candidates.tsv"))?;
    let rows: Vec<Vec<&str>> = diagnostics
        .lines()
        .skip(1)
        .map(|line| line.split('\t').collect())
        .collect();
    let knows_edge_type = EdgeLabel::Knows.as_i32().to_string();
    let knows_row = rows
        .iter()
        .find(|cols| cols.get(2).copied() == Some(knows_edge_type.as_str()))
        .expect("diagnostics should include the selected Knows candidate");
    assert_eq!(knows_row.get(10).copied(), Some("true"));

    let has_creator_edge_type = EdgeLabel::HasCreator.as_i32().to_string();
    let has_creator_row = rows
        .iter()
        .find(|cols| cols.get(2).copied() == Some(has_creator_edge_type.as_str()))
        .expect("diagnostics should include the budget-rejected HasCreator candidate");
    assert_eq!(has_creator_row.get(10).copied(), Some("false"));
    assert_eq!(
        has_creator_row.get(11).copied(),
        Some("fanout_budget_exhausted")
    );

    let knows_neighbors = engine
        .get_neighbors_typed(src, EdgeLabel::Knows.as_i32(), engine.current_snapshot())
        .await?;
    assert_eq!(knows_neighbors.len(), 2);
    let has_creator_neighbors = engine
        .get_neighbors_typed(
            src,
            EdgeLabel::HasCreator.as_i32(),
            engine.current_snapshot(),
        )
        .await?;
    assert_eq!(has_creator_neighbors.len(), 1);
    Ok(())
}

#[tokio::test]
async fn budgeted_semantic_hard_file_budget_persists_across_flushes() -> anyhow::Result<()> {
    let tmp = target_tempdir("budgeted-semantic-global-file-budget-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(128 * 1024)
        .with_semantic_budget_min_edge_type_score(0.1)
        .with_semantic_budget_core_edge_weight(4.0)
        .with_semantic_budget_max_extra_l0_files(Some(1))
        .with_semantic_budget_min_exact_bytes(0)
        .with_semantic_budget_min_benefit_score(0.0);
    let engine = Engine::create(config).await?;
    let src = encoded(VertexLabel::Person, 740);
    engine
        .insert_edge(
            src,
            encoded(VertexLabel::Person, 741),
            EdgeLabel::Knows.as_i32(),
        )
        .await?;
    engine
        .insert_edge(
            src,
            encoded(VertexLabel::Person, 742),
            EdgeLabel::Knows.as_i32(),
        )
        .await?;
    engine.flush_active().await?;

    engine
        .insert_edge(
            src,
            encoded(VertexLabel::Comment, 743),
            EdgeLabel::HasCreator.as_i32(),
        )
        .await?;
    engine
        .insert_edge(
            src,
            encoded(VertexLabel::Comment, 744),
            EdgeLabel::HasCreator.as_i32(),
        )
        .await?;
    engine.flush_active().await?;

    let guard = engine.version_guard();
    let l0 = &guard.version().levels[0];
    assert!(
        l0.iter()
            .any(|meta| meta.edge_type_partition == EdgeLabel::Knows.as_i32()),
        "the first flush should reserve the single global exact-file budget"
    );
    let second_flush = l0
        .iter()
        .find(|meta| {
            meta.edge_type_partition == EdgeLabel::HasCreator.as_i32()
                && meta.min_src == src
                && meta.max_src == src
        })
        .expect("the second flush should keep HasCreator as a schema-style partition");
    assert_eq!(second_flush.degree_class, DegreeClass::Mixed);
    assert!(!second_flush.degree_class_exact);

    let diagnostics = std::fs::read_to_string(tmp.path().join("budgeted-edge-candidates.tsv"))?;
    let has_creator_edge_type = EdgeLabel::HasCreator.as_i32().to_string();
    let has_creator_row = diagnostics
        .lines()
        .skip(1)
        .map(|line| line.split('\t').collect::<Vec<_>>())
        .find(|cols| cols.get(2).copied() == Some(has_creator_edge_type.as_str()))
        .expect("diagnostics should include the second-flush HasCreator candidate");
    assert_eq!(has_creator_row.get(10).copied(), Some("false"));
    assert_eq!(
        has_creator_row.get(11).copied(),
        Some("fanout_budget_exhausted")
    );

    let knows_neighbors = engine
        .get_neighbors_typed(src, EdgeLabel::Knows.as_i32(), engine.current_snapshot())
        .await?;
    assert_eq!(knows_neighbors.len(), 2);
    let has_creator_neighbors = engine
        .get_neighbors_typed(
            src,
            EdgeLabel::HasCreator.as_i32(),
            engine.current_snapshot(),
        )
        .await?;
    assert_eq!(has_creator_neighbors.len(), 2);
    Ok(())
}

#[tokio::test]
async fn budgeted_semantic_l0_cost_model_preserves_skewed_non_snb_edge_type() -> anyhow::Result<()>
{
    let tmp = target_tempdir("budgeted-semantic-cost-model-non-snb-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(1)
        .with_semantic_budget_min_exact_bytes(64)
        .with_semantic_budget_min_benefit_score(1.0);
    let engine = Engine::create(config).await?;
    let low_src = encoded(VertexLabel::Person, 850);
    let medium_src = encoded(VertexLabel::Person, 851);
    let edge_type = 99;
    engine
        .insert_edge(low_src, encoded(VertexLabel::Person, 950), edge_type)
        .await?;
    for i in 0..32u64 {
        engine
            .insert_edge(
                medium_src,
                encoded(VertexLabel::Person, 10_000 + i),
                edge_type,
            )
            .await?;
    }
    engine.flush_active().await?;

    let guard = engine.version_guard();
    let l0 = &guard.version().levels[0];
    assert!(
        l0.iter().any(|meta| meta.edge_type_partition == edge_type
            && meta.degree_class == DegreeClass::Medium
            && meta.degree_class_exact),
        "cost model should preserve a high-benefit degree class even for an edge type outside the SNB schema"
    );
    assert!(
        !l0.iter()
            .any(|meta| meta.edge_type_partition == MIXED_EDGE_TYPE),
        "the edge type should be preserved because its flush-time partition cost is low enough"
    );

    let low_neighbors = engine
        .get_neighbors_typed(low_src, edge_type, engine.current_snapshot())
        .await?;
    assert_eq!(low_neighbors.len(), 1);
    let medium_neighbors = engine
        .get_neighbors_typed(medium_src, edge_type, engine.current_snapshot())
        .await?;
    assert_eq!(medium_neighbors.len(), 32);
    Ok(())
}

#[tokio::test]
async fn budgeted_semantic_l0_merges_cold_degree_classes_by_edge_type() -> anyhow::Result<()> {
    let tmp = target_tempdir("budgeted-semantic-l0-merges-cold-degree-classes-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(1)
        .with_semantic_budget_min_exact_bytes(usize::MAX);
    let engine = Engine::create(config).await?;
    let low_src = encoded(VertexLabel::Person, 800);
    let medium_src = encoded(VertexLabel::Person, 801);
    let edge_type = EdgeLabel::IsLocatedIn.as_i32();
    engine
        .insert_edge(low_src, encoded(VertexLabel::Place, 900), edge_type)
        .await?;
    for i in 0..17u64 {
        engine
            .insert_edge(
                medium_src,
                encoded(VertexLabel::Place, 1_000 + i),
                edge_type,
            )
            .await?;
    }
    engine.flush_active().await?;

    let guard = engine.version_guard();
    let l0 = &guard.version().levels[0];
    assert!(
        l0.iter().any(|meta| meta.edge_type_partition == edge_type
            && meta.degree_class == DegreeClass::Mixed
            && !meta.degree_class_exact),
        "cold edge types should keep exact edge partitions while merging degree classes"
    );
    assert!(
        !l0.iter()
            .any(|meta| meta.edge_type_partition == MIXED_EDGE_TYPE),
        "large enough cold edge type groups should not be demoted to mixed edge partitions"
    );

    let low_neighbors = engine
        .get_neighbors_typed(low_src, edge_type, engine.current_snapshot())
        .await?;
    assert_eq!(low_neighbors.len(), 1);
    let medium_neighbors = engine
        .get_neighbors_typed(medium_src, edge_type, engine.current_snapshot())
        .await?;
    assert_eq!(medium_neighbors.len(), 17);
    Ok(())
}

#[tokio::test]
async fn schema_epoch_change_keeps_old_segments_readable() -> anyhow::Result<()> {
    let tmp = target_tempdir("schema-epoch-change-keeps-old-segments-")?;
    let base_config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(1)
        .with_semantic_budget_min_exact_bytes(1)
        .with_schema_epoch(0);
    let src = encoded(VertexLabel::Person, 1_200);

    let engine = Engine::create(base_config.clone()).await?;
    engine
        .insert_edge(
            src,
            encoded(VertexLabel::Person, 1_201),
            EdgeLabel::Knows.as_i32(),
        )
        .await?;
    engine.flush_active().await?;
    drop(engine);

    let evolved_config = base_config.clone().with_schema_epoch(1);
    let evolved = Engine::open(evolved_config.clone()).await?;
    evolved
        .insert_edge(
            src,
            encoded(VertexLabel::Comment, 1_202),
            EdgeLabel::HasCreator.as_i32(),
        )
        .await?;
    evolved.flush_active().await?;

    let guard = evolved.version_guard();
    let l0 = &guard.version().levels[0];
    assert!(
        l0.iter().any(|meta| meta.schema_epoch == 0
            && meta.edge_type_partition == EdgeLabel::Knows.as_i32()
            && meta.summary_completeness == SemanticSummaryCompleteness::Exact),
        "old segment should remain valid at schema epoch 0"
    );
    assert!(
        l0.iter().any(|meta| meta.schema_epoch == 1
            && meta.edge_type_partition == EdgeLabel::HasCreator.as_i32()
            && meta.summary_completeness == SemanticSummaryCompleteness::Exact),
        "new writes after schema evolution should carry the new schema epoch"
    );

    let old_neighbors = evolved
        .get_neighbors_typed(src, EdgeLabel::Knows.as_i32(), evolved.current_snapshot())
        .await?;
    assert_eq!(old_neighbors.len(), 1);
    let new_neighbors = evolved
        .get_neighbors_typed(
            src,
            EdgeLabel::HasCreator.as_i32(),
            evolved.current_snapshot(),
        )
        .await?;
    assert_eq!(new_neighbors.len(), 1);
    drop(evolved);

    let reopened = Engine::open(evolved_config).await?;
    let old_neighbors = reopened
        .get_neighbors_typed(src, EdgeLabel::Knows.as_i32(), reopened.current_snapshot())
        .await?;
    assert_eq!(old_neighbors.len(), 1);
    let new_neighbors = reopened
        .get_neighbors_typed(
            src,
            EdgeLabel::HasCreator.as_i32(),
            reopened.current_snapshot(),
        )
        .await?;
    assert_eq!(new_neighbors.len(), 1);
    Ok(())
}

#[tokio::test]
async fn schema_catalog_persists_changes_and_drives_new_segment_epoch() -> anyhow::Result<()> {
    let tmp = target_tempdir("schema-catalog-persists-changes-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(1)
        .with_semantic_budget_min_exact_bytes(1);
    let src = encoded(VertexLabel::Person, 1_300);

    let engine = Engine::create(config.clone()).await?;
    assert_eq!(engine.current_schema_epoch(), 0);
    engine
        .insert_edge(
            src,
            encoded(VertexLabel::Person, 1_301),
            EdgeLabel::Knows.as_i32(),
        )
        .await?;
    engine.flush_active().await?;

    let person_epoch = engine.add_schema_vertex_label(VertexLabel::Person as i32, "Person")?;
    let comment_epoch = engine.add_schema_vertex_label(VertexLabel::Comment as i32, "Comment")?;
    let reacts_edge_type = 99;
    let edge_epoch = engine.add_schema_edge_label(
        reacts_edge_type,
        "REACTS_TO",
        VertexLabel::Person as i32,
        VertexLabel::Comment as i32,
    )?;
    let property_epoch = engine.add_schema_property(NewPropertyEntry {
        id: 1,
        owner: PropertyOwner::EdgeLabel(reacts_edge_type),
        name: "weight".to_string(),
        logical_type: "int64".to_string(),
        physical_encoding: "plain_i64".to_string(),
        encoding_version: 1,
        default_or_null_rule: "null".to_string(),
    })?;
    assert_eq!(person_epoch, 1);
    assert_eq!(comment_epoch, 2);
    assert_eq!(edge_epoch, 3);
    assert_eq!(property_epoch, 4);
    assert_eq!(engine.current_schema_epoch(), 4);

    engine
        .insert_edge(src, encoded(VertexLabel::Comment, 1_302), reacts_edge_type)
        .await?;
    engine.flush_active().await?;

    let catalog_path = SchemaCatalog::path_for_store(tmp.path());
    assert!(
        catalog_path.exists(),
        "schema catalog should be persisted at store level"
    );
    let catalog = SchemaCatalog::load(&catalog_path)?;
    assert_eq!(catalog.current_epoch, 4);
    assert_eq!(
        catalog
            .edge_labels
            .get(&reacts_edge_type)
            .map(|entry| entry.name.as_str()),
        Some("REACTS_TO")
    );
    assert_eq!(
        catalog
            .properties
            .get(&1)
            .map(|entry| entry.valid_from_epoch),
        Some(4)
    );

    let guard = engine.version_guard();
    let l0 = &guard.version().levels[0];
    assert!(
        l0.iter()
            .any(|meta| meta.schema_epoch == 0
                && meta.edge_type_partition == EdgeLabel::Knows.as_i32()),
        "the pre-change segment should keep epoch 0"
    );
    assert!(
        l0.iter()
            .any(|meta| meta.schema_epoch == 4 && meta.edge_type_partition == reacts_edge_type),
        "new segment should use the persisted catalog's current epoch"
    );

    let old_neighbors = engine
        .get_neighbors_typed(src, EdgeLabel::Knows.as_i32(), engine.current_snapshot())
        .await?;
    assert_eq!(old_neighbors.len(), 1);
    let new_neighbors = engine
        .get_neighbors_typed(src, reacts_edge_type, engine.current_snapshot())
        .await?;
    assert_eq!(new_neighbors.len(), 1);
    drop(engine);

    let reopened = Engine::open(config).await?;
    assert_eq!(reopened.current_schema_epoch(), 4);
    assert_eq!(
        reopened
            .schema_catalog_snapshot()
            .edge_labels
            .get(&reacts_edge_type)
            .map(|entry| entry.valid_from_epoch),
        Some(3)
    );
    let old_neighbors = reopened
        .get_neighbors_typed(src, EdgeLabel::Knows.as_i32(), reopened.current_snapshot())
        .await?;
    assert_eq!(old_neighbors.len(), 1);
    let new_neighbors = reopened
        .get_neighbors_typed(src, reacts_edge_type, reopened.current_snapshot())
        .await?;
    assert_eq!(new_neighbors.len(), 1);
    Ok(())
}

#[tokio::test]
async fn schema_alias_and_drop_persist_across_reopen() -> anyhow::Result<()> {
    let tmp = target_tempdir("schema-alias-drop-persist-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(1)
        .with_semantic_budget_min_exact_bytes(1);
    let src = encoded(VertexLabel::Person, 1_360);
    let dst = encoded(VertexLabel::Comment, 1_361);
    let canonical_edge_type = 99;
    let alias_edge_type = 100;

    let engine = Engine::create(config.clone()).await?;
    let edge_epoch = engine.add_schema_edge_label(
        canonical_edge_type,
        "REACTS_TO",
        VertexLabel::Person as i32,
        VertexLabel::Comment as i32,
    )?;
    assert_eq!(edge_epoch, 1);
    engine.insert_edge(src, dst, canonical_edge_type).await?;
    engine.flush_active().await?;

    let alias_epoch =
        engine.alias_schema_edge_label(alias_edge_type, "LIKES", canonical_edge_type)?;
    assert_eq!(alias_epoch, 2);
    assert_eq!(
        engine
            .schema_catalog_snapshot()
            .resolve_edge_label(alias_edge_type, alias_epoch),
        Some(canonical_edge_type)
    );

    let resolved_edge_type = engine
        .schema_catalog_snapshot()
        .resolve_edge_label(alias_edge_type, engine.current_schema_epoch())
        .expect("alias should resolve to canonical edge type");
    let neighbors = engine
        .get_neighbors_typed(src, resolved_edge_type, engine.current_snapshot())
        .await?;
    assert_eq!(
        neighbors.iter().map(|edge| edge.dst).collect::<Vec<_>>(),
        vec![dst]
    );

    let drop_epoch = engine.drop_schema_edge_label(alias_edge_type)?;
    assert_eq!(drop_epoch, 3);
    let catalog = engine.schema_catalog_snapshot();
    assert_eq!(
        catalog.resolve_edge_label(alias_edge_type, alias_epoch),
        Some(canonical_edge_type)
    );
    assert_eq!(
        catalog.resolve_edge_label(alias_edge_type, drop_epoch),
        None
    );
    assert_eq!(
        catalog.resolve_edge_label(canonical_edge_type, drop_epoch),
        Some(canonical_edge_type)
    );

    let report = engine.schema_evolution_report();
    assert_eq!(report.alias_entries.len(), 1);
    assert_eq!(report.alias_entries[0].entry_kind, "edge_label");
    assert_eq!(report.alias_entries[0].id, alias_edge_type.to_string());
    assert_eq!(
        report.alias_entries[0].canonical_id,
        canonical_edge_type.to_string()
    );
    assert!(!report.alias_entries[0].visible_at_current_epoch);
    assert_eq!(report.dropped_entries.len(), 1);
    assert_eq!(report.dropped_entries[0].entry_kind, "edge_label");
    assert_eq!(report.dropped_entries[0].id, alias_edge_type.to_string());

    drop(engine);

    let reopened = Engine::open(config).await?;
    let catalog = reopened.schema_catalog_snapshot();
    assert_eq!(catalog.current_epoch, 3);
    assert_eq!(
        catalog.edge_labels.get(&alias_edge_type).unwrap().alias_of,
        Some(canonical_edge_type)
    );
    assert_eq!(
        catalog
            .edge_labels
            .get(&alias_edge_type)
            .unwrap()
            .dropped_at_epoch,
        Some(3)
    );
    assert_eq!(
        catalog.resolve_edge_label(alias_edge_type, 2),
        Some(canonical_edge_type)
    );
    assert_eq!(catalog.resolve_edge_label(alias_edge_type, 3), None);
    let neighbors = reopened
        .get_neighbors_typed(src, canonical_edge_type, reopened.current_snapshot())
        .await?;
    assert_eq!(
        neighbors.iter().map(|edge| edge.dst).collect::<Vec<_>>(),
        vec![dst]
    );
    Ok(())
}

#[tokio::test]
async fn add_property_keeps_segments_readable_and_records_absent_presence() -> anyhow::Result<()> {
    let tmp = target_tempdir("add-property-records-absent-presence-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(1)
        .with_semantic_budget_min_exact_bytes(1);
    let src = encoded(VertexLabel::Person, 1_400);
    let edge_type = EdgeLabel::Knows.as_i32();
    let property_id = 5;

    let engine = Engine::create(config.clone()).await?;
    engine
        .insert_edge(src, encoded(VertexLabel::Person, 1_401), edge_type)
        .await?;
    engine.flush_active().await?;

    let property_epoch = engine.add_schema_property(NewPropertyEntry {
        id: property_id,
        owner: PropertyOwner::EdgeLabel(edge_type),
        name: "strength".to_string(),
        logical_type: "int64".to_string(),
        physical_encoding: "plain_i64".to_string(),
        encoding_version: 1,
        default_or_null_rule: "null".to_string(),
    })?;
    assert_eq!(property_epoch, 1);

    engine
        .insert_edge(src, encoded(VertexLabel::Person, 1_402), edge_type)
        .await?;
    engine.flush_active().await?;

    let guard = engine.version_guard();
    let l0 = &guard.version().levels[0];
    assert_eq!(l0.len(), 2);
    assert!(
        l0.iter().any(|meta| meta.schema_epoch == 0
            && meta.property_summary_completeness == SemanticSummaryCompleteness::Exact
            && meta.definitely_lacks_property(property_id)
            && !meta.may_contain_property(property_id)),
        "pre-property segment should remain valid and explicitly lack inline property values"
    );
    assert!(
        l0.iter().any(|meta| meta.schema_epoch == 1
            && meta.property_summary_completeness == SemanticSummaryCompleteness::Exact
            && meta.definitely_lacks_property(property_id)
            && !meta.may_contain_property(property_id)),
        "post-property segment without inline properties should record exact absence"
    );

    let neighbors = engine
        .get_neighbors_typed(src, edge_type, engine.current_snapshot())
        .await?;
    assert_eq!(neighbors.len(), 2);

    drop(engine);
    let reopened = Engine::open(config).await?;
    assert_eq!(
        reopened
            .schema_catalog_snapshot()
            .properties
            .get(&property_id)
            .map(|entry| entry.default_or_null_rule.as_str()),
        Some("null")
    );
    let neighbors = reopened
        .get_neighbors_typed(src, edge_type, reopened.current_snapshot())
        .await?;
    assert_eq!(neighbors.len(), 2);
    Ok(())
}

#[tokio::test]
async fn schema_evolution_report_summarizes_epoch_and_pruning_boundaries() -> anyhow::Result<()> {
    let tmp = target_tempdir("schema-evolution-report-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(1)
        .with_semantic_budget_min_exact_bytes(1);
    let src = encoded(VertexLabel::Person, 1_450);
    let old_edge_type = EdgeLabel::Knows.as_i32();
    let new_edge_type = 99;
    let property_id = 5;
    let high_property_id = 65;

    let engine = Engine::create(config).await?;
    engine
        .insert_edge(src, encoded(VertexLabel::Person, 1_451), old_edge_type)
        .await?;
    engine.flush_active().await?;

    assert_eq!(
        engine.add_schema_edge_label(
            new_edge_type,
            "REACTS_TO",
            VertexLabel::Person as i32,
            VertexLabel::Comment as i32,
        )?,
        1
    );
    assert_eq!(
        engine.add_schema_property(NewPropertyEntry {
            id: property_id,
            owner: PropertyOwner::EdgeLabel(new_edge_type),
            name: "strength".to_string(),
            logical_type: "int64".to_string(),
            physical_encoding: "plain_i64".to_string(),
            encoding_version: 1,
            default_or_null_rule: "null".to_string(),
        })?,
        2
    );
    assert_eq!(
        engine.add_schema_property(NewPropertyEntry {
            id: high_property_id,
            owner: PropertyOwner::EdgeLabel(new_edge_type),
            name: "wide_property".to_string(),
            logical_type: "int64".to_string(),
            physical_encoding: "plain_i64".to_string(),
            encoding_version: 1,
            default_or_null_rule: "null".to_string(),
        })?,
        3
    );

    engine
        .insert_edge(src, encoded(VertexLabel::Comment, 1_452), new_edge_type)
        .await?;
    engine.flush_active().await?;

    let report = engine.schema_evolution_report();
    assert_eq!(report.current_schema_epoch, 3);
    assert_eq!(report.live_segment_count, 2);
    assert_eq!(
        report.segment_count_by_schema_epoch.get(&0).copied(),
        Some(1)
    );
    assert_eq!(
        report.segment_count_by_schema_epoch.get(&3).copied(),
        Some(1)
    );
    assert_eq!(
        report
            .segment_count_by_property_encoding_epoch
            .get(&0)
            .copied(),
        Some(1)
    );
    assert_eq!(
        report
            .segment_count_by_property_encoding_epoch
            .get(&3)
            .copied(),
        Some(1)
    );
    assert_eq!(
        report
            .segment_count_by_semantic_summary
            .get("Exact")
            .copied(),
        Some(2)
    );
    assert_eq!(
        report
            .segment_count_by_property_summary
            .get("Exact")
            .copied(),
        Some(2)
    );

    let edge_report = report
        .edge_label_reports
        .iter()
        .find(|entry| entry.edge_label_id == new_edge_type)
        .expect("new edge label should appear in report");
    assert_eq!(edge_report.exact_matching_segments, 1);
    assert_eq!(edge_report.exact_disjoint_segments, 1);
    assert_eq!(edge_report.conservative_segments, 0);

    let property_report = report
        .property_reports
        .iter()
        .find(|entry| entry.property_id == property_id)
        .expect("low property id should appear in report");
    assert_eq!(property_report.logical_type, "int64");
    assert_eq!(property_report.physical_encoding, "plain_i64");
    assert_eq!(property_report.encoding_version, 1);
    assert_eq!(property_report.encoding_epoch, 2);
    assert!(property_report.representable_in_presence_bitmap);
    assert_eq!(property_report.exact_absent_segments, 2);
    assert_eq!(property_report.required_present_prunable_segments, 2);
    assert_eq!(property_report.conservative_or_present_segments, 0);

    let high_property_report = report
        .property_reports
        .iter()
        .find(|entry| entry.property_id == high_property_id)
        .expect("high property id should appear in report");
    assert_eq!(high_property_report.encoding_epoch, 3);
    assert!(!high_property_report.representable_in_presence_bitmap);
    assert_eq!(high_property_report.exact_absent_segments, 0);
    assert_eq!(high_property_report.required_present_prunable_segments, 0);
    assert_eq!(high_property_report.conservative_or_present_segments, 2);
    Ok(())
}

#[tokio::test]
async fn property_encoding_change_advances_catalog_and_segment_epochs() -> anyhow::Result<()> {
    let tmp = target_tempdir("property-encoding-change-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(1)
        .with_semantic_budget_min_exact_bytes(1);
    let src = encoded(VertexLabel::Person, 1_475);
    let edge_type = EdgeLabel::Knows.as_i32();
    let property_id = 5;

    let engine = Engine::create(config.clone()).await?;
    let property_epoch = engine.add_schema_property(NewPropertyEntry {
        id: property_id,
        owner: PropertyOwner::EdgeLabel(edge_type),
        name: "strength".to_string(),
        logical_type: "int64".to_string(),
        physical_encoding: "plain_i64".to_string(),
        encoding_version: 1,
        default_or_null_rule: "null".to_string(),
    })?;
    assert_eq!(property_epoch, 1);
    engine
        .insert_edge(src, encoded(VertexLabel::Person, 1_476), edge_type)
        .await?;
    engine.flush_active().await?;

    let encoding_epoch =
        engine.change_schema_property_encoding(property_id, "float64", "plain_f64", 2, "null")?;
    assert_eq!(encoding_epoch, 2);
    engine
        .insert_edge(src, encoded(VertexLabel::Person, 1_477), edge_type)
        .await?;
    engine.flush_active().await?;

    let report = engine.schema_evolution_report();
    assert_eq!(
        report
            .segment_count_by_property_encoding_epoch
            .get(&1)
            .copied(),
        Some(1)
    );
    assert_eq!(
        report
            .segment_count_by_property_encoding_epoch
            .get(&2)
            .copied(),
        Some(1)
    );
    let property_report = report
        .property_reports
        .iter()
        .find(|entry| entry.property_id == property_id)
        .expect("property should appear in report");
    assert_eq!(property_report.logical_type, "float64");
    assert_eq!(property_report.physical_encoding, "plain_f64");
    assert_eq!(property_report.encoding_version, 2);
    assert_eq!(property_report.encoding_epoch, 2);
    let migration_candidate = report
        .property_encoding_migration_candidates
        .iter()
        .find(|entry| {
            entry.property_id == property_id
                && entry.current_encoding_epoch == 2
                && entry.old_encoding_epoch == 1
        })
        .expect("old encoding epoch should be reported as a migration candidate");
    assert_eq!(migration_candidate.segments, 1);
    assert!(!migration_candidate.safe_to_rewrite);
    assert!(migration_candidate.blocked_by_snapshot_gc);

    drop(engine);
    let reopened = Engine::open(config).await?;
    let property = reopened
        .schema_catalog_snapshot()
        .properties
        .get(&property_id)
        .cloned()
        .expect("property encoding change should persist");
    assert_eq!(property.physical_encoding, "plain_f64");
    assert_eq!(property.encoding_version, 2);
    assert_eq!(property.encoding_epoch, 2);
    Ok(())
}

#[tokio::test]
async fn engine_csr_value_predicate_prototype_reads_property_bearing_segments() -> anyhow::Result<()>
{
    let tmp = target_tempdir("engine-csr-value-predicate-prototype-")?;
    let config = LsmGraphConfig::new(tmp.path()).with_memgraph_capacity(64 * 1024 * 1024);
    let src = encoded(VertexLabel::Person, 1_490);
    let matching_dst = encoded(VertexLabel::Person, 1_491);
    let other_dst = encoded(VertexLabel::Person, 1_492);
    let edge_type = EdgeLabel::Knows.as_i32();
    let property_id = 5;

    let engine = Engine::create(config.clone()).await?;
    let property_epoch = engine.add_schema_property(NewPropertyEntry {
        id: property_id,
        owner: PropertyOwner::EdgeLabel(edge_type),
        name: "strength".to_string(),
        logical_type: "int64".to_string(),
        physical_encoding: "plain_i64".to_string(),
        encoding_version: 1,
        default_or_null_rule: "null".to_string(),
    })?;
    drop(engine);

    let backend = Arc::new(BlockingPreadBackend::new(128, Arc::new(Metrics::default())));
    let writer = CsrWriter::new(backend, tmp.path(), property_epoch);
    let meta = writer
        .write_segment_with_properties(
            0,
            1,
            vec![
                EdgeRecordWithProperties {
                    edge: EdgeRecord::insert(src, matching_dst, edge_type, 1),
                    properties: vec![EdgePropertyValue::encoded(
                        property_id,
                        property_epoch,
                        42_i64.to_le_bytes(),
                    )],
                },
                EdgeRecordWithProperties {
                    edge: EdgeRecord::insert(src, other_dst, edge_type, 2),
                    properties: vec![EdgePropertyValue::encoded(
                        property_id,
                        property_epoch,
                        7_i64.to_le_bytes(),
                    )],
                },
            ],
        )
        .await?;
    Manifest::new(tmp.path()).append(&ManifestRecord::CreateFile { meta })?;

    let reopened = Engine::open(config).await?;
    let csr_snapshot = reopened.current_snapshot();
    let matching = reopened
        .get_neighbors_matching_csr_property_value_prototype(
            src,
            Some(edge_type),
            csr_snapshot,
            CsrPropertyValuePredicate::equals(property_id, PropertyValue::I64(42)),
        )
        .await?;
    assert_eq!(
        matching,
        vec![EdgeRecord::insert(src, matching_dst, edge_type, 1)]
    );

    let missing_value = reopened
        .get_neighbors_matching_csr_property_value_prototype(
            src,
            Some(edge_type),
            csr_snapshot,
            CsrPropertyValuePredicate::equals(property_id, PropertyValue::I64(100)),
        )
        .await?;
    assert!(missing_value.is_empty());

    let blocker_snapshot = reopened.insert_edge(src, matching_dst, edge_type).await?;
    let old_snapshot_still_matches = reopened
        .get_neighbors_matching_csr_property_value_prototype(
            src,
            Some(edge_type),
            csr_snapshot,
            CsrPropertyValuePredicate::equals(property_id, PropertyValue::I64(42)),
        )
        .await?;
    assert_eq!(
        old_snapshot_still_matches,
        vec![EdgeRecord::insert(src, matching_dst, edge_type, 1)]
    );

    let hidden_by_topology_only_insert = reopened
        .get_neighbors_matching_csr_property_value_prototype(
            src,
            Some(edge_type),
            blocker_snapshot,
            CsrPropertyValuePredicate::equals(property_id, PropertyValue::I64(42)),
        )
        .await?;
    assert!(
        hidden_by_topology_only_insert.is_empty(),
        "topology-only memgraph inserts should act as non-matching blockers"
    );
    Ok(())
}

#[tokio::test]
async fn property_aware_compaction_preserves_csr_value_predicate_matches() -> anyhow::Result<()> {
    let tmp = target_tempdir("property-aware-compaction-preserves-values-")?;
    let config = LsmGraphConfig::new(tmp.path()).with_memgraph_capacity(64 * 1024 * 1024);
    let src = encoded(VertexLabel::Person, 1_493);
    let dst = encoded(VertexLabel::Person, 1_494);
    let edge_type = EdgeLabel::Knows.as_i32();
    let property_id = 5;

    let engine = Engine::create(config.clone()).await?;
    let property_epoch = engine.add_schema_property(NewPropertyEntry {
        id: property_id,
        owner: PropertyOwner::EdgeLabel(edge_type),
        name: "strength".to_string(),
        logical_type: "int64".to_string(),
        physical_encoding: "plain_i64".to_string(),
        encoding_version: 1,
        default_or_null_rule: "null".to_string(),
    })?;
    drop(engine);

    let backend = Arc::new(BlockingPreadBackend::new(128, Arc::new(Metrics::default())));
    let writer = CsrWriter::new(backend, tmp.path(), property_epoch);
    let meta = writer
        .write_segment_with_properties(
            0,
            1,
            vec![
                EdgeRecordWithProperties {
                    edge: EdgeRecord::insert(src, dst, edge_type, 1),
                    properties: vec![EdgePropertyValue::encoded(
                        property_id,
                        property_epoch,
                        42_i64.to_le_bytes(),
                    )],
                },
                EdgeRecordWithProperties::topology_only(EdgeRecord::insert(src, dst, edge_type, 2)),
            ],
        )
        .await?;
    Manifest::new(tmp.path()).append(&ManifestRecord::CreateFile { meta })?;

    let reopened = Engine::open(config.clone()).await?;
    let predicate = CsrPropertyValuePredicate::equals(property_id, PropertyValue::I64(42));
    let before_compaction_old_snapshot = reopened
        .get_neighbors_matching_csr_property_value_prototype(
            src,
            Some(edge_type),
            1,
            predicate.clone(),
        )
        .await?;
    assert_eq!(
        before_compaction_old_snapshot,
        vec![EdgeRecord::insert(src, dst, edge_type, 1)]
    );
    assert!(
        reopened
            .get_neighbors_matching_csr_property_value_prototype(
                src,
                Some(edge_type),
                2,
                predicate.clone(),
            )
            .await?
            .is_empty(),
        "newer topology-only insert should hide the older matching value"
    );

    let output = reopened
        .compact_l0_to_l1()
        .await?
        .expect("forced compaction should emit an L1 segment");
    assert!(
        output.has_property_value_section(),
        "compaction output should retain encoded property sections"
    );
    assert_eq!(output.property_encoding_epoch, property_epoch);

    let after_compaction_old_snapshot = reopened
        .get_neighbors_matching_csr_property_value_prototype(
            src,
            Some(edge_type),
            1,
            predicate.clone(),
        )
        .await?;
    assert_eq!(
        after_compaction_old_snapshot,
        vec![EdgeRecord::insert(src, dst, edge_type, 1)]
    );
    assert!(
        reopened
            .get_neighbors_matching_csr_property_value_prototype(
                src,
                Some(edge_type),
                2,
                predicate.clone(),
            )
            .await?
            .is_empty(),
        "compacted topology-only insert should still hide the old matching value"
    );
    drop(reopened);

    let reopened_after_compaction = Engine::open(config).await?;
    let after_reopen = reopened_after_compaction
        .get_neighbors_matching_csr_property_value_prototype(src, Some(edge_type), 1, predicate)
        .await?;
    assert_eq!(
        after_reopen,
        vec![EdgeRecord::insert(src, dst, edge_type, 1)]
    );
    Ok(())
}

#[tokio::test]
async fn memgraph_property_delta_prototype_matches_before_and_after_flush() -> anyhow::Result<()> {
    let tmp = target_tempdir("memgraph-property-delta-prototype-")?;
    let config = LsmGraphConfig::new(tmp.path()).with_memgraph_capacity(64 * 1024 * 1024);
    let src = encoded(VertexLabel::Person, 1_495);
    let matching_dst = encoded(VertexLabel::Person, 1_496);
    let other_dst = encoded(VertexLabel::Person, 1_497);
    let edge_type = EdgeLabel::Knows.as_i32();
    let property_id = 5;

    let engine = Engine::create(config.clone()).await?;
    let property_epoch = engine.add_schema_property(NewPropertyEntry {
        id: property_id,
        owner: PropertyOwner::EdgeLabel(edge_type),
        name: "strength".to_string(),
        logical_type: "int64".to_string(),
        physical_encoding: "plain_i64".to_string(),
        encoding_version: 1,
        default_or_null_rule: "null".to_string(),
    })?;
    let matching_snapshot = engine
        .insert_edge_with_properties_prototype(
            src,
            matching_dst,
            edge_type,
            vec![EdgePropertyValue::encoded(
                property_id,
                property_epoch,
                42_i64.to_le_bytes(),
            )],
        )
        .await?;
    engine
        .insert_edge_with_properties_prototype(
            src,
            other_dst,
            edge_type,
            vec![EdgePropertyValue::encoded(
                property_id,
                property_epoch,
                7_i64.to_le_bytes(),
            )],
        )
        .await?;
    let predicate = CsrPropertyValuePredicate::equals(property_id, PropertyValue::I64(42));

    let before_flush = engine
        .get_neighbors_matching_csr_property_value_prototype(
            src,
            Some(edge_type),
            engine.current_snapshot(),
            predicate.clone(),
        )
        .await?;
    assert_eq!(
        before_flush,
        vec![EdgeRecord::insert(
            src,
            matching_dst,
            edge_type,
            matching_snapshot
        )]
    );

    engine.flush_active().await?;
    let after_flush = engine
        .get_neighbors_matching_csr_property_value_prototype(
            src,
            Some(edge_type),
            engine.current_snapshot(),
            predicate.clone(),
        )
        .await?;
    assert_eq!(
        after_flush,
        vec![EdgeRecord::insert(
            src,
            matching_dst,
            edge_type,
            matching_snapshot
        )]
    );
    assert!(
        engine
            .version_guard()
            .version()
            .levels
            .iter()
            .flatten()
            .any(|meta| meta.has_property_value_section()),
        "flushing property-bearing memgraph rows should write property-aware CSR segments"
    );

    let blocker_snapshot = engine.insert_edge(src, matching_dst, edge_type).await?;
    assert!(
        engine
            .get_neighbors_matching_csr_property_value_prototype(
                src,
                Some(edge_type),
                blocker_snapshot,
                predicate.clone(),
            )
            .await?
            .is_empty(),
        "newer topology-only memgraph row should hide the older matching value"
    );
    let old_snapshot = engine
        .get_neighbors_matching_csr_property_value_prototype(
            src,
            Some(edge_type),
            matching_snapshot,
            predicate,
        )
        .await?;
    assert_eq!(
        old_snapshot,
        vec![EdgeRecord::insert(
            src,
            matching_dst,
            edge_type,
            matching_snapshot
        )]
    );
    Ok(())
}

#[tokio::test]
async fn typed_property_insert_prototype_uses_current_schema_encoding() -> anyhow::Result<()> {
    let tmp = target_tempdir("typed-property-insert-prototype-")?;
    let config = LsmGraphConfig::new(tmp.path()).with_memgraph_capacity(64 * 1024 * 1024);
    let src = encoded(VertexLabel::Person, 1_498);
    let first_dst = encoded(VertexLabel::Person, 1_499);
    let second_dst = encoded(VertexLabel::Person, 1_500);
    let edge_type = EdgeLabel::Knows.as_i32();
    let property_id = 5;

    let engine = Engine::create(config).await?;
    let i64_epoch = engine.add_schema_property(NewPropertyEntry {
        id: property_id,
        owner: PropertyOwner::EdgeLabel(edge_type),
        name: "strength".to_string(),
        logical_type: "int64".to_string(),
        physical_encoding: "plain_i64".to_string(),
        encoding_version: 1,
        default_or_null_rule: "null".to_string(),
    })?;
    let first_snapshot = engine
        .insert_edge_with_property_values_prototype(
            src,
            first_dst,
            edge_type,
            vec![(property_id, PropertyValue::I64(42))],
        )
        .await?;
    let first_match = engine
        .get_neighbors_matching_csr_property_value_prototype(
            src,
            Some(edge_type),
            first_snapshot,
            CsrPropertyValuePredicate::equals(property_id, PropertyValue::I64(42)),
        )
        .await?;
    assert_eq!(
        first_match,
        vec![EdgeRecord::insert(
            src,
            first_dst,
            edge_type,
            first_snapshot
        )]
    );
    engine.flush_active().await?;

    let f64_epoch =
        engine.change_schema_property_encoding(property_id, "float64", "plain_f64", 2, "null")?;
    assert!(f64_epoch > i64_epoch);
    let second_snapshot = engine
        .insert_edge_with_property_values_prototype(
            src,
            second_dst,
            edge_type,
            vec![(property_id, PropertyValue::F64(42.5))],
        )
        .await?;
    let second_match = engine
        .get_neighbors_matching_csr_property_value_prototype(
            src,
            Some(edge_type),
            second_snapshot,
            CsrPropertyValuePredicate::equals(property_id, PropertyValue::F64(42.5)),
        )
        .await?;
    assert_eq!(
        second_match,
        vec![EdgeRecord::insert(
            src,
            second_dst,
            edge_type,
            second_snapshot
        )]
    );

    engine.flush_active().await?;
    let report = engine.schema_evolution_report();
    assert_eq!(
        report
            .segment_count_by_property_encoding_epoch
            .get(&i64_epoch)
            .copied(),
        Some(1)
    );
    assert_eq!(
        report
            .segment_count_by_property_encoding_epoch
            .get(&f64_epoch)
            .copied(),
        Some(1)
    );
    let after_flush_old_encoding = engine
        .get_neighbors_matching_csr_property_value_prototype(
            src,
            Some(edge_type),
            engine.current_snapshot(),
            CsrPropertyValuePredicate::equals(property_id, PropertyValue::I64(42)),
        )
        .await?;
    assert_eq!(
        after_flush_old_encoding,
        vec![EdgeRecord::insert(
            src,
            first_dst,
            edge_type,
            first_snapshot
        )]
    );
    let after_flush_new_encoding = engine
        .get_neighbors_matching_csr_property_value_prototype(
            src,
            Some(edge_type),
            engine.current_snapshot(),
            CsrPropertyValuePredicate::equals(property_id, PropertyValue::F64(42.5)),
        )
        .await?;
    assert_eq!(
        after_flush_new_encoding,
        vec![EdgeRecord::insert(
            src,
            second_dst,
            edge_type,
            second_snapshot
        )]
    );
    Ok(())
}

#[tokio::test]
async fn typed_property_insert_rejects_wrong_value_type() -> anyhow::Result<()> {
    let tmp = target_tempdir("typed-property-insert-rejects-wrong-type-")?;
    let engine = Engine::create(LsmGraphConfig::new(tmp.path())).await?;
    let src = encoded(VertexLabel::Person, 1_501);
    let dst = encoded(VertexLabel::Person, 1_502);
    let edge_type = EdgeLabel::Knows.as_i32();
    let property_id = 5;
    engine.add_schema_property(NewPropertyEntry {
        id: property_id,
        owner: PropertyOwner::EdgeLabel(edge_type),
        name: "strength".to_string(),
        logical_type: "int64".to_string(),
        physical_encoding: "plain_i64".to_string(),
        encoding_version: 1,
        default_or_null_rule: "null".to_string(),
    })?;

    let err = engine
        .insert_edge_with_property_values_prototype(
            src,
            dst,
            edge_type,
            vec![(property_id, PropertyValue::F64(1.0))],
        )
        .await
        .expect_err("plain_i64 property should reject F64 values");
    assert!(
        err.to_string().contains("plain_i64 expects"),
        "unexpected error: {err:#}"
    );
    Ok(())
}

#[tokio::test]
async fn schema_default_absent_value_predicate_matches_topology_only_rows() -> anyhow::Result<()> {
    let tmp = target_tempdir("schema-default-absent-value-predicate-")?;
    let config = LsmGraphConfig::new(tmp.path()).with_memgraph_capacity(64 * 1024 * 1024);
    let src = encoded(VertexLabel::Person, 1_503);
    let absent_dst = encoded(VertexLabel::Person, 1_504);
    let explicit_dst = encoded(VertexLabel::Person, 1_505);
    let edge_type = EdgeLabel::Knows.as_i32();
    let property_id = 5;

    let engine = Engine::create(config).await?;
    engine.add_schema_property(NewPropertyEntry {
        id: property_id,
        owner: PropertyOwner::EdgeLabel(edge_type),
        name: "strength".to_string(),
        logical_type: "int64".to_string(),
        physical_encoding: "plain_i64".to_string(),
        encoding_version: 1,
        default_or_null_rule: "default:42".to_string(),
    })?;
    let absent_snapshot = engine.insert_edge(src, absent_dst, edge_type).await?;
    let explicit_snapshot = engine
        .insert_edge_with_property_values_prototype(
            src,
            explicit_dst,
            edge_type,
            vec![(property_id, PropertyValue::I64(7))],
        )
        .await?;

    let default_predicate = engine.csr_property_value_predicate_with_current_default_prototype(
        property_id,
        PropertyValue::I64(42),
    )?;
    let default_match_before_flush = engine
        .get_neighbors_matching_csr_property_value_prototype(
            src,
            Some(edge_type),
            engine.current_snapshot(),
            default_predicate.clone(),
        )
        .await?;
    assert_eq!(
        default_match_before_flush,
        vec![EdgeRecord::insert(
            src,
            absent_dst,
            edge_type,
            absent_snapshot
        )]
    );

    let explicit_predicate = engine.csr_property_value_predicate_with_current_default_prototype(
        property_id,
        PropertyValue::I64(7),
    )?;
    let explicit_match_before_flush = engine
        .get_neighbors_matching_csr_property_value_prototype(
            src,
            Some(edge_type),
            engine.current_snapshot(),
            explicit_predicate.clone(),
        )
        .await?;
    assert_eq!(
        explicit_match_before_flush,
        vec![EdgeRecord::insert(
            src,
            explicit_dst,
            edge_type,
            explicit_snapshot
        )]
    );

    engine.flush_active().await?;
    let default_match_after_flush = engine
        .get_neighbors_matching_csr_property_value_prototype(
            src,
            Some(edge_type),
            engine.current_snapshot(),
            default_predicate,
        )
        .await?;
    assert_eq!(
        default_match_after_flush,
        vec![EdgeRecord::insert(
            src,
            absent_dst,
            edge_type,
            absent_snapshot
        )]
    );
    let explicit_match_after_flush = engine
        .get_neighbors_matching_csr_property_value_prototype(
            src,
            Some(edge_type),
            engine.current_snapshot(),
            explicit_predicate,
        )
        .await?;
    assert_eq!(
        explicit_match_after_flush,
        vec![EdgeRecord::insert(
            src,
            explicit_dst,
            edge_type,
            explicit_snapshot
        )]
    );
    Ok(())
}

#[tokio::test]
async fn schema_null_absent_value_predicate_does_not_match_topology_only_rows() -> anyhow::Result<()>
{
    let tmp = target_tempdir("schema-null-absent-value-predicate-")?;
    let engine = Engine::create(LsmGraphConfig::new(tmp.path())).await?;
    let src = encoded(VertexLabel::Person, 1_506);
    let dst = encoded(VertexLabel::Person, 1_507);
    let edge_type = EdgeLabel::Knows.as_i32();
    let property_id = 5;
    engine.add_schema_property(NewPropertyEntry {
        id: property_id,
        owner: PropertyOwner::EdgeLabel(edge_type),
        name: "strength".to_string(),
        logical_type: "int64".to_string(),
        physical_encoding: "plain_i64".to_string(),
        encoding_version: 1,
        default_or_null_rule: "null".to_string(),
    })?;
    engine.insert_edge(src, dst, edge_type).await?;

    let predicate = engine.csr_property_value_predicate_with_current_default_prototype(
        property_id,
        PropertyValue::I64(42),
    )?;
    let matches = engine
        .get_neighbors_matching_csr_property_value_prototype(
            src,
            Some(edge_type),
            engine.current_snapshot(),
            predicate,
        )
        .await?;
    assert!(
        matches.is_empty(),
        "null absent rule should not match a numeric equality predicate"
    );
    Ok(())
}

#[tokio::test]
async fn public_property_value_query_resolves_alias_and_schema_default() -> anyhow::Result<()> {
    let tmp = target_tempdir("public-property-value-query-alias-default-")?;
    let config = LsmGraphConfig::new(tmp.path()).with_memgraph_capacity(64 * 1024 * 1024);
    let src = encoded(VertexLabel::Person, 1_508);
    let absent_dst = encoded(VertexLabel::Person, 1_509);
    let explicit_dst = encoded(VertexLabel::Person, 1_510);
    let edge_type = 123;
    let property_id = 5;
    let property_alias_id = 6;

    let engine = Engine::create(config).await?;
    engine.add_schema_edge_label(
        edge_type,
        "TEST_KNOWS",
        VertexLabel::Person as i32,
        VertexLabel::Person as i32,
    )?;
    engine.add_schema_property(NewPropertyEntry {
        id: property_id,
        owner: PropertyOwner::EdgeLabel(edge_type),
        name: "strength".to_string(),
        logical_type: "int64".to_string(),
        physical_encoding: "plain_i64".to_string(),
        encoding_version: 1,
        default_or_null_rule: "default:42".to_string(),
    })?;
    engine.alias_schema_property(property_alias_id, "score", property_id)?;

    let absent_snapshot = engine.insert_edge(src, absent_dst, edge_type).await?;
    let explicit_snapshot = engine
        .insert_edge_with_property_values_prototype(
            src,
            explicit_dst,
            edge_type,
            vec![(property_alias_id, PropertyValue::I64(7))],
        )
        .await?;

    let default_match_before_flush = engine
        .get_neighbors_matching_property_value(
            src,
            edge_type,
            engine.current_snapshot(),
            property_alias_id,
            PropertyValue::I64(42),
        )
        .await?;
    assert_eq!(
        default_match_before_flush,
        vec![EdgeRecord::insert(
            src,
            absent_dst,
            edge_type,
            absent_snapshot
        )]
    );
    let explicit_match_before_flush = engine
        .get_neighbors_matching_property_value(
            src,
            edge_type,
            engine.current_snapshot(),
            property_alias_id,
            PropertyValue::I64(7),
        )
        .await?;
    assert_eq!(
        explicit_match_before_flush,
        vec![EdgeRecord::insert(
            src,
            explicit_dst,
            edge_type,
            explicit_snapshot
        )]
    );

    engine.flush_active().await?;
    let default_match_after_flush = engine
        .get_neighbors_matching_property_value(
            src,
            edge_type,
            engine.current_snapshot(),
            property_alias_id,
            PropertyValue::I64(42),
        )
        .await?;
    assert_eq!(
        default_match_after_flush,
        vec![EdgeRecord::insert(
            src,
            absent_dst,
            edge_type,
            absent_snapshot
        )]
    );
    let explicit_match_after_flush = engine
        .get_neighbors_matching_property_value(
            src,
            edge_type,
            engine.current_snapshot(),
            property_alias_id,
            PropertyValue::I64(7),
        )
        .await?;
    assert_eq!(
        explicit_match_after_flush,
        vec![EdgeRecord::insert(
            src,
            explicit_dst,
            edge_type,
            explicit_snapshot
        )]
    );
    Ok(())
}

#[tokio::test]
async fn public_property_value_query_resolves_edge_label_alias() -> anyhow::Result<()> {
    let tmp = target_tempdir("public-property-value-query-edge-alias-")?;
    let config = LsmGraphConfig::new(tmp.path()).with_memgraph_capacity(64 * 1024 * 1024);
    let src = encoded(VertexLabel::Person, 1_517);
    let dst = encoded(VertexLabel::Person, 1_518);
    let canonical_edge_type = 223;
    let alias_edge_type = 224;
    let property_id = 5;

    let engine = Engine::create(config.clone()).await?;
    engine.add_schema_edge_label(
        canonical_edge_type,
        "CANONICAL_KNOWS",
        VertexLabel::Person as i32,
        VertexLabel::Person as i32,
    )?;
    engine.add_schema_property(NewPropertyEntry {
        id: property_id,
        owner: PropertyOwner::EdgeLabel(canonical_edge_type),
        name: "strength".to_string(),
        logical_type: "int64".to_string(),
        physical_encoding: "plain_i64".to_string(),
        encoding_version: 1,
        default_or_null_rule: "null".to_string(),
    })?;
    let snapshot = engine
        .insert_edge_with_property_values_prototype(
            src,
            dst,
            canonical_edge_type,
            vec![(property_id, PropertyValue::I64(42))],
        )
        .await?;
    let alias_epoch =
        engine.alias_schema_edge_label(alias_edge_type, "ALIAS_KNOWS", canonical_edge_type)?;
    assert_eq!(
        engine
            .schema_catalog_snapshot()
            .resolve_edge_label(alias_edge_type, alias_epoch),
        Some(canonical_edge_type)
    );

    let before_flush = engine
        .get_neighbors_matching_property_value(
            src,
            alias_edge_type,
            engine.current_snapshot(),
            property_id,
            PropertyValue::I64(42),
        )
        .await?;
    assert_eq!(
        before_flush,
        vec![EdgeRecord::insert(src, dst, canonical_edge_type, snapshot)]
    );

    engine.flush_active().await?;
    let after_flush = engine
        .get_neighbors_matching_property_value(
            src,
            alias_edge_type,
            engine.current_snapshot(),
            property_id,
            PropertyValue::I64(42),
        )
        .await?;
    assert_eq!(
        after_flush,
        vec![EdgeRecord::insert(src, dst, canonical_edge_type, snapshot)]
    );

    let report = engine.schema_evolution_report();
    assert!(
        report.alias_entries.iter().any(|entry| {
            entry.entry_kind == "edge_label"
                && entry.id == alias_edge_type.to_string()
                && entry.canonical_id == canonical_edge_type.to_string()
                && entry.visible_at_current_epoch
        }),
        "schema-evolution report should expose the visible edge-label alias"
    );

    drop(engine);
    let reopened = Engine::open(config).await?;
    let reopened_match = reopened
        .get_neighbors_matching_property_value(
            src,
            alias_edge_type,
            reopened.current_snapshot(),
            property_id,
            PropertyValue::I64(42),
        )
        .await?;
    assert_eq!(
        reopened_match,
        vec![EdgeRecord::insert(src, dst, canonical_edge_type, snapshot)]
    );
    Ok(())
}

#[tokio::test]
async fn public_property_value_query_uses_row_encoding_epoch_after_type_change(
) -> anyhow::Result<()> {
    let tmp = target_tempdir("public-property-value-query-encoding-epoch-")?;
    let config = LsmGraphConfig::new(tmp.path()).with_memgraph_capacity(64 * 1024 * 1024);
    let src = encoded(VertexLabel::Person, 1_514);
    let old_dst = encoded(VertexLabel::Person, 1_515);
    let new_dst = encoded(VertexLabel::Person, 1_516);
    let edge_type = EdgeLabel::Knows.as_i32();
    let property_id = 5;

    let engine = Engine::create(config.clone()).await?;
    engine.add_schema_property(NewPropertyEntry {
        id: property_id,
        owner: PropertyOwner::EdgeLabel(edge_type),
        name: "strength".to_string(),
        logical_type: "int64".to_string(),
        physical_encoding: "plain_i64".to_string(),
        encoding_version: 1,
        default_or_null_rule: "null".to_string(),
    })?;
    let old_snapshot = engine
        .insert_edge_with_property_values_prototype(
            src,
            old_dst,
            edge_type,
            vec![(property_id, PropertyValue::I64(42))],
        )
        .await?;
    engine.flush_active().await?;

    let f64_epoch =
        engine.change_schema_property_encoding(property_id, "float64", "plain_f64", 2, "null")?;
    let new_snapshot = engine
        .insert_edge_with_property_values_prototype(
            src,
            new_dst,
            edge_type,
            vec![(property_id, PropertyValue::F64(42.5))],
        )
        .await?;
    engine.flush_active().await?;

    let old_encoding_match = engine
        .get_neighbors_matching_property_value(
            src,
            edge_type,
            engine.current_snapshot(),
            property_id,
            PropertyValue::I64(42),
        )
        .await?;
    assert_eq!(
        old_encoding_match,
        vec![EdgeRecord::insert(src, old_dst, edge_type, old_snapshot)]
    );
    let new_encoding_match = engine
        .get_neighbors_matching_property_value(
            src,
            edge_type,
            engine.current_snapshot(),
            property_id,
            PropertyValue::F64(42.5),
        )
        .await?;
    assert_eq!(
        new_encoding_match,
        vec![EdgeRecord::insert(src, new_dst, edge_type, new_snapshot)]
    );

    let report = engine.schema_evolution_report();
    assert!(
        report
            .property_encoding_migration_candidates
            .iter()
            .any(|entry| {
                entry.property_id == property_id
                    && entry.current_encoding_epoch == f64_epoch
                    && entry.old_encoding_epoch < f64_epoch
                    && entry.blocked_by_snapshot_gc
            }),
        "old property encoding should remain visible as a migration candidate"
    );

    drop(engine);
    let reopened = Engine::open(config).await?;
    let reopened_old_encoding_match = reopened
        .get_neighbors_matching_property_value(
            src,
            edge_type,
            reopened.current_snapshot(),
            property_id,
            PropertyValue::I64(42),
        )
        .await?;
    assert_eq!(
        reopened_old_encoding_match,
        vec![EdgeRecord::insert(src, old_dst, edge_type, old_snapshot)]
    );
    let reopened_new_encoding_match = reopened
        .get_neighbors_matching_property_value(
            src,
            edge_type,
            reopened.current_snapshot(),
            property_id,
            PropertyValue::F64(42.5),
        )
        .await?;
    assert_eq!(
        reopened_new_encoding_match,
        vec![EdgeRecord::insert(src, new_dst, edge_type, new_snapshot)]
    );
    Ok(())
}

#[tokio::test]
async fn public_property_value_query_rejects_wrong_property_owner() -> anyhow::Result<()> {
    let tmp = target_tempdir("public-property-value-query-owner-check-")?;
    let engine = Engine::create(LsmGraphConfig::new(tmp.path())).await?;
    let src = encoded(VertexLabel::Person, 1_511);
    let edge_type = EdgeLabel::Knows.as_i32();
    let other_edge_type = 321;

    engine.add_schema_property(NewPropertyEntry {
        id: 5,
        owner: PropertyOwner::EdgeLabel(edge_type),
        name: "strength".to_string(),
        logical_type: "int64".to_string(),
        physical_encoding: "plain_i64".to_string(),
        encoding_version: 1,
        default_or_null_rule: "null".to_string(),
    })?;
    let wrong_edge_err = engine
        .get_neighbors_matching_property_value(
            src,
            other_edge_type,
            engine.current_snapshot(),
            5,
            PropertyValue::I64(42),
        )
        .await
        .expect_err("edge property should reject the wrong edge type");
    assert!(
        wrong_edge_err.to_string().contains("belongs to edge label"),
        "unexpected error: {wrong_edge_err:#}"
    );

    engine.add_schema_property(NewPropertyEntry {
        id: 7,
        owner: PropertyOwner::VertexLabel(VertexLabel::Person as i32),
        name: "age".to_string(),
        logical_type: "int64".to_string(),
        physical_encoding: "plain_i64".to_string(),
        encoding_version: 1,
        default_or_null_rule: "null".to_string(),
    })?;
    let vertex_property_err = engine
        .get_neighbors_matching_property_value(
            src,
            edge_type,
            engine.current_snapshot(),
            7,
            PropertyValue::I64(42),
        )
        .await
        .expect_err("vertex property should reject edge query");
    assert!(
        vertex_property_err
            .to_string()
            .contains("belongs to vertex label"),
        "unexpected error: {vertex_property_err:#}"
    );
    Ok(())
}

#[tokio::test]
async fn drop_property_hides_current_value_query_but_keeps_topology_readable() -> anyhow::Result<()>
{
    let tmp = target_tempdir("drop-property-keeps-topology-readable-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(1)
        .with_semantic_budget_min_exact_bytes(1);
    let src = encoded(VertexLabel::Person, 1_512);
    let dst = encoded(VertexLabel::Person, 1_513);
    let edge_type = EdgeLabel::Knows.as_i32();
    let property_id = 5;

    let engine = Engine::create(config.clone()).await?;
    let property_epoch = engine.add_schema_property(NewPropertyEntry {
        id: property_id,
        owner: PropertyOwner::EdgeLabel(edge_type),
        name: "strength".to_string(),
        logical_type: "int64".to_string(),
        physical_encoding: "plain_i64".to_string(),
        encoding_version: 1,
        default_or_null_rule: "null".to_string(),
    })?;
    let insert_snapshot = engine
        .insert_edge_with_property_values_prototype(
            src,
            dst,
            edge_type,
            vec![(property_id, PropertyValue::I64(42))],
        )
        .await?;
    engine.flush_active().await?;

    let before_drop = engine
        .get_neighbors_matching_property_value(
            src,
            edge_type,
            engine.current_snapshot(),
            property_id,
            PropertyValue::I64(42),
        )
        .await?;
    assert_eq!(
        before_drop,
        vec![EdgeRecord::insert(src, dst, edge_type, insert_snapshot)]
    );

    let drop_epoch = engine.drop_schema_property(property_id)?;
    assert_eq!(
        engine
            .schema_catalog_snapshot()
            .resolve_property(property_id, property_epoch),
        Some(property_id)
    );
    assert_eq!(
        engine
            .schema_catalog_snapshot()
            .resolve_property(property_id, drop_epoch),
        None
    );

    let current_topology = engine
        .get_neighbors_typed(src, edge_type, engine.current_snapshot())
        .await?;
    assert_eq!(
        current_topology,
        vec![EdgeRecord::insert(src, dst, edge_type, insert_snapshot)]
    );
    let old_snapshot_topology = engine
        .get_neighbors_typed(src, edge_type, insert_snapshot)
        .await?;
    assert_eq!(
        old_snapshot_topology,
        vec![EdgeRecord::insert(src, dst, edge_type, insert_snapshot)]
    );

    let current_value_query_err = engine
        .get_neighbors_matching_property_value(
            src,
            edge_type,
            engine.current_snapshot(),
            property_id,
            PropertyValue::I64(42),
        )
        .await
        .expect_err("dropped property should not be visible to current value queries");
    assert!(
        current_value_query_err
            .to_string()
            .contains("property id 5 is not visible"),
        "unexpected error: {current_value_query_err:#}"
    );

    let report = engine.schema_evolution_report();
    assert!(
        report.dropped_entries.iter().any(|entry| {
            entry.entry_kind == "property"
                && entry.id == property_id.to_string()
                && entry.dropped_at_epoch == Some(drop_epoch)
        }),
        "schema-evolution report should expose the dropped property boundary"
    );

    drop(engine);
    let reopened = Engine::open(config).await?;
    assert_eq!(reopened.current_schema_epoch(), drop_epoch);
    let reopened_topology = reopened
        .get_neighbors_typed(src, edge_type, reopened.current_snapshot())
        .await?;
    assert_eq!(
        reopened_topology,
        vec![EdgeRecord::insert(src, dst, edge_type, insert_snapshot)]
    );
    let reopened_value_query_err = reopened
        .get_neighbors_matching_property_value(
            src,
            edge_type,
            reopened.current_snapshot(),
            property_id,
            PropertyValue::I64(42),
        )
        .await
        .expect_err("dropped property should stay hidden after reopen");
    assert!(
        reopened_value_query_err
            .to_string()
            .contains("property id 5 is not visible"),
        "unexpected error: {reopened_value_query_err:#}"
    );
    Ok(())
}

#[tokio::test]
async fn property_required_predicate_prunes_exact_absent_segments() -> anyhow::Result<()> {
    let tmp = target_tempdir("property-required-prunes-exact-absent-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(1)
        .with_semantic_budget_min_exact_bytes(1);
    let src = encoded(VertexLabel::Person, 1_500);
    let edge_type = EdgeLabel::Knows.as_i32();
    let property_id = 5;

    let engine = Engine::create(config).await?;
    engine.add_schema_property(NewPropertyEntry {
        id: property_id,
        owner: PropertyOwner::EdgeLabel(edge_type),
        name: "strength".to_string(),
        logical_type: "int64".to_string(),
        physical_encoding: "plain_i64".to_string(),
        encoding_version: 1,
        default_or_null_rule: "null".to_string(),
    })?;
    engine
        .insert_edge(src, encoded(VertexLabel::Person, 1_501), edge_type)
        .await?;
    engine
        .insert_edge(src, encoded(VertexLabel::Person, 1_502), edge_type)
        .await?;
    engine.flush_active().await?;

    let required = engine
        .get_neighbors_by_signature(
            GraphAccessSignature::neighbor_scan(src, Some(edge_type))
                .with_required_property(property_id),
            engine.current_snapshot(),
        )
        .await?;
    assert!(
        required.is_empty(),
        "required-property predicate should prune exact-absent segments"
    );

    let absent_or_default = engine
        .get_neighbors_by_signature(
            GraphAccessSignature::neighbor_scan(src, Some(edge_type))
                .with_absent_or_default_property(property_id),
            engine.current_snapshot(),
        )
        .await?;
    assert_eq!(
        absent_or_default.len(),
        2,
        "default/null predicate should keep exact-absent segments"
    );
    Ok(())
}

#[tokio::test]
async fn legacy_missing_property_summary_keeps_required_property_conservative() -> anyhow::Result<()>
{
    let tmp = target_tempdir("legacy-missing-property-summary-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(1)
        .with_semantic_budget_min_exact_bytes(1);
    let src = encoded(VertexLabel::Person, 1_600);
    let edge_type = EdgeLabel::Knows.as_i32();
    let property_id = 5;

    let engine = Engine::create(config.clone()).await?;
    engine.add_schema_property(NewPropertyEntry {
        id: property_id,
        owner: PropertyOwner::EdgeLabel(edge_type),
        name: "strength".to_string(),
        logical_type: "int64".to_string(),
        physical_encoding: "plain_i64".to_string(),
        encoding_version: 1,
        default_or_null_rule: "null".to_string(),
    })?;
    engine
        .insert_edge(src, encoded(VertexLabel::Person, 1_601), edge_type)
        .await?;
    engine
        .insert_edge(src, encoded(VertexLabel::Person, 1_602), edge_type)
        .await?;
    engine.flush_active().await?;

    let exact_absent = engine
        .get_neighbors_by_signature(
            GraphAccessSignature::neighbor_scan(src, Some(edge_type))
                .with_required_property(property_id),
            engine.current_snapshot(),
        )
        .await?;
    assert!(
        exact_absent.is_empty(),
        "fresh exact-absent metadata should prune required-property predicate"
    );
    drop(engine);

    strip_manifest_property_summary_fields(tmp.path())?;
    let reopened = Engine::open(config).await?;
    let guard = reopened.version_guard();
    let meta = guard
        .version()
        .levels
        .get(0)
        .and_then(|level| level.first())
        .expect("test should have one L0 segment");
    assert_eq!(
        meta.property_summary_completeness,
        SemanticSummaryCompleteness::Unknown
    );
    assert!(meta.may_contain_property(property_id));

    let conservative = reopened
        .get_neighbors_by_signature(
            GraphAccessSignature::neighbor_scan(src, Some(edge_type))
                .with_required_property(property_id),
            reopened.current_snapshot(),
        )
        .await?;
    assert_eq!(
        conservative.len(),
        2,
        "legacy missing property summary must not prune required-property predicate"
    );
    Ok(())
}

#[tokio::test]
async fn new_edge_label_prunes_exact_segments_but_reads_mixed_segments() -> anyhow::Result<()> {
    let exact_tmp = target_tempdir("new-edge-label-prunes-exact-")?;
    let exact_config = LsmGraphConfig::new(exact_tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(1)
        .with_semantic_budget_min_exact_bytes(1);
    let src = encoded(VertexLabel::Person, 1_700);
    let old_edge_type = EdgeLabel::Knows.as_i32();
    let new_edge_type = 99;
    let exact_engine = Engine::create(exact_config).await?;
    exact_engine
        .insert_edge(src, encoded(VertexLabel::Person, 1_701), old_edge_type)
        .await?;
    exact_engine.flush_active().await?;
    exact_engine.add_schema_edge_label(
        new_edge_type,
        "REACTS_TO",
        VertexLabel::Person as i32,
        VertexLabel::Comment as i32,
    )?;
    exact_engine.metrics().reset();
    let exact_result = exact_engine
        .get_neighbors_typed(src, new_edge_type, exact_engine.current_snapshot())
        .await?;
    let exact_candidates = exact_engine.metrics().snapshot_json()["csr"]["candidate_l0_segments"]
        .as_u64()
        .unwrap_or_default();
    assert!(exact_result.is_empty());
    assert_eq!(
        exact_candidates, 0,
        "exact old edge-type segment should be safely pruned for a newly added edge label"
    );

    let mixed_tmp = target_tempdir("new-edge-label-keeps-mixed-")?;
    let mixed_config = LsmGraphConfig::new(mixed_tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::LabelOnly);
    let mixed_engine = Engine::create(mixed_config).await?;
    mixed_engine
        .insert_edge(src, encoded(VertexLabel::Person, 1_702), old_edge_type)
        .await?;
    mixed_engine.flush_active().await?;
    {
        let guard = mixed_engine.version_guard();
        assert!(
            guard.version().levels[0]
                .iter()
                .any(|meta| meta.edge_type_partition == MIXED_EDGE_TYPE),
            "test setup should create a mixed L0 segment"
        );
    }
    mixed_engine.add_schema_edge_label(
        new_edge_type,
        "REACTS_TO",
        VertexLabel::Person as i32,
        VertexLabel::Comment as i32,
    )?;
    mixed_engine.metrics().reset();
    let mixed_result = mixed_engine
        .get_neighbors_typed(src, new_edge_type, mixed_engine.current_snapshot())
        .await?;
    let mixed_candidates = mixed_engine.metrics().snapshot_json()["csr"]["candidate_l0_segments"]
        .as_u64()
        .unwrap_or_default();
    assert!(mixed_result.is_empty());
    assert!(
        mixed_candidates > 0,
        "mixed segment must be read conservatively for a newly added edge label"
    );
    Ok(())
}

#[tokio::test]
async fn feedback_compaction_picks_hot_l0_partition_and_reduces_candidates() -> anyhow::Result<()> {
    let tmp = target_tempdir("feedback-compaction-picks-hot-partition-")?;
    let config = feedback_compaction_config(tmp.path());
    let engine = Engine::create(config).await?;
    let src = encoded(VertexLabel::Person, 20_000);
    let edge_type = EdgeLabel::Knows.as_i32();

    for i in 0..3u64 {
        engine
            .insert_edge(src, encoded(VertexLabel::Person, 30_000 + i), edge_type)
            .await?;
        engine.flush_active().await?;
    }

    let before_candidates = record_feedback_queries(&engine, src, edge_type, 3, 3).await?;
    assert!(
        before_candidates >= 9,
        "three repeated queries over three matching L0 files should expose high L0 read amplification"
    );

    let decision = engine
        .compact_best_l0_partition_by_score()
        .await?
        .expect("hot L0 partition should be selected by feedback score");
    assert_eq!(decision.key.src_label, VertexLabel::Person as i32);
    assert_eq!(decision.key.edge_type, edge_type);
    assert!(decision.key.range_start <= src && src <= decision.key.range_end);
    assert_eq!(decision.query_count, 3);
    assert!(
        decision.avg_candidate_segments >= 3.0,
        "feedback score should see all matching L0 segments as candidates"
    );
    assert!(
        decision.selected_l0_segments >= 3,
        "auto-pick should rewrite all matching hot L0 files in the selected range"
    );
    assert!(
        !decision.outputs.is_empty(),
        "selected hot L0 partition should produce an L1 output segment"
    );

    let after_candidates = record_feedback_queries(&engine, src, edge_type, 3, 1).await?;
    assert_eq!(
        after_candidates, 0,
        "after feedback compaction the hot source should no longer probe L0 files"
    );
    Ok(())
}

#[tokio::test]
async fn feedback_compaction_priority_shifts_after_metrics_reset() -> anyhow::Result<()> {
    let tmp = target_tempdir("feedback-compaction-workload-shift-")?;
    let config = feedback_compaction_config(tmp.path());
    let engine = Engine::create(config).await?;
    let src_a = encoded(VertexLabel::Person, 40_000);
    let src_b = encoded(VertexLabel::Person, 41_024);
    let edge_type = EdgeLabel::Knows.as_i32();

    for i in 0..3u64 {
        engine
            .insert_edge(src_a, encoded(VertexLabel::Person, 50_000 + i), edge_type)
            .await?;
        engine.flush_active().await?;
    }
    for i in 0..3u64 {
        engine
            .insert_edge(src_b, encoded(VertexLabel::Person, 60_000 + i), edge_type)
            .await?;
        engine.flush_active().await?;
    }

    let phase_a_candidates = record_feedback_queries(&engine, src_a, edge_type, 3, 3).await?;
    assert!(phase_a_candidates >= 9);
    let phase_a = engine
        .compact_best_l0_partition_by_score()
        .await?
        .expect("phase A hot partition should be selected");
    assert!(phase_a.key.range_start <= src_a && src_a <= phase_a.key.range_end);
    assert!(
        src_b < phase_a.key.range_start || src_b > phase_a.key.range_end,
        "phase A selection should be scoped to the first hot source range"
    );
    assert!(phase_a.selected_l0_segments >= 3);

    let after_phase_a_candidates = record_feedback_queries(&engine, src_a, edge_type, 3, 1).await?;
    assert_eq!(
        after_phase_a_candidates, 0,
        "phase A source should not fall back to probing phase B L0 files after phase A compaction"
    );

    let phase_b_candidates = record_feedback_queries(&engine, src_b, edge_type, 3, 3).await?;
    assert!(phase_b_candidates >= 9);
    let phase_b = engine
        .compact_best_l0_partition_by_score()
        .await?
        .expect("phase B hot partition should be selected after feedback reset");
    assert!(phase_b.key.range_start <= src_b && src_b <= phase_b.key.range_end);
    assert_ne!(
        phase_a.key.range_start, phase_b.key.range_start,
        "auto-pick priority should move to the new hot source range"
    );
    assert!(phase_b.selected_l0_segments >= 3);
    assert!(
        !phase_b.outputs.is_empty(),
        "phase B compaction should rewrite the new hot partition"
    );

    let after_b_candidates = record_feedback_queries(&engine, src_b, edge_type, 3, 1).await?;
    assert_eq!(
        after_b_candidates, 0,
        "the phase B hot source should no longer probe L0 files after its feedback compaction"
    );
    Ok(())
}

#[tokio::test]
async fn incremental_semantic_index_matches_reopen_full_rebuild() -> anyhow::Result<()> {
    let tmp = target_tempdir("incremental-semantic-index-matches-reopen-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_semantic_budget_min_edge_type_bytes(1)
        .with_semantic_budget_min_exact_bytes(1);
    let engine = Engine::create(config.clone()).await?;
    let query_src = encoded(VertexLabel::Person, 1_000);
    let edge_type = EdgeLabel::Knows.as_i32();

    for (src_idx, bracket_src) in [
        encoded(VertexLabel::Person, 900),
        encoded(VertexLabel::Person, 1_100),
    ]
    .into_iter()
    .enumerate()
    {
        for i in 0..17u64 {
            engine
                .insert_edge(
                    bracket_src,
                    encoded(VertexLabel::Person, 50_000 + (src_idx as u64 * 1_000) + i),
                    edge_type,
                )
                .await?;
        }
    }
    engine.flush_active().await?;

    engine
        .insert_edge(query_src, encoded(VertexLabel::Person, 60_000), edge_type)
        .await?;
    engine.flush_active().await?;

    engine.metrics().reset();
    let live_neighbors = engine
        .get_neighbors_typed(query_src, edge_type, engine.current_snapshot())
        .await?;
    let live_candidates = engine.metrics().snapshot_json()["csr"]["candidate_l0_segments"]
        .as_u64()
        .unwrap_or_default();
    assert_eq!(live_neighbors.len(), 1);
    assert_eq!(
        live_candidates, 1,
        "incremental semantic directory should prune the bracketing medium-degree segment"
    );

    drop(engine);
    let reopened = Engine::open(config).await?;
    reopened.metrics().reset();
    let reopened_neighbors = reopened
        .get_neighbors_typed(query_src, edge_type, reopened.current_snapshot())
        .await?;
    let reopened_candidates = reopened.metrics().snapshot_json()["csr"]["candidate_l0_segments"]
        .as_u64()
        .unwrap_or_default();

    assert_eq!(reopened_neighbors.len(), live_neighbors.len());
    assert_eq!(
        reopened_candidates, live_candidates,
        "incremental semantic index maintenance should match reopen-time full rebuild"
    );
    Ok(())
}

#[tokio::test]
async fn layout_label_only_metadata_sanitized() -> anyhow::Result<()> {
    let tmp = target_tempdir("label-only-metadata-sanitized-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::LabelOnly);
    let engine = Engine::create(config).await?;

    let person_src = encoded(VertexLabel::Person, 1);
    let tag_src = encoded(VertexLabel::Tag, 2);

    engine
        .insert_edge(
            person_src,
            encoded(VertexLabel::Person, 100),
            EdgeLabel::Knows.as_i32(),
        )
        .await?;
    engine
        .insert_edge(
            person_src,
            encoded(VertexLabel::Tag, 200),
            EdgeLabel::HasInterest.as_i32(),
        )
        .await?;
    engine
        .insert_edge(
            tag_src,
            encoded(VertexLabel::Forum, 300),
            EdgeLabel::HasTag.as_i32(),
        )
        .await?;
    engine.flush_active().await?;

    let guard = engine.version_guard();
    let l0 = &guard.version().levels[0];

    let person_segment = l0
        .iter()
        .find(|m| m.src_label == VertexLabel::Person as i32)
        .expect("label-only layout should create a Person segment");

    assert!(
        person_segment.dst_label == UNKNOWN_SOURCE_LABEL,
        "dst_label must be forced to UNKNOWN"
    );
    assert!(
        person_segment.edge_type_partition == MIXED_EDGE_TYPE,
        "edge_type_partition must be forced to MIXED"
    );
    assert!(
        person_segment.degree_class == DegreeClass::Mixed,
        "degree_class must be forced to Mixed"
    );
    assert!(
        !person_segment.degree_class_exact,
        "degree_class_exact must be false"
    );
    assert!(
        person_segment.src_label == VertexLabel::Person as i32,
        "src_label should be exact from vertex IDs"
    );

    let tag_segment = l0
        .iter()
        .find(|m| m.src_label == VertexLabel::Tag as i32)
        .expect("label-only layout should create a Tag segment");

    assert_eq!(tag_segment.src_label, VertexLabel::Tag as i32);
    assert_eq!(tag_segment.dst_label, UNKNOWN_SOURCE_LABEL);
    assert_eq!(tag_segment.edge_type_partition, MIXED_EDGE_TYPE);
    assert_eq!(tag_segment.degree_class, DegreeClass::Mixed);
    assert!(!tag_segment.degree_class_exact);

    let tag_signature =
        GraphAccessSignature::neighbor_scan(tag_src, None).with_degree_class(DegreeClass::Low);
    let tag_neighbors = engine
        .get_neighbors_by_signature(tag_signature, engine.current_snapshot())
        .await?;
    assert_eq!(tag_neighbors.len(), 1);
    assert_eq!(tag_neighbors[0].dst, encoded(VertexLabel::Forum, 300));

    Ok(())
}

#[tokio::test]
async fn layout_edge_type_only_metadata_sanitized() -> anyhow::Result<()> {
    let tmp = target_tempdir("edge-type-only-metadata-sanitized-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::EdgeTypeOnly);
    let engine = Engine::create(config).await?;

    let person_src = encoded(VertexLabel::Person, 1);
    let comment_src = encoded(VertexLabel::Comment, 2);

    engine
        .insert_edge(
            person_src,
            encoded(VertexLabel::Person, 100),
            EdgeLabel::Knows.as_i32(),
        )
        .await?;
    engine
        .insert_edge(
            person_src,
            encoded(VertexLabel::Tag, 200),
            EdgeLabel::HasInterest.as_i32(),
        )
        .await?;
    engine
        .insert_edge(
            comment_src,
            encoded(VertexLabel::Person, 300),
            EdgeLabel::HasCreator.as_i32(),
        )
        .await?;
    engine.flush_active().await?;

    let guard = engine.version_guard();
    let l0 = &guard.version().levels[0];

    let knows_segment = l0
        .iter()
        .find(|m| m.edge_type_partition == EdgeLabel::Knows.as_i32())
        .expect("edge-type-only layout should create a Knows segment");

    assert!(
        knows_segment.src_label == UNKNOWN_SOURCE_LABEL,
        "src_label must be forced to UNKNOWN"
    );
    assert!(
        knows_segment.dst_label == UNKNOWN_SOURCE_LABEL,
        "dst_label must be forced to UNKNOWN"
    );
    assert!(
        knows_segment.degree_class == DegreeClass::Mixed,
        "degree_class must be forced to Mixed"
    );
    assert!(
        !knows_segment.degree_class_exact,
        "degree_class_exact must be false"
    );
    assert!(
        knows_segment.edge_type_partition == EdgeLabel::Knows.as_i32(),
        "edge_type_partition should be exact from edges"
    );

    let has_creator_segment = l0
        .iter()
        .find(|m| m.edge_type_partition == EdgeLabel::HasCreator.as_i32())
        .expect("edge-type-only layout should create a HasCreator segment");

    assert_eq!(has_creator_segment.src_label, UNKNOWN_SOURCE_LABEL);
    assert_eq!(has_creator_segment.dst_label, UNKNOWN_SOURCE_LABEL);
    assert_eq!(
        has_creator_segment.edge_type_partition,
        EdgeLabel::HasCreator.as_i32()
    );
    assert_eq!(has_creator_segment.degree_class, DegreeClass::Mixed);
    assert!(!has_creator_segment.degree_class_exact);

    let query_src = encoded(VertexLabel::Comment, 2);
    let signature =
        GraphAccessSignature::neighbor_scan(query_src, Some(EdgeLabel::HasCreator.as_i32()));
    let neighbors = engine
        .get_neighbors_by_signature(signature, engine.current_snapshot())
        .await?;
    assert_eq!(neighbors.len(), 1);
    assert_eq!(neighbors[0].dst, encoded(VertexLabel::Person, 300));

    Ok(())
}

#[tokio::test]
async fn layout_degree_only_metadata_sanitized() -> anyhow::Result<()> {
    let tmp = target_tempdir("degree-only-metadata-sanitized-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::DegreeOnly);
    let engine = Engine::create(config).await?;

    let low_src = encoded(VertexLabel::Person, 1);
    let medium_src = encoded(VertexLabel::Forum, 2);

    engine
        .insert_edge(
            low_src,
            encoded(VertexLabel::Person, 100),
            EdgeLabel::Knows.as_i32(),
        )
        .await?;
    for i in 0..30u64 {
        engine
            .insert_edge(
                medium_src,
                encoded(VertexLabel::Comment, 200 + i),
                EdgeLabel::HasCreator.as_i32(),
            )
            .await?;
    }
    engine.flush_active().await?;

    let guard = engine.version_guard();
    let l0 = &guard.version().levels[0];

    let low_segment = l0
        .iter()
        .find(|m| m.degree_class == DegreeClass::Low)
        .expect("degree-only layout should create a Low segment");

    assert!(
        low_segment.src_label == UNKNOWN_SOURCE_LABEL,
        "src_label must be forced to UNKNOWN"
    );
    assert!(
        low_segment.dst_label == UNKNOWN_SOURCE_LABEL,
        "dst_label must be forced to UNKNOWN"
    );
    assert!(
        low_segment.edge_type_partition == MIXED_EDGE_TYPE,
        "edge_type_partition must be forced to MIXED"
    );
    assert!(
        low_segment.degree_class_exact,
        "degree_class_exact must be true for degree-only"
    );
    assert!(
        matches!(low_segment.degree_class, DegreeClass::Low),
        "degree_class should be exact from degree stats"
    );

    let medium_segment = l0
        .iter()
        .find(|m| m.degree_class == DegreeClass::Medium)
        .expect("degree-only layout should create a Medium segment");

    assert_eq!(medium_segment.src_label, UNKNOWN_SOURCE_LABEL);
    assert_eq!(medium_segment.dst_label, UNKNOWN_SOURCE_LABEL);
    assert_eq!(medium_segment.edge_type_partition, MIXED_EDGE_TYPE);
    assert!(medium_segment.degree_class_exact);
    assert!(matches!(medium_segment.degree_class, DegreeClass::Medium));

    let signature = GraphAccessSignature::neighbor_scan(low_src, Some(EdgeLabel::Knows.as_i32()))
        .with_degree_class(DegreeClass::Low);
    let neighbors = engine
        .get_neighbors_by_signature(signature, engine.current_snapshot())
        .await?;
    assert_eq!(neighbors.len(), 1);
    assert_eq!(neighbors[0].dst, encoded(VertexLabel::Person, 100));

    Ok(())
}

#[tokio::test]
async fn layout_combination_label_etype_no_cross_pruning() -> anyhow::Result<()> {
    let label_tmp = target_tempdir("cross-prune-label-only-")?;
    let label_config = LsmGraphConfig::new(label_tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::LabelOnly);
    let label_engine = Engine::create(label_config).await?;

    let label_src = encoded(VertexLabel::Person, 1);
    let label_dst = encoded(VertexLabel::Tag, 2);

    label_engine
        .insert_edge(label_src, label_dst, EdgeLabel::HasInterest.as_i32())
        .await?;
    label_engine.flush_active().await?;

    let etype_tmp = target_tempdir("cross-prune-etype-only-")?;
    let etype_config = LsmGraphConfig::new(etype_tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::EdgeTypeOnly);
    let etype_engine = Engine::create(etype_config).await?;

    let etype_src = encoded(VertexLabel::Forum, 3);

    etype_engine
        .insert_edge(
            etype_src,
            encoded(VertexLabel::Person, 4),
            EdgeLabel::HasModerator.as_i32(),
        )
        .await?;
    etype_engine.flush_active().await?;

    let label_guard = label_engine.version_guard();
    let label_l0 = &label_guard.version().levels[0];
    let label_segment = label_l0
        .iter()
        .find(|m| m.src_label == VertexLabel::Person as i32)
        .expect("label-only should have Person segment");
    assert_eq!(
        label_segment.dst_label, UNKNOWN_SOURCE_LABEL,
        "label-only segment must have UNKNOWN dst_label"
    );

    let etype_guard = etype_engine.version_guard();
    let etype_l0 = &etype_guard.version().levels[0];
    let etype_segment = etype_l0
        .iter()
        .find(|m| m.edge_type_partition == EdgeLabel::HasModerator.as_i32())
        .expect("edge-type-only should have HasModerator segment");
    assert_eq!(
        etype_segment.src_label, UNKNOWN_SOURCE_LABEL,
        "edge-type-only segment must have UNKNOWN src_label"
    );

    let query_with_dst_label =
        GraphAccessSignature::neighbor_scan(label_src, Some(EdgeLabel::HasInterest.as_i32()));
    let label_candidates = label_engine
        .get_neighbors_by_signature(
            query_with_dst_label.clone(),
            label_engine.current_snapshot(),
        )
        .await?;
    assert_eq!(
        label_candidates.len(), 1,
        "label-only segment should NOT be pruned by dst_label filter since it has UNKNOWN dst_label"
    );

    let query_with_src_label =
        GraphAccessSignature::neighbor_scan(etype_src, Some(EdgeLabel::HasModerator.as_i32()));
    let etype_candidates = etype_engine
        .get_neighbors_by_signature(query_with_src_label, etype_engine.current_snapshot())
        .await?;
    assert_eq!(
        etype_candidates.len(),
        1,
        "edge-type-only segment should be found by its exact edge_type"
    );

    let mismatched_src_label = GraphAccessSignature::neighbor_scan(
        encoded(VertexLabel::Person, 99),
        Some(EdgeLabel::HasModerator.as_i32()),
    );
    let etype_mismatch = etype_engine
        .get_neighbors_by_signature(mismatched_src_label, etype_engine.current_snapshot())
        .await?;
    assert_eq!(
        etype_mismatch.len(),
        0,
        "edge-type-only segment should not return false positives for src mismatch"
    );

    Ok(())
}

#[tokio::test]
#[ignore = "LsmGraphStyle layout policy not yet implemented with semantic pruning disabled"]
async fn layout_lsmgraph_style_no_semantic_pruning() -> anyhow::Result<()> {
    let tmp = target_tempdir("lsmgraph-style-no-semantic-pruning-")?;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::LsmGraphStyle);
    let engine = Engine::create(config).await?;

    let src = encoded(VertexLabel::Person, 1);

    engine
        .insert_edge(
            src,
            encoded(VertexLabel::Person, 100),
            EdgeLabel::Knows.as_i32(),
        )
        .await?;
    engine.flush_active().await?;

    let guard = engine.version_guard();
    let l0 = &guard.version().levels[0];

    assert!(
        !l0.is_empty(),
        "LsmGraphStyle should produce at least one L0 segment"
    );

    let snapshot = engine.current_snapshot();
    let neighbors = engine
        .get_neighbors_typed(src, EdgeLabel::Knows.as_i32(), snapshot)
        .await?;
    assert_eq!(neighbors.len(), 1, "basic neighbor lookup must work");

    engine.metrics().reset();
    let signature = GraphAccessSignature::neighbor_scan(src, Some(EdgeLabel::Knows.as_i32()));
    let by_sig = engine
        .get_neighbors_by_signature(signature, snapshot)
        .await?;
    assert_eq!(
        by_sig.len(),
        1,
        "signature-based lookup must return same results as typed lookup"
    );

    let label_guard = engine.version_guard();
    let label_l0 = &label_guard.version().levels[0];
    for meta in label_l0 {
        assert!(
            !meta.summary_completeness.allows_semantic_pruning(),
            "LsmGraphStyle segments should NOT allow semantic pruning — conservative reads only"
        );
    }

    Ok(())
}

#[tokio::test]
async fn layout_single_dim_correctness_vs_schema() -> anyhow::Result<()> {
    let schema_tmp = target_tempdir("correctness-schema-")?;
    let label_tmp = target_tempdir("correctness-label-only-")?;
    let etype_tmp = target_tempdir("correctness-etype-only-")?;
    let degree_tmp = target_tempdir("correctness-degree-only-")?;

    let schema_config = LsmGraphConfig::new(schema_tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::Schema);
    let label_config = LsmGraphConfig::new(label_tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::LabelOnly);
    let etype_config = LsmGraphConfig::new(etype_tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::EdgeTypeOnly);
    let degree_config = LsmGraphConfig::new(degree_tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::DegreeOnly);

    let schema_engine = Engine::create(schema_config).await?;
    let label_engine = Engine::create(label_config).await?;
    let etype_engine = Engine::create(etype_config).await?;
    let degree_engine = Engine::create(degree_config).await?;

    let src_a = encoded(VertexLabel::Person, 1);
    let src_b = encoded(VertexLabel::Forum, 2);
    let dst_a = encoded(VertexLabel::Person, 100);
    let dst_b = encoded(VertexLabel::Tag, 200);
    let edge_knows = EdgeLabel::Knows.as_i32();
    let edge_has_tag = EdgeLabel::HasTag.as_i32();

    let edges = vec![
        (src_a, dst_a, edge_knows),
        (src_a, dst_b, edge_has_tag),
        (src_b, dst_a, edge_has_tag),
    ];
    for (src, dst, etype) in &edges {
        schema_engine.insert_edge(*src, *dst, *etype).await?;
        label_engine.insert_edge(*src, *dst, *etype).await?;
        etype_engine.insert_edge(*src, *dst, *etype).await?;
        degree_engine.insert_edge(*src, *dst, *etype).await?;
    }

    schema_engine.flush_active().await?;
    label_engine.flush_active().await?;
    etype_engine.flush_active().await?;
    degree_engine.flush_active().await?;

    let test_queries = vec![
        GraphAccessSignature::neighbor_scan(src_a, Some(edge_knows)),
        GraphAccessSignature::neighbor_scan(src_a, Some(edge_has_tag)),
        GraphAccessSignature::neighbor_scan(src_a, None),
        GraphAccessSignature::neighbor_scan(src_b, Some(edge_has_tag)),
        GraphAccessSignature::neighbor_scan(src_b, None),
    ];

    for signature in test_queries {
        let schema_result = schema_engine
            .get_neighbors_by_signature(signature, schema_engine.current_snapshot())
            .await?;

        let label_result = label_engine
            .get_neighbors_by_signature(signature, label_engine.current_snapshot())
            .await?;

        let etype_result = etype_engine
            .get_neighbors_by_signature(signature, etype_engine.current_snapshot())
            .await?;

        let degree_result = degree_engine
            .get_neighbors_by_signature(signature, degree_engine.current_snapshot())
            .await?;

        let mut schema_dsts: Vec<_> = schema_result.iter().map(|e| e.dst).collect();
        schema_dsts.sort();
        let mut label_dsts: Vec<_> = label_result.iter().map(|e| e.dst).collect();
        label_dsts.sort();
        let mut etype_dsts: Vec<_> = etype_result.iter().map(|e| e.dst).collect();
        etype_dsts.sort();
        let mut degree_dsts: Vec<_> = degree_result.iter().map(|e| e.dst).collect();
        degree_dsts.sort();

        assert_eq!(
            schema_dsts, label_dsts,
            "label-only must return correct results matching schema layout"
        );
        assert_eq!(
            schema_dsts, etype_dsts,
            "edge-type-only must return correct results matching schema layout"
        );
        assert_eq!(
            schema_dsts, degree_dsts,
            "degree-only must return correct results matching schema layout"
        );

        assert!(
            label_dsts.len() <= 3,
            "single-dim layouts may read extra data but must not return fewer results than schema"
        );
    }

    Ok(())
}
