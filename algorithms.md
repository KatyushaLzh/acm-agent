# TOC

[TOC]

# 图论

## 树上路径分解示例

- Source: `OJ000A`
- Model: 将全局路径问题拆成若干互不重叠的局部区间。
- Invariant / correctness: 每个区间恰好覆盖一次，合并结果与原路径等价。
- Implementation: 维护端点和父子关系，按固定顺序合并局部答案。
- Complexity: 预处理 $O(n \log n)$，单次查询 $O(\log n)$。
- Pitfalls: 明确端点是否重复计入，并固定根节点和空区间约定。
