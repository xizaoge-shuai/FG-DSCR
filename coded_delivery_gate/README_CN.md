# 第一阶段验证：FG-DSCR → 历史异构缓存 → MCAST / strict-XOR2 门禁

这套代码只回答一个问题：

> **真实 Docker layer + 上一篇 FG-DSCR 产生的历史 cache state，是否能形成足够大的 reciprocal side information，使编码相对“已经去重后的 MCAST”仍有显著收益？**

它**不会修改 FG-DSCR 原始代码**。

---

## 1. 为什么不是直接使用旧 case 的 `initial_cache`

上一篇 `build_fg_case_from_catalog_and_trace.py` 通常把相同的 hot cache 复制给各节点。直接拿它做编码实验，会人为压低 cache heterogeneity。

这里采用两个连续窗口：

```text
Window 0（历史）
request batch R0
    ↓
FG-DSCR placement + ordering + PGDSF replacement
    ↓
每个节点自然形成不同的 final cache H_n

Window 1（目标）
request batch R1
    ↓
固定 placement
    ↓
D_n = 节点上目标容器需要的 layer 并集
W_n = D_n - H_n
    ↓
Unicast / MCAST / strict-XOR2
```

这样 side information 是历史服务部署自然形成的，不是人为构造的。

---

## 2. 安装位置

推荐把本目录直接放在 FG-DSCR 根目录下：

```text
FG-DSCR/
├── scripts/
├── cases/
├── data/
└── coded_delivery_gate/
    ├── run_gate.py
    ├── run_sweep.py
    └── ...
```

安装依赖：

```bash
pip install -r coded_delivery_gate/requirements.txt
```

上一篇本来就使用 `networkx/pandas/matplotlib`，一般无需额外环境。

---

## 3. 单个 case：最快门禁

在 FG-DSCR 根目录执行：

```bash
python coded_delivery_gate/run_gate.py \
  --repo-root . \
  --case cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_200.json \
  --placement fg_dscr \
  --warmup-fraction 0.5 \
  --target-fraction 0.5 \
  --split-mode sequential \
  --seed 42 \
  --scheduler-config coded_delivery_gate/scheduler_config_paper_like.json \
  --out results/coded_gate/gate_200.json \
  --dump-state
```

如果你的实际 case 目录不同，只改 `--case`。

### 建议先用 `dev` 分支

当前仓库的 dev 分支包含更后期的 hard-resource/network/cache 实验逻辑。适配器通过运行时 introspection 自动忽略当前分支不支持的 scheduler config 参数，因此 master/dev 都尽量兼容。

---

## 4. 最核心输出

结果 JSON 中：

```text
metrics.unicast_mb
metrics.mcast_mb
metrics.xor2_mb
metrics.multicast_gain
metrics.xor2_gain_over_mcast
metrics.coding_density
metrics.cache_heterogeneity
metrics.peer_coverage_ratio
metrics.strict_xor2_matching_pairs
```

定义如下。

### Unicast

每个节点分别拉自己的缺失 layer：

\[
B_{uni}=\sum_n\sum_{l\in W_n}s_l.
\]

### MCAST

共享 backhaul 上，同一缺失 layer 只传一次：

\[
B_{mcast}=\sum_{l\in \cup_nW_n}s_l.
\]

### Multicast Gain

\[
G_{mcast}=\frac{B_{uni}-B_{mcast}}{B_{uni}}.
\]

### Reciprocal Side-Information Density

receiver-demand 顶点为 \(v_{n,l}\)。如果：

\[
l_i\in H_m,\quad l_j\in H_n,
\]

则 \((n,l_i)\) 与 \((m,l_j)\) 有 reciprocal coding edge。

\[
\rho_{coding}=\frac{2|E_{coding}|}{|V|(|V|-1)}.
\]

### strict-XOR2

为了不把 receiver-level 的局部机会错误算成 MCAST 之后的实际节省，第一阶段采用**保守条件**：

layer A、B 只有在：

1. 所有需要 A 的节点都已经持有 B；
2. 所有需要 B 的节点都已经持有 A；

时才允许用 A/B 做一次全体 receiver 都能立即解码的 pairwise XOR。

若两层大小不同，最大节省：

\[
g(A,B)=\min(s_A,s_B).
\]

对所有合法 layer pair 做 maximum-weight matching：

\[
B_{xor2}=B_{mcast}-\sum_{(A,B)\in M^*}\min(s_A,s_B).
\]

因此：

\[
G_{xor2}=\frac{B_{mcast}-B_{xor2}}{B_{mcast}}.
\]

这是一个**保守门禁**。它不利用多轮解码后新 side information，也不做 3-way/4-way IDNC。如果它已经有明显收益，后续继续 CRA-IDNC 很有价值。

---

## 5. 判断标准

第一阶段建议按：

- `xor2_gain_over_mcast >= 0.10`：绿灯，继续完整 IDNC/原型；
- `0.05 ~ 0.10`：黄灯，需要看 burst、高 cache heterogeneity、AI workload；
- `< 0.05`：不建议继续押编码主线。

注意：这是研究决策门禁，不是理论阈值。

---

## 6. 为什么同时报 `coding_density` 和 `strict-XOR2 gain`

`coding_density > 0` 只能说明 receiver-level 互补关系存在；并不意味着相对 MCAST 一定节省。

典型反例：两个节点都缺 A，只有其中一个有 B；第三个节点缺 B 且有 A。局部 A XOR B 可以服务两个节点，但另一个缺 A 的节点仍需 A，因此总 backhaul 可能与 MCAST 一样。

所以：

- `coding_density` = **机会结构**；
- `xor2_gain_over_mcast` = **保守的实际通信收益**。

两者必须同时看。

---

## 7. 批量跑多个 case / seed / placement

例如：

```bash
python coded_delivery_gate/run_sweep.py \
  --repo-root . \
  --cases 'cases/drtp_scale_nodes/**/*.json' \
  --placements fg_dscr round_robin \
  --seeds 1 2 3 4 5 \
  --split-mode random \
  --warmup-fraction 0.5 \
  --target-fraction 0.5 \
  --scheduler-config coded_delivery_gate/scheduler_config_paper_like.json \
  --out-csv results/coded_gate/gate_sweep.csv \
  --out-jsonl results/coded_gate/gate_sweep.jsonl
```

注意使用单引号包住 glob，让 Python 自己递归展开。

建议至少对比：

```text
FG-DSCR placement + coding
Round-Robin placement + coding
```

目的是验证编码机会不是上一篇 placement 算法人为制造出来的。

---

## 8. 快速画图

```bash
python coded_delivery_gate/plot_gate.py \
  --csv results/coded_gate/gate_sweep.csv \
  --out-dir results/coded_gate/figs
```

会生成：

1. Unicast / MCAST / strict-XOR2 平均 backhaul traffic；
2. cache heterogeneity vs coding gain；
3. reciprocal coding density vs coding gain。

---

## 9. 单元测试

在 `coded_delivery_gate` 目录：

```bash
python -m unittest discover -s tests -v
```

测试包含：

- 标准 2-node reciprocal XOR；
- 纯公共需求只产生 multicast gain、不产生 coding gain；
- 局部 reciprocal opportunity 不能被错误计算成 MCAST saving；
- unequal layer size 只节省较小 layer 的大小。

---

## 10. 第一阶段有意不做的事情

当前代码**不做**：

- 4 MB 真 chunk 化；
- 3-way/4-way IDNC；
- dynamic side-information update；
- CRA-IDNC readiness weight；
- containerd 原型；
- 编码 CPU 实测。

这些都必须等门禁结果出来后再投入。

第一阶段唯一目标是判断：

\[
B_{uni} > B_{mcast} > B_{xor2}
\]

中的第二个差距，在真实历史 cache state 下是否足够大。
