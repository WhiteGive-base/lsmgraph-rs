# W7 workload-shift progress

## Session goal

Run only the W7 low-risk smoke concurrently with W6, not the formal SF30 workload-shift run.

Estimated time:
- Smoke: 5-15 minutes, including any cargo build wait.
- Formal SF30: not started in this concurrent session.

Resource decision:
- Current machine has 503GiB memory and about 446GiB MemAvailable.
- `/data` is the bottleneck: W6 is expected to peak near `schema + naive + current variant`, so no SF30 task is allowed while W6 is running.
- W7 smoke uses a synthetic SF1-class workload and writes to a target smoke store, so it is safe to run with W6.

Command:
`RUN_ID=w7-workload-shift-smoke-20260613-1653 TIMEOUT_SECONDS=900 PHASE_FLUSHES=4 QUERIES_PER_FLUSH=3 bash baseline/run_w7_workload_shift_20260612.sh`

Stop conditions:
- `/data` free space below 200GiB.
- MemAvailable below 80GiB.
- Smoke timeout or JSON output missing/invalid.
- W6 reports FAILED, compare mismatch, or resource stop condition.

## Live status

- 2026-06-13 16:52: Prepared concurrent W7 smoke plan. Formal SF30 W7 is explicitly not started.
- 2026-06-13 16:53: Launched W7 smoke in background. Run id `w7-workload-shift-smoke-20260613-1653`, PID 3992041, log dir `remote-logs/w7-workload-shift-smoke-20260613-1653/`.
- 2026-06-13 16:57: W7 smoke completed. `remote-logs/w7-workload-shift-smoke-20260613-1653/DONE` exists and `w3-workload-shift-smoke.json` passed `python3 -m json.tool` validation. Output size: 473,600 bytes.
- 2026-06-13 21:50: Rechecked the remote W7 runner. `baseline/run_w7_workload_shift_20260612.sh` explicitly defaults to SF1-class synthetic smoke and does not expose a clear SF30 formal mode. Do not count this as Tier 2 W7 formal evidence until a formal workload-shift runner/command is defined and run.
- 2026-06-15 00:20: Pulled the remote W7 smoke runner back to the local workspace for alignment. Rechecked the available feedback benchmark evidence: it supports controlled microbenchmark claims, but still does not define the requested W7 formal SF30 three-way run (`feedback-only / static-budgeted / no-feedback`). Keep W7 Tier 2 unchecked.
- 2026-06-15 15:03: Re-audited W7 after W10 paper-safety pass. Checked `baseline/run_w7_workload_shift_20260612.sh`, `src/bin/w3_workload_shift.rs`, and `src/bin/p3_feedback_bench.rs`. The shell runner states it defaults to SF1-class synthetic smoke; `w3_workload_shift.rs` writes `"scale": "synthetic-sf1-smoke"` and generates synthetic sources/edge types internally; `p3_feedback_bench.rs` is a deterministic microbench with synthetic `segments_per_phase`/`repeats` inputs. No hidden LDBC/SF30 store input or formal three-way SF30 mode was found. No experiment was started.
- 2026-06-15 15:31: Implemented a new W7 real-SF30-derived formal runner path: `src/bin/w7_sf30_workload_shift.rs`, `baseline/run_w7_sf30_workload_shift_20260615.sh`, and `baseline/summarize_w7_sf30_workload_shift_20260615.py`. `bash -n`, `python3 -m py_compile`, and `cargo build --release --bin w7-sf30-workload-shift` passed on Linux. Smoke goal: validate real SF30 CSV ingestion, three variants, feedback compaction telemetry, JSON output, and summary generation. Smoke command: `RUN_ID=w7-sf30-workload-shift-smoke-20260615-1531 MODE=smoke TIMEOUT_SECONDS=900 bash baseline/run_w7_sf30_workload_shift_20260615.sh`. Estimate: 5-15 minutes including no rebuild or minor rebuild. Stop conditions: `/data` <200GiB, MemAvailable <80GiB, timeout, JSON invalid, runner error, missing summary.
- 2026-06-15 15:33: W7 real-SF30-derived smoke completed. Run id `w7-sf30-workload-shift-smoke-20260615-1531`; `DONE` exists, `FAILED` absent, JSON valid, summary generated at `remote-logs/w7-sf30-workload-shift-smoke-20260615-1531/summary.md`. Smoke Gate = GO: feedback-only selected a hot semantic partition in phase A and phase B, and selected hot range changed after workload shift. This is a smoke validation, not the final formal evidence.
- 2026-06-15 15:36: Starting W7 real-SF30-derived formal run. Command: `RUN_ID=w7-sf30-workload-shift-formal-20260615-1536 MODE=formal RUN_FORMAL=1 bash baseline/run_w7_sf30_workload_shift_20260615.sh`. Formal defaults: phase_flushes=8, hot_sources=64, edges_per_source_per_flush=1, queries_per_source=8, max_scan_rows_per_phase=2,000,000. Estimate: 30-120 minutes; expected store size is small because the runner builds a derived workload store from SF30 CSV rather than copying W8 113G stores. Stop conditions: `/data` <200GiB, MemAvailable <80GiB, timeout, JSON invalid, no feedback-only hot compaction for phase A/B, missing phase-B migration, or summary Gate=FALLBACK.
- 2026-06-15 15:38: W7 real-SF30-derived formal run completed. Run id `w7-sf30-workload-shift-formal-20260615-1536`; `DONE` exists, `FAILED` absent, JSON valid, summary generated at `remote-logs/w7-sf30-workload-shift-formal-20260615-1536/summary.md` and copied to `baseline/w7-sf30-workload-shift-summary-20260615-cn.md`. Formal Gate = GO. Feedback-only selected a hot semantic partition in phase A and phase B, and the selected hot range changed after workload shift. Parameters: phase_flushes=8, hot_sources=64, edges_per_source_per_flush=1, queries_per_source=8. Resource after run: `/data` 307G free, MemAvailable 447GiB, formal derived store size 552K.

## Current state

Status: DONE with caveat. Real-SF30-derived formal W7 completed and Gate=GO; this is derived from SF30 CSV, not a full 113G-store production trace.

Completed:
- Resource review.
- Runner review.
- W7 smoke launch.
- W7 smoke completion.
- W7 smoke JSON validation.
- W7 formal feasibility audit after W10: current runner/bin cannot be directly upgraded to SF30 formal by environment variables alone.
- New W7 real-SF30-derived runner/script/summary implemented.
- Linux build checks passed for `w7-sf30-workload-shift`.
- W7 real-SF30-derived smoke completed with Gate GO.
- W7 real-SF30-derived formal run completed with Gate GO.
- W7 formal summary copied to `baseline/w7-sf30-workload-shift-summary-20260615-cn.md`.

Not yet completed:
- Full-store SF30 self-tuning production trace. This is intentionally not pursued unless the paper needs a stronger-than-derived W7 claim.

Next minimal action:
- Treat W7 as ready-for-paper with the caveat that it is a real-SF30-derived workload-shift formal run, not a full-store production trace. Update W10/current progress and stop expanding experiments.
