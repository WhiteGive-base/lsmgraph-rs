# System: LiveGraph

- **version/source**: https://github.com/thu-pacman/LiveGraph (VLDB 2020)
- **build status**: Failed - missing TBB library
- **build commands**: 
  ```bash
  mkdir build && cd build
  cmake ..
  make
  ```
- **benchmark command**: No standard benchmark suite provided; requires custom application using the API
- **LDBC SNB support**: No
- **blocking issue**: Intel TBB (Threading Building Blocks) is required but not installed. The system has `apt-cache show libtbb-dev` available but not installed. Installing with `apt-get install libtbb-dev` may resolve this.
- **what comparison was replaced by**: None - LiveGraph could be a direct comparison for transactional graph workloads
- **measured metrics (if any)**: Transaction throughput, edge scan latency, throughput for mixed read/write workloads
- **notes**: 
  - LiveGraph is a C++ library providing transactional graph storage with purely sequential adjacency list scans
  - API provides Transaction, EdgeIterator classes for vertex/edge operations
  - VLDB 2020 paper: "LiveGraph: a transactional graph storage system with purely sequential adjacency list scans"
  - Key differentiating feature: ensures adjacency list scans are purely sequential even with concurrent transactions
  - To enable build: `sudo apt-get install libtbb-dev`
