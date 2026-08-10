# TOC

[TOC]

# Optimization

## 分治找根时从两边扫的 O(n log n) 摊还

- Source: [CF 2234E](https://codeforces.com/contest/2234/problem/E)

- Trigger: 分治处理区间 `[l, r]` 时，需要找满足 `a[i] = (i - l + 1) * (r - i + 1)` 的根；若直接从左到右扫，最坏会退化到 `O(n^2)`。

- Conclusion / Proof:

  改成按 `l, r, l+1, r-1, ...` 从两端往中间扫。若根在 `x`，本层扫描代价是 `O(min(x-l+1, r-x+1)) = O(min(L, R) + 1)`，其中 `L = x-l, R = r-x`。

  摊还核心: 递推变成 `T(n) = T(L) + T(R) + O(min(L, R) + 1)`，而不是 `T(n) = T(L) + T(R) + O(n)`。

  直觉: 根靠边时这一层很便宜；根不靠边时虽然这一层较贵，但会把区间切得更均衡，后续层数变少。花得多的层不会出现太多次。两端扫的代价只和较小子区间成正比，而 `n log n - L log L - R log R` 正好能吸收 `min(L, R)`，因此总复杂度 `O(n log n)`。
