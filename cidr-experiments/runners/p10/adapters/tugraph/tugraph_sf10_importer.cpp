// Frozen TuGraph 4.5.2 importer for the CIDR P10 SF10 typed-neighbor store.
//
// The importer consumes the canonical dense edge list:
//   <vertex_count>\n
//   <src_dense> <edge_type> <dst_dense>\n
//
// It creates vertices in dense-ID order and rejects any TuGraph internal VID
// that differs from the dense ID.  Each of the 34 non-zero edge types is
// imported under the exact label supplied by sf10-edge-type-labels.tsv.
// Credentials are read only from CIDR_TUGRAPH_PASSWORD.

#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "lgraph/lgraph.h"

namespace fs = std::filesystem;
using Clock = std::chrono::steady_clock;

namespace {

constexpr const char* kSchema = "cidr-p10-tugraph-sf10-import-result-v1";
constexpr const char* kPasswordEnvironment = "CIDR_TUGRAPH_PASSWORD";

struct Options {
    bool capabilities = false;
    fs::path edges;
    fs::path database;
    fs::path edge_map;
    fs::path result_json;
    std::string graph = "default";
    std::string user = "admin";
    uint64_t expected_vertices = 0;
    uint64_t expected_edges = 0;
    uint64_t batch_size = 100000;
    uint64_t progress_every = 5000000;
};

uint64_t parse_u64(const std::string& value, const std::string& option) {
    if (value.empty() || value.front() == '-') {
        throw std::runtime_error(option + " must be an unsigned integer");
    }
    size_t consumed = 0;
    uint64_t parsed = 0;
    try {
        parsed = std::stoull(value, &consumed);
    } catch (const std::exception&) {
        throw std::runtime_error(option + " must be an unsigned integer");
    }
    if (consumed != value.size()) {
        throw std::runtime_error(option + " must be an unsigned integer");
    }
    return parsed;
}

Options parse_options(int argc, char** argv) {
    Options options;
    for (int index = 1; index < argc; ++index) {
        const std::string argument = argv[index];
        auto next = [&]() -> std::string {
            if (++index >= argc) {
                throw std::runtime_error("missing value after " + argument);
            }
            return argv[index];
        };
        if (argument == "--capabilities") options.capabilities = true;
        else if (argument == "--edges") options.edges = next();
        else if (argument == "--db-dir") options.database = next();
        else if (argument == "--edge-type-map") options.edge_map = next();
        else if (argument == "--result-json") options.result_json = next();
        else if (argument == "--graph") options.graph = next();
        else if (argument == "--user") options.user = next();
        else if (argument == "--expected-vertices") {
            options.expected_vertices = parse_u64(next(), argument);
        } else if (argument == "--expected-edges") {
            options.expected_edges = parse_u64(next(), argument);
        } else if (argument == "--batch-size") {
            options.batch_size = parse_u64(next(), argument);
        } else if (argument == "--progress-every") {
            options.progress_every = parse_u64(next(), argument);
        } else {
            throw std::runtime_error("unknown argument: " + argument);
        }
    }
    if (options.capabilities) return options;
    if (options.edges.empty() || options.database.empty() || options.edge_map.empty() ||
        options.result_json.empty() || options.graph.empty() || options.user.empty()) {
        throw std::runtime_error("edges, db-dir, edge map, result JSON, graph, and user are required");
    }
    if (!options.expected_vertices || !options.expected_edges || !options.batch_size ||
        !options.progress_every) {
        throw std::runtime_error("expected counts, batch size, and progress interval must be positive");
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

std::map<int, std::string> read_edge_map(const fs::path& path) {
    std::ifstream input(path);
    if (!input) throw std::runtime_error("cannot open edge-type map: " + path.string());
    std::string line;
    if (!std::getline(input, line) || line != "edge_type\tlabel") {
        throw std::runtime_error("edge-type map has an unexpected header");
    }
    std::map<int, std::string> labels;
    std::set<std::string> names;
    while (std::getline(input, line)) {
        const auto fields = split_tsv(line);
        if (fields.size() != 2 || fields[1].empty()) {
            throw std::runtime_error("edge-type map contains a malformed row");
        }
        size_t consumed = 0;
        int edge_type = 0;
        try {
            edge_type = std::stoi(fields[0], &consumed);
        } catch (const std::exception&) {
            throw std::runtime_error("edge-type map contains a malformed edge type");
        }
        if (consumed != fields[0].size() || edge_type == 0 || edge_type < -17 ||
            edge_type > 17 || !labels.emplace(edge_type, fields[1]).second ||
            !names.insert(fields[1]).second) {
            throw std::runtime_error("edge-type map contains an invalid or duplicate entry");
        }
    }
    if (labels.size() != 34) {
        throw std::runtime_error("edge-type map must contain exactly 34 labels");
    }
    for (int edge_type = -17; edge_type <= 17; ++edge_type) {
        if (edge_type != 0 && labels.count(edge_type) != 1) {
            throw std::runtime_error("edge-type map does not cover every non-zero type in [-17,17]");
        }
    }
    return labels;
}

std::string json_escape(const std::string& value) {
    std::ostringstream output;
    for (const unsigned char character : value) {
        switch (character) {
            case '\"': output << "\\\""; break;
            case '\\': output << "\\\\"; break;
            case '\n': output << "\\n"; break;
            case '\r': output << "\\r"; break;
            case '\t': output << "\\t"; break;
            default:
                if (character < 0x20) {
                    const char* hex = "0123456789abcdef";
                    output << "\\u00" << hex[(character >> 4) & 0x0f]
                           << hex[character & 0x0f];
                } else {
                    output << static_cast<char>(character);
                }
        }
    }
    return output.str();
}

void write_result(
    const fs::path& path,
    const std::string& status,
    const Options& options,
    uint64_t vertices,
    uint64_t edges,
    double elapsed_seconds,
    const std::map<int, uint64_t>& edge_counts,
    const std::string& error = "") {
    if (!path.parent_path().empty()) fs::create_directories(path.parent_path());
    const fs::path temporary = path.string() + ".tmp";
    std::ofstream output(temporary, std::ios::trunc);
    if (!output) throw std::runtime_error("cannot create result JSON: " + temporary.string());
    output << "{\n"
           << "  \"schema_version\": \"" << kSchema << "\",\n"
           << "  \"status\": \"" << json_escape(status) << "\",\n"
           << "  \"performance_eligible\": false,\n"
           << "  \"formal_performance_points\": 0,\n"
           << "  \"database_path\": \"" << json_escape(options.database.string()) << "\",\n"
           << "  \"graph\": \"" << json_escape(options.graph) << "\",\n"
           << "  \"loaded_vertices\": " << vertices << ",\n"
           << "  \"loaded_edges\": " << edges << ",\n"
           << "  \"elapsed_seconds_engineering_only\": " << elapsed_seconds << ",\n"
           << "  \"edge_type_counts\": {";
    bool first = true;
    for (const auto& [edge_type, count] : edge_counts) {
        if (!first) output << ',';
        output << "\n    \"" << edge_type << "\": " << count;
        first = false;
    }
    if (!edge_counts.empty()) output << '\n';
    output << "  },\n"
           << "  \"error\": \"" << json_escape(error) << "\"\n"
           << "}\n";
    output.close();
    if (!output) throw std::runtime_error("cannot flush result JSON: " + temporary.string());
    fs::rename(temporary, path);
}

void log_progress(const char* phase, uint64_t completed, uint64_t expected, const Clock::time_point& started) {
    const double seconds = std::chrono::duration<double>(Clock::now() - started).count();
    std::cerr << "progress phase=" << phase << " completed=" << completed
              << " expected=" << expected << " elapsed_s=" << seconds << '\n';
}

}  // namespace

int main(int argc, char** argv) {
    Options options;
    uint64_t loaded_vertices = 0;
    uint64_t loaded_edges = 0;
    std::map<int, uint64_t> edge_counts;
    const auto started = Clock::now();
    try {
        options = parse_options(argc, argv);
        if (options.capabilities) {
            std::cout << "{\"schema_version\":\"cidr-p10-tugraph-sf10-importer-capabilities-v1\","
                         "\"credential_source\":\"CIDR_TUGRAPH_PASSWORD\","
                         "\"dense_vid_contract\":\"internal-vid-equals-dense-id-v1\","
                         "\"formal_performance_points\":0}\n";
            return 0;
        }

        if (!options.database.is_absolute() || !options.result_json.is_absolute()) {
            throw std::runtime_error("database and result paths must be absolute");
        }
        options.edges = fs::canonical(options.edges);
        options.edge_map = fs::canonical(options.edge_map);
        options.database = fs::absolute(options.database).lexically_normal();
        options.result_json = fs::absolute(options.result_json).lexically_normal();
        if (!fs::is_regular_file(options.edges) || !fs::is_regular_file(options.edge_map)) {
            throw std::runtime_error("edges and edge-type map must be regular files");
        }
        if (fs::exists(options.database)) {
            throw std::runtime_error("refusing an existing database path: " + options.database.string());
        }
        if (!fs::exists(options.database.parent_path()) ||
            !fs::is_directory(options.database.parent_path())) {
            throw std::runtime_error("database parent must already exist");
        }
        const char* password = std::getenv(kPasswordEnvironment);
        if (password == nullptr || *password == '\0') {
            throw std::runtime_error("CIDR_TUGRAPH_PASSWORD is required");
        }
        const auto labels = read_edge_map(options.edge_map);

        std::ifstream input(options.edges);
        if (!input) throw std::runtime_error("cannot open dense edges: " + options.edges.string());
        uint64_t header_vertices = 0;
        if (!(input >> header_vertices) || header_vertices != options.expected_vertices) {
            throw std::runtime_error("dense edge header differs from expected vertex count");
        }

        lgraph_api::Galaxy galaxy(
            options.database.string(), options.user, password, false, true);
        auto database = galaxy.OpenGraph(options.graph, false);
        if (!database.AddVertexLabel(
                "vertex",
                {lgraph_api::FieldSpec("dense_id", lgraph_api::FieldType::INT64, false)},
                lgraph_api::VertexOptions("dense_id"))) {
            throw std::runtime_error("failed to create vertex label");
        }
        for (const auto& [edge_type, label] : labels) {
            (void)edge_type;
            if (!database.AddEdgeLabel(label, {}, lgraph_api::EdgeOptions())) {
                throw std::runtime_error("failed to create edge label: " + label);
            }
        }

        {
            auto transaction = database.CreateWriteTxn(false);
            const size_t vertex_label = transaction.GetVertexLabelId("vertex");
            const auto field_ids = transaction.GetVertexFieldIds(vertex_label, {"dense_id"});
            for (uint64_t dense = 0; dense < options.expected_vertices; ++dense) {
                const auto vid = transaction.AddVertex(
                    vertex_label,
                    field_ids,
                    {lgraph_api::FieldData::Int64(static_cast<int64_t>(dense))});
                if (vid != static_cast<int64_t>(dense)) {
                    throw std::runtime_error("TuGraph internal VID differs from dense ID at " +
                                             std::to_string(dense));
                }
                ++loaded_vertices;
                if (loaded_vertices % options.batch_size == 0 &&
                    loaded_vertices != options.expected_vertices) {
                    transaction.Commit();
                    transaction = database.CreateWriteTxn(false);
                }
                if (loaded_vertices % options.progress_every == 0) {
                    log_progress("vertices", loaded_vertices, options.expected_vertices, started);
                }
            }
            transaction.Commit();
        }
        log_progress("vertices", loaded_vertices, options.expected_vertices, started);

        std::map<int, size_t> edge_label_ids;
        {
            auto transaction = database.CreateReadTxn();
            for (const auto& [edge_type, label] : labels) {
                edge_label_ids.emplace(edge_type, transaction.GetEdgeLabelId(label));
            }
            transaction.Abort();
        }

        {
            auto transaction = database.CreateWriteTxn(false);
            uint64_t source = 0;
            uint64_t destination = 0;
            long long edge_type_raw = 0;
            while (input >> source >> edge_type_raw >> destination) {
                if (source >= options.expected_vertices || destination >= options.expected_vertices) {
                    throw std::runtime_error("dense edge endpoint is outside the frozen vertex range");
                }
                if (edge_type_raw < -17 || edge_type_raw > 17 || edge_type_raw == 0) {
                    throw std::runtime_error("dense edge contains an unsupported edge type");
                }
                const int edge_type = static_cast<int>(edge_type_raw);
                const auto label = edge_label_ids.find(edge_type);
                if (label == edge_label_ids.end()) {
                    throw std::runtime_error("dense edge type is absent from the frozen edge map");
                }
                transaction.AddEdge(
                    static_cast<int64_t>(source),
                    static_cast<int64_t>(destination),
                    label->second,
                    {},
                    {});
                ++loaded_edges;
                ++edge_counts[edge_type];
                if (loaded_edges > options.expected_edges) {
                    throw std::runtime_error("dense edge list exceeds the expected edge count");
                }
                if (loaded_edges % options.batch_size == 0 &&
                    loaded_edges != options.expected_edges) {
                    transaction.Commit();
                    transaction = database.CreateWriteTxn(false);
                }
                if (loaded_edges % options.progress_every == 0) {
                    log_progress("edges", loaded_edges, options.expected_edges, started);
                }
            }
            if (!input.eof()) throw std::runtime_error("dense edge list contains malformed trailing data");
            if (loaded_edges != options.expected_edges) {
                throw std::runtime_error("dense edge count differs from the frozen expected count");
            }
            transaction.Commit();
        }
        if (edge_counts.size() != 34) {
            throw std::runtime_error("imported edges do not cover all 34 frozen edge types");
        }
        database.Flush();
        database.Close();
        galaxy.Close();
        log_progress("edges", loaded_edges, options.expected_edges, started);
        const double elapsed = std::chrono::duration<double>(Clock::now() - started).count();
        write_result(
            options.result_json,
            "PASS",
            options,
            loaded_vertices,
            loaded_edges,
            elapsed,
            edge_counts);
        return 0;
    } catch (const std::exception& error) {
        const double elapsed = std::chrono::duration<double>(Clock::now() - started).count();
        try {
            if (!options.result_json.empty()) {
                write_result(
                    options.result_json,
                    "FAIL",
                    options,
                    loaded_vertices,
                    loaded_edges,
                    elapsed,
                    edge_counts,
                    error.what());
            }
        } catch (const std::exception& result_error) {
            std::cerr << "failed to write importer result: " << result_error.what() << '\n';
        }
        std::cerr << "TuGraph SF10 importer error: " << error.what() << '\n';
        return 2;
    }
}
