# LSMGraph Startup Guide

## Prerequisites

- Rust toolchain with Cargo.
- Linux for `direct-io` and `uring` feature checks.
- Optional LDBC SNB data prepared outside this repository.

## Build And Test

```bash
cargo test
cargo build --release
cargo build --release --features direct-io,uring
```

## Configure Local Paths

```bash
export LDBC_SNB_DATA=${LDBC_SNB_DATA:-data/social_network}
export LSMGRAPH_STORE=${LSMGRAPH_STORE:-store/sf1-full}
```

Both directories are local/user-provided. `store/` and `data/` are ignored by Git.

## Import Data

```bash
target/release/lsmgraph import \
  --input "$LDBC_SNB_DATA" \
  --data-dir "$LSMGRAPH_STORE" \
  --relation snb-full
```

For a smaller topology-only check:

```bash
target/release/lsmgraph import \
  --input "$LDBC_SNB_DATA" \
  --data-dir store/sf1-topology \
  --relation all-topology
```

## Run Server

```bash
target/release/lsmgraph snb-server \
  --data-dir "$LSMGRAPH_STORE" \
  --host 127.0.0.1 \
  --port 9090
```

## Optional Validation Assets

The public branch does not vendor LDBC Java driver trees or validation parameter files. If you keep them locally, pass explicit paths with CLI flags such as `--validation-params` and `--validation-splits`.
