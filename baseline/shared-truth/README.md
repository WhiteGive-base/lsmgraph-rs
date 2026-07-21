# P01 shared truth and ID bridge

This package freezes one typed-neighbor query list for SemL0 and the external
systems.  The truth TSV always uses **dense IDs** and this exact header:

```text
query_index\tedge_type\tsrc\tcount\tsum_hash\txor_hash
```

`query_index` must be contiguous from zero.  `sum_hash` and `xor_hash` use the
same wrapping `mix64` algorithm in the existing C++ baseline drivers.  The
committed SF10 truth has 1,700 rows and SHA-256
`876ec4be45bb7c220ce595b8d8efd5c79d13db285569cc0d6ae3e8bc19a1c788`.

## 1. Produce the dense graph and durable ID bridge

```bash
python3 baseline/convert_livegraph_edges.py \
  --input run/edges-original.tsv \
  --output run/edges-dense.txt \
  --summary run/convert-summary.json \
  --id-map-dir run/id-map

(cd run/id-map && sha256sum -c SHA256SUMS)
```

The converter writes:

- `original-to-dense.tsv` and `dense-to-original.tsv`;
- `id-map-manifest.json`, including vertex count and the stable
  `fnv1a64-le-dense-original-v1` mapping hash;
- `SHA256SUMS` for both map files.

The map is valid only for the exact dense edge list/truth generation lineage.
Do not combine a map regenerated from a different scan order with an archived
truth TSV, even if vertex and edge counts happen to match.

## 2. SemL0 correctness gate and shared sample plan

```bash
target/release/lsmgraph --io-backend blocking shared-truth-verify \
  --data-dir run/store/schema \
  --truth-tsv run/truth-s50-seed42.tsv \
  --id-map-dir run/id-map \
  --expected-queries 1700 \
  --l0-layout schema \
  --sample-plan-out run/shared-truth-plan.json \
  --output run/schema-truth-result.json
```

For a signature-forced variant, add `--force-signature`; for semantic degree
routing, add `--semantic-degree-hint`.  Acceptance is
`verification.status=PASS`, `checked=1700`, and `mismatches=0` for every
A0--A6 store.  The command is explicitly correctness-only and its elapsed time
must not be used in a performance figure.

`shared-truth-plan.json` is compatible with the existing benchmark path:

```bash
target/release/lsmgraph --io-backend blocking storage-bench \
  --data-dir run/store/schema \
  --sample-plan-in run/shared-truth-plan.json \
  --repeats 1 --l0-layout schema
```

The plan contains original SemL0 source IDs but preserves the truth TSV query
order and expected degree.  Generate it once from the frozen map/truth pair and
reuse the exact file across configurations.

## 3. LiveGraph shared-truth consumer

`baseline/external-drivers/livegraph_driver.cpp` accepts the same truth file:

```bash
baseline/external-drivers/livegraph_driver \
  --edges run/edges-dense.txt \
  --truth-tsv run/truth-s50-seed42.tsv \
  --block-path run/livegraph-block \
  --wal-path run/livegraph-wal \
  --output run/livegraph-result.json
```

In truth mode it executes all TSV rows in order, records latency distributions,
and rejects any `count/sum_hash/xor_hash` mismatch.  Building this driver still
requires the existing LiveGraph headers and shared library.

## 4. Small-fixture tests

```bash
python3 -m unittest baseline/shared-truth/test_convert_livegraph_edges.py
cargo test shared_truth --bin lsmgraph --lib
```

The fixture covers negative edge types, the bidirectional map, mapping hash,
SHA-256 provenance, SemL0 dense-digest verification, and sample-plan order.
