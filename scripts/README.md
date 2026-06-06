# Scripts Directory

Date: 2026-06-05

Scripts are grouped by current usefulness. Do not put new scripts directly under the
repository root.

## Layout

| Path | Purpose |
|---|---|
| `experiments/active/` | scripts relevant to the next experiment-closure work |
| `experiments/optional/` | scale or sensitivity scripts that may be useful later |
| `experiments/legacy/` | older round scripts kept for provenance, not first-choice commands |
| `analysis/current/` | table/log summarizers and renderers |
| `maintenance/current/` | non-destructive helper scripts |
| `maintenance/dangerous-archive/` | destructive or cleanup scripts; do not run casually |

## Active Experiment Scripts

```text
experiments/active/run-p1-c1-latest-sf1-controlled.sh
experiments/active/run-p1-c1-benefit-scored-sf1.sh
experiments/active/run-p1-c1-fine-threshold-sweep-sf1.sh
experiments/active/run-p1-c1-micro-threshold-sweep-sf1.sh
experiments/active/run-p3-feedback-workload-shift-microbench.sh
experiments/active/run-p3-feedback-vs-no-feedback-microbench.sh
```

## Important Boundary

Old script presence is not proof that an experiment is complete. New experiment closure
still requires fresh log directories, fresh pass/fail summaries, and current-code
readiness checks.

