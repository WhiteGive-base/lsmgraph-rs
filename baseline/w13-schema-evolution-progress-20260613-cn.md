# W13 schema-evolution progress

## Session status

Status: DONE.

Reason: W13 was safe to run concurrently because it is a correctness/test runner rather than an SF30/SF100 store-building experiment. W7/W8/W9 formal runs remain blocked while W6 holds SF100 stores.

## Goal

Close the schema-evolution reviewer question for SemL0:

- schema epoch advances on logical schema changes
- old segments remain readable under their stored `schema_epoch`
- stable edge-type id / alias/drop behavior is explicit
- property encoding epoch changes remain decodable inside the implemented boundary
- exact pruning stays safe; uncertain/mixed/legacy metadata falls back to conservative reads
- output can be cited in W10 as measured/tested evidence, not just prose

## Candidate evidence already present

Code tests identified in `tests/engine_tests.rs`:

- `schema_epoch_snapshot_mixed_delta_survives_compaction_and_reopen`
- `property_schema_snapshot_mixed_delta_survives_compaction_and_reopen`
- `schema_epoch_change_keeps_old_segments_readable`
- `schema_catalog_persists_changes_and_drives_new_segment_epoch`
- `schema_evolution_report_summarizes_epoch_and_pruning_boundaries`
- `property_encoding_change_advances_catalog_and_segment_epochs`
- `public_property_value_query_resolves_edge_label_alias`
- `public_property_value_query_uses_row_encoding_epoch_after_type_change`
- `drop_property_hides_current_value_query_but_keeps_topology_readable`
- `new_edge_label_prunes_exact_segments_but_reads_mixed_segments`

Existing related driver:

`scripts/experiments/active/run-e4-e5-linux-driver.sh`

This driver runs E4 feedback tests and E5 schema/delta stress tests together. For W13, either create a W13-only wrapper around the E5/schema-evolution subset or run the existing driver and cite only the E5 outputs.

Dedicated W13 runner:

`baseline/run_w13_schema_evolution_20260613.sh`

Existing paper-side artifacts:

- `paper/schema-evolution-section-draft-md.md`
- `paper/schema-evolution-reviewer-brief.md`
- `paper/schema-change-decision-matrix.md`
- `paper/tables/table10-schema-evolution-claim-gap-map.tex`
- `remote-logs/p6-20-schema-report-smoke/schema-evolution-report-sf1-schema.json`

## Estimated time

- Runner/wrapper creation: completed; Linux `bash -n` passed.
- Test execution: likely minutes to low hours, depending on cargo rebuild state.
- Summary/report material: 1-3 hours.

## Start preconditions

- W6 is not in a fragile disk phase, or W6 has completed.
- `free -h` shows MemAvailable above 80GiB.
- `df -h /data/WorkSpace /tmp` passes.
- Use the W13-only schema-evolution runner unless it fails syntax or test-discovery checks.
- Result directory must write `DONE` on success or `FAILED` on failure.

## Planned command

Preferred command after W6 is stable/done:

```bash
RUN_ID=w13-schema-evolution-<date> bash baseline/run_w13_schema_evolution_20260613.sh
```

Fallback command using existing driver:

```bash
TAG=w13-schema-evolution-<date> bash scripts/experiments/active/run-e4-e5-linux-driver.sh
```

## Stop conditions

- Any schema-evolution test fails.
- Any test filter matches zero tests.
- Cargo test cannot produce a stable log.
- Runner exits without DONE/FAILED.
- Report cannot map test evidence to the schema-evolution claims listed above.

## Live status

- 2026-06-13 21:47: Created W13 progress document while W6 was running. No W13 test or runner was started. Main gap: no dedicated W13 runner yet; candidate E5 tests and existing E4/E5 driver identified.
- 2026-06-13 21:55: Added dedicated W13-only runner `baseline/run_w13_schema_evolution_20260613.sh`. It runs selected schema-evolution engine tests, writes `tests.tsv` and `summary.md`, and marks `DONE` or `FAILED`. Runner has not been executed while W6 is in a fragile disk phase.
- 2026-06-13 21:56: Linux `bash -n baseline/run_w13_schema_evolution_20260613.sh` passed.
- 2026-06-14 00:04: Concurrent-start assessment completed. Resource snapshot: `/data` 245G free, `/` 2.0G free, MemAvailable 440GiB. Decision: do not start W7/W8/W9 formal runners because they either create SF30 stores or need missing SF30 stores. Start W13 only, with `nice`, `TMPDIR` under the repo, and `MIN_FREE_GIB=220`. Estimated time: minutes to low hours depending on cargo rebuild state. Stop immediately on test failure, zero-test filter, missing DONE/FAILED marker, MemAvailable below 80GiB, or `/data` below 220GiB before a test.
- 2026-06-14 00:04: W13 runner completed successfully. Run id: `w13-schema-evolution-20260614-0004`. Marker: `remote-logs/w13-schema-evolution-20260614-0004/DONE`. All 10 selected schema-evolution tests passed and `tests.tsv` plus `summary.md` were generated.
- 2026-06-15 14:36: Supplemental sanity rerun completed after a help-check accidentally invoked the runner. Run id: `w13-schema-evolution-20260615-143649`. Marker: `remote-logs/w13-schema-evolution-20260615-143649/DONE`; `FAILED` absent. All 10 selected schema-evolution tests passed and `summary.md` was generated. Treat this as supplemental verification; primary W13 evidence can remain `w13-schema-evolution-20260614-0004` unless W10 consistently switches to the newer run.

## Completed

- W13 evidence candidates identified.
- Existing E4/E5 driver identified.
- Start preconditions and stop conditions recorded.
- Dedicated W13-only runner created.
- W13 runner syntax checked on Linux.
- W13 test execution completed with DONE marker.
- Schema-evolution evidence summary generated at `remote-logs/w13-schema-evolution-20260614-0004/summary.md`.
- Supplemental W13 sanity rerun completed at `remote-logs/w13-schema-evolution-20260615-143649/summary.md`.

## Not yet completed

- Paper-facing claim map update.

## Next minimal action

In W10, cite `remote-logs/w13-schema-evolution-20260614-0004/summary.md` for schema-evolution correctness claims, and update the paper-facing claim map.
