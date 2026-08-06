#include <algorithm>
#include <iostream>
#include <numeric>
#include <tuple>
#include <vector>

struct Dsu {
    std::vector<int> p, size;
    explicit Dsu(int n) : p(n), size(n, 1) { std::iota(p.begin(), p.end(), 0); }
    int find(int x) { return p[x] == x ? x : p[x] = find(p[x]); }
    bool join(int x, int y) {
        x = find(x); y = find(y);
        if (x == y) return false;
        if (size[x] < size[y]) std::swap(x, y);
        p[y] = x; size[x] += size[y];
        return true;
    }
};

int main() {
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);
    int n, m;
    if (!(std::cin >> n >> m)) return 1;
    std::vector<std::tuple<int, int, int>> edges;
    edges.reserve(m);
    for (int i = 0, x, y, t; i < m; ++i) {
        std::cin >> x >> y >> t;
        edges.emplace_back(t, x - 1, y - 1);
    }
    if (n == 1) {
        std::cout << 0 << '\n';
        return 0;
    }
    std::sort(edges.begin(), edges.end());
    Dsu dsu(n);
    int components = n;
    for (auto [t, x, y] : edges) {
        if (dsu.join(x, y) && --components == 1) {
            std::cout << t << '\n';
            return 0;
        }
    }
    std::cout << -1 << '\n';
    return 0;
}
