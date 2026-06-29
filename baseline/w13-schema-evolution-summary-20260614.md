# W13 Schema Evolution Summary

run_id=w13-schema-evolution-20260614-0004
log_root=/data/WorkSpace/lsmgraph-rs/remote-logs/w13-schema-evolution-20260614-0004

## Tests

test	status	log_dir
schema_epoch_snapshot_mixed_delta_survives_compaction_and_reopen	pass	/data/WorkSpace/lsmgraph-rs/remote-logs/w13-schema-evolution-20260614-0004/schema_epoch_snapshot_mixed_delta_survives_compaction_and_reopen
property_schema_snapshot_mixed_delta_survives_compaction_and_reopen	pass	/data/WorkSpace/lsmgraph-rs/remote-logs/w13-schema-evolution-20260614-0004/property_schema_snapshot_mixed_delta_survives_compaction_and_reopen
schema_epoch_change_keeps_old_segments_readable	pass	/data/WorkSpace/lsmgraph-rs/remote-logs/w13-schema-evolution-20260614-0004/schema_epoch_change_keeps_old_segments_readable
schema_catalog_persists_changes_and_drives_new_segment_epoch	pass	/data/WorkSpace/lsmgraph-rs/remote-logs/w13-schema-evolution-20260614-0004/schema_catalog_persists_changes_and_drives_new_segment_epoch
schema_evolution_report_summarizes_epoch_and_pruning_boundaries	pass	/data/WorkSpace/lsmgraph-rs/remote-logs/w13-schema-evolution-20260614-0004/schema_evolution_report_summarizes_epoch_and_pruning_boundaries
property_encoding_change_advances_catalog_and_segment_epochs	pass	/data/WorkSpace/lsmgraph-rs/remote-logs/w13-schema-evolution-20260614-0004/property_encoding_change_advances_catalog_and_segment_epochs
public_property_value_query_resolves_edge_label_alias	pass	/data/WorkSpace/lsmgraph-rs/remote-logs/w13-schema-evolution-20260614-0004/public_property_value_query_resolves_edge_label_alias
public_property_value_query_uses_row_encoding_epoch_after_type_change	pass	/data/WorkSpace/lsmgraph-rs/remote-logs/w13-schema-evolution-20260614-0004/public_property_value_query_uses_row_encoding_epoch_after_type_change
drop_property_hides_current_value_query_but_keeps_topology_readable	pass	/data/WorkSpace/lsmgraph-rs/remote-logs/w13-schema-evolution-20260614-0004/drop_property_hides_current_value_query_but_keeps_topology_readable
new_edge_label_prunes_exact_segments_but_reads_mixed_segments	pass	/data/WorkSpace/lsmgraph-rs/remote-logs/w13-schema-evolution-20260614-0004/new_edge_label_prunes_exact_segments_but_reads_mixed_segments

## Claim Mapping

- schema epochs advance on logical schema changes: covered by schema catalog/report tests.
- old segments remain readable under stored schema_epoch: covered by mixed epoch and old-segment readability tests.
- alias/drop behavior is explicit: covered by public edge-label alias and drop-property tests.
- property encoding epochs remain decodable inside the implemented fixed-width boundary: covered by property encoding tests.
- exact pruning remains conservative under new edge labels and mixed metadata: covered by new-edge-label pruning test.
