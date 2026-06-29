# W8 property + 2-hop progress

## Session status

Status: DONE. Conclusion: `ready-for-paper with caveats`.

Reason: W8 SF30 formal completed for `schema`, `budg-b64`, and `semantic`. All property predicate and 2-hop wrapped JSON payloads passed validation, `DONE` exists, `FAILED` is absent, and the W8 summary was regenerated from raw JSON.

## Goal

Run the W8 property predicate + 2-hop evidence block at SF30 scale for:

- `schema`
- `budg-b64`
- `semantic`

Required outputs:

- property predicate modes: `required-property`, `presence`, `equality`, `absent-default`
- 2-hop typed expansion
- candidate/body/latency summaries for each variant
- JSON validation for every output file
- progress conclusion: `ready-for-paper / fallback-claim`

## Estimated time

- SF1 smoke, if added before formal: 10-30 minutes.
- SF30 formal: runner estimates 20-40 minutes import per variant and 10-20 minutes bench per variant.
- Expected full wall clock: several hours, depending on whether stores can be reused.

## Current runner

Runner on Linux:

`/data/WorkSpace/lsmgraph-rs/baseline/run_w8_property_2hop_20260612.sh`

The runner defaults:

- `INPUT=/data/WorkSpace/ldbc-sf30/social_network`
- `SAMPLES=5000`
- `WARMUP_RUNS=1`
- `REPEATS=3`
- `EDGE_TYPE=1`
- `PROPERTY_ID=5`
- `PROPERTY_VALUE_I64=42`
- `PROPERTY_DEFAULT_I64=42`
- `TWO_HOP_FANOUT=64`
- `ABORT_TIMEOUT_SECONDS=10800`

## Planned command

Formal run selected after W6 DONE:

```bash
RUN_ID=w8-property-2hop-20260614-2025 TMPDIR=/data/WorkSpace/lsmgraph-rs/tmp ABORT_TIMEOUT_SECONDS=10800 bash baseline/run_w8_property_2hop_20260612.sh
```

## Start preconditions

- W6 is either DONE, or current W6 non-anchor store has been deleted and `/data` free space is comfortably above 200GiB.
- No second SF100 task is running.
- `free -h` shows MemAvailable above 80GiB.
- `df -h /data/WorkSpace /tmp` passes; `/tmp` must not be used for large outputs.
- `target/release/lsmgraph` exists and is executable.
- Decide whether a short SF1 smoke is required before formal. The current runner is formal-oriented and does not expose a dedicated smoke mode.

## Stop conditions

- Any single variant exceeds `ABORT_TIMEOUT_SECONDS`.
- `/data` free space drops below 200GiB.
- MemAvailable drops below 80GiB.
- Any output JSON is missing, zero bytes, or fails `python3 -m json.tool`.
- Predicate correctness is inconsistent with brute-force/filter expectation.
- Runner exits without `DONE`; create or record `FAILED` before the next session relies on the directory.

## Live status

- 2026-06-13 21:42: Created progress document while W6 was running. W8 formal was explicitly not started. Current blocker is W6 disk headroom: W6 `kv-lsm` bench is running with `/data` around 245G free.
- 2026-06-14 20:24: W6 is DONE and final Gate 1/2 are recorded. Resource check before W8 launch: `/data` 379G free, `/` 1.8G free, MemAvailable 448GiB. Decision: start only W8 formal, not W7/W9. W8 ETA remains several hours: each variant import about 20-40 minutes and benches about 10-20 minutes. Stop conditions unchanged: `/data` <200GiB, MemAvailable <80GiB, any variant timeout, missing/invalid JSON, or correctness/predicate inconsistency.
- 2026-06-14 20:25: Launched W8 formal in background. Run id `w8-property-2hop-20260614-2025`, PID 358466, log dir `remote-logs/w8-property-2hop-20260614-2025/`, store root `store/w8-property-2hop-20260614-2025/`.
- 2026-06-14 20:27: Initial health check passed. Build completed in release mode and runner entered SF30 `schema` import. Resource check: `/data` 374G free, `/` 1.8G free, MemAvailable 448GiB. Current phase is `import-schema`.
- 2026-06-14 20:45: `schema` import still running after about 18 minutes. Store root is about 74G, all under `store/w8-property-2hop-20260614-2025/schema`; `/data` has 305G free and MemAvailable is about 426GiB. Explicit judgement: CONTINUE. Disk is dropping faster than the most optimistic estimate, so monitor carefully before the second and third variants; do not start W7/W9 in parallel.
- 2026-06-14 20:47: To keep W8 below the `/data` stop line for all three SF30 variants, deleted completed W6 current matrix store `store/w6-sf100-matrix-20260613-132325` after W6 final report and `DONE` were verified. `/data` increased from about 285G to 553G free. Base graph and old SF100 reference store were retained.
- 2026-06-14 21:26: `schema` import reached about 60 minutes and is still running, but edge import, flush, and adjacency cache write have completed. Import child process remains active, so the import JSON is not final yet. Store root is about 113G; `/data` has 535G free and MemAvailable is about 387GiB. Explicit judgement: CONTINUE.
- 2026-06-14 21:45: `schema` import completed at 21:30:38 and runner moved to `bench-schema-property-required-property`. `schema/import.json` is a wrapped runner output: it contains `[w8] start/done` lines plus one JSON object, so the whole file is not pure JSON. This is a runner-format caveat rather than an import failure; later W8 summarization must extract and validate JSON object lines from wrapped outputs. Current bench output `property-required-property.json` only contains its start line while the bench is still running. Resource check: `/data` 535G free, MemAvailable about 351GiB.
- 2026-06-14 21:50: `bench-schema-property-required-property` completed and the wrapped JSON payload passed validation by stripping the first and last runner lines. Runner moved to `bench-schema-property-presence`. Continue using wrapped-output extraction for W8 JSON validation.
- 2026-06-14 22:14: `bench-schema-property-presence` completed and its wrapped JSON payload passed validation. Runner moved to `bench-schema-property-equality`. Resource check: `/data` 535G free, MemAvailable about 412GiB.
- 2026-06-14 22:33: `bench-schema-property-equality` completed and its wrapped JSON payload passed validation. Runner moved to `bench-schema-property-absent-default`. Resource check: `/data` 535G free, MemAvailable about 412GiB.
- 2026-06-14 22:52: `bench-schema-property-absent-default` completed and its wrapped JSON payload passed validation. Runner moved to `bench-schema-2hop`. Resource check: `/data` 535G free, MemAvailable about 407GiB.
- 2026-06-14 23:09: `bench-schema-2hop` is still running after about 21 minutes. `schema/2hop-typed.json` currently only has the runner start line, so no JSON validation yet. Resource check: `/data` 535G free, MemAvailable about 448GiB. Explicit judgement: CONTINUE.
- 2026-06-14 23:18: `bench-schema-2hop` is still running after about 30 minutes. `schema/2hop-typed.json` still has only the runner start line, so no JSON validation yet. Resource check: `/data` 535G free, MemAvailable about 448GiB. Explicit judgement: CONTINUE.
- 2026-06-14 23:29: `bench-schema-2hop` is still running after about 41 minutes. The actual `lsmgraph storage-bench` child process is active at about 118% CPU, so this does not look like a dead wait. `schema/2hop-typed.json` still has only the runner start line. Resource check: `/data` 535G free, MemAvailable about 448GiB. Explicit judgement: CONTINUE.
- 2026-06-14 23:48: `bench-schema-2hop` is still running after about 60 minutes. The actual `lsmgraph storage-bench` child process is active at about 122% CPU. `schema/2hop-typed.json` still has only the runner start line. Resource check: `/data` 535G free, MemAvailable about 448GiB. Explicit judgement: CONTINUE until the configured 10800s timeout or another stop condition.
- 2026-06-15 00:18: `bench-schema-2hop` is still running after about 90 minutes. The actual `lsmgraph storage-bench` child process is active at about 125% CPU. `schema/2hop-typed.json` still has only the runner start line. Resource check: `/data` 535G free, MemAvailable about 448GiB. This is beyond the runner's rough bench ETA, but no hard stop condition has fired. Explicit judgement: CONTINUE until the configured 10800s timeout or another stop condition.
- 2026-06-15 00:38: `bench-schema-2hop` completed after about 1h50m. Runner moved to `import-budg-b64`.
- 2026-06-15 00:51: `schema/2hop-typed.json` wrapped JSON payload passed validation. Current phase is `import-budg-b64`; `budg-b64` import has been running about 13 minutes. Resource check: `/data` 468G free, MemAvailable about 435GiB. Store sizes: `schema` 113G, `budg-b64` 69G, W8 root 181G. Explicit judgement: CONTINUE.
- 2026-06-15 01:08: `import-budg-b64` is still running after about 30 minutes. `budg-b64/import.json` currently only has the runner start line, so JSON validation is not available yet. Resource check: W8 root about 201G, `/data` 446G free, MemAvailable about 393GiB. Explicit judgement: CONTINUE.
- 2026-06-15 01:28: `import-budg-b64` is still running after about 51 minutes. `budg-b64/import.json` still has only the runner start line. Resource check: W8 root about 207G, `/data` 441G free, MemAvailable about 380GiB. Explicit judgement: CONTINUE.
- 2026-06-15 01:38: `import-budg-b64` completed after about 60 minutes. Runner moved to `bench-budg-b64-property-required-property`.
- 2026-06-15 01:44: `budg-b64/import.json` wrapped JSON payload passed validation. Current bench output `property-required-property.json` only has the runner start line. Resource check: W8 root about 225G, `/data` 423G free, MemAvailable about 394GiB. Explicit judgement: CONTINUE.
- 2026-06-15 01:55: `bench-budg-b64-property-required-property` completed. Runner moved to `bench-budg-b64-property-presence`.
- 2026-06-15 02:05: `budg-b64/property-required-property.json` wrapped JSON payload passed validation. Current phase is `bench-budg-b64-property-presence`, running about 10 minutes. Resource check: `/data` 423G free, MemAvailable about 374GiB. Explicit judgement: CONTINUE.
- 2026-06-15 02:14: `bench-budg-b64-property-presence` completed. Runner moved to `bench-budg-b64-property-equality`.
- 2026-06-15 02:18: `budg-b64/property-presence.json` wrapped JSON payload passed validation. Current phase is `bench-budg-b64-property-equality`, running about 4 minutes. Resource check: `/data` 423G free, MemAvailable about 412GiB. Explicit judgement: CONTINUE.
- 2026-06-15 02:34: `bench-budg-b64-property-equality` completed. Runner moved to `bench-budg-b64-property-absent-default`.
- 2026-06-15 02:34: `budg-b64/property-equality.json` wrapped JSON payload passed validation. Current phase is `bench-budg-b64-property-absent-default`. Resource check: `/data` 423G free, MemAvailable about 437GiB. Explicit judgement: CONTINUE.
- 2026-06-15 02:53: `bench-budg-b64-property-absent-default` completed. Runner moved to `bench-budg-b64-2hop`.
- 2026-06-15 02:56: `budg-b64/property-absent-default.json` wrapped JSON payload passed validation. Current phase is `bench-budg-b64-2hop`, running about 4 minutes. The actual child process is active at about 100% CPU with about 42GiB RSS. Resource check: `/data` 423G free, MemAvailable about 407GiB. Explicit judgement: CONTINUE.
- 2026-06-15 03:17: `bench-budg-b64-2hop` is still running after about 25 minutes. `budg-b64/2hop-typed.json` still has only the runner start line. The actual child process is active at about 109% CPU. Resource check: `/data` 423G free, MemAvailable about 448GiB. Explicit judgement: CONTINUE.
- 2026-06-15 03:48: `bench-budg-b64-2hop` is still running after about 55 minutes. `budg-b64/2hop-typed.json` still has only the runner start line. The actual child process is active at about 121% CPU. Resource check: `/data` 423G free, MemAvailable about 448GiB. Explicit judgement: CONTINUE.
- 2026-06-15 04:20: `bench-budg-b64-2hop` completed after about 1h27m. Runner moved to `import-semantic`.
- 2026-06-15 04:24: `budg-b64/2hop-typed.json` wrapped JSON payload passed validation. Current phase is `import-semantic`, running about 4 minutes. Resource check: W8 root about 259G, `/data` 390G free, MemAvailable about 445GiB. Explicit judgement: CONTINUE.
- 2026-06-15 04:40: `import-semantic` is still running after about 20 minutes. `semantic/import.json` currently only has the runner start line, so JSON validation is not available yet. Resource check: W8 root about 303G, `/data` 345G free, MemAvailable about 415GiB. Explicit judgement: CONTINUE; do not start concurrent formal tasks.
- 2026-06-15 05:01: `import-semantic` is still running after about 41 minutes. `semantic/import.json` still has only the runner start line. Resource check: W8 root about 319G, `/data` 329G free, MemAvailable about 359GiB. Explicit judgement: CONTINUE; do not start concurrent formal tasks.
- 2026-06-15 05:22: `import-semantic` is still running after about 62 minutes. `semantic/import.json` still has only the runner start line. Resource check: W8 root about 337G, `/data` 311G free, MemAvailable about 365GiB. Explicit judgement: CONTINUE; do not start concurrent formal tasks.
- 2026-06-15 05:36: `import-semantic` completed after about 1h17m. Runner moved to `bench-semantic-property-required-property`.
- 2026-06-15 05:37: `semantic/import.json` wrapped JSON payload passed validation. Current phase is `bench-semantic-property-required-property`. Resource check: W8 root about 341G, `/data` 307G free, MemAvailable about 437GiB. Explicit judgement: CONTINUE.
- 2026-06-15 06:09: `bench-semantic-property-required-property` is still running after about 32 minutes. `semantic/property-required-property.json` still has only the runner start line. The actual child process is active at about 100% CPU with about 13GiB RSS. Resource check: `/data` 307G free, MemAvailable about 435GiB. Explicit judgement: CONTINUE.
- 2026-06-15 06:29: `bench-semantic-property-required-property` is still running after about 53 minutes. `semantic/property-required-property.json` still has only the runner start line. The actual child process is active at about 100% CPU with about 127GiB RSS. Resource check: `/data` 307G free, MemAvailable about 320GiB. Explicit judgement: CONTINUE.
- 2026-06-15 06:30: `bench-semantic-property-required-property` completed after about 54 minutes. Runner moved to `bench-semantic-property-presence`.
- 2026-06-15 07:00: `semantic/property-required-property.json` wrapped JSON payload passed validation. Current phase is `bench-semantic-property-presence`, running about 30 minutes. The actual child process is active at about 100% CPU with about 13GiB RSS. Resource check: `/data` 307G free, MemAvailable about 435GiB. Explicit judgement: CONTINUE.
- 2026-06-15 07:24: `bench-semantic-property-presence` completed after about 54 minutes. Runner moved to `bench-semantic-property-equality`.
- 2026-06-15 07:32: `semantic/property-presence.json` wrapped JSON payload passed validation. Current phase is `bench-semantic-property-equality`, running about 7 minutes. The actual child process is active at about 100% CPU with about 13GiB RSS. Resource check: `/data` 307G free, MemAvailable about 435GiB. Explicit judgement: CONTINUE.
- 2026-06-15 08:03: `bench-semantic-property-equality` is still running after about 38 minutes. `semantic/property-equality.json` still has only the runner start line. The actual child process is active at about 100% CPU with about 53GiB RSS. Resource check: `/data` 307G free, MemAvailable about 394GiB. Explicit judgement: CONTINUE.
- 2026-06-15 08:18: `bench-semantic-property-equality` completed after about 54 minutes. Runner moved to `bench-semantic-property-absent-default`.
- 2026-06-15 08:24: `semantic/property-equality.json` wrapped JSON payload passed validation. Current phase is `bench-semantic-property-absent-default`, running about 6 minutes. The actual child process is active at about 100% CPU with about 13GiB RSS. Resource check: `/data` 307G free, MemAvailable about 435GiB. Explicit judgement: CONTINUE.
- 2026-06-15 08:55: `bench-semantic-property-absent-default` is still running after about 37 minutes. `semantic/property-absent-default.json` still has only the runner start line. The actual child process is active at about 100% CPU with about 45GiB RSS. Resource check: `/data` 307G free, MemAvailable about 407GiB. Explicit judgement: CONTINUE.
- 2026-06-15 09:13: `bench-semantic-property-absent-default` completed after about 54 minutes. Runner moved to `bench-semantic-2hop`.
- 2026-06-15 09:17: `semantic/property-absent-default.json` wrapped JSON payload passed validation. Current phase is `bench-semantic-2hop`, running about 5 minutes. The actual child process is active at about 100% CPU with about 13GiB RSS. Resource check: `/data` 307G free, MemAvailable about 435GiB. Explicit judgement: CONTINUE.
- 2026-06-15 09:49: `bench-semantic-2hop` is still running after about 36 minutes. `semantic/2hop-typed.json` still has only the runner start line. The actual child process is active at about 100% CPU with about 31GiB RSS. Resource check: `/data` 307G free, MemAvailable about 417GiB. Explicit judgement: CONTINUE.
- 2026-06-15 10:31: `bench-semantic-2hop` is still running after about 78 minutes. `semantic/2hop-typed.json` still has only the runner start line. The actual child process is active at about 109% CPU with about 13GiB RSS. Resource check: `/data` 307G free, MemAvailable about 435GiB. Explicit judgement: CONTINUE.
- 2026-06-15 11:13: `bench-semantic-2hop` is still running after about 2h00m. `semantic/2hop-typed.json` still has only the runner start line. The actual child process is active at about 117% CPU with about 13GiB RSS. Resource check: `/data` 307G free, MemAvailable about 435GiB. Explicit judgement: CONTINUE until the configured 10800s timeout or another stop condition.
- 2026-06-15 11:16: `bench-semantic-2hop` completed after about 2h03m. Runner wrote `DONE`.
- 2026-06-15 11:45: `semantic/2hop-typed.json` wrapped JSON payload passed validation. `DONE` exists and `FAILED` is absent. W8 store root is about 341G. Resource check: `/data` 307G free, MemAvailable about 448GiB. Explicit judgement: RAW RUN DONE; next generate summary table and conclusion.
- 2026-06-15 11:49: Generated `baseline/w8-property-2hop-summary-20260615-cn.md` from raw JSON using `baseline/summarize_w8_property_2hop_20260615.py`. Conclusion: `ready-for-paper with caveats`. Property predicates support candidate/body/read/elapsed improvements; 2-hop supports body/read/elapsed improvements, but not universal candidate-L0 reduction.

## Completed

- W8 runner path identified.
- Formal start command and stop conditions recorded.
- `schema` import, four property benches, and two-hop bench completed.
- Wrapped JSON payload validation passed for all completed `schema` outputs.
- `budg-b64` import completed and wrapped JSON payload validation passed.
- `budg-b64` required-property bench completed and wrapped JSON payload validation passed.
- `budg-b64` presence bench completed and wrapped JSON payload validation passed.
- `budg-b64` equality bench completed and wrapped JSON payload validation passed.
- `budg-b64` absent-default bench completed and wrapped JSON payload validation passed.
- `budg-b64` two-hop bench completed and wrapped JSON payload validation passed.
- `semantic` import completed and wrapped JSON payload validation passed.
- `semantic` required-property bench completed and wrapped JSON payload validation passed.
- `semantic` presence bench completed and wrapped JSON payload validation passed.
- `semantic` equality bench completed and wrapped JSON payload validation passed.
- `semantic` absent-default bench completed and wrapped JSON payload validation passed.
- `semantic` two-hop bench completed and wrapped JSON payload validation passed.
- W8 runner `DONE` marker present; `FAILED` marker absent.
- W8 summary table generated from raw JSON.
- W8 conclusion written: `ready-for-paper with caveats`.

## Not yet completed

- None inside W8.
- Downstream W10 must carry the W8 caveat into paper claims: property predicates support candidate/body/read/elapsed improvements; 2-hop supports body/read/elapsed improvements, but not universal candidate-L0 reduction.

## Next minimal action

- No further W8 experiment action.
- Use `baseline/w8-property-2hop-summary-20260615-cn.md` as the W8 table source in W10.
- Treat the physical W8 stores as mutable/consumed after W9 formal starts, because W9 steady-state reuses them as starting stores.
