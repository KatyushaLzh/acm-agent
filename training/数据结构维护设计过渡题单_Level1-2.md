# 数据结构维护设计过渡题单（Level 1–2）

> 定位：难度整体高于 `README.md` 中以恢复、模板和典型应用为主的训练，低于《数据结构应用强化题单》中 CF526F、CF997E、CF1416D、CF576E、CF938G 一类需要多层建模或多结构组合的题目。
>
> 前置：默认已经掌握树状数组、线段树、FHQ Treap、主席树、分块、莫队、树链剖分、DSU on Tree、并查集、Trie、线性基等已有内容；**不要求 LCT，也不扩展新的数据结构家族**。
>
> 题目：24 道主线题 + 4 道上界验收题。与 `README.md` 和《数据结构应用强化题单》均无重复。
>
> Codeforces 评分记录于 **2026-08-17**，仅作工作量参考；真正的分级依据是“需要独立完成几步建模”。

---

## 1. 本题单中的 Level 定义

### Level 1：结构可见，重点是维护字段

通常已经能判断使用线段树、树状数组、集合、Trie 或分块；主要问题是：

- 叶子表示什么；
- 区间节点保存哪些字段；
- 左右区间怎样合并；
- 单次修改为什么只影响局部信息。

目标：大部分题在 **45～60 分钟内独立推出核心维护**。

### Level 1.5：需要一次轻量重述

在设计维护前，还要完成一次比较自然的变化，例如：

- 把树上操作乘上深度奇偶符号；
- 把动态集合答案改写成“总跨度减最大空隙”；
- 把询问按步长大小分类；
- 把树的子树转成 DFS 序区间。

目标：允许打开一级提示，但打开后应能自行完成算法。

### Level 2：需要一次关键转化

数据结构不是题面直接给出的，需要先完成一次核心转化，例如：

- 正向过程改成逆向恢复；
- 物理过程改成按速度排序后的二维贡献；
- 所有路径贡献改成按点权激活连通块；
- 连通一个顶点区间改成检查相邻点对。

目标：可以看核心提示，但不要直接照抄完整题解。

---

## 2. 做题规则：避免“看懂题解但没有学会”

每道新题按以下流程：

1. **前 25 分钟**：只写朴素算法、状态和瓶颈，禁止看提示。
2. **25～50 分钟**：尝试回答三个问题：
   - 单个位置或状态代表什么？
   - 新事件会改变哪些位置？
   - 这些位置能否形成区间、前缀、后缀、子树或有序集合？
3. **L1 卡到 50 分钟、L1.5 卡到 65 分钟、L2 卡到 80 分钟**：打开本文件中的一级提示。
4. 再思考 20～30 分钟；仍然不会，再打开核心提示。
5. 看过题解时，只看到“状态定义和关键转化”为止，然后关闭题解，自己写伪代码和实现。
6. 若看过完整题解，必须在本周复习日闭卷重做，不能把第一次 AC 计为完成。

建议每周的结果分布是：

- 2 题独立完成；
- 2 题在一级提示后完成；
- 2 题在核心提示或题解后完成。

这比要求 6 题全部独立更符合过渡阶段。

---

# 第一周：区间节点究竟保存什么

这一周只练一件事：**把一个区间压缩成常数个、能够合并的信息。**

| 日程 | 题目 | CF 评分 | Level | 已有工具 | 训练目标 |
|---|---|---:|---:|---|---|
| D1 | [CF1263E Editor](https://codeforces.com/problemset/problem/1263/E) | 2100 | L1 | 线段树、括号前缀和 | 动态编辑后，不维护括号串本身，而维护整条前缀函数的合法性与最大值。 |
| D2 | [CF1567E Non-Decreasing Dilemma](https://codeforces.com/problemset/problem/1567/E) | 2200 | L1 | 线段树、自定义区间信息 | 统计区间内所有非降子数组，重点是跨越左右儿子的贡献如何计算。 |
| D3 | [CF1881G Anya and the Mysterious String](https://codeforces.com/problemset/problem/1881/G) | 2000 | L1 | 区间加、集合或树状数组 | 发现“存在任意长度回文”等价于只检查长度 2 和 3，并分析区间平移只改变哪些边界。 |
| D4 | [CF1556E Equilibrium](https://codeforces.com/problemset/problem/1556/E) | 2200 | L1.5 | 前缀和、RMQ/线段树 | 把一类区间配平操作翻译成差分前缀必须满足的端点和单侧约束。 |
| D5 | [CF1478E Nezzar and Binary String](https://codeforces.com/problemset/problem/1478/E) | 1900 | L2 | 区间赋值线段树 | 正向过程存在大量选择时，逆向过程为何被区间多数唯一确定。 |
| D6 | [CF1217E Sum Queries?](https://codeforces.com/problemset/problem/1217/E) | 2300 | L2 | 线段树、十进制位分类 | 先证明最优坏子集可以缩成两个数，再决定节点只需保留哪些候选。 |
| D7 | **本周闭卷回炉** | — | — | — | 从看过提示最多的两题中选一题，20 分钟写出字段和 `merge`，再闭卷实现。 |

<details>
<summary>D1 一级提示</summary>

把当前位置字符转成 `(` 为 `+1`、`)` 为 `-1`、其他为 `0`。一次输入只是一个位置的数值被覆盖。

</details>

<details>
<summary>D1 核心提示</summary>

正确括号文本要求最终总和为 0，所有前缀和非负；最少颜色数等于最大前缀和。因此维护整条数组的 `sum`、最小前缀和、最大前缀和。

</details>

<details>
<summary>D2 一级提示</summary>

先考虑一个固定区间：所有非降子数组会被若干个极大非降段分割，每段长度为 `len` 时贡献是 `len(len+1)/2`。

</details>

<details>
<summary>D2 核心提示</summary>

节点保存左右端值、最长非降前缀、最长非降后缀、区间答案和长度。当左区间末值 `<=` 右区间首值时，新增跨界贡献为“左后缀长度 × 右前缀长度”。

</details>

<details>
<summary>D3 一级提示</summary>

任何长度至少 2 的回文都包含长度 2 或长度 3 的回文中心。因此只需要维护两类坏位置：`s[i]=s[i+1]` 与 `s[i]=s[i+2]`。

</details>

<details>
<summary>D3 核心提示</summary>

一个区间内所有字符同时循环平移时，区间内部的相等关系不变；只有修改区间两端附近常数个长度 2/3 关系会变化。另用区间加、单点查询维护字符当前偏移。

</details>

<details>
<summary>D4 一级提示</summary>

令新数组为 `c[i]=a[i]-b[i]` 或相反符号，研究一次操作对 `c` 的作用。它会在若干位置交替产生 `+1,-1,+1,-1,...`。

</details>

<details>
<summary>D4 核心提示</summary>

选择合适符号后，区间可行性可写成：区间差分总和为 0，且相对起点的所有前缀和始终位于同一侧。答案是该段前缀函数相对起点的最大偏移，因此只需区间最值与端点前缀和。

</details>

<details>
<summary>D5 一级提示</summary>

正向的某次操作结束后，所操作区间必须全相同；而最终串已知。尝试从最终串倒着恢复每次操作前的状态。

</details>

<details>
<summary>D5 核心提示</summary>

逆序处理区间 `[l,r]`。当前区间中 0 或 1 的严格多数，就是该次操作前整个区间的值；若数量相等则不可能。随后对整个区间做赋值，最后与初始串比较。

</details>

<details>
<summary>D6 一级提示</summary>

单个数一定平衡。尝试证明：若存在不平衡子集，则存在一个不平衡的二元子集。

</details>

<details>
<summary>D6 核心提示</summary>

最优答案等价于寻找两个数，使它们在某一十进制位上都非零，并最小化二者之和。每个线段树节点对每一位只需保存该位非零的最小元素，查询后枚举十个数位配对。

</details>

---

# 第二周：维护动态最优集合与离线贡献

这一周的共同问题是：**答案通常不需要维护整个过程，只需要维护决定最优解的边界或候选。**

| 日程 | 题目 | CF 评分 | Level | 已有工具 | 训练目标 |
|---|---|---:|---:|---|---|
| D8 | [CF1418D Trash Problem](https://codeforces.com/problemset/problem/1418/D) | 2100 | L1 | `set`、`multiset` | 把几何过程的最优值改写成“总跨度减去最大相邻空隙”。 |
| D9 | [CF1398E Two Types of Spells](https://codeforces.com/problemset/problem/1398/E) | 2200 | L1.5 | 两个有序集合、动态前若干大 | 动态维护应被翻倍的前 `k` 大元素，并处理“闪电不能翻倍自己”的边界。 |
| D10 | [CF899E Segments Removal](https://codeforces.com/problemset/problem/899/E) | 2000 | L1.5 | 优先队列、双向链表/并查集 | 删除最长等值段后，只可能使左右两个相邻段合并。 |
| D11 | [CF1311F Moving Points](https://codeforces.com/problemset/problem/1311/F) | 1900 | L2 | 排序、树状数组 | 先判断一对点的最小距离何时为零，再把剩余点对贡献化成二维偏序求和。 |
| D12 | [CF1514D Cut and Stick](https://codeforces.com/problemset/problem/1514/D) | 2000 | L2 | 线段树候选、位置表二分 | 区间答案只由最高频值决定；节点不保存完整频率，只保存可合并的候选。 |
| D13 | [CF1847D Professor Higashikata](https://codeforces.com/problemset/problem/1847/D) | 1900 | L2 | DSU 跳点、树状数组/线段树 | 把多个区间按给定顺序展开成“每个位置第一次出现的序列”，之后只维护该序列前缀。 |
| D14 | **本周闭卷回炉** | — | — | — | 闭卷写出 D9 的集合不变量或 D13 的重排定义，并重新实现其中一题。 |

<details>
<summary>D8 一级提示</summary>

排序后，最终保留两个坐标，相当于允许不跨越某一个相邻空隙。

</details>

<details>
<summary>D8 核心提示</summary>

当点数不少于 3 时，答案为 `maxPoint-minPoint-maxAdjacentGap`。维护所有点的有序集合和所有相邻差值的多重集合；插入或删除只改变前驱、后继附近至多三个空隙。

</details>

<details>
<summary>D9 一级提示</summary>

若当前有 `k` 个闪电法术，理想情况下应让数值最大的 `k` 个法术额外计算一次。

</details>

<details>
<summary>D9 核心提示</summary>

维护“被额外计算”的前 `k` 大集合与其余集合，并维持两集合大小和边界有序。如果前 `k` 大全部是闪电，需要用最大的未选火焰替换最小的已选闪电；不存在火焰时少翻倍一个闪电。

</details>

<details>
<summary>D10 一级提示</summary>

先把原数组压缩成极大等值段。此后每一步只删除一个段，而不是删除许多个单独元素。

</details>

<details>
<summary>D10 核心提示</summary>

优先队列按“段长降序、左端点升序”取段；用双向链表维护仍存活段。删除后若左右邻段值相同，则合并并生成一个新版本；优先队列采用版本号或存活标记懒删除。

</details>

<details>
<summary>D11 一级提示</summary>

先按初始坐标从左到右考虑点对 `i<j`。什么时候左点能追上右点？

</details>

<details>
<summary>D11 核心提示</summary>

当 `v[i]>v[j]` 时最小距离为 0；否则贡献为 `x[j]-x[i]`。扫描 `j`，对所有此前满足 `v[i]<=v[j]` 的点求 `count*x[j]-sum(x[i])`，用按速度离散化的两个树状数组维护数量与坐标和。

</details>

<details>
<summary>D12 一级提示</summary>

先证明答案只依赖区间长度与区间内最大出现次数 `cntMax`，其他值如何分布并不重要。

</details>

<details>
<summary>D12 核心提示</summary>

线段树节点用 Boyer–Moore 式抵消合并，只返回一个“可能的绝对众数候选”；再用该值的位置数组二分得到真实次数。得到 `cntMax` 后代入题目的配对结论。

</details>

<details>
<summary>D13 一级提示</summary>

依次遍历给定区间，把一个位置第一次被覆盖时加入新序列，以后重复覆盖时忽略它。

</details>

<details>
<summary>D13 核心提示</summary>

用“下一个未访问位置”并查集在线性总复杂度内建立序列 `p`。设原串 1 的总数为 `cnt1`，理想状态要求 `p` 的前 `min(cnt1,|p|)` 个位置全为 1；答案是该前缀中的 0 数。翻转只影响总 1 数以及对应序列位置的单点值。

</details>

---

# 第三周：按参数分治、坐标域与 Trie

这一周练习：**当一种统一维护太贵时，按参数大小、坐标范围或二进制前缀拆开处理。**

| 日程 | 题目 | CF 评分 | Level | 已有工具 | 训练目标 |
|---|---|---:|---:|---|---|
| D15 | [CF1207F Remainder Problem](https://codeforces.com/problemset/problem/1207/F) | 2100 | L1 | 根号分治、预处理桶 | 小模数预处理、 大模数暴力枚举，平衡修改和查询。 |
| D16 | [CF103D Time to Raid Cowavans](https://codeforces.com/problemset/problem/103/D) | 2100 | L1.5 | 根号分治、离线 DP | 询问步长较小时共享整张 DP，步长较大时单次枚举很短。 |
| D17 | [CF817E Choosing The Commander](https://codeforces.com/problemset/problem/817/E) | 2000 | L1.5 | 可删除 01-Trie | 不再查询最大异或，而是统计满足 `(x xor p)<k` 的数有多少个。 |
| D18 | [CF282E Sausage Maximization](https://codeforces.com/problemset/problem/282/E) | 2200 | L1.5 | 前后缀异或、01-Trie | 把不相交前缀与后缀的选择改成按分界点在线插入和查询。 |
| D19 | [CF915E Physical Education Lessons](https://codeforces.com/problemset/problem/915/E) | 2300 | L1.5 | 动态开点/离散化线段树、区间赋值 | 坐标范围巨大时，叶子代表的是一段真实长度，而不是一个下标。 |
| D20 | [CF785E Anton and Permutation](https://codeforces.com/problemset/problem/785/E) | 2200 | L2 | 分块、块内排序 | 交换两个位置时，只重新计算与两个端点相关的逆序对，而不重算全局。 |
| D21 | **本周闭卷回炉** | — | — | — | 写出“小参数预处理、大参数枚举”的复杂度平衡；再闭卷完成 D17 或 D20。 |

<details>
<summary>D15 一级提示</summary>

设阈值为 `B`。对查询模数 `x` 分成 `x<=B` 和 `x>B` 两类。

</details>

<details>
<summary>D15 核心提示</summary>

维护 `small[x][r] = Σ a[i]，i mod x=r`。单点修改更新所有 `x<=B` 的桶，复杂度 `O(B)`；查询 `x<=B` 直接取桶，否则枚举 `y,y+x,y+2x,...`，复杂度 `O(N/x)`。

</details>

<details>
<summary>D16 一级提示</summary>

这题没有修改。同一个步长 `b` 的所有询问可以共享一次从后往前的递推。

</details>

<details>
<summary>D16 核心提示</summary>

对小步长 `b`，计算 `dp[i]=w[i]+dp[i+b]`，同一 `b` 的询问均为 `dp[a]`；对大步长，单次直接枚举的项数不超过 `N/B`。只为实际出现的小步长建 DP 可控制内存。

</details>

<details>
<summary>D17 一级提示</summary>

从最高位向最低位比较 `(value xor p)` 与 `k`，本质与比较两个普通二进制数大小相同。

</details>

<details>
<summary>D17 核心提示</summary>

若当前 `k` 位为 0，只能沿着使异或位为 0 的儿子继续；若 `k` 位为 1，可以把使异或位为 0 的整棵子树计入答案，再沿使异或位为 1 的儿子继续。Trie 节点保存当前多重集合计数以支持删除。

</details>

<details>
<summary>D18 一级提示</summary>

枚举前缀和后缀之间的分界线。对每条分界线，允许选择的前缀集合只比上一条多一个。

</details>

<details>
<summary>D18 核心提示</summary>

预处理后缀异或；从左到右移动分界线，把所有合法前缀异或逐个插入 Trie，用当前后缀异或查询最大异或值。注意插入与查询顺序必须保证两段不相交，空前缀和空后缀也要覆盖。

</details>

<details>
<summary>D19 一级提示</summary>

所有真正可能改变状态的坐标只有每次操作的 `l` 和 `r+1`。

</details>

<details>
<summary>D19 核心提示</summary>

可把所有边界离散化，叶子表示相邻离散坐标之间的一整段，节点保存该段工作日的**真实长度和**；区间赋值标记为全工作或全休息。也可使用动态开点线段树直接覆盖 `[1,n]`。

</details>

<details>
<summary>D20 一级提示</summary>

交换 `l<r` 后，既不涉及位置 `l/r`、也不位于二者之间的点对，逆序关系完全不变。

</details>

<details>
<summary>D20 核心提示</summary>

答案变化只来自端点二者以及中间元素与两个端点的关系。需要快速统计位置区间 `(l,r)` 中，值落在若干值域内的元素数；用位置分块，每块维护排序后的值，整块二分、散块暴力，交换后重建两个块。

</details>

---

# 第四周：树和图上的“一步降维”

这一周不使用 LCT。重点是：**先把树或图问题降成区间、深度轴、连通块贡献或静态树路径，再调用已有结构。**

| 日程 | 题目 | CF 评分 | Level | 已有工具 | 训练目标 |
|---|---|---:|---:|---|---|
| D22 | [CF383C Propagating Tree](https://codeforces.com/problemset/problem/383/C) | 2000 | L1 | DFS 序、树状数组 | 用深度奇偶符号把交替加减统一成普通子树区间加。 |
| D23 | [CF1076E Vasya and a Tree](https://codeforces.com/problemset/problem/1076/E) | 1900 | L1.5 | DFS、深度差分、回滚 | 一个祖先操作对当前 DFS 路径上的哪些深度有效。 |
| D24 | [CF1899G Unusual Entertainment](https://codeforces.com/problemset/problem/1899/G) | 1900 | L1.5 | DFS 序、主席树/归并树 | “排列区间是否包含某子树节点”变成二维矩形中是否有点。 |
| D25 | [CF609E Minimum spanning tree for each edge](https://codeforces.com/problemset/problem/609/E) | 2100 | L1.5 | Kruskal、LCA/树剖路径最大值 | 强制加入一条非树边时，只需删除原 MST 环上的最大边。 |
| D26 | [CF915F Imbalance Value of a Tree](https://codeforces.com/problemset/problem/915/F) | 2400 | L2 | 排序、并查集激活 | 把所有路径最大值/最小值之和改成按点权激活时新增的点对贡献。 |
| D27 | [CF1706E Qpwoeirut and Vertices](https://codeforces.com/problemset/problem/1706/E) | 2300 | L2 | Kruskal 重构树、RMQ | 顶点编号区间全部连通，等价于区间内每对相邻编号顶点都已连通。 |
| D28 | **最终验收** | — | — | — | 随机抽 4 题，各用 12 分钟写出状态、一次变化的影响集合、正确性与复杂度；至少完整写出 3 题。 |

<details>
<summary>D22 一级提示</summary>

节点 `x` 的一次操作，对与 `x` 深度奇偶相同的后代加 `val`，对奇偶不同的后代减 `val`。

</details>

<details>
<summary>D22 核心提示</summary>

给每个点乘上符号 `sgn[u]=(-1)^{depth[u]}`。在变换后的值域中，一次操作变为对子树 DFS 序区间统一加 `val*sgn[x]`；查询节点后再乘 `sgn[u]` 还原。

</details>

<details>
<summary>D23 一级提示</summary>

DFS 到节点 `u` 时，所有能影响 `u` 的操作都来自当前祖先链；每个操作只在一段连续深度范围内有效。

</details>

<details>
<summary>D23 核心提示</summary>

进入节点 `v` 时，对每个 `(d,x)` 在深度轴上加入区间 `[depth[v],depth[v]+d]`；节点答案是当前深度位置的累计值。用深度差分/Fenwick 支持区间加点查，退出子树时撤销这些操作。

</details>

<details>
<summary>D24 一级提示</summary>

将树做 DFS 序后，子树 `v` 对应值域 `[tin[v],tout[v]]`；排列位置 `[x,y]` 是另一个维度。

</details>

<details>
<summary>D24 核心提示</summary>

构造数组 `b[i]=tin[p[i]]`。询问变成：`b[x..y]` 中是否存在值落入 `[tin[v],tout[v]]`。可用前缀主席树作二维计数，也可用归并树在位置区间中二分值域。

</details>

<details>
<summary>D25 一级提示</summary>

先求一棵 MST。若要求答案必须包含某条非树边 `(u,v,w)`，加入它后会产生唯一一个环。

</details>

<details>
<summary>D25 核心提示</summary>

为了保持生成树，必须从该环删除一条边；删除路径 `u-v` 上权值最大的 MST 边最优。因此答案为 `MSTsum+w-maxEdgeOnPath(u,v)`。预处理倍增或树剖路径最大值。

</details>

<details>
<summary>D26 一级提示</summary>

总答案是“所有路径最大值之和”减去“所有路径最小值之和”，两部分可完全对称地计算。

</details>

<details>
<summary>D26 核心提示</summary>

按点权升序激活顶点。激活 `u` 并与已激活邻居连通时，两个连通块大小乘积就是新产生、且路径最大值首次变为 `a[u]` 的点对数，累加 `a[u]*size1*size2`。降序再做一次得到最小值贡献。

</details>

<details>
<summary>D27 一级提示</summary>

边按输入顺序逐步加入。先思考任意两个点 `u,v` 最早在第几条边加入后连通，以及如何一次性表示所有点对的这个时刻。

</details>

<details>
<summary>D27 核心提示</summary>

按边序建立 Kruskal 重构树，内部节点权值为发生合并的边编号，则两点最早连通时间为其 LCA 权值。编号区间 `[l,r]` 全连通，当且仅当相邻点对 `(l,l+1),...,(r-1,r)` 全连通；预处理每个相邻点对的时间，询问取区间最大值。

</details>

---

# 3. 四道上界验收题

只有在主线至少完成 18 题、且复习日能闭卷重做后再做。它们仍然属于 Level 2，但更接近《数据结构应用强化题单》的下界。

| 题目 | CF 评分 | 定位 | 训练点 |
|---|---:|---|---|
| [CF487B Strip](https://codeforces.com/problemset/problem/487/B) | 2000 | L2 | 单调队列确定合法左边界，再用区间最小值优化分段 DP。 |
| [CF1667B Optimal Partition](https://codeforces.com/problemset/problem/1667/B) | 2100 | L2 | 按前缀和大小分类 DP 转移，用权值轴数据结构取最优值。 |
| [CF1609E William The Oblivious](https://codeforces.com/problemset/problem/1609/E) | 2400 | L2 上界 | 线段树节点表示一段字符串上的小状态 DP，而不只是统计量。 |
| [CF246E Blood Cousins Return](https://codeforces.com/problemset/problem/246/E) | 2400 | L2 上界 | DSU on Tree 与固定深度去重统计结合，重点是容器按什么维度组织。 |

<details>
<summary>CF487B 核心提示</summary>

用两个单调队列维护当前窗口最大最小值，得到每个右端点最早合法左端点。令 `dp[i]` 为覆盖前 `i` 个元素的最少段数，转移是对一段合法前驱下标取最小值。

</details>

<details>
<summary>CF1667B 核心提示</summary>

令前缀和为 `s[i]`。枚举最后一段时，得分只取决于 `s[i]` 与 `s[j]` 的大小关系；把三种关系分别整理，在线段树/树状数组的权值轴上维护相应的 `dp[j]±j` 最大值。

</details>

<details>
<summary>CF1609E 核心提示</summary>

节点保存把区间修改成不含 `abc` 子序列所需的若干小状态，例如只允许 `a`、只允许 `b`、`a/b` 两阶段、`b/c` 两阶段和完整三阶段；合并时枚举分界落在左右哪一侧。

</details>

<details>
<summary>CF246E 核心提示</summary>

询问只关心某个子树中固定绝对深度的名字种数。DSU on Tree 保留重儿子容器，并按“深度 → 名字集合/计数”组织信息；也可以按深度分桶后做子树离线去重。

</details>

---

# 4. 每题复盘模板

```text
题目：
完成方式：独立 / 一级提示 / 核心提示 / 完整题解

1. 朴素算法是什么，瓶颈在哪：
2. 最关键的一次重述或转化：
3. 叶子 / 单个状态表示什么：
4. 节点或容器维护哪些字段：
5. 一次操作只影响哪些字段，为什么：
6. merge / pushup / 集合不变量：
7. 复杂度证明：
8. 一周后闭卷时最可能忘记什么：
```

---

# 5. 完成判定

一道题满足以下条件才标记完成：

- 能不看代码说明维护对象；
- 能写出一次变化影响的集合；
- 能解释为什么该集合是区间、前缀、后缀、子树、相邻元素或少数候选；
- 能给出复杂度，而不是只背结论；
- 看过完整题解的题已在复习日闭卷重做。

完成主线后，再回到《数据结构应用强化题单》，优先尝试其中相对靠前的：

1. CF750E；
2. CF703D；
3. CF833B；
4. CF570D；
5. CF1213G。

若这五题中有三题能够在只看一级提示的情况下推出，就说明已经从模板阶段稳定进入维护设计阶段。
