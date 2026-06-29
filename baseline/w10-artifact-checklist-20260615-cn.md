# W10 artifact checklist

Generated: 2026-06-15 15:52 CST

This checklist is the current artifact/reproducibility map for the SemL0 submission freeze. It is scoped to the evidence that already exists; it does not propose W11/W12 or new experiments.

## Required Environment Notes

| Item | Value |
|---|---|
| Repo root | `/data/WorkSpace/lsmgraph-rs` |
| Large-data root | `/data/WorkSpace` |
| Current resource floor used in runners | `/data` free >= 200GiB; MemAvailable >= 80GiB |
| Current resource snapshot at W10 inventory | `/data` 307G free; MemAvailable 448GiB |
| Result convention | Every result directory used by paper must have `DONE` or `FAILED`; current cited W6/W7/W8/W9/W13 dirs have `DONE` and no `FAILED` |

## Reproducibility Checklist

| Evidence | Reproduce / regenerate command | Raw outputs | Validation marker | Paper use |
|---|---|---|---|---|
| W6 SF100 matrix | `W6_ALLOW_SF100=1 bash baseline/run_w6_sf100_matrix_20260613.sh` then `python3 baseline/summarize_w6_sf100_matrix_20260613.py --log-dir remote-logs/w6-sf100-matrix-20260613-132325 --out baseline/sf100-matrix-20260613-cn.md` | `remote-logs/w6-sf100-matrix-20260613-132325/*.json`, `manifest.tsv`, `resource-monitor.tsv` | `remote-logs/w6-sf100-matrix-20260613-132325/DONE`; compare mismatches=0 | Main SF100 table, correctness, RSS, Gate 1/2 |
| W7 workload shift | `RUN_ID=w7-sf30-workload-shift-formal-20260615-1536 MODE=formal RUN_FORMAL=1 bash baseline/run_w7_sf30_workload_shift_20260615.sh`, then `python3 baseline/summarize_w7_sf30_workload_shift_20260615.py --json remote-logs/w7-sf30-workload-shift-formal-20260615-1536/w7-sf30-workload-shift.json --out baseline/w7-sf30-workload-shift-summary-20260615-cn.md` | `remote-logs/w7-sf30-workload-shift-formal-20260615-1536/w7-sf30-workload-shift.json` | `remote-logs/w7-sf30-workload-shift-formal-20260615-1536/DONE`; `FAILED` absent; summary Gate=GO | Self-tuning evidence with derived-workload caveat; not full-store production trace |
| W8 property + 2-hop | `bash baseline/run_w8_property_2hop_20260612.sh` with recorded run id, then `python3 baseline/summarize_w8_property_2hop_20260615.py --log-root remote-logs/w8-property-2hop-20260614-2025 --output baseline/w8-property-2hop-summary-20260615-cn.md --source-label remote-logs/w8-property-2hop-20260614-2025` | W8 per-variant wrapped JSON under `remote-logs/w8-property-2hop-20260614-2025/{schema,budg-b64,semantic}` | `remote-logs/w8-property-2hop-20260614-2025/DONE`; wrapped payloads parsed | Property/2-hop evaluation |
| W9 mixed read/write | `MODE=formal RUN_FORMAL=1 DURATION=1800 CHECKPOINT_SECS=300 SCHEMA_STORE=... BUDG_B64_STORE=... SEMANTIC_STORE=... bash baseline/run_w9_steady_state_20260612.sh`, then `python3 baseline/summarize_w9_steady_state_20260615.py --log-root remote-logs/w9-steady-state-formal-20260615-1200 --output baseline/w9-steady-state-summary-20260615-cn.md --source-label remote-logs/w9-steady-state-formal-20260615-1200` | `remote-logs/w9-steady-state-formal-20260615-1200/{schema,budg-b64,semantic}.jsonl` | `DONE` exists; `FAILED` absent; each JSONL has start + 6 checkpoints + done; writer_errors=0 | Mixed read/write steady-state |
| W13 schema evolution | `RUN_ID=w13-schema-evolution-20260614-0004 bash baseline/run_w13_schema_evolution_20260613.sh` | `remote-logs/w13-schema-evolution-20260614-0004/tests.tsv`, per-test stdout/stderr, `summary.md` | `DONE` exists; all 10 tests pass | Schema-evolution correctness claim map |
| W13 supplemental sanity | Existing run `w13-schema-evolution-20260615-143649`; do not rerun unless needed | `remote-logs/w13-schema-evolution-20260615-143649/summary.md` | `DONE` exists; all 10 tests pass | Supplemental only |

## Artifact Packaging Map

| Artifact group | Include | Exclude / caveat |
|---|---|---|
| Source code | `src/`, `tests/`, runner scripts in `baseline/run_w*.sh`, summarizers in `baseline/summarize_*.py` | Do not include large generated stores in paper artifact by default. |
| Raw evidence | W6/W7/W8/W9/W13 remote-log directories listed above | W8 physical stores were consumed/mutated by W9; use W8 raw JSON and summary as frozen evidence, not the physical stores. W7 is a real-SF30-derived workload-shift formal run, not a full-store production trace. |
| Generated tables | `baseline/sf100-matrix-20260613-cn.md`, `baseline/w7-sf30-workload-shift-summary-20260615-cn.md`, `baseline/w8-property-2hop-summary-20260615-cn.md`, `baseline/w9-steady-state-summary-20260615-cn.md`, W13 `summary.md` | Old partial reports should not be cited unless explicitly marked historical. |
| Progress and audit | `baseline/seml0-current-progress-cn.md`, `baseline/w10-evidence-inventory-20260615-cn.md`, this checklist, final caveat table | Progress docs are audit trail, not paper tables. |
| W7 evidence | `remote-logs/w7-sf30-workload-shift-formal-20260615-1536`, `baseline/w7-sf30-workload-shift-summary-20260615-cn.md`, `baseline/run_w7_sf30_workload_shift_20260615.sh`, `baseline/summarize_w7_sf30_workload_shift_20260615.py`, and `baseline/w7-workload-shift-progress-20260613-cn.md` | Must be labeled real-SF30-derived formal evidence with a derived-workload caveat, not a full-store production trace. |

## Verification Commands

```bash
cd /data/WorkSpace/lsmgraph-rs

test -f remote-logs/w6-sf100-matrix-20260613-132325/DONE
test ! -f remote-logs/w6-sf100-matrix-20260613-132325/FAILED
python3 baseline/summarize_w6_sf100_matrix_20260613.py --log-dir remote-logs/w6-sf100-matrix-20260613-132325 --out baseline/sf100-matrix-20260613-cn.md

test -f remote-logs/w7-sf30-workload-shift-formal-20260615-1536/DONE
test ! -f remote-logs/w7-sf30-workload-shift-formal-20260615-1536/FAILED
python3 baseline/summarize_w7_sf30_workload_shift_20260615.py --json remote-logs/w7-sf30-workload-shift-formal-20260615-1536/w7-sf30-workload-shift.json --out baseline/w7-sf30-workload-shift-summary-20260615-cn.md

test -f remote-logs/w8-property-2hop-20260614-2025/DONE
test ! -f remote-logs/w8-property-2hop-20260614-2025/FAILED
python3 baseline/summarize_w8_property_2hop_20260615.py --log-root remote-logs/w8-property-2hop-20260614-2025 --output baseline/w8-property-2hop-summary-20260615-cn.md --source-label remote-logs/w8-property-2hop-20260614-2025

test -f remote-logs/w9-steady-state-formal-20260615-1200/DONE
test ! -f remote-logs/w9-steady-state-formal-20260615-1200/FAILED
python3 baseline/summarize_w9_steady_state_20260615.py --log-root remote-logs/w9-steady-state-formal-20260615-1200 --output baseline/w9-steady-state-summary-20260615-cn.md --source-label remote-logs/w9-steady-state-formal-20260615-1200

test -f remote-logs/w13-schema-evolution-20260614-0004/DONE
test ! -f remote-logs/w13-schema-evolution-20260614-0004/FAILED
```

## Remaining W10 Work

| Task | Status |
|---|---|
| Paper evaluation table rewrite | Not done |
| Measured/controlled/simulated/TODO labels in paper | Not done |
| Appendix command table | Not done |
| Artifact README/checklist integration | Draft checklist done here; paper/artifact integration not done |
| Submission-ready verdict | Not done |
