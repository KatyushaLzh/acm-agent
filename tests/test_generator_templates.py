from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class GeneratorTemplatePropertyTests(unittest.TestCase):
    def test_all_primitive_families_hold_invariants_for_256_seeds(self) -> None:
        compiler = shutil.which("g++")
        if compiler is None:
            self.skipTest("g++ is unavailable")
        root = Path("tools/acm_agent/generator_templates/cpp")
        assets = "\n".join(
            (root / name).read_text(encoding="utf-8")
            for name in ("rng.hpp", "structures.hpp", "labels.hpp", "serializers.hpp")
        )
        checks = r'''
#include <cassert>
#include <chrono>
#include <sstream>
using namespace acm_recipe;

static int components(int n, const std::vector<Edge>& edges) {
    std::vector<int> parent(n + 1);
    for (int i = 1; i <= n; ++i) parent[i] = i;
    std::function<int(int)> find = [&](int x) { return parent[x] == x ? x : parent[x] = find(parent[x]); };
    for (const auto& edge : edges) {
        assert(1 <= edge.u && edge.u <= n && 1 <= edge.v && edge.v <= n && edge.u != edge.v);
        int a = find(edge.u), b = find(edge.v);
        if (a != b) parent[a] = b;
    }
    std::set<int> roots;
    for (int i = 1; i <= n; ++i) roots.insert(find(i));
    return static_cast<int>(roots.size());
}

static void simple(const std::vector<Edge>& edges) {
    std::set<std::pair<int, int>> seen;
    for (auto edge : edges) {
        if (edge.u > edge.v) std::swap(edge.u, edge.v);
        assert(seen.insert({edge.u, edge.v}).second);
    }
}

static void simple_linear(int n, const std::vector<Edge>& edges) {
    std::vector<unsigned char> seen(static_cast<std::size_t>(n) * n);
    for (auto edge : edges) {
        assert(1 <= edge.u && edge.u <= n && 1 <= edge.v && edge.v <= n && edge.u != edge.v);
        if (edge.u > edge.v) std::swap(edge.u, edge.v);
        const std::size_t key = static_cast<std::size_t>(edge.u - 1) * n + (edge.v - 1);
        assert(!seen[key]);
        seen[key] = 1;
    }
}

static void tree_ok(int n, const std::vector<Edge>& edges) {
    assert(static_cast<int>(edges.size()) == n - 1);
    simple(edges);
    assert(components(n, edges) == 1);
}

int main() {
    for (unsigned long long seed = 0; seed < 256; ++seed) {
        Rng rng(seed);
        auto a0 = array_uniform(24, -5, 5, rng); assert(a0.size() == 24);
        auto a1 = array_permutation(24, 3, rng); assert(std::set<long long>(a1.begin(), a1.end()).size() == 24);
        auto a2 = array_equal(24, 7); assert(std::set<long long>(a2.begin(), a2.end()).size() == 1);
        auto a3 = array_few_values(24, 3, -5, 5, rng); assert(std::set<long long>(a3.begin(), a3.end()).size() <= 3);
        auto a4 = array_monotone(24, -5, 5, true, rng); assert(std::is_sorted(a4.begin(), a4.end()));
        auto a5 = array_periodic(24, 3, -5, 5, rng); for (int i = 3; i < 24; ++i) assert(a5[i] == a5[i - 3]);
        auto a6 = array_runs(24, 4, -5, 5, rng); assert(a6.size() == 24);
        auto a7 = array_extreme(24, -5, 5, rng); for (auto x : a7) assert(x == -5 || x == 5);

        auto s0 = string_uniform(24, "abcd", rng); assert(s0.size() == 24);
        auto s1 = string_equal(24, 'x'); assert(s1 == std::string(24, 'x'));
        auto s2 = string_few_chars(24, "abcd", 2, rng); assert(std::set<char>(s2.begin(), s2.end()).size() <= 2);
        auto s3 = string_monotone(24, "abcd", true, rng); assert(std::is_sorted(s3.begin(), s3.end()));
        auto s4 = string_permutation("abcdefghijkl", rng); assert(std::set<char>(s4.begin(), s4.end()).size() == 12);
        auto s5 = string_extreme(24, "abcd", rng); for (char ch : s5) assert(ch == 'a' || ch == 'd');
        auto s6 = string_periodic(24, "abc"); for (int i = 3; i < 24; ++i) assert(s6[i] == s6[i - 3]);
        auto s7 = string_runs(24, 4, "abcd", rng); assert(s7.size() == 24);

        auto m0 = matrix_uniform(7, 9, -5, 5, rng); assert(m0.size() == 7 && m0[0].size() == 9);
        auto m1 = matrix_equal(7, 9, 1); assert(m1[6][8] == 1);
        auto m2 = matrix_few_values(7, 9, 3, -5, 5, rng); assert(m2.size() == 7);
        auto m3 = matrix_monotone(7, 9, -5, 5, true, rng); assert(m3.size() == 7);
        auto m4 = matrix_permutation(7, 9, 1, rng); assert(m4.size() == 7);
        auto m5 = matrix_periodic(7, 9, 2, 3, -5, 5, rng); assert(m5[6][8] == m5[0][2]);
        auto m6 = matrix_runs(7, 9, 4, -5, 5, rng); assert(m6.size() == 7);
        auto m7 = matrix_extreme(7, 9, -5, 5, rng); for (auto& row : m7) for (auto x : row) assert(x == -5 || x == 5);

        for (const auto& xs : {intervals_random(12, 0, 30, rng), intervals_points(12, 0, 30, rng),
                               intervals_nested(12, 0, 30), intervals_disjoint(12, 0, 30),
                               intervals_high_overlap(12, 0, 30, rng), intervals_endpoint_heavy(12, 0, 30, rng)}) {
            assert(xs.size() == 12); for (auto x : xs) assert(0 <= x.l && x.l <= x.r && x.r <= 30);
        }

        for (const auto& edges : {tree_path(24), tree_star(24), tree_caterpillar(24, 8, rng),
                                  tree_prufer(24, rng), tree_binary(24), tree_kary(24, 4),
                                  tree_prim_biased(24, 0, rng), tree_prim_biased(24, 100, rng)}) tree_ok(24, edges);

        auto g0 = graph_random_simple(16, 30, rng); assert(g0.size() == 30); simple(g0);
        auto g1 = graph_connected(16, 30, rng); assert(g1.size() == 30 && components(16, g1) == 1); simple(g1);
        auto g2 = graph_components(16, 20, 4, rng); assert(g2.size() == 20 && components(16, g2) == 4); simple(g2);
        auto g3 = graph_bipartite(8, 9, 30, rng); assert(g3.size() == 30); simple(g3); for (auto e : g3) assert(e.u <= 8 && e.v > 8);
        auto g4 = graph_cycle(16); assert(g4.size() == 16 && components(16, g4) == 1); simple(g4);
        auto g5 = graph_unicyclic(16, rng); assert(g5.size() == 16 && components(16, g5) == 1); simple(g5);
        auto g6 = graph_self_loops(16, 30, rng); assert(g6.size() == 30); assert(std::any_of(g6.begin(), g6.end(), [](auto e){ return e.u == e.v; }));
        auto g7 = graph_parallel_edges(16, 30, rng); assert(g7.size() == 30); std::multiset<std::pair<int,int>> pairs; for(auto e:g7){if(e.u>e.v)std::swap(e.u,e.v);pairs.insert({e.u,e.v});} assert(std::any_of(pairs.begin(),pairs.end(),[&](auto p){return pairs.count(p)>=2;}));

        for (const char* policy : {"uniform", "equal", "distinct", "layered", "monotone", "extreme", "permutation"}) {
            auto values = label_values(16, policy, 1, 100, rng); assert(values.size() == 16);
            for (auto value : values) assert(1 <= value && value <= 100);
        }

        std::ostringstream out;
        serialize_list_n(a0, out); serialize_string_n(s0, out); serialize_matrix_nm(m0, out);
        serialize_intervals_n(intervals_random(3, 0, 3, rng), out);
        serialize_edge_list_n_m(16, g0, out); label_edges(g0, "layered", 1, 4, rng);
        serialize_weighted_edge_list_n_m(16, g0, out); assert(!out.str().empty());
    }

    std::vector<long long> p1111_micros;
    for (unsigned long long seed_base : {200001ULL, 300001ULL}) {
        for (unsigned long long seed = seed_base; seed < seed_base + 20; ++seed) {
            Rng rng(seed);
            constexpr int n = 96;
            constexpr int complete = n * (n - 1) / 2;
            auto g0 = graph_random_simple(n, complete - 1, rng);
            assert(static_cast<int>(g0.size()) == complete - 1); simple_linear(n, g0);
            auto g1 = graph_connected(n, complete - 1, rng);
            assert(static_cast<int>(g1.size()) == complete - 1 && components(n, g1) == 1); simple_linear(n, g1);
            auto g2 = graph_components(n, 2 * 48 * 47 / 2 - 1, 2, rng);
            assert(static_cast<int>(g2.size()) == 2 * 48 * 47 / 2 - 1 && components(n, g2) == 2); simple_linear(n, g2);
            auto g3 = graph_bipartite(48, 48, 48 * 48 - 1, rng);
            assert(static_cast<int>(g3.size()) == 48 * 48 - 1); simple_linear(n, g3);
            for (auto edge : g3) assert(edge.u <= 48 && edge.v > 48);

            const auto started = std::chrono::steady_clock::now();
            auto p1111 = graph_components(1000, 249499, 2, rng);
            const auto elapsed = std::chrono::steady_clock::now() - started;
            assert(p1111.size() == 249499 && components(1000, p1111) == 2);
            simple_linear(1000, p1111);
            assert(elapsed < std::chrono::seconds(1));
            p1111_micros.push_back(std::chrono::duration_cast<std::chrono::microseconds>(elapsed).count());
        }
    }
    std::sort(p1111_micros.begin(), p1111_micros.end());
    assert(p1111_micros[37] < 200000);
}
'''
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "template_properties.cpp"
            executable = Path(temp) / "template_properties.exe"
            source.write_text(assets + checks, encoding="utf-8")
            built = subprocess.run(
                [compiler, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", str(source), "-o", str(executable)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            ran = subprocess.run([str(executable)], capture_output=True, text=True, timeout=60)
            self.assertEqual(ran.returncode, 0, ran.stderr)


if __name__ == "__main__":
    unittest.main()
