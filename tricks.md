# TOC

[TOC]

# Optimization

## 从较小一侧枚举

- Source: `OJ000B`

- Trigger: 一次操作把规模拆成两部分，直接扫描整个区间可能退化为平方复杂度。

- Conclusion / Proof: 只支付较小部分的代价；代价可由势能下降吸收，从而得到对数层数上的摊还上界。

- Implementation: 每次比较两侧规模，从较短的一侧扫描或迁移元素。

- Pitfalls: 需要证明每个元素被收费的次数有界，不能仅凭“看起来更短”断言复杂度。
