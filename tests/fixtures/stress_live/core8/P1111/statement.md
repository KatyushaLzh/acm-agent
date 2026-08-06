# P1111 Repairing Roads

There are `n` villages and `m` roads. Initially no road is usable. Road `i`
connects villages `x_i` and `y_i` and becomes usable at integer time `t_i`.
Roads are undirected. At a time `T`, every road with `t_i <= T` is usable.

Print the earliest time at which every village is connected through usable
roads. Print `-1` if this never happens. A single village is already connected,
so for `n=1` print `0`. Parallel roads and self-loops are harmless and allowed.

## Input

- First line: `n m`.
- Next `m` lines: `x_i y_i t_i`.

## Output

One integer: the earliest connection time, `0` for one village, or `-1`.

## Constraints

- `1 <= n <= 1000`.
- `0 <= m <= 100000`.
- `1 <= x_i,y_i <= n`.
- `1 <= t_i <= 100000`.
