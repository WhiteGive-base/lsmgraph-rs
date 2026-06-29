// Generate fixed-seed typed-neighbor query truth from a SemL0 dense edge list.
//
// Dense edge-list text format:
//   <vertex_count>
//   <src_dense> <edge_type> <dst_dense>

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <map>
#include <random>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

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

static void usage(const char* argv0) {
    std::cerr << "usage: " << argv0
              << " --edges <dense.txt> --truth-tsv <truth.tsv> --summary-json <truth.json>"
              << " --done <DONE> [--samples N] [--seed N]\n";
}

int main(int argc, char** argv) {
    std::string edges_path;
    std::string truth_path;
    std::string summary_path;
    std::string done_path;
    int samples = 50;
    uint64_t seed = 42;

    for (int i = 1; i < argc; i++) {
        std::string arg = argv[i];
        auto next = [&]() -> std::string {
            if (++i >= argc) {
                std::cerr << "missing value after " << arg << "\n";
                std::exit(2);
            }
            return std::string(argv[i]);
        };
        if (arg == "--edges") edges_path = next();
        else if (arg == "--truth-tsv") truth_path = next();
        else if (arg == "--summary-json") summary_path = next();
        else if (arg == "--done") done_path = next();
        else if (arg == "--samples") samples = std::stoi(next());
        else if (arg == "--seed") seed = std::stoull(next());
        else {
            std::cerr << "unknown arg " << arg << "\n";
            usage(argv[0]);
            return 2;
        }
    }
    if (edges_path.empty() || truth_path.empty() || summary_path.empty() || done_path.empty()) {
        usage(argv[0]);
        return 2;
    }

    auto start = clk::now();
    std::ifstream in(edges_path);
    if (!in) {
        std::cerr << "cannot open " << edges_path << "\n";
        return 2;
    }
    uint64_t vcount = 0;
    in >> vcount;
    if (!in) {
        std::cerr << "cannot read vertex count from " << edges_path << "\n";
        return 2;
    }

    std::mt19937_64 rng(seed);
    std::map<int, Reservoir> reservoirs;
    uint64_t edge_count = 0;
    uint64_t src = 0, dst = 0;
    long edge_type_long = 0;
    while (in >> src >> edge_type_long >> dst) {
        reservoir_add(reservoirs[(int)edge_type_long], src, samples, rng);
        edge_count++;
    }
    if (!in.eof()) {
        std::cerr << "failed while reading triples from " << edges_path << "\n";
        return 2;
    }

    std::vector<QueryKey> queries;
    std::unordered_map<QueryKey, Digest, QueryKeyHash> truth;
    for (const auto& kv : reservoirs) {
        for (uint64_t sampled_src : kv.second.srcs) {
            QueryKey key{kv.first, sampled_src};
            queries.push_back(key);
            truth.emplace(key, Digest{});
        }
    }

    std::ifstream truth_in(edges_path);
    if (!truth_in) {
        std::cerr << "cannot reopen " << edges_path << "\n";
        return 2;
    }
    uint64_t header = 0;
    truth_in >> header;
    while (truth_in >> src >> edge_type_long >> dst) {
        QueryKey key{(int)edge_type_long, src};
        auto found = truth.find(key);
        if (found != truth.end()) digest_add(found->second, dst);
    }
    if (!truth_in.eof()) {
        std::cerr << "failed while rereading triples from " << edges_path << "\n";
        return 2;
    }

    std::ofstream out(truth_path);
    if (!out) {
        std::cerr << "cannot write " << truth_path << "\n";
        return 2;
    }
    out << "query_index\tedge_type\tsrc\tcount\tsum_hash\txor_hash\n";
    for (size_t i = 0; i < queries.size(); i++) {
        const auto& q = queries[i];
        const auto& d = truth[q];
        out << i << '\t' << q.edge_type << '\t' << q.src << '\t'
            << d.count << '\t' << d.sum_hash << '\t' << d.xor_hash << '\n';
    }
    out.close();

    double elapsed_s = std::chrono::duration<double>(clk::now() - start).count();
    std::ofstream js(summary_path);
    if (!js) {
        std::cerr << "cannot write " << summary_path << "\n";
        return 2;
    }
    js << "{\n";
    js << "  \"generator\": \"dense_truth_driver.cpp\",\n";
    js << "  \"vertex_count\": " << vcount << ",\n";
    js << "  \"edge_count\": " << edge_count << ",\n";
    js << "  \"samples_per_edge_type\": " << samples << ",\n";
    js << "  \"seed\": " << seed << ",\n";
    js << "  \"sampled_queries\": " << queries.size() << ",\n";
    js << "  \"unique_queries\": " << truth.size() << ",\n";
    js << "  \"truth_path\": \"";
    for (char c : truth_path) {
        if (c == '\\' || c == '"') js << '\\';
        js << c;
    }
    js << "\",\n";
    js << "  \"elapsed_s\": " << elapsed_s << ",\n";
    js << "  \"per_edge_type\": [\n";
    bool first = true;
    for (const auto& kv : reservoirs) {
        uint64_t truth_neighbors = 0;
        uint64_t unique_queries = 0;
        for (uint64_t sampled_src : kv.second.srcs) {
            QueryKey key{kv.first, sampled_src};
            truth_neighbors += truth[key].count;
        }
        for (const auto& tk : truth) {
            if (tk.first.edge_type == kv.first) unique_queries++;
        }
        if (!first) js << ",\n";
        first = false;
        js << "    {\"edge_type\": " << kv.first
           << ", \"sampled_queries\": " << kv.second.srcs.size()
           << ", \"unique_queries\": " << unique_queries
           << ", \"truth_neighbors\": " << truth_neighbors << "}";
    }
    js << "\n  ]\n";
    js << "}\n";
    js.close();

    std::ofstream done(done_path);
    done << "done\n";
    std::cerr << "dense truth generated: sampled_queries=" << queries.size()
              << " unique_queries=" << truth.size()
              << " edge_count=" << edge_count
              << " elapsed_s=" << elapsed_s << "\n";
    return 0;
}
