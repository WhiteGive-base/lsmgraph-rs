// LiveGraph query worker for the CIDR P10 typed-neighbor adapter.
//
// The upstream LiveGraph revision vendored by this repository cannot reopen a
// block/WAL pair: both files are truncated by Graph's constructor and graph
// metadata lives only in process memory.  This worker therefore advertises an
// explicit import-only capability and is usable only for correctness fixtures.
// The Python adapter refuses formal mode until a future worker advertises the
// query-only, reopenable-store capability.

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <string>
#include <tuple>
#include <vector>

#include <time.h>

#include "livegraph.hpp"

namespace fs = std::filesystem;

constexpr const char* kContractVersion = "cidr-typed-neighbor-adapter-v1";
constexpr const char* kWorkerSchema = "cidr-livegraph-p10-worker-v1";
constexpr const char* kStoreCapability = "import-only-process-lifetime-v1";

struct Digest {
    uint64_t count = 0;
    uint64_t sum_hash = 0;
    uint64_t xor_hash = 0;
};

struct TruthQuery {
    uint64_t query_index = 0;
    int64_t edge_type = 0;
    uint64_t src = 0;
    Digest expected;
};

struct PhaseSummary {
    uint64_t started_ns = 0;
    uint64_t ended_ns = 0;
    uint64_t elapsed_ns = 0;
    uint64_t total_query_latency_ns = 0;
    uint64_t passes = 0;
};

struct Options {
    bool capabilities = false;
    std::string store_mode;
    fs::path edges;
    fs::path truth;
    fs::path block;
    fs::path wal;
    fs::path output_dir;
    uint64_t warmup_passes = 0;
    uint64_t measured_passes = 0;
    uint64_t timeout_ms = 0;
    uint64_t expected_query_count = 0;
    uint64_t repeat_index = 0;
};

static uint64_t monotonic_ns() {
    struct timespec value {};
    if (clock_gettime(CLOCK_MONOTONIC, &value) != 0) {
        throw std::runtime_error("clock_gettime(CLOCK_MONOTONIC) failed");
    }
    return static_cast<uint64_t>(value.tv_sec) * 1000000000ULL +
           static_cast<uint64_t>(value.tv_nsec);
}

static uint64_t parse_u64(const std::string& text, const char* option) {
    size_t consumed = 0;
    uint64_t value = 0;
    try {
        value = std::stoull(text, &consumed);
    } catch (const std::exception&) {
        throw std::runtime_error(std::string(option) + " must be an unsigned integer");
    }
    if (consumed != text.size()) {
        throw std::runtime_error(std::string(option) + " must be an unsigned integer");
    }
    return value;
}

static Options parse_options(int argc, char** argv) {
    Options options;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        auto next = [&]() -> std::string {
            if (++i >= argc) throw std::runtime_error("missing value after " + arg);
            return argv[i];
        };
        if (arg == "--capabilities") options.capabilities = true;
        else if (arg == "--store-mode") options.store_mode = next();
        else if (arg == "--edges") options.edges = next();
        else if (arg == "--truth-tsv") options.truth = next();
        else if (arg == "--block-path") options.block = next();
        else if (arg == "--wal-path") options.wal = next();
        else if (arg == "--output-dir") options.output_dir = next();
        else if (arg == "--warmup-passes") options.warmup_passes = parse_u64(next(), "--warmup-passes");
        else if (arg == "--measured-passes") options.measured_passes = parse_u64(next(), "--measured-passes");
        else if (arg == "--per-query-timeout-ms") options.timeout_ms = parse_u64(next(), "--per-query-timeout-ms");
        else if (arg == "--expected-query-count") options.expected_query_count = parse_u64(next(), "--expected-query-count");
        else if (arg == "--repeat-index") options.repeat_index = parse_u64(next(), "--repeat-index");
        else throw std::runtime_error("unknown argument: " + arg);
    }
    if (options.capabilities) return options;
    if (options.store_mode != "import") {
        throw std::runtime_error(
            "this LiveGraph build supports only --store-mode import; query-only reopen is unavailable");
    }
    if (options.edges.empty() || options.truth.empty() || options.block.empty() ||
        options.wal.empty() || options.output_dir.empty()) {
        throw std::runtime_error("edges, truth, block, WAL, and output paths are required");
    }
    if (!options.warmup_passes || !options.measured_passes || !options.timeout_ms ||
        !options.expected_query_count || !options.repeat_index) {
        throw std::runtime_error(
            "pass counts, timeout, expected query count, and repeat index must be positive");
    }
    return options;
}

static uint64_t mix64(uint64_t value) {
    value += 0x9e3779b97f4a7c15ULL;
    value = (value ^ (value >> 30)) * 0xbf58476d1ce4e5b9ULL;
    value = (value ^ (value >> 27)) * 0x94d049bb133111ebULL;
    return value ^ (value >> 31);
}

static void digest_add(Digest& digest, uint64_t dense_dst) {
    ++digest.count;
    digest.sum_hash += mix64(dense_dst);
    digest.xor_hash ^= mix64(dense_dst ^ 0xd6e8feb86659fd93ULL);
}

static std::vector<std::string> split_tsv(const std::string& line) {
    std::vector<std::string> fields;
    std::stringstream stream(line);
    std::string field;
    while (std::getline(stream, field, '\t')) fields.push_back(field);
    if (!line.empty() && line.back() == '\t') fields.emplace_back();
    return fields;
}

static std::vector<TruthQuery> read_truth(const fs::path& path, uint64_t expected_count) {
    std::ifstream input(path);
    if (!input) throw std::runtime_error("cannot open truth TSV: " + path.string());
    std::string line;
    if (!std::getline(input, line) ||
        line != "query_index\tedge_type\tsrc\tcount\tsum_hash\txor_hash") {
        throw std::runtime_error("truth TSV has an unexpected header");
    }
    std::vector<TruthQuery> queries;
    size_t line_number = 1;
    while (std::getline(input, line)) {
        ++line_number;
        if (line.empty()) throw std::runtime_error("truth TSV contains a blank row");
        const auto fields = split_tsv(line);
        if (fields.size() != 6) {
            throw std::runtime_error("truth TSV row " + std::to_string(line_number) +
                                     " does not have six fields");
        }
        TruthQuery query;
        try {
            query.query_index = std::stoull(fields[0]);
            query.edge_type = std::stoll(fields[1]);
            query.src = std::stoull(fields[2]);
            query.expected.count = std::stoull(fields[3]);
            query.expected.sum_hash = std::stoull(fields[4]);
            query.expected.xor_hash = std::stoull(fields[5]);
        } catch (const std::exception&) {
            throw std::runtime_error("truth TSV row " + std::to_string(line_number) +
                                     " contains a malformed integer");
        }
        if (query.query_index != queries.size()) {
            throw std::runtime_error("truth query_index is not contiguous at row " +
                                     std::to_string(line_number));
        }
        if (query.edge_type < -32768 || query.edge_type > 65535) {
            throw std::runtime_error("truth edge type is outside LiveGraph label_t range");
        }
        queries.push_back(query);
    }
    if (!input.eof()) throw std::runtime_error("failed while reading truth TSV");
    if (queries.size() != expected_count) {
        throw std::runtime_error("truth query count differs from --expected-query-count");
    }
    return queries;
}

static uint64_t import_graph(lg::Graph& graph, const fs::path& edges, uint64_t vertex_count) {
    std::ifstream input(edges);
    if (!input) throw std::runtime_error("cannot open dense edge list: " + edges.string());
    uint64_t header_vertices = 0;
    if (!(input >> header_vertices) || header_vertices != vertex_count) {
        throw std::runtime_error("dense edge-list vertex header changed during import");
    }
    auto loader = graph.begin_batch_loader();
    for (uint64_t vertex = 0; vertex < vertex_count; ++vertex) loader.new_vertex();
    uint64_t src = 0;
    uint64_t dst = 0;
    int64_t edge_type = 0;
    uint64_t edge_count = 0;
    while (input >> src >> edge_type >> dst) {
        if (src >= vertex_count || dst >= vertex_count) {
            throw std::runtime_error("dense edge endpoint is outside the declared vertex range");
        }
        if (edge_type < -32768 || edge_type > 65535) {
            throw std::runtime_error("dense edge type is outside LiveGraph label_t range");
        }
        loader.put_edge(src, static_cast<lg::label_t>(edge_type), dst, std::string_view());
        ++edge_count;
    }
    if (!input.eof()) throw std::runtime_error("malformed dense edge list");
    loader.commit();
    return edge_count;
}

static uint64_t read_vertex_count(const fs::path& edges) {
    std::ifstream input(edges);
    uint64_t vertex_count = 0;
    if (!input || !(input >> vertex_count) || vertex_count == 0) {
        throw std::runtime_error("dense edge list has no positive vertex-count header");
    }
    return vertex_count;
}

static PhaseSummary run_phase(
    const std::string& phase,
    uint64_t passes,
    uint64_t timeout_ns,
    const std::vector<TruthQuery>& queries,
    lg::Transaction& transaction,
    std::ofstream& observations,
    const std::string& system_id,
    const std::string& group,
    uint64_t repeat_index) {
    PhaseSummary summary;
    summary.passes = passes;
    summary.started_ns = monotonic_ns();
    for (uint64_t pass_index = 0; pass_index < passes; ++pass_index) {
        for (const auto& query : queries) {
            const uint64_t query_start = monotonic_ns();
            Digest actual;
            {
                auto iterator = transaction.get_edges(
                    query.src, static_cast<lg::label_t>(query.edge_type));
                while (iterator.valid()) {
                    digest_add(actual, iterator.dst_id());
                    iterator.next();
                }
            }
            const uint64_t query_end = monotonic_ns();
            const uint64_t latency_ns = std::max<uint64_t>(1, query_end - query_start);
            summary.total_query_latency_ns += latency_ns;
            const bool timed_out = latency_ns >= timeout_ns;
            observations << kContractVersion << '\t' << system_id << '\t' << group << '\t'
                         << repeat_index << '\t' << phase << '\t' << pass_index << '\t'
                         << query.query_index << '\t' << query.edge_type << '\t' << query.src << '\t'
                         << query.expected.count << '\t';
            if (!timed_out) observations << actual.count;
            observations << '\t' << query.expected.sum_hash << '\t';
            if (!timed_out) observations << actual.sum_hash;
            observations << '\t' << query.expected.xor_hash << '\t';
            if (!timed_out) observations << actual.xor_hash;
            observations << '\t' << (timed_out ? "timeout" : "ok") << '\t' << latency_ns << '\n';
            if (!observations) throw std::runtime_error("failed writing query observations");
        }
    }
    do {
        summary.ended_ns = monotonic_ns();
    } while (summary.ended_ns - summary.started_ns < summary.total_query_latency_ns);
    summary.elapsed_ns = summary.ended_ns - summary.started_ns;
    return summary;
}

static void write_event(std::ofstream& output, const char* phase, const char* event, uint64_t ns) {
    output << "{\"contract_version\":\"" << kContractVersion << "\",\"event\":\""
           << event << "\",\"monotonic_ns\":" << ns << ",\"phase\":\"" << phase
           << "\"}\n";
}

static void write_phase_json(std::ofstream& output, const char* name, const PhaseSummary& phase) {
    output << "  \"" << name << "\": {\"elapsed_ns\":" << phase.elapsed_ns
           << ",\"ended_monotonic_ns\":" << phase.ended_ns << ",\"passes\":" << phase.passes
           << ",\"started_monotonic_ns\":" << phase.started_ns
           << ",\"total_query_latency_ns\":" << phase.total_query_latency_ns << "}";
}

int main(int argc, char** argv) {
    try {
        const Options options = parse_options(argc, argv);
        if (options.capabilities) {
            std::cout << "{\"schema_version\":\"" << kWorkerSchema
                      << "\",\"store_capability\":\"" << kStoreCapability << "\"}\n";
            return 0;
        }
        if (!fs::is_regular_file(options.edges) || !fs::is_regular_file(options.truth)) {
            throw std::runtime_error("edges and truth inputs must be regular files");
        }
        if (!fs::is_directory(options.output_dir)) {
            throw std::runtime_error("output directory must already exist");
        }
        if (fs::exists(options.block) || fs::exists(options.wal)) {
            throw std::runtime_error("import mode refuses to truncate an existing block or WAL file");
        }
        const auto queries = read_truth(options.truth, options.expected_query_count);
        const uint64_t vertex_count = read_vertex_count(options.edges);
        for (const auto& query : queries) {
            if (query.src >= vertex_count) {
                throw std::runtime_error("truth source is outside the dense edge-list vertex range");
            }
        }

        const uint64_t import_start = monotonic_ns();
        lg::Graph graph(options.block.string(), options.wal.string(), 1ULL << 40,
                        std::max<uint64_t>(vertex_count + 1, 2));
        const uint64_t edge_count = import_graph(graph, options.edges, vertex_count);
        const uint64_t import_elapsed_ns = monotonic_ns() - import_start;

        std::ofstream observations(options.output_dir / "query-observations.tsv");
        if (!observations) throw std::runtime_error("cannot create query-observations.tsv");
        observations << "contract_version\tsystem_id\tgroup\trepeat_index\tphase\tpass_index\t"
                        "query_index\tedge_type\tsrc\texpected_count\tactual_count\t"
                        "expected_sum_hash\tactual_sum_hash\texpected_xor_hash\t"
                        "actual_xor_hash\tstatus\tlatency_ns\n";
        auto transaction = graph.begin_read_only_transaction();
        const uint64_t timeout_ns = options.timeout_ms * 1000000ULL;
        const PhaseSummary warmup = run_phase(
            "warmup", options.warmup_passes, timeout_ns, queries, transaction,
            observations, "livegraph", "embedded", options.repeat_index);
        const PhaseSummary measured = run_phase(
            "measured", options.measured_passes, timeout_ns, queries, transaction,
            observations, "livegraph", "embedded", options.repeat_index);
        observations.close();

        std::ofstream events(options.output_dir / "phase-events.jsonl");
        if (!events) throw std::runtime_error("cannot create phase-events.jsonl");
        write_event(events, "warmup", "start", warmup.started_ns);
        write_event(events, "warmup", "end", warmup.ended_ns);
        write_event(events, "measured", "start", measured.started_ns);
        write_event(events, "measured", "end", measured.ended_ns);
        events.close();

        std::ofstream summary(options.output_dir / "livegraph-worker-summary.json");
        if (!summary) throw std::runtime_error("cannot create livegraph-worker-summary.json");
        summary << "{\n  \"edge_count\":" << edge_count
                << ",\n  \"import_elapsed_ns\":" << import_elapsed_ns << ",\n";
        write_phase_json(summary, "measured", measured);
        summary << ",\n  \"schema_version\":\"" << kWorkerSchema
                << "\",\n  \"store_capability\":\"" << kStoreCapability
                << "\",\n  \"truth_query_count\":" << queries.size() << ",\n";
        write_phase_json(summary, "warmup", warmup);
        summary << ",\n  \"vertex_count\":" << vertex_count << "\n}\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "livegraph_p10_driver: " << error.what() << '\n';
        return 2;
    }
}
