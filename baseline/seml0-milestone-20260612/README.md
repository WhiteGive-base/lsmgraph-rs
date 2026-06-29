# SemL0 Milestone-0 Package

生成时间：2026-06-13。目标目录：`baseline/seml0-milestone-20260612/`。

## Generated Files

| File | Purpose |
|---|---|
| `seml0-paper-milestone-cn.md` | 当前阶段中文论文草稿，冻结故事线并保守表述证据 |
| `seml0-current-evidence-tables.md` | 当前证据表、source、caveat 和 TODO 汇总 |
| `seml0-next-strengthening-plan-cn.md` | 下一阶段 P0-P5 强化计划与 Go/No-Go gates |
| `README.md` | 本文件，记录输入、缺失项、命令和安全保证 |

## Input Files Used

| Input | Status |
|---|---|
| `baseline/seml0-linux-main-paper-draft-cn-20260611.md` | used |
| `baseline/seml0-linux-rerun-plan-20260611-cn.md` | used |
| `baseline/sf100-results-s5000.tsv` | used |
| `baseline/sf100-strong-baseline-traces/summary-sf100.tsv` | used |
| `baseline/sf100-strong-baseline-traces/summary-sf100.md` | used |
| `remote-logs/qslsm-sf100-strong-baseline-20260610/summary-sf100.tsv` | used |
| `remote-logs/qslsm-sf100-strong-baseline-20260610/summary-sf100.md` | used |
| `remote-logs/qslsm-sf100-strong-baseline-20260610/manifest.tsv` | used |
| `baseline/seml0-gap-analysis-20260612/seml0-top-conf-gap-analysis-cn.md` | used |
| `baseline/seml0-gap-analysis-20260612/summary-sf100.tsv` | used |
| `baseline/seml0-gap-analysis-20260612/funnel-sf100.tsv` | used |
| `baseline/seml0-gap-analysis-20260612/latency-percentiles-sf100.tsv` | used |
| `baseline/seml0-gap-analysis-20260612/implementation-status-20260612.md` | used for status/caveats |
| `remote-logs/p6-sustained-feedback-20260612/sustained.tsv` | used |
| `remote-logs/p6-sustained-feedback-20260612/sustained.json` | used |
| `remote-logs/qslsm-sf100-maintenance-table-20260612/maintenance-table.tsv` | used |

## Missing Inputs

No requested source file was missing among the files listed for Milestone-0. The following evidence gaps remain and are recorded as TODO/caveat rather than invented numbers:

1. Real measured RocksDB/KV-style baseline.
2. SF100 warm-up/repeats/cache-controlled latency reruns.
3. Precise-offset-read reruns for full semantic and budg-b1024 read_bytes.
4. Mixed read/write steady-state workload.
5. Property-aware workload with measured pruning/latency results.
6. RSS after degree_directory fix and large-store validation.
7. Correctness compare anchored against naive/oracle, not only schema-relative.
8. External baseline numeric table after reproducibility gate.

## Commands Run

Only lightweight inspection and file-copy commands were run. Representative commands:

```bash
pwd
git status --short
ls baseline | head
find baseline -maxdepth 2 \( -name '*seml0*' -o -name '*sf100*' \) | head -50
git rev-parse HEAD
git branch --show-current
find baseline/seml0-gap-analysis-20260612 -maxdepth 1 -type f \( -name '*.tsv' -o -name '*.md' \)
find remote-logs/p6-sustained-feedback-20260612 -maxdepth 2 -type f
find remote-logs/qslsm-sf100-maintenance-table-20260612 -maxdepth 2 -type f
scp -F tmp-ssh-config ... small markdown/tsv/json summary files ...
```

No `cargo build --release` was run. No cargo command was needed for this documentation-only task. No SF100 store was opened. No SF100 import, storage-bench, correctness compare, heavy benchmark, or release binary overwrite was performed. Existing result files under `baseline/sf100-strong-baseline-traces/` and `remote-logs/` were not overwritten.

## Final Git Status Expected

After syncing this package to Linux, `git status --short` is expected to include the pre-existing worktree changes plus this new untracked directory:

```text
 M src/csr/reader.rs
 M src/metrics.rs
?? baseline/seml0-milestone-20260612/
?? baseline/w2-c10-timebox-plan-20260613-cn.md
```
