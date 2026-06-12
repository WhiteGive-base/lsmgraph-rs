// Streaming LiveGraph external-baseline driver for large SemL0 edge lists.
//
// Consumes the same dense edge-list format as livegraph_driver.cpp, but does
// not keep all edges or all per-type source pools in memory. It streams edges
// into LiveGraph and keeps a bounded reservoir of sampled sources per edge
// type for the neighbor-scan benchmark.

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <map>
#include <random>
#include <sstream>
#include <string>
#include <vector>

#include "livegraph.hpp"

using clk = std::chrono::steady_clock;

struct Reservoir {
    uint64_t seen = 0;
    std::vector<uint64_t> srcs;
};

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

static void reservoir_add(Reservoir& reservoir, uint64_t src, int samples, std::mt19937_64& rng) {
    reservoir.seen++;
    if ((int)reservoir.srcs.size() < samples) {
        reservoir.srcs.push_back(src);
        return;
    }
    if (samples <= 0) return;
    std::uniform_int_distribution<uint64_t> dist(0, reservoir.seen - 1);
    uint64_t idx = dist(rng);
    if (idx < (uint64_t)samples) reservoir.srcs[(size_t)idx] = src;
}

int main(int argc, char** argv) {
    std::string edges_path, block_path = "/tmp/lg-driver-block", wal_path = "/tmp/lg-driver-wal";
    std::string out_path = "-";
    std::vector<int> want_etypes;
    int samples = 5000;
    uint64_t seed = 42;

    for (int i = 1; i < argc; i++) {
        std::string arg = argv[i];
        auto next = [&]() -> std::string {
            if (++i >= argc) {
                std::fprintf(stderr, "missing value after %s\n", arg.c_str());
                std::exit(2);
            }
            return std::string(argv[i]);
        };
        if (arg == "--edges") edges_path = next();
        else if (arg == "--block-path") block_path = next();
        else if (arg == "--wal-path") wal_path = next();
        else if (arg == "--samples") samples = std::stoi(next());
        else if (arg == "--seed") seed = std::stoull(next());
        else if (arg == "--output") out_path = next();
        else if (arg == "--edge-types") {
            std::stringstream ss(next());
            std::string tok;
            while (std::getline(ss, tok, ',')) {
                if (!tok.empty()) want_etypes.push_back(std::stoi(tok));
            }
        } else {
            std::fprintf(stderr, "unknown arg %s\n", arg.c_str());
            return 2;
        }
    }
    if (edges_path.empty()) {
        std::fprintf(stderr, "need --edges <file>\n");
        return 2;
    }
    if (samples < 0) {
        std::fprintf(stderr, "--samples must be >= 0\n");
        return 2;
    }

    std::ifstream in(edges_path);
    if (!in) {
        std::fprintf(stderr, "cannot open %s\n", edges_path.c_str());
        return 2;
    }

    uint64_t vcount = 0;
    in >> vcount;
    if (!in) {
        std::fprintf(stderr, "cannot read vertex count from %s\n", edges_path.c_str());
        return 2;
    }

    std::mt19937_64 reservoir_rng(seed);
    std::map<int, Reservoir> reservoirs;
    uint64_t edge_count = 0;

    lg::Graph graph(block_path, wal_path);
    auto t0 = clk::now();
    {
        auto loader = graph.begin_batch_loader();
        for (uint64_t i = 0; i < vcount; i++) loader.new_vertex();

        uint64_t src = 0, dst = 0;
        long edge_type = 0;
        while (in >> src >> edge_type >> dst) {
            loader.put_edge(src, (lg::label_t)edge_type, dst, std::string_view());
            reservoir_add(reservoirs[(int)edge_type], src, samples, reservoir_rng);
            edge_count++;
        }
        if (!in.eof()) {
            std::fprintf(stderr, "failed while reading edge triples from %s\n", edges_path.c_str());
            return 2;
        }
        loader.commit();
    }
    double load_s = std::chrono::duration<double>(clk::now() - t0).count();
    std::fprintf(stderr, "stream-loaded edge list: vcount=%lu edges=%lu load_s=%.1f\n", vcount, edge_count, load_s);

    if (want_etypes.empty()) {
        for (const auto& kv : reservoirs) want_etypes.push_back(kv.first);
    }

    std::ostringstream js;
    js << "{\n  \"system\": \"livegraph-stream\",\n  \"edges_path\": \"" << edges_path << "\",\n";
    js << "  \"vertex_count\": " << vcount << ",\n";
    js << "  \"edge_count\": " << edge_count << ",\n";
    js << "  \"load_s\": " << load_s << ",\n";
    js << "  \"samples_per_edge_type\": " << samples << ",\n";
    js << "  \"benchmarks\": [\n";

    std::mt19937_64 bench_rng(seed);
    bool first = true;
    for (int et : want_etypes) {
        auto found = reservoirs.find(et);
        if (found == reservoirs.end() || found->second.srcs.empty()) continue;
        const auto& pool = found->second.srcs;
        std::vector<double> lat_us;
        lat_us.reserve(pool.size());
        uint64_t total_neighbors = 0;
        auto txn = graph.begin_read_only_transaction();
        std::uniform_int_distribution<size_t> dist(0, pool.size() - 1);
        int ops = std::min<int>(samples, (int)pool.size());
        for (int i = 0; i < ops; i++) {
            uint64_t src = pool[dist(bench_rng)];
            auto q0 = clk::now();
            auto iter = txn.get_edges(src, (lg::label_t)et);
            uint64_t count = 0;
            while (iter.valid()) {
                (void)iter.dst_id();
                count++;
                iter.next();
            }
            lat_us.push_back(std::chrono::duration<double, std::micro>(clk::now() - q0).count());
            total_neighbors += count;
        }
        std::sort(lat_us.begin(), lat_us.end());
        auto pct = [&](double p) {
            if (lat_us.empty()) return 0.0;
            size_t idx = std::min<size_t>(lat_us.size() - 1, (size_t)(p * lat_us.size()));
            return lat_us[idx];
        };
        double avg = 0.0;
        for (double value : lat_us) avg += value;
        if (!lat_us.empty()) avg /= lat_us.size();

        if (!first) js << ",\n";
        first = false;
        js << "    {\"edge_type\": " << et
           << ", \"ops\": " << lat_us.size()
           << ", \"sample_pool\": " << pool.size()
           << ", \"stream_seen\": " << found->second.seen
           << ", \"total_neighbors\": " << total_neighbors
           << ", \"avg_us\": " << avg
           << ", \"p50_us\": " << pct(0.50)
           << ", \"p90_us\": " << pct(0.90)
           << ", \"p99_us\": " << pct(0.99)
           << "}";
    }

    js << "\n  ],\n  \"peak_rss_kb\": " << peak_rss_kb() << "\n}\n";

    if (out_path == "-") {
        std::fputs(js.str().c_str(), stdout);
    } else {
        std::ofstream out(out_path);
        if (!out) {
            std::fprintf(stderr, "cannot write %s\n", out_path.c_str());
            return 2;
        }
        out << js.str();
    }
    return 0;
}
