// SPDX-License-Identifier: MIT
// Deterministic local generator primitive. No runtime or quoted-include dependency.
#ifndef ACM_RECIPE_RNG_HPP
#define ACM_RECIPE_RNG_HPP

#include <cstdint>
#include <iterator>
#include <stdexcept>
#include <utility>

namespace acm_recipe {

struct Rng {
    explicit Rng(std::uint64_t seed) : state(seed) {}

    std::uint64_t next() {
        std::uint64_t z = (state += 0x9e3779b97f4a7c15ULL);
        z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
        z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
        return z ^ (z >> 31);
    }

    long long bounded(long long bound) {
        if (bound <= 0) {
            throw std::invalid_argument("Rng::bounded requires a positive bound");
        }
        const std::uint64_t modulus = static_cast<std::uint64_t>(bound);
        const std::uint64_t threshold = (0ULL - modulus) % modulus;
        for (;;) {
            const std::uint64_t value = next();
            if (value >= threshold) {
                return static_cast<long long>(value % modulus);
            }
        }
    }

    long long uniform(long long lo, long long hi) {
        if (lo > hi) {
            throw std::invalid_argument("Rng::uniform requires lo <= hi");
        }
        const std::uint64_t span = static_cast<std::uint64_t>(hi)
            - static_cast<std::uint64_t>(lo) + 1ULL;
        if (span == 0ULL) {
            return static_cast<long long>(next());
        }
        if (span > static_cast<std::uint64_t>(0x7fffffffffffffffULL)) {
            const std::uint64_t threshold = (0ULL - span) % span;
            for (;;) {
                const std::uint64_t value = next();
                if (value >= threshold) {
                    return static_cast<long long>(static_cast<std::uint64_t>(lo) + value % span);
                }
            }
        }
        return lo + bounded(static_cast<long long>(span));
    }

    template <class RandomAccessIterator>
    void shuffle(RandomAccessIterator first, RandomAccessIterator last) {
        using Difference = typename std::iterator_traits<RandomAccessIterator>::difference_type;
        const Difference size = last - first;
        for (Difference i = size; i > 1; --i) {
            const Difference j = static_cast<Difference>(bounded(static_cast<long long>(i)));
            using std::swap;
            swap(first[i - 1], first[j]);
        }
    }

private:
    std::uint64_t state;
};

}  // namespace acm_recipe

#endif
