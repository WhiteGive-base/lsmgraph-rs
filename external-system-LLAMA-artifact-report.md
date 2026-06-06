# System: LLAMA (goatdb/llama)

- **version/source**: https://github.com/goatdb/llama (ICDE 2015)
- **build status**: **SUCCEEDED**
- **build commands**:
  ```bash
  cd llama
  make benchmark-memory   # in-memory version
  make benchmark-persistent   # persistent version
  make benchmark-slcsr   # single-level CSR (baseline)
  ```
- **benchmark command**: 
  ```bash
  ./bin/benchmark-memory [OPTIONS] INPUT_FILE
  ./bin/benchmark-memory -r <task> -t <threads> INPUT_FILE
  ```
  Available tasks: bfs_count, pagerank, sssp, triangle_counting, bc_adj, bc_random, degree_distribution, etc.
- **LDBC SNB support**: No - uses custom SNAP edge-list format or FGF format
- **blocking issue**: None - successfully built and binary available
- **what comparison was replaced by**: None - LLAMA is a viable comparison for graph analytics workloads
- **measured metrics (if any)**:
  - BFS execution time
  - PageRank iteration time
  - SSSP path computation time
  - Triangle counting time
  - Memory usage
  - Update throughput
- **notes**:
  - LLAMA = "Large Multiversioned Arrays" - a graph storage and analysis system
  - Built on CSR representation with multi-versioning for mutability
  - Supports out-of-memory execution (persistent version)
  - ICDE 2015 paper: "LLAMA: Efficient Graph Analytics Using Large Multiversioned Arrays"
  - Binary location: `/tmp/llama/bin/benchmark-memory`
  - Input format: SNAP edge-list or FGF binary format
  - No LDBC SNB integration - uses custom data loaders
  - Performance characteristics: 3-18% overhead vs immutable CSR for in-memory; outperforms out-of-memory systems
