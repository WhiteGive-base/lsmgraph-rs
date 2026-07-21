// Build a tiny isolated TuGraph database for adapter correctness tests only.

#include <filesystem>
#include <iostream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "lgraph/lgraph.h"

namespace fs = std::filesystem;

int main(int argc, char** argv) {
    try {
        if (argc != 2) throw std::runtime_error("usage: tugraph_fixture_builder DB_PATH");
        const fs::path database_path = fs::absolute(argv[1]);
        if (fs::exists(database_path)) {
            throw std::runtime_error("fixture builder refuses an existing database path");
        }

        lgraph_api::Galaxy galaxy(
            database_path.string(), "admin", "73@TuGraph", true, true);
        auto database = galaxy.OpenGraph("default", false);
        if (!database.AddVertexLabel(
                "vertex",
                {lgraph_api::FieldSpec("dense_id", lgraph_api::FieldType::INT64, false)},
                lgraph_api::VertexOptions("dense_id"))) {
            throw std::runtime_error("failed to create fixture vertex label");
        }
        for (int edge_type = -17; edge_type <= 17; ++edge_type) {
            if (edge_type == 0) continue;
            const std::string label = edge_type < 0
                ? "et_neg_" + std::to_string(-edge_type)
                : "et_pos_" + std::to_string(edge_type);
            if (!database.AddEdgeLabel(label, {}, lgraph_api::EdgeOptions())) {
                throw std::runtime_error("failed to create edge label " + label);
            }
        }

        auto transaction = database.CreateWriteTxn(false);
        for (int64_t dense = 0; dense < 4; ++dense) {
            const auto vid = transaction.AddVertex(
                "vertex", {"dense_id"}, {std::to_string(dense)});
            if (vid != dense) {
                throw std::runtime_error("fixture internal VID is not the requested dense ID");
            }
        }
        for (int edge_type = -17; edge_type <= 17; ++edge_type) {
            if (edge_type == 0) continue;
            const std::string label = edge_type < 0
                ? "et_neg_" + std::to_string(-edge_type)
                : "et_pos_" + std::to_string(edge_type);
            for (const auto [src, dst] : {
                     std::pair<int64_t, int64_t>{0, 1},
                     std::pair<int64_t, int64_t>{0, 2},
                     std::pair<int64_t, int64_t>{1, 3},
                 }) {
                transaction.AddEdge(
                    src,
                    dst,
                    label,
                    std::vector<std::string>{},
                    std::vector<std::string>{});
            }
        }
        transaction.Commit();
        database.Flush();
        database.Close();
        galaxy.Close();
        std::cout << database_path << '\n';
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "TuGraph fixture builder error: " << error.what() << '\n';
        return 2;
    }
}
