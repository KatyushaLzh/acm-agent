// SPDX-License-Identifier: MIT
// Canonical whitespace serializers. structures.hpp must be inlined first.
#ifndef ACM_RECIPE_SERIALIZERS_HPP
#define ACM_RECIPE_SERIALIZERS_HPP

#include <ostream>
#include <string>
#include <vector>

namespace acm_recipe {

inline void serialize_list_n(const std::vector<long long>& values, std::ostream& out) {
    out << values.size() << '\n';
    for (std::size_t i = 0; i < values.size(); ++i) {
        if (i) out << ' ';
        out << values[i];
    }
    out << '\n';
}

inline void serialize_string_n(const std::string& value, std::ostream& out) {
    out << value.size() << '\n' << value << '\n';
}

inline void serialize_matrix_nm(
    const std::vector<std::vector<long long>>& matrix, std::ostream& out
) {
    const std::size_t cols = matrix.empty() ? 0U : matrix.front().size();
    out << matrix.size() << ' ' << cols << '\n';
    for (const auto& row : matrix) {
        if (row.size() != cols) throw std::invalid_argument("serialize_matrix_nm requires a rectangular matrix");
        for (std::size_t j = 0; j < row.size(); ++j) {
            if (j) out << ' ';
            out << row[j];
        }
        out << '\n';
    }
}

inline void serialize_intervals_n(const std::vector<Interval>& intervals, std::ostream& out) {
    out << intervals.size() << '\n';
    for (const Interval& interval : intervals) out << interval.l << ' ' << interval.r << '\n';
}

inline void serialize_edge_list_n_m(int n, const std::vector<Edge>& edges, std::ostream& out) {
    out << n << ' ' << edges.size() << '\n';
    for (const Edge& edge : edges) out << edge.u << ' ' << edge.v << '\n';
}

inline void serialize_weighted_edge_list_n_m(
    int n, const std::vector<Edge>& edges, std::ostream& out
) {
    out << n << ' ' << edges.size() << '\n';
    for (const Edge& edge : edges) out << edge.u << ' ' << edge.v << ' ' << edge.w << '\n';
}

}  // namespace acm_recipe

#endif
