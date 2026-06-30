
# LSMGraph Architecture

This document describes the public code package at a subsystem level. It deliberately avoids experiment-only paths and private deployment assumptions.

## Core Layers

- `MemGraph`: mutable in-memory graph state with active/frozen rotation and low-degree inline storage.
- `Delta` and version chain logic: preserves visible-neighbor semantics across inserts, deletes, and compaction.
- `CSR`: immutable segment format with sparse source offsets, binary edge bodies, and semantic metadata.
- `Levels`: L0/L1+ organization and lookup dispatch across mutable and immutable components.
- `Schema` and `PropertyEncoding`: versioned schema metadata and property decoding boundaries.
- `SNB`: import, query, and adapter code for LDBC SNB-style workloads.

## Data Flow

1. Loaders parse external graph data from user-provided paths.
2. The engine writes mutable updates into MemGraph and rotates frozen state when configured limits are reached.
3. Compaction converts frozen/L0 state into immutable CSR segments.
4. Query paths merge MemGraph, L0, and L1+ results while applying schema and semantic pruning metadata.
5. Optional HTTP adapter exposes SNB-compatible query endpoints for external validation harnesses.

## Storage Boundaries

Generated stores belong under a user-selected directory such as `store/sf1-full`; this repository does not track generated data. External datasets and validation drivers are supplied by the user and referenced by CLI flags.

## Feature Flags

- Default build: portable blocking I/O backend.
- `direct-io`: Linux direct I/O support through `libc`.
- `uring`: Linux `io-uring` backend.

## Verification

Use `cargo test` for correctness regressions and `cargo build --release --features direct-io,uring` on Linux to verify optional I/O backends.
