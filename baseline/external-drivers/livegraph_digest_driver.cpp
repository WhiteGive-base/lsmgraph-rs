// LiveGraph digest-correctness verifier for the SemL0 external baseline.
//
// The benchmark driver reports aggregate neighbor-scan latency. This verifier
// adds the missing correctness gate: it samples typed-neighbor queries with a
// fixed seed, computes ground-truth count/hash digests from the dense edge list,
// then compares them against LiveGraph get_edges(src,label).
//
// Dense edge-list text format:
//   <vertex_count>
//   <src_dense> <etype> <dst_dense>
//   ...

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
#include <string_view>
#include <unordered_map>
#include <vector>

#include "livegraph.hpp"

using clk = std::chrono::steady_clock;

struct Reservoir {
    uint64_t seen = 0;
    std::vector<uint64_t> srcs;
};

struct QueryKey {
    int edge_type = 0;
    uint64_t src = 0;

    bool operator==(const QueryKey& other) const {
        return edge_type == other.edge_type && src == other.src;
    }
};

struct QueryKeyHash {
    size_t operator()(const QueryKey& key) const {
        uint64_t x = (uint64_t)(uint32_t)key.edge_type;
        x ^= key.src + 0x9e3779b97f4a7c15ULL + (x << 6) + (x >> 2);
        x ^= x >> 30;
        x *= 0xbf58476d1ce4e5b9ULL;
        x ^= x >> 27;
        x *= 0x94d049bb133111ebULL;
        x ^= x >> 31;
        return (size_t)x;
    }
};

struct Digest {
    uint64_t count = 0;
    uint64_t sum_hash = 0;
    uint64_t xor_hash = 0;
};

struct EdgeTypeStats {
    uint64_t sampled_queries = 0;
    uint64_t unique_queries = 0;
    uint64_t checked = 0;
    uint64_t mismatches = 0;
    uint64_t truth_neighbors = 0;
    uint64_t livegraph_neighbors = 0;
};

static uint64_t mix64(uint64_t x) {
    x += 0x9e3779b97f4a7c15ULL;
    x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ULL;
    x = (x ^ (x >> 27)) * 0x94d049bb133111ebULL;
    return x ^ (x >> 31);
}

static void digest_add(Digest& digest, uint64_t dst) {
    uint64_t h1 = mix64(dst);
    uint64_t h2 = mix64(dst ^ 0xd6e8feb86659fd93ULL);
    digest.count++;
    digest.sum_hash += h1;
    digest.xor_hash ^= h2;
}

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

static void json_escape(std::ostream& out, const std::string& s) {
    for (char c : s) {
        if (c == '"' || c == '\\') out << '\\' << c;
        else if (c == '\n') out << "\\n";
        else out << c;
    }
}

int main(int argc, char** argv) {
    std::string edges_path;
    std::string block_path = "/tmp/lg-digest-block";
    std::string wal_path = "/tmp/lg-digest-wal";
    std::string out_path = "-";
    std::vector<int> want_etypes;
    int samples = 1000;
    int max_mismatches = 20;
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
        else if (arg == "--max-mismatches") max_mismatches = std::stoi(next());
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

    std::map<int, bool> wanted;
    for (int et : want_etypes) wanted[et] = true;

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

    std::mt19937_64 sample_rng(seed);
    std::map<int, Reservoir> reservoirs;
    uint64_t edge_count = 0;

    lg::Graph graph(block_path, wal_path);
    auto load_start = clk::now();
    {
        auto loader = graph.begin_batch_loader();
        for (uint64_t i = 0; i < vcount; i++) loader.new_vertex();

        uint64_t src = 0, dst = 0;
        long edge_type_long = 0;
        while (in >> src >> edge_type_long >> dst) {
            int edge_type = (int)edge_type_long;
            loader.put_edge(src, (lg::label_t)edge_type, dst, std::string_view());
            if (wanted.empty() || wanted.count(edge_type)) {
                reservoir_add(reservoirs[edge_type], src, samples, sample_rng);
            }
            edge_count++;
        }
        if (!in.eof()) {
            std::fprintf(stderr, "failed while reading edge triples from %s\n", edges_path.c_str());
            return 2;
        }
        loader.commit();
    }
    double load_s = std::chrono::duration<double>(clk::now() - load_start).count();
    std::fprintf(stderr, "loaded LiveGraph: vcount=%lu edges=%lu load_s=%.3f\n", vcount, edge_count, load_s);

    std::vector<QueryKey> queries;
    std::unordered_map<QueryKey, Digest, QueryKeyHash> truth;
    std::map<int, EdgeTypeStats> stats;
    for (const auto& kv : reservoirs) {
        int edge_type = kv.first;
        for (uint64_t src : kv.second.srcs) {
            QueryKey key{edge_type, src};
            queries.push_back(key);
            truth.emplace(key, Digest{});
            stats[edge_type].sampled_queries++;
        }
    }
    for (const auto& kv : truth) {
        stats[kv.first.edge_type].unique_queries++;
    }

    auto truth_start = clk::now();
    std::ifstream truth_in(edges_path);
    if (!truth_in) {
        std::fprintf(stderr, "cannot reopen %s\n", edges_path.c_str());
        return 2;
    }
    uint64_t header = 0;
    truth_in >> header;
    uint64_t src = 0, dst = 0;
    long edge_type_long = 0;
    while (truth_in >> src >> edge_type_long >> dst) {
        QueryKey key{(int)edge_type_long, src};
        auto found = truth.find(key);
        if (found != truth.end()) digest_add(found->second, dst);
    }
    if (!truth_in.eof()) {
        std::fprintf(stderr, "failed while rereading edge triples from %s\n", edges_path.c_str());
        return 2;
    }
    double truth_s = std::chrono::duration<double>(clk::now() - truth_start).count();
    std::fprintf(stderr, "computed truth digests: unique_queries=%zu truth_s=%.3f\n", truth.size(), truth_s);

    auto verify_start = clk::now();
    uint64_t checked = 0;
    uint64_t mismatches = 0;
    std::vector<std::string> mismatch_json;
    auto txn = graph.begin_read_only_transaction();

    for (const QueryKey& key : queries) {
        Digest live;
        auto iter = txn.get_edges(key.src, (lg::label_t)key.edge_type);
        while (iter.valid()) {
            digest_add(live, iter.dst_id());
            iter.next();
        }

        const Digest& expected = truth[key];
        EdgeTypeStats& st = stats[key.edge_type];
        st.checked++;
        st.truth_neighbors += expected.count;
        st.livegraph_neighbors += live.count;
        checked++;

        if (expected.count != live.count ||
            expected.sum_hash != live.sum_hash ||
            expected.xor_hash != live.xor_hash) {
            mismatches++;
            st.mismatches++;
            if ((int)mismatch_json.size() < max_mismatches) {
                std::ostringstream row;
                row << "    {\"edge_type\": " << key.edge_type
                    << ", \"src\": " << key.src
                    << ", \"expected_count\": " << expected.count
                    << ", \"observed_count\": " << live.count
                    << ", \"expected_sum_hash\": " << expected.sum_hash
                    << ", \"observed_sum_hash\": " << live.sum_hash
                    << ", \"expected_xor_hash\": " << expected.xor_hash
                    << ", \"observed_xor_hash\": " << live.xor_hash
                    << "}";
                mismatch_json.push_back(row.str());
            }
        }
    }
    double verify_s = std::chrono::duration<double>(clk::now() - verify_start).count();

    std::ostringstream js;
    js << "{\n";
    js << "  \"system\": \"livegraph-digest\",\n";
    js << "  \"edges_path\": \"";
    json_escape(js, edges_path);
    js << "\",\n";
    js << "  \"vertex_count\": " << vcount << ",\n";
    js << "  \"edge_count\": " << edge_count << ",\n";
    js << "  \"samples_per_edge_type\": " << samples << ",\n";
    js << "  \"seed\": " << seed << ",\n";
    js << "  \"sampled_queries\": " << queries.size() << ",\n";
    js << "  \"unique_queries\": " << truth.size() << ",\n";
    js << "  \"checked\": " << checked << ",\n";
    js << "  \"mismatches\": " << mismatches << ",\n";
    js << "  \"status\": \"" << (mismatches == 0 ? "PASS" : "FAIL") << "\",\n";
    js << "  \"load_s\": " << load_s << ",\n";
    js << "  \"truth_s\": " << truth_s << ",\n";
    js << "  \"verify_s\": " << verify_s << ",\n";
    js << "  \"peak_rss_kb\": " << peak_rss_kb() << ",\n";
    js << "  \"per_edge_type\": [\n";
    bool first = true;
    for (const auto& kv : stats) {
        if (!first) js << ",\n";
        first = false;
        const EdgeTypeStats& st = kv.second;
        js << "    {\"edge_type\": " << kv.first
           << ", \"sampled_queries\": " << st.sampled_queries
           << ", \"unique_queries\": " << st.unique_queries
           << ", \"checked\": " << st.checked
           << ", \"mismatches\": " << st.mismatches
           << ", \"truth_neighbors\": " << st.truth_neighbors
           << ", \"livegraph_neighbors\": " << st.livegraph_neighbors
           << "}";
    }
    js << "\n  ],\n";
    js << "  \"sample_mismatches\": [\n";
    for (size_t i = 0; i < mismatch_json.size(); i++) {
        if (i) js << ",\n";
        js << mismatch_json[i];
    }
    js << "\n  ]\n";
    js << "}\n";

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

    std::fprintf(stderr, "verified checked=%lu mismatches=%lu verify_s=%.3f\n", checked, mismatches, verify_s);
    return mismatches == 0 ? 0 : 1;
}
