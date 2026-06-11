# SemL0 Limitations Section Draft MD

Date: 2026-06-05

## Status

```text
limitations_section_draft_ready=yes
md_only=yes
paper_section_draft=yes
tex_generated=no
experiments_run=0
benchmarks_run=0
stores_deleted=0
cleanup_approved=no
safe_to_run_now=no
safe_to_delete_now=no
final_submission_ready=no
```

## Purpose

This MD document is the paper-section draft for Limitations and Future Work. It
turns the current evidence boundaries into paper prose so the SemL0 claims stay
defensible: latest-code SF1 evidence is strong enough for the current source
package, but final submission, broader scale, production write behavior,
sustained adaptation, broad schema migration, and store cleanup remain open.

This stage does not edit TeX, generate PDF, run experiments, rerun benchmarks,
delete stores, approve cleanup, choose a venue, or mark final submission ready.

## Source Anchors

| Anchor | Role |
|---|---|
| `paper/final-paper-outline-md.md` | required limitations list |
| `paper/paper-narrative-spine.md` | safe claim ledger and contribution boundary |
| `paper/evaluation-section-draft-md.md` | evaluation boundary and claims to avoid |
| `paper/evaluation-prose-polish-md.md` | RQ-to-system-chain map and evaluation claim boundaries |
| `paper/query-semantic-physical-design-prose-polish-md.md` | C1 SF1 and materialization policy boundary |
| `paper/feedback-compaction-prose-polish-md.md` | controlled feedback and sustained-run boundary |
| `paper/schema-evolution-prose-polish-md.md` | schema/snapshot safety and old-store no-invalidation boundary |
| `paper/related-work-section-draft-md.md` | bridge from positioning to limitations |
| `paper/five-step-total-control-summary.md` | five-step state and open work |
| `paper/sustained-feedback-experiment-approval-packet.md` | sustained feedback not-approved boundary |
| `paper/store-cleanup-approval-packet.md` | cleanup not-approved boundary |
| `paper/schema-evolution-section-draft-md.md` | schema migration limitations |
| `paper/mixed-schema-delta-stress-inventory.md` | targeted correctness coverage and future testing |
| `paper/final-submission-decision-record.md` | venue/TeX/PDF/page/visual gates |

## Intended Paper Placement

This draft corresponds to:

```text
Section 9. Limitations And Future Work
```

It should appear after Related Work and before Conclusion. It answers:

```text
What does the current SemL0 package not prove yet?
```

## Draft Section Text

### 9. Limitations And Future Work

SemL0's current evidence package is intentionally bounded. The paper argues that
property-graph query semantics can guide LSM delta layout, candidate pruning,
feedback compaction, and schema/snapshot metadata. It does not claim that the
current prototype is a complete graph DBMS, a production-tuned storage engine,
or a finished venue-specific submission artifact.

The most important limitation is scale freshness. The current latest-code
performance claim is SF1-scoped. The package contains latest-code SF1 evidence
for query-semantic layout, benefit-scored materialization, controlled feedback
adaptation, targeted schema/snapshot correctness, and write/rewrite cost
proxies. It does not contain a final latest-code SF30/SF100 refresh after the
latest schema and snapshot changes. Older larger-scale evidence may remain
useful as historical context, but it should not be used as the current final
performance claim without a new approved run, regenerated tables, and an
updated claim decision.

The limitations follow the same chain as the Evaluation:

```text
C1 pruning surface -> materialization policy -> P3 maintenance -> P2/P4 safety -> P5 cost/evidence accounting
```

This section is the claim-boundary layer for that chain. It says which parts are
currently supported and which parts require a separate approved stage before the
paper can strengthen the claim.

Feedback compaction is also bounded. The current P3 evidence shows controlled
workload-shift adaptation: feedback can move compaction priority toward hot
semantic ranges in deterministic microbenchmarks, and the paper records rewrite
cost proxies separately from read-byte savings. The sustained feedback
experiment is prepared but not approved:

```text
experiment_approval_status=not_approved
safe_to_run_now=no
experiments_run=0
```

Therefore the paper should not claim production long-run adaptation, global
optimality, negligible feedback overhead, or production write-stall safety. A
future strengthening step should use a fresh log directory, fresh benchmark
stores under that directory, and the acceptance criteria in
`paper/sustained-feedback-experiment-approval-packet.md`.

This is the P3 limitation in system terms: P3 can maintain the C1 semantic
pruning surface under a controlled workload shift, but the current artifact does
not prove that this maintenance layer is production-scheduled, long-running, or
write-stall safe.

Write-path characterization remains a cost-proxy study rather than a production
study. The current evidence can discuss import time, store bytes, file counts,
manifest size, feedback compaction rewrite bytes, and compaction-window latency
proxies. It does not characterize write stalls under concurrent production
traffic, active-reader snapshot garbage collection, compaction scheduling under
mixed foreground workloads, or resource isolation. Those require workload and
runtime controls that are outside the present source package.

Schema evolution is deliberately scoped. SemL0 supports the paper claim that
additive schema changes do not invalidate old storage: catalog epochs advance,
future writes use the current interpretation, old segments remain readable under
their own `schema_epoch`, and unknown or mixed metadata is read conservatively.
This is not a claim of full physical schema migration. Rename, drop, type or
encoding changes, and label split/merge behavior are boundary or future-work
areas unless new implementation and evidence are added. The current property
boundary is fixed-width equality and presence/absence metadata; range, string,
compound predicates, and SQL null semantics remain future work.

This is the P2/P4 limitation in system terms: schema and snapshot interpretation
decide when metadata is safe to use, but the current paper claims targeted
correctness and additive no-rebuild behavior, not a complete physical migration
engine. The central answer remains:

```text
Schema changes do not automatically invalidate old storage.
```

Correctness evidence is targeted rather than exhaustive. The package contains
source-level tests and summaries for schema epochs, tombstones, snapshot-visible
deltas, property epochs, alias/drop/encoding boundaries, compaction, and reopen.
These tests support no-false-negative pruning for the implemented boundary. They
do not replace randomized differential testing over arbitrary dynamic graph
workloads, exhaustive schema-change sequences, or production active-reader
snapshot-GC validation.

The artifact also keeps store cleanup separate from paper claims. The store
cleanup packet identifies exact candidate paths and evidence that must be
preserved, but no cleanup is approved and no store has been deleted:

```text
delete_approval_status=not_approved
safe_to_delete_now=no
stores_deleted=0
```

Future cleanup must be path-by-path, restricted to the approved candidate list,
and recorded in a separate cleanup execution stage. Broad patterns, prefixes, or
directory-wide cleanup are not acceptable evidence-preserving operations.

This is the P5 limitation in system terms: the current package is source-ready
and evidence-mapped, but cleanup and final submission are separate execution
gates. A documentation stage cannot authorize deletion or final readiness.

Finally, the paper is not final-submission ready. The source package is
organized, but the owner decision record still has unset venue and TeX routes.
The remaining gates include venue/template selection, anonymity and appendix
policy, page budget, TeX backend approval, PDF compilation, table and figure
visual inspection, and final source-to-PDF consistency checks. Until those gates
are approved and executed, the correct status is:

```text
final_submission_ready=no
```

### Summary Of Boundaries

| Area | Current supported claim | Not yet supported |
|---|---|---|
| performance scale | latest-code SF1 evidence | final latest-code SF30/SF100 performance |
| feedback compaction | controlled workload-shift adaptation | production long-run adaptation or write-stall safety |
| write cost | import/store/rewrite proxies | production write-overhead characterization |
| schema evolution | additive no-rebuild old-storage readability | full physical schema migration |
| property predicates | fixed-width equality and presence/absence boundary | range/string/compound predicates and SQL null semantics |
| correctness | targeted schema/snapshot/tombstone regression coverage | exhaustive dynamic-graph differential proof |
| artifacts | source-ready MD/table/evidence package | venue-specific compiled final PDF |
| store cleanup | retention and approval packet | deletion execution |

## System-Chain Boundary Map

| System role | Current support | Boundary before strengthening |
|---|---|---|
| C1 pruning surface | latest-code SF1 evidence | approved SF30/SF100 refresh |
| materialization policy | benefit-scored policy evidence | broader workload and optimizer comparison |
| P3 maintenance | controlled workload-shift evidence | sustained run and production write-path study |
| P2/P4 safety | targeted schema/snapshot/tombstone correctness | randomized dynamic differential testing and full migration work |
| P5 evidence package | Markdown source package and Linux validation | venue/template/TeX/PDF/page/visual gates |
| cleanup | retention and approval packet | exact-path deletion approval and execution log |

These limitations do not weaken the core paper thesis. They keep it precise:

```text
SemL0 shows that property-graph query semantics can safely guide LSM delta
layout and maintenance under a conservative exact-proof pruning contract.
```

## Paper-Safe Wording

Safe wording:

```text
The current package supports source-ready SF1, controlled feedback, targeted
correctness, and cost-proxy claims, while leaving SF30/SF100 refreshes,
production write-stall characterization, sustained feedback, broad schema
migration, store cleanup execution, and final venue/PDF readiness as open work.
```

Unsafe wording:

```text
SemL0 is final-submission ready.
SemL0 proves final latest-code SF30/SF100 performance.
SemL0 proves production write-stall safety.
SemL0 implements full schema migration.
SemL0 proves exhaustive dynamic-graph correctness.
The sustained feedback experiment is complete.
Store cleanup is complete.
```

## Future Work Plan

Future work should be separated into explicit stages:

1. SF30/SF100 latest-code refresh, only after compute and store scope are
   approved.
2. Sustained feedback experiment, only with fresh log-dir stores and explicit
   owner approval.
3. Production write-path study with foreground workload, compaction scheduling,
   and active-reader snapshot-GC controls.
4. Broader schema migration work for rename/drop/type-change and physical
   reclamation.
5. Range/string/compound predicate support and clearer null/default semantics.
6. Randomized mixed schema/delta differential testing.
7. Exact-path store cleanup execution, only after path-by-path approval.
8. Venue-specific TeX/PDF conversion and final visual inspection.

## Bridge To Conclusion

The conclusion should not introduce new claims. It should return to the bounded
thesis:

```text
query semantics can guide LSM physical design and maintenance while preserving
conservative correctness under schema evolution and dynamic deltas.
```
