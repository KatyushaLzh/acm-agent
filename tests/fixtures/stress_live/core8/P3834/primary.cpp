#include <algorithm>
#include <iostream>
#include <vector>

struct Node { int left = 0, right = 0, count = 0; };

int main() {
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);
    int n, m;
    if (!(std::cin >> n >> m)) return 1;
    std::vector<long long> a(n + 1), values;
    values.reserve(n);
    for (int i = 1; i <= n; ++i) {
        std::cin >> a[i];
        values.push_back(a[i]);
    }
    std::sort(values.begin(), values.end());
    values.erase(std::unique(values.begin(), values.end()), values.end());
    std::vector<Node> tree(1);
    tree.reserve(static_cast<std::size_t>(n) * 20 + 1);
    auto clone = [&](int node) { tree.push_back(tree[node]); return int(tree.size()) - 1; };
    auto insert = [&](auto&& self, int old, int left, int right, int pos) -> int {
        int node = clone(old);
        ++tree[node].count;
        if (left != right) {
            int middle = (left + right) / 2;
            if (pos <= middle) tree[node].left = self(self, tree[old].left, left, middle, pos);
            else tree[node].right = self(self, tree[old].right, middle + 1, right, pos);
        }
        return node;
    };
    std::vector<int> roots(n + 1);
    for (int i = 1; i <= n; ++i) {
        int pos = int(std::lower_bound(values.begin(), values.end(), a[i]) - values.begin());
        roots[i] = insert(insert, roots[i - 1], 0, int(values.size()) - 1, pos);
    }
    auto kth = [&](auto&& self, int before, int after, int left, int right, int k) -> int {
        if (left == right) return left;
        int count_left = tree[tree[after].left].count - tree[tree[before].left].count;
        int middle = (left + right) / 2;
        if (k <= count_left) return self(self, tree[before].left, tree[after].left, left, middle, k);
        return self(self, tree[before].right, tree[after].right, middle + 1, right, k - count_left);
    };
    while (m--) {
        int left, right, k;
        std::cin >> left >> right >> k;
        int rank = kth(kth, roots[left - 1], roots[right], 0, int(values.size()) - 1, k);
        std::cout << values[rank] << '\n';
    }
    return 0;
}
