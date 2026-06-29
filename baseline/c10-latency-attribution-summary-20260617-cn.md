# C10 Latency Attribution Summary

Last update: 2026-06-17 14:00 CST

## Scope

This is an offline attribution pass. It reuses existing W6/W8/W14 JSON and stderr logs only; no new SF30/SF100 runner was started.

Raw TSV:

```text
baseline/c10-latency-attribution-summary-20260617.tsv
```

## Focus Rows

| Source | Scenario | Variant | rows | elapsed_sum_ms | open_s | probe_total_us | offset_lookup_us | body_read_us | candidate_l0 | filter_passed | body_reads | body_bytes |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| W6 | core | budg-b64 | 9 | 390288 |  | 384660083 | 361451118 | 21812600 | 6022507 | 548514 | 532193 | 153829408 |
| W6 | core | edge-type-only | 9 | 390493 |  | 384905392 | 361759196 | 21746533 | 5899015 | 548404 | 532193 | 153829408 |
| W6 | core | schema | 9 | 393126 |  | 387530229 | 364399730 | 21769970 | 5929197 | 548549 | 532227 | 153835168 |
| W6 | core | semantic | 9 | 372610 |  | 365336026 | 341987556 | 21854747 | 7782877 | 552099 | 532193 | 153829408 |
| W8 | 2hop-typed | budg-b64 | 1 | 1054609 |  | 1039200600 | 968002610 | 66046980 | 3573234 | 1635874 | 1629603 | 2247665568 |
| W8 | 2hop-typed | schema | 1 | 1394167 |  | 1378473697 | 1289664315 | 82679985 | 2779873 | 2060374 | 2058576 | 2276854176 |
| W8 | 2hop-typed | semantic | 1 | 1050198 |  | 1034790434 | 963721845 | 65843199 | 3597424 | 1635833 | 1629603 | 2247665568 |
| W8 | property-equality | budg-b64 | 1 | 22792 |  | 22603665 | 21150235 | 1362229 | 33608 | 0 | 33425 | 12547936 |
| W8 | property-equality | schema | 1 | 28634 |  | 28429160 | 26634543 | 1684338 | 43074 | 0 | 42980 | 12944032 |
| W8 | property-equality | semantic | 1 | 21451 |  | 21228740 | 19873573 | 1266420 | 33684 | 0 | 33425 | 12547936 |
| W8 | property-presence | budg-b64 | 1 | 22174 |  | 21981312 | 20568793 | 1325091 | 33608 | 0 | 33425 | 12547936 |
| W8 | property-presence | schema | 1 | 29485 |  | 29283780 | 27436652 | 1737142 | 43074 | 0 | 42980 | 12944032 |
| W8 | property-presence | semantic | 1 | 22083 |  | 21853770 | 20452487 | 1311881 | 33684 | 0 | 33425 | 12547936 |
| W14 | degree-class | budg-b64 | 1 | 5973 | 0.067 | 5234241 | 5023833 | 196023 | 1068450 | 7351 | 5000 | 621600 |
| W14 | degree-class | edge-type-only | 1 | 6071 | 0.029 | 5332912 | 5115147 | 203316 | 1069591 | 7349 | 5000 | 621600 |
| W14 | degree-class | schema | 1 | 776331 | 594.798 | 774249620 | 773361032 | 202552 | 1072533 | 1072533 | 5002 | 621664 |
| W14 | degree-class | semantic | 1 | 6070 | 674.880 | 5339969 | 5124755 | 200661 | 1068646 | 7383 | 5000 | 621600 |
| W14 | property-required | budg-b64 | 1 | 7 | 0.067 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| W14 | property-required | edge-type-only | 1 | 6 | 0.020 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| W14 | property-required | schema | 1 | 7 | 583.300 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| W14 | property-required | semantic | 1 | 8 | 744.251 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## Interpretation

- C10 confirms that latency cannot be inferred from read bytes alone. The available metrics split end-to-end storage time into CSR probe stages, and the dominant visible fixed cost remains offset/probe work plus engine open/materialization effects.
- W14 degree-class is the clearest current evidence: schema spends about 775s in the 5000-sample run, while edge-type-only/budg-b64/semantic finish in seconds after reuse-store open. This supports schema-vs-pruned latency cliff avoidance.
- W14 does not support a strong composite-over-edge-type-only latency claim: edge-type-only, budg-b64, and semantic are close on degree-class, and property-required reports weak/zero candidate/body counters in the current JSON.
- W6/W8 should be used for read-amplification and candidate/body-read attribution. C10 should label latency as explained/qualified, not as a standalone broad advantage claim.

## Verdict

```text
G2/C10 verdict: FALLBACK / CLAIM-SAFETY
Allowed in paper: explain why read/candidate reductions do not always translate to latency; use degree-class schema-vs-pruned cliff as bounded latency evidence.
Not allowed in paper: broad SemL0 latency superiority, or composite semantic latency superiority over edge-type-only.
Next gate: G3/S0 can run only as a small targeted semantic-dilution diagnosis, not as full S3 implementation.
```

## Next Minimal Action

Run S0 only if it reuses existing stores/logs or stays small enough to diagnose semantic dilution. Do not start S1/S2/S3 implementation.
