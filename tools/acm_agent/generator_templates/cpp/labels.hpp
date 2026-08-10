// SPDX-License-Identifier: MIT
// Weight/value policies. rng.hpp and structures.hpp must be inlined first.
#ifndef ACM_RECIPE_LABELS_HPP
#define ACM_RECIPE_LABELS_HPP

#include <algorithm>
#include <cstdint>
#include <limits>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

namespace acm_recipe {

inline std::vector<long long> label_values(
    int count, const std::string& policy, long long lo, long long hi, Rng& rng
) {
    require_nonnegative_size(count, "label_values");
    if (lo > hi) throw std::invalid_argument("label_values requires lo <= hi");
    if (policy == "uniform") return array_uniform(count, lo, hi, rng);
    if (policy == "equal") return array_equal(count, rng.uniform(lo, hi));
    if (policy == "extreme") return array_extreme(count, lo, hi, rng);
    if (policy == "monotone") return array_monotone(count, lo, hi, true, rng);

    const std::uint64_t span = static_cast<std::uint64_t>(hi)
        - static_cast<std::uint64_t>(lo) + 1ULL;
    if (policy == "distinct" || policy == "permutation") {
        if (span != 0ULL && static_cast<std::uint64_t>(count) > span) {
            throw std::invalid_argument("distinct/permutation labels exceed the value domain");
        }
        std::vector<long long> result;
        result.reserve(static_cast<std::size_t>(count));
        if (span <= 1000000ULL) {
            std::vector<long long> domain;
            domain.reserve(static_cast<std::size_t>(span));
            for (std::uint64_t offset = 0; offset < span; ++offset) {
                domain.push_back(static_cast<long long>(static_cast<std::uint64_t>(lo) + offset));
            }
            rng.shuffle(domain.begin(), domain.end());
            result.assign(domain.begin(), domain.begin() + count);
        } else {
            std::set<long long> chosen;
            while (static_cast<int>(chosen.size()) < count) chosen.insert(rng.uniform(lo, hi));
            result.assign(chosen.begin(), chosen.end());
            rng.shuffle(result.begin(), result.end());
        }
        if (policy == "distinct") std::sort(result.begin(), result.end());
        return result;
    }
    if (policy == "layered") {
        const int layer_count = span == 0ULL
            ? 4
            : static_cast<int>(std::min<std::uint64_t>(4, span));
        const std::uint64_t max_offset = span == 0ULL
            ? std::numeric_limits<std::uint64_t>::max()
            : span - 1ULL;
        std::vector<long long> layers;
        layers.reserve(static_cast<std::size_t>(layer_count));
        for (int i = 0; i < layer_count; ++i) {
            const std::uint64_t denominator = static_cast<std::uint64_t>(layer_count - 1);
            const std::uint64_t offset = layer_count == 1
                ? 0ULL
                : (max_offset / denominator) * static_cast<std::uint64_t>(i)
                    + (max_offset % denominator) * static_cast<std::uint64_t>(i) / denominator;
            layers.push_back(static_cast<long long>(static_cast<std::uint64_t>(lo) + offset));
        }
        std::vector<long long> result(static_cast<std::size_t>(count));
        for (int i = 0; i < count; ++i) {
            result[static_cast<std::size_t>(i)] = layers[static_cast<std::size_t>(i % layer_count)];
        }
        rng.shuffle(result.begin(), result.end());
        return result;
    }
    throw std::invalid_argument("unknown label policy: " + policy);
}

inline void label_edges(
    std::vector<Edge>& edges,
    const std::string& policy,
    long long lo,
    long long hi,
    Rng& rng
) {
    const auto values = label_values(static_cast<int>(edges.size()), policy, lo, hi, rng);
    for (std::size_t i = 0; i < edges.size(); ++i) edges[i].w = values[i];
}

}  // namespace acm_recipe

#endif
