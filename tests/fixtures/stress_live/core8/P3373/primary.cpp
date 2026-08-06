#include <iostream>
#include <vector>

using i64 = long long;
using i128 = __int128_t;

struct SegmentTree {
    int n;
    i64 mod;
    std::vector<i64> sum, mul, add;
    SegmentTree(int n_, i64 mod_) : n(n_), mod(mod_), sum(4*n_), mul(4*n_, 1 % mod_), add(4*n_) {}
    i64 norm(i64 x) const { x %= mod; return x < 0 ? x + mod : x; }
    i64 product(i64 x, i64 y) const { return i64(i128(x) * y % mod); }
    void build(int node, int left, int right, const std::vector<i64>& a) {
        if (left == right) { sum[node] = norm(a[left]); return; }
        int middle = (left + right) / 2;
        build(2*node, left, middle, a); build(2*node+1, middle+1, right, a);
        sum[node] = (sum[2*node] + sum[2*node+1]) % mod;
    }
    void apply(int node, int length, i64 times, i64 plus) {
        sum[node] = (product(sum[node], times) + product(plus, length)) % mod;
        mul[node] = product(mul[node], times);
        add[node] = (product(add[node], times) + plus) % mod;
    }
    void push(int node, int left, int right) {
        if (left == right) return;
        int middle = (left + right) / 2;
        apply(2*node, middle-left+1, mul[node], add[node]);
        apply(2*node+1, right-middle, mul[node], add[node]);
        mul[node] = 1 % mod; add[node] = 0;
    }
    void update(int node, int left, int right, int ql, int qr, i64 times, i64 plus) {
        if (ql <= left && right <= qr) { apply(node, right-left+1, times, plus); return; }
        push(node, left, right);
        int middle = (left + right) / 2;
        if (ql <= middle) update(2*node, left, middle, ql, qr, times, plus);
        if (middle < qr) update(2*node+1, middle+1, right, ql, qr, times, plus);
        sum[node] = (sum[2*node] + sum[2*node+1]) % mod;
    }
    i64 query(int node, int left, int right, int ql, int qr) {
        if (ql <= left && right <= qr) return sum[node];
        push(node, left, right);
        int middle = (left + right) / 2;
        i64 answer = 0;
        if (ql <= middle) answer += query(2*node, left, middle, ql, qr);
        if (middle < qr) answer += query(2*node+1, middle+1, right, ql, qr);
        return answer % mod;
    }
};

int main() {
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);
    int n, operations;
    i64 mod;
    if (!(std::cin >> n >> operations >> mod)) return 1;
    std::vector<i64> a(n + 1);
    for (int i = 1; i <= n; ++i) std::cin >> a[i];
    SegmentTree tree(n, mod);
    tree.build(1, 1, n, a);
    while (operations--) {
        int kind, left, right;
        std::cin >> kind >> left >> right;
        if (kind == 1 || kind == 2) {
            i64 value; std::cin >> value; value = tree.norm(value);
            tree.update(1, 1, n, left, right, kind == 1 ? value : 1 % mod,
                        kind == 2 ? value : 0);
        } else {
            std::cout << tree.query(1, 1, n, left, right) << '\n';
        }
    }
    return 0;
}
