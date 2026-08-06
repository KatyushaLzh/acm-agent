# P3834 Static Range K-th Smallest

An immutable array `a[1..n]` is given. Each query contains `l r k` and asks for
the `k`-th smallest value in the multiset `a[l],a[l+1],...,a[r]`. Duplicate
values occupy separate ranks.

## Input

- First line: `n m`.
- Second line: `n` signed integers.
- Next `m` lines: `l r k`.

## Output

For each query, print the requested value on its own line.

## Constraints

- `1 <= n <= 200000`.
- `0 <= m <= 200000`.
- `-10^9 <= a_i <= 10^9`.
- `1 <= l <= r <= n` and `1 <= k <= r-l+1`.
