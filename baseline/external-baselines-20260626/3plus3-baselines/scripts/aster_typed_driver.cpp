#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

#include "rocksdb/cache.h"
#include "rocksdb/filter_policy.h"
#include "rocksdb/graph.h"
#include "rocksdb/options.h"
#include "rocksdb/statistics.h"
#include "rocksdb/table.h"

namespace {

using rocksdb::Edges;
using rocksdb::free_edges;
using rocksdb::node_id_t;
using rocksdb::RocksGraph;

constexpr uint64_t MASK = UINT64_MAX;

struct Key {
  int64_t edge_type;
  int64_t src;

  bool operator==(const Key& other) const {
    return edge_type == other.edge_type && src == other.src;
  }
};

struct KeyHash {
  size_t operator()(const Key& key) const {
    uint64_t a = Mix64(static_cast<uint64_t>(key.edge_type));
    uint64_t b = Mix64(static_cast<uint64_t>(key.src) ^ 0xD6E8FEB86659FD93ULL);
    return static_cast<size_t>(a ^ (b + 0x9E3779B97F4A7C15ULL + (a << 6) + (a >> 2)));
  }

  static uint64_t Mix64(uint64_t x) {
    x = (x + 0x9E3779B97F4A7C15ULL) & MASK;
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ULL) & MASK;
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EBULL) & MASK;
    return (x ^ (x >> 31)) & MASK;
  }
};

struct Digest {
  uint64_t count = 0;
  uint64_t sum_hash = 0;
  uint64_t xor_hash = 0;
};

struct Query {
  int64_t query_index = 0;
  int64_t edge_type = 0;
  int64_t src = 0;
  Digest expected;
};

struct Mismatch {
  int64_t query_index = 0;
  int64_t edge_type = 0;
  int64_t src = 0;
  Digest expected;
  Digest observed;
};

struct Args {
  std::string dense_path;
  std::string truth_path;
  std::string db_path;
  std::string out_path;
  std::string scale;
  int max_mismatches = 10;
};

uint64_t Mix64(uint64_t x) {
  x = (x + 0x9E3779B97F4A7C15ULL) & MASK;
  x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ULL) & MASK;
  x = ((x ^ (x >> 27)) * 0x94D049BB133111EBULL) & MASK;
  return (x ^ (x >> 31)) & MASK;
}

void DigestAdd(Digest& digest, int64_t dst) {
  uint64_t h1 = Mix64(static_cast<uint64_t>(dst));
  uint64_t h2 = Mix64(static_cast<uint64_t>(dst) ^ 0xD6E8FEB86659FD93ULL);
  digest.count += 1;
  digest.sum_hash = (digest.sum_hash + h1) & MASK;
  digest.xor_hash ^= h2;
}

std::vector<std::string> SplitTabs(const std::string& line) {
  std::vector<std::string> out;
  std::string cur;
  for (char ch : line) {
    if (ch == '\t') {
      out.push_back(cur);
      cur.clear();
    } else {
      cur.push_back(ch);
    }
  }
  out.push_back(cur);
  return out;
}

std::vector<Query> ReadTruth(const std::string& path) {
  std::ifstream in(path);
  if (!in) {
    throw std::runtime_error("cannot open truth file: " + path);
  }
  std::vector<Query> queries;
  std::string line;
  std::getline(in, line);
  while (std::getline(in, line)) {
    if (line.empty()) continue;
    auto parts = SplitTabs(line);
    if (parts.size() < 6) {
      throw std::runtime_error("bad truth row: " + line);
    }
    Query q;
    q.query_index = std::stoll(parts[0]);
    q.edge_type = std::stoll(parts[1]);
    q.src = std::stoll(parts[2]);
    q.expected.count = std::stoull(parts[3]);
    q.expected.sum_hash = std::stoull(parts[4]);
    q.expected.xor_hash = std::stoull(parts[5]);
    queries.push_back(q);
  }
  return queries;
}

double SecondsSince(std::chrono::steady_clock::time_point start) {
  auto end = std::chrono::steady_clock::now();
  return std::chrono::duration_cast<std::chrono::duration<double>>(end - start).count();
}

double Percentile(std::vector<double> values, double pct) {
  if (values.empty()) return 0.0;
  std::sort(values.begin(), values.end());
  size_t idx = static_cast<size_t>(std::ceil((pct / 100.0) * values.size()));
  if (idx == 0) idx = 1;
  idx -= 1;
  if (idx >= values.size()) idx = values.size() - 1;
  return values[idx];
}

uint64_t PeakRssKb() {
  std::ifstream in("/proc/self/status");
  std::string key;
  while (in >> key) {
    if (key == "VmHWM:") {
      uint64_t kb = 0;
      in >> kb;
      return kb;
    }
    std::string rest;
    std::getline(in, rest);
  }
  return 0;
}

std::string JsonEscape(const std::string& s) {
  std::ostringstream out;
  for (char ch : s) {
    switch (ch) {
      case '"': out << "\\\""; break;
      case '\\': out << "\\\\"; break;
      case '\n': out << "\\n"; break;
      case '\r': out << "\\r"; break;
      case '\t': out << "\\t"; break;
      default: out << ch; break;
    }
  }
  return out.str();
}

Args ParseArgs(int argc, char** argv) {
  Args args;
  for (int i = 1; i < argc; ++i) {
    std::string a(argv[i]);
    auto eq = a.find('=');
    std::string key = eq == std::string::npos ? a : a.substr(0, eq);
    std::string val = eq == std::string::npos ? "" : a.substr(eq + 1);
    if (key == "--dense") args.dense_path = val;
    else if (key == "--truth") args.truth_path = val;
    else if (key == "--db") args.db_path = val;
    else if (key == "--out") args.out_path = val;
    else if (key == "--scale") args.scale = val;
    else if (key == "--max-mismatches") args.max_mismatches = std::stoi(val);
    else throw std::runtime_error("unknown arg: " + a);
  }
  if (args.dense_path.empty() || args.truth_path.empty() ||
      args.db_path.empty() || args.out_path.empty() || args.scale.empty()) {
    throw std::runtime_error("required args: --dense --truth --db --out --scale");
  }
  return args;
}

}  // namespace

int main(int argc, char** argv) {
  try {
    Args args = ParseArgs(argc, argv);
    auto queries = ReadTruth(args.truth_path);

    std::unordered_map<Key, node_id_t, KeyHash> logical_ids;
    std::vector<std::vector<node_id_t>> adjacency;
    logical_ids.reserve(1 << 20);
    adjacency.reserve(1 << 20);

    auto load_start = std::chrono::steady_clock::now();
    std::ifstream dense(args.dense_path);
    if (!dense) {
      throw std::runtime_error("cannot open dense file: " + args.dense_path);
    }
    int64_t vertex_count = 0;
    dense >> vertex_count;
    int64_t src = 0;
    int64_t edge_type = 0;
    int64_t dst = 0;
    uint64_t loaded_edges = 0;
    while (dense >> src >> edge_type >> dst) {
      Key key{edge_type, src};
      auto it = logical_ids.find(key);
      if (it == logical_ids.end()) {
        node_id_t id = static_cast<node_id_t>(adjacency.size());
        it = logical_ids.emplace(key, id).first;
        adjacency.emplace_back();
      }
      adjacency[static_cast<size_t>(it->second)].push_back(static_cast<node_id_t>(dst));
      loaded_edges += 1;
    }
    double dense_group_s = SecondsSince(load_start);

    rocksdb::Options options;
    options.create_if_missing = true;
    options.statistics = rocksdb::CreateDBStatistics();
    options.write_buffer_size = 64ULL * 1024ULL * 1024ULL;
    options.max_bytes_for_level_base =
        options.write_buffer_size * options.max_bytes_for_level_multiplier;
    rocksdb::BlockBasedTableOptions table_options;
    table_options.block_cache = rocksdb::NewLRUCache(256ULL * 1024ULL * 1024ULL);
    options.table_factory.reset(rocksdb::NewBlockBasedTableFactory(table_options));

    RocksGraph graph(options, EDGE_UPDATE_EAGER, ENCODING_TYPE_NONE, true, args.db_path);
    for (size_t id = 0; id < adjacency.size(); ++id) {
      std::vector<node_id_t> in_neighbors;
      graph.AddVertexWithEdges(static_cast<node_id_t>(id), adjacency[id], in_neighbors);
    }
    adjacency.clear();
    adjacency.shrink_to_fit();
    double load_s = SecondsSince(load_start);

    std::vector<double> latencies_us;
    std::vector<double> positive_latencies_us;
    std::vector<double> negative_latencies_us;
    std::vector<Mismatch> mismatches;
    uint64_t total_neighbors = 0;
    uint64_t positive_neighbors = 0;
    uint64_t negative_neighbors = 0;
    auto query_start = std::chrono::steady_clock::now();
    for (const auto& q : queries) {
      auto t0 = std::chrono::steady_clock::now();
      Digest observed;
      auto id_it = logical_ids.find(Key{q.edge_type, q.src});
      if (id_it != logical_ids.end()) {
        Edges edges;
        auto s = graph.GetAllEdges(id_it->second, &edges);
        if (s.ok()) {
          for (uint32_t i = 0; i < edges.num_edges_out; ++i) {
            DigestAdd(observed, edges.nxts_out[i].nxt);
          }
          free_edges(&edges);
        }
      }
      double elapsed_us = SecondsSince(t0) * 1000000.0;
      latencies_us.push_back(elapsed_us);
      total_neighbors += observed.count;
      if (q.edge_type > 0) {
        positive_latencies_us.push_back(elapsed_us);
        positive_neighbors += observed.count;
      } else {
        negative_latencies_us.push_back(elapsed_us);
        negative_neighbors += observed.count;
      }
      bool ok = observed.count == q.expected.count &&
                observed.sum_hash == q.expected.sum_hash &&
                observed.xor_hash == q.expected.xor_hash;
      if (!ok && static_cast<int>(mismatches.size()) < args.max_mismatches) {
        mismatches.push_back(Mismatch{q.query_index, q.edge_type, q.src, q.expected, observed});
      }
    }
    double query_s = SecondsSince(query_start);

    auto summary = [](const std::vector<double>& vals, const char* name) {
      double sum = 0.0;
      for (double v : vals) sum += v;
      std::ostringstream out;
      out << "\"" << name << "\":{";
      out << "\"ops\":" << vals.size() << ",";
      out << "\"avg_us\":" << (vals.empty() ? 0.0 : sum / vals.size()) << ",";
      out << "\"p50_us\":" << Percentile(vals, 50) << ",";
      out << "\"p90_us\":" << Percentile(vals, 90) << ",";
      out << "\"p99_us\":" << Percentile(vals, 99);
      out << "}";
      return out.str();
    };

    std::ofstream out(args.out_path);
    out << std::fixed << std::setprecision(6);
    out << "{\n";
    out << "  \"system\":\"Aster RocksGraph\",\n";
    out << "  \"status\":\"" << (mismatches.empty() ? "PASS" : "FAIL") << "\",\n";
    out << "  \"scale\":\"" << JsonEscape(args.scale) << "\",\n";
    out << "  \"dense_path\":\"" << JsonEscape(args.dense_path) << "\",\n";
    out << "  \"truth_path\":\"" << JsonEscape(args.truth_path) << "\",\n";
    out << "  \"db_dir\":\"" << JsonEscape(args.db_path) << "\",\n";
    out << "  \"bridge\":\"compact logical vertex id per (edge_type,src); avoids sparse-id Morris counter blow-up\",\n";
    out << "  \"input_vertices\":" << vertex_count << ",\n";
    out << "  \"loaded_edges\":" << loaded_edges << ",\n";
    out << "  \"loaded_logical_vertices\":" << logical_ids.size() << ",\n";
    out << "  \"dense_group_s\":" << dense_group_s << ",\n";
    out << "  \"load_s\":" << load_s << ",\n";
    out << "  \"query_s\":" << query_s << ",\n";
    out << "  \"checked\":" << queries.size() << ",\n";
    out << "  \"mismatches\":" << mismatches.size() << ",\n";
    out << "  \"total_neighbors\":" << total_neighbors << ",\n";
    out << "  \"positive_neighbors\":" << positive_neighbors << ",\n";
    out << "  \"negative_neighbors\":" << negative_neighbors << ",\n";
    out << "  \"peak_rss_kb\":" << PeakRssKb() << ",\n";
    out << "  " << summary(latencies_us, "all_types") << ",\n";
    out << "  " << summary(positive_latencies_us, "positive_edge_types") << ",\n";
    out << "  " << summary(negative_latencies_us, "negative_edge_types") << ",\n";
    out << "  \"sample_mismatches\":[";
    for (size_t i = 0; i < mismatches.size(); ++i) {
      const auto& m = mismatches[i];
      if (i) out << ",";
      out << "{\"query_index\":" << m.query_index
          << ",\"edge_type\":" << m.edge_type
          << ",\"src\":" << m.src
          << ",\"expected\":{\"count\":" << m.expected.count
          << ",\"sum_hash\":" << m.expected.sum_hash
          << ",\"xor_hash\":" << m.expected.xor_hash
          << "},\"observed\":{\"count\":" << m.observed.count
          << ",\"sum_hash\":" << m.observed.sum_hash
          << ",\"xor_hash\":" << m.observed.xor_hash << "}}";
    }
    out << "]\n";
    out << "}\n";
    return mismatches.empty() ? 0 : 2;
  } catch (const std::exception& ex) {
    std::cerr << "aster_typed_driver failed: " << ex.what() << "\n";
    return 1;
  }
}
