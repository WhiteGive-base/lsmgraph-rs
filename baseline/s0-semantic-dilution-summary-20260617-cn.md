# S0 Semantic Dilution Diagnosis

Last update: 2026-06-17 14:07 CST

## Scope

This is an offline/proxy diagnosis. It does not run compaction, create a new store, or implement S1/S2/S3. It compares existing schema/coarse evidence against semantic-pruned variants using C10 attribution rows.

Raw TSV:

```text
baseline/s0-semantic-dilution-summary-20260617.tsv
```

## Proxy Dilution Table

Ratio means `schema/coarse baseline divided by variant`. Larger than 1 means the variant avoids work that the coarse layout cannot prune.

| Source | Scenario | Variant | elapsed ratio | filter-passed ratio | body-read ratio | read-byte ratio | offset-lookup ratio |
|---|---|---|---:|---:|---:|---:|---:|
| W14 | degree-class | edge-type-only | 127.875 | 145.943 | 1.000 | 125.913 | 151.190 |
| W14 | degree-class | budg-b64 | 129.973 | 145.903 | 1.000 | 125.926 | 153.938 |
| W14 | degree-class | semantic | 127.896 | 145.271 | 1.000 | 125.597 | 150.907 |
| W14 | property-required | edge-type-only | 1.167 |  |  |  |  |
| W14 | property-required | budg-b64 | 1.000 |  |  |  |  |
| W14 | property-required | semantic | 0.875 |  |  |  |  |
| W8 | 2hop-typed | budg-b64 | 1.322 | 1.259 | 1.263 | 1.079 | 1.332 |
| W8 | 2hop-typed | semantic | 1.328 | 1.260 | 1.263 | 1.080 | 1.338 |
| W8 | property-presence | budg-b64 | 1.330 |  | 1.286 | 1.169 | 1.334 |
| W8 | property-presence | semantic | 1.335 |  | 1.286 | 1.169 | 1.341 |
| W8 | property-equality | budg-b64 | 1.256 |  | 1.286 | 1.169 | 1.259 |
| W8 | property-equality | semantic | 1.335 |  | 1.286 | 1.169 | 1.340 |
| W6 | core | edge-type-only | 1.007 | 1.000 | 1.000 | 1.000 | 1.007 |
| W6 | core | budg-b64 | 1.007 | 1.000 | 1.000 | 1.001 | 1.008 |
| W6 | core | semantic | 1.055 | 0.994 | 1.000 | 1.039 | 1.066 |

## Interpretation

- W14 degree-class is a strong proxy signal for pruning-surface loss: schema/coarse behavior passes far more segments than edge-type-only/budg-b64/semantic and pays a much larger offset/probe cost.
- W8 2-hop and property scenarios provide smaller but useful proxy evidence that property/query-aware pruning can reduce passed segments and latency versus schema.
- W6 core is weak for S0: semantic is not consistently better than edge-type-only/budg-b64, so it should not be used as evidence for composite semantic superiority.
- This is still proxy evidence. It does not directly measure post-compaction L1/L2 exactness retention, schema/tombstone fallback after merge, or rewrite cost.

## Verdict

```text
G3/S0 verdict: FALLBACK / PROXY SIGNAL
What it supports: semantic dilution is plausible and visible when exact pruning surfaces are replaced by coarse/schema behavior.
What it does not support: starting S1/S2/S3 implementation for this submission, or claiming measured multi-level semantic compaction.
Paper handling: write as limitation/future-work motivation unless a later small targeted compaction diagnosis is explicitly approved.
```

## Next Minimal Action

Do not start full S3. Return to W10 claim safety / writing freeze with W14, C10, and S0 caveats integrated.
