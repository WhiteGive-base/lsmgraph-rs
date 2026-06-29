# External Baseline Status（2026-06-26）

| System | Status | Scale | Evidence | Remaining boundary |
|---|---|---:|---|---|
| LiveGraph | measured external baseline; digest PASS | SF1 smoke + SF10 typed-neighbor | `livegraph/artifact-report.md`, `livegraph/metrics.tsv`, `livegraph/correctness.tsv`, `livegraph/run-config.json`, `livegraph/build.log` | SF10 typed-neighbor only; not production deployment; not a full apples-to-apples graph DB comparison |
| Teseo | not attempted in this run | - | CIDR survey only | build/load/workload attempt |
| GraphOne | not attempted in this run | - | CIDR survey only | build/load/workload attempt |
| LLAMA | not attempted in this run | - | CIDR survey only | analytics-only sanity if needed |

Current use: LiveGraph can now support a numeric external-baseline sidebar/table for the SF10 typed-neighbor workload, with explicit scope boundaries.
