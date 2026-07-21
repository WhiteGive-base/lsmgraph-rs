use lsmgraph::config::{L0LayoutPolicy, LsmGraphConfig, QueryControlStage};
use lsmgraph::csr::EdgePropertyValue;
use lsmgraph::graph::Engine;
use lsmgraph::types::{EdgeLabel, VertexId};
use lsmgraph::{
    DegreeClass, GraphAccessSignature, NewPropertyEntry, PropertyOwner, PropertyValue, VertexLabel,
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

struct StageObservation {
    result_dsts: Vec<VertexId>,
    routed_l0_segments: u64,
    candidate_l0_segments: u64,
    semantic_pruned_segments: u64,
    has_exact_degree_partition: bool,
    cpu_clock_failures: u64,
    cpu_phase_sum_ns: u64,
}

fn metric_u64(value: &Value, path: &[&str]) -> u64 {
    path.iter()
        .fold(value, |cursor, key| &cursor[*key])
        .as_u64()
        .unwrap_or_default()
}

fn canonical_stage_config(store: &std::path::Path, stage: QueryControlStage) -> LsmGraphConfig {
    let mut config = LsmGraphConfig::new(store)
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_query_control_stage(stage)
        .with_auto_compaction(stage == QueryControlStage::A6)
        .with_semantic_budget_edge_type_allowlist(vec![
            EdgeLabel::Knows.as_i32(),
            EdgeLabel::HasInterest.as_i32(),
        ])
        .with_semantic_budget_min_edge_type_bytes(1)
        .with_semantic_budget_min_exact_bytes(1)
        .with_semantic_budget_min_benefit_score(0.0);
    config.l0_file_threshold = 100;
    config
}

fn store_snapshot(root: &std::path::Path) -> anyhow::Result<Vec<(String, Vec<u8>)>> {
    fn visit(
        root: &std::path::Path,
        current: &std::path::Path,
        out: &mut Vec<(String, Vec<u8>)>,
    ) -> anyhow::Result<()> {
        for entry in std::fs::read_dir(current)? {
            let entry = entry?;
            let path = entry.path();
            if entry.file_type()?.is_dir() {
                visit(root, &path, out)?;
            } else if entry.file_type()?.is_file() {
                let relative = path
                    .strip_prefix(root)?
                    .to_string_lossy()
                    .replace('\\', "/");
                out.push((relative, std::fs::read(path)?));
            }
        }
        Ok(())
    }

    let mut out = Vec::new();
    visit(root, root, &mut out)?;
    out.sort_by(|left, right| left.0.cmp(&right.0));
    Ok(out)
}

async fn observe_stage(stage: QueryControlStage) -> anyhow::Result<StageObservation> {
    let tmp = target_tempdir(&format!("query-control-{stage:?}-"))?;
    let knows = EdgeLabel::Knows.as_i32();
    let has_interest = EdgeLabel::HasInterest.as_i32();
    // A0 and A1 deliberately share the same physical evidence store; A1's
    // only change is enabling exact-evidence admission.
    let layout = L0LayoutPolicy::SemanticBudgeted;
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(layout)
        .with_query_control_stage(stage)
        .with_query_cpu_phase_instrumentation(true)
        .with_semantic_budget_edge_type_allowlist(vec![knows, has_interest])
        .with_semantic_budget_min_edge_type_bytes(1)
        .with_semantic_budget_min_exact_bytes(1)
        .with_semantic_budget_min_benefit_score(0.0)
        .with_semantic_budget_disable_feedback(false);
    let engine = Engine::create(config).await?;
    let src = encoded(VertexLabel::Person, 7);
    let knows_dsts = [
        encoded(VertexLabel::Person, 101),
        encoded(VertexLabel::Person, 102),
    ];
    for dst in knows_dsts {
        engine.insert_edge(src, dst, knows).await?;
    }
    engine
        .insert_edge(src, encoded(VertexLabel::Tag, 201), has_interest)
        .await?;
    engine.flush_active().await?;

    let has_exact_degree_partition =
        engine.version_guard().version().levels[0]
            .iter()
            .any(|meta| {
                meta.edge_type_partition == knows
                    && meta.degree_class_exact
                    && !matches!(meta.degree_class, DegreeClass::Mixed | DegreeClass::Unknown)
            });

    engine.metrics().reset();
    let mut result_dsts = engine
        .get_neighbors_typed(src, knows, engine.current_snapshot())
        .await?
        .into_iter()
        .map(|edge| edge.dst)
        .collect::<Vec<_>>();
    result_dsts.sort_unstable();
    let metrics = engine.metrics().snapshot_json();
    let semantic_pruned_segments = metrics["csr"]["pruning_reasons"]
        .as_object()
        .map(|reasons| {
            reasons
                .values()
                .map(|reason| metric_u64(reason, &["pruned_segments"]))
                .sum()
        })
        .unwrap_or_default();
    let cpu_phase_sum_ns = [
        "query_setup",
        "metadata_admission",
        "routing_index",
        "body_decode_filter",
        "mvcc_result",
    ]
    .into_iter()
    .map(|phase| metric_u64(&metrics, &["storage", "query_cpu_ns", phase]))
    .sum();

    Ok(StageObservation {
        result_dsts,
        routed_l0_segments: metric_u64(&metrics, &["csr", "routed_l0_segments"]),
        candidate_l0_segments: metric_u64(&metrics, &["csr", "candidate_l0_segments"]),
        semantic_pruned_segments,
        has_exact_degree_partition,
        cpu_clock_failures: metric_u64(&metrics, &["storage", "query_cpu_ns", "clock_failures"]),
        cpu_phase_sum_ns,
    })
}

#[tokio::test]
async fn staircase_preserves_results_and_isolates_first_three_components() -> anyhow::Result<()> {
    let a0 = observe_stage(QueryControlStage::A0).await?;
    let a1 = observe_stage(QueryControlStage::A1).await?;
    let a2 = observe_stage(QueryControlStage::A2).await?;
    let a3 = observe_stage(QueryControlStage::A3).await?;

    assert_eq!(a0.result_dsts, a1.result_dsts);
    assert_eq!(a1.result_dsts, a2.result_dsts);
    assert_eq!(a2.result_dsts, a3.result_dsts);

    assert_eq!(a0.semantic_pruned_segments, 0);
    assert!(a1.semantic_pruned_segments > 0);
    assert!(a2.routed_l0_segments < a1.routed_l0_segments);
    assert!(a2.candidate_l0_segments <= a1.candidate_l0_segments);
    assert!(!a2.has_exact_degree_partition);
    assert!(a3.has_exact_degree_partition);

    for observation in [&a0, &a1, &a2, &a3] {
        assert_eq!(observation.cpu_clock_failures, 0);
        assert!(observation.cpu_phase_sum_ns > 0);
    }
    Ok(())
}

#[tokio::test]
async fn signature_builder_is_inside_query_setup_and_e2e_latency_boundaries() -> anyhow::Result<()>
{
    let tmp = target_tempdir("query-control-signature-builder-")?;
    let config = canonical_stage_config(tmp.path(), QueryControlStage::A3)
        .with_query_cpu_phase_instrumentation(true);
    let engine = Engine::create(config).await?;
    let src = encoded(VertexLabel::Person, 77);
    let edge_type = EdgeLabel::Knows.as_i32();
    engine.metrics().reset();

    let result = engine
        .get_neighbors_by_signature_builder(engine.current_snapshot(), || {
            let deadline = std::time::Instant::now() + std::time::Duration::from_millis(15);
            let mut work = 0_u64;
            while std::time::Instant::now() < deadline {
                work = std::hint::black_box(work.wrapping_add(1));
            }
            std::hint::black_box(work);
            GraphAccessSignature::neighbor_scan(src, Some(edge_type))
                .with_degree_class(DegreeClass::Low)
        })
        .await?;
    assert!(result.is_empty());

    let metrics = engine.metrics().snapshot_json();
    assert!(
        metric_u64(&metrics, &["storage", "query_cpu_ns", "query_setup"]) >= 1_000_000,
        "signature construction CPU must be charged to QuerySetup"
    );
    assert!(
        metric_u64(&metrics, &["storage", "get_neighbors_latency", "max_us"]) >= 5_000,
        "signature construction wall time must be inside Engine E2E latency"
    );
    Ok(())
}

#[tokio::test]
async fn a0_and_a1_materialize_byte_identical_evidence_stores() -> anyhow::Result<()> {
    let a0_tmp = target_tempdir("query-control-a0-store-")?;
    let a1_tmp = target_tempdir("query-control-a1-store-")?;
    for (stage, root) in [
        (QueryControlStage::A0, a0_tmp.path()),
        (QueryControlStage::A1, a1_tmp.path()),
    ] {
        let engine = Engine::create(canonical_stage_config(root, stage)).await?;
        let src = encoded(VertexLabel::Person, 70);
        for (edge_type, dst) in [
            (EdgeLabel::Knows.as_i32(), encoded(VertexLabel::Person, 71)),
            (EdgeLabel::Knows.as_i32(), encoded(VertexLabel::Person, 72)),
            (
                EdgeLabel::HasInterest.as_i32(),
                encoded(VertexLabel::Tag, 73),
            ),
        ] {
            engine.insert_edge(src, dst, edge_type).await?;
        }
        engine.flush_active().await?;
        drop(engine);
    }

    assert_eq!(
        store_snapshot(a0_tmp.path())?,
        store_snapshot(a1_tmp.path())?,
        "A0->A1 must change only read admission, not physical store bytes"
    );
    Ok(())
}

#[derive(Debug, PartialEq, Eq)]
struct CanonicalLogicalResult {
    typed_current: Vec<VertexId>,
    degree_current: Vec<VertexId>,
    property_current: Vec<VertexId>,
    value_current: Vec<VertexId>,
    typed_before_delete: Vec<VertexId>,
}

async fn canonical_logical_result(
    stage: QueryControlStage,
) -> anyhow::Result<CanonicalLogicalResult> {
    let tmp = target_tempdir(&format!("query-control-full-{stage:?}-"))?;
    let config = canonical_stage_config(tmp.path(), stage);
    let engine = Engine::create(config.clone()).await?;
    let src = encoded(VertexLabel::Person, 800);
    let keep_property = encoded(VertexLabel::Person, 801);
    let keep_absent = encoded(VertexLabel::Person, 802);
    let deleted_property = encoded(VertexLabel::Person, 803);
    let edge_type = EdgeLabel::Knows.as_i32();
    let property_id = 5;
    let property_epoch = engine.add_schema_property(NewPropertyEntry {
        id: property_id,
        owner: PropertyOwner::EdgeLabel(edge_type),
        name: "strength".to_string(),
        logical_type: "int64".to_string(),
        physical_encoding: "plain_i64".to_string(),
        encoding_version: 1,
        default_or_null_rule: "null".to_string(),
    })?;
    for dst in [keep_property, deleted_property] {
        engine
            .insert_edge_with_properties_prototype(
                src,
                dst,
                edge_type,
                vec![EdgePropertyValue::encoded(
                    property_id,
                    property_epoch,
                    42_i64.to_le_bytes(),
                )],
            )
            .await?;
    }
    engine.insert_edge(src, keep_absent, edge_type).await?;
    engine.flush_active().await?;
    let before_delete = engine.current_snapshot();
    engine.delete_edge(src, deleted_property, edge_type).await?;
    engine.flush_active().await?;

    match stage {
        QueryControlStage::A4 => {
            engine.compact_l0_to_l1().await?;
        }
        QueryControlStage::A5 => {
            engine.compact_l0_to_l1_partitioned().await?;
        }
        _ => {}
    }
    drop(engine);

    let reopened = Engine::open(config).await?;
    let current = reopened.current_snapshot();
    let sorted_dsts = |edges: Vec<lsmgraph::EdgeRecord>| {
        let mut dsts = edges.into_iter().map(|edge| edge.dst).collect::<Vec<_>>();
        dsts.sort_unstable();
        dsts
    };
    let typed_current = sorted_dsts(
        reopened
            .get_neighbors_typed(src, edge_type, current)
            .await?,
    );
    let degree_current = sorted_dsts(
        reopened
            .get_neighbors_by_signature(
                GraphAccessSignature::neighbor_scan(src, Some(edge_type))
                    .with_degree_class(DegreeClass::Low),
                current,
            )
            .await?,
    );
    let property_current = sorted_dsts(
        reopened
            .get_neighbors_with_present_property_prototype(
                src,
                Some(edge_type),
                current,
                property_id,
            )
            .await?,
    );
    let value_current = sorted_dsts(
        reopened
            .get_neighbors_matching_property_value(
                src,
                edge_type,
                current,
                property_id,
                PropertyValue::I64(42),
            )
            .await?,
    );
    let typed_before_delete = sorted_dsts(
        reopened
            .get_neighbors_typed(src, edge_type, before_delete)
            .await?,
    );
    Ok(CanonicalLogicalResult {
        typed_current,
        degree_current,
        property_current,
        value_current,
        typed_before_delete,
    })
}

#[tokio::test]
async fn all_stages_preserve_typed_degree_property_mvcc_and_reopen_results() -> anyhow::Result<()> {
    let mut baseline = None;
    for stage in [
        QueryControlStage::A0,
        QueryControlStage::A1,
        QueryControlStage::A2,
        QueryControlStage::A3,
        QueryControlStage::A4,
        QueryControlStage::A5,
        QueryControlStage::A6,
    ] {
        let observed = canonical_logical_result(stage).await?;
        if let Some(expected) = &baseline {
            assert_eq!(&observed, expected, "logical result drift at {stage:?}");
        } else {
            assert_eq!(observed.typed_current.len(), 2);
            assert_eq!(observed.degree_current, observed.typed_current);
            assert_eq!(observed.property_current.len(), 1);
            assert_eq!(observed.value_current, observed.property_current);
            assert_eq!(observed.typed_before_delete.len(), 3);
            baseline = Some(observed);
        }
    }
    Ok(())
}

#[tokio::test]
async fn a6_property_queries_trigger_maintenance_and_charge_query_latency() -> anyhow::Result<()> {
    for use_value_query in [false, true] {
        for (stage, expect_l1) in [
            (QueryControlStage::A5, false),
            (QueryControlStage::A6, true),
        ] {
            let tmp = target_tempdir(&format!(
                "query-control-property-maint-{stage:?}-{use_value_query}-"
            ))?;
            let mut config = canonical_stage_config(tmp.path(), stage);
            config.auto_compaction = true;
            config.l0_ra_min_queries = 1;
            config.l0_ra_min_l0_segments = 1;
            config.l0_ra_min_score = 0.0;
            let engine = Engine::create(config).await?;
            let src = encoded(VertexLabel::Person, 900);
            let dst = encoded(VertexLabel::Person, 901);
            let edge_type = EdgeLabel::Knows.as_i32();
            let property_id = 5;
            let property_epoch = engine.add_schema_property(NewPropertyEntry {
                id: property_id,
                owner: PropertyOwner::EdgeLabel(edge_type),
                name: "strength".to_string(),
                logical_type: "int64".to_string(),
                physical_encoding: "plain_i64".to_string(),
                encoding_version: 1,
                default_or_null_rule: "null".to_string(),
            })?;
            engine
                .insert_edge_with_properties_prototype(
                    src,
                    dst,
                    edge_type,
                    vec![EdgePropertyValue::encoded(
                        property_id,
                        property_epoch,
                        42_i64.to_le_bytes(),
                    )],
                )
                .await?;
            engine.flush_active().await?;
            assert_eq!(
                engine
                    .live_file_count_by_level()
                    .get(1)
                    .copied()
                    .unwrap_or_default(),
                0
            );
            engine.metrics().reset();
            let result = if use_value_query {
                engine
                    .get_neighbors_matching_property_value(
                        src,
                        edge_type,
                        engine.current_snapshot(),
                        property_id,
                        PropertyValue::I64(42),
                    )
                    .await?
            } else {
                engine
                    .get_neighbors_with_present_property_prototype(
                        src,
                        Some(edge_type),
                        engine.current_snapshot(),
                        property_id,
                    )
                    .await?
            };
            assert_eq!(result.len(), 1);
            let levels = engine.live_file_count_by_level();
            assert_eq!(
                levels.get(1).copied().unwrap_or_default() > 0,
                expect_l1,
                "property query lifecycle mismatch for {stage:?}"
            );
            let metrics = engine.metrics().snapshot_json();
            let query_sum = metric_u64(&metrics, &["storage", "get_neighbors_latency", "sum_us"]);
            let compaction_sum = metric_u64(&metrics, &["storage", "compaction_latency", "sum_us"]);
            if expect_l1 {
                assert!(compaction_sum > 0);
                assert!(
                    query_sum >= compaction_sum,
                    "client-visible query latency must include awaited maintenance"
                );
            } else {
                assert_eq!(compaction_sum, 0);
            }
        }
    }
    Ok(())
}

#[test]
fn lifecycle_stage_boundaries_are_explicit() {
    let mut config = LsmGraphConfig::new("unused").with_auto_compaction(true);
    config.query_control_stage = QueryControlStage::A4;
    assert!(config.supports_feedback_compaction());
    assert!(!config.automatic_maintenance_enabled());

    config.query_control_stage = QueryControlStage::A5;
    assert!(lsmgraph::LevelMergePolicy::from_config(&config).semantic_partition_outputs);
    assert!(!config.automatic_maintenance_enabled());

    config.query_control_stage = QueryControlStage::A6;
    assert!(config.automatic_maintenance_enabled());
}

async fn compact_stage(stage: QueryControlStage) -> anyhow::Result<(usize, Vec<Vec<VertexId>>)> {
    let tmp = target_tempdir(&format!("query-control-compaction-{stage:?}-"))?;
    let knows = EdgeLabel::Knows.as_i32();
    let has_interest = EdgeLabel::HasInterest.as_i32();
    let config = LsmGraphConfig::new(tmp.path())
        .with_memgraph_capacity(64 * 1024 * 1024)
        .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
        .with_query_control_stage(stage)
        .with_semantic_budget_edge_type_allowlist(vec![knows, has_interest])
        .with_semantic_budget_min_edge_type_bytes(1)
        .with_semantic_budget_min_exact_bytes(1)
        .with_semantic_budget_min_benefit_score(0.0);
    let engine = Engine::create(config).await?;
    let src = encoded(VertexLabel::Person, 300);
    for (edge_type, dst) in [
        (knows, encoded(VertexLabel::Person, 301)),
        (knows, encoded(VertexLabel::Person, 302)),
        (has_interest, encoded(VertexLabel::Tag, 401)),
    ] {
        engine.insert_edge(src, dst, edge_type).await?;
    }
    engine.flush_active().await?;

    let output_count = if stage.semantic_compaction_enabled() {
        engine.compact_l0_to_l1_partitioned().await?.len()
    } else {
        usize::from(engine.compact_l0_to_l1().await?.is_some())
    };
    let mut results = Vec::new();
    for edge_type in [knows, has_interest] {
        let mut dsts = engine
            .get_neighbors_typed(src, edge_type, engine.current_snapshot())
            .await?
            .into_iter()
            .map(|edge| edge.dst)
            .collect::<Vec<_>>();
        dsts.sort_unstable();
        results.push(dsts);
    }
    Ok((output_count, results))
}

#[tokio::test]
async fn a5_semantic_compaction_changes_outputs_without_changing_results() -> anyhow::Result<()> {
    let (a4_outputs, a4_results) = compact_stage(QueryControlStage::A4).await?;
    let (a5_outputs, a5_results) = compact_stage(QueryControlStage::A5).await?;
    assert_eq!(a4_results, a5_results);
    assert_eq!(a4_outputs, 1, "A4 retains the legacy global merge output");
    assert!(
        a5_outputs >= 2,
        "A5 must preserve at least the two edge-type partitions"
    );
    Ok(())
}

#[tokio::test]
async fn a6_is_the_only_stage_that_runs_automatic_maintenance() -> anyhow::Result<()> {
    for (stage, expect_l1) in [
        (QueryControlStage::A5, false),
        (QueryControlStage::A6, true),
    ] {
        let tmp = target_tempdir(&format!("query-control-auto-{stage:?}-"))?;
        let mut config = LsmGraphConfig::new(tmp.path())
            .with_memgraph_capacity(64 * 1024 * 1024)
            .with_l0_layout(L0LayoutPolicy::SemanticBudgeted)
            .with_query_control_stage(stage)
            .with_auto_compaction(true)
            .with_semantic_budget_edge_type_allowlist(vec![EdgeLabel::Knows.as_i32()]);
        config.l0_file_threshold = 1;
        let engine = Engine::create(config).await?;
        engine
            .insert_edge(
                encoded(VertexLabel::Person, 500),
                encoded(VertexLabel::Person, 501),
                EdgeLabel::Knows.as_i32(),
            )
            .await?;
        engine.flush_active().await?;
        let levels = engine.live_file_count_by_level();
        assert_eq!(
            levels.get(1).copied().unwrap_or_default() > 0,
            expect_l1,
            "unexpected automatic-maintenance state for {stage:?}"
        );
    }
    Ok(())
}
