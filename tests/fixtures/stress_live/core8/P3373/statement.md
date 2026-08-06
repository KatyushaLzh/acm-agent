# P3373 Range Multiply, Range Add, Range Sum

Maintain an array under a positive modulus `p`. Operations are processed in
order and all stored values and answers are reduced to the canonical range
`[0,p-1]`.

- `1 l r k`: multiply every `a_i` in `[l,r]` by `k` modulo `p`.
- `2 l r k`: add `k` to every `a_i` in `[l,r]` modulo `p`.
- `3 l r`: print the sum of `a_l..a_r` modulo `p`.

Intervals use one-based inclusive indices. The modulus may be `1`, in which
case every value and answer is zero.

## Input

- First line: `n m p`.
- Second line: `n` integers `a_i`.
- Next `m` lines: one operation in the grammar above.

## Constraints

- `1 <= n <= 100000`, `0 <= m <= 100000`.
- `1 <= p <= 10^9`.
- `1 <= l <= r <= n`.
- Array values and operation values fit signed 64-bit integers.
