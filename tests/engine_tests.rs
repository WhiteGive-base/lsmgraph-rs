use lsmgraph::config::LsmGraphConfig;
use lsmgraph::graph::Engine;
use lsmgraph::types::EdgeLabel;

#[tokio::test]
async fn insert_flush_reopen_and_scan() -> anyhow::Result<()> {
    let tmp = tempfile::tempdir()?;
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
    let tmp = tempfile::tempdir()?;
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
async fn compact_l0_to_l1_preserves_visible_edges() -> anyhow::Result<()> {
    let tmp = tempfile::tempdir()?;
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
