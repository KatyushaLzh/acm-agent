# CF380C Sereja and Brackets

A non-empty string `s` consists only of `(` and `)`. For each query interval
`[l,r]`, find the maximum length of a correct bracket subsequence of the
substring `s[l..r]`. A subsequence keeps relative order but may delete
characters. Print the length, not the number of pairs.

## Input

- First line: the bracket string `s`.
- Second line: integer `q`.
- Next `q` lines: `l r`, using one-based inclusive indices.

## Output

For every query print one even integer on its own line.

## Constraints

- `1 <= |s| <= 10^6`.
- `0 <= q <= 100000`.
- `1 <= l <= r <= |s|`.
