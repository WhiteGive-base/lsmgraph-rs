# W9 steady-state progress

## Session goal

Run only the W9 low-risk smoke concurrently with W6, not the formal SF30 steady-state run.

Estimated time:
- Private SF1 store copy: about 1-3 minutes.
- Smoke: 3-10 minutes after build/open overhead.
- Formal SF30: not started in this concurrent session.

Resource decision:
- Current machine has 503GiB memory and about 446GiB MemAvailable.
- `/data` is the bottleneck: W6 is expected to peak near `schema + naive + current variant`, so no SF30 task is allowed while W6 is running.
- W9 smoke normally points at `store/sf1-base-graph`; to avoid mutating the base graph, this session uses a private copy under `store/w9-steady-state-smoke-20260613-1653/schema`.

Command:
`RUN_ID=w9-steady-state-smoke-20260613-1653 MODE=smoke STORE=/data/WorkSpace/lsmgraph-rs/store/w9-steady-state-smoke-20260613-1653/schema DURATION=180 CHECKPOINT_SECS=30 bash baseline/run_w9_steady_state_20260612.sh`

Stop conditions:
- `/data` free space below 200GiB.
- MemAvailable below 80GiB.
- Smoke checkpoint has fewer than the configured minimum queries.
- Writer op exceeds abort threshold or runner reports writer errors.
- W6 reports FAILED, compare mismatch, or resource stop condition.

## Live status

- 2026-06-13 16:52: Prepared concurrent W9 smoke plan. Formal SF30 W9 is explicitly not started. W9 will use a private copy of `store/sf1-base-graph`.
- 2026-06-13 16:53: Copied `store/sf1-base-graph` to private store `store/w9-steady-state-smoke-20260613-1653/schema` and launched W9 smoke in background. Run id `w9-steady-state-smoke-20260613-1653`, PID 3992592, log dir `remote-logs/w9-steady-state-smoke-20260613-1653/`.
- 2026-06-13 16:57: W9 smoke is running and has produced checkpoint JSONL in `remote-logs/w9-steady-state-smoke-20260613-1653/schema.jsonl`. First observed checkpoint has queries > 1 and writer errors 0, so the smoke has passed the early liveness condition. DONE is not present yet.
- 2026-06-13 17:04: W9 smoke completed. `DONE` exists. Local JSON decoder parsed 8 JSON objects from `schema.jsonl`: `start`, 6 checkpoints, and final `done`. Last three checkpoints: at 120.0s queries=4914 p50=13.284us p99=2818.389us L0=19; at 150.0s queries=4912 p50=340.814us p99=3380.019us L0=24; at 180.0s queries=4922 p50=399.631us p99=4126.023us L0=28. Total writer_errors=0 and slow_ops=0.
- 2026-06-13 21:33: Checked formal SF30 store preconditions while W6 was running. Remote `store/` currently has `store/sf30-base-graph`, but not the W9 formal runner defaults `store/sf30-schema`, `store/sf30-budg-b64`, or `store/sf30-semantic`. Do not start `MODE=formal RUN_FORMAL=1` until the runner store strategy is fixed or those variant stores are created.
- 2026-06-15 11:57: Rechecked W9 code. `w5_steady_state_real_store` mutates the store through insert/delete/flush and optional compaction, so W9 cannot be treated as read-only. Patched `baseline/run_w9_steady_state_20260612.sh` to support explicit `SCHEMA_STORE`, `BUDG_B64_STORE`, and `SEMANTIC_STORE` in formal mode, plus `/data` and MemAvailable resource gates and store-existence checks. Linux `bash -n` passed.
- 2026-06-15 12:08: Launched W9 formal in background. Run id `w9-steady-state-formal-20260615-1200`, PID 661226, log dir `remote-logs/w9-steady-state-formal-20260615-1200/`.
- 2026-06-15 12:09: Initial health check passed. Build completed, resource gates passed, runner entered `schema` variant using W8 frozen schema store as mutable steady-state store. Resource check: `/data` 307G free, MemAvailable about 448GiB. First checkpoint is not written yet.
- 2026-06-15 12:18: First `schema` checkpoint passed at elapsed 300s. Observed queries=49150, query_rate=163.83/s, latency p50=28.94us, p99=1270.59us, mean candidate L0=48.61, l0_files=1124, compaction_rewrite_bytes_delta=0, io_write_bytes_delta=2269088, writer_errors=0, writer_slow_ops=0. Resource check remains safe: `/data` 307G free, MemAvailable about 448GiB. Explicit judgement: CONTINUE.
- 2026-06-15 12:22: Second `schema` checkpoint passed at elapsed 600s. Observed queries=49155, query_rate=163.85/s, latency p50=50.33us, p99=2689.06us, mean candidate L0=96.85, l0_files=1172, compaction_rewrite_bytes_delta=0, io_write_bytes_delta=2273488, writer_errors=0, writer_slow_ops=0. Resource check remains safe: `/data` 307G free, MemAvailable about 448GiB. Explicit judgement: CONTINUE.
- 2026-06-15 12:25: Third `schema` checkpoint passed at elapsed 900s. Observed queries=49065, query_rate=163.55/s, latency p50=396.14us, p99=4070.45us, mean candidate L0=145.13, l0_files=1220, compaction_rewrite_bytes_delta=0, io_write_bytes_delta=2268992, writer_errors=0, writer_slow_ops=0. Resource check remains safe: `/data` 307G free, MemAvailable about 448GiB. Explicit judgement: CONTINUE.
- 2026-06-15 12:29: Added `baseline/summarize_w9_steady_state_20260615.py` and generated a partial summary at `baseline/w9-steady-state-summary-20260615-cn.md` from raw JSONL. Fourth `schema` checkpoint passed at elapsed 1200s: queries=48917, query_rate=163.05/s, latency p50=447.92us, p99=5493.15us, mean candidate L0=193.41, l0_files=1269, compaction_rewrite_bytes_delta=0, io_write_bytes_delta about 2.21MiB, writer_errors=0, writer_slow_ops=0. Summary status remains `partial-running` because budg-b64 and semantic have not started yet. Explicit judgement: CONTINUE.
- 2026-06-15 12:39: `schema` variant completed and runner moved to `budg-b64`. `schema.jsonl` has 8 rows: start, 6 checkpoints, and final done event. Regenerated partial summary from raw JSONL. Schema aggregate: total queries=293502, mean query rate=163.06/s, mean candidate L0=169.26, final candidate L0=289.89, mean p50=409.20us, mean p99=4858.40us, max p99=8740.23us, final L0 files=1365, rewrite bytes=0, io write bytes=13.04MiB, flushes=289, writer max op=17416us, writer_errors=0, writer_slow_ops=0. Resource check remains safe: `/data` 307G free, MemAvailable about 448GiB. Explicit judgement: CONTINUE.
- 2026-06-15 12:46: First `budg-b64` checkpoint passed at elapsed 300s. Observed queries=49214, query_rate=164.04/s, latency p50=27.67us, p99=1275.69us, mean candidate L0=41.62, l0_files=1191, compaction_rewrite_bytes_delta=0, io write bytes about 2.16MiB, writer_errors=0, writer_slow_ops=0. Regenerated partial summary from raw JSONL; status remains `partial-running` until budg-b64 and semantic both finish. Explicit judgement: CONTINUE.
- 2026-06-15 12:51: Second `budg-b64` checkpoint passed at elapsed 600s. Observed queries=49138, query_rate=163.80/s, latency p50=48.64us, p99=2668.46us, mean candidate L0=89.85, l0_files=1239, compaction_rewrite_bytes_delta=0, writer_errors=0, writer_slow_ops=0. Resource check remains safe: `/data` 307G free, MemAvailable about 448GiB. Explicit judgement: CONTINUE.
- 2026-06-15 13:00: Fourth `budg-b64` checkpoint passed at elapsed 1200s. Observed queries=48896, query_rate=162.99/s, latency p50=455.66us, p99=5558.31us, mean candidate L0=186.38, l0_files=1336, compaction_rewrite_bytes_delta=0, writer_errors=0, writer_slow_ops=0. Third checkpoint at 900s also passed with writer_errors=0 and writer_slow_ops=0. Explicit judgement: CONTINUE.
- 2026-06-15 13:10: `budg-b64` variant completed and runner moved to `semantic`. `budg-b64.jsonl` has 8 rows: start, 6 checkpoints, and final done event. Regenerated partial summary from raw JSONL. Budg-b64 aggregate: total queries=293411, mean query rate=163.01/s, mean candidate L0=162.25, final candidate L0=282.85, mean p50=416.74us, mean p99=4801.19us, max p99=8255.77us, final L0 files=1432, rewrite bytes=0, io write bytes=13.04MiB, flushes=289, writer max op=45243us, writer_errors=0, writer_slow_ops=0. Resource check remains safe: `/data` 307G free, MemAvailable about 441GiB. Explicit judgement: CONTINUE.
- 2026-06-15 13:19: `semantic` process has been active for about 10m but has not written `semantic.jsonl` yet. Process is alive at about 100% CPU, RSS about 12.5GiB, `/data` 307G free, MemAvailable about 435GiB, and no `FAILED` marker exists. This is treated as store open/init before first checkpoint rather than a stop condition. Explicit judgement: CONTINUE, but keep monitoring first-checkpoint latency.
- 2026-06-15 13:40: `semantic` process has been active for about 31m but still has not written `semantic.jsonl`. Process remains alive at about 100% CPU, RSS about 12.5GiB, `/data` 307G free, MemAvailable about 435GiB, and no `FAILED` marker exists. This is now a risk point for "steady-state unable to produce checkpoint", but still not a resource or crash failure. Guard set: continue until first JSONL/checkpoint or about 60m from semantic launch; if still no JSONL then record W9 semantic as fallback/limitation rather than waiting indefinitely. Explicit judgement: CONTINUE with 60m guard.
- 2026-06-15 13:58: `semantic` JSONL became active before the 60m guard. It now has 3 rows: start plus two checkpoints. Latest checkpoint at elapsed 600s: queries=49259, query_rate=164.20/s, latency p50=80.79us, p99=557.76us, mean candidate L0=170.65, l0_files=2254, compaction_rewrite_bytes_delta=0, writer_errors=0, writer_slow_ops=0. Regenerated partial summary from raw JSONL. Explicit judgement: CONTINUE and clear the no-JSONL guard.
- 2026-06-15 14:05: Fourth `semantic` checkpoint passed at elapsed 1200s. Observed queries=49163, query_rate=163.88/s, latency p50=162.53us, p99=958.19us, mean candidate L0=363.55, l0_files=2446, compaction_rewrite_bytes_delta=0, writer_errors=0, writer_slow_ops=0. Third checkpoint at 900s also passed with writer_errors=0 and writer_slow_ops=0. Explicit judgement: CONTINUE.
- 2026-06-15 14:22: W9 formal completed. `DONE` exists, `FAILED` absent. `schema`, `budg-b64`, and `semantic` each have 8 JSONL rows: start, 6 checkpoints, and final done event. Regenerated `baseline/w9-steady-state-summary-20260615-cn.md` from raw JSONL. Summary status: `ready-for-paper with caveats`. Final deltas vs schema: `budg-b64` mean candidate L0 -4.1%, mean p50 +1.8%, mean p99 -1.2%, last L0 files +4.9%, io write bytes 0.0%; `semantic` mean candidate L0 +86.3%, mean p50 -63.9%, mean p99 -82.5%, last L0 files +93.4%, io write bytes -0.7%. Explicit conclusion: usable as mixed read/write workload-coverage evidence; only claim advantages where the delta table supports them.

## Formal SF30 plan

Status: DONE.

Formal store strategy:
- W8 raw evidence is frozen: all W8 wrapped JSON payloads validated, W8 `DONE` exists, and `baseline/w8-property-2hop-summary-20260615-cn.md` was regenerated from raw JSON.
- W9 steady-state is a mixed read/write workload and will mutate its input stores.
- To avoid a 300GiB+ copy/import under current disk pressure, W9 formal will reuse the already-imported W8 SF30 stores as mutable steady-state starting points:
  - `SCHEMA_STORE=/data/WorkSpace/lsmgraph-rs/store/w8-property-2hop-20260614-2025/schema`
  - `BUDG_B64_STORE=/data/WorkSpace/lsmgraph-rs/store/w8-property-2hop-20260614-2025/budg-b64`
  - `SEMANTIC_STORE=/data/WorkSpace/lsmgraph-rs/store/w8-property-2hop-20260614-2025/semantic`
- This is safe for W8 paper evidence because W8 raw JSON and summary are already frozen; however, the physical W8 stores should be treated as consumed/mutable after W9 starts.

Planned command:

```bash
RUN_ID=w9-steady-state-formal-20260615-1200 \
MODE=formal RUN_FORMAL=1 \
DURATION=1800 CHECKPOINT_SECS=300 \
SCHEMA_STORE=/data/WorkSpace/lsmgraph-rs/store/w8-property-2hop-20260614-2025/schema \
BUDG_B64_STORE=/data/WorkSpace/lsmgraph-rs/store/w8-property-2hop-20260614-2025/budg-b64 \
SEMANTIC_STORE=/data/WorkSpace/lsmgraph-rs/store/w8-property-2hop-20260614-2025/semantic \
bash baseline/run_w9_steady_state_20260612.sh
```

Expected time:
- Build/open overhead: 5-20 minutes.
- Formal run: 30 minutes per variant, three variants sequentially.
- Total expected wall clock: 1.5-2.5 hours.

Stop conditions:
- `/data` free space below 200GiB.
- MemAvailable below 80GiB.
- Any checkpoint has fewer than `ABORT_MIN_QUERIES`.
- Writer errors, slow writer operations above threshold, runner `ABORT`, or missing final `DONE`.

## Current state

Status: DONE. Conclusion: `ready-for-paper with caveats`.

Completed:
- Resource review.
- Runner review.
- Base store mutation risk identified and avoided by private-copy plan.
- Private SF1 store copy.
- W9 smoke launch.
- W9 smoke completion.
- W9 smoke JSON object validation.
- W9 summary script created and partial summary generated from raw JSONL.
- Formal `schema` variant completed with final `done` event and no writer errors/slow ops.
- Formal `budg-b64` variant completed with final `done` event and no writer errors/slow ops.
- Formal `semantic` variant completed with final `done` event and no writer errors/slow ops.
- Final W9 summary regenerated from raw JSONL.

Not yet completed:
- None inside W9.
- Downstream W10 must carry the W9 caveat: mixed read/write is covered, but semantic has higher candidate L0 and L0 files; only latency advantages are strong for semantic.

Next minimal action:
- Use `baseline/w9-steady-state-summary-20260615-cn.md` as the W9 table source in W10.
