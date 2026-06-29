# SemL0 3+3 Baselines

Generated at: 2026-06-30T02:49:33+08:00

This package tracks the baseline strengthening work for the CIDR draft.

- `progress-table.md`: current status of each selected system.
- `effect-table.md`: only measured rows that pass the gate, plus clearly labeled internal/layout rows.
- `metrics.tsv`: machine-readable metric rows used by the effect table.
- `correctness.tsv`: machine-readable correctness rows.
- `artifact-attempt-summary.md`: short paper-facing summary of what can and cannot be claimed.
- `systems/`: per-system status files, raw logs, and DONE/FAILED markers as experiments run.

Do not promote a system into the numeric table unless it passes the same-workload count/hash digest gate.
