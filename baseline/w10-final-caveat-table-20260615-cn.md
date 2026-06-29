# W10 final caveat table

Generated: 2026-06-15 15:52 CST

This table is the paper-facing caveat set for the current SemL0 submission freeze. It should be copied into the paper/appendix after the evaluation tables are rewritten from regenerated evidence.

| ID | Scope | Caveat | Evidence | Required paper wording |
|---|---|---|---|---|
| C1 | W6 / SF100 latency | Gate 1 is `FALLBACK`: `budg-b64` mean latency is lower than `schema`, but the 1-stddev intervals overlap. | `baseline/sf100-matrix-20260613-cn.md`, Gate Candidates | Do not claim stable latency superiority at SF100. Say the main robust signal is candidate/read-amplification reduction and avoiding the full-semantic cliff. |
| C2 | W6 / read bytes | W6 read bytes are measured, but prior reader-overread caveat still needs careful wording. | `baseline/sf100-matrix-20260613-cn.md`; `baseline/reader-overread-rootcause-eval-cn.md` | Keep read bytes secondary unless the text explicitly explains the caveat. |
| C3 | W6 / RSS | Gate 2 is `GO`, but it is an empirical RSS result, not a theoretical bound. | `baseline/sf100-matrix-20260613-cn.md`, Import RSS | Write measured RSS exactly: schema/naive=0.99x and budg-b64/naive=0.91x. |
| C4 | W7 / self-tuning | W7 now has a real-SF30-derived formal workload-shift run with Gate=GO, but it is not a full-store production trace. | `baseline/w7-sf30-workload-shift-summary-20260615-cn.md`; `remote-logs/w7-sf30-workload-shift-formal-20260615-1536`; `baseline/w7-workload-shift-progress-20260613-cn.md` | Present W7 as query-signature-driven self-tuning evidence with a derived-workload caveat. Do not claim full-store production workload-shift adaptation. |
| C5 | W8 / property predicates | Property predicates support strong improvements, but required-property is an exact-prune sanity case with zeros for all variants. | `baseline/w8-property-2hop-summary-20260615-cn.md` | Use presence/equality/absent-default for aggregate improvement claims; describe required-property separately as exact-prune validation. |
| C6 | W8 / 2-hop | 2-hop reduces body reads, read bytes, and elapsed time, but candidate L0 increases. | `baseline/w8-property-2hop-summary-20260615-cn.md`, 2-hop deltas | Do not claim universal 2-hop candidate reduction. Say 2-hop pruning is mixed and the defensible win is body/read/elapsed. |
| C7 | W9 / mixed read/write | W9 completed for all variants with no writer errors or slow ops, but `semantic` improves latency while increasing candidate L0 and L0 files. | `baseline/w9-steady-state-summary-20260615-cn.md` | Use W9 as workload-coverage/stability evidence. For advantages, state only the delta-backed metrics: semantic p50/p99 improves, candidate L0 and L0 files worsen. |
| C8 | W9 / budg-b64 steady state | `budg-b64` is close to `schema`: candidate L0 -4.1%, p99 -1.2%, p50 +1.8%, final L0 files +4.9%. | `baseline/w9-steady-state-summary-20260615-cn.md` | Do not oversell budg-b64 steady-state latency; describe it as comparable/stable with slight candidate/p99 improvement. |
| C9 | W13 / schema evolution | Tests cover schema epochs, old-segment readability, aliases/drop behavior, fixed-width property encoding, and conservative pruning. They do not cover full online migration or reclamation. | `remote-logs/w13-schema-evolution-20260614-0004/summary.md`; supplemental `remote-logs/w13-schema-evolution-20260615-143649/summary.md` | Claim bounded schema-evolution correctness only. Do not claim full migration/reclamation. |
| C10 | Paper title/scope | W7 formal is available, but W8 2-hop and W9 semantic behavior still have caveats. The broadest title should be chosen carefully. | W7/W8/W9 summaries | Safe default title scope: `Query-Semantic L0 Design`. Broaden to `Query-Semantic Physical Design` only if W10 text explicitly absorbs the caveats. |
| C11 | BACH / property-range positioning | BACH can be framed as solving AL-vs-CSR physical continuity, while SemL0 adds segment-level semantic pruning. Current SemL0 evidence covers labels, edge type, degree class, property presence/equality/absence, feedback compaction, and schema epoch; it does not yet prove numeric property-range pruning such as `Like.time` ranges. | W6/W7/W8/W9/W13 summaries; BACH related-work positioning | Use BACH as motivation/related work. Claim SemL0 prunes impossible L0 segments using exact semantic metadata, but keep property-range pruning as future work or a limitation unless a measured range-metadata experiment is added later. |

## Submission Gate Interpretation

| Gate | Status | Consequence |
|---|---|---|
| Minimum line: W6 + W13 + W10 | Evidence reached; writing assembly in progress | W6 and W13 are complete. W10 has safe draft/checklist/caveat materials, but the final SIGMOD-format paper/appendix still needs assembly. |
| Recommended line: W6 + W7 + W8 + W9 + W13 + W10 | Evidence reached with caveats; writing assembly in progress | W6/W7/W8/W9/W13 are complete. W7 is derived-workload formal evidence; W8/W9 caveats remain paper wording constraints. |
| Stop-expansion line | Reached | Do not start W11/W12 or new external-baseline experiments. Converge W10 writing and artifact packaging from the frozen evidence set. |
