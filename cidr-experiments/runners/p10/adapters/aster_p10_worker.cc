#include <algorithm>
#include <cerrno>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <time.h>
#include <unordered_map>
#include <utility>
#include <vector>

#include "rocksdb/cache.h"
#include "rocksdb/graph.h"
#include "rocksdb/options.h"
#include "rocksdb/statistics.h"
#include "rocksdb/table.h"

namespace fs = std::filesystem;

namespace {

using rocksdb::Edges;
using rocksdb::RocksGraph;
using rocksdb::free_edges;
using rocksdb::node_id_t;

constexpr const char* kWorkerSchema = "p10-aster-rocksgraph-worker-v1";
constexpr const char* kStoreSchema = "p10-aster-rocksgraph-store-v1";
constexpr const char* kKeyMapName = "p10-aster-key-map.tsv";
constexpr const char* kStoreMetadataName = "p10-aster-store.json";
constexpr uint64_t kDigestSalt = 0xd6e8feb86659fd93ULL;

uint64_t Mix64(uint64_t x) {
  x += 0x9e3779b97f4a7c15ULL;
  x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ULL;
  x = (x ^ (x >> 27)) * 0x94d049bb133111ebULL;
  return x ^ (x >> 31);
}

uint64_t MonotonicNs() {
  struct timespec value {};
  if (clock_gettime(CLOCK_MONOTONIC, &value) != 0) {
    throw std::runtime_error(std::string("clock_gettime(CLOCK_MONOTONIC): ") +
                             std::strerror(errno));
  }
  return static_cast<uint64_t>(value.tv_sec) * 1000000000ULL +
         static_cast<uint64_t>(value.tv_nsec);
}

struct Key {
  int64_t edge_type = 0;
  int64_t src = 0;

  bool operator==(const Key& other) const {
    return edge_type == other.edge_type && src == other.src;
  }
};

struct KeyHash {
  size_t operator()(const Key& key) const {
    const uint64_t left = Mix64(static_cast<uint64_t>(key.edge_type));
    const uint64_t right = Mix64(static_cast<uint64_t>(key.src) ^ kDigestSalt);
    return static_cast<size_t>(left ^
                               (right + 0x9e3779b97f4a7c15ULL + (left << 6) +
                                (left >> 2)));
  }
};

struct Digest {
  uint64_t count = 0;
  uint64_t sum_hash = 0;
  uint64_t xor_hash = 0;

  bool operator==(const Digest& other) const {
    return count == other.count && sum_hash == other.sum_hash &&
           xor_hash == other.xor_hash;
  }
};

void DigestAdd(Digest* digest, int64_t dst) {
  const uint64_t dense_dst = static_cast<uint64_t>(dst);
  digest->count += 1;
  digest->sum_hash += Mix64(dense_dst);
  digest->xor_hash ^= Mix64(dense_dst ^ kDigestSalt);
}

struct Query {
  uint64_t query_index = 0;
  int64_t edge_type = 0;
  int64_t src = 0;
  Digest expected;
};

struct PhaseSummary {
  uint64_t passes = 0;
  uint64_t requested_queries = 0;
  uint64_t completed_queries = 0;
  uint64_t timeout_queries = 0;
  uint64_t mismatch_queries = 0;
  uint64_t started_monotonic_ns = 0;
  uint64_t ended_monotonic_ns = 0;
};

struct Args {
  bool capabilities = false;
  bool digest_stream = false;
  std::string lifecycle;
  fs::path dataset;
  fs::path truth;
  fs::path store;
  fs::path output_dir;
  std::string source_commit;
  std::string dataset_sha256;
  std::string binary_sha256;
  uint64_t warmup_passes = 0;
  uint64_t measured_passes = 0;
  uint64_t timeout_ms = 0;
  uint64_t block_cache_bytes = 256ULL * 1024ULL * 1024ULL;
};

rocksdb::Options GraphOptions(uint64_t block_cache_bytes);

std::string JsonEscape(const std::string& input) {
  std::string output;
  output.reserve(input.size());
  for (unsigned char ch : input) {
    switch (ch) {
      case '"': output += "\\\""; break;
      case '\\': output += "\\\\"; break;
      case '\n': output += "\\n"; break;
      case '\r': output += "\\r"; break;
      case '\t': output += "\\t"; break;
      default:
        if (ch < 0x20) {
          throw std::runtime_error("control byte in JSON string");
        }
        output.push_back(static_cast<char>(ch));
    }
  }
  return output;
}

uint64_t ParseUint64(const std::string& raw, const std::string& context) {
  size_t used = 0;
  uint64_t value = 0;
  try {
    value = std::stoull(raw, &used, 10);
  } catch (const std::exception&) {
    throw std::runtime_error(context + " must be an unsigned integer");
  }
  if (used != raw.size()) {
    throw std::runtime_error(context + " contains trailing characters");
  }
  return value;
}

int64_t ParseInt64(const std::string& raw, const std::string& context) {
  size_t used = 0;
  int64_t value = 0;
  try {
    value = std::stoll(raw, &used, 10);
  } catch (const std::exception&) {
    throw std::runtime_error(context + " must be a signed integer");
  }
  if (used != raw.size()) {
    throw std::runtime_error(context + " contains trailing characters");
  }
  return value;
}

Args ParseArgs(int argc, char** argv) {
  Args args;
  if (argc == 2 && std::string(argv[1]) == "--capabilities") {
    args.capabilities = true;
    return args;
  }
  if (argc == 4 && std::string(argv[1]) == "--dump-store-digest-stream" &&
      std::string(argv[2]) == "--store") {
    args.digest_stream = true;
    args.store = fs::absolute(argv[3]);
    return args;
  }
  std::unordered_map<std::string, std::string> values;
  for (int index = 1; index < argc; index += 2) {
    if (index + 1 >= argc) {
      throw std::runtime_error("worker arguments must be --key value pairs");
    }
    const std::string key(argv[index]);
    if (key.rfind("--", 0) != 0 || values.count(key) != 0) {
      throw std::runtime_error("invalid or duplicate worker argument: " + key);
    }
    values.emplace(key, argv[index + 1]);
  }
  const std::vector<std::string> required = {
      "--lifecycle",       "--dataset",        "--truth",
      "--store",           "--output-dir",     "--source-commit",
      "--dataset-sha256",  "--binary-sha256", "--warmup-passes",
      "--measured-passes", "--timeout-ms"};
  for (const auto& key : required) {
    if (values.count(key) == 0 || values.at(key).empty()) {
      throw std::runtime_error("missing worker argument: " + key);
    }
  }
  const std::vector<std::string> allowed = {
      "--lifecycle",       "--dataset",          "--truth",
      "--store",           "--output-dir",       "--source-commit",
      "--dataset-sha256",  "--binary-sha256",   "--warmup-passes",
      "--measured-passes", "--timeout-ms",       "--block-cache-bytes"};
  for (const auto& item : values) {
    if (std::find(allowed.begin(), allowed.end(), item.first) == allowed.end()) {
      throw std::runtime_error("unknown worker argument: " + item.first);
    }
  }
  args.lifecycle = values.at("--lifecycle");
  if (args.lifecycle != "fresh" && args.lifecycle != "reopen") {
    throw std::runtime_error("--lifecycle must be fresh or reopen");
  }
  args.dataset = fs::absolute(values.at("--dataset"));
  args.truth = fs::absolute(values.at("--truth"));
  args.store = fs::absolute(values.at("--store"));
  args.output_dir = fs::absolute(values.at("--output-dir"));
  args.source_commit = values.at("--source-commit");
  args.dataset_sha256 = values.at("--dataset-sha256");
  args.binary_sha256 = values.at("--binary-sha256");
  args.warmup_passes = ParseUint64(values.at("--warmup-passes"), "--warmup-passes");
  args.measured_passes = ParseUint64(values.at("--measured-passes"), "--measured-passes");
  args.timeout_ms = ParseUint64(values.at("--timeout-ms"), "--timeout-ms");
  if (values.count("--block-cache-bytes") != 0) {
    args.block_cache_bytes =
        ParseUint64(values.at("--block-cache-bytes"), "--block-cache-bytes");
  }
  if (args.warmup_passes == 0 || args.measured_passes == 0 ||
      args.timeout_ms == 0 || args.block_cache_bytes == 0) {
    throw std::runtime_error("passes, timeout, and block cache must be positive");
  }
  if (args.timeout_ms > std::numeric_limits<uint64_t>::max() / 1000000ULL) {
    throw std::runtime_error("--timeout-ms is too large");
  }
  return args;
}

void WriteU64(std::ostream* output, uint64_t value) {
  for (unsigned int byte = 0; byte < 8; ++byte) {
    output->put(static_cast<char>((value >> (byte * 8)) & 0xff));
  }
}

void WriteBlob(std::ostream* output, const std::string& label,
               const char* data, size_t size) {
  WriteU64(output, label.size());
  output->write(label.data(), static_cast<std::streamsize>(label.size()));
  WriteU64(output, size);
  output->write(data, static_cast<std::streamsize>(size));
  if (!*output) {
    throw std::runtime_error("cannot write logical store digest stream");
  }
}

void WriteFileBlob(std::ostream* output, const fs::path& root,
                   const std::string& name) {
  const fs::path path = root / name;
  std::ifstream input(path, std::ios::binary);
  if (!input) {
    throw std::runtime_error("logical store digest input is missing: " +
                             path.string());
  }
  const uint64_t size = fs::file_size(path);
  WriteU64(output, name.size());
  output->write(name.data(), static_cast<std::streamsize>(name.size()));
  WriteU64(output, size);
  std::vector<char> buffer(1024 * 1024);
  while (input) {
    input.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
    const std::streamsize count = input.gcount();
    if (count > 0) {
      output->write(buffer.data(), count);
    }
  }
  if (!input.eof() || !*output) {
    throw std::runtime_error("cannot stream logical store sidecar: " +
                             path.string());
  }
}

int DumpStoreDigestStream(const fs::path& store) {
  if (!fs::is_directory(store)) {
    throw std::runtime_error("logical store digest requires an existing store directory");
  }
  const std::string prefix = "p10-aster-query-store-digest-stream-v1\n";
  std::cout.write(prefix.data(), static_cast<std::streamsize>(prefix.size()));
  WriteFileBlob(&std::cout, store, kKeyMapName);
  WriteFileBlob(&std::cout, store, kStoreMetadataName);
  WriteFileBlob(&std::cout, store, "GraphMeta.log");

  auto options = GraphOptions(8ULL * 1024ULL * 1024ULL);
  options.create_if_missing = false;
  rocksdb::DB* database = nullptr;
  rocksdb::Status opened =
      rocksdb::DB::OpenForReadOnly(options, store.string(), &database, false);
  if (!opened.ok()) {
    throw std::runtime_error("cannot open Aster default column family read-only: " +
                             opened.ToString());
  }
  std::unique_ptr<rocksdb::Iterator> iterator(
      database->NewIterator(rocksdb::ReadOptions()));
  for (iterator->SeekToFirst(); iterator->Valid(); iterator->Next()) {
    const rocksdb::Slice key = iterator->key();
    const rocksdb::Slice value = iterator->value();
    WriteBlob(&std::cout, "default-key", key.data(), key.size());
    WriteBlob(&std::cout, "default-value", value.data(), value.size());
  }
  const rocksdb::Status iterated = iterator->status();
  iterator.reset();
  const rocksdb::Status closed = database->Close();
  delete database;
  if (!iterated.ok() || !closed.ok()) {
    throw std::runtime_error("Aster logical store iteration/close failed: " +
                             iterated.ToString() + "; " + closed.ToString());
  }
  std::cout.flush();
  if (!std::cout) {
    throw std::runtime_error("cannot finalize logical store digest stream");
  }
  return 0;
}

std::vector<std::string> SplitTabs(const std::string& line) {
  std::vector<std::string> values;
  size_t start = 0;
  while (true) {
    const size_t tab = line.find('\t', start);
    if (tab == std::string::npos) {
      values.push_back(line.substr(start));
      return values;
    }
    values.push_back(line.substr(start, tab - start));
    start = tab + 1;
  }
}

std::vector<Query> ReadTruth(const fs::path& path) {
  std::ifstream input(path);
  if (!input) {
    throw std::runtime_error("cannot open truth TSV: " + path.string());
  }
  std::string line;
  if (!std::getline(input, line) ||
      line != "query_index\tedge_type\tsrc\tcount\tsum_hash\txor_hash") {
    throw std::runtime_error("truth TSV header mismatch");
  }
  std::vector<Query> queries;
  uint64_t line_number = 1;
  while (std::getline(input, line)) {
    ++line_number;
    if (line.empty() || (!line.empty() && line.back() == '\r')) {
      throw std::runtime_error("truth TSV contains an empty/CRLF row at line " +
                               std::to_string(line_number));
    }
    const auto fields = SplitTabs(line);
    if (fields.size() != 6) {
      throw std::runtime_error("truth TSV row does not have six fields at line " +
                               std::to_string(line_number));
    }
    Query query;
    query.query_index = ParseUint64(fields[0], "truth query_index");
    query.edge_type = ParseInt64(fields[1], "truth edge_type");
    query.src = ParseInt64(fields[2], "truth src");
    query.expected.count = ParseUint64(fields[3], "truth count");
    query.expected.sum_hash = ParseUint64(fields[4], "truth sum_hash");
    query.expected.xor_hash = ParseUint64(fields[5], "truth xor_hash");
    if (query.query_index != queries.size() || query.src < 0) {
      throw std::runtime_error("truth query_index is not contiguous or src is negative");
    }
    queries.push_back(query);
  }
  if (queries.empty()) {
    throw std::runtime_error("truth TSV contains no queries");
  }
  return queries;
}

rocksdb::Options GraphOptions(uint64_t block_cache_bytes) {
  rocksdb::Options options;
  options.create_if_missing = true;
  options.statistics = rocksdb::CreateDBStatistics();
  options.write_buffer_size = 64ULL * 1024ULL * 1024ULL;
  options.max_bytes_for_level_base =
      options.write_buffer_size * options.max_bytes_for_level_multiplier;
  rocksdb::BlockBasedTableOptions table_options;
  table_options.block_cache = rocksdb::NewLRUCache(block_cache_bytes);
  options.table_factory.reset(rocksdb::NewBlockBasedTableFactory(table_options));
  return options;
}

void RequireEmptyStore(const fs::path& store) {
  if (!fs::is_directory(store)) {
    throw std::runtime_error("fresh lifecycle requires an existing store directory");
  }
  if (fs::directory_iterator(store) != fs::directory_iterator()) {
    throw std::runtime_error("fresh lifecycle refuses a non-empty store directory");
  }
}

using KeyMap = std::unordered_map<Key, node_id_t, KeyHash>;

struct ImportData {
  int64_t declared_vertices = 0;
  uint64_t loaded_edges = 0;
  KeyMap logical_ids;
  std::vector<Key> keys;
  std::vector<std::vector<node_id_t>> adjacency;
};

ImportData ReadDataset(const fs::path& dataset) {
  std::ifstream input(dataset);
  if (!input) {
    throw std::runtime_error("cannot open dense dataset: " + dataset.string());
  }
  ImportData data;
  data.logical_ids.reserve(1U << 20);
  data.keys.reserve(1U << 20);
  data.adjacency.reserve(1U << 20);
  if (!(input >> data.declared_vertices) || data.declared_vertices < 0) {
    throw std::runtime_error("dense dataset lacks a non-negative vertex count");
  }
  int64_t src = 0;
  int64_t edge_type = 0;
  int64_t dst = 0;
  while (input >> src >> edge_type >> dst) {
    if (src < 0 || dst < 0) {
      throw std::runtime_error("dense dataset contains a negative vertex ID");
    }
    const Key key{edge_type, src};
    auto found = data.logical_ids.find(key);
    if (found == data.logical_ids.end()) {
      if (data.adjacency.size() >=
          static_cast<size_t>(std::numeric_limits<node_id_t>::max())) {
        throw std::runtime_error("too many typed source keys for RocksGraph node IDs");
      }
      const node_id_t id = static_cast<node_id_t>(data.adjacency.size());
      found = data.logical_ids.emplace(key, id).first;
      data.keys.push_back(key);
      data.adjacency.emplace_back();
    }
    data.adjacency.at(static_cast<size_t>(found->second))
        .push_back(static_cast<node_id_t>(dst));
    ++data.loaded_edges;
  }
  if (!input.eof()) {
    throw std::runtime_error("dense dataset contains a malformed edge row");
  }
  return data;
}

void WriteKeyMap(const fs::path& path, const std::vector<Key>& keys) {
  std::ofstream output(path, std::ios::trunc);
  if (!output) {
    throw std::runtime_error("cannot create Aster key map: " + path.string());
  }
  output << "edge_type\tsrc\trocksgraph_vertex_id\n";
  for (size_t id = 0; id < keys.size(); ++id) {
    output << keys[id].edge_type << '\t' << keys[id].src << '\t' << id << '\n';
  }
  output.close();
  if (!output) {
    throw std::runtime_error("cannot finalize Aster key map");
  }
}

KeyMap ReadKeyMap(const fs::path& path) {
  std::ifstream input(path);
  if (!input) {
    throw std::runtime_error("cannot open frozen Aster key map: " + path.string());
  }
  std::string line;
  if (!std::getline(input, line) ||
      line != "edge_type\tsrc\trocksgraph_vertex_id") {
    throw std::runtime_error("Aster key-map header mismatch");
  }
  KeyMap mapping;
  uint64_t expected_id = 0;
  while (std::getline(input, line)) {
    if (line.empty() || line.back() == '\r') {
      throw std::runtime_error("Aster key map contains an empty/CRLF row");
    }
    const auto fields = SplitTabs(line);
    if (fields.size() != 3) {
      throw std::runtime_error("Aster key map row does not have three fields");
    }
    const Key key{ParseInt64(fields[0], "key-map edge_type"),
                  ParseInt64(fields[1], "key-map src")};
    const uint64_t id = ParseUint64(fields[2], "key-map vertex ID");
    if (key.src < 0 || id != expected_id ||
        id > static_cast<uint64_t>(std::numeric_limits<node_id_t>::max()) ||
        !mapping.emplace(key, static_cast<node_id_t>(id)).second) {
      throw std::runtime_error("Aster key map is non-contiguous or duplicated");
    }
    ++expected_id;
  }
  if (mapping.empty()) {
    throw std::runtime_error("Aster key map contains no typed source keys");
  }
  return mapping;
}

void WriteStoreMetadata(const Args& args, const ImportData& data) {
  const fs::path path = args.store / kStoreMetadataName;
  std::ofstream output(path, std::ios::trunc);
  if (!output) {
    throw std::runtime_error("cannot create Aster store metadata");
  }
  output << "{\n"
         << "  \"schema_version\": \"" << kStoreSchema << "\",\n"
         << "  \"bridge\": \"compact-first-occurrence-id-per-edge-type-src-v1\",\n"
         << "  \"source_commit\": \"" << JsonEscape(args.source_commit) << "\",\n"
         << "  \"dataset_sha256\": \"" << JsonEscape(args.dataset_sha256) << "\",\n"
         << "  \"builder_binary_sha256\": \"" << JsonEscape(args.binary_sha256) << "\",\n"
         << "  \"declared_vertices\": " << data.declared_vertices << ",\n"
         << "  \"loaded_edges\": " << data.loaded_edges << ",\n"
         << "  \"typed_source_keys\": " << data.keys.size() << ",\n"
         << "  \"key_map\": \"" << kKeyMapName << "\",\n"
         << "  \"edge_update_policy\": \"EDGE_UPDATE_EAGER\",\n"
         << "  \"encoding_type\": \"ENCODING_TYPE_NONE\"\n"
         << "}\n";
  output.close();
  if (!output) {
    throw std::runtime_error("cannot finalize Aster store metadata");
  }
}

KeyMap FreshImport(const Args& args) {
  RequireEmptyStore(args.store);
  ImportData data = ReadDataset(args.dataset);
  {
    auto options = GraphOptions(args.block_cache_bytes);
    RocksGraph graph(options, EDGE_UPDATE_EAGER, ENCODING_TYPE_NONE, false,
                     args.store.string());
    for (size_t id = 0; id < data.adjacency.size(); ++id) {
      std::vector<node_id_t> inbound;
      rocksdb::Status status = graph.AddVertexWithEdges(
          static_cast<node_id_t>(id), data.adjacency[id], inbound);
      if (!status.ok()) {
        throw std::runtime_error("RocksGraph import failed at typed vertex " +
                                 std::to_string(id) + ": " + status.ToString());
      }
    }
  }
  WriteKeyMap(args.store / kKeyMapName, data.keys);
  WriteStoreMetadata(args, data);
  return std::move(data.logical_ids);
}

Digest QueryTypedNeighbors(RocksGraph* graph, const KeyMap& keys,
                           const Query& query) {
  Digest digest;
  const auto found = keys.find(Key{query.edge_type, query.src});
  if (found == keys.end()) {
    return digest;
  }
  Edges edges{};
  rocksdb::Status status = graph->GetAllEdges(found->second, &edges);
  if (status.IsNotFound()) {
    return digest;
  }
  if (!status.ok()) {
    throw std::runtime_error("RocksGraph GetAllEdges failed: " + status.ToString());
  }
  for (uint32_t index = 0; index < edges.num_edges_out; ++index) {
    DigestAdd(&digest, edges.nxts_out[index].nxt);
  }
  free_edges(&edges);
  return digest;
}

void WriteEvent(std::ofstream* events, const std::string& phase,
                const std::string& event, uint64_t monotonic_ns) {
  *events << "{\"phase\":\"" << phase << "\",\"event\":\"" << event
          << "\",\"monotonic_ns\":" << monotonic_ns << "}\n";
  events->flush();
  if (!*events) {
    throw std::runtime_error("cannot write Aster phase event");
  }
}

PhaseSummary RunPhase(const std::string& phase, uint64_t passes,
                      uint64_t timeout_ns, RocksGraph* graph,
                      const KeyMap& keys, const std::vector<Query>& queries,
                      std::ofstream* observations, std::ofstream* events) {
  PhaseSummary summary;
  summary.passes = passes;
  summary.requested_queries = passes * queries.size();
  summary.started_monotonic_ns = MonotonicNs();
  WriteEvent(events, phase, "start", summary.started_monotonic_ns);
  for (uint64_t pass = 0; pass < passes; ++pass) {
    for (const Query& query : queries) {
      const uint64_t started = MonotonicNs();
      const Digest actual = QueryTypedNeighbors(graph, keys, query);
      const uint64_t ended = MonotonicNs();
      const uint64_t latency_ns = std::max<uint64_t>(1, ended - started);
      const bool timeout = latency_ns >= timeout_ns;
      *observations << phase << '\t' << pass << '\t' << query.query_index << '\t'
                    << query.edge_type << '\t' << query.src << '\t'
                    << query.expected.count << '\t';
      if (timeout) {
        *observations << "\t";
      } else {
        *observations << actual.count << '\t';
      }
      *observations << query.expected.sum_hash << '\t';
      if (timeout) {
        *observations << "\t";
      } else {
        *observations << actual.sum_hash << '\t';
      }
      *observations << query.expected.xor_hash << '\t';
      if (timeout) {
        *observations << "\ttimeout\t" << latency_ns << '\n';
        ++summary.timeout_queries;
      } else {
        *observations << actual.xor_hash << "\tok\t" << latency_ns << '\n';
        ++summary.completed_queries;
        if (!(actual == query.expected)) {
          ++summary.mismatch_queries;
        }
      }
      if (!*observations) {
        throw std::runtime_error("cannot write Aster query observation");
      }
    }
  }
  observations->flush();
  summary.ended_monotonic_ns =
      std::max<uint64_t>(summary.started_monotonic_ns + 1, MonotonicNs());
  WriteEvent(events, phase, "end", summary.ended_monotonic_ns);
  return summary;
}

void WritePhaseJson(std::ofstream* output, const char* name,
                    const PhaseSummary& phase, bool trailing_comma) {
  *output << "  \"" << name << "\": {\n"
          << "    \"passes\": " << phase.passes << ",\n"
          << "    \"requested_queries\": " << phase.requested_queries << ",\n"
          << "    \"completed_queries\": " << phase.completed_queries << ",\n"
          << "    \"timeout_queries\": " << phase.timeout_queries << ",\n"
          << "    \"mismatch_queries\": " << phase.mismatch_queries << ",\n"
          << "    \"started_monotonic_ns\": " << phase.started_monotonic_ns << ",\n"
          << "    \"ended_monotonic_ns\": " << phase.ended_monotonic_ns << ",\n"
          << "    \"elapsed_ns\": "
          << (phase.ended_monotonic_ns - phase.started_monotonic_ns) << "\n"
          << "  }" << (trailing_comma ? "," : "") << "\n";
}

int Run(const Args& args) {
  if (!fs::is_regular_file(args.dataset) || !fs::is_regular_file(args.truth) ||
      !fs::is_directory(args.store) || !fs::is_directory(args.output_dir)) {
    throw std::runtime_error("dataset/truth/store/output paths have the wrong type");
  }
  const fs::path observations_path = args.output_dir / "aster-raw-observations.tsv";
  const fs::path events_path = args.output_dir / "aster-raw-phase-events.jsonl";
  const fs::path summary_path = args.output_dir / "aster-worker-summary.json";
  for (const auto& path : {observations_path, events_path, summary_path}) {
    if (fs::exists(path)) {
      throw std::runtime_error("refusing to overwrite worker artifact: " + path.string());
    }
  }

  const std::vector<Query> queries = ReadTruth(args.truth);
  KeyMap keys = args.lifecycle == "fresh"
                    ? FreshImport(args)
                    : ReadKeyMap(args.store / kKeyMapName);

  std::ofstream observations(observations_path, std::ios::trunc);
  std::ofstream events(events_path, std::ios::trunc);
  if (!observations || !events) {
    throw std::runtime_error("cannot create Aster raw output files");
  }
  observations
      << "phase\tpass_index\tquery_index\tedge_type\tsrc\texpected_count\t"
         "actual_count\texpected_sum_hash\tactual_sum_hash\texpected_xor_hash\t"
         "actual_xor_hash\tstatus\tlatency_ns\n";

  const uint64_t timeout_ns = args.timeout_ms * 1000000ULL;
  PhaseSummary warmup;
  PhaseSummary measured;
  {
    auto options = GraphOptions(args.block_cache_bytes);
    RocksGraph graph(options, EDGE_UPDATE_EAGER, ENCODING_TYPE_NONE, false,
                     args.store.string());
    warmup = RunPhase("warmup", args.warmup_passes, timeout_ns, &graph, keys,
                      queries, &observations, &events);
    measured = RunPhase("measured", args.measured_passes, timeout_ns, &graph,
                        keys, queries, &observations, &events);
  }
  observations.close();
  events.close();
  if (!observations || !events) {
    throw std::runtime_error("cannot finalize Aster raw output files");
  }

  std::ofstream summary(summary_path, std::ios::trunc);
  if (!summary) {
    throw std::runtime_error("cannot create Aster worker summary");
  }
  summary << "{\n"
          << "  \"schema_version\": \"" << kWorkerSchema << "\",\n"
          << "  \"lifecycle\": \"" << args.lifecycle << "\",\n"
          << "  \"query_count\": " << queries.size() << ",\n"
          << "  \"source_commit\": \"" << JsonEscape(args.source_commit) << "\",\n"
          << "  \"dataset_sha256\": \"" << JsonEscape(args.dataset_sha256) << "\",\n"
          << "  \"binary_sha256\": \"" << JsonEscape(args.binary_sha256) << "\",\n"
          << "  \"key_count\": " << keys.size() << ",\n";
  WritePhaseJson(&summary, "warmup", warmup, true);
  WritePhaseJson(&summary, "measured", measured, false);
  summary << "}\n";
  summary.close();
  if (!summary) {
    throw std::runtime_error("cannot finalize Aster worker summary");
  }
  return 0;
}

}  // namespace

int main(int argc, char** argv) {
  try {
    const Args args = ParseArgs(argc, argv);
    if (args.capabilities) {
      std::cout
          << "{\"schema_version\":\"p10-aster-rocksgraph-capabilities-v1\","
             "\"store_capability\":\"fresh-import-and-reopen-v1\","
             "\"clock\":\"CLOCK_MONOTONIC\","
             "\"typed_bridge\":\"compact-first-occurrence-id-per-edge-type-src-v1\"}\n";
      return 0;
    }
    if (args.digest_stream) {
      return DumpStoreDigestStream(args.store);
    }
    return Run(args);
  } catch (const std::exception& error) {
    std::cerr << "aster_p10_worker failed: " << error.what() << '\n';
    return 2;
  }
}
