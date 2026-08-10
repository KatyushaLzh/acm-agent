// SPDX-License-Identifier: MIT
// Standard data-structure generators inspired by testlib/jngen interfaces.
// This file intentionally has no quoted include; rng.hpp must be inlined first.
#ifndef ACM_RECIPE_STRUCTURES_HPP
#define ACM_RECIPE_STRUCTURES_HPP

#include <algorithm>
#include <cstdint>
#include <functional>
#include <limits>
#include <queue>
#include <set>
#include <stdexcept>
#include <string>
#include <unordered_set>
#include <utility>
#include <vector>

namespace acm_recipe {

struct Edge {
    int u;
    int v;
    long long w = 0;
};

struct Interval {
    long long l;
    long long r;
};

inline void require_nonnegative_size(int n, const char* name) {
    if (n < 0) {
        throw std::invalid_argument(std::string(name) + " requires n >= 0");
    }
}

inline std::vector<long long> array_uniform(int n, long long lo, long long hi, Rng& rng) {
    require_nonnegative_size(n, "array_uniform");
    std::vector<long long> result(static_cast<std::size_t>(n));
    for (long long& value : result) value = rng.uniform(lo, hi);
    return result;
}

inline std::vector<long long> array_permutation(int n, long long base, Rng& rng) {
    require_nonnegative_size(n, "array_permutation");
    if (n > 0 && base > std::numeric_limits<long long>::max() - (n - 1LL)) {
        throw std::invalid_argument("array_permutation value range overflows");
    }
    std::vector<long long> result(static_cast<std::size_t>(n));
    for (int i = 0; i < n; ++i) result[static_cast<std::size_t>(i)] = base + i;
    rng.shuffle(result.begin(), result.end());
    return result;
}

inline std::vector<long long> array_equal(int n, long long value) {
    require_nonnegative_size(n, "array_equal");
    return std::vector<long long>(static_cast<std::size_t>(n), value);
}

inline std::vector<long long> array_few_values(
    int n, int value_count, long long lo, long long hi, Rng& rng
) {
    require_nonnegative_size(n, "array_few_values");
    if (value_count <= 0 || lo > hi) {
        throw std::invalid_argument("array_few_values has an invalid value domain");
    }
    const std::uint64_t span = static_cast<std::uint64_t>(hi)
        - static_cast<std::uint64_t>(lo) + 1ULL;
    if (span != 0ULL && static_cast<std::uint64_t>(value_count) > span) {
        throw std::invalid_argument("array_few_values cannot choose enough distinct values");
    }
    std::set<long long> chosen;
    while (static_cast<int>(chosen.size()) < value_count) chosen.insert(rng.uniform(lo, hi));
    std::vector<long long> pool(chosen.begin(), chosen.end());
    std::vector<long long> result(static_cast<std::size_t>(n));
    for (long long& value : result) value = pool[static_cast<std::size_t>(rng.bounded(value_count))];
    return result;
}

inline std::vector<long long> array_monotone(
    int n, long long lo, long long hi, bool nondecreasing, Rng& rng
) {
    auto result = array_uniform(n, lo, hi, rng);
    std::sort(result.begin(), result.end());
    if (!nondecreasing) std::reverse(result.begin(), result.end());
    return result;
}

inline std::vector<long long> array_periodic(
    int n, int period, long long lo, long long hi, Rng& rng
) {
    require_nonnegative_size(n, "array_periodic");
    if (period <= 0) throw std::invalid_argument("array_periodic requires period > 0");
    const auto pattern = array_uniform(std::min(n, period), lo, hi, rng);
    std::vector<long long> result(static_cast<std::size_t>(n));
    for (int i = 0; i < n; ++i) {
        result[static_cast<std::size_t>(i)] = pattern[static_cast<std::size_t>(i % period)];
    }
    return result;
}

inline std::vector<long long> array_runs(
    int n, int run_count, long long lo, long long hi, Rng& rng
) {
    require_nonnegative_size(n, "array_runs");
    if (n == 0) return {};
    if (run_count <= 0 || run_count > n) {
        throw std::invalid_argument("array_runs requires 1 <= run_count <= n");
    }
    std::vector<int> cuts;
    for (int i = 1; i < n; ++i) cuts.push_back(i);
    rng.shuffle(cuts.begin(), cuts.end());
    cuts.resize(static_cast<std::size_t>(run_count - 1));
    std::sort(cuts.begin(), cuts.end());
    cuts.push_back(n);
    std::vector<long long> result;
    result.reserve(static_cast<std::size_t>(n));
    int previous = 0;
    for (int cut : cuts) {
        const long long value = rng.uniform(lo, hi);
        result.insert(result.end(), static_cast<std::size_t>(cut - previous), value);
        previous = cut;
    }
    return result;
}

inline std::vector<long long> array_extreme(int n, long long lo, long long hi, Rng& rng) {
    require_nonnegative_size(n, "array_extreme");
    if (lo > hi) throw std::invalid_argument("array_extreme requires lo <= hi");
    std::vector<long long> result(static_cast<std::size_t>(n));
    for (int i = 0; i < n; ++i) {
        result[static_cast<std::size_t>(i)] = rng.bounded(2) == 0 ? lo : hi;
    }
    return result;
}

inline std::string string_uniform(int n, const std::string& alphabet, Rng& rng) {
    require_nonnegative_size(n, "string_uniform");
    if (alphabet.empty()) throw std::invalid_argument("string_uniform requires a nonempty alphabet");
    std::string result(static_cast<std::size_t>(n), alphabet.front());
    for (char& ch : result) {
        ch = alphabet[static_cast<std::size_t>(rng.bounded(static_cast<long long>(alphabet.size())))];
    }
    return result;
}

inline std::string string_equal(int n, char value) {
    require_nonnegative_size(n, "string_equal");
    return std::string(static_cast<std::size_t>(n), value);
}

inline std::string string_few_chars(
    int n, const std::string& alphabet, int char_count, Rng& rng
) {
    require_nonnegative_size(n, "string_few_chars");
    if (char_count < 1 || static_cast<std::size_t>(char_count) > alphabet.size()) {
        throw std::invalid_argument("string_few_chars requires 1 <= char_count <= alphabet.size");
    }
    std::string pool = alphabet;
    rng.shuffle(pool.begin(), pool.end());
    pool.resize(static_cast<std::size_t>(char_count));
    return string_uniform(n, pool, rng);
}

inline std::string string_monotone(
    int n, const std::string& alphabet, bool nondecreasing, Rng& rng
) {
    std::string result = string_uniform(n, alphabet, rng);
    std::sort(result.begin(), result.end());
    if (!nondecreasing) std::reverse(result.begin(), result.end());
    return result;
}

inline std::string string_permutation(std::string characters, Rng& rng) {
    rng.shuffle(characters.begin(), characters.end());
    return characters;
}

inline std::string string_extreme(int n, const std::string& alphabet, Rng& rng) {
    require_nonnegative_size(n, "string_extreme");
    if (alphabet.empty()) throw std::invalid_argument("string_extreme requires a nonempty alphabet");
    const auto bounds = std::minmax_element(alphabet.begin(), alphabet.end());
    std::string result(static_cast<std::size_t>(n), *bounds.first);
    for (char& ch : result) ch = rng.bounded(2) == 0 ? *bounds.first : *bounds.second;
    return result;
}

inline std::string string_periodic(int n, const std::string& pattern) {
    require_nonnegative_size(n, "string_periodic");
    if (pattern.empty()) throw std::invalid_argument("string_periodic requires a nonempty pattern");
    std::string result;
    result.reserve(static_cast<std::size_t>(n));
    for (int i = 0; i < n; ++i) result.push_back(pattern[static_cast<std::size_t>(i) % pattern.size()]);
    return result;
}

inline std::string string_runs(int n, int run_count, const std::string& alphabet, Rng& rng) {
    const auto codes = array_runs(n, run_count, 0, static_cast<long long>(alphabet.size()) - 1, rng);
    if (alphabet.empty()) throw std::invalid_argument("string_runs requires a nonempty alphabet");
    std::string result;
    result.reserve(codes.size());
    for (long long code : codes) result.push_back(alphabet[static_cast<std::size_t>(code)]);
    return result;
}

inline std::vector<std::vector<long long>> matrix_uniform(
    int rows, int cols, long long lo, long long hi, Rng& rng
) {
    require_nonnegative_size(rows, "matrix_uniform");
    require_nonnegative_size(cols, "matrix_uniform");
    std::vector<std::vector<long long>> result(
        static_cast<std::size_t>(rows), std::vector<long long>(static_cast<std::size_t>(cols))
    );
    for (auto& row : result) for (long long& value : row) value = rng.uniform(lo, hi);
    return result;
}

inline int matrix_cell_count(int rows, int cols, const char* name) {
    require_nonnegative_size(rows, name);
    require_nonnegative_size(cols, name);
    const long long count = static_cast<long long>(rows) * cols;
    if (count > std::numeric_limits<int>::max()) {
        throw std::invalid_argument(std::string(name) + " cell count exceeds supported range");
    }
    return static_cast<int>(count);
}

inline std::vector<std::vector<long long>> matrix_from_flat(
    int rows, int cols, const std::vector<long long>& flat
) {
    if (matrix_cell_count(rows, cols, "matrix_from_flat") != static_cast<int>(flat.size())) {
        throw std::invalid_argument("matrix_from_flat size mismatch");
    }
    std::vector<std::vector<long long>> result(
        static_cast<std::size_t>(rows), std::vector<long long>(static_cast<std::size_t>(cols))
    );
    for (int i = 0; i < rows; ++i) for (int j = 0; j < cols; ++j) {
        result[static_cast<std::size_t>(i)][static_cast<std::size_t>(j)]
            = flat[static_cast<std::size_t>(i * cols + j)];
    }
    return result;
}

inline std::vector<std::vector<long long>> matrix_equal(int rows, int cols, long long value) {
    const int count = matrix_cell_count(rows, cols, "matrix_equal");
    return matrix_from_flat(rows, cols, array_equal(count, value));
}

inline std::vector<std::vector<long long>> matrix_few_values(
    int rows, int cols, int value_count, long long lo, long long hi, Rng& rng
) {
    const int count = matrix_cell_count(rows, cols, "matrix_few_values");
    return matrix_from_flat(rows, cols, array_few_values(count, value_count, lo, hi, rng));
}

inline std::vector<std::vector<long long>> matrix_monotone(
    int rows, int cols, long long lo, long long hi, bool nondecreasing, Rng& rng
) {
    const int count = matrix_cell_count(rows, cols, "matrix_monotone");
    return matrix_from_flat(rows, cols, array_monotone(count, lo, hi, nondecreasing, rng));
}

inline std::vector<std::vector<long long>> matrix_permutation(
    int rows, int cols, long long base, Rng& rng
) {
    const int count = matrix_cell_count(rows, cols, "matrix_permutation");
    return matrix_from_flat(rows, cols, array_permutation(count, base, rng));
}

inline std::vector<std::vector<long long>> matrix_periodic(
    int rows,
    int cols,
    int period_rows,
    int period_cols,
    long long lo,
    long long hi,
    Rng& rng
) {
    matrix_cell_count(rows, cols, "matrix_periodic");
    if (period_rows <= 0 || period_cols <= 0) {
        throw std::invalid_argument("matrix_periodic requires positive periods");
    }
    const int pattern_rows = std::min(rows, period_rows);
    const int pattern_cols = std::min(cols, period_cols);
    if (rows == 0 || cols == 0) return matrix_equal(rows, cols, 0);
    const auto pattern = matrix_uniform(pattern_rows, pattern_cols, lo, hi, rng);
    std::vector<std::vector<long long>> result(
        static_cast<std::size_t>(rows), std::vector<long long>(static_cast<std::size_t>(cols))
    );
    for (int i = 0; i < rows; ++i) for (int j = 0; j < cols; ++j) {
        result[static_cast<std::size_t>(i)][static_cast<std::size_t>(j)]
            = pattern[static_cast<std::size_t>(i % period_rows)][static_cast<std::size_t>(j % period_cols)];
    }
    return result;
}

inline std::vector<std::vector<long long>> matrix_periodic(
    int rows, int cols, int period, long long lo, long long hi, Rng& rng
) {
    return matrix_periodic(rows, cols, period, period, lo, hi, rng);
}

inline std::vector<std::vector<long long>> matrix_runs(
    int rows, int cols, int run_count, long long lo, long long hi, Rng& rng
) {
    const int count = matrix_cell_count(rows, cols, "matrix_runs");
    if (count == 0) {
        if (run_count != 0) throw std::invalid_argument("empty matrix requires run_count == 0");
        return matrix_equal(rows, cols, 0);
    }
    return matrix_from_flat(rows, cols, array_runs(count, run_count, lo, hi, rng));
}

inline std::vector<std::vector<long long>> matrix_extreme(
    int rows, int cols, long long lo, long long hi, Rng& rng
) {
    const int count = matrix_cell_count(rows, cols, "matrix_extreme");
    return matrix_from_flat(rows, cols, array_extreme(count, lo, hi, rng));
}

inline std::vector<Interval> intervals_random(
    int n, long long lo, long long hi, Rng& rng
) {
    require_nonnegative_size(n, "intervals_random");
    if (lo > hi) throw std::invalid_argument("intervals_random requires lo <= hi");
    std::vector<Interval> result;
    result.reserve(static_cast<std::size_t>(n));
    for (int i = 0; i < n; ++i) {
        long long l = rng.uniform(lo, hi);
        long long r = rng.uniform(lo, hi);
        if (l > r) std::swap(l, r);
        result.push_back({l, r});
    }
    return result;
}

inline std::vector<Interval> intervals_points(int n, long long lo, long long hi, Rng& rng) {
    require_nonnegative_size(n, "intervals_points");
    std::vector<Interval> result;
    result.reserve(static_cast<std::size_t>(n));
    for (int i = 0; i < n; ++i) {
        const long long point = rng.uniform(lo, hi);
        result.push_back({point, point});
    }
    return result;
}

inline std::vector<Interval> intervals_nested(int n, long long lo, long long hi) {
    require_nonnegative_size(n, "intervals_nested");
    const std::uint64_t width = static_cast<std::uint64_t>(hi) - static_cast<std::uint64_t>(lo);
    if (lo > hi || (n > 0 && width < static_cast<std::uint64_t>(2LL * (n - 1)))) {
        throw std::invalid_argument("intervals_nested has insufficient endpoint range");
    }
    std::vector<Interval> result;
    result.reserve(static_cast<std::size_t>(n));
    for (int i = 0; i < n; ++i) result.push_back({lo + i, hi - i});
    return result;
}

inline std::vector<Interval> intervals_disjoint(int n, long long lo, long long hi) {
    require_nonnegative_size(n, "intervals_disjoint");
    const std::uint64_t span = static_cast<std::uint64_t>(hi)
        - static_cast<std::uint64_t>(lo) + 1ULL;
    if (lo > hi || (n > 0 && span != 0ULL && span < static_cast<std::uint64_t>(n))) {
        throw std::invalid_argument("intervals_disjoint has insufficient endpoint range");
    }
    std::vector<Interval> result;
    result.reserve(static_cast<std::size_t>(n));
    for (int i = 0; i < n; ++i) result.push_back({lo + i, lo + i});
    return result;
}

inline std::vector<Interval> intervals_high_overlap(
    int n, long long lo, long long hi, Rng& rng
) {
    require_nonnegative_size(n, "intervals_high_overlap");
    const long long pivot = rng.uniform(lo, hi);
    std::vector<Interval> result;
    result.reserve(static_cast<std::size_t>(n));
    for (int i = 0; i < n; ++i) {
        result.push_back({rng.uniform(lo, pivot), rng.uniform(pivot, hi)});
    }
    return result;
}

inline std::vector<Interval> intervals_endpoint_heavy(
    int n, long long lo, long long hi, Rng& rng
) {
    require_nonnegative_size(n, "intervals_endpoint_heavy");
    if (lo > hi) throw std::invalid_argument("intervals_endpoint_heavy requires lo <= hi");
    std::vector<long long> pool{lo, hi};
    if (lo < hi) {
        pool.push_back(lo + 1);
        pool.push_back(hi - 1);
    }
    std::vector<Interval> result;
    result.reserve(static_cast<std::size_t>(n));
    for (int i = 0; i < n; ++i) {
        long long l = pool[static_cast<std::size_t>(rng.bounded(static_cast<long long>(pool.size())))];
        long long r = pool[static_cast<std::size_t>(rng.bounded(static_cast<long long>(pool.size())))];
        if (l > r) std::swap(l, r);
        result.push_back({l, r});
    }
    return result;
}

inline std::uint64_t undirected_key(int u, int v) {
    if (u > v) std::swap(u, v);
    return (static_cast<std::uint64_t>(static_cast<std::uint32_t>(u)) << 32)
        | static_cast<std::uint32_t>(v);
}

inline void relabel_vertices(std::vector<Edge>& edges, int n, Rng& rng) {
    if (n < 0) throw std::invalid_argument("relabel_vertices requires n >= 0");
    std::vector<int> permutation(static_cast<std::size_t>(n));
    for (int i = 0; i < n; ++i) permutation[static_cast<std::size_t>(i)] = i + 1;
    rng.shuffle(permutation.begin(), permutation.end());
    for (Edge& edge : edges) {
        if (edge.u < 1 || edge.u > n || edge.v < 1 || edge.v > n) {
            throw std::invalid_argument("relabel_vertices endpoint outside [1,n]");
        }
        edge.u = permutation[static_cast<std::size_t>(edge.u - 1)];
        edge.v = permutation[static_cast<std::size_t>(edge.v - 1)];
    }
}

inline void shuffle_edges(std::vector<Edge>& edges, Rng& rng) {
    rng.shuffle(edges.begin(), edges.end());
}

inline void require_tree_size(int n, const char* name) {
    if (n < 1) throw std::invalid_argument(std::string(name) + " requires n >= 1");
}

inline std::vector<Edge> tree_path(int n) {
    require_tree_size(n, "tree_path");
    std::vector<Edge> result;
    result.reserve(static_cast<std::size_t>(n - 1));
    for (int v = 2; v <= n; ++v) result.push_back({v - 1, v, 0});
    return result;
}

inline std::vector<Edge> tree_star(int n) {
    require_tree_size(n, "tree_star");
    std::vector<Edge> result;
    result.reserve(static_cast<std::size_t>(n - 1));
    for (int v = 2; v <= n; ++v) result.push_back({1, v, 0});
    return result;
}

inline std::vector<Edge> tree_caterpillar(int n, int spine_length, Rng& rng) {
    require_tree_size(n, "tree_caterpillar");
    if (spine_length < 1 || spine_length > n) {
        throw std::invalid_argument("tree_caterpillar requires 1 <= spine_length <= n");
    }
    std::vector<Edge> result = tree_path(spine_length);
    result.reserve(static_cast<std::size_t>(n - 1));
    for (int v = spine_length + 1; v <= n; ++v) {
        result.push_back({1 + static_cast<int>(rng.bounded(spine_length)), v, 0});
    }
    return result;
}

inline std::vector<Edge> tree_prufer(int n, Rng& rng) {
    require_tree_size(n, "tree_prufer");
    if (n <= 2) return tree_path(n);
    std::vector<int> code(static_cast<std::size_t>(n - 2));
    std::vector<int> degree(static_cast<std::size_t>(n + 1), 1);
    for (int& value : code) {
        value = 1 + static_cast<int>(rng.bounded(n));
        ++degree[static_cast<std::size_t>(value)];
    }
    std::priority_queue<int, std::vector<int>, std::greater<int>> leaves;
    for (int v = 1; v <= n; ++v) if (degree[static_cast<std::size_t>(v)] == 1) leaves.push(v);
    std::vector<Edge> result;
    result.reserve(static_cast<std::size_t>(n - 1));
    for (int parent : code) {
        const int leaf = leaves.top();
        leaves.pop();
        result.push_back({leaf, parent, 0});
        if (--degree[static_cast<std::size_t>(parent)] == 1) leaves.push(parent);
    }
    const int first = leaves.top();
    leaves.pop();
    result.push_back({first, leaves.top(), 0});
    return result;
}

inline std::vector<Edge> tree_binary(int n) {
    require_tree_size(n, "tree_binary");
    std::vector<Edge> result;
    result.reserve(static_cast<std::size_t>(n - 1));
    for (int v = 2; v <= n; ++v) result.push_back({v / 2, v, 0});
    return result;
}

inline std::vector<Edge> tree_kary(int n, int k) {
    require_tree_size(n, "tree_kary");
    if (k < 1) throw std::invalid_argument("tree_kary requires k >= 1");
    std::vector<Edge> result;
    result.reserve(static_cast<std::size_t>(n - 1));
    for (int v = 2; v <= n; ++v) result.push_back({(v - 2) / k + 1, v, 0});
    return result;
}

inline std::vector<Edge> tree_prim_biased(int n, int elongation, Rng& rng) {
    require_tree_size(n, "tree_prim_biased");
    if (elongation < 0 || elongation > 100) {
        throw std::invalid_argument("tree_prim_biased requires 0 <= elongation <= 100");
    }
    std::vector<Edge> result;
    result.reserve(static_cast<std::size_t>(n - 1));
    int frontier = 1;
    for (int v = 2; v <= n; ++v) {
        const bool elongate = rng.bounded(100) < elongation;
        const int parent = elongate ? frontier : 1 + static_cast<int>(rng.bounded(v - 1));
        result.push_back({parent, v, 0});
        frontier = v;
    }
    return result;
}

inline std::vector<Edge> tree_prim_biased(int n, Rng& rng) {
    return tree_prim_biased(n, 70, rng);
}

inline std::vector<Edge> graph_cycle(int n) {
    if (n < 3) throw std::invalid_argument("graph_cycle requires n >= 3");
    auto result = tree_path(n);
    result.push_back({n, 1, 0});
    return result;
}

inline long long simple_edge_capacity(int n) {
    if (n < 0) throw std::invalid_argument("graph vertex count must be nonnegative");
    return static_cast<long long>(n) * (n - 1LL) / 2LL;
}

inline std::vector<long long> sample_without_replacement(
    long long universe_size, long long sample_size, Rng& rng
) {
    if (universe_size < 0 || sample_size < 0 || sample_size > universe_size) {
        throw std::invalid_argument("sample_without_replacement has an invalid size");
    }
    const bool take_complement = sample_size > universe_size / 2;
    const long long selected_size = take_complement ? universe_size - sample_size : sample_size;
    std::set<long long> selected;
    for (long long j = universe_size - selected_size; j < universe_size; ++j) {
        const long long candidate = rng.bounded(j + 1);
        if (!selected.insert(candidate).second) selected.insert(j);
    }
    std::vector<long long> result;
    result.reserve(static_cast<std::size_t>(sample_size));
    if (!take_complement) {
        result.assign(selected.begin(), selected.end());
    } else {
        auto excluded = selected.begin();
        for (long long rank = 0; rank < universe_size; ++rank) {
            if (excluded != selected.end() && *excluded == rank) {
                ++excluded;
            } else {
                result.push_back(rank);
            }
        }
    }
    rng.shuffle(result.begin(), result.end());
    return result;
}

inline std::pair<int, int> simple_edge_from_rank(int n, long long rank) {
    const long long capacity = simple_edge_capacity(n);
    if (rank < 0 || rank >= capacity) {
        throw std::invalid_argument("simple edge rank is out of range");
    }
    int lo = 0;
    int hi = n - 1;
    while (lo + 1 < hi) {
        const int mid = lo + (hi - lo) / 2;
        const long long prefix = static_cast<long long>(mid) * (2LL * n - mid - 1LL) / 2LL;
        if (prefix <= rank) lo = mid;
        else hi = mid;
    }
    const long long prefix = static_cast<long long>(lo) * (2LL * n - lo - 1LL) / 2LL;
    return {lo + 1, lo + 2 + static_cast<int>(rank - prefix)};
}

inline long long simple_edge_rank(int n, int u, int v) {
    if (u > v) std::swap(u, v);
    if (u < 1 || v > n || u == v) {
        throw std::invalid_argument("simple edge endpoint is out of range");
    }
    const long long zero_u = u - 1LL;
    return zero_u * (2LL * n - zero_u - 1LL) / 2LL + (v - u - 1LL);
}

inline long long rank_excluding_sorted(
    long long allowed_rank, long long universe_size, const std::vector<long long>& excluded
) {
    if (allowed_rank < 0 || allowed_rank >= universe_size - static_cast<long long>(excluded.size())) {
        throw std::invalid_argument("allowed edge rank is out of range");
    }
    std::size_t lo = 0;
    std::size_t hi = excluded.size();
    while (lo < hi) {
        const std::size_t mid = lo + (hi - lo) / 2;
        if (excluded[mid] - static_cast<long long>(mid) <= allowed_rank) lo = mid + 1;
        else hi = mid;
    }
    return allowed_rank + static_cast<long long>(lo);
}

inline std::vector<Edge> graph_random_simple(int n, int m, Rng& rng) {
    if (n < 0 || m < 0 || static_cast<long long>(m) > simple_edge_capacity(n)) {
        throw std::invalid_argument("graph_random_simple has an impossible n/m pair");
    }
    const auto ranks = sample_without_replacement(simple_edge_capacity(n), m, rng);
    std::vector<Edge> result;
    result.reserve(static_cast<std::size_t>(m));
    for (const long long rank : ranks) {
        const auto [u, v] = simple_edge_from_rank(n, rank);
        result.push_back({u, v, 0});
    }
    return result;
}

inline std::vector<Edge> graph_connected(int n, int m, Rng& rng) {
    require_tree_size(n, "graph_connected");
    if (m < n - 1 || static_cast<long long>(m) > simple_edge_capacity(n)) {
        throw std::invalid_argument("graph_connected has an impossible n/m pair");
    }
    auto result = tree_prim_biased(n, rng);
    std::vector<long long> tree_ranks;
    tree_ranks.reserve(result.size());
    for (const Edge& edge : result) tree_ranks.push_back(simple_edge_rank(n, edge.u, edge.v));
    std::sort(tree_ranks.begin(), tree_ranks.end());
    const long long remaining_capacity = simple_edge_capacity(n) - static_cast<long long>(tree_ranks.size());
    const auto ranks = sample_without_replacement(remaining_capacity, m - (n - 1LL), rng);
    for (const long long rank : ranks) {
        const auto [u, v] = simple_edge_from_rank(
            n, rank_excluding_sorted(rank, simple_edge_capacity(n), tree_ranks)
        );
        result.push_back({u, v, 0});
    }
    return result;
}

inline std::vector<Edge> graph_bipartite(int n_left, int n_right, int m, Rng& rng) {
    if (n_left < 0 || n_right < 0 || m < 0
        || static_cast<long long>(m) > static_cast<long long>(n_left) * n_right) {
        throw std::invalid_argument("graph_bipartite has an impossible partition/m pair");
    }
    const auto ranks = sample_without_replacement(static_cast<long long>(n_left) * n_right, m, rng);
    std::vector<Edge> result;
    result.reserve(static_cast<std::size_t>(m));
    for (const long long rank : ranks) {
        const int u = 1 + static_cast<int>(rank / n_right);
        const int v = n_left + 1 + static_cast<int>(rank % n_right);
        result.push_back({u, v, 0});
    }
    return result;
}

inline std::vector<Edge> graph_components(int n, int m, int component_count, Rng& rng) {
    if (n < 1 || component_count < 1 || component_count > n || m < n - component_count) {
        throw std::invalid_argument("graph_components has invalid n/m/component_count");
    }
    std::vector<std::vector<int>> groups(static_cast<std::size_t>(component_count));
    for (int v = 1; v <= n; ++v) groups[static_cast<std::size_t>((v - 1) % component_count)].push_back(v);
    long long capacity = 0;
    std::vector<long long> group_offsets;
    group_offsets.reserve(groups.size() + 1);
    group_offsets.push_back(0);
    for (const auto& group : groups) {
        capacity += static_cast<long long>(group.size()) * (static_cast<long long>(group.size()) - 1) / 2;
        group_offsets.push_back(capacity);
    }
    if (m > capacity) throw std::invalid_argument("graph_components m exceeds balanced partition capacity");
    std::vector<Edge> result;
    result.reserve(static_cast<std::size_t>(m));
    std::vector<long long> tree_ranks;
    tree_ranks.reserve(static_cast<std::size_t>(n - component_count));
    for (std::size_t group_index = 0; group_index < groups.size(); ++group_index) {
        const auto& group = groups[group_index];
        for (std::size_t i = 1; i < group.size(); ++i) {
            const int parent_index = static_cast<int>(rng.bounded(static_cast<long long>(i)));
            const int u = group[static_cast<std::size_t>(parent_index)];
            const int v = group[i];
            result.push_back({u, v, 0});
            tree_ranks.push_back(
                group_offsets[group_index]
                + simple_edge_rank(static_cast<int>(group.size()), parent_index + 1, static_cast<int>(i) + 1)
            );
        }
    }
    std::sort(tree_ranks.begin(), tree_ranks.end());
    const auto ranks = sample_without_replacement(
        capacity - static_cast<long long>(tree_ranks.size()),
        m - static_cast<long long>(tree_ranks.size()),
        rng
    );
    for (const long long allowed_rank : ranks) {
        const long long rank = rank_excluding_sorted(allowed_rank, capacity, tree_ranks);
        const std::size_t group_index = static_cast<std::size_t>(
            std::upper_bound(group_offsets.begin(), group_offsets.end(), rank) - group_offsets.begin() - 1
        );
        const auto [local_u, local_v] = simple_edge_from_rank(
            static_cast<int>(groups[group_index].size()), rank - group_offsets[group_index]
        );
        result.push_back({
            groups[group_index][static_cast<std::size_t>(local_u - 1)],
            groups[group_index][static_cast<std::size_t>(local_v - 1)],
            0,
        });
    }
    return result;
}

inline std::vector<Edge> graph_unicyclic(int n, Rng& rng) {
    if (n < 3) throw std::invalid_argument("graph_unicyclic requires n >= 3");
    const int cycle_size = 3 + static_cast<int>(rng.bounded(n - 2));
    std::vector<Edge> result = graph_cycle(cycle_size);
    result.reserve(static_cast<std::size_t>(n));
    for (int v = cycle_size + 1; v <= n; ++v) {
        result.push_back({1 + static_cast<int>(rng.bounded(v - 1)), v, 0});
    }
    relabel_vertices(result, n, rng);
    return result;
}

inline std::vector<Edge> graph_self_loops(int n, int m, Rng& rng) {
    if (n < 1 || m < 1) throw std::invalid_argument("graph_self_loops requires n,m >= 1");
    std::vector<Edge> result;
    result.reserve(static_cast<std::size_t>(m));
    const int loop_vertex = 1 + static_cast<int>(rng.bounded(n));
    result.push_back({loop_vertex, loop_vertex, 0});
    while (static_cast<int>(result.size()) < m) {
        result.push_back(
            {1 + static_cast<int>(rng.bounded(n)), 1 + static_cast<int>(rng.bounded(n)), 0}
        );
    }
    return result;
}

inline std::vector<Edge> graph_parallel_edges(int n, int m, Rng& rng) {
    if (n < 2 || m < 2) throw std::invalid_argument("graph_parallel_edges requires n,m >= 2");
    int u = 1 + static_cast<int>(rng.bounded(n));
    int v = 1 + static_cast<int>(rng.bounded(n - 1));
    if (v >= u) ++v;
    std::vector<Edge> result{{u, v, 0}, {u, v, 0}};
    result.reserve(static_cast<std::size_t>(m));
    while (static_cast<int>(result.size()) < m) {
        u = 1 + static_cast<int>(rng.bounded(n));
        v = 1 + static_cast<int>(rng.bounded(n - 1));
        if (v >= u) ++v;
        result.push_back({u, v, 0});
    }
    return result;
}

}  // namespace acm_recipe

#endif
