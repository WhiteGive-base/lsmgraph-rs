# lsmgraph-rs

`lsmgraph-rs` is a Rust prototype of an LSM-style dynamic property graph storage engine. It includes the storage core, CSR segment reader/writer, semantic metadata, SNB import/query support, and regression tests used to validate the engine behavior.

This public branch intentionally does not include datasets, generated stores, experiment logs, paper drafts, or vendored LDBC driver trees. Prepare those inputs separately and pass their paths through CLI flags or environment variables.

## What Is Included

- Tokio-based engine shell and bounded pread/pwrite I/O abstraction.
- Optional Linux `direct-io` and `io-uring` backends.
- MemGraph rotation, L0 version chains, L0-to-L1 compaction, and multi-level lookup.
- CSR storage format with sparse per-source offsets and semantic metadata.
- SNB topology/property import and DGS-compatible HTTP adapter.
- Regression tests covering engine, CSR, schema, semantic, and lifecycle behavior.

## Build

```bash
cargo test
cargo build --release
cargo build --release --features direct-io,uring
```

The `direct-io` and `uring` features are Linux-oriented. Use the default build first when checking portability.

## Data Layout

The examples below assume:

```bash
export LDBC_SNB_DATA=${LDBC_SNB_DATA:-data/social_network}
export LSMGRAPH_STORE=${LSMGRAPH_STORE:-store/sf1-full}
```

`LDBC_SNB_DATA` should point to an LDBC SNB social network directory. `LSMGRAPH_STORE` is a local generated store and is ignored by Git.

## Import Examples

Import `person_knows` into a fresh store:

```bash
target/release/lsmgraph import \
  --input "$LDBC_SNB_DATA" \
  --data-dir store/sf1 \
  --relation person_knows
```

Import supported SNB topology relations:

```bash
target/release/lsmgraph import \
  --input "$LDBC_SNB_DATA" \
  --data-dir store/sf1-topology \
  --relation all-topology
```

Import label-encoded SNB topology plus properties:

```bash
target/release/lsmgraph import \
  --input "$LDBC_SNB_DATA" \
  --data-dir "$LSMGRAPH_STORE" \
  --relation snb-full
```

Rebuild the persisted SNB adjacency cache:

```bash
target/release/lsmgraph snb-cache --data-dir "$LSMGRAPH_STORE"
```

## Server

Start the HTTP adapter on localhost:

```bash
target/release/lsmgraph snb-server \
  --data-dir "$LSMGRAPH_STORE" \
  --host 127.0.0.1 \
  --port 9090
```

Validation files and Java driver integrations are optional external assets. If you use them, pass explicit paths such as `--validation-params deps/ldbc_snb_interactive_impls/dgs/validation_params.csv`.

## License

Licensed under either the MIT license or Apache License 2.0, at your option.
