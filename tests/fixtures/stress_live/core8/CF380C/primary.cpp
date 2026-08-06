#include <iostream>
#include <string>
#include <vector>

struct Node { int pairs = 0, open = 0, close = 0; };

Node combine(Node left, Node right) {
    int joined = std::min(left.open, right.close);
    return {left.pairs + right.pairs + joined,
            left.open + right.open - joined,
            left.close + right.close - joined};
}

int main() {
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);
    std::string s;
    if (!(std::cin >> s)) return 1;
    int size = 1;
    while (size < int(s.size())) size <<= 1;
    std::vector<Node> tree(2 * size);
    for (int i = 0; i < int(s.size()); ++i) {
        if (s[i] == '(') tree[size + i].open = 1;
        else tree[size + i].close = 1;
    }
    for (int i = size - 1; i; --i) tree[i] = combine(tree[2 * i], tree[2 * i + 1]);
    int q;
    std::cin >> q;
    while (q--) {
        int left, right;
        std::cin >> left >> right;
        --left;
        left += size; right += size;
        Node before, after;
        while (left < right) {
            if (left & 1) before = combine(before, tree[left++]);
            if (right & 1) after = combine(tree[--right], after);
            left >>= 1; right >>= 1;
        }
        std::cout << 2 * combine(before, after).pairs << '\n';
    }
    return 0;
}
