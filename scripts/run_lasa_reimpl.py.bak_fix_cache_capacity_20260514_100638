import argparse
import json
import math
import os
import time
from collections import defaultdict, OrderedDict
from typing import Dict, List, Set, Tuple, Any


def load_json(p: str) -> Any:
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(obj: Any, p: str):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def get_node_id(node: Dict[str, Any], idx: int) -> str:
    return (
        node.get("eid")
        or node.get("id")
        or node.get("nid")
        or node.get("name")
        or node.get("node_id")
        or f"edge-{idx+1}"
    )


def size_of_layers(layers: Set[str], layer_sizes: Dict[str, float]) -> float:
    return sum(float(layer_sizes.get(l, 0.0)) for l in layers)


def normalize_layer_sizes(layer_sizes):
    out = {}
    for k, v in layer_sizes.items():
        try:
            out[k] = float(v)
        except Exception:
            out[k] = 0.0
    return out


class LASAReimpl:
    """
    LASA-inspired baseline:
      1) layer grouping: group layers by the same container-set relation
      2) LCAA-inspired assignment: greedily select container-node pair
         score = ((1-alpha)*incremental_layer_size + alpha*existing_layer_size) / bandwidth
      3) GLSA-inspired sequencing: on each node, select the container with the least
         remaining grouped layer size first.

    这个实现用于适配当前 FG-DSCR 的 case 格式。
    """

    def __init__(
        self,
        case: Dict[str, Any],
        alpha: float = 0.5,
        cache_policy: str = "lru",
        ignore_resource_capacity: bool = True,
        algo_name: str = "LASA-reimpl",
    ):
        self.case = case
        self.alpha = alpha
        self.cache_policy = cache_policy
        self.ignore_resource_capacity = ignore_resource_capacity
        self.algo_name = algo_name

        self.layer_sizes = normalize_layer_sizes(case.get("layer_sizes_mb", {}))

        self.containers = {}
        for c in case["containers"]:
            cid = c["cid"]
            self.containers[cid] = {
                "cid": cid,
                "layers": set(c.get("layers", [])),
                "resources": c.get("resources", {}),
                "run_time": float(c.get("run_time", c.get("runtime", 20.0))),
                "raw": c,
            }

        self.nodes = {}
        for i, n in enumerate(case["nodes"]):
            eid = get_node_id(n, i)
            self.nodes[eid] = {
                "eid": eid,
                "resources": n.get("resources", {}),
                "repo_capacity_mb": float(n.get("repo_capacity_mb", n.get("storage", n.get("cache_mb", 1024)))),
                "bandwidth_mb_s": float(n.get("bandwidth_mb_s", n.get("bandwidth", 60.0))),
                "initial_cache": set(n.get("initial_cache", [])),
                "raw": n,
            }

    def resource_feasible(self, used: Dict[str, Dict[str, float]], eid: str, cid: str) -> bool:
        if self.ignore_resource_capacity:
            return True

        node_res = self.nodes[eid].get("resources", {})
        c_res = self.containers[cid].get("resources", {})

        for r in ["cpu", "mem", "disk"]:
            cap = float(node_res.get(r, 1e18))
            cur = float(used[eid].get(r, 0.0))
            req = float(c_res.get(r, 0.0))
            if cur + req > cap:
                return False
        return True

    def assign_containers(self) -> Dict[str, List[str]]:
        """
        LCAA-inspired container assignment.
        """
        remaining = set(self.containers.keys())

        # L_k: 每个节点当前已经“计划下载/已有”的层集合
        node_layers = {
            eid: set(info["initial_cache"])
            for eid, info in self.nodes.items()
        }

        assignment = {eid: [] for eid in self.nodes}
        used_res = {eid: defaultdict(float) for eid in self.nodes}

        while remaining:
            best = None

            for cid in remaining:
                c_layers = self.containers[cid]["layers"]

                for eid, ninfo in self.nodes.items():
                    if not self.resource_feasible(used_res, eid, cid):
                        continue

                    before = node_layers[eid]
                    after = before | c_layers

                    inc_size = size_of_layers(after - before, self.layer_sizes)
                    exist_size = size_of_layers(before, self.layer_sizes)
                    bw = max(float(ninfo["bandwidth_mb_s"]), 1e-9)

                    score = ((1.0 - self.alpha) * inc_size + self.alpha * exist_size) / bw

                    key = (score, inc_size, len(assignment[eid]), eid, cid)

                    if best is None or key < best[0]:
                        best = (key, cid, eid)

            # 如果资源约束导致没有候选，则退化为忽略资源约束继续分配
            if best is None:
                cid = sorted(remaining)[0]
                eid = min(self.nodes.keys(), key=lambda x: len(assignment[x]))
            else:
                _, cid, eid = best

            assignment[eid].append(cid)
            node_layers[eid] |= self.containers[cid]["layers"]

            for r, v in self.containers[cid].get("resources", {}).items():
                try:
                    used_res[eid][r] += float(v)
                except Exception:
                    pass

            remaining.remove(cid)

        return assignment

    def group_layers_for_node(self, cids: List[str]) -> Dict[Tuple[str, ...], Set[str]]:
        """
        层分组：被同一组容器需要的层放到一个 group。
        """
        if not cids:
            return {}

        layer_to_cids = defaultdict(list)
        cid_set = set(cids)

        all_layers = set()
        for cid in cids:
            all_layers |= self.containers[cid]["layers"]

        for l in all_layers:
            users = []
            for cid in cids:
                if l in self.containers[cid]["layers"]:
                    users.append(cid)
            layer_to_cids[tuple(sorted(users))].append(l)

        groups = {}
        for key, layers in layer_to_cids.items():
            groups[key] = set(layers)
        return groups

    def order_node_lasa(self, cids: List[str]) -> List[str]:
        """
        GLSA-inspired sequencing:
        根据剩余未排序 layer group size 选择容器。
        """
        remaining = list(cids)
        ordered = []
        sequenced_layers = set()

        groups = self.group_layers_for_node(cids)

        while remaining:
            best = None

            for cid in remaining:
                needed = set()
                for users, layers in groups.items():
                    if cid in users:
                        needed |= layers
                rest = needed - sequenced_layers
                rest_size = size_of_layers(rest, self.layer_sizes)

                # 论文 Algorithm 2 是选择剩余层大小最小的容器
                key = (rest_size, len(rest), cid)

                if best is None or key < best[0]:
                    best = (key, cid, rest)

            _, cid, rest = best
            ordered.append(cid)
            sequenced_layers |= self.containers[cid]["layers"]
            remaining.remove(cid)

        return ordered

    def evict_if_needed(self, cache: Set[str], last_used: Dict[str, int], protected: Set[str], capacity_mb: float) -> Set[str]:
        """
        简单 LRU eviction，用于适配当前有限 cache case。
        LASA 原文主要是 storage capacity + layer sequencing，不强调动态 eviction。
        这里为了公平适配 FG-DSCR 的有限 cache setting。
        """
        if capacity_mb <= 0:
            return cache

        while size_of_layers(cache, self.layer_sizes) > capacity_mb + 1e-9:
            candidates = [l for l in cache if l not in protected]
            if not candidates:
                break

            # LRU: last_used 越小越先淘汰；同等情况下大层优先
            victim = min(
                candidates,
                key=lambda l: (last_used.get(l, -1), -float(self.layer_sizes.get(l, 0.0)), l)
            )
            cache.remove(victim)

        return cache

    def simulate(self, ordered_queues: Dict[str, List[str]]) -> Dict[str, Any]:
        node_details = {}
        container_metrics = {}

        total_downloaded = 0.0
        total_reused = 0.0
        completion_times = []

        clock_counter = 0

        for eid, q in ordered_queues.items():
            ninfo = self.nodes[eid]
            bw = max(float(ninfo["bandwidth_mb_s"]), 1e-9)
            cap = float(ninfo["repo_capacity_mb"])

            cache = set(ninfo["initial_cache"])
            last_used = {l: 0 for l in cache}

            t = 0.0
            node_downloaded = 0.0
            node_reused = 0.0

            for pos, cid in enumerate(q):
                c = self.containers[cid]
                layers = c["layers"]

                hit_layers = layers & cache
                miss_layers = layers - cache

                reused_mb = size_of_layers(hit_layers, self.layer_sizes)
                downloaded_mb = size_of_layers(miss_layers, self.layer_sizes)

                pull_time = downloaded_mb / bw
                start_time = t + pull_time
                finish_time = start_time + float(c["run_time"])

                total_downloaded += downloaded_mb
                total_reused += reused_mb
                node_downloaded += downloaded_mb
                node_reused += reused_mb

                clock_counter += 1
                for l in hit_layers:
                    last_used[l] = clock_counter

                cache |= miss_layers
                for l in miss_layers:
                    last_used[l] = clock_counter

                # 当前容器需要的层不淘汰
                cache = self.evict_if_needed(cache, last_used, protected=layers, capacity_mb=cap)

                container_metrics[cid] = {
                    "node": eid,
                    "position": pos,
                    "pull_time": pull_time,
                    "start_time": start_time,
                    "finish_time": finish_time,
                    "downloaded_mb": downloaded_mb,
                    "reused_mb": reused_mb,
                }

                completion_times.append(finish_time)
                t = finish_time

            node_details[eid] = {
                "num_containers": len(q),
                "finish_time": t,
                "downloaded_mb": node_downloaded,
                "reused_mb": node_reused,
                "final_cache_mb": size_of_layers(cache, self.layer_sizes),
                "final_cache_layers": len(cache),
            }

        n = len(completion_times)
        act = sum(completion_times) / n if n else 0.0
        ams = max((v["finish_time"] for v in node_details.values()), default=0.0)
        objective = 0.5 * act + 0.5 * ams
        reuse_rate = total_reused / max(total_reused + total_downloaded, 1e-9)

        summary = {
            "algo": self.algo_name,
            "num_containers": len(self.containers),
            "num_nodes": len(self.nodes),
            "ACT": act,
            "AMS": ams,
            "downloaded_mb": int(round(total_downloaded)),
            "reused_mb": int(round(total_reused)),
            "reuse_rate": reuse_rate,
            "objective": objective,
        }

        return {
            "summary": summary,
            "node_details": node_details,
            "container_metrics": container_metrics,
        }

    def run(self):
        t0 = time.time()

        assignment = self.assign_containers()

        ordered_queues = {}
        for eid, cids in assignment.items():
            ordered_queues[eid] = self.order_node_lasa(cids)

        sim = self.simulate(ordered_queues)

        sim["assignment"] = assignment
        sim["ordered_queues"] = ordered_queues
        sim["summary"]["elapsed_s_internal"] = time.time() - t0
        sim["meta"] = {
            "alpha": self.alpha,
            "cache_policy": self.cache_policy,
            "ignore_resource_capacity": self.ignore_resource_capacity,
            "note": "LASA-inspired reimplementation adapted to FG-DSCR finite-cache cases.",
        }
        return sim


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--alpha", type=float, default=0.5)
    ap.add_argument("--cache-policy", type=str, default="lru", choices=["lru"])
    ap.add_argument("--algo-name", type=str, default="LASA-reimpl")
    ap.add_argument("--enforce-resource-capacity", action="store_true")
    args = ap.parse_args()

    case = load_json(args.case)
    solver = LASAReimpl(
        case=case,
        alpha=args.alpha,
        cache_policy=args.cache_policy,
        ignore_resource_capacity=not args.enforce_resource_capacity,
        algo_name=args.algo_name,
    )
    res = solver.run()
    save_json(res, args.out)

    print(json.dumps(res["summary"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
