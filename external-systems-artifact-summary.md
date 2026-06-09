# External Systems Artifact Summary for SemL0 Paper

## Overview
This document summarizes the investigation of four dynamic graph storage systems for potential comparison with SemL0 (lsmgraph-rs) in the paper.

## System Availability Summary

| System | Source | Build Status | Blocking Issues | LDBC SNB |
|--------|--------|--------------|----------------|----------|
| LiveGraph | thu-pacman/LiveGraph | ❌ Failed | Missing TBB | No |
| Teseo | cwida/teseo | ❌ Failed | Missing autotools, libnuma, libevent | Graphalytics only |
| GraphOne | the-data-lab/GraphOne | ⚠️ Partial | Missing TBB (linker error) | No |
| LLAMA | goatdb/llama | ✅ **SUCCESS** | None | No |

## Key Findings

### LLAMA - The Only Runnable System
Only **LLAMA** (goatdb/llama) successfully builds and produces runnable binaries:
- Binary: `/tmp/llama/bin/benchmark-memory`
- Available benchmarks: BFS, PageRank, SSSP, Triangle Counting, Betweenness Centrality
- Input format: SNAP edge-list or FGF binary format

### Missing Dependencies Summary
Three systems fail to build due to missing dependencies:

1. **LiveGraph** and **GraphOne**: Both require Intel TBB
   - Install with: `sudo apt-get install libtbb-dev`
   - Note: TBB package exists in apt cache but is not installed

2. **Teseo**: Requires multiple build tools
   - Install with: `sudo apt-get install autoconf automake libtool libnuma-dev libevent-dev`
   - Also needs newer compiler (gcc-10+ recommended, currently have gcc 9.4.0)

## LDBC SNB Compatibility

**None of the four systems natively support LDBC SNB.**

- **LiveGraph**: C++ library with custom API (Transaction, EdgeIterator)
- **Teseo**: C++ library, supports LDBC Graphalytics (BFS, PageRank, etc.) via separate GFE driver
- **GraphOne**: Storage engine with GraphView APIs (not a query engine)
- **LLAMA**: Custom benchmark suite with SNAP/FGF format loaders

## Comparison Recommendations

### For SemL0 Paper
Given the LDBC SNB focus of the SemL0 project, consider these alternatives:

1. **Continue with existing lsmgraph-rs LDBC SNB implementation** - this is the primary comparison baseline

2. **Add LLAMA as a secondary comparison** (if time permits):
   - ✅ Can build and run now
   - Provides graph analytics performance comparison
   - Limited to vertex-centric algorithms (BFS, PageRank, SSSP)

3. **Consider installing missing dependencies** to enable more systems:
   ```bash
   sudo apt-get install libtbb-dev autoconf automake libtool libnuma-dev libevent-dev
   ```

### Alternative Systems Mentioned in Literature
The HAL paper (VLDB 2024) also compares against these systems - their implementation at `https://gitlab.inria.fr/cedar/gfe-driver` may include integrated benchmarks.

## What Each System Could Compare Against

| System | Best Comparison Target | Metrics |
|--------|----------------------|---------|
| LiveGraph | SemL0 transaction throughput | Edge insert/scan latency, mixed workload throughput |
| Teseo | SemL0 update + analytics | Graph analysis time, update latency |
| GraphOne | SemL0 ingestion rate | Update throughput, batch analytics performance |
| LLAMA | SemL0 read performance | BFS/PageRank/SSSP execution time |

## Action Items

1. **Immediate**: Install TBB and other dependencies to enable LiveGraph, GraphOne, and Teseo builds
2. **Short-term**: Generate SNAP-format graph data for LLAMA benchmarks
3. **Medium-term**: Implement custom LDBC SNB-like workloads for comparison systems
4. **Long-term**: Consider integrating HAL's GFE driver for standardized comparisons
