## 6. Schema-Snapshot Correctness of the Pruning Surface (Contribution C3)

> Path B / lifecycle framing. This section is **Contribution C3: keeping the
> query-semantic pruning surface conservative-correct under additive schema
> evolution, tombstones, degree change, and snapshot-visible deltas.** The
> mechanisms are already strong (W13 plus the lifecycle/reopen tests); the Path B
> re-narration adds (i) the three structural invariants and (ii) the
> no-false-negative theorem that bound the entire pruning surface from Section 4
> and that any compaction retention scheme in Section 5 must preserve.

**Reader question.** *If SemL0's pruning surface depends on labels, edge types,
properties, degree classes, and schema metadata, does a schema change — or a
tombstone, a degree change, or a snapshot view — make old semantic storage
unsafe or useless?*

**Lead answer.** No. Additive schema changes advance catalog interpretation and
future-write routing; old segments remain readable under their original
`schema_epoch`. Exact metadata may still prune; uncertain metadata is read
conservatively. The cost of any schema or snapshot uncertainty is *extra reads*,
never a false negative or discarded storage.

### 6.1 Versioned schema catalog and segment `schema_epoch`

SemL0 separates *logical* schema evolution from *physical* storage layout. The
`SchemaCatalog` (`src/schema.rs`) assigns stable physical identifiers to vertex
labels, edge labels, and properties; records aliases, drop epochs, and an
encoding history; and advances a monotone `current_epoch` whenever the logical
schema changes (`add_vertex_label`, `add_edge_label`, `add_property`,
`alias_edge_label`, and the drop/encoding-change operations each bump the epoch).
Catalog entries carry `valid_from_epoch`, `valid_to_epoch`/`dropped_at_epoch`,
and (for properties) an `encoding_epoch`, so the resolver can answer "what did
identifier *x* mean at epoch *e*?" rather than only "what does it mean now."

Each CSR segment independently records the `schema_epoch` and
`property_encoding_epoch` under which its semantic summaries were built
(`CsrSegmentMeta`, `src/csr/format.rs`). A query compiled under the current
catalog therefore interprets an older segment *through that segment's own epoch*,
not through the latest epoch. The segment's `semantic_state(current_epoch)`
derivation classifies its schema dimension as `Current`, `OlderEpoch`, or
`Uncertain` (an epoch ahead of the catalog), and the read path treats
`OlderEpoch`/`Uncertain` as candidates rather than pruning on stale assumptions.

### 6.2 Additive schema change does not invalidate old storage

The central reviewer concern is whether adding a label or property forces a
global rebuild. It does not.

**Add an edge label.** SemL0 creates a catalog entry, exposes the *stable*
physical edge-type identifier, advances the epoch, and routes future writes
through the new epoch. Existing segments keep their older `schema_epoch`. Because
edge-type ids are stable, an old segment that is an *exact* partition for a
different edge type can be auto-skipped for a query over the new label — the
`edge_type` prune reason from Section 4 fires directly, without any rewrite. An
old segment that is mixed, unknown, tombstone-sensitive, or has incomplete
summary completeness is *kept* (the `mixed_unknown_fallback` /
`schema_tombstone_fallback` keep reasons). Adding a label is thus a catalog +
future-write event, not a store-invalidation event:

```text
add edge label  =>  catalog epoch advance + future-write routing
                =>  old exact-disjoint segments auto-skip (stable edge_type id)
                =/= old store invalidation
                =/= mandatory segment rewrite
```

**Add a property.** Adding a property records a property id, its
default-or-null rule, and an `encoding_epoch` for future rows; old rows are not
rewritten to add a physical column. A required-property predicate over the new
property can be exactly pruned only where a segment's exact property summary
proves absence; otherwise the segment is read conservatively, and a projection
returns the catalog-defined default/`NULL` without reading a nonexistent column.
This holds within the implemented fixed-width-property boundary only (W13).

This is the W13 result: `new_edge_label_prunes_exact_segments_but_reads_mixed_segments`
and `schema_epoch_change_keeps_old_segments_readable` both pass, confirming that
additive change shifts pruning *precision* without producing a false negative or
requiring a global rewrite
(`baseline/w13-schema-evolution-summary-20260614.md`).

### 6.3 Exact-proof pruning vs conservative fallback under schema change

The Section 4 contract is unchanged here: prune only on proof of disjointness or
absence; otherwise read conservatively. Schema evolution interacts with it in
exactly one direction — it can *lower* pruning precision by turning some prune
reasons into keep reasons until a later compaction refreshes hot metadata, but it
can never turn a keep into an unsafe prune. Concretely, a segment written before
an additive change resolves its summaries against its own epoch; if that epoch
no longer suffices to prove disjointness for the new predicate, the segment falls
into `mixed_unknown_fallback` (kept) rather than being skipped. Schema evolution
therefore changes *interpretation* before it changes *layout*: the catalog epoch
affects future writes immediately, while old bytes stay correct under their
recorded epoch.

### 6.4 Alias, drop, encoding, and degree-change boundaries (no over-claim)

Non-additive history is *representable* in the catalog, but the paper must not
inflate it into a full migration engine:

- **Alias / rename.** A logical rename is a catalog alias to a canonical
  physical id (`alias_edge_label`; `resolve_edge_label` honors the alias from its
  `valid_from_epoch`). No old segment is rewritten.
- **Drop.** A dropped property is hidden from current public value queries (its
  `dropped_at_epoch` makes `entry_is_visible_at` return false at the query epoch)
  while older topology and historically visible bytes remain readable where
  snapshot rules require them. Physical reclamation after drop is future work.
- **Encoding / type change.** An encoding change advances the property
  `encoding_epoch`, and a row is decoded under *its own* `property_encoding_epoch`,
  so old rows dispatch to the decoder appropriate for their stored encoding
  (W13: `property_encoding_change_advances_catalog_and_segment_epochs`,
  `public_property_value_query_uses_row_encoding_epoch_after_type_change`).
- **Degree change.** A source whose degree class changes over time is handled by
  the same conservative rule: a segment is pruned on a degree predicate only when
  its `degree_class_exact` flag holds; an uncertain or stale degree class is kept.

These cases demonstrate versioned interpretation, not arbitrary migration. Broad
rename/drop rewrite, online full-store rewrite, and general type migration remain
future work.

### 6.5 Snapshot / tombstone interaction and the compaction safe point

Schema and snapshot visibility must be handled together. A single segment can mix
visible inserts, hidden tombstones, old property encodings, and metadata written
under an older epoch. Two rules keep this safe:

1. **Tombstone-sensitive segments are never pruned on property absence.** When an
   exact summary would otherwise prune via `property_absence` but
   `may_contain_tombstones` is set, the decision falls back to
   `schema_tombstone_fallback` (kept). A segment that may delete an edge cannot be
   skipped, because the deletion is part of the visible answer.
2. **Compaction cannot collapse history past the snapshot safe point.** Merge may
   garbage-collect a tombstone only once no live snapshot can still observe the
   pre-deletion state; until then, both the insert and its tombstone must survive
   compaction and reopen. The W13 lifecycle tests
   `schema_epoch_snapshot_mixed_delta_survives_compaction_and_reopen` and
   `property_schema_snapshot_mixed_delta_survives_compaction_and_reopen` exercise
   exactly this: a mixed schema + snapshot + tombstone delta survives both
   compaction and engine reopen with correct results.

### 6.6 Three invariants and the no-false-negative theorem

The mechanisms above can be stated as three structural invariants that bound the
entire pruning surface created in Section 4 and that any compaction retention
scheme in Section 5 must preserve.

**Invariant I (Segment Schema).** Every segment records the `schema_epoch` and
`property_encoding_epoch` under which its semantic summaries and property
encodings were produced, and these are immutable for the life of the segment.
*(Anchor: `CsrSegmentMeta.schema_epoch` / `property_encoding_epoch`.)*

**Invariant II (Epoch-Aware Resolution).** A read interprets each segment through
its *own* recorded epoch via the versioned catalog (`valid_from_epoch`,
`dropped_at_epoch`, `encoding_epoch`), never through the current epoch alone; a
segment whose epoch differs from the catalog's is classified `OlderEpoch` or
`Uncertain` rather than assumed current. *(Anchors: `SchemaCatalog::resolve_*`,
`entry_is_visible_at`, `CsrSegmentMeta::semantic_state`.)*

**Invariant III (Conservative Pruning).** A segment is pruned only when an
`Exact`/`Conservative` summary *proves* disjointness or absence for the
signature; mixed, unknown, tombstone-sensitive, older-epoch, or uncertain
metadata is always kept. *(Anchor:
`CsrSegmentMeta::signature_pruning_decision`; `allows_semantic_pruning`.)*

**Theorem (No False Negative under schema and snapshot change).** *Let `S` be a
read whose `GraphAccessSignature` is compiled under catalog epoch `e`, and let
`g` be a segment that contains at least one edge visible to `S` under epoch `e`
and the read's snapshot. Then, under Invariants I–III, `signature_pruning_decision`
does not prune `g`; i.e. `g` is retained as a candidate and its rows are
validated. Consequently, additive schema changes, tombstones, degree changes,
and snapshot-visible deltas can reduce pruning precision (move mass from prune
reasons to keep reasons) but cannot remove a segment that holds a visible match.*

*Proof sketch.* Pruning occurs only through a *prune* reason of the Section 4
taxonomy. Each prune reason requires an `Exact`/`Conservative` summary
(Invariant III) interpreted at `g`'s own epoch (Invariant II) over immutable,
correctly-recorded metadata (Invariant I): `time` requires a disjoint timestamp
window; `src_label`/`dst_label`/`edge_type`/`direction`/`degree` each require a
proven key/class disjointness; `property_absence` requires an exact summary
proving the property is absent *and* the segment to be tombstone-clean (otherwise
`schema_tombstone_fallback` keeps it). If `g` holds an edge visible to `S`, none
of these disjointness proofs can succeed for that segment, so the procedure
reaches a *keep* reason. Schema/snapshot uncertainty can only *invalidate* a
disjointness proof (degrading `Exact` toward `Conservative`/`Unknown`, which
forces `mixed_unknown_fallback`), never manufacture one. Hence `g` is kept. ∎

This theorem is the safety contract for the whole lifecycle: Section 4's surface
is created under it, and any Section 5 compaction-retention scheme is only
*permitted* to change which keep/prune reason a segment lands in — it can blur or
sharpen the surface, but it cannot violate the theorem.

### 6.7 Evidence and claim boundary

The W13 suite (`baseline/w13-schema-evolution-summary-20260614.md`) reports ten
schema-evolution tests passing, covering: epoch advance on logical change; old-segment readability
across epochs; mixed schema + snapshot + tombstone deltas surviving compaction
and reopen; alias resolution; drop-property boundary; epoch-aware fixed-width
decoding; and exact-vs-mixed pruning under a new edge label.

**Safe claims.** Add-label / add-property schema changes do not invalidate old
storage; stable edge-type ids let old exact segments auto-skip; unknown / mixed /
tombstone-sensitive / older-epoch metadata causes *extra reads, not false
negatives*; compaction cannot collapse snapshot-visible history past the safe
point.

**Do not write.** Arbitrary or complete schema migration; online full-store
rewrite; automatic physical reclamation after drop; general type migration;
range / string / compound property predicates; SQL null semantics; negligible
schema-change overhead.

### 6.8 Figure hook and bridge

A timeline figure can show epoch `e0` (old segment written with `{KNOWS}`),
epoch `e1` (catalog adds `LIKES`), future `LIKES` writes entering new L0
segments, a read resolving `LIKES` under `e1`, the old exact-disjoint segment
auto-skipping, an unknown/mixed old segment read conservatively, and a hot mixed
region lazily re-layouted by compaction — without implying any rewrite at the
schema-change boundary. The bridge to Evaluation: schema evolution and
snapshot-correct deltas are treated as a *correctness boundary* on the pruning
surface, evaluated as a no-false-negative property, not as a physical-migration
performance claim.

### Evidence map

- `baseline/w13-schema-evolution-summary-20260614.md` — W13: the schema-evolution
  and lifecycle/reopen test suite (epoch advance, old-segment readability,
  mixed-delta survival across compaction + reopen, alias/drop, encoding-epoch
  decoding, exact-vs-mixed pruning under a new label).
- `SEML0-K4-STATUS-AND-PLAN-20260617-CN.md` — Path B framing; C3 = reuse +
  re-narrate, add the three invariants + the no-false-negative theorem.
- Code anchors: `SchemaCatalog`, `SemanticSummaryCompleteness`,
  `entry_is_visible_at`, `resolve_edge_label`/`resolve_property` (`src/schema.rs`);
  segment `schema_epoch` / `property_encoding_epoch`, `semantic_state`,
  `signature_pruning_decision` with `schema_tombstone_fallback` (`src/csr/format.rs`).
- Cross-section anchors: §4 prune/keep reason taxonomy (the lever this section
  bounds); §5 (C2) compaction retention must preserve the no-false-negative
  theorem.
