# P3379 Lowest Common Ancestor

An undirected graph is guaranteed to be a tree with vertices `1..n`. It is
rooted at vertex `s`. For every query `(a,b)`, print the lowest common ancestor
of `a` and `b` in this rooted tree. A vertex is its own ancestor.

## Input

- First line: `n m s`.
- Next `n-1` lines: one undirected tree edge `u v`.
- Next `m` lines: one query `a b`.

## Output

For each query, print one vertex number on its own line.

## Constraints

- `1 <= n,m <= 500000` (the benchmark also permits `m=0`).
- `1 <= s,u,v,a,b <= n`.
- The `n-1` edges form one connected acyclic tree.
