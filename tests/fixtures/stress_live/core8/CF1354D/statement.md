# CF1354D Multiset

Maintain a multiset of positive integers. It starts with `n` values and then
receives `q` commands, each encoded by one signed integer `x`:

- If `x>0`, insert value `x`.
- If `x<0`, let `k=-x` and delete exactly one occurrence of the current
  `k`-th smallest value. Every deletion is guaranteed legal at that moment.

After all commands, print the smallest remaining value, or `0` if the multiset
is empty. Equal values are separate occurrences.

## Input

- First line: `n q`.
- Second logical record: `n` positive initial values (empty when `n=0`).
- Then exactly `q` signed command integers, with arbitrary whitespace.

## Constraints

- `0 <= n,q <= 1000000`.
- Every inserted or initial value is positive and at most `10^6`.
- For every negative command, `1 <= -x <= current multiset size`.
