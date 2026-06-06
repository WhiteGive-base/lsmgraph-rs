# System: GraphOne

- **version/source**: https://github.com/the-data-lab/GraphOne (FAST 2019)
- **build status**: Partial build success, linking failed - missing TBB library
- **build commands**:
  ```bash
  mkdir build && cd build
  cmake ..
  make
  ```
- **benchmark command**: Built-in test executables with various job numbers (-j flag):
  ```bash
  ./graphone64 -i <input_dir> -v <vertex_count> -j <job_number>
  # Jobs: 0=BFS, 1=logging only, 2=archiving, 3=mixed edge list/adjacency, etc.
  ```
- **LDBC SNB support**: No - this is a storage engine, not a query engine
- **blocking issue**: Intel TBB library not installed (`cannot find -ltbb` linker error)
- **what comparison was replaced by**: None - GraphOne is a relevant comparison for dynamic graph storage with real-time analytics
- **measured metrics (if any)**: 
  - Data ingestion rate
  - Batch analytics performance (BFS, PageRank)
  - Stream analytics performance
  - Memory usage
- **notes**:
  - GraphOne is a graph data store (storage engine), not a full graph database
  - Provides GraphView APIs for data access (Static View, Stream View, Historical Snapshot View)
  - Supports concurrent batch and stream analytics on the same data store
  - Hybrid storage: edge log + adjacency store with dual versioning
  - FAST 2019 paper: "GraphOne: A Data Store for Real-time Analytics on Evolving Graphs"
  - No LDBC SNB integration possible - requires using own GraphView APIs
  - To enable full build: `sudo apt-get install libtbb-dev`
