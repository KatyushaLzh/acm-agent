# P2596 Bookshelf

The shelf contains exactly the distinct books `1..n` in a mutable top-to-bottom
order. Process `m` operations in sequence. Every position and legality check is
against the current order after all preceding operations.

- `Top s`: move book `s` to the top.
- `Bottom s`: move book `s` to the bottom.
- `Insert s t`: move book `s` by signed displacement `t`, where
  `t` is exactly `-1`, `0`, or `1`. `-1` swaps it with the book immediately
  above, `1` swaps it with the book immediately below, and `0` changes nothing.
  The command is guaranteed not to cross the current top or bottom boundary.
- `Ask s`: print the number of books currently above book `s`. This answer is
  zero-based: the top book answers `0`.
- `Query k`: print the book currently at one-based position `k`. The top
  position is `1`.

## Input

- First line: `n m`.
- Second line: a permutation of `1..n`, from top to bottom.
- Next `m` lines: one operation in the grammar above.

## Constraints

- `1 <= n <= 80000`, `0 <= m <= 80000`.
- Book arguments are in `1..n`; query positions are in `1..n`.
- Every `Insert` displacement belongs to `{-1,0,1}` and is dynamically legal.
