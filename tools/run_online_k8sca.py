#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Online K8s-CA simulator for FG-DSCR and baselines.

Correct experimental semantics:
  For each request:
    1) Kubernetes-style cumulative resource filter:
         used[node][q] + request[c][q] <= capacity[node][q]
    2) Run the selected policy only on feasible nodes.
    3) If no feasible edge node exists, send request to __CA_GUARANTEE__.
    4) Edge and CA requests are both counted in ACT / AMS / downloaded / objective.

Resource model:
  cumulative request reservation, no release during the experiment.
  This matches long-lived service/container placement more than transient batch jobs.

CA model:
  virtual guarantee pool. By default CA requests are handled in parallel:
    finish_time = ca_extra_delay + full_image_mb / ca_bandwidth + run_time.
  Reuse on CA is counted as 0; full image size is counted as downloaded.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List, Set, Tuple, Optional

CA_EID = "__CA_GUARANTEE__"
RES_KEYS = ["cpu", "mem", "disk"]


# ============================================================
# Basic IO / parsing
# ============================================================

def load_json(p: str) -> Any:
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(obj: Any, p: str):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def import_module_from_path(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import module from {path}")
    mod = importlib.util.module_from_spec(spec)

    # Important for dataclass / postponed annotations:
    # dataclasses may look up sys.modules[cls.__module__] during class creation.
    sys.modules[name] = mod

    spec.loader.exec_module(mod)
    return mod


def node_id(n: Dict[str, Any], idx: int = 0) -> str:
    return str(
        n.get("eid")
        or n.get("id")
        or n.get("nid")
        or n.get("name")
        or n.get("node_id")
        or f"edge-{idx+1}"
    )


def c_id(c: Dict[str, Any], idx: int = 0) -> str:
    return str(c.get("cid") or c.get("id") or c.get("name") or f"c{idx}")


def c_layers(c: Dict[str, Any]) -> List[str]:
    return [str(x) for x in (c.get("layers") or c.get("image_layers") or c.get("layer_ids") or [])]


def c_res(c: Dict[str, Any], q: str) -> float:
    return float((c.get("resources", {}) or {}).get(q, 0.0))


def n_res(n: Dict[str, Any], q: str) -> float:
    return float((n.get("resources", {}) or {}).get(q, 0.0))


def run_time(c: Dict[str, Any]) -> float:
    return float(c.get("run_time", c.get("runtime", c.get("duration", 0.0))))


def bandwidth(n: Dict[str, Any]) -> float:
    return float(n.get("bandwidth_mb_s", n.get("bandwidth", 1.0)) or 1.0)


def cache_cap(n: Dict[str, Any]) -> float:
    return float(n.get("repo_capacity_mb", n.get("cache_capacity_mb", n.get("cache_mb", 1024))) or 0.0)


def initial_cache(n: Dict[str, Any]) -> Set[str]:
    return set(str(x) for x in (n.get("initial_cache", []) or []))


def layer_mb(layer_sizes: Dict[str, float], l: str) -> float:
    v = layer_sizes.get(str(l), 0.0)
    try:
        v = float(v)
    except Exception:
        v = 0.0
    return v if v > 0 else 1.0


def image_size(c: Dict[str, Any], layer_sizes: Dict[str, float]) -> float:
    return sum(layer_mb(layer_sizes, l) for l in c_layers(c))


def cache_size(cache: Set[str], layer_sizes: Dict[str, float]) -> float:
    return sum(layer_mb(layer_sizes, l) for l in cache)


def build_layer_pop(containers: List[Dict[str, Any]]) -> Counter:
    pop = Counter()
    for c in containers:
        for l in set(c_layers(c)):
            pop[l] += 1
    return pop


def reused_missing_mb(c: Dict[str, Any], cache: Set[str], layer_sizes: Dict[str, float]) -> Tuple[float, float]:
    reused = 0.0
    missing = 0.0
    for l in c_layers(c):
        if l in cache:
            reused += layer_mb(layer_sizes, l)
        else:
            missing += layer_mb(layer_sizes, l)
    return reused, missing


def evict_to_fit(cache: Set[str], cap: float, layer_sizes: Dict[str, float], layer_pop: Counter, protected: Optional[Set[str]] = None) -> Set[str]:
    cache = set(cache)
    protected = set(protected or set())

    if cap <= 0:
        return set()

    if cache_size(cache, layer_sizes) <= cap + 1e-9:
        return cache

    def key(l: str):
        # low popularity first; tie: larger layers first
        return (layer_pop.get(l, 0), -layer_mb(layer_sizes, l), str(l))

    removable = [l for l in cache if l not in protected]
    removable.sort(key=key)

    for l in removable:
        if cache_size(cache, layer_sizes) <= cap + 1e-9:
            break
        cache.remove(l)

    if cache_size(cache, layer_sizes) > cap + 1e-9:
        rest = list(cache)
        rest.sort(key=key)
        for l in rest:
            if cache_size(cache, layer_sizes) <= cap + 1e-9:
                break
            cache.remove(l)

    return cache


def add_to_cache(cache: Set[str], layers: Set[str], cap: float, layer_sizes: Dict[str, float], layer_pop: Counter) -> Set[str]:
    cache = set(cache)
    for l in layers:
        cache.add(l)
    return evict_to_fit(cache, cap, layer_sizes, layer_pop, protected=set(layers))


def k8s_feasible(c: Dict[str, Any], n: Dict[str, Any], used: Dict[str, float]) -> bool:
    for q in RES_KEYS:
        if float(used.get(q, 0.0)) + c_res(c, q) > n_res(n, q) + 1e-9:
            return False
    return True


def add_used(used: Dict[str, float], c: Dict[str, Any]):
    for q in RES_KEYS:
        used[q] = float(used.get(q, 0.0)) + c_res(c, q)


def resource_pressure(c: Dict[str, Any], n: Dict[str, Any], used: Dict[str, float]) -> Tuple[float, float]:
    ratios = []
    for q in RES_KEYS:
        cap = max(n_res(n, q), 1e-9)
        ratios.append((float(used.get(q, 0.0)) + c_res(c, q)) / cap)
    avg = sum(ratios) / len(ratios)
    max_ratio = max(ratios)
    imbalance = sum(abs(x - avg) for x in ratios) / len(ratios)
    return max_ratio, imbalance


# ============================================================
# Optional original modules: FG and ILRSA
# ============================================================

class OriginalContexts:
    def __init__(self, args, case):
        self.args = args
        self.case = case
        self.fg_mod = None
        self.fg_scheduler = None
        self.fg_containers = None
        self.fg_nodes = None

        self.ilr_mod = None
        self.ilr_solver = None
        self.ilr_containers = None
        self.ilr_nodes = None

    def need_fg(self):
        if self.fg_scheduler is not None:
            return

        path = self.args.fg_script
        if not os.path.exists(path):
            raise RuntimeError(f"FG script not found: {path}")

        self.fg_mod = import_module_from_path(path, "fg_dscr_online_imported")
        containers, nodes, layer_sizes = self.fg_mod.load_case(self.args.case)

        scheduler = self.fg_mod.FGDscrScheduler(
            layer_sizes_mb=layer_sizes,
            beam_width=self.args.beam,
            unit_mb=self.args.unit_mb,
            algo_name="FG-DSCR-GC-OnlineK8sCA",
            lambda_cong=self.args.lambda_cong,
            bw_gamma=self.args.bw_gamma,
            lambda_frag=self.args.lambda_frag,
            lambda_aff=self.args.lambda_aff,
            k_pin=self.args.k_pin,
            cache_policy=self.args.cache_policy,
            cache_bw_eta=self.args.cache_bw_eta,
            cache_bw_ref=self.args.cache_bw_ref,
            order_policy=self.args.fg_order_policy,
            disable_future_share=self.args.disable_future_share,
            max_best_response_rounds=0,
            phase1_neighbor_mode="move",
            lambda_balance=0.0,
            lambda_idle=0.0,
            theta_cong_count=self.args.theta_cong_count,
            greedy_load_factor=self.args.greedy_load_factor,
            lambda_task_load=self.args.lambda_task_load,
            lambda_cache_core=self.args.lambda_cache_core,
            cache_core_ratio=self.args.cache_core_ratio,
            task_load_power=self.args.task_load_power,
            task_load_factor=self.args.task_load_factor,
        )
        scheduler.set_data(containers, nodes)

        self.fg_scheduler = scheduler
        self.fg_containers = {c.cid: c for c in containers}
        self.fg_nodes = {n.eid: n for n in nodes}

    def need_ilr(self):
        if self.ilr_solver is not None:
            return

        path = self.args.ilrsa_script
        if not os.path.exists(path):
            raise RuntimeError(f"ILRSA script not found: {path}")

        self.ilr_mod = import_module_from_path(path, "ilrsa_online_imported")
        containers, nodes, layer_sizes = self.ilr_mod.load_case_from_json(self.args.case)
        solver = self.ilr_mod.ILRSA(
            layer_sizes_mb=layer_sizes,
            alpha=self.args.ilrsa_alpha,
            exact_threshold=self.args.ilrsa_exact_threshold,
            random_seed=self.args.seed,
            cache_knapsack=self.args.ilrsa_knapsack,
            knapsack_unit_mb=self.args.ilrsa_knapsack_unit_mb,
        )
        self.ilr_solver = solver
        self.ilr_containers = {c.cid: c for c in containers}
        self.ilr_nodes = {n.eid: n for n in nodes}


# ============================================================
# Policy scores
# ============================================================

def choose_lrscheduler(c, feasible, nodes_by_id, caches, used, layer_sizes, args):
    best = None
    total_mb = max(image_size(c, layer_sizes), 1e-9)

    for nid in feasible:
        n = nodes_by_id[nid]
        exist_mb, missing_mb = reused_missing_mb(c, caches[nid], layer_sizes)

        # ComputeLayerScore in your LRScheduler-style script.
        res_score = exist_mb / total_mb * 100.0 / 2.0

        cpu_cap = max(n_res(n, "cpu"), 1e-9)
        mem_cap = max(n_res(n, "mem"), 1e-9)
        occu_cpu = (used[nid]["cpu"] + c_res(c, "cpu")) / cpu_cap
        occu_mem = (used[nid]["mem"] + c_res(c, "mem")) / mem_cap
        std = abs((occu_cpu - occu_mem) / 2.0)

        if exist_mb > args.lrs_layer_mb_threshold and std < args.lrs_std_threshold and occu_cpu < args.lrs_cpu_threshold:
            dynamic_weight = args.lrs_high_weight
        else:
            dynamic_weight = args.lrs_low_weight

        layer_plugin_score = res_score * dynamic_weight
        resource_balance_score = max(0.0, 1.0 - 2.0 * std) * 100.0
        final_score = args.lrs_plugin_weight_layer * layer_plugin_score + args.lrs_plugin_weight_resource * resource_balance_score

        key = (final_score, -missing_mb, -sum(used[nid].values()), nid)
        if best is None or key > best[0]:
            best = (key, nid)

    return best[1] if best else None


def choose_gahrl(c, feasible, nodes_by_id, caches, used, queue_finish, layer_sizes, norm_time, norm_storage, args):
    best = None

    for nid in feasible:
        n = nodes_by_id[nid]
        bw = bandwidth(n)

        reused, missing = reused_missing_mb(c, caches[nid], layer_sizes)
        total_mb = max(reused + missing, 1e-9)

        storage_cost = missing
        startup_time = missing / max(bw, 1e-9)
        service_latency = queue_finish[nid] + startup_time + run_time(c)

        max_pressure, imbalance = resource_pressure(c, n, used[nid])
        layer_hit_ratio = reused / total_mb

        cap = cache_cap(n)
        new_layers = {l for l in c_layers(c) if l not in caches[nid]}
        new_cache_mb = cache_size(caches[nid], layer_sizes) + sum(layer_mb(layer_sizes, l) for l in new_layers)
        cache_pressure = max(0.0, new_cache_mb - cap) / max(cap, 1e-9)

        latency_term = service_latency / max(norm_time, 1e-9)
        storage_term = storage_cost / max(norm_storage, 1e-9)

        cost = (
            args.gahrl_lambda_latency * latency_term
            + (1.0 - args.gahrl_lambda_latency) * storage_term
            + args.gahrl_w_resource * max_pressure
            + args.gahrl_w_imbalance * imbalance
            + args.gahrl_w_cache_pressure * cache_pressure
            - args.gahrl_w_layer_hit * layer_hit_ratio
        )

        key = (-cost, layer_hit_ratio, -missing, -queue_finish[nid], nid)
        if best is None or key > best[0]:
            best = (key, nid)

    return best[1] if best else None


def choose_orr(c, feasible, nodes_by_id, caches, used, queue_finish, queue_len, layer_sizes, args):
    best = None

    for nid in feasible:
        n = nodes_by_id[nid]
        bw = bandwidth(n)
        reused, missing = reused_missing_mb(c, caches[nid], layer_sizes)
        total = max(reused + missing, 1e-9)
        hit = reused / total
        max_pressure, imbalance = resource_pressure(c, n, used[nid])

        cost = (
            queue_finish[nid]
            + missing / max(bw, 1e-9)
            + run_time(c)
            + args.orr_w_resource * max_pressure
            + args.orr_w_queue_len * queue_len[nid]
            - args.orr_w_reuse * hit
        )

        key = (-cost, hit, -missing, -queue_finish[nid], nid)
        if best is None or key > best[0]:
            best = (key, nid)

    return best[1] if best else None


def norm01(xs):
    xs = list(xs)
    if not xs:
        return []
    lo, hi = min(xs), max(xs)
    if abs(hi - lo) < 1e-12:
        return [0.5 for _ in xs]
    return [(x - lo) / (hi - lo) for x in xs]


def fg_k8s_pack_score(c, n, used, args):
    """
    Higher is better.

    fill_score:
      prefer tighter placement, i.e., best-fit style packing.

    shape_score:
      prefer balanced residual shape, reducing CPU/MEM/DISK stranded fragments.
    """
    after_utils = []
    residuals = []

    for q in RES_KEYS:
        cap = max(n_res(n, q), 1e-9)
        after = (float(used.get(q, 0.0)) + c_res(c, q)) / cap
        after = max(0.0, min(1.0, after))
        after_utils.append(after)
        residuals.append(1.0 - after)

    fill_score = sum(after_utils) / len(after_utils)

    # residual shape: smaller spread is better
    spread = max(residuals) - min(residuals)
    shape_score = 1.0 - spread

    # optional: avoid creating tiny unusable leftovers in exactly one dimension
    tiny = float(getattr(args, "fg_k8s_tiny_residual", 0.0))
    tiny_penalty = 0.0
    if tiny > 0:
        for r in residuals:
            if 0.0 < r < tiny:
                tiny_penalty += 1.0

    return fill_score + args.fg_k8s_shape_weight * shape_score - args.fg_k8s_tiny_weight * tiny_penalty

def choose_fg(c, feasible, current_assignment_map, ctx: OriginalContexts, nodes_by_id, caches, used, layer_sizes):
    ctx.need_fg()
    sched = ctx.fg_scheduler
    cid = c_id(c)

    cand = []

    for nid in feasible:
        trial = dict(current_assignment_map)
        trial[cid] = nid

        # Original FG potential: lower is better.
        phi = sched.potential(trial)

        n = nodes_by_id[nid]
        reused, missing = reused_missing_mb(c, caches[nid], layer_sizes)
        hit = reused / max(reused + missing, 1e-9)

        pack = fg_k8s_pack_score(c, n, used[nid], ctx.args)

        # Queue/load proxy: smaller current queue is better.
        load_proxy = len([1 for _, e in current_assignment_map.items() if e == nid])

        cand.append({
            "nid": nid,
            "phi_good": -float(phi),
            "pack": float(pack),
            "hit": float(hit),
            "load_good": -float(load_proxy),
        })

    if not cand:
        return None

    phi_norm = norm01([x["phi_good"] for x in cand])
    pack_norm = norm01([x["pack"] for x in cand])
    hit_norm = norm01([x["hit"] for x in cand])
    load_norm = norm01([x["load_good"] for x in cand])

    best = None
    for i, x in enumerate(cand):
        score = (
            ctx.args.fg_k8s_phi_weight * phi_norm[i]
            + ctx.args.fg_k8s_pack_weight * pack_norm[i]
            + ctx.args.fg_k8s_reuse_weight * hit_norm[i]
            + ctx.args.fg_k8s_load_weight * load_norm[i]
        )

        key = (score, phi_norm[i], pack_norm[i], hit_norm[i], x["nid"])
        if best is None or key > best[0]:
            best = (key, x["nid"])

    return best[1] if best else None


def choose_ilrsa(c, feasible, assignment, ctx: OriginalContexts):
    ctx.need_ilr()
    solver = ctx.ilr_solver
    cid = c_id(c)
    c_obj = ctx.ilr_containers[cid]

    best = None
    for nid in feasible:
        n_obj = ctx.ilr_nodes[nid]
        trial_q = [ctx.ilr_containers[x] for x in assignment[nid]] + [c_obj]
        qstats = solver.simulate_queue(trial_q, n_obj, random_eviction=True)
        score = ctx.args.ilrsa_alpha * qstats.act + (1.0 - ctx.args.ilrsa_alpha) * qstats.ams
        key = (-score, -len(assignment[nid]), nid)
        if best is None or key > best[0]:
            best = (key, nid)

    return best[1] if best else None


# ============================================================
# LASA global LCAA-style assignment
# ============================================================

def lasa_group_layers(cids: List[str], c_by_id: Dict[str, Dict[str, Any]]) -> Dict[Tuple[str, ...], Set[str]]:
    if not cids:
        return {}

    all_layers = set()
    for cid in cids:
        all_layers |= set(c_layers(c_by_id[cid]))

    groups = {}
    for l in all_layers:
        users = []
        for cid in cids:
            if l in c_layers(c_by_id[cid]):
                users.append(cid)
        groups.setdefault(tuple(sorted(users)), set()).add(l)
    return groups


def lasa_order_node(cids: List[str], c_by_id: Dict[str, Dict[str, Any]], layer_sizes: Dict[str, float]) -> List[str]:
    remaining = list(cids)
    ordered = []
    sequenced_layers = set()
    groups = lasa_group_layers(cids, c_by_id)

    while remaining:
        best = None
        for cid in remaining:
            needed = set()
            for users, layers in groups.items():
                if cid in users:
                    needed |= layers
            rest = needed - sequenced_layers
            rest_size = sum(layer_mb(layer_sizes, l) for l in rest)
            key = (rest_size, len(rest), cid)
            if best is None or key < best[0]:
                best = (key, cid)
        _, cid = best
        ordered.append(cid)
        sequenced_layers |= set(c_layers(c_by_id[cid]))
        remaining.remove(cid)

    return ordered


def assign_lasa(case, args, nodes_by_id, c_by_id, layer_sizes, layer_pop):
    containers = case["containers"]
    nodes = case["nodes"]

    assignment = {node_id(n, i): [] for i, n in enumerate(nodes)}
    ca_cids = []
    used = {nid: {q: 0.0 for q in RES_KEYS} for nid in assignment}
    node_layers = {nid: set(nodes_by_id[nid].get("initial_cache", []) or []) for nid in assignment}

    remaining = set(c_id(c, i) for i, c in enumerate(containers))

    while remaining:
        best = None

        for cid in sorted(remaining):
            c = c_by_id[cid]
            cset = set(c_layers(c))

            for nid, n in nodes_by_id.items():
                if not k8s_feasible(c, n, used[nid]):
                    continue

                before = node_layers[nid]
                after = before | cset
                inc_size = sum(layer_mb(layer_sizes, l) for l in (after - before))
                exist_size = sum(layer_mb(layer_sizes, l) for l in before)
                bw = max(bandwidth(n), 1e-9)

                # Same style as LASA-reimpl LCAA score.
                score = ((1.0 - args.lasa_alpha) * inc_size + args.lasa_alpha * exist_size) / bw
                key = (score, inc_size, len(assignment[nid]), nid, cid)

                if best is None or key < best[0]:
                    best = (key, cid, nid)

        if best is None:
            # No remaining request can be placed on edge.
            for cid in sorted(remaining):
                ca_cids.append(cid)
            remaining.clear()
            break

        _, cid, nid = best
        assignment[nid].append(cid)
        add_used(used[nid], c_by_id[cid])
        node_layers[nid] |= set(c_layers(c_by_id[cid]))
        remaining.remove(cid)

    return assignment, ca_cids, used


# ============================================================
# Online assignment
# ============================================================

def compute_norms(case, layer_sizes):
    containers = case["containers"]
    nodes = case["nodes"]
    avg_container_mb = sum(image_size(c, layer_sizes) for c in containers) / max(len(containers), 1)
    avg_runtime = sum(run_time(c) for c in containers) / max(len(containers), 1)
    avg_bw = sum(bandwidth(n) for n in nodes) / max(len(nodes), 1)
    norm_time = max(avg_runtime + avg_container_mb / max(avg_bw, 1e-9), 1e-9)
    norm_storage = max(avg_container_mb, 1e-9)
    return norm_time, norm_storage


def online_assign(case, args, ctx: OriginalContexts):
    layer_sizes = {str(k): float(v) for k, v in case["layer_sizes_mb"].items()}
    containers = case["containers"]
    nodes = case["nodes"]

    c_by_id = {c_id(c, i): c for i, c in enumerate(containers)}
    nodes_by_id = {node_id(n, i): n for i, n in enumerate(nodes)}
    layer_pop = build_layer_pop(containers)

    if args.algo == "lasa":
        assignment, ca_cids, used = assign_lasa(case, args, nodes_by_id, c_by_id, layer_sizes, layer_pop)
        return assignment, ca_cids, used, c_by_id, nodes_by_id, layer_sizes, layer_pop

    assignment = {nid: [] for nid in nodes_by_id}
    ca_cids = []
    used = {nid: {q: 0.0 for q in RES_KEYS} for nid in nodes_by_id}
    caches = {nid: initial_cache(n) for nid, n in nodes_by_id.items()}
    queue_finish = {nid: 0.0 for nid in nodes_by_id}
    queue_len = {nid: 0 for nid in nodes_by_id}

    norm_time, norm_storage = compute_norms(case, layer_sizes)

    current_assignment_map = {}

    for idx, c in enumerate(containers):
        cid = c_id(c, idx)
        feasible = [nid for nid, n in nodes_by_id.items() if k8s_feasible(c, n, used[nid])]

        if not feasible:
            ca_cids.append(cid)
            continue

        if args.algo == "fg":
            best_nid = choose_fg(c, feasible, current_assignment_map, ctx, nodes_by_id, caches, used, layer_sizes)
        elif args.algo == "ilrsa":
            best_nid = choose_ilrsa(c, feasible, assignment, ctx)
        elif args.algo == "lrscheduler":
            best_nid = choose_lrscheduler(c, feasible, nodes_by_id, caches, used, layer_sizes, args)
        elif args.algo == "gahrl":
            best_nid = choose_gahrl(
                c, feasible, nodes_by_id, caches, used, queue_finish,
                layer_sizes, norm_time, norm_storage, args
            )
        elif args.algo == "orr":
            best_nid = choose_orr(c, feasible, nodes_by_id, caches, used, queue_finish, queue_len, layer_sizes, args)
        else:
            raise RuntimeError(f"Unknown algo: {args.algo}")

        if best_nid is None:
            ca_cids.append(cid)
            continue

        assignment[best_nid].append(cid)
        current_assignment_map[cid] = best_nid
        add_used(used[best_nid], c)

        # Update approximate online state for policies.
        n = nodes_by_id[best_nid]
        reused, missing = reused_missing_mb(c, caches[best_nid], layer_sizes)
        queue_finish[best_nid] += missing / max(bandwidth(n), 1e-9) + run_time(c)
        queue_len[best_nid] += 1
        miss_layers = set(c_layers(c)) - caches[best_nid]
        caches[best_nid] = add_to_cache(caches[best_nid], miss_layers, cache_cap(n), layer_sizes, layer_pop)

    return assignment, ca_cids, used, c_by_id, nodes_by_id, layer_sizes, layer_pop


# ============================================================
# Queue ordering and simulation
# ============================================================

def generic_cache_greedy_order(cids, c_by_id, init_cache, layer_sizes):
    remaining = list(cids)
    cache = set(init_cache)
    out = []

    while remaining:
        best_idx = 0
        best_key = None

        for idx, cid in enumerate(remaining):
            c = c_by_id[cid]
            reused, missing = reused_missing_mb(c, cache, layer_sizes)
            hit = reused / max(reused + missing, 1e-9)
            key = (hit, reused, -missing, -run_time(c), cid)
            if best_key is None or key > best_key:
                best_key = key
                best_idx = idx

        cid = remaining.pop(best_idx)
        out.append(cid)
        cache |= set(c_layers(c_by_id[cid]))

    return out


def simulate_generic_edge_queue(cids, c_by_id, node, layer_sizes, layer_pop, order_policy="cache"):
    if order_policy == "cache":
        q = generic_cache_greedy_order(cids, c_by_id, initial_cache(node), layer_sizes)
    else:
        q = list(cids)

    cache = initial_cache(node)
    cap = cache_cap(node)
    bw = bandwidth(node)

    t = 0.0
    completion = []
    downloaded = 0.0
    reused_total = 0.0
    container_metrics = []

    for pos, cid in enumerate(q, start=1):
        c = c_by_id[cid]
        reused, missing = reused_missing_mb(c, cache, layer_sizes)
        pull = missing / max(bw, 1e-9)
        start = t
        finish = t + pull + run_time(c)
        t = finish

        downloaded += missing
        reused_total += reused
        completion.append(finish)

        miss_layers = set(c_layers(c)) - cache
        cache = add_to_cache(cache, miss_layers, cap, layer_sizes, layer_pop)

        container_metrics.append({
            "cid": cid,
            "node": node_id(node),
            "position": pos,
            "start_time": start,
            "finish_time": finish,
            "downloaded_mb": missing,
            "reused_mb": reused,
            "pull_time": pull,
            "run_time": run_time(c),
        })

    return {
        "ordered": q,
        "finish_time": t,
        "completion_times": completion,
        "downloaded_mb": downloaded,
        "reused_mb": reused_total,
        "container_metrics": container_metrics,
    }


def simulate_ca_queue(ca_cids, c_by_id, layer_sizes, ca_bw, ca_extra_delay, ca_parallel=True):
    completion = []
    downloaded = 0.0
    metrics = []
    t = 0.0

    for pos, cid in enumerate(ca_cids, start=1):
        c = c_by_id[cid]
        full_mb = image_size(c, layer_sizes)
        pull = full_mb / max(ca_bw, 1e-9)
        duration = ca_extra_delay + pull + run_time(c)

        if ca_parallel:
            start = 0.0
            finish = duration
        else:
            start = t
            finish = t + duration
            t = finish

        downloaded += full_mb
        completion.append(finish)
        metrics.append({
            "cid": cid,
            "node": CA_EID,
            "position": pos,
            "start_time": start,
            "finish_time": finish,
            "downloaded_mb": full_mb,
            "reused_mb": 0.0,
            "pull_time": pull,
            "run_time": run_time(c),
            "ca_extra_delay": ca_extra_delay,
        })

    makespan = max(completion) if ca_parallel and completion else (t if completion else 0.0)

    return {
        "ordered": list(ca_cids),
        "finish_time": makespan,
        "completion_times": completion,
        "downloaded_mb": downloaded,
        "reused_mb": 0.0,
        "container_metrics": metrics,
    }


def final_simulate(case, args, ctx, assignment, ca_cids, c_by_id, nodes_by_id, layer_sizes, layer_pop):
    ordered_queues = {}
    node_details = {}
    container_metrics = {}

    all_completion = []
    total_downloaded = 0.0
    total_reused = 0.0
    makespans = []

    # Edge nodes.
    if args.algo == "fg":
        ctx.need_fg()
        sched = ctx.fg_scheduler
        for nid, cids in assignment.items():
            fg_node = ctx.fg_nodes[nid]
            ordered = sched.order_node(cids, fg_node)
            qm = sched.simulate_queue(ordered, fg_node)

            ordered_queues[nid] = ordered
            node_details[nid] = {
                "num_containers": len(ordered),
                "finish_time": qm.ams,
                "downloaded_mb": qm.downloaded_mb,
                "reused_mb": qm.reused_mb,
                "completion_times": qm.completion_times,
            }
            for rec in qm.container_logs:
                container_metrics[rec["cid"]] = rec

            all_completion.extend(qm.completion_times)
            total_downloaded += qm.downloaded_mb
            total_reused += qm.reused_mb
            makespans.append(qm.ams)

    elif args.algo == "ilrsa":
        ctx.need_ilr()
        solver = ctx.ilr_solver
        for nid, cids in assignment.items():
            ilr_node = ctx.ilr_nodes[nid]
            q_objs = [ctx.ilr_containers[cid] for cid in cids]
            ordered_objs = solver.sequence_phase2(q_objs)
            ordered = [c.cid for c in ordered_objs]
            stats = solver.simulate_queue(ordered_objs, ilr_node, random_eviction=False)

            ordered_queues[nid] = ordered
            node_details[nid] = {
                "num_containers": len(ordered),
                "finish_time": stats.ams,
                "downloaded_mb": stats.downloaded_mb,
                "reused_mb": stats.reused_mb,
                "completion_times": stats.completion_times,
            }
            for cid, ft in zip(ordered, stats.completion_times):
                container_metrics[cid] = {
                    "cid": cid,
                    "node": nid,
                    "finish_time": ft,
                }

            all_completion.extend(stats.completion_times)
            total_downloaded += stats.downloaded_mb
            total_reused += stats.reused_mb
            makespans.append(stats.ams)

    else:
        for nid, cids in assignment.items():
            n = nodes_by_id[nid]
            if args.algo == "lasa":
                ordered = lasa_order_node(cids, c_by_id, layer_sizes)
                sim = simulate_generic_edge_queue(ordered, c_by_id, n, layer_sizes, layer_pop, order_policy="arrival")
            else:
                sim = simulate_generic_edge_queue(cids, c_by_id, n, layer_sizes, layer_pop, order_policy=args.queue_order)

            ordered_queues[nid] = sim["ordered"]
            node_details[nid] = {
                "num_containers": len(sim["ordered"]),
                "finish_time": sim["finish_time"],
                "downloaded_mb": sim["downloaded_mb"],
                "reused_mb": sim["reused_mb"],
                "completion_times": sim["completion_times"],
            }
            for rec in sim["container_metrics"]:
                container_metrics[rec["cid"]] = rec

            all_completion.extend(sim["completion_times"])
            total_downloaded += sim["downloaded_mb"]
            total_reused += sim["reused_mb"]
            makespans.append(sim["finish_time"])

    # CA virtual guarantee.
    if args.ca_bandwidth <= 0:
        ca_bw = sum(bandwidth(n) for n in nodes_by_id.values()) / max(len(nodes_by_id), 1)
    else:
        ca_bw = args.ca_bandwidth

    ca_sim = simulate_ca_queue(
        ca_cids=ca_cids,
        c_by_id=c_by_id,
        layer_sizes=layer_sizes,
        ca_bw=ca_bw,
        ca_extra_delay=args.ca_extra_delay,
        ca_parallel=not args.ca_serial,
    )

    if ca_cids:
        ordered_queues[CA_EID] = ca_sim["ordered"]
        node_details[CA_EID] = {
            "num_containers": len(ca_cids),
            "finish_time": ca_sim["finish_time"],
            "downloaded_mb": ca_sim["downloaded_mb"],
            "reused_mb": 0.0,
            "completion_times": ca_sim["completion_times"],
            "ca_bandwidth_mb_s": ca_bw,
            "ca_extra_delay": args.ca_extra_delay,
            "ca_parallel": not args.ca_serial,
        }
        for rec in ca_sim["container_metrics"]:
            container_metrics[rec["cid"]] = rec

        all_completion.extend(ca_sim["completion_times"])
        total_downloaded += ca_sim["downloaded_mb"]
        makespans.append(ca_sim["finish_time"])

    total = len(case["containers"])
    edge_num = total - len(ca_cids)
    ca_num = len(ca_cids)

    ACT = sum(all_completion) / max(len(all_completion), 1)

    if args.ams_mode == "max":
        AMS = max(makespans) if makespans else 0.0
    else:
        denom = len(nodes_by_id) + (1 if ca_cids else 0)
        AMS = sum(makespans) / max(denom, 1)

    fail_rate = ca_num / max(total, 1)
    ca_penalty = args.ca_penalty * fail_rate
    objective_without_ca = args.alpha_obj * ACT + (1.0 - args.alpha_obj) * AMS
    objective = objective_without_ca + ca_penalty

    reuse_rate = total_reused / max(total_reused + total_downloaded, 1e-9)

    summary = {
        "algo": f"{args.algo}-online-k8sca",
        "num_containers": total,
        "num_edge_containers": edge_num,
        "num_ca_containers": ca_num,
        "failed_deployments": ca_num,
        "fail_rate": fail_rate,
        "num_nodes": len(nodes_by_id),
        "num_eval_nodes": len(nodes_by_id) + (1 if ca_cids else 0),
        "resource_model": "online_cumulative_k8s_filter_no_release",
        "ca_model": "parallel_virtual_guarantee" if not args.ca_serial else "serial_virtual_guarantee",
        "ca_bandwidth_mb_s": ca_bw,
        "ca_extra_delay": args.ca_extra_delay,
        "ca_penalty": ca_penalty,
        "ca_fixed_penalty": args.ca_penalty,
        "ACT": ACT,
        "AMS": AMS,
        "AMS_mode": args.ams_mode,
        "downloaded_mb": total_downloaded,
        "reused_mb": total_reused,
        "reuse_rate": reuse_rate,
        "objective_without_ca_penalty": objective_without_ca,
        "objective": objective,
    }

    out = {
        "assignment": {**assignment, CA_EID: list(ca_cids)},
        "ordered_queues": ordered_queues,
        "summary": summary,
        "node_details": node_details,
        "container_metrics": container_metrics,
        "config": vars(args),
    }
    return out


# ============================================================
# Main
# ============================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--algo", required=True, choices=["fg", "ilrsa", "lrscheduler", "gahrl", "orr", "lasa"])

    ap.add_argument("--alpha-obj", type=float, default=0.5)
    ap.add_argument("--ams-mode", choices=["avg", "max"], default="avg")

    # CA model
    ap.add_argument("--ca-penalty", type=float, default=1000.0)
    ap.add_argument("--ca-bandwidth", type=float, default=-1.0, help="<=0 means average edge bandwidth.")
    ap.add_argument("--ca-extra-delay", type=float, default=0.0)
    ap.add_argument("--ca-serial", action="store_true", help="Use one serial CA queue instead of parallel guarantee.")

    # Generic queue order for simple baselines
    ap.add_argument("--queue-order", choices=["arrival", "cache"], default="cache")

    # Original module paths
    ap.add_argument("--fg-script", default="scripts/fg_dscr.py")
    ap.add_argument("--ilrsa-script", default="scripts/ilrsa_reference_impl.py")

    # FG params
    ap.add_argument("--beam", type=int, default=4)
    ap.add_argument("--unit-mb", type=int, default=50)
    ap.add_argument("--lambda-cong", type=float, default=1.0)
    ap.add_argument("--bw-gamma", type=float, default=1.0)
    ap.add_argument("--lambda-frag", type=float, default=0.1)
    ap.add_argument("--lambda-aff", type=float, default=0.2)
    ap.add_argument("--lambda-task-load", type=float, default=0.03)
    ap.add_argument("--theta-cong-count", type=float, default=0.0)
    ap.add_argument("--greedy-load-factor", type=float, default=0.0)
    ap.add_argument("--lambda-cache-core", type=float, default=0.0)
    ap.add_argument("--cache-core-ratio", type=float, default=0.90)
    ap.add_argument("--task-load-power", type=float, default=2.0)
    ap.add_argument("--task-load-factor", type=float, default=1.8)
    ap.add_argument("--k-pin", type=int, default=6)
    ap.add_argument("--cache-policy", choices=["pgdsf", "lru", "lfu"], default="pgdsf")
    ap.add_argument("--cache-bw-eta", type=float, default=0.0)
    ap.add_argument("--cache-bw-ref", type=float, default=100.0)
    ap.add_argument("--fg-order-policy", choices=["dynamic_state", "static_ilrsa", "arrival"], default="dynamic_state")
    ap.add_argument("--disable-future-share", action="store_true")

    # Extra online K8s resource-packing score for FG policy.
    # Defaults keep the original potential-only behavior.
    ap.add_argument("--fg-k8s-phi-weight", type=float, default=1.0)
    ap.add_argument("--fg-k8s-pack-weight", type=float, default=0.0)
    ap.add_argument("--fg-k8s-reuse-weight", type=float, default=0.0)
    ap.add_argument("--fg-k8s-load-weight", type=float, default=0.0)
    ap.add_argument("--fg-k8s-shape-weight", type=float, default=0.5)
    ap.add_argument("--fg-k8s-tiny-residual", type=float, default=0.0)
    ap.add_argument("--fg-k8s-tiny-weight", type=float, default=0.0)

    # ILRSA params
    ap.add_argument("--ilrsa-alpha", type=float, default=0.5)
    ap.add_argument("--ilrsa-exact-threshold", type=int, default=12)
    ap.add_argument("--ilrsa-knapsack", choices=["exact", "greedy"], default="exact")
    ap.add_argument("--ilrsa-knapsack-unit-mb", type=int, default=50)
    ap.add_argument("--seed", type=int, default=42)

    # LRScheduler params
    ap.add_argument("--lrs-layer-mb-threshold", type=float, default=10.0)
    ap.add_argument("--lrs-std-threshold", type=float, default=0.16)
    ap.add_argument("--lrs-cpu-threshold", type=float, default=0.6)
    ap.add_argument("--lrs-high-weight", type=float, default=2.0)
    ap.add_argument("--lrs-low-weight", type=float, default=0.5)
    ap.add_argument("--lrs-plugin-weight-layer", type=float, default=2.0)
    ap.add_argument("--lrs-plugin-weight-resource", type=float, default=1.0)

    # GAHRL params
    ap.add_argument("--gahrl-lambda-latency", type=float, default=0.5)
    ap.add_argument("--gahrl-w-resource", type=float, default=0.30)
    ap.add_argument("--gahrl-w-imbalance", type=float, default=0.20)
    ap.add_argument("--gahrl-w-cache-pressure", type=float, default=0.20)
    ap.add_argument("--gahrl-w-layer-hit", type=float, default=0.20)

    # ORR params
    ap.add_argument("--orr-w-resource", type=float, default=0.30)
    ap.add_argument("--orr-w-queue-len", type=float, default=0.05)
    ap.add_argument("--orr-w-reuse", type=float, default=0.20)

    # LASA params
    ap.add_argument("--lasa-alpha", type=float, default=0.5)

    args = ap.parse_args()

    case = load_json(args.case)
    ctx = OriginalContexts(args, case)

    assignment, ca_cids, used, c_by_id, nodes_by_id, layer_sizes, layer_pop = online_assign(case, args, ctx)
    res = final_simulate(case, args, ctx, assignment, ca_cids, c_by_id, nodes_by_id, layer_sizes, layer_pop)

    save_json(res, args.out)
    print(json.dumps(res["summary"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
