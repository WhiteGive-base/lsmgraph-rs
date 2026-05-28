# lsmgraph-rs

LSMGraph prototype for reproducing the storage-engine core on the LDBC SNB
SF1 data under `/data/WorkSpace/dgs/data/social_network_tugraph`.

This project intentionally does not read or depend on the existing DGS
`dgs_db`. DGS is used only as a schema, query-reference implementation and
LDBC validation harness.

## Implemented

- Tokio-based engine shell.
- `IoBackend` abstraction with bounded blocking `pread`/`pwrite`.
- Optional `direct-io` and functional `uring` I/O backends.
- MemGraph with active/frozen rotation, low-degree inline segments and
  high-degree `BTreeMap` overflow.
- CSR writer/reader with sparse per-source offsets and fixed binary edge
  bodies.
- Manifest replay for reopening a persisted store.
- L0 version chain and visible-neighbor merge.
- L0 to L1 compaction.
- Simple multi-level index for L1+ lookup.
- SNB full topology/property import for IC1-IC14 validation.
- Persisted SNB adjacency cache.
- DGS-compatible HTTP adapter for the copied LDBC Java driver.
- Mixed IC/IS/IU validation against `validation_params_tugraph.csv`.

## Vendored LDBC Assets

The upstream implementation tree is copied unchanged under:

```text
/data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls
```

LSMGraph-specific wrappers live in:

```text
/data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/lsmgraph
```

Current adapter scope is IC1-IC14. The copied IS1-IS7 and IU1-IU8 validation
files are present unchanged. IS1-IS7 and IU1-IU8 are enabled for the mixed
Tugraph validation flow and the HTTP adapter.

## Server Setup

Run these on the Linux server:

```bash
cd /data/WorkSpace/lsmgraph-rs
export PATH="$HOME/.cargo/bin:$PATH"
```

Build and test:

```bash
cargo test
cargo build --release --features direct-io,uring
```

Start the DGS-compatible LSMGraph HTTP adapter. The wrapper defaults to
`127.0.0.1:9090`, matching the copied Java driver validation properties.

```bash
deps/ldbc_snb_interactive_impls/lsmgraph/start_server.sh
```

Equivalent explicit command:

```bash
target/release/lsmgraph snb-server \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full \
  --host 127.0.0.1 \
  --port 9090
```

## Import Commands

Import SF1 `person_knows` into a fresh store:

```bash
target/release/lsmgraph import \
  --input /data/WorkSpace/dgs/data/social_network_tugraph \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1 \
  --relation person_knows
```

Import all currently supported SNB topology relations:

```bash
target/release/lsmgraph import \
  --input /data/WorkSpace/dgs/data/social_network_tugraph \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-topology \
  --relation all-topology
```

Import label-encoded SNB topology plus vertex/edge properties for IC validation:

```bash
target/release/lsmgraph import \
  --input /data/WorkSpace/dgs/data/social_network_tugraph \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full \
  --relation snb-full
```

Rebuild the persisted SNB adjacency cache for an existing `snb-full` store:

```bash
target/release/lsmgraph snb-cache \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full
```

## Java Driver Validation

The final LDBC validation path is the copied Java driver under
`deps/ldbc_snb_interactive_impls/dgs`. LSMGraph only starts the HTTP adapter
and exposes the port; the DGS kernel is not used.

Prepare a clean initial store for mixed IC/IS/IU validation:

```bash
FORCE=1 deps/ldbc_snb_interactive_impls/lsmgraph/prepare_validation_store.sh
```

Start LSMGraph on the standard Java driver port:

```bash
DATA_DIR=/data/WorkSpace/lsmgraph-rs/store/sf1-full-validation \
  deps/ldbc_snb_interactive_impls/lsmgraph/start_server.sh
```

Run the copied Java LDBC driver on `validation_params_tugraph.csv`:

```bash
MAX_LINES=100 deps/ldbc_snb_interactive_impls/lsmgraph/run_driver_validate_mixed.sh
```

Run copied Java driver single-query validations when needed:

```bash
QUERIES=ic2 deps/ldbc_snb_interactive_impls/lsmgraph/run_driver_validate_ic.sh
QUERIES=is1 deps/ldbc_snb_interactive_impls/lsmgraph/run_driver_validate_ic.sh
QUERIES=iu5 deps/ldbc_snb_interactive_impls/lsmgraph/run_driver_validate_ic.sh
```

## Internal Debug Commands

Storage smoke checks:

```bash
target/release/lsmgraph scan \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full

target/release/lsmgraph stats \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full
```

Validate all `person_knows` adjacency lists against the raw CSV:

```bash
target/release/lsmgraph validate-knows \
  --input /data/WorkSpace/dgs/data/social_network_tugraph \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1 \
  --max-vertices 100000
```

Validate against legacy combined `validation_params.csv`:

```bash
target/release/lsmgraph snb-validate \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full \
  --validation-params /data/WorkSpace/dgs/deps/ldbc_snb_interactive_impls/dgs/validation_params.csv \
  --max-lines 500
```

Validate directly against copied official split files:

```bash
deps/ldbc_snb_interactive_impls/lsmgraph/validate_splits.sh
```

Useful split-validation overrides:

```bash
QUERIES=ic1,ic2,ic14 MAX_LINES_PER_QUERY=100 \
  deps/ldbc_snb_interactive_impls/lsmgraph/validate_splits.sh

MAX_LINES_PER_QUERY=0 \
  deps/ldbc_snb_interactive_impls/lsmgraph/validate_splits.sh
```

`MAX_LINES_PER_QUERY=0` means all rows in each selected split file.

The Rust validators below are internal debugging helpers, not the final LDBC
validation path.

Validate the copied Tugraph mixed IC/IS/IU file directly:

```bash
target/release/lsmgraph snb-validate-mixed \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full-validation \
  --validation-params /data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/dgs/validation_params_tugraph.csv \
  --max-lines 500
```

## Performance Commands

Query validation wall time and memory:

```bash
/usr/bin/time -v target/release/lsmgraph snb-validate \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full \
  --validation-params /data/WorkSpace/dgs/deps/ldbc_snb_interactive_impls/dgs/validation_params.csv \
  --max-lines 500
```

Storage scan throughput smoke:

```bash
/usr/bin/time -v target/release/lsmgraph scan \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full
```

Blocking/direct/io_uring backend smoke comparisons:

```bash
/usr/bin/time -v target/release/lsmgraph --io-backend blocking scan \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full

/usr/bin/time -v target/release/lsmgraph --io-backend direct scan \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full

/usr/bin/time -v target/release/lsmgraph --io-backend uring scan \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full
```

## Observed SF1 Results

Expected SF1 `person_knows` result:

- CSV data rows: `180623`
- Directed records after bidirectional import: `361246`
- Vertices with at least one knows edge in this file: `9163`

Observed SF1 `all-topology` smoke result:

- Input rows visited across supported CSV sources: `19308214`
- Directed topology edge records: `17436661`
- Default 64 MiB MemGraph produced `9` L0 CSR files before compaction.

Observed SF1 `snb-full` result:

- Directed topology edge records including reverse lookup edges: `34692699`
- Default 64 MiB MemGraph produced `17` L0 CSR files before compaction.
- Persisted SNB adjacency cache: `snb_adjacency.bin`, `11330230` groups, about `611M`.
- `snb-validate --max-lines 500` passed `500/500` for IC1-IC14.
- Direct split validation checked IC1-IC14 first `100` rows each and passed
  `1400/1400`.
- Mixed Tugraph validation checked the first `500` rows from
  `validation_params_tugraph.csv`: `476` reads passed, `24` updates applied,
  `0` failures.
- Copied Java LDBC driver mixed validation checked the first `100` rows from
  `validation_params_tugraph.csv`: `23` operation types executed, `0` crashed,
  `0` incorrect, `BUILD SUCCESS`.
- Java LDBC driver validation against `snb-server` processed IC2 full split:
  `1428` operations, `0` incorrect.

Observed I/O backend smoke results:

- `--io-backend direct` imported and validated SF1 `person_knows`: `361246/361246`.
- `--io-backend uring` imported SF1 `person_knows`; scan returned `361246`.
- `uring` currently creates a ring per operation for correctness testing. Full
  random-neighbor validation is much slower than the blocking backend until a
  persistent ring worker is added.

## Next Work

- Split the large SNB query module into `ic`, `is`, `iu`, `validation` and
  `http_adapter` modules.
- Run the Java LDBC driver against the mixed IC/IS/IU validation properties.
- mmap the SNB adjacency cache to reduce validator startup memory copy cost.
- Replace the functional `uring` backend with persistent ring workers and
  batched submissions.


FORCE=1 INPUT=/data/WorkSpace/dgs/data/social_network_tugraph DATA_DIR=/data/WorkSpace/lsmgraph-rs/store/sf1-bench bash /data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/lsmgraph/prepare_validation_store.sh > /data/WorkSpace/lsmgraph-rs/logs/prepare-sf1-bench.log 2>&1 &

FORCE=1 INPUT=/data/WorkSpace/ldbc-sf10/social_network DATA_DIR=/data/WorkSpace/lsmgraph-rs/store/sf10-bench bash /data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/lsmgraph/prepare_validation_store.sh > /data/WorkSpace/lsmgraph-rs/logs/prepare-sf10-bench.log 2>&1 &

FORCE=1 INPUT=/data/WorkSpace/ldbc-sf30/social_network DATA_DIR=/data/WorkSpace/lsmgraph-rs/store/sf30-bench bash /data/WorkSpace/lsmgraph-rs/deps/ldbc_snb_interactive_impls/lsmgraph/prepare_validation_store.sh > /data/WorkSpace/lsmgraph-rs/logs/prepare-sf30-bench.log 2>&1 &