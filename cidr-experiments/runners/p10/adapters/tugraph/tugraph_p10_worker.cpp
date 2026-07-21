// Native TuGraph worker for the CIDR P10 matched typed-neighbor contract.
//
// The worker opens one immutable TuGraph database through the embedded C++ API,
// resolves edge-label ids once, and executes warmup followed by measured passes
// in this same process.  Database open/schema resolution are setup; every
// per-query interval includes read-transaction creation, source lookup, full
// outgoing-adjacency traversal with native-label filtering, complete
// destination materialization, digesting, and abort.

#include <algorithm>
#include <cstdlib>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <sys/types.h>
#include <unistd.h>
#include <utility>
#include <vector>

#include <time.h>

#include "lgraph/lgraph.h"
#include "tools/json.hpp"

namespace fs = std::filesystem;
using json = nlohmann::json;

namespace {

constexpr const char* kWorkerSchema = "cidr-p10-tugraph-worker-v1";
constexpr const char* kContractVersion = "cidr-typed-neighbor-adapter-v1";
constexpr const char* kInterfaceScope = "typed-neighbor-dense-id-v1";
constexpr const char* kExecutionModel = "native-embedded-single-worker-process-v1";
constexpr uint64_t kFormalQueryCount = 1700;

struct Digest {
    uint64_t count = 0;
    uint64_t sum_hash = 0;
    uint64_t xor_hash = 0;
};

struct TruthRow {
    uint64_t query_index = 0;
    int64_t edge_type = 0;
    uint64_t src = 0;
    Digest expected;
};

struct Observation {
    std::string phase;
    uint64_t pass_index = 0;
    TruthRow truth;
    Digest actual;
    bool timeout = false;
    uint64_t latency_ns = 0;
};

struct PhaseSummary {
    uint64_t passes = 0;
    uint64_t started_ns = 0;
    uint64_t ended_ns = 0;
    uint64_t elapsed_ns = 0;
    uint64_t total_query_latency_ns = 0;
    uint64_t timeout_queries = 0;
    uint64_t mismatch_queries = 0;
};

struct Options {
    bool capabilities = false;
    fs::path db_path;
    std::string graph;
    std::string user;
    fs::path truth_path;
    fs::path edge_map_path;
    fs::path output_dir;
    std::string system_version;
    uint64_t warmup_passes = 0;
    uint64_t measured_passes = 0;
    uint64_t timeout_ms = 0;
    uint64_t expected_query_count = 0;
    uint64_t repeat_index = 0;
};

uint64_t monotonic_ns() {
    timespec value{};
    if (clock_gettime(CLOCK_MONOTONIC, &value) != 0) {
        throw std::runtime_error("clock_gettime(CLOCK_MONOTONIC) failed");
    }
    return static_cast<uint64_t>(value.tv_sec) * 1000000000ULL +
           static_cast<uint64_t>(value.tv_nsec);
}

uint64_t parse_u64(const std::string& text, const std::string& option) {
    if (text.empty() || text.front() == '-') {
        throw std::runtime_error(option + " must be an unsigned integer");
    }
    size_t consumed = 0;
    uint64_t value = 0;
    try {
        value = std::stoull(text, &consumed);
    } catch (const std::exception&) {
        throw std::runtime_error(option + " must be an unsigned integer");
    }
    if (consumed != text.size()) {
        throw std::runtime_error(option + " must be an unsigned integer");
    }
    return value;
}

Options parse_options(int argc, char** argv) {
    Options options;
    for (int index = 1; index < argc; ++index) {
        const std::string arg = argv[index];
        auto next = [&]() -> std::string {
            if (++index >= argc) throw std::runtime_error("missing value after " + arg);
            return argv[index];
        };
        if (arg == "--capabilities") options.capabilities = true;
        else if (arg == "--db-path") options.db_path = next();
        else if (arg == "--graph") options.graph = next();
        else if (arg == "--user") options.user = next();
        else if (arg == "--truth-tsv") options.truth_path = next();
        else if (arg == "--edge-type-map") options.edge_map_path = next();
        else if (arg == "--output-dir") options.output_dir = next();
        else if (arg == "--system-version") options.system_version = next();
        else if (arg == "--warmup-passes") {
            options.warmup_passes = parse_u64(next(), arg);
        } else if (arg == "--measured-passes") {
            options.measured_passes = parse_u64(next(), arg);
        } else if (arg == "--per-query-timeout-ms") {
            options.timeout_ms = parse_u64(next(), arg);
        } else if (arg == "--expected-query-count") {
            options.expected_query_count = parse_u64(next(), arg);
        } else if (arg == "--repeat-index") {
            options.repeat_index = parse_u64(next(), arg);
        } else {
            throw std::runtime_error("unknown argument: " + arg);
        }
    }
    if (options.capabilities) return options;
    if (options.db_path.empty() || options.graph.empty() || options.user.empty() ||
        options.truth_path.empty() ||
        options.edge_map_path.empty() || options.output_dir.empty() ||
        options.system_version.empty()) {
        throw std::runtime_error("database, graph, truth, edge map, output, and system version are required");
    }
    if (!options.warmup_passes || !options.measured_passes || !options.timeout_ms ||
        !options.expected_query_count || !options.repeat_index) {
        throw std::runtime_error("pass counts, timeout, query count, and repeat index must be positive");
    }
    if (options.expected_query_count != kFormalQueryCount) {
        throw std::runtime_error("TuGraph P10 worker freezes exactly 1700 ordered truth queries");
    }
    return options;
}

std::vector<std::string> split_tsv(const std::string& line) {
    std::vector<std::string> fields;
    std::stringstream input(line);
    std::string field;
    while (std::getline(input, field, '\t')) fields.push_back(field);
    if (!line.empty() && line.back() == '\t') fields.emplace_back();
    return fields;
}

std::vector<TruthRow> read_truth(const fs::path& path, uint64_t expected_count) {
    std::ifstream input(path);
    if (!input) throw std::runtime_error("cannot open truth TSV: " + path.string());
    std::string line;
    if (!std::getline(input, line) ||
        line != "query_index\tedge_type\tsrc\tcount\tsum_hash\txor_hash") {
        throw std::runtime_error("truth TSV has an unexpected header");
    }
    std::vector<TruthRow> rows;
    uint64_t line_number = 1;
    while (std::getline(input, line)) {
        ++line_number;
        if (line.empty()) throw std::runtime_error("truth TSV contains a blank row");
        const auto fields = split_tsv(line);
        if (fields.size() != 6) {
            throw std::runtime_error("truth row " + std::to_string(line_number) +
                                     " does not contain six fields");
        }
        TruthRow row;
        try {
            size_t consumed = 0;
            row.query_index = std::stoull(fields[0], &consumed);
            if (consumed != fields[0].size()) throw std::invalid_argument("suffix");
            consumed = 0;
            row.edge_type = std::stoll(fields[1], &consumed);
            if (consumed != fields[1].size()) throw std::invalid_argument("suffix");
            consumed = 0;
            row.src = std::stoull(fields[2], &consumed);
            if (consumed != fields[2].size()) throw std::invalid_argument("suffix");
            consumed = 0;
            row.expected.count = std::stoull(fields[3], &consumed);
            if (consumed != fields[3].size()) throw std::invalid_argument("suffix");
            consumed = 0;
            row.expected.sum_hash = std::stoull(fields[4], &consumed);
            if (consumed != fields[4].size()) throw std::invalid_argument("suffix");
            consumed = 0;
            row.expected.xor_hash = std::stoull(fields[5], &consumed);
            if (consumed != fields[5].size()) throw std::invalid_argument("suffix");
        } catch (const std::exception&) {
            throw std::runtime_error("truth row " + std::to_string(line_number) +
                                     " contains a malformed integer");
        }
        if (row.query_index != rows.size()) {
            throw std::runtime_error("truth query_index is not contiguous at row " +
                                     std::to_string(line_number));
        }
        rows.push_back(row);
    }
    if (!input.eof()) throw std::runtime_error("I/O failure while reading truth TSV");
    if (rows.size() != expected_count) {
        throw std::runtime_error("truth row count differs from the frozen 1700-query count");
    }
    return rows;
}

std::map<int64_t, std::string> read_edge_map(const fs::path& path) {
    std::ifstream input(path);
    if (!input) throw std::runtime_error("cannot open edge-type map: " + path.string());
    std::string line;
    if (!std::getline(input, line) || line != "edge_type\tlabel") {
        throw std::runtime_error("edge-type map has an unexpected header");
    }
    std::map<int64_t, std::string> result;
    std::set<std::string> labels;
    while (std::getline(input, line)) {
        const auto fields = split_tsv(line);
        if (fields.size() != 2 || fields[1].empty()) {
            throw std::runtime_error("edge-type map row is malformed");
        }
        size_t consumed = 0;
        int64_t edge_type = 0;
        try {
            edge_type = std::stoll(fields[0], &consumed);
        } catch (const std::exception&) {
            throw std::runtime_error("edge-type map contains malformed edge_type");
        }
        if (consumed != fields[0].size() || !result.emplace(edge_type, fields[1]).second ||
            !labels.insert(fields[1]).second) {
            throw std::runtime_error("edge-type map contains a duplicate or malformed entry");
        }
    }
    if (result.empty()) throw std::runtime_error("edge-type map is empty");
    return result;
}

uint64_t mix64(uint64_t value) {
    value += 0x9e3779b97f4a7c15ULL;
    value = (value ^ (value >> 30)) * 0xbf58476d1ce4e5b9ULL;
    value = (value ^ (value >> 27)) * 0x94d049bb133111ebULL;
    return value ^ (value >> 31);
}

void digest_add(Digest& digest, uint64_t dense_dst) {
    ++digest.count;
    digest.sum_hash += mix64(dense_dst);
    digest.xor_hash ^= mix64(dense_dst ^ 0xd6e8feb86659fd93ULL);
}

bool same_digest(const Digest& left, const Digest& right) {
    return left.count == right.count && left.sum_hash == right.sum_hash &&
           left.xor_hash == right.xor_hash;
}

fs::path process_executable() {
    std::vector<char> buffer(4096);
    const auto count = readlink("/proc/self/exe", buffer.data(), buffer.size() - 1);
    if (count <= 0) throw std::runtime_error("cannot resolve /proc/self/exe");
    buffer[static_cast<size_t>(count)] = '\0';
    return fs::canonical(buffer.data());
}

fs::path loaded_liblgraph() {
    std::ifstream maps("/proc/self/maps");
    if (!maps) throw std::runtime_error("cannot read /proc/self/maps");
    std::set<fs::path> candidates;
    std::string line;
    while (std::getline(maps, line)) {
        const auto slash = line.find('/');
        if (slash == std::string::npos) continue;
        fs::path path = line.substr(slash);
        if (path.filename() == "liblgraph.so" && fs::is_regular_file(path)) {
            candidates.insert(fs::canonical(path));
        }
    }
    if (candidates.size() != 1) {
        throw std::runtime_error("expected exactly one loaded liblgraph.so mapping");
    }
    return *candidates.begin();
}

Digest execute_query(
    lgraph_api::GraphDB& database,
    const TruthRow& query,
    uint16_t label_id) {
    Digest digest;
    std::vector<uint64_t> materialized;
    {
        auto transaction = database.CreateReadTxn();
        {
            auto vertex = transaction.GetVertexIterator(
                static_cast<int64_t>(query.src), false);
            if (!vertex.IsValid() ||
                vertex.GetId() != static_cast<int64_t>(query.src)) {
                throw std::runtime_error("dense source VID is absent: " +
                                         std::to_string(query.src));
            }
            // TuGraph's public EdgeUid value ordering and its internal
            // OutEdgeSortOrder are different.  A nearest EdgeUid seek is not
            // a documented label-range API; on SF10 it can land inside the
            // requested label and omit later destinations.  Iterate the
            // complete outgoing adjacency and filter by the resolved native
            // label id, which is the same API contract used by TuGraph's
            // reference traversal examples.
            auto edge = vertex.GetOutEdgeIterator();
            while (edge.IsValid()) {
                const auto uid = edge.GetUid();
                if (uid.src != static_cast<int64_t>(query.src)) {
                    throw std::runtime_error("TuGraph out-edge iterator changed source VID");
                }
                if (uid.lid == label_id) {
                    if (uid.dst < 0) {
                        throw std::runtime_error("TuGraph returned a negative destination VID");
                    }
                    materialized.push_back(static_cast<uint64_t>(uid.dst));
                }
                edge.Next();
            }
        }
        for (const auto destination : materialized) digest_add(digest, destination);
        transaction.Abort();
    }
    return digest;
}

PhaseSummary run_phase(
    const std::string& phase,
    uint64_t passes,
    uint64_t timeout_ns,
    const std::vector<TruthRow>& truth,
    const std::map<int64_t, uint16_t>& label_ids,
    lgraph_api::GraphDB& database,
    std::vector<Observation>& observations) {
    PhaseSummary summary;
    summary.passes = passes;
    summary.started_ns = monotonic_ns();
    for (uint64_t pass_index = 0; pass_index < passes; ++pass_index) {
        for (const auto& query : truth) {
            const auto started = monotonic_ns();
            const auto actual = execute_query(database, query, label_ids.at(query.edge_type));
            const auto ended = monotonic_ns();
            const auto latency = std::max<uint64_t>(1, ended - started);
            const bool timeout = latency > timeout_ns;
            observations.push_back(
                Observation{phase, pass_index, query, actual, timeout, latency});
            summary.total_query_latency_ns += latency;
            if (timeout) ++summary.timeout_queries;
            if (!timeout && !same_digest(actual, query.expected)) ++summary.mismatch_queries;
        }
    }
    summary.ended_ns = monotonic_ns();
    summary.elapsed_ns = summary.ended_ns - summary.started_ns;
    if (!summary.elapsed_ns || summary.elapsed_ns < summary.total_query_latency_ns) {
        throw std::runtime_error("phase boundary is inconsistent with serial query intervals");
    }
    return summary;
}

void write_observations(
    const fs::path& path,
    const std::vector<Observation>& rows,
    uint64_t repeat_index) {
    std::ofstream output(path);
    if (!output) throw std::runtime_error("cannot create observations TSV");
    output << "contract_version\tsystem_id\tgroup\trepeat_index\tphase\tpass_index\t"
              "query_index\tedge_type\tsrc\texpected_count\tactual_count\t"
              "expected_sum_hash\tactual_sum_hash\texpected_xor_hash\tactual_xor_hash\t"
              "status\tlatency_ns\n";
    for (const auto& row : rows) {
        output << kContractVersion << "\ttugraph\tembedded\t" << repeat_index << '\t'
               << row.phase << '\t' << row.pass_index << '\t' << row.truth.query_index << '\t'
               << row.truth.edge_type << '\t' << row.truth.src << '\t'
               << row.truth.expected.count << '\t';
        if (!row.timeout) output << row.actual.count;
        output << '\t' << row.truth.expected.sum_hash << '\t';
        if (!row.timeout) output << row.actual.sum_hash;
        output << '\t' << row.truth.expected.xor_hash << '\t';
        if (!row.timeout) output << row.actual.xor_hash;
        output << '\t' << (row.timeout ? "timeout" : "ok") << '\t' << row.latency_ns << '\n';
    }
    output.flush();
    if (!output) throw std::runtime_error("failed while writing observations TSV");
}

json phase_json(const PhaseSummary& phase) {
    return {
        {"passes", phase.passes},
        {"started_monotonic_ns", phase.started_ns},
        {"ended_monotonic_ns", phase.ended_ns},
        {"elapsed_ns", phase.elapsed_ns},
        {"total_query_latency_ns", phase.total_query_latency_ns},
        {"timeout_queries", phase.timeout_queries},
        {"mismatch_queries", phase.mismatch_queries},
    };
}

void atomic_json(const fs::path& path, const json& value) {
    const fs::path temporary = path.string() + ".tmp";
    {
        std::ofstream output(temporary);
        if (!output) throw std::runtime_error("cannot create " + temporary.string());
        output << value.dump(2) << '\n';
        output.flush();
        if (!output) throw std::runtime_error("failed while writing " + temporary.string());
    }
    fs::rename(temporary, path);
}

int run(const Options& options) {
    if (!fs::is_directory(options.db_path)) {
        throw std::runtime_error("TuGraph database directory does not exist");
    }
    if (!fs::is_directory(options.output_dir)) {
        throw std::runtime_error("output directory must already exist");
    }
    const auto truth = read_truth(options.truth_path, options.expected_query_count);
    const auto edge_names = read_edge_map(options.edge_map_path);
    std::set<int64_t> required_edge_types;
    for (const auto& row : truth) required_edge_types.insert(row.edge_type);
    for (const auto edge_type : required_edge_types) {
        if (!edge_names.count(edge_type)) {
            throw std::runtime_error("edge-type map omits truth edge type " +
                                     std::to_string(edge_type));
        }
    }
    if (edge_names.size() != required_edge_types.size()) {
        throw std::runtime_error("edge-type map must exactly cover truth edge types");
    }

    // No process other than this worker opens the selected DB.  false/false
    // means non-durable access and never creates a missing galaxy; OpenGraph
    // then enforces the read-only handle used for all query transactions.
    const auto canonical_database = fs::canonical(options.db_path).string();
    const char* password = std::getenv("CIDR_TUGRAPH_PASSWORD");
    if (password == nullptr || *password == '\0') {
        throw std::runtime_error("CIDR_TUGRAPH_PASSWORD is required");
    }
    lgraph_api::Galaxy galaxy(
        canonical_database, options.user, password, false, false);
    auto database = galaxy.OpenGraph(options.graph, true);

    std::map<int64_t, uint16_t> label_ids;
    json resolved_labels = json::array();
    {
        auto transaction = database.CreateReadTxn();
        for (const auto& [edge_type, label] : edge_names) {
            const auto raw_id = transaction.GetEdgeLabelId(label);
            if (raw_id > std::numeric_limits<uint16_t>::max()) {
                throw std::runtime_error("TuGraph edge label id exceeds EdgeUid range");
            }
            const auto label_id = static_cast<uint16_t>(raw_id);
            label_ids.emplace(edge_type, label_id);
            resolved_labels.push_back(
                {{"edge_type", edge_type}, {"label", label}, {"label_id", label_id}});
        }
        transaction.Abort();
    }

    std::vector<Observation> observations;
    observations.reserve(
        truth.size() * (options.warmup_passes + options.measured_passes));
    const uint64_t timeout_ns = options.timeout_ms * 1000000ULL;
    const auto warmup = run_phase(
        "warmup", options.warmup_passes, timeout_ns, truth, label_ids, database,
        observations);
    const auto measured = run_phase(
        "measured", options.measured_passes, timeout_ns, truth, label_ids, database,
        observations);
    database.Close();
    galaxy.Close();

    write_observations(options.output_dir / "query-observations.tsv", observations,
                       options.repeat_index);
    json summary = {
        {"schema_version", kWorkerSchema},
        {"contract_version", kContractVersion},
        {"interface_scope", kInterfaceScope},
        {"engine_api", "lgraph_api::Galaxy/OpenGraph/CreateReadTxn/GetOutEdgeIterator"},
        {"execution_model", kExecutionModel},
        {"system_version", options.system_version},
        {"pid", static_cast<uint64_t>(getpid())},
        {"ppid", static_cast<uint64_t>(getppid())},
        {"process_executable", process_executable().string()},
        {"loaded_liblgraph", loaded_liblgraph().string()},
        {"database_path", fs::canonical(options.db_path).string()},
        {"graph", options.graph},
        {"user", options.user},
        {"read_only", true},
        {"durable", false},
        {"create_if_not_exist", false},
        {"query_count", truth.size()},
        {"resolved_edge_labels", resolved_labels},
        {"warmup", phase_json(warmup)},
        {"measured", phase_json(measured)},
    };
    atomic_json(options.output_dir / "tugraph-worker-summary.json", summary);
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    try {
        const auto options = parse_options(argc, argv);
        if (options.capabilities) {
            std::cout << json({
                {"schema_version", kWorkerSchema},
                {"contract_version", kContractVersion},
                {"interface_scope", kInterfaceScope},
                {"execution_model", kExecutionModel},
                {"formal_query_count", kFormalQueryCount},
                {"fixture_only", false},
            }).dump() << '\n';
            return 0;
        }
        return run(options);
    } catch (const std::exception& error) {
        std::cerr << "TuGraph P10 worker error: " << error.what() << '\n';
        return 2;
    }
}
