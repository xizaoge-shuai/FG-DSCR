from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple, Optional
import json
import math
import copy
import argparse
import statistics
from collections import Counter, defaultdict


# =========================
# 数据结构
# =========================

@dataclass
class Container:
    cid: str
    layers: Set[str]
    resources: Dict[str, float]   # {"cpu":..., "mem":..., "disk":...}
    run_time: float               # seconds
    service_type: str = "default"


@dataclass
class EdgeNode:
    eid: str
    resources: Dict[str, float]   # capacities
    repo_capacity_mb: int
    bandwidth_mb_s: float
    initial_cache: Set[str] = field(default_factory=set)


@dataclass
class BeamState:
    seq: List[str]
    remaining: Set[str]
    cache: Set[str]
    hist_freq: Dict[str, int]
    clock: float
    score: float


@dataclass
class QueueMetrics:
    completion_times: List[float]
    downloaded_mb: int
    reused_mb: int
    act: float
    ams: float
    final_cache: Set[str]
    step_logs: List[Dict] = field(default_factory=list)
    container_logs: List[Dict] = field(default_factory=list)


# =========================
# 主算法
# =========================

class FGDscrScheduler:
    """
    FG-DSCR:
      Phase 1: Fragmentation-aware Game Placement
      Phase 2: Dynamic State-aware Cache-Reuse Ordering + Layer-aware Slot-aware PGDSF
    """

    def __init__(
        self,
        layer_sizes_mb: Dict[str, int],
        alpha_obj: float = 0.5,
        lambda_balance: float = 0.0,
        lambda_idle: float = 0.0,
        theta_cong_count: float = 0.0,
        greedy_load_factor: float = 0.0,
    # Phase 1 weights
        lambda_cong: float = 1.0,
        lambda_frag: float = 1.0,
        lambda_aff: float = 0.2,
        # lambda_balance: float = 0.0,
        # lambda_idle: float = 0.0,
        # theta_cong_count: float = 0.0,
        tiny_gap_thresholds: Optional[Dict[str, float]] = None,
        lambda_task_load: float = 0.03,
        task_load_power: float = 2.0,
        task_load_factor: float = 1.8,
    # Phase 2 weights
        alpha1_reuse: float = 1.0,
        alpha2_future: float = 0.15,
        alpha3_pull: float = 50.0,
        alpha4_evict: float = 0.02,
    # LSPGDSF weights
        beta_slot: float = 2.0,
        rho_centrality: float = 0.2,
        kappa_overshoot: float = 0.5,
        k_pin: int = 6,
        beam_width: int = 4,
        unit_mb: int = 50,
        max_best_response_rounds: int = 20,
        cache_policy: str = "pgdsf",
        order_policy: str = "dynamic_state",
        disable_future_share: bool = False,
        algo_name: str = "FG-DSCR",
    ):
        self.layer_sizes_mb = dict(layer_sizes_mb)
        self.alpha_obj = alpha_obj

        self.lambda_cong = lambda_cong
        self.lambda_frag = lambda_frag
        self.lambda_aff = lambda_aff
        self.lambda_balance = lambda_balance
        self.lambda_idle = lambda_idle
        self.theta_cong_count = theta_cong_count
        self.lambda_task_load = lambda_task_load
        self.task_load_power = task_load_power
        self.task_load_factor = task_load_factor
        self.alpha1_reuse = alpha1_reuse
        self.alpha2_future = alpha2_future
        self.alpha3_pull = alpha3_pull
        self.alpha4_evict = alpha4_evict

        self.beta_slot = beta_slot
        self.rho_centrality = rho_centrality
        self.kappa_overshoot = kappa_overshoot
        self.k_pin = k_pin
        self.beam_width = beam_width
        self.unit_mb = max(unit_mb, 1)
        self.max_best_response_rounds = max_best_response_rounds
        self.cache_policy = cache_policy
        self.order_policy = order_policy
        self.disable_future_share = disable_future_share
        self.greedy_load_factor = greedy_load_factor
        self.algo_name = algo_name

        self.tiny_gap_thresholds = tiny_gap_thresholds or {
            "cpu": 0.15,
            "mem": 0.15,
            "disk": 0.15,
        }

        self.containers: Dict[str, Container] = {}
        self.nodes: Dict[str, EdgeNode] = {}
        self.layer_centrality: Dict[str, int] = {}
        self.typical_demands: Dict[str, float] = {}

    # 日志
        self.phase1_history: List[Dict] = []
        self.phase2_reuse_history: List[Dict] = []
        self.node_step_logs: List[Dict] = []
        self.container_metrics: List[Dict] = []

    # -------------------------
    # 基础工具
    # -------------------------

    def set_data(self, containers: List[Container], nodes: List[EdgeNode]):
        self.containers = {c.cid: c for c in containers}
        self.nodes = {n.eid: n for n in nodes}

        cnt = Counter()
        for c in containers:
            for l in c.layers:
                cnt[l] += 1
        self.layer_centrality = dict(cnt)

    # 用于图1：定义“典型需求”，判断哪些剩余资源属于碎片
        for q in ["cpu", "mem", "disk"]:
            vals = [c.resources.get(q, 0.0) for c in containers if c.resources.get(q, 0.0) > 0]
            self.typical_demands[q] = float(statistics.median(vals)) if vals else 0.0

    def image_size(self, cid: str) -> int:
        return sum(self.layer_sizes_mb[l] for l in self.containers[cid].layers)

    def cache_size(self, cache_layers: Set[str]) -> int:
        return sum(self.layer_sizes_mb[l] for l in cache_layers)
    def aggregate_resource_usage(
        self,
        eid: str,
        assignment: Dict[str, str],
    ) -> Dict[str, float]:
        used = defaultdict(float)
        for cid, ne in assignment.items():
            if ne != eid:
                continue
            c = self.containers[cid]
            for q, v in c.resources.items():
                used[q] += v
        return dict(used)


    def fragmented_resource_amounts(
        self,
        assignment: Dict[str, str],
    ) -> Dict[str, float]:
        """
        用于图1：
        统计“剩余但不足以支撑一个典型镜像”的资源量，视为碎片化资源量。
        这是诊断指标，不一定完全等同于可行性约束。
        """
        totals = {"cpu": 0.0, "mem": 0.0, "disk": 0.0}
        for eid, node in self.nodes.items():
            used = self.aggregate_resource_usage(eid, assignment)
            for q, cap in node.resources.items():
                rem = max(0.0, cap - used.get(q, 0.0))
                thr = self.typical_demands.get(q, 0.0)
                if 0.0 < rem < thr:
                    totals[q] += rem
        return totals





    def potential_components(
        self,
        assignment: Dict[str, str],
    ) -> Tuple[float, Dict[str, Dict]]:
        """
        Phase-1 potential with scale normalization.

        The original implementation directly summed:
            cong_term + frag_term + aff_term + task_load_term

        However, these terms have very different scales:
            cong_term:       D_j^2 / bandwidth, often 1e5~1e6
            frag_term:       small shape-imbalance value, often O(1)
            aff_term:        layer-size based reward, often 1e4
            task_load_term:  already scaled by cong_scale

        This version keeps congestion as the reference scale and normalizes
        fragmentation, affinity, and load before aggregation.
        """
        layer_cnts = self.node_layer_counts(assignment)
        node_counts = Counter(assignment.values())

        total_num = len(self.containers)
        num_nodes = max(len(self.nodes), 1)
        avg_per_node = total_num / num_nodes

        total_run_time = sum(c.run_time for c in self.containers.values())
        avg_run_per_node = total_run_time / num_nodes

        eps = 1e-9

        raw_cong_terms: Dict[str, float] = {}
        raw_frag_terms: Dict[str, float] = {}
        raw_aff_terms: Dict[str, float] = {}
        raw_load_terms: Dict[str, float] = {}
        aux: Dict[str, Dict] = {}

        # 1) First pass: collect raw components.
        for eid, node in self.nodes.items():
            m_j = node_counts.get(eid, 0)
            D_j = self.distinct_missing_size(eid, layer_cnts[eid])
            Frag_j = self.fragmentation_penalty(eid, assignment)
            Aff_j = self.affinity_gain(eid, layer_cnts[eid])

            raw_cong_terms[eid] = self.lambda_cong * (
                (D_j ** 2) / max(node.bandwidth_mb_s, eps)
            )

            load_ratio = m_j / max(avg_per_node, eps)
            soft_limit = self.task_load_factor * avg_per_node
            overload = max(0.0, m_j - soft_limit)
            overload_ratio = overload / max(avg_per_node, eps)

            node_run_sum = sum(
                self.containers[cid].run_time
                for cid, ne in assignment.items()
                if ne == eid
            )
            run_ratio = node_run_sum / max(avg_run_per_node, eps)

            raw_frag_terms[eid] = Frag_j
            raw_aff_terms[eid] = Aff_j
            raw_load_terms[eid] = (overload_ratio ** self.task_load_power) * run_ratio

            aux[eid] = {
                "m_j": m_j,
                "D_j": D_j,
                "Frag_j": Frag_j,
                "Aff_j": Aff_j,
                "load_ratio": load_ratio,
                "soft_limit": soft_limit,
                "overload": overload,
                "overload_ratio": overload_ratio,
                "node_run_sum": node_run_sum,
                "run_ratio": run_ratio,
            }

        # 2) Reference scale.
        # Use average congestion as the common scale so that all components
        # contribute at comparable magnitudes.
        cong_scale = max(
            sum(abs(v) for v in raw_cong_terms.values()) / max(num_nodes, 1),
            1.0
        )

        # 3) Component normalization scales.
        # max-scale keeps each normalized component in roughly [0, 1].
        frag_scale = max(max((abs(v) for v in raw_frag_terms.values()), default=0.0), eps)
        aff_scale = max(max((abs(v) for v in raw_aff_terms.values()), default=0.0), eps)
        load_scale = max(max((abs(v) for v in raw_load_terms.values()), default=0.0), eps)

        total = 0.0
        comps: Dict[str, Dict] = {}

        for eid, node in self.nodes.items():
            raw_cong = raw_cong_terms[eid]
            raw_frag = raw_frag_terms[eid]
            raw_aff = raw_aff_terms[eid]
            raw_load = raw_load_terms[eid]

            frag_norm = raw_frag / frag_scale
            aff_norm = raw_aff / aff_scale
            load_norm = raw_load / load_scale if load_scale > eps else 0.0

            # Congestion remains the reference term.
            cong_term = raw_cong

            # Other terms are normalized and then mapped to the same scale.
            frag_term = self.lambda_frag * cong_scale * frag_norm
            aff_term = - self.lambda_aff * cong_scale * aff_norm
            task_load_term = self.lambda_task_load * cong_scale * load_norm

            balance_term = 0.0
            idle_term = 0.0

            node_val = cong_term + frag_term + aff_term + task_load_term
            total += node_val

            comps[eid] = {
                **aux[eid],

                # scaled terms used by the actual potential
                "cong_term": cong_term,
                "frag_term": frag_term,
                "aff_term": aff_term,
                "task_load_term": task_load_term,
                "node_potential": node_val,

                # normalized terms for plotting/debugging
                "cong_norm": raw_cong / max(cong_scale, eps),
                "frag_norm": frag_norm,
                "aff_norm": aff_norm,
                "load_norm": load_norm,

                # raw terms for interpretation
                "raw_cong_term": raw_cong,
                "raw_frag_term": raw_frag,
                "raw_aff_term": raw_aff,
                "raw_load_term": raw_load,

                # scales
                "cong_scale": cong_scale,
                "frag_scale": frag_scale,
                "aff_scale": aff_scale,
                "load_scale": load_scale,

                "balance_term": balance_term,
                "idle_term": idle_term,
            }

        return total, comps


    def log_phase1_state(
        self,
        cycle: int,
        assignment: Dict[str, str],
        label: str,
    ):
        phi, comps = self.potential_components(assignment)
        frag = self.fragmented_resource_amounts(assignment)
        counts = Counter(assignment.values())
        loads = [counts.get(eid, 0) for eid in self.nodes]
        mean_load = sum(loads) / max(len(loads), 1)
        max_node_load = max(loads) if loads else 0
        load_var = sum((x - mean_load) ** 2 for x in loads) / max(len(loads), 1)
        self.phase1_history.append({
            "cycle": cycle,
            "label": label,
            "potential": phi,
            "fragmented_cpu": frag["cpu"],
            "fragmented_mem": frag["mem"],
            "fragmented_disk": frag["disk"],
            "active_nodes": sum(1 for eid in self.nodes if counts.get(eid, 0) > 0),
            "idle_nodes": sum(1 for eid in self.nodes if counts.get(eid, 0) == 0),
            "node_container_counts": {eid: counts.get(eid, 0) for eid in self.nodes},
            "node_components": comps,
            "max_node_load": max_node_load,
            "load_var": load_var,
        })


    def build_phase2_reuse_history(
        self,
        step_logs: List[Dict],
    ) -> List[Dict]:
        bucket = defaultdict(lambda: {
            "round": 0,
            "step_reuse_mb_global": 0,
            "step_downloaded_mb_global": 0,
            "active_nodes": 0,
        })

        for rec in step_logs:
            r = rec["local_step"]
            bucket[r]["round"] = r
            bucket[r]["step_reuse_mb_global"] += rec["reuse_mb"]
            bucket[r]["step_downloaded_mb_global"] += rec["downloaded_mb"]
            bucket[r]["active_nodes"] += 1

        rows = [bucket[k] for k in sorted(bucket.keys())]

        cum_reuse = 0
        cum_download = 0
        for row in rows:
            cum_reuse += row["step_reuse_mb_global"]
            cum_download += row["step_downloaded_mb_global"]
            row["cumulative_reuse_mb_global"] = cum_reuse
            row["cumulative_downloaded_mb_global"] = cum_download

        return rows
        # =========================
        # Phase 1: 势博弈节点分配
        # =========================

    def feasible_assign(
        self,
        cid: str,
        eid: str,
        assignment: Dict[str, str],
    ) -> bool:
        node = self.nodes[eid]
        c = self.containers[cid]

    # 只检查单镜像是否能在该节点上运行
    # 不再把同一时间片已分配到该节点的所有镜像资源直接累加
        for q, demand in c.resources.items():
            if demand > node.resources.get(q, 0.0):
                return False

    # cache/repo capacity only controls reusable layer retention.
    # It should NOT be a hard deployment feasibility constraint.
    # A container can still be pulled and started even if its image
    # cannot be fully retained in the reusable cache.
    # if self.image_size(cid) > node.repo_capacity_mb:
    #     return False

        return True

    def node_layer_counts(
        self,
        assignment: Dict[str, str],
    ) -> Dict[str, Counter]:
        cnts = {eid: Counter() for eid in self.nodes}
        for cid, eid in assignment.items():
            for l in self.containers[cid].layers:
                cnts[eid][l] += 1
        return cnts

    def distinct_missing_size(self, eid: str, layer_cnt: Counter) -> int:
        node = self.nodes[eid]
        total = 0
        for l, k in layer_cnt.items():
            if k > 0 and l not in node.initial_cache:
                total += self.layer_sizes_mb[l]
        return total

    def fragmentation_penalty(
        self,
        eid: str,
        assignment: Dict[str, str],
    ) -> float:
        """
        顺序队列模型下，不再把“已分配镜像资源总和”当作并发占用。
        这里改成“需求形状失衡惩罚”：
        - 看分到该节点的镜像，在 cpu/mem/disk 三维上的平均归一化需求是否过于偏斜
        - 越偏斜，说明后续更容易形成资源碎片化倾向
        """
        node = self.nodes[eid]
        cids = [cid for cid, ne in assignment.items() if ne == eid]

        if not cids:
            return 0.0

        prof = []
        tiny_gap_pen = 0.0

        for q, cap in node.resources.items():
            vals = [
                self.containers[cid].resources.get(q, 0.0) / max(cap, 1e-8)
                for cid in cids
            ]
            avg_q = sum(vals) / len(vals)
            prof.append(avg_q)

        # 如果某一维长期非常低，说明该节点上这类资源利用结构不均衡
            if 0.0 < avg_q < self.tiny_gap_thresholds.get(q, 0.15):
                tiny_gap_pen += 0.5

        mean_p = sum(prof) / len(prof)
        var_p = sum((x - mean_p) ** 2 for x in prof) / len(prof)
        return var_p + tiny_gap_pen

    def affinity_gain(self, eid: str, layer_cnt: Counter) -> float:
        # 层共享亲和：sum s_l * log(1+n_jl)
        gain = 0.0
        for l, k in layer_cnt.items():
            if k > 0:
                gain += self.layer_sizes_mb[l] * math.log(1.0 + k)
        return gain

    def potential(self, assignment: Dict[str, str]) -> float:
        total, _ = self.potential_components(assignment)
        return total
    
    def assignment_signature(self, assignment: Dict[str, str]) -> Dict[str, float]:
        """
        用于 plateau move 的结构判定：
        - idle_nodes 越少越好
        - max_load 越小越好
        - load_var 越小越好
        """
        counts = Counter(assignment.values())
        loads = [counts.get(eid, 0) for eid in self.nodes]

        idle_nodes = sum(1 for x in loads if x == 0)
        max_load = max(loads) if loads else 0
        mean_load = sum(loads) / max(len(loads), 1)
        load_var = sum((x - mean_load) ** 2 for x in loads) / max(len(loads), 1)

        return {
            "idle_nodes": idle_nodes,
            "max_load": max_load,
            "load_var": load_var,
        }


    def is_structurally_better(
        self,
        new_sig: Dict[str, float],
        old_sig: Dict[str, float],
        eps: float = 1e-9,
    ) -> bool:
        """
        plateau 接受规则：
        势函数差不多时，只要结构更好也接受。
        采用字典序：
        1) idle_nodes 更少
        2) max_load 更小
        3) load_var 更小
        """
        if new_sig["idle_nodes"] < old_sig["idle_nodes"]:
            return True
        if new_sig["idle_nodes"] > old_sig["idle_nodes"]:
            return False

        if new_sig["max_load"] < old_sig["max_load"]:
            return True
        if new_sig["max_load"] > old_sig["max_load"]:
            return False

        if new_sig["load_var"] + eps < old_sig["load_var"]:
            return True

        return False
    

    def _candidate_cids_on_node(
        self,
        eid: str,
        assignment: Dict[str, str],
        topk: int = 8,
    ) -> List[str]:
        """
        从某个节点上挑若干候选容器。
        默认优先挑“大镜像 + 资源重”的。
        """
        cids = [cid for cid, ne in assignment.items() if ne == eid]
        cids.sort(
            key=lambda cid: (
                self.image_size(cid),
                sum(self.containers[cid].resources.values())
            ),
            reverse=True
        )
        return cids[:topk]


    def _apply_move(
        self,
        assignment: Dict[str, str],
        cid: str,
        dst: str,
    ) -> Dict[str, str]:
        trial = dict(assignment)
        trial[cid] = dst
        return trial


    def _apply_swap(
        self,
        assignment: Dict[str, str],
        cid1: str,
        cid2: str,
    ) -> Dict[str, str]:
        trial = dict(assignment)
        e1 = trial[cid1]
        e2 = trial[cid2]
        trial[cid1] = e2
        trial[cid2] = e1
        return trial
    def _overlap_mb(self, cid1: str, cid2: str) -> int:
        return sum(
            self.layer_sizes_mb[l]
            for l in (self.containers[cid1].layers & self.containers[cid2].layers)
        )


    def _make_similar_block(
        self,
        src_eid: str,
        assignment: Dict[str, str],
        seed_cid: str,
        block_size: int = 3,
    ) -> List[str]:
        """
        在同一节点上，以 seed 为中心找若干最相似容器组成小簇。
        """
        cids = [cid for cid, ne in assignment.items() if ne == src_eid and cid != seed_cid]
        cids.sort(key=lambda cid: self._overlap_mb(seed_cid, cid), reverse=True)

        block = [seed_cid]
        for cid in cids:
            if len(block) >= block_size:
                break
            block.append(cid)
        return block


    def _feasible_block_assign(
        self,
        block: List[str],
        dst_eid: str,
        partial_assignment: Dict[str, str],
    ) -> bool:
        """
        逐个检查 block 中每个容器搬到 dst 是否可行。
        你当前 feasible_assign 是顺序队列模型，只检查单镜像可行性。
        """
        trial = dict(partial_assignment)
        for cid in block:
            if not self.feasible_assign(cid, dst_eid, trial):
                return False
            trial[cid] = dst_eid
        return True


    def _best_move_neighbor(
        self,
        assignment: Dict[str, str],
        eps: float = 1e-6,
        move_topk_per_node: int = 8,
    ) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
        """
        搜索单容器 move 邻域
        只接受真正让势函数下降的动作
        """
        base_phi = self.potential(assignment)
        counts = Counter(assignment.values())

        src_nodes = sorted(self.nodes.keys(), key=lambda eid: counts.get(eid, 0), reverse=True)
        dst_nodes = sorted(self.nodes.keys(), key=lambda eid: counts.get(eid, 0))

        best_trial = None
        best_delta = float("inf")

        for src in src_nodes:
            cand_cids = self._candidate_cids_on_node(src, assignment, topk=move_topk_per_node)

            for cid in cand_cids:
                cur_eid = assignment[cid]
                for dst in dst_nodes:
                    if dst == cur_eid:
                        continue

                    partial = dict(assignment)
                    del partial[cid]
                    if not self.feasible_assign(cid, dst, partial):
                        continue

                    trial = dict(partial)
                    trial[cid] = dst

                    new_phi = self.potential(trial)
                    delta = new_phi - base_phi

                    if delta < -eps and delta < best_delta:
                        best_delta = delta
                        best_trial = trial

        if best_trial is not None:
            return best_trial, "move"
        return None, None


    def _best_swap_neighbor(
        self,
        assignment: Dict[str, str],
        eps: float = 1e-6,
        swap_topk_per_node: int = 6,
    ) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
        """
        搜索 swap 邻域：重载节点 <-> 轻载节点交换两个容器
        只接受真正让势函数下降的动作
        """
        base_phi = self.potential(assignment)
        counts = Counter(assignment.values())

        src_nodes = sorted(self.nodes.keys(), key=lambda eid: counts.get(eid, 0), reverse=True)
        dst_nodes = sorted(self.nodes.keys(), key=lambda eid: counts.get(eid, 0))

        heavy_nodes = src_nodes[:min(4, len(src_nodes))]
        light_nodes = dst_nodes[:min(4, len(dst_nodes))]

        best_trial = None
        best_delta = float("inf")

        for src in heavy_nodes:
            src_cands = self._candidate_cids_on_node(src, assignment, topk=swap_topk_per_node)

            for dst in light_nodes:
                if src == dst:
                    continue
                dst_cands = self._candidate_cids_on_node(dst, assignment, topk=swap_topk_per_node)

                for cid1 in src_cands:
                    for cid2 in dst_cands:
                        e1 = assignment[cid1]
                        e2 = assignment[cid2]
                        if e1 == e2:
                            continue

                        partial = dict(assignment)
                        del partial[cid1]
                        del partial[cid2]

                        if not self.feasible_assign(cid1, e2, partial):
                            continue
                        if not self.feasible_assign(cid2, e1, partial):
                            continue

                        trial = dict(partial)
                        trial[cid1] = e2
                        trial[cid2] = e1

                        new_phi = self.potential(trial)
                        delta = new_phi - base_phi

                        if delta < -eps and delta < best_delta:
                            best_delta = delta
                            best_trial = trial

        if best_trial is not None:
            return best_trial, "swap"
        return None, None

    def _best_block_neighbor(
        self,
        assignment: Dict[str, str],
        eps: float = 1e-6,
        seed_topk_per_node: int = 5,
        block_sizes: Tuple[int, ...] = (2, 3),
    ) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
        """
        搜索 block move 邻域：把一个小簇一起搬到别的节点
        只接受真正让势函数下降的动作
        """
        base_phi = self.potential(assignment)
        counts = Counter(assignment.values())

        src_nodes = sorted(self.nodes.keys(), key=lambda eid: counts.get(eid, 0), reverse=True)
        dst_nodes = sorted(self.nodes.keys(), key=lambda eid: counts.get(eid, 0))

        heavy_nodes = src_nodes[:min(4, len(src_nodes))]
        light_nodes = dst_nodes[:min(4, len(dst_nodes))]

        best_trial = None
        best_delta = float("inf")

        for src in heavy_nodes:
            seeds = self._candidate_cids_on_node(src, assignment, topk=seed_topk_per_node)

            for seed in seeds:
                for bsz in block_sizes:
                    block = self._make_similar_block(src, assignment, seed, block_size=bsz)
                    if len(block) <= 1:
                        continue

                    for dst in light_nodes:
                        if dst == src:
                            continue

                        partial = dict(assignment)
                        for cid in block:
                            del partial[cid]

                        if not self._feasible_block_assign(block, dst, partial):
                            continue

                        trial = dict(partial)
                        for cid in block:
                            trial[cid] = dst

                        new_phi = self.potential(trial)
                        delta = new_phi - base_phi

                        if delta < -eps and delta < best_delta:
                            best_delta = delta
                            best_trial = trial

        if best_trial is not None:
            return best_trial, "block_move"
        return None, None

    def _perturb_rebalance(
        self,
        assignment: Dict[str, str],
    ) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
        return None, None
    

    def greedy_init_assignment(self) -> Dict[str, str]:
        """
        带软任务上限的 greedy 初始化。

        原来的 greedy 会过度追求镜像层亲和，导致 edge-2 这类节点堆很多容器。
        这里加入 greedy_load_factor：
          - greedy_load_factor <= 0: 保持原行为
          - greedy_load_factor > 0: 节点任务数达到 soft_cap 后，优先不再继续塞入

        soft_cap = ceil(greedy_load_factor * N / M)
        """
        # 大镜像、重资源优先
        cids = list(self.containers.keys())
        cids.sort(
            key=lambda cid: (
                self.image_size(cid),
                sum(self.containers[cid].resources.values())
            ),
            reverse=True
        )

        assignment: Dict[str, str] = {}

        avg_load = len(cids) / max(len(self.nodes), 1)
        soft_cap = None
        if self.greedy_load_factor and self.greedy_load_factor > 0:
            soft_cap = max(1, math.ceil(self.greedy_load_factor * avg_load))

        for cid in cids:
            feasible_eids = []
            for eid in self.nodes:
                if self.feasible_assign(cid, eid, assignment):
                    feasible_eids.append(eid)

            if not feasible_eids:
                raise ValueError(f"No feasible node for container {cid}")

            # 关键改动：
            # 初始化阶段优先选择未超过 soft_cap 的节点。
            # 只有所有可行节点都超过 soft_cap 时，才退回所有可行节点。
            candidate_eids = feasible_eids
            if soft_cap is not None:
                counts = Counter(assignment.values())
                under_cap = [
                    eid for eid in feasible_eids
                    if counts.get(eid, 0) < soft_cap
                ]
                if under_cap:
                    candidate_eids = under_cap

            best_eid = None
            best_phi = float("inf")

            for eid in candidate_eids:
                trial = dict(assignment)
                trial[cid] = eid
                phi = self.potential(trial)

                if phi < best_phi:
                    best_phi = phi
                    best_eid = eid

            if best_eid is None:
                raise ValueError(f"No feasible node for container {cid}")

            assignment[cid] = best_eid

        return assignment


    def best_response_assignment(self) -> Dict[str, str]:
        assignment = self.greedy_init_assignment()

        self.phase1_history = []
        self.log_phase1_state(0, assignment, "greedy_init")

        rounds = 0
        while rounds < self.max_best_response_rounds:
            rounds += 1

            candidates = []

            move_trial, move_tag = self._best_move_neighbor(assignment)
            if move_trial is not None:
                candidates.append((self.potential(move_trial), move_trial, move_tag))

            swap_trial, swap_tag = self._best_swap_neighbor(assignment)
            if swap_trial is not None:
                candidates.append((self.potential(swap_trial), swap_trial, swap_tag))

            block_trial, block_tag = self._best_block_neighbor(assignment)
            if block_trial is not None:
                candidates.append((self.potential(block_trial), block_trial, block_tag))

            if not candidates:
                self.log_phase1_state(rounds, assignment, f"no_improve_round_{rounds}")
                break

            candidates.sort(key=lambda x: x[0])
            best_phi, best_trial, best_tag = candidates[0]

            if best_phi + 1e-9 < self.potential(assignment):
                assignment = best_trial
                self.log_phase1_state(rounds, assignment, f"{best_tag}_round_{rounds}")
            else:
                self.log_phase1_state(rounds, assignment, f"no_improve_round_{rounds}")
                break

        return assignment

    # =========================
    # Phase 2 + 3:
    # 排序与替换联动
    # =========================

    def future_counts(self, remaining: Set[str]) -> Counter:
        cnt = Counter()
        for cid in remaining:
            for l in self.containers[cid].layers:
                cnt[l] += 1
        return cnt

    def top_future_layers(self, remaining: Set[str], k: int) -> Set[str]:
        if not remaining:
            return set()
        cnt = self.future_counts(remaining)
        items = []
        for l, freq in cnt.items():
            val = self.layer_sizes_mb[l] * freq
            items.append((val, l))
        items.sort(key=lambda x: (-x[0], x[1]))
        return {l for _, l in items[:k]}

    def layer_priority(
        self,
        layer: str,
        hist_freq: Dict[str, int],
        slot_future_cnt: Counter,
        clock: float,
        bandwidth: float,
    ) -> float:
        f_hist = hist_freq.get(layer, 0)
        f_slot = slot_future_cnt.get(layer, 0)
        size = self.layer_sizes_mb[layer]
        cost_redl = size / max(bandwidth, 1e-8)
        centrality = self.layer_centrality.get(layer, 1)
        return clock + (
            ((f_hist + self.beta_slot * f_slot) * cost_redl * (1.0 + self.rho_centrality * centrality))
            / max(size, 1e-8)
        )

    def enforce_cache_capacity(
        self,
        cache_layers: Set[str],
        priorities: Dict[str, float],
        node: EdgeNode,
    ) -> Tuple[Set[str], Set[str]]:
        """
        Force the reusable cache to satisfy node.repo_capacity_mb.

        Important:
        - repo_capacity_mb is only the reusable layer-cache budget.
        - Layers larger than the cache budget can still be pulled for current deployment,
          but they will not be retained for future reuse.
        - cache=0 is a valid no-cache setting.
        """
        cap = int(node.repo_capacity_mb)

        if cap <= 0:
            return set(), set(cache_layers)

        kept: Set[str] = set()
        used = 0

        def keep_key(l: str):
            size = self.layer_sizes_mb[l]
            pri = priorities.get(l, 0.0)
            density = pri / max(size, 1e-8)
            centrality = self.layer_centrality.get(l, 0)
            # priority density first, then absolute priority, then centrality.
            # Smaller size is preferred when other scores are close.
            return (density, pri, centrality, -size, l)

        for l in sorted(cache_layers, key=keep_key, reverse=True):
            size = self.layer_sizes_mb[l]
            if size > cap:
                # This layer can be downloaded for current use, but cannot be retained.
                continue
            if used + size <= cap:
                kept.add(l)
                used += size

        evicted = set(cache_layers) - kept
        return kept, evicted


    def choose_eviction_set(
        self,
        cache_plus: Set[str],
        pinned: Set[str],
        need_free_mb: int,
        priorities: Dict[str, float],
        freq_scores: Optional[Dict[str, float]] = None,
    ) -> Set[str]:
        """
        cache_policy:
          - pgdsf: 原来的改造版 PGDSF / LSPGDSF，用 DP 最小化优先级损失 + overshoot
          - lru: 近似 LRU，优先淘汰 priority 最低的层
          - lfu: 近似 LFU，优先淘汰历史/未来频率最低的层
        """
        if need_free_mb <= 0:
            return set()

        candidates = [l for l in cache_plus if l not in pinned]
        if not candidates:
            return set()

        policy = getattr(self, "cache_policy", "pgdsf")

        # LRU / LFU 用贪心淘汰，作为消融 baseline
        if policy in ("lru", "lfu"):
            freq_scores = freq_scores or {}

            def evict_key(l: str):
                size = self.layer_sizes_mb[l]
                if policy == "lru":
                    # priority 越低，越先淘汰；同等情况下大层优先，减少淘汰数量
                    return (priorities.get(l, 0.0), -size, l)
                else:
                    # frequency 越低，越先淘汰；同等情况下低 priority、大层优先
                    return (freq_scores.get(l, 0.0), priorities.get(l, 0.0), -size, l)

            evicted = set()
            freed = 0
            for l in sorted(candidates, key=evict_key):
                if freed >= need_free_mb:
                    break
                evicted.add(l)
                freed += self.layer_sizes_mb[l]
            return evicted

        # 默认：原来的改造版 PGDSF / LSPGDSF
        single_ok = [l for l in candidates if self.layer_sizes_mb[l] >= need_free_mb]
        if single_ok:
            single_ok.sort(
                key=lambda l: priorities[l] + self.kappa_overshoot * (self.layer_sizes_mb[l] - need_free_mb)
            )
            return {single_ok[0]}

        unit = self.unit_mb
        items = []
        for l in candidates:
            size = self.layer_sizes_mb[l]
            w = max(1, math.ceil(size / unit))
            cost = priorities[l]
            items.append((l, size, w, cost))

        need = math.ceil(need_free_mb / unit)
        max_w = sum(w for _, _, w, _ in items)

        INF = 1e18
        dp = [INF] * (max_w + 1)
        choose: List[Optional[Set[str]]] = [None] * (max_w + 1)
        dp[0] = 0.0
        choose[0] = set()

        for lid, size, w, cost in items:
            for cur_w in range(max_w - w, -1, -1):
                if dp[cur_w] >= INF:
                    continue
                nw = cur_w + w
                overshoot = max(0, nw - need)
                val = dp[cur_w] + cost
                if nw >= need:
                    val += self.kappa_overshoot * overshoot
                if val < dp[nw]:
                    dp[nw] = val
                    choose[nw] = set(choose[cur_w])
                    choose[nw].add(lid)

        best_w = None
        best_val = INF
        for w in range(need, max_w + 1):
            if dp[w] < best_val:
                best_val = dp[w]
                best_w = w

        return choose[best_w] if best_w is not None and choose[best_w] is not None else set()

    def score_candidate(
        self,
        cid: str,
        cache: Set[str],
        remaining: Set[str],
        hist_freq: Dict[str, int],
        clock: float,
        node: EdgeNode,
    ) -> Tuple[float, Set[str], Set[str], float]:
        """
        返回:
        (gain, new_cache, evicted_set, new_clock)
        """
        c = self.containers[cid]
        future_remaining = set(remaining)
        if cid in future_remaining:
            future_remaining.remove(cid)

        if getattr(self, "disable_future_share", False):


            slot_future_cnt = {}


        else:


            slot_future_cnt = self.future_counts(future_remaining)

        # 1) 当前复用收益
        reuse_mb = sum(self.layer_sizes_mb[l] for l in c.layers if l in cache)

        # 2) 对未来的帮助
        future_share = 0.0
        for l in c.layers:
            future_share += self.layer_sizes_mb[l] * slot_future_cnt.get(l, 0)

        # 3) 新增拉取代价
        missing_layers = c.layers - cache
        pull_cost = sum(self.layer_sizes_mb[l] for l in missing_layers) / max(node.bandwidth_mb_s, 1e-8)

        # 4) Reusable cache admission.
        #
        # The current image layers are transiently available for this deployment,
        # but they are NOT required to be fully retained in the reusable cache.
        # This decouples deployment feasibility from cache capacity.
        cache_plus = set(cache) | set(c.layers)

        # Future pinned layers are only soft hints. The final hard trimming below
        # will still enforce repo_capacity_mb, so small caches remain feasible.
        future_pinned = self.top_future_layers(future_remaining, self.k_pin)
        pinned = {
            l for l in future_pinned
            if self.layer_sizes_mb[l] <= int(node.repo_capacity_mb)
        }

        # Layer priorities for replacement/admission.
        priorities: Dict[str, float] = {}
        freq_scores: Dict[str, float] = {}
        for l in cache_plus:
            priorities[l] = self.layer_priority(
                layer=l,
                hist_freq=hist_freq,
                slot_future_cnt=slot_future_cnt,
                clock=clock,
                bandwidth=node.bandwidth_mb_s,
            )
            freq_scores[l] = hist_freq.get(l, 0) + self.beta_slot * slot_future_cnt.get(l, 0)

        if int(node.repo_capacity_mb) <= 0:
            # no-cache setting: deployment still works, but nothing is retained.
            evicted = set(cache_plus)
            new_cache = set()
            new_clock = clock
        else:
            need_free = max(0, self.cache_size(cache_plus) - int(node.repo_capacity_mb))
            evicted = self.choose_eviction_set(
                cache_plus=cache_plus,
                pinned=pinned,
                need_free_mb=need_free,
                priorities=priorities,
                freq_scores=freq_scores,
            )

            new_cache = cache_plus - evicted

            # Final safety pass:
            # if pinned layers or oversized current layers still make cache overflow,
            # keep only the most valuable subset. This prevents cache size from
            # exceeding repo_capacity_mb under small-cache settings.
            if self.cache_size(new_cache) > int(node.repo_capacity_mb):
                fitted_cache, forced_evicted = self.enforce_cache_capacity(
                    cache_layers=new_cache,
                    priorities=priorities,
                    node=node,
                )
                new_cache = fitted_cache
                evicted = set(evicted) | set(forced_evicted)

            new_clock = clock
            if evicted:
                new_clock = max(clock, max(priorities.get(l, 0.0) for l in evicted))

        evict_loss = sum(priorities.get(l, 0.0) for l in evicted)

        gain = (
            self.alpha1_reuse * reuse_mb
            + self.alpha2_future * future_share
            - self.alpha3_pull * pull_cost
            - self.alpha4_evict * evict_loss
        )
        return gain, new_cache, evicted, new_clock

    def order_node(
        self,
        cids: List[str],
        node: EdgeNode,
    ) -> List[str]:
        """
        order_policy:
          - dynamic_state: 原来的动态状态感知 beam ordering
          - static_ilrsa: 静态 ILR-SA 风格排序，不使用动态替换状态，只按当前缓存复用和镜像层重叠贪心
          - arrival: 不排序，按分配后的原始顺序
        """
        policy = getattr(self, "order_policy", "dynamic_state")
        if policy == "arrival":
            return list(cids)
        if policy == "static_ilrsa":
            return self.order_node_static_ilrsa(cids, node)
        return self.order_node_with_beam(cids, node)


    def order_node_static_ilrsa(
        self,
        cids: List[str],
        node: EdgeNode,
    ) -> List[str]:
        """
        静态 ILR-SA 风格排序：
        - 不调用动态 PGDSF 替换状态；
        - 每一步优先选择与当前静态缓存重叠最大的容器；
        - 次级指标为与剩余容器的层共享潜力；
        - 选中后只把其 layer 加入静态 cache，不做容量驱逐模拟。
        这用于和 dynamic_state 排序做消融对比。
        """
        remaining = list(cids)
        seq: List[str] = []
        static_cache = set(node.initial_cache)

        while remaining:
            best_cid = None
            best_key = None

            for cid in remaining:
                c = self.containers[cid]
                reuse_mb = sum(self.layer_sizes_mb[l] for l in c.layers if l in static_cache)
                missing_mb = sum(self.layer_sizes_mb[l] for l in c.layers if l not in static_cache)

                future_share = 0
                for other in remaining:
                    if other == cid:
                        continue
                    overlap = self.containers[cid].layers & self.containers[other].layers
                    future_share += sum(self.layer_sizes_mb[l] for l in overlap)

                # reuse 越大越好，future_share 越大越好，missing 越小越好
                key = (reuse_mb, future_share, -missing_mb, cid)

                if best_key is None or key > best_key:
                    best_key = key
                    best_cid = cid

            seq.append(best_cid)
            remaining.remove(best_cid)
            static_cache |= self.containers[best_cid].layers

        return seq


    def order_node_with_beam(
        self,
        cids: List[str],
        node: EdgeNode,
    ) -> List[str]:
        if not cids:
            return []

        init_state = BeamState(
            seq=[],
            remaining=set(cids),
            cache=set(node.initial_cache),
            hist_freq={},
            clock=0.0,
            score=0.0,
        )
        beam = [init_state]

        for _ in range(len(cids)):
            new_beam: List[BeamState] = []

            for st in beam:
                if not st.remaining:
                    new_beam.append(st)
                    continue

                # 可以全枚举；如果太大可以加筛选
                for cid in sorted(st.remaining):
                    gain, new_cache, _, new_clock = self.score_candidate(
                        cid=cid,
                        cache=st.cache,
                        remaining=st.remaining,
                        hist_freq=st.hist_freq,
                        clock=st.clock,
                        node=node,
                    )

                    new_hist = dict(st.hist_freq)
                    for l in self.containers[cid].layers:
                        new_hist[l] = new_hist.get(l, 0) + 1

                    nxt = BeamState(
                        seq=st.seq + [cid],
                        remaining=set(st.remaining - {cid}),
                        cache=new_cache,
                        hist_freq=new_hist,
                        clock=new_clock,
                        score=st.score + gain,
                    )
                    new_beam.append(nxt)

            # 保留 top-B
            new_beam.sort(key=lambda x: x.score, reverse=True)
            beam = new_beam[:self.beam_width]

        beam.sort(key=lambda x: x.score, reverse=True)
        return beam[0].seq

    # =========================
    # 最终评估：顺序仿真 ACT / AMS
    # =========================

    def simulate_queue(
        self,
        ordered_cids: List[str],
        node: EdgeNode,
    ) -> QueueMetrics:
        cache = set(node.initial_cache)
        hist_freq: Dict[str, int] = {}
        clock = 0.0

        t = 0.0
        downloaded_mb = 0
        reused_mb = 0
        completion = []

        remaining = set(ordered_cids)
        step_logs: List[Dict] = []
        container_logs: List[Dict] = []

        for step_idx, cid in enumerate(ordered_cids, start=1):
            c = self.containers[cid]

            t_before = t
            cache_before_mb = self.cache_size(cache)

            # 统计下载/复用
            reuse_now = sum(self.layer_sizes_mb[l] for l in c.layers if l in cache)
            miss_now = sum(self.layer_sizes_mb[l] for l in c.layers if l not in cache)

            reused_mb += reuse_now
            downloaded_mb += miss_now

            pull_time = miss_now / max(node.bandwidth_mb_s, 1e-8)
            wait_time = t_before
            deploy_delay = wait_time + pull_time

            t += pull_time + c.run_time
            completion.append(t)

            # 调用同一套 replacement
            gain, new_cache, evicted, new_clock = self.score_candidate(
                cid=cid,
                cache=cache,
                remaining=remaining,
                hist_freq=hist_freq,
                clock=clock,
                node=node,
            )
            _ = gain

            evicted_mb = sum(self.layer_sizes_mb[l] for l in evicted)
            cache_after_mb = self.cache_size(new_cache)

            step_rec = {
                "algo": self.algo_name,
                "node_id": node.eid,
                "cid": cid,
                "service_type": c.service_type,
                "local_step": step_idx,
                "reuse_mb": reuse_now,
                "downloaded_mb": miss_now,
                "cumulative_reuse_mb": reused_mb,
                "cumulative_downloaded_mb": downloaded_mb,
                "cache_size_before_mb": cache_before_mb,
                "cache_size_after_mb": cache_after_mb,
                "evicted_mb": evicted_mb,
                "num_evicted_layers": len(evicted),
                "wait_time": wait_time,
                "pull_time": pull_time,
                "deploy_delay": deploy_delay,
                "completion_time": t,
                "run_time": c.run_time,
            }
            step_logs.append(step_rec)

            container_logs.append({
                "algo": self.algo_name,
                "cid": cid,
                "service_type": c.service_type,
                "node_id": node.eid,
                "local_step": step_idx,
                "wait_time": wait_time,
                "pull_time": pull_time,
                "deploy_delay": deploy_delay,
                "completion_time": t,
                "run_time": c.run_time,
                "reuse_mb": reuse_now,
                "downloaded_mb": miss_now,
                "cache_size_before_mb": cache_before_mb,
                "cache_size_after_mb": cache_after_mb,
                "evicted_mb": evicted_mb,
                "num_evicted_layers": len(evicted),
            })

            cache = new_cache
            clock = new_clock

            for l in c.layers:
                hist_freq[l] = hist_freq.get(l, 0) + 1

            remaining.remove(cid)

        act = sum(completion) / max(len(completion), 1)
        ams = max(completion) if completion else 0.0

        return QueueMetrics(
            completion_times=completion,
            downloaded_mb=downloaded_mb,
            reused_mb=reused_mb,
            act=act,
            ams=ams,
            final_cache=cache,
            step_logs=step_logs,
            container_logs=container_logs,
        )

    # =========================
    # 总流程
    # =========================

    def run(self) -> Dict:
        # Phase 1
        assign_map = self.best_response_assignment()

        # 每个节点的分组
        node_to_cids: Dict[str, List[str]] = {eid: [] for eid in self.nodes}
        for cid, eid in assign_map.items():
            node_to_cids[eid].append(cid)

        # 清空旧日志
        self.node_step_logs = []
        self.container_metrics = []

        # Phase 2
        ordered: Dict[str, List[str]] = {}
        node_details = {}

        total_completion_sum = 0.0
        total_num = 0
        total_downloaded = 0
        total_reused = 0
        total_makespan = 0.0

        for eid, cids in node_to_cids.items():
            ordered_seq = self.order_node(cids, self.nodes[eid])
            ordered[eid] = ordered_seq

            qm = self.simulate_queue(ordered_seq, self.nodes[eid])

            node_details[eid] = {
                "queue": ordered_seq,
                "act": qm.act,
                "ams": qm.ams,
                "downloaded_mb": qm.downloaded_mb,
                "reused_mb": qm.reused_mb,
                "completion_times": qm.completion_times,
            }

            self.node_step_logs.extend(qm.step_logs)
            self.container_metrics.extend(qm.container_logs)

            total_completion_sum += sum(qm.completion_times)
            total_num += len(ordered_seq)
            total_downloaded += qm.downloaded_mb
            total_reused += qm.reused_mb
            total_makespan += qm.ams

        self.phase2_reuse_history = self.build_phase2_reuse_history(self.node_step_logs)

        ACT = total_completion_sum / max(total_num, 1)
        AMS = total_makespan / max(len(self.nodes), 1)
        objective = self.alpha_obj * ACT + (1.0 - self.alpha_obj) * AMS

        out = {
            "assignment": node_to_cids,
            "ordered_queues": ordered,
            "summary": {
                "algo": self.algo_name,
                "num_containers": total_num,
                "num_nodes": len(self.nodes),
                "ACT": ACT,
                "AMS": AMS,
                "downloaded_mb": total_downloaded,
                "reused_mb": total_reused,
                "reuse_rate": total_reused / max(total_reused + total_downloaded, 1),
                "objective": objective,
            },
            "node_details": node_details,
            "phase1_history": self.phase1_history,
            "phase2_reuse_history": self.phase2_reuse_history,
            "node_step_logs": self.node_step_logs,
            "container_metrics": self.container_metrics,
        }
        return out


# =========================
# IO
# =========================


def load_case(path: str):
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)

    layer_sizes = obj["layer_sizes_mb"]

    containers = []
    for x in obj["containers"]:
        containers.append(
            Container(
                cid=x["cid"],
                layers=set(x["layers"]),
                resources=x["resources"],
                run_time=float(x["run_time"]),
                service_type=x.get("service_type", x.get("image_type", "default")),
            )
        )

    nodes = []
    for x in obj["nodes"]:
        nodes.append(
            EdgeNode(
                eid=x["eid"],
                resources=x["resources"],
                repo_capacity_mb=int(x["repo_capacity_mb"]),
                bandwidth_mb_s=float(x["bandwidth_mb_s"]),
                initial_cache=set(x.get("initial_cache", [])),
            )
        )

    return containers, nodes, layer_sizes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", type=str, required=True)
    parser.add_argument("--out", type=str, default="fg_dscr_result.json")
    parser.add_argument("--beam", type=int, default=4)
    parser.add_argument("--unit-mb", type=int, default=50)
    parser.add_argument("--algo-name", type=str, default="FG-DSCR")

    # Phase 1 势函数消融项
    parser.add_argument("--lambda-cong", type=float, default=1.0)
    parser.add_argument("--lambda-frag", type=float, default=0.1)
    parser.add_argument("--lambda-aff", type=float, default=0.2)

    # Phase 2 排序/缓存替换消融项
    parser.add_argument("--k-pin", type=int, default=6)
    parser.add_argument("--cache-policy", type=str, default="pgdsf", choices=["pgdsf", "lru", "lfu"])
    parser.add_argument("--order-policy", type=str, default="dynamic_state", choices=["dynamic_state", "static_ilrsa", "arrival"])
    parser.add_argument("--disable-future-share", action="store_true", help="Disable FutureShare term in dynamic ordering.")

    parser.add_argument("--max-best-response-rounds", type=int, default=20)

    # 下面三个默认是0，只加日志不改算法行为
    parser.add_argument("--lambda-balance", type=float, default=0.0)
    parser.add_argument("--lambda-idle", type=float, default=0.0)
    parser.add_argument("--theta-cong-count", type=float, default=0.0)
    parser.add_argument("--greedy-load-factor", type=float, default=0.0)
    parser.add_argument("--lambda-task-load", type=float, default=0.03)
    parser.add_argument("--task-load-power", type=float, default=2.0)
    parser.add_argument("--task-load-factor", type=float, default=1.8)
    args = parser.parse_args()

    containers, nodes, layer_sizes = load_case(args.case)

    scheduler = FGDscrScheduler(
        layer_sizes_mb=layer_sizes,
        beam_width=args.beam,
        unit_mb=args.unit_mb,
        algo_name=args.algo_name,
        lambda_cong=args.lambda_cong,
        lambda_frag=args.lambda_frag,
        lambda_aff=args.lambda_aff,
        k_pin=args.k_pin,
        cache_policy=args.cache_policy,
        order_policy=args.order_policy,
        disable_future_share=args.disable_future_share,
        max_best_response_rounds=args.max_best_response_rounds,
        lambda_balance=args.lambda_balance,
        lambda_idle=args.lambda_idle,
        theta_cong_count=args.theta_cong_count,
        greedy_load_factor=args.greedy_load_factor,
        lambda_task_load=args.lambda_task_load,
        task_load_power=args.task_load_power,
        task_load_factor=args.task_load_factor,
    )
    scheduler.set_data(containers, nodes)
    res = scheduler.run()

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2, ensure_ascii=False)

    print(json.dumps({
        "summary": res["summary"],
        "assignment": res["assignment"],
        "ordered_queues": res["ordered_queues"],
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()