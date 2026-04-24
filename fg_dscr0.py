from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple, Optional
import json
import math
import copy
import argparse
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
        # Phase 1 weights
        lambda_cong: float = 1.0,
        lambda_frag: float = 1.0,
        lambda_aff: float = 0.2,
        tiny_gap_thresholds: Optional[Dict[str, float]] = None,
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
    ):
        self.layer_sizes_mb = dict(layer_sizes_mb)
        self.alpha_obj = alpha_obj

        self.lambda_cong = lambda_cong
        self.lambda_frag = lambda_frag
        self.lambda_aff = lambda_aff

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

        self.tiny_gap_thresholds = tiny_gap_thresholds or {
            "cpu": 0.15,
            "mem": 0.15,
            "disk": 0.15,
        }

        self.containers: Dict[str, Container] = {}
        self.nodes: Dict[str, EdgeNode] = {}
        self.layer_centrality: Dict[str, int] = {}

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

    def image_size(self, cid: str) -> int:
        return sum(self.layer_sizes_mb[l] for l in self.containers[cid].layers)

    def cache_size(self, cache_layers: Set[str]) -> int:
        return sum(self.layer_sizes_mb[l] for l in cache_layers)

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

        # 单镜像基本可行性
        for q, demand in c.resources.items():
            if demand > node.resources.get(q, 0.0):
                return False

        # 当前时间片聚合资源约束
        used = defaultdict(float)
        for other_cid, other_eid in assignment.items():
            if other_eid != eid or other_cid == cid:
                continue
            oc = self.containers[other_cid]
            for q, v in oc.resources.items():
                used[q] += v

        for q, v in c.resources.items():
            if used[q] + v > node.resources.get(q, 0.0):
                return False
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
        node = self.nodes[eid]
        used = defaultdict(float)
        for cid, ne in assignment.items():
            if ne != eid:
                continue
            c = self.containers[cid]
            for q, v in c.resources.items():
                used[q] += v

        ratios = []
        tiny_gap_pen = 0.0
        for q, cap in node.resources.items():
            rem = max(0.0, cap - used[q])
            rho = rem / max(cap, 1e-8)
            ratios.append(rho)
            if 0.0 < rho < self.tiny_gap_thresholds.get(q, 0.15):
                tiny_gap_pen += 1.0

        if len(ratios) == 0:
            return 0.0
        mean_r = sum(ratios) / len(ratios)
        var_r = sum((x - mean_r) ** 2 for x in ratios) / len(ratios)
        return var_r + tiny_gap_pen

    def affinity_gain(self, eid: str, layer_cnt: Counter) -> float:
        # 层共享亲和：sum s_l * log(1+n_jl)
        gain = 0.0
        for l, k in layer_cnt.items():
            if k > 0:
                gain += self.layer_sizes_mb[l] * math.log(1.0 + k)
        return gain

    def potential(self, assignment: Dict[str, str]) -> float:
        layer_cnts = self.node_layer_counts(assignment)
        total = 0.0
        for eid, node in self.nodes.items():
            D_j = self.distinct_missing_size(eid, layer_cnts[eid])
            Frag_j = self.fragmentation_penalty(eid, assignment)
            Aff_j = self.affinity_gain(eid, layer_cnts[eid])
            total += (
                self.lambda_cong * (D_j ** 2) / max(node.bandwidth_mb_s, 1e-8)
                + self.lambda_frag * Frag_j
                - self.lambda_aff * Aff_j
            )
        return total

    def greedy_init_assignment(self) -> Dict[str, str]:
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
        for cid in cids:
            best_eid = None
            best_phi = float("inf")
            for eid in self.nodes:
                if not self.feasible_assign(cid, eid, assignment):
                    continue
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

        improved = True
        rounds = 0
        while improved and rounds < self.max_best_response_rounds:
            improved = False
            rounds += 1

            for cid in list(self.containers.keys()):
                cur_eid = assignment[cid]
                cur_phi = self.potential(assignment)

                best_eid = cur_eid
                best_phi = cur_phi

                for eid in self.nodes:
                    if eid == cur_eid:
                        continue
                    trial = dict(assignment)
                    del trial[cid]
                    if not self.feasible_assign(cid, eid, trial):
                        continue
                    trial[cid] = eid
                    phi = self.potential(trial)
                    if phi + 1e-9 < best_phi:
                        best_phi = phi
                        best_eid = eid

                if best_eid != cur_eid:
                    assignment[cid] = best_eid
                    improved = True

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
        items.sort(reverse=True)
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

    def choose_eviction_set(
        self,
        cache_plus: Set[str],
        pinned: Set[str],
        need_free_mb: int,
        priorities: Dict[str, float],
    ) -> Set[str]:
        """
        在满足释放空间的前提下，最小化：
            sum(priority[l] for l in E) + kappa * overshoot
        这里对候选层做一个缩放版 DP。
        """
        if need_free_mb <= 0:
            return set()

        candidates = [l for l in cache_plus if l not in pinned]
        if not candidates:
            return set()

        # 先尝试单层满足需求
        single_ok = [l for l in candidates if self.layer_sizes_mb[l] >= need_free_mb]
        if single_ok:
            single_ok.sort(key=lambda l: priorities[l] + self.kappa_overshoot * (self.layer_sizes_mb[l] - need_free_mb))
            return {single_ok[0]}

        # 缩放 DP
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

        # 4) 先把当前镜像层加进缓存
        cache_plus = set(cache) | set(c.layers)

        # Pinned layers = 当前镜像层 + 未来 top-k 关键层
        pinned = set(c.layers) | self.top_future_layers(future_remaining, self.k_pin)

        # 层优先级
        priorities: Dict[str, float] = {}
        for l in cache_plus:
            priorities[l] = self.layer_priority(
                layer=l,
                hist_freq=hist_freq,
                slot_future_cnt=slot_future_cnt,
                clock=clock,
                bandwidth=node.bandwidth_mb_s,
            )

        need_free = max(0, self.cache_size(cache_plus) - node.repo_capacity_mb)
        evicted = self.choose_eviction_set(cache_plus, pinned, need_free, priorities)

        evict_loss = sum(priorities[l] for l in evicted)
        new_cache = cache_plus - evicted
        new_clock = clock
        if evicted:
            new_clock = max(clock, max(priorities[l] for l in evicted))

        gain = (
            self.alpha1_reuse * reuse_mb
            + self.alpha2_future * future_share
            - self.alpha3_pull * pull_cost
            - self.alpha4_evict * evict_loss
        )
        return gain, new_cache, evicted, new_clock

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
                for cid in list(st.remaining):
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
        for cid in ordered_cids:
            c = self.containers[cid]

            # 统计下载/复用
            reuse_now = sum(self.layer_sizes_mb[l] for l in c.layers if l in cache)
            miss_now = sum(self.layer_sizes_mb[l] for l in c.layers if l not in cache)

            reused_mb += reuse_now
            downloaded_mb += miss_now

            pull_time = miss_now / max(node.bandwidth_mb_s, 1e-8)
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
            _ = gain  # 评估时不需要
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

        # Phase 2
        ordered: Dict[str, List[str]] = {}
        node_details = {}

        total_completion_sum = 0.0
        total_num = 0
        total_downloaded = 0
        total_reused = 0
        total_makespan = 0.0

        for eid, cids in node_to_cids.items():
            ordered_seq = self.order_node_with_beam(cids, self.nodes[eid])
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

            total_completion_sum += sum(qm.completion_times)
            total_num += len(ordered_seq)
            total_downloaded += qm.downloaded_mb
            total_reused += qm.reused_mb
            total_makespan += qm.ams

        ACT = total_completion_sum / max(total_num, 1)
        AMS = total_makespan / max(len(self.nodes), 1)
        objective = self.alpha_obj * ACT + (1.0 - self.alpha_obj) * AMS

        out = {
            "assignment": node_to_cids,
            "ordered_queues": ordered,
            "summary": {
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
    args = parser.parse_args()

    containers, nodes, layer_sizes = load_case(args.case)

    scheduler = FGDscrScheduler(
        layer_sizes_mb=layer_sizes,
        beam_width=args.beam,
        unit_mb=args.unit_mb,
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