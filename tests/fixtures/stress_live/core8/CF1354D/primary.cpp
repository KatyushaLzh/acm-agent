#include <algorithm>
#include <iostream>
#include <vector>

struct Fenwick {
    std::vector<int> bit;
    explicit Fenwick(int n) : bit(n + 1) {}
    void add(int index, int delta) { for (++index; index < int(bit.size()); index += index & -index) bit[index] += delta; }
    int kth(int k) const {
        int index = 0;
        int step = 1;
        while ((step << 1) < int(bit.size())) step <<= 1;
        for (; step; step >>= 1) {
            int next = index + step;
            if (next < int(bit.size()) && bit[next] < k) { index = next; k -= bit[next]; }
        }
        return index;
    }
};

int main() {
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);
    int n, q;
    if (!(std::cin >> n >> q)) return 1;
    std::vector<int> initial(n), commands(q), values;
    values.reserve(n + q);
    for (int& value : initial) { std::cin >> value; values.push_back(value); }
    for (int& command : commands) {
        std::cin >> command;
        if (command > 0) values.push_back(command);
    }
    std::sort(values.begin(), values.end());
    values.erase(std::unique(values.begin(), values.end()), values.end());
    Fenwick tree(int(values.size()));
    int count = 0;
    auto insert = [&](int value) {
        int rank = int(std::lower_bound(values.begin(), values.end(), value) - values.begin());
        tree.add(rank, 1); ++count;
    };
    for (int value : initial) insert(value);
    for (int command : commands) {
        if (command > 0) insert(command);
        else { int rank = tree.kth(-command); tree.add(rank, -1); --count; }
    }
    std::cout << (count ? values[tree.kth(1)] : 0) << '\n';
    return 0;
}
