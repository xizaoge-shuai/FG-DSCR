from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List, Set, Tuple
from functools import lru_cache
import json
import math
import random
import argparse


@dataclass
class Container:
    cid: str
    layers: Set[str]
    resources: Dict[str, float]
    run_time: float  # 秒


@dataclass
class EdgeNode:
    eid: str
    resources: Dict[str, float]
    repo_capacity_mb: int
    bandwidth_mb_s: float


@dataclass
class QueueStats:
    act: float
    ams: float
    completion_times: List[float]
    downloaded_mb: int
    reused_mb: int


@dataclass
class ILRSAResult:
    assignment: Dict[str, List[str]]
    ordered_queues: Dict[str, List[str]]
    summary: Dict[str, float]
    node_details: Dict[str, dict]


class ILRSA:
    """
    ILR-SA 参考复现实现
    1) Phase-1: greedy 节点部署
    2) Phase-2: Hamiltonian path + decomposition
    3) Phase-3: image layer update by future value
    """

    def __init__(
        self,
        layer_sizes_mb: Dict[str, int],
        alpha: float = 0.5,
        exact_threshold: int = 12,
        random_seed: int = 42,
        phase1_random_eviction: bool = True,
        cache_knapsack: str = "exact",   # exact / greedy
        knapsack_unit_mb: int = 50,      # exact 背包时的容量缩放单位
    ):
        self.layer_sizes_mb = dict(layer_sizes_mb)
        self.alpha = alpha
        self.exact_threshold = exact_threshold
        self.rng = random.Random(random_seed)
        self.phase1_random_eviction = phase1_random_eviction
        self.cache_knapsack = cache_knapsack
        self.knapsack_unit_mb = max(knapsack_unit_mb, 1)

    # =========================
    # 基础工具
    # =========================

    def image_size(self, c: Container) -> int:
        return sum(self.layer_sizes_mb[l] for l in c.layers)

    def overlap(self, a: Container, b: Container) -> int:
        return sum(self.layer_sizes_mb[l] for l in (a.layers & b.layers))

    def feasible(self, c: Container, node: EdgeNode) -> bool:
        for k, v in c.resources.items():
            if v > node.resources.get(k, 0):
                return False
        if self.image_size(c) > node.repo_capacity_mb:
            return False
        return True

    def future_layer_value(self, layer: str, future_queue: List[Container]) -> int:
        size = self.layer_sizes_mb[layer]
        return sum(size for c in future_queue if layer in c.layers)

    # =========================
    # Phase 3: 镜像层更新
    # =========================

    def _knapsack_exact(self, items: List[Tuple[str, int, int]], capacity_mb: int) -> Set[str]:
        """
        items: [(layer_id, size_mb, value)]
        exact knapsack，先按 knapsack_unit_mb 做容量缩放
        """
        unit = self.knapsack_unit_mb
        cap = capacity_mb // unit
        scaled = [(lid, max(1, size // unit), value) for lid, size, value in items]

        dp = [0] * (cap + 1)
        choose: List[Set[str]] = [set() for _ in range(cap + 1)]

        for lid, w, val in scaled:
            for c in range(cap, w - 1, -1):
                nv = dp[c - w] + val
                if nv > dp[c]:
                    dp[c] = nv
                    choose[c] = set(choose[c - w])
                    choose[c].add(lid)

        best_c = max(range(cap + 1), key=lambda x: dp[x])
        return choose[best_c]

    def _knapsack_greedy(self, items: List[Tuple[str, int, int]], capacity_mb: int) -> Set[str]:
        items = sorted(items, key=lambda x: (x[2] / max(x[1], 1), x[2]), reverse=True)
        keep = set()
        used = 0
        for lid, size, val in items:
            if used + size <= capacity_mb:
                keep.add(lid)
                used += size
        return keep

    def update_layers_runtime(
        self,
        current_cache: Set[str],
        current: Container,
        future_queue: List[Container],
        node: EdgeNode,
    ) -> Set[str]:
        """
        对应论文的 Phase-3.

        顺序执行场景下：
        - 当前容器可复用层 = current_cache ∩ current.layers
        - 当前容器运行需要的层必须保留
        - 其他空闲层按未来价值做背包选择
        """
        reusable_now = current_cache & current.layers
        must_keep = set(current.layers)
        _ = reusable_now  # 保留命名，便于和论文符号对应

        fixed_size = sum(self.layer_sizes_mb[l] for l in must_keep)
        if fixed_size > node.repo_capacity_mb:
            raise ValueError(
                f"container {current.cid} image size={fixed_size}MB > repo_capacity={node.repo_capacity_mb}MB"
            )

        remaining_capacity = node.repo_capacity_mb - fixed_size
        free_layers = [l for l in current_cache if l not in must_keep]

        items = []
        for lid in free_layers:
            items.append((lid, self.layer_sizes_mb[lid], self.future_layer_value(lid, future_queue)))

        if self.cache_knapsack == "exact":
            keep_free = self._knapsack_exact(items, remaining_capacity)
        else:
            keep_free = self._knapsack_greedy(items, remaining_capacity)

        new_cache = must_keep | keep_free
        return new_cache

    # =========================
    # 单节点顺序仿真
    # =========================

    def simulate_queue(
        self,
        queue: List[Container],
        node: EdgeNode,
        random_eviction: bool = False,
    ) -> QueueStats:
        """
        单节点顺序队列仿真
        - 下载时间 = 缺失层大小 / 带宽
        - 完成时间用于 ACT / AMS
        - random_eviction=True 时，用于 Phase-1 的粗粒度 simulated scheduling
        """
        cache: Set[str] = set()
        t = 0.0
        completion_times = []
        total_downloaded = 0
        total_reused = 0

        for i, c in enumerate(queue):
            reusable = cache & c.layers
            missing = c.layers - cache

            reused_mb = sum(self.layer_sizes_mb[l] for l in reusable)
            download_mb = sum(self.layer_sizes_mb[l] for l in missing)

            total_reused += reused_mb
            total_downloaded += download_mb

            download_time = download_mb / max(node.bandwidth_mb_s, 1e-8)
            t += download_time + c.run_time
            completion_times.append(t)

            after_pull_cache = set(cache) | set(c.layers)

            if random_eviction:
                # Phase-1：未来未知，随机驱逐
                kept = set(c.layers)
                free = [l for l in after_pull_cache if l not in kept]
                self.rng.shuffle(free)
                used = sum(self.layer_sizes_mb[l] for l in kept)
                for lid in free:
                    size = self.layer_sizes_mb[lid]
                    if used + size <= node.repo_capacity_mb:
                        kept.add(lid)
                        used += size
                cache = kept
            else:
                future_queue = queue[i + 1:]
                cache = self.update_layers_runtime(after_pull_cache, c, future_queue, node)

        act = sum(completion_times) / max(len(completion_times), 1)
        ams = max(completion_times) if completion_times else 0.0

        return QueueStats(
            act=act,
            ams=ams,
            completion_times=completion_times,
            downloaded_mb=total_downloaded,
            reused_mb=total_reused,
        )

    # =========================
    # Phase 1: 节点部署
    # =========================

    def deploy_phase1(self, containers: List[Container], nodes: List[EdgeNode]) -> Dict[str, List[Container]]:
        """
        对应论文 Algorithm 1：
        按输入顺序逐个容器尝试放到每个可行节点，
        对该节点当前队列 + 当前容器做粗粒度 simulated scheduling，
        计算 alpha*ACT + (1-alpha)*AMS，选最优节点。
        """
        assign: Dict[str, List[Container]] = {n.eid: [] for n in nodes}

        for c in containers:
            candidates = []
            for n in nodes:
                if not self.feasible(c, n):
                    continue

                trial_queue = assign[n.eid] + [c]
                qstats = self.simulate_queue(
                    trial_queue,
                    n,
                    random_eviction=self.phase1_random_eviction,
                )
                score = self.alpha * qstats.act + (1.0 - self.alpha) * qstats.ams
                candidates.append((score, n.eid))

            if not candidates:
                raise ValueError(f"no feasible node for container={c.cid}")

            _, best_eid = min(candidates, key=lambda x: x[0])
            assign[best_eid].append(c)

        return assign

    # =========================
    # Phase 2: 队列排序
    # =========================

    def _exact_hamiltonian_path(self, queue: List[Container]) -> List[Container]:
        """
        最大权 Hamiltonian path 的精确 DP
        """
        n = len(queue)
        if n <= 1:
            return queue[:]

        w = [[0] * n for _ in range(n)]
        for i in range(n):
            for j in range(n):
                if i != j:
                    w[i][j] = self.overlap(queue[i], queue[j])

        @lru_cache(None)
        def dp(mask: int, last: int):
            if mask == (1 << last):
                return 0, [last]

            best_val = -1
            best_path = None
            pmask = mask ^ (1 << last)

            for prev in range(n):
                if pmask & (1 << prev):
                    val, path = dp(pmask, prev)
                    val += w[prev][last]
                    if val > best_val:
                        best_val = val
                        best_path = path + [last]

            return best_val, best_path

        full = (1 << n) - 1
        best = (-1, None)
        for last in range(n):
            cand = dp(full, last)
            if cand[0] > best[0]:
                best = cand

        return [queue[i] for i in best[1]]

    def _group_queue(self, queue: List[Container]) -> List[List[Container]]:
        """
        对应论文 Algorithm 2 的分组思路：
        从未分组容器中取第一个为 anchor，
        再取 sqrt(N)-1 个与其 overlap 最大的未分组容器。
        """
        n = len(queue)
        if n == 0:
            return []

        group_size = max(1, math.ceil(math.sqrt(n)))
        ungrouped = queue[:]
        groups: List[List[Container]] = []

        while ungrouped:
            anchor = ungrouped[0]
            rest = ungrouped[1:]
            rest = sorted(rest, key=lambda x: self.overlap(anchor, x), reverse=True)
            group = [anchor] + rest[: group_size - 1]

            gids = {c.cid for c in group}
            ungrouped = [c for c in ungrouped if c.cid not in gids]
            groups.append(group)

        return groups

    def _compose_group_paths(self, group_paths: List[List[Container]]) -> List[Container]:
        """
        把 group 内 path 作为 super-node。
        每个 super-node 有两个方向（原向 / 反向），
        通过 DP 最大化组间 bridge overlap。
        """
        g = len(group_paths)
        if g == 0:
            return []
        if g == 1:
            return group_paths[0]

        oriented = {(i, 0): group_paths[i] for i in range(g)}
        oriented.update({(i, 1): list(reversed(group_paths[i])) for i in range(g)})

        def bridge(i: int, oi: int, j: int, oj: int) -> int:
            left = oriented[(i, oi)]
            right = oriented[(j, oj)]
            return self.overlap(left[-1], right[0])

        @lru_cache(None)
        def dp(mask: int, last: int, ori: int):
            if mask == (1 << last):
                return 0, [(last, ori)]

            best_val = -1
            best_trace = None
            pmask = mask ^ (1 << last)

            for prev in range(g):
                if pmask & (1 << prev):
                    for pori in (0, 1):
                        val, trace = dp(pmask, prev, pori)
                        val += bridge(prev, pori, last, ori)
                        if val > best_val:
                            best_val = val
                            best_trace = trace + [(last, ori)]

            return best_val, best_trace

        full = (1 << g) - 1
        best_val = -1
        best_trace = None

        for last in range(g):
            for ori in (0, 1):
                val, trace = dp(full, last, ori)
                if val > best_val:
                    best_val = val
                    best_trace = trace

        final_queue: List[Container] = []
        for gid, ori in best_trace:
            final_queue.extend(oriented[(gid, ori)])

        return final_queue

    def sequence_phase2(self, queue: List[Container]) -> List[Container]:
        """
        Phase 2:
        - 小队列：exact Hamiltonian path
        - 大队列：decomposition
        """
        n = len(queue)
        if n <= 1:
            return queue[:]

        if n <= self.exact_threshold:
            return self._exact_hamiltonian_path(queue)

        groups = self._group_queue(queue)
        group_paths = []

        for grp in groups:
            if len(grp) <= self.exact_threshold:
                group_paths.append(self._exact_hamiltonian_path(grp))
            else:
                # 递归分解
                group_paths.append(self.sequence_phase2(grp))

        return self._compose_group_paths(group_paths)

    # =========================
    # 整体运行
    # =========================

    def run(self, containers: List[Container], nodes: List[EdgeNode]) -> ILRSAResult:
        assign = self.deploy_phase1(containers, nodes)

        ordered: Dict[str, List[Container]] = {}
        node_details: Dict[str, dict] = {}

        total_act_sum = 0.0
        total_reused = 0
        total_downloaded = 0
        total_makespan = 0.0
        total_num = 0

        for node in nodes:
            q = assign[node.eid]
            q_sorted = self.sequence_phase2(q)
            ordered[node.eid] = q_sorted

            stats = self.simulate_queue(q_sorted, node, random_eviction=False)

            total_act_sum += sum(stats.completion_times)
            total_reused += stats.reused_mb
            total_downloaded += stats.downloaded_mb
            total_makespan += stats.ams
            total_num += len(q_sorted)

            node_details[node.eid] = {
                "queue": [c.cid for c in q_sorted],
                "act": stats.act,
                "ams": stats.ams,
                "downloaded_mb": stats.downloaded_mb,
                "reused_mb": stats.reused_mb,
                "completion_times": stats.completion_times,
            }

        ACT = total_act_sum / max(total_num, 1)
        AMS = total_makespan / max(len(nodes), 1)

        summary = {
            "num_containers": total_num,
            "num_nodes": len(nodes),
            "ACT": ACT,
            "AMS": AMS,
            "downloaded_mb": total_downloaded,
            "reused_mb": total_reused,
            "reuse_rate": total_reused / max(total_reused + total_downloaded, 1),
            "objective": self.alpha * ACT + (1.0 - self.alpha) * AMS,
        }

        return ILRSAResult(
            assignment={k: [c.cid for c in v] for k, v in assign.items()},
            ordered_queues={k: [c.cid for c in v] for k, v in ordered.items()},
            summary=summary,
            node_details=node_details,
        )


# =========================
# 数据读写
# =========================

def load_case_from_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)

    layer_sizes = obj["layer_sizes_mb"]

    containers = [
        Container(
            cid=x["cid"],
            layers=set(x["layers"]),
            resources=x["resources"],
            run_time=float(x["run_time"]),
        )
        for x in obj["containers"]
    ]

    nodes = [
        EdgeNode(
            eid=x["eid"],
            resources=x["resources"],
            repo_capacity_mb=int(x["repo_capacity_mb"]),
            bandwidth_mb_s=float(x["bandwidth_mb_s"]),
        )
        for x in obj["nodes"]
    ]

    return containers, nodes, layer_sizes


def save_result_json(res: ILRSAResult, path: str):
    out = {
        "assignment": res.assignment,
        "ordered_queues": res.ordered_queues,
        "summary": res.summary,
        "node_details": res.node_details,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)


# =========================
# main
# =========================

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", type=str, required=True)
    parser.add_argument("--out", type=str, default="ilrsa_result.json")
    parser.add_argument("--alpha", type=float, default=0.5)
    parser.add_argument("--exact-threshold", type=int, default=12)
    parser.add_argument("--knapsack", type=str, default="exact", choices=["exact", "greedy"])
    parser.add_argument("--knapsack-unit-mb", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    containers, nodes, layer_sizes = load_case_from_json(args.case)

    solver = ILRSA(
        layer_sizes_mb=layer_sizes,
        alpha=args.alpha,
        exact_threshold=args.exact_threshold,
        cache_knapsack=args.knapsack,
        knapsack_unit_mb=args.knapsack_unit_mb,
        random_seed=args.seed,
    )

    res = solver.run(containers, nodes)
    save_result_json(res, args.out)

    print(json.dumps({
        "summary": res.summary,
        "assignment": res.assignment,
        "ordered_queues": res.ordered_queues,
    }, indent=2, ensure_ascii=False))