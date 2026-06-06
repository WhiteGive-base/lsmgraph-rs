# System: Teseo

- **version/source**: https://github.com/cwida/teseo (VLDB 2021)
- **build status**: Failed - missing build tools (autoreconf, autotools)
- **build commands**:
  ```bash
  ./autoreconf -iv
  mkdir build && cd build
  ../configure --enable-optimize --disable-debug
  make -j
  ```
- **benchmark command**: No standard benchmark suite in this repo. The paper experiments use a separate GFE driver at https://github.com/cwida/gfe_driver
- **LDBC SNB support**: No - supports LDBC Graphalytics benchmark (vertex-centric algorithms like BFS, PageRank, etc.)
- **blocking issue**: 
  - Missing `autoreconf` / autotools (./autoreconf: No such file or directory)
  - Would also need: libnuma, libevent 2.1.1+
  - gcc-10 or clang-10 recommended (currently have gcc 9.4.0)
- **what comparison was replaced by**: None - Teseo is a relevant comparison for dynamic graph analysis
- **measured metrics (if any)**: Graph analysis throughput, update latency, memory usage
- **notes**:
  - Teseo is a C++ library for dynamic graph analysis with full transactional support
  - Supports undirected weighted graphs with 64-bit vertex IDs
  - Uses sparse arrays and fat trees as underlying data structure
  - Databases cannot be serialized - must be reinitialized on restart
  - VLDB 2021 paper: "Teseo and the Analysis of Structural Dynamic Graphs"
  - Separate GFE driver (https://github.com/cwida/gfe_driver) required for paper experiments
  - To enable build: `sudo apt-get install autoconf automake libtool libnuma-dev libevent-dev`
