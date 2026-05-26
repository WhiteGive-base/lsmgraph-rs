# lsmgraph-rs

LSMGraph prototype for reproducing the storage-engine core on the LDBC SNB SF1
data under `/data/WorkSpace/dgs/data/social_network_tugraph`.

This project intentionally does not read or depend on the existing DGS
`dgs_db`. DGS is used only as a schema and query-reference implementation.

## Implemented

- Tokio-based engine shell.
- `IoBackend` abstraction with bounded blocking `pread`/`pwrite`.
- MemGraph with active/frozen rotation, low-degree inline segments and
  high-degree `BTreeMap` overflow.
- CSR writer/reader with sparse per-source offsets and fixed binary edge
  bodies.
- Manifest replay for reopening a persisted store.
- L0 version chain and visible-neighbor merge.
- L0 to L1 compaction.
- Simple multi-level index for L1+ lookup.
- `person_knows` CSV import and validation against the raw CSV.

## Commands

```bash
cd /data/WorkSpace/lsmgraph-rs

# Build/test
/home/ydl/.cargo/bin/cargo test
/home/ydl/.cargo/bin/cargo build --release

# Optional I/O backends
/home/ydl/.cargo/bin/cargo build --release --features direct-io,uring

# Import SF1 person_knows into a fresh store
target/release/lsmgraph import \
  --input /data/WorkSpace/dgs/data/social_network_tugraph \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1 \
  --relation person_knows

# Or import all currently supported SNB topology relations
target/release/lsmgraph import \
  --input /data/WorkSpace/dgs/data/social_network_tugraph \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-topology \
  --relation all-topology

# Import label-encoded SNB topology plus vertex/edge properties for IC validation
target/release/lsmgraph import \
  --input /data/WorkSpace/dgs/data/social_network_tugraph \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full \
  --relation snb-full

# Validate all person_knows adjacency lists against the raw CSV
target/release/lsmgraph validate-knows \
  --input /data/WorkSpace/dgs/data/social_network_tugraph \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1 \
  --max-vertices 100000

# Scan current snapshot
target/release/lsmgraph scan \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1

# Compact L0 into L1 and validate again
target/release/lsmgraph compact \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1

target/release/lsmgraph neighbors \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1 \
  --src 933

# Validate the migrated IC1/IC2 adapter against LDBC validation_params.csv.
# The validator builds an in-memory adjacency cache from CSR on startup.
target/release/lsmgraph snb-validate \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full \
  --validation-params /data/WorkSpace/dgs/deps/ldbc_snb_interactive_impls/dgs/validation_params.csv \
  --max-lines 100

# Rebuild the persisted SNB adjacency cache for an existing snb-full store
target/release/lsmgraph snb-cache \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-full

# Select a storage I/O backend. blocking is the default.
target/release/lsmgraph --io-backend direct scan \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-direct-smoke

target/release/lsmgraph --io-backend uring scan \
  --data-dir /data/WorkSpace/lsmgraph-rs/store/sf1-uring-smoke
```

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
- `snb-validate --max-lines 100` checked `100` IC1/IC2 rows and passed `100`.
- Persisted SNB adjacency cache: `snb_adjacency.bin`, `11330230` groups, about `611M`.
- With the persisted cache, `snb-validate --max-lines 100` passed `100/100` in about `31s`.

Observed I/O backend smoke results:

- `--io-backend direct` imported and validated SF1 `person_knows`: `361246/361246`.
- `--io-backend uring` imported SF1 `person_knows`; scan returned `361246`.
- `uring` currently creates a ring per operation for correctness testing. Full random-neighbor validation is much slower than the blocking backend until a persistent ring worker is added.

## Next Work

- Migrate IC3-IC14 over the LSMGraph SNB adapter.
- mmap the SNB adjacency cache to reduce validator startup memory copy cost.
- Replace the functional `uring` backend with persistent ring workers and batched submissions.
