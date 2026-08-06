#include <iostream>
#include <vector>

int main() {
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);
    int n, m, root;
    if (!(std::cin >> n >> m >> root)) return 1;
    std::vector<std::vector<int>> graph(n + 1);
    for (int i = 1, u, v; i < n; ++i) {
        std::cin >> u >> v;
        graph[u].push_back(v);
        graph[v].push_back(u);
    }
    int log = 1;
    while ((1LL << log) <= n) ++log;
    std::vector<std::vector<int>> up(log, std::vector<int>(n + 1));
    std::vector<int> depth(n + 1), order{root};
    up[0][root] = 0;
    for (std::size_t i = 0; i < order.size(); ++i) {
        int u = order[i];
        for (int v : graph[u]) if (v != up[0][u]) {
            up[0][v] = u;
            depth[v] = depth[u] + 1;
            order.push_back(v);
        }
    }
    for (int bit = 1; bit < log; ++bit) {
        for (int v = 1; v <= n; ++v) up[bit][v] = up[bit - 1][up[bit - 1][v]];
    }
    auto lca = [&](int u, int v) {
        if (depth[u] < depth[v]) std::swap(u, v);
        int delta = depth[u] - depth[v];
        for (int bit = 0; bit < log; ++bit) if (delta >> bit & 1) u = up[bit][u];
        if (u == v) return u;
        for (int bit = log - 1; bit >= 0; --bit) if (up[bit][u] != up[bit][v]) {
            u = up[bit][u];
            v = up[bit][v];
        }
        return up[0][u];
    };
    while (m--) {
        int u, v;
        std::cin >> u >> v;
        std::cout << lca(u, v) << '\n';
    }
    return 0;
}
