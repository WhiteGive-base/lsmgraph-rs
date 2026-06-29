# W8 property + 2-hop summary

Generated from raw wrapped JSON outputs under:

`remote-logs/w8-property-2hop-20260614-2025`

All W8 JSON payloads were parsed after stripping runner start/done lines.

## Import validation

| variant | input rows | directed edges | snapshot |
|---|---:|---:|---:|
| `schema` | 608,041,914 | 1,087,848,423 | 1,087,848,423 |
| `budg-b64` | 608,041,914 | 1,087,848,423 | 1,087,848,423 |
| `semantic` | 608,041,914 | 1,087,848,423 | 1,087,848,423 |

## Property predicate results

| variant | predicate | candidate L0 mean | body reads mean | read bytes mean | elapsed mean | p50 us | p90 us | p99 us | one-hop edges mean |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `schema` | `required_property` | 0 | 0 | 0 B | 4.0 ms | 25 | 25 | 25 | 0 |
| `schema` | `presence` | 43,074 | 42,980 | 27.84 MiB | 28.93 s | 7,500 | 10,000 | 10,000 | 0 |
| `schema` | `equality` | 43,074 | 42,980 | 27.84 MiB | 28.42 s | 7,500 | 10,000 | 10,000 | 0 |
| `schema` | `absent_default` | 43,074 | 42,980 | 27.84 MiB | 28.76 s | 7,500 | 10,000 | 10,000 | 392,123 |
| `budg-b64` | `required_property` | 0 | 0 | 0 B | 4.3 ms | 25 | 25 | 25 | 0 |
| `budg-b64` | `presence` | 33,608 | 33,425 | 23.81 MiB | 21.95 s | 7,500 | 7,500 | 7,500 | 0 |
| `budg-b64` | `equality` | 33,608 | 33,425 | 23.81 MiB | 22.15 s | 7,500 | 7,500 | 7,500 | 0 |
| `budg-b64` | `absent_default` | 33,608 | 33,425 | 23.81 MiB | 21.99 s | 7,500 | 7,500 | 7,500 | 392,123 |
| `semantic` | `required_property` | 0 | 0 | 0 B | 5.3 ms | 25 | 25 | 25 | 0 |
| `semantic` | `presence` | 33,684 | 33,425 | 23.82 MiB | 22.01 s | 7,500 | 7,500 | 7,500 | 0 |
| `semantic` | `equality` | 33,684 | 33,425 | 23.82 MiB | 21.70 s | 7,500 | 7,500 | 7,500 | 0 |
| `semantic` | `absent_default` | 33,684 | 33,425 | 23.82 MiB | 21.84 s | 7,500 | 7,500 | 7,500 | 392,123 |

## Property deltas vs schema

The required-property mode is an exact-prune case with zero candidates/body reads for all variants, so the aggregate below uses presence/equality/absent-default.

| variant | candidate L0 delta | body reads delta | read bytes delta | elapsed delta |
|---|---:|---:|---:|---:|
| `budg-b64` | -22.0% | -22.2% | -14.5% | -23.2% |
| `semantic` | -21.8% | -22.2% | -14.4% | -23.9% |

## 2-hop results

| variant | candidate L0 mean | body reads mean | read bytes mean | elapsed mean | p50 us | p90 us | p99 us | one-hop edges mean | two-hop edges mean | two-hop sources mean |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `schema` | 2,779,873 | 2,058,576 | 2.84 GiB | 23.21 min | 7,500 | 10,000 | 10,000 | 392,123 | 69,847,426 | 193,581 |
| `budg-b64` | 3,573,234 | 1,629,603 | 2.64 GiB | 17.45 min | 7,500 | 7,500 | 7,500 | 392,123 | 69,847,426 | 193,581 |
| `semantic` | 3,597,424 | 1,629,603 | 2.63 GiB | 17.45 min | 7,500 | 7,500 | 7,500 | 392,123 | 69,847,426 | 193,581 |

## 2-hop deltas vs schema

| variant | candidate L0 delta | body reads delta | read bytes delta | elapsed delta |
|---|---:|---:|---:|---:|
| `budg-b64` | +28.5% | -20.8% | -7.3% | -24.8% |
| `semantic` | +29.4% | -20.8% | -7.4% | -24.8% |

## Conclusion

W8 is `ready-for-paper` with caveats. It supports the reviewer-facing claim that property predicates and 2-hop traversals are covered by the SemL0 evidence block. For property presence/equality/absent-default, `budg-b64` and `semantic` reduce candidate L0, body reads, read bytes, and elapsed time versus `schema`. For 2-hop, `budg-b64` and `semantic` reduce body reads, read bytes, and elapsed time, but their candidate L0 count is higher than `schema`; therefore the paper must not claim universal 2-hop candidate reduction.

Safe claim: W8 strengthens the property-predicate and 2-hop coverage, but 2-hop candidate pruning should be described as mixed; latency/body/read-byte improvements are the defensible 2-hop result.
