#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""第一阶段门禁指标与严格 XOR2 下界式验证。"""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations
from typing import Dict, Iterable, List, Mapping, Sequence, Set, Tuple, Any

import networkx as nx


def _mb(layer: str, layer_sizes_mb: Mapping[str, float]) -> float:
    return float(layer_sizes_mb.get(layer, 0.0))


def build_demand_state(
    containers_by_id: Mapping[str, Any],
    assignment: Mapping[str, str],
    node_ids: Sequence[str],
    cache_by_node: Mapping[str, Set[str]],
) -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]]]:
    """根据固定 placement 构造 D_n 与 W_n=D_n-H_n。"""
    demand: Dict[str, Set[str]] = {eid: set() for eid in node_ids}
    for cid, eid in assignment.items():
        c = containers_by_id.get(cid)
        if c is None or eid not in demand:
            continue
        demand[eid].update(set(c.layers))

    wants: Dict[str, Set[str]] = {}
    for eid in node_ids:
        wants[eid] = demand[eid] - set(cache_by_node.get(eid, set()))
    return demand, wants


def cache_similarity_metrics(
    cache_by_node: Mapping[str, Set[str]],
    layer_sizes_mb: Mapping[str, float],
) -> Dict[str, float]:
    """平均集合 Jaccard / 字节加权 Jaccard；heterogeneity=1-similarity。"""
    eids = list(cache_by_node)
    if len(eids) < 2:
        return {
            "avg_cache_jaccard": 1.0,
            "avg_cache_byte_jaccard": 1.0,
            "cache_heterogeneity": 0.0,
            "cache_byte_heterogeneity": 0.0,
        }

    js: List[float] = []
    bjs: List[float] = []
    for a, b in combinations(eids, 2):
        A = set(cache_by_node[a])
        B = set(cache_by_node[b])
        union = A | B
        inter = A & B
        js.append(len(inter) / len(union) if union else 1.0)

        ub = sum(_mb(l, layer_sizes_mb) for l in union)
        ib = sum(_mb(l, layer_sizes_mb) for l in inter)
        bjs.append(ib / ub if ub > 0 else 1.0)

    avg_j = sum(js) / len(js)
    avg_bj = sum(bjs) / len(bjs)
    return {
        "avg_cache_jaccard": avg_j,
        "avg_cache_byte_jaccard": avg_bj,
        "cache_heterogeneity": 1.0 - avg_j,
        "cache_byte_heterogeneity": 1.0 - avg_bj,
    }


def reciprocal_side_information_metrics(
    wants_by_node: Mapping[str, Set[str]],
    cache_by_node: Mapping[str, Set[str]],
) -> Dict[str, float | int]:
    """
    Receiver-demand 顶点 v_(n,l) 的 reciprocal coding edge 数。

    对节点对 (n,m)，可形成 reciprocal edge 的数量为：
      |W_n ∩ H_m| * |W_m ∩ H_n|。
    因为 W_n 与 H_n 不交，i=j 不会被错误计入。
    """
    eids = list(wants_by_node)
    num_vertices = sum(len(wants_by_node[e]) for e in eids)
    edges = 0
    active_node_pairs = 0
    total_node_pairs = len(eids) * (len(eids) - 1) // 2

    for i in range(len(eids)):
        n = eids[i]
        for j in range(i + 1, len(eids)):
            m = eids[j]
            a = len(set(wants_by_node[n]) & set(cache_by_node.get(m, set())))
            b = len(set(wants_by_node[m]) & set(cache_by_node.get(n, set())))
            if a > 0 and b > 0:
                active_node_pairs += 1
                edges += a * b

    denom = num_vertices * (num_vertices - 1)
    density = (2.0 * edges / denom) if denom > 0 else 0.0
    node_pair_rate = active_node_pairs / total_node_pairs if total_node_pairs > 0 else 0.0

    return {
        "num_want_vertices": num_vertices,
        "num_reciprocal_edges": edges,
        "coding_density": density,
        "reciprocal_active_node_pairs": active_node_pairs,
        "reciprocal_node_pair_rate": node_pair_rate,
    }


def peer_coverage_metrics(
    wants_by_node: Mapping[str, Set[str]],
    cache_by_node: Mapping[str, Set[str]],
    layer_sizes_mb: Mapping[str, float],
) -> Dict[str, float]:
    """缺失需求中，有多少字节至少存在于另一个 edge cache。按 receiver 需求计字节。"""
    eids = list(wants_by_node)
    total = 0.0
    peer_servable = 0.0
    for n in eids:
        others = set()
        for m in eids:
            if m != n:
                others |= set(cache_by_node.get(m, set()))
        for l in wants_by_node[n]:
            s = _mb(l, layer_sizes_mb)
            total += s
            if l in others:
                peer_servable += s
    return {
        "peer_servable_unicast_mb": peer_servable,
        "peer_coverage_ratio": peer_servable / total if total > 0 else 0.0,
    }


def strict_layer_xor2(
    wants_by_node: Mapping[str, Set[str]],
    cache_by_node: Mapping[str, Set[str]],
    layer_sizes_mb: Mapping[str, float],
) -> Dict[str, Any]:
    """
    保守且可直接与无编码 multicast 比较的 XOR2。

    把每个“唯一缺失 layer”视为 MCAST 的一次发送对象。若 layer A 与 B 满足：
      1) 所有需要 A 的节点都已经持有 B；
      2) 所有需要 B 的节点都已经持有 A；
    则可用 A XOR B 的共同前缀替代两次 MCAST 发送的一部分。

    若大小不同，最大可节省 min(size(A), size(B)) MB；较大 layer 的尾部仍单独发送。
    对所有合法 layer-pair 构造加权图，再求 maximum-weight matching，避免同一 layer 被重复配对。

    这是“第一阶段门禁”的保守版本：
    - 不依赖多轮 side-information 更新；
    - 不把 receiver-level 局部机会虚算成 MCAST 之后的实际节省；
    - 因而如果这里已经有明显收益，证据很强。
    """
    demanders: Dict[str, Set[str]] = defaultdict(set)
    for n, wants in wants_by_node.items():
        for l in wants:
            demanders[l].add(n)

    layers = sorted(demanders)
    G = nx.Graph()
    for l in layers:
        G.add_node(l)

    edge_count = 0
    for i in range(len(layers)):
        A = layers[i]
        demand_A = demanders[A]
        for j in range(i + 1, len(layers)):
            B = layers[j]
            demand_B = demanders[B]

            # 所有 A-receivers 都必须有 B；所有 B-receivers 都必须有 A。
            ok_A = all(B in cache_by_node.get(n, set()) for n in demand_A)
            if not ok_A:
                continue
            ok_B = all(A in cache_by_node.get(n, set()) for n in demand_B)
            if not ok_B:
                continue

            saving = min(_mb(A, layer_sizes_mb), _mb(B, layer_sizes_mb))
            if saving <= 0:
                continue
            G.add_edge(A, B, weight=saving)
            edge_count += 1

    matching = nx.algorithms.matching.max_weight_matching(
        G, maxcardinality=False, weight="weight"
    )

    matched_pairs = []
    saving_mb = 0.0
    for a, b in matching:
        w = float(G[a][b]["weight"])
        saving_mb += w
        matched_pairs.append(
            {
                "layer_a": a,
                "layer_b": b,
                "size_a_mb": _mb(a, layer_sizes_mb),
                "size_b_mb": _mb(b, layer_sizes_mb),
                "saving_mb": w,
                "demanders_a": sorted(demanders[a]),
                "demanders_b": sorted(demanders[b]),
            }
        )

    matched_pairs.sort(key=lambda x: (-x["saving_mb"], x["layer_a"], x["layer_b"]))
    return {
        "strict_xor2_candidate_edges": edge_count,
        "strict_xor2_matching_pairs": len(matched_pairs),
        "strict_xor2_saving_mb": saving_mb,
        "strict_xor2_pairs": matched_pairs,
    }


def evaluate_gate_metrics(
    wants_by_node: Mapping[str, Set[str]],
    cache_by_node: Mapping[str, Set[str]],
    layer_sizes_mb: Mapping[str, float],
) -> Dict[str, Any]:
    """计算门禁实验全部核心指标。"""
    # L0：每个节点各自拉缺失 layer。
    unicast_mb = sum(
        _mb(l, layer_sizes_mb)
        for wants in wants_by_node.values()
        for l in wants
    )

    # L1：同一个缺失 layer 在共享 backhaul 上只传一次。
    missing_union = set().union(*[set(x) for x in wants_by_node.values()]) if wants_by_node else set()
    mcast_mb = sum(_mb(l, layer_sizes_mb) for l in missing_union)

    xor = strict_layer_xor2(wants_by_node, cache_by_node, layer_sizes_mb)
    xor2_mb = max(0.0, mcast_mb - float(xor["strict_xor2_saving_mb"]))

    out: Dict[str, Any] = {
        "unicast_mb": unicast_mb,
        "mcast_mb": mcast_mb,
        "xor2_mb": xor2_mb,
        "multicast_gain": (unicast_mb - mcast_mb) / unicast_mb if unicast_mb > 0 else 0.0,
        "xor2_gain_over_mcast": (mcast_mb - xor2_mb) / mcast_mb if mcast_mb > 0 else 0.0,
        "total_saving_vs_unicast": (unicast_mb - xor2_mb) / unicast_mb if unicast_mb > 0 else 0.0,
        "demand_redundancy_factor": unicast_mb / mcast_mb if mcast_mb > 0 else 1.0,
        "num_unique_missing_layers": len(missing_union),
    }
    out.update(reciprocal_side_information_metrics(wants_by_node, cache_by_node))
    out.update(peer_coverage_metrics(wants_by_node, cache_by_node, layer_sizes_mb))
    out.update(cache_similarity_metrics(cache_by_node, layer_sizes_mb))
    out.update(xor)
    return out
