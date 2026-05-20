#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
LASA paper-style reimplementation for FG-DSCR experiments.

Based on the core ideas of:
J. Lou et al., "Efficient Container Assignment and Layer Sequencing in Edge Computing,"
IEEE TSC 2023.

Implemented ideas:
1) Layer grouping: layers shared by the same set of containers are grouped.
2) LCAA-style assignment:
      score = ((1-alpha) * incremental_layer_size + alpha * existing_layer_size) / bandwidth
   where incremental_layer_size captures layer sharing benefit and existing_layer_size
   works as a load-balancing proxy.
3) GLSA-style sequencing:
   after assignment, each node sequences containers greedily by minimum remaining
   not-yet-downloaded layer-group size.
4) Cache-aware evaluation:
   cache/repo_capacity_mb is treated as extra reusable layer cache, not native K8s runtime disk.
   cache=0 means no-cache, containers can still run but layers are not retained.
"""

import argparse
import json
import os
import time
from dataclasses import dataclass
from collections import OrderedDict, defaultdict
from typing import Dict, List, Set, Tuple, Any


@dataclass
class Container:
    cid: str
    layers: Set[str]
    resources: Dict[str, float]
    run_time: float
    service_type: str = "default"


@dataclass
class EdgeNode:
    eid: str
    resources: Dict[str, float]
    repo_capacity_mb: float
    bandwidth_mb_s: float
    initial_cache: Set[str]


def load_case(path: str):
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)

    layer_sizes = {str(k): float(v) for k, v in obj["layer_sizes_mb"].items()}

    containers = []
    for x in obj["containers"]:
        containers.append(
            Container(
                cid=str(x["cid"]),
                layers=set(str(l) for l in x["layers"]),
                resources={k: float(v) for k, v in x.get("resources", {}).items()},
                run_time=float(x.get("run_time", 0.0)),
                service_type=x.get("service_type", x.get("image_type", "default")),
            )
        )

    nodes = []
    for x in obj["nodes"]:
        nodes.append(
            EdgeNode(
                eid=str(x["eid"]),
                resources={k: float(v) for k, v in x.get("resources", {}).items()},
                repo_capacity_mb=float(
                    x.get("repo_capacity_mb", x.get("cache_capacity_mb", x.get("cache_mb", 0.0)))
                ),
                bandwidth_mb_s=float(x.get("bandwidth_mb_s", x.get("bandwidth", 100.0))),
                initial_cache=set(str(l) for l in x.get("initial_cache", [])),
            )
        )

    return containers, nodes, layer_sizes


def save_json(obj: Any, path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


class LASAPaperReimpl:
    def __init__(
        self,
        containers: List[Container],
        nodes: List[EdgeNode],
        layer_sizes_mb: Dict[str, float],
        alpha_lasa: float = 0.5,
        alpha_obj: float = 0.5,
        max_containers_per_node: int = 10**18,
        use_resource_feasible: bool = True,
        cache_policy: str = "lru",
        algo_name: str = "LASA-paper-reimpl",
    ):
        self.containers_list = containers
        self.nodes_list = nodes
        self.layer_sizes_mb = layer_sizes_mb
        self.alpha_lasa = float(alpha_lasa)
        self.alpha_obj = float(alpha_obj)
        self.max_containers_per_node = int(max_containers_per_node)
        self.use_resource_feasible = bool(use_resource_feasible)
        self.cache_policy = cache_policy
        self.algo_name = algo_name

        self.containers: Dict[str, Container] = {c.cid: c for c in containers}
        self.nodes: Dict[str, EdgeNode] = {n.eid: n for n in nodes}

        # resource usage after assignment
        self.node_resource_used = {
            n.eid: {r: 0.0 for r in n.resources}
            for n in nodes
        }

        # ===== LASA layer grouping =====
        self.layer_to_group: Dict[str, str] = {}
        self.group_to_layers: Dict[str, Set[str]] = {}
        self.group_size_mb: Dict[str, float] = {}
        self.container_groups: Dict[str, Set[str]] = {}
        self.initial_group_cache: Dict[str, Set[str]] = {}

        self.build_layer_groups()

    def build_layer_groups(self):
        """
        Group layers that have exactly the same relation with containers.
        Signature = tuple of container ids that require this layer.
        """
        layer_users = defaultdict(list)

        for c in self.containers_list:
            for l in c.layers:
                layer_users[l].append(c.cid)

        sig_to_gid = {}
        gid_counter = 0

        for l in sorted(layer_users):
            sig = tuple(sorted(layer_users[l]))
            if sig not in sig_to_gid:
                gid = f"g{gid_counter:06d}"
                gid_counter += 1
                sig_to_gid[sig] = gid
                self.group_to_layers[gid] = set()
            gid = sig_to_gid[sig]
            self.layer_to_group[l] = gid
            self.group_to_layers[gid].add(l)

        for gid, layers in self.group_to_layers.items():
            self.group_size_mb[gid] = sum(self.layer_sizes_mb[l] for l in layers)

        for c in self.containers_list:
            self.container_groups[c.cid] = set(self.layer_to_group[l] for l in c.layers)

        for node in self.nodes_list:
            self.initial_group_cache[node.eid] = set(
                self.layer_to_group[l]
                for l in node.initial_cache
                if l in self.layer_to_group
            )

    def group_size(self, groups: Set[str]) -> float:
        return sum(self.group_size_mb[g] for g in groups)

    def resource_feasible(self, cid: str, eid: str) -> bool:
        if not self.use_resource_feasible:
            return True

        c = self.containers[cid]
        n = self.nodes[eid]
        used = self.node_resource_used[eid]

        for r, req in c.resources.items():
            cap = n.resources.get(r, float("inf"))
            if used.get(r, 0.0) + req > cap + 1e-9:
                return False
        return True

    def add_resource_usage(self, cid: str, eid: str):
        c = self.containers[cid]
        used = self.node_resource_used[eid]
        for r, req in c.resources.items():
            used[r] = used.get(r, 0.0) + req

    def lcaa_assignment(self) -> Dict[str, str]:
        """
        Paper Algorithm 1 LCAA-style assignment.

        L_k: existing layer-group set on node k.
        N_k: number of assigned containers on node k.
        score = ((1-alpha)*sum(DL) + alpha*sum(L_k)) / b_k
        """
        node_groups: Dict[str, Set[str]] = {
            eid: set(self.initial_group_cache[eid])
            for eid in self.nodes
        }
        node_counts = {eid: 0 for eid in self.nodes}
        remaining = set(self.containers.keys())
        assignment: Dict[str, str] = {}

        total_group_size = sum(self.group_size_mb.values())
        min_bw = max(min(n.bandwidth_mb_s for n in self.nodes_list), 1e-8)
        default_score = 2.0 * total_group_size / min_bw

        while remaining:
            best_cid = None
            best_eid = None
            best_score = default_score

            for cid in sorted(remaining):
                c_groups = self.container_groups[cid]

                for eid, node in self.nodes.items():
                    if node_counts[eid] + 1 > self.max_containers_per_node:
                        continue
                    if not self.resource_feasible(cid, eid):
                        continue

                    old_groups = node_groups[eid]
                    new_groups = old_groups | c_groups
                    delta_groups = new_groups - old_groups

                    inc_size = self.group_size(delta_groups)
                    existing_size = self.group_size(old_groups)

                    score = (
                        (1.0 - self.alpha_lasa) * inc_size
                        + self.alpha_lasa * existing_size
                    ) / max(node.bandwidth_mb_s, 1e-8)

                    if score < best_score - 1e-12:
                        best_score = score
                        best_cid = cid
                        best_eid = eid
                    elif abs(score - best_score) <= 1e-12:
                        # deterministic tie-breaking
                        if best_cid is None or (cid, eid) < (best_cid, best_eid):
                            best_cid = cid
                            best_eid = eid

            if best_cid is None or best_eid is None:
                raise ValueError(
                    "LASA LCAA failed: no feasible container-node pair. "
                    "If this occurs in large-N cases, resource capacities are insufficient."
                )

            assignment[best_cid] = best_eid
            node_counts[best_eid] += 1
            node_groups[best_eid] |= self.container_groups[best_cid]
            self.add_resource_usage(best_cid, best_eid)
            remaining.remove(best_cid)

        return assignment

    def glsa_order_node(self, cids: List[str], eid: str) -> List[str]:
        """
        Practical GLSA-style sequencing.

        The original GLSA uses Sidney Decomposition and then greedily orders containers
        inside each set by minimum remaining layer size. To keep the implementation
        dependency-free and robust, we directly apply the same greedy remaining-layer
        principle at the node level.
        """
        remaining = set(cids)
        sequenced_groups = set(self.initial_group_cache[eid])
        ordered = []

        while remaining:
            best_cid = None
            best_key = None

            for cid in sorted(remaining):
                c_groups = self.container_groups[cid]
                missing_groups = c_groups - sequenced_groups
                rem_size = self.group_size(missing_groups)

                # tie-break:
                # smaller remaining size first;
                # then larger total shared group size to encourage shared-image clustering;
                # then cid for determinism.
                total_size = self.group_size(c_groups)
                key = (rem_size, -total_size, cid)

                if best_key is None or key < best_key:
                    best_key = key
                    best_cid = cid

            ordered.append(best_cid)
            sequenced_groups |= self.container_groups[best_cid]
            remaining.remove(best_cid)

        return ordered

    def cache_used_mb(self, cache: OrderedDict) -> float:
        return sum(self.layer_sizes_mb[l] for l in cache.keys())

    def admit_layer(self, cache: OrderedDict, layer: str, cap: float) -> float:
        """
        Extra reusable cache admission.
        Layers can always be pulled for current deployment.
        If cap=0 or layer_size>cap, the layer is not retained.
        """
        cap = float(cap)
        if cap <= 0:
            return 0.0

        size = self.layer_sizes_mb[layer]
        if size > cap:
            return 0.0

        if layer in cache:
            cache.move_to_end(layer)
            return 0.0

        evicted_mb = 0.0
        while self.cache_used_mb(cache) + size > cap and cache:
            victim, _ = cache.popitem(last=False)
            evicted_mb += self.layer_sizes_mb[victim]

        if self.cache_used_mb(cache) + size <= cap:
            cache[layer] = True

        return evicted_mb

    def simulate_queue(self, ordered_cids: List[str], node: EdgeNode):
        cache = OrderedDict()
        for l in sorted(node.initial_cache):
            if l in self.layer_sizes_mb:
                # initial cache also respects cache budget
                self.admit_layer(cache, l, node.repo_capacity_mb)

        t = 0.0
        downloaded_mb = 0.0
        reused_mb = 0.0
        evicted_mb_total = 0.0
        completion = []

        step_logs = []
        container_logs = []

        for step_idx, cid in enumerate(ordered_cids, start=1):
            c = self.containers[cid]
            t_before = t
            cache_before = self.cache_used_mb(cache)

            hit_layers = [l for l in c.layers if l in cache]
            miss_layers = [l for l in c.layers if l not in cache]

            reuse_now = sum(self.layer_sizes_mb[l] for l in hit_layers)
            miss_now = sum(self.layer_sizes_mb[l] for l in miss_layers)

            # hit layers become recently used
            for l in hit_layers:
                cache.move_to_end(l)

            # missing layers are pulled for this deployment; admitted if cache allows
            evicted_now = 0.0
            for l in sorted(miss_layers):
                evicted_now += self.admit_layer(cache, l, node.repo_capacity_mb)

            pull_time = miss_now / max(node.bandwidth_mb_s, 1e-8)
            wait_time = t_before
            deploy_delay = wait_time + pull_time

            t += pull_time + c.run_time
            completion.append(t)

            downloaded_mb += miss_now
            reused_mb += reuse_now
            evicted_mb_total += evicted_now

            rec = {
                "algo": self.algo_name,
                "node_id": node.eid,
                "cid": cid,
                "service_type": c.service_type,
                "local_step": step_idx,
                "reuse_mb": reuse_now,
                "downloaded_mb": miss_now,
                "cumulative_reuse_mb": reused_mb,
                "cumulative_downloaded_mb": downloaded_mb,
                "cache_size_before_mb": cache_before,
                "cache_size_after_mb": self.cache_used_mb(cache),
                "evicted_mb": evicted_now,
                "wait_time": wait_time,
                "pull_time": pull_time,
                "deploy_delay": deploy_delay,
                "completion_time": t,
                "run_time": c.run_time,
            }
            step_logs.append(rec)
            container_logs.append(rec.copy())

        act = sum(completion) / max(len(completion), 1)
        ams = max(completion) if completion else 0.0

        return {
            "completion_times": completion,
            "downloaded_mb": downloaded_mb,
            "reused_mb": reused_mb,
            "evicted_mb": evicted_mb_total,
            "act": act,
            "ams": ams,
            "step_logs": step_logs,
            "container_logs": container_logs,
        }

    def run(self):
        t0 = time.time()

        assignment_map = self.lcaa_assignment()

        node_to_cids = {eid: [] for eid in self.nodes}
        for cid, eid in assignment_map.items():
            node_to_cids[eid].append(cid)

        ordered = {}
        node_details = {}

        total_completion_sum = 0.0
        total_num = 0
        total_downloaded = 0.0
        total_reused = 0.0
        total_evicted = 0.0
        total_makespan = 0.0

        node_step_logs = []
        container_metrics = []

        for eid, cids in node_to_cids.items():
            seq = self.glsa_order_node(cids, eid)
            ordered[eid] = seq

            qm = self.simulate_queue(seq, self.nodes[eid])

            node_details[eid] = {
                "queue": seq,
                "act": qm["act"],
                "ams": qm["ams"],
                "downloaded_mb": qm["downloaded_mb"],
                "reused_mb": qm["reused_mb"],
                "evicted_mb": qm["evicted_mb"],
                "completion_times": qm["completion_times"],
                "num_containers": len(seq),
            }

            node_step_logs.extend(qm["step_logs"])
            container_metrics.extend(qm["container_logs"])

            total_completion_sum += sum(qm["completion_times"])
            total_num += len(seq)
            total_downloaded += qm["downloaded_mb"]
            total_reused += qm["reused_mb"]
            total_evicted += qm["evicted_mb"]
            total_makespan += qm["ams"]

        ACT = total_completion_sum / max(total_num, 1)
        AMS = total_makespan / max(len(self.nodes), 1)
        objective = self.alpha_obj * ACT + (1.0 - self.alpha_obj) * AMS
        reuse_rate = total_reused / max(total_reused + total_downloaded, 1.0)

        summary = {
            "algo": self.algo_name,
            "num_containers": total_num,
            "num_nodes": len(self.nodes),
            "num_layer_groups": len(self.group_to_layers),
            "num_unique_layers": len(self.layer_sizes_mb),
            "ACT": ACT,
            "AMS": AMS,
            "downloaded_mb": int(round(total_downloaded)),
            "reused_mb": int(round(total_reused)),
            "cache_hit_mb": int(round(total_reused)),
            "evicted_mb": int(round(total_evicted)),
            "reuse_rate": reuse_rate,
            "objective": objective,
            "elapsed_s_internal": time.time() - t0,
            "alpha_lasa": self.alpha_lasa,
            "alpha_obj": self.alpha_obj,
            "cache_policy": self.cache_policy,
        }

        return {
            "assignment": node_to_cids,
            "ordered_queues": ordered,
            "summary": summary,
            "node_details": node_details,
            "node_step_logs": node_step_logs,
            "container_metrics": container_metrics,
            "layer_grouping": {
                "layer_to_group": self.layer_to_group,
                "group_size_mb": self.group_size_mb,
                "group_to_layers": {k: sorted(v) for k, v in self.group_to_layers.items()},
            },
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--alpha-lasa", type=float, default=0.5)
    ap.add_argument("--alpha-obj", type=float, default=0.5)
    ap.add_argument("--max-containers-per-node", type=int, default=10**18)
    ap.add_argument("--no-resource-feasible", action="store_true")
    ap.add_argument("--algo-name", default="LASA-paper-reimpl")
    args = ap.parse_args()

    containers, nodes, layer_sizes = load_case(args.case)

    solver = LASAPaperReimpl(
        containers=containers,
        nodes=nodes,
        layer_sizes_mb=layer_sizes,
        alpha_lasa=args.alpha_lasa,
        alpha_obj=args.alpha_obj,
        max_containers_per_node=args.max_containers_per_node,
        # LASA paper mainly uses storage capacity and running-container limit.
        # In this reimplementation, cache/repo capacity is treated as reusable-layer cache,
        # so CPU/mem/disk static feasibility should not block layer-aware scheduling.
        use_resource_feasible=False,
        algo_name=args.algo_name,
    )

    res = solver.run()
    save_json(res, args.out)
    print(json.dumps(res["summary"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
