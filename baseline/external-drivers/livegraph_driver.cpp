// LiveGraph external-baseline driver for the SemL0 neighbor-scan workload.
//
// Consumes a dense edge list (produced by convert_ldbc_edges.py from
// `lsmgraph scan --dump-edges`), loads it into LiveGraph, then samples K sources
// per edge type and times get_edges(src,label) full scans. Outputs JSON with
// load time, per-edge-type latency percentiles, and peak RSS so it can sit next
// to the lsmgraph storage-bench numbers (latency + memory, NOT LSM "read bytes").
//
// Edge-list text format (1 header line then triples):
//   <vertex_count>
//   <src_dense> <etype> <dst_dense>
//   ...
// vertex ids are dense [0, vertex_count); etype is the lsmgraph numbering.
//
// Build: see Makefile (links deps/LiveGraph/build/liblivegraph.so).

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <map>
#include <random>
#include <sstream>
#include <string>
#include <vector>

#include "livegraph.hpp" // deps/LiveGraph/bind/livegraph.hpp

using clk = std::chrono::steady_clock;

static long peak_rss_kb() {
    std::ifstream st("/proc/self/status");
    std::string line;
    while (std::getline(st, line)) {
        if (line.rfind("VmHWM:", 0) == 0) {
            long kb = 0;
            std::sscanf(line.c_str(), "VmHWM: %ld", &kb);
            return kb;
        }
    }
    return -1;
}

int main(int argc, char** argv) {
    std::string edges_path, block_path = "/tmp/lg-driver-block", wal_path = "/tmp/lg-driver-wal";
    std::string out_path = "-";
    std::vector<int> want_etypes;
    int samples = 5000;
    uint64_t seed = 42;
    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        auto next = [&]() { return std::string(argv[++i]); };
        if (a == "--edges") edges_path = next();
        else if (a == "--block-path") block_path = next();
        else if (a == "--wal-path") wal_path = next();
        else if (a == "--samples") samples = std::stoi(next());
        else if (a == "--seed") seed = std::stoull(next());
        else if (a == "--output") out_path = next();
        else if (a == "--edge-types") {
            std::stringstream ss(next()); std::string tok;
            while (std::getline(ss, tok, ',')) if (!tok.empty()) want_etypes.push_back(std::stoi(tok));
        } else { std::fprintf(stderr, "unknown arg %s\n", a.c_str()); return 2; }
    }
    if (edges_path.empty()) { std::fprintf(stderr, "need --edges <file>\n"); return 2; }

    // --- read edge list ---
    std::ifstream in(edges_path);
    if (!in) { std::fprintf(stderr, "cannot open %s\n", edges_path.c_str()); return 2; }
    uint64_t vcount = 0;
    in >> vcount;
    std::vector<std::tuple<uint64_t, uint16_t, uint64_t>> edges;
    edges.reserve(1 << 20);
    std::map<int, std::vector<uint64_t>> srcs_by_etype; // etype -> sources seen
    uint64_t s, d; long et;
    while (in >> s >> et >> d) {
        edges.emplace_back(s, (uint16_t)et, d);
        srcs_by_etype[(int)et].push_back(s);
    }
    std::fprintf(stderr, "loaded edge list: vcount=%lu edges=%zu\n", vcount, edges.size());

    // --- load into LiveGraph ---
    lg::Graph g(block_path, wal_path);
    auto t0 = clk::now();
    {
        auto loader = g.begin_batch_loader();
        for (uint64_t i = 0; i < vcount; i++) loader.new_vertex();
        for (auto& e : edges)
            loader.put_edge(std::get<0>(e), (lg::label_t)std::get<1>(e), std::get<2>(e), std::string_view());
        loader.commit();
    }
    double load_s = std::chrono::duration<double>(clk::now() - t0).count();
    std::fprintf(stderr, "loaded into LiveGraph in %.1fs\n", load_s);

    if (want_etypes.empty())
        for (auto& kv : srcs_by_etype) want_etypes.push_back(kv.first);

    // --- sampled neighbor-scan benchmark ---
    std::mt19937_64 rng(seed);
    std::ostringstream js;
    js << "{\n  \"system\": \"livegraph\",\n  \"edges_path\": \"" << edges_path << "\",\n";
    js << "  \"vertex_count\": " << vcount << ",\n  \"edge_count\": " << edges.size() << ",\n";
    js << "  \"load_s\": " << load_s << ",\n  \"samples_per_edge_type\": " << samples << ",\n";
    js << "  \"benchmarks\": [\n";
    bool first = true;
    for (int et : want_etypes) {
        auto it = srcs_by_etype.find(et);
        if (it == srcs_by_etype.end() || it->second.empty()) continue;
        auto& pool = it->second;
        std::vector<double> lat_us;
        uint64_t total_nbrs = 0;
        auto r = g.begin_read_only_transaction();
        int n = std::min<int>(samples, (int)pool.size());
        for (int k = 0; k < n; k++) {
            uint64_t src = pool[rng() % pool.size()];
            auto q0 = clk::now();
            auto eit = r.get_edges(src, (lg::label_t)et);
            uint64_t cnt = 0;
            while (eit.valid()) { (void)eit.dst_id(); ++cnt; eit.next(); }
            lat_us.push_back(std::chrono::duration<double, std::micro>(clk::now() - q0).count());
            total_nbrs += cnt;
        }
        std::sort(lat_us.begin(), lat_us.end());
        auto pct = [&](double p) { return lat_us.empty() ? 0.0 : lat_us[std::min<size_t>(lat_us.size() - 1, (size_t)(p * lat_us.size()))]; };
        double avg = 0; for (double x : lat_us) avg += x; if (!lat_us.empty()) avg /= lat_us.size();
        if (!first) js << ",\n"; first = false;
        js << "    {\"edge_type\": " << et << ", \"ops\": " << lat_us.size()
           << ", \"total_neighbors\": " << total_nbrs
           << ", \"avg_us\": " << avg << ", \"p50_us\": " << pct(0.50)
           << ", \"p90_us\": " << pct(0.90) << ", \"p99_us\": " << pct(0.99) << "}";
    }
    js << "\n  ],\n  \"peak_rss_kb\": " << peak_rss_kb() << "\n}\n";

    if (out_path == "-") std::fputs(js.str().c_str(), stdout);
    else { std::ofstream o(out_path); o << js.str(); }
    return 0;
}
