#!/usr/bin/env python3
import argparse
import json
import math
import os
import statistics
from collections import defaultdict, Counter

CA_EID = "__CA_GUARANTEE__"

def node_id(n, i=0):
    return n.get("eid") or n.get("id") or n.get("nid") or n.get("name") or n.get("node_id") or f"edge-{i+1}"

def cid_of(c, i=0):
    return c.get("cid") or c.get("id") or c.get("name") or f"c{i:04d}"

def c_res(c, q):
    return float((c.get("resources", {}) or {}).get(q, 0.0))

def n_res(n, q):
    return float((n.get("resources", {}) or {}).get(q, 0.0))

def c_layers(c):
    return list(c.get("layers") or c.get("image_layers") or c.get("layer_ids") or [])

def run_time(c):
    return float(c.get("run_time", c.get("duration", 0.0)))

def bandwidth(n):
    return float(n.get("bandwidth_mb_s", n.get("bandwidth", 1.0)) or 1.0)

def repo_cap(n):
    return float(n.get("repo_capacity_mb", n.get("cache_capacity_mb", n.get("cache_mb", 1024))) or 0.0)

def load_layer_sizes(case):
    for k in ["layer_sizes_mb", "layer_sizes", "layers_size_mb"]:
        v = case.get(k)
        if isinstance(v, dict):
            return {str(a): float(b) for a, b in v.items()}

    # Some cases store layers as list of dicts.
    out = {}
    for x in case.get("layers", []):
        if isinstance(x, dict):
            lid = str(x.get("lid") or x.get("id") or x.get("name"))
            out[lid] = float(x.get("size_mb", x.get("size", 1.0)))
    return out

def layer_size(layer_sizes, l):
    return float(layer_sizes.get(str(l), 1.0))

def size_of_layers(layers, layer_sizes):
    return sum(layer_size(layer_sizes, l) for l in layers)

def image_size(c, layer_sizes):
    return size_of_layers(c_layers(c), layer_sizes)

def reused_missing_mb(c, cache, layer_sizes):
    layers = set(c_layers(c))
    reused_layers = layers & cache
    missing_layers = layers - cache
    return size_of_layers(reused_layers, layer_sizes), size_of_layers(missing_layers, layer_sizes)

def k8s_feasible(c, n, used):
    for q in ["cpu", "mem", "disk"]:
        req = c_res(c, q)
        cap = n_res(n, q)
        cur = float(used.get(q, 0.0))
        if cur + req > cap + 1e-9:
            return False
    return True

def add_to_cache(cache, add_layers, cap, layer_sizes, layer_pop):
    cache = set(cache)
    for l in add_layers:
        cache.add(l)

    if cap <= 0:
        return set()

    def total():
        return size_of_layers(cache, layer_sizes)

    while total() > cap + 1e-9 and cache:
        # Evict low-popularity large layers first.
        victim = min(
            cache,
            key=lambda x: (
                layer_pop.get(x, 0) / max(layer_size(layer_sizes, x), 1e-9),
                layer_pop.get(x, 0),
                -layer_size(layer_sizes, x),
                str(x),
            ),
        )
        cache.remove(victim)
    return cache

def greedy_cache_order(cids, c_by_id, initial_cache, layer_sizes):
    remaining = list(cids)
    cache = set(initial_cache)
    ordered = []

    while remaining:
        best_idx = 0
        best_key = None
        for idx, cid in enumerate(remaining):
            c = c_by_id[cid]
            reused, missing = reused_missing_mb(c, cache, layer_sizes)
            key = (-missing, reused, -run_time(c), str(cid))
            if best_key is None or key > best_key:
                best_key = key
                best_idx = idx

        cid = remaining.pop(best_idx)
        ordered.append(cid)
        cache |= set(c_layers(c_by_id[cid]))

    return ordered

def resource_balance_score(c, n, used):
    vals = []
    for q in ["cpu", "mem", "disk"]:
        cap = max(n_res(n, q), 1e-9)
        vals.append((float(used.get(q, 0.0)) + c_res(c, q)) / cap)
    std = statistics.pstdev(vals) if len(vals) > 1 else 0.0
    pressure = max(vals)
    return max(0.0, 1.0 - 2.0 * std), pressure, std

def choose_node(algo, c, feasible, nodes_by_id, caches, used, queue_finish, queue_len, layer_sizes, layer_pop, args):
    best = None

    for nid in feasible:
        n = nodes_by_id[nid]
        cache = caches[nid]
        reused, missing = reused_missing_mb(c, cache, layer_sizes)
        total = max(image_size(c, layer_sizes), 1e-9)
        bw = max(bandwidth(n), 1e-9)
        bal_score, pressure, std = resource_balance_score(c, n, used[nid])

        if algo == "lrscheduler":
            layer_score = reused / total
            max_finish = max(max(queue_finish.values()), 1e-9)
            load_score = 1.0 - queue_finish[nid] / max_finish
            load_score = max(0.0, min(1.0, load_score))
            score = args.w_layer * layer_score + args.w_resource * bal_score + args.w_load * load_score
            key = (score, -queue_finish[nid], -queue_len[nid], nid)

        elif algo == "gahrl":
            download_cost = missing / bw
            load_cost = queue_finish[nid]
            cache_pressure = max(0.0, (size_of_layers(cache, layer_sizes) + missing - repo_cap(n)) / max(repo_cap(n), 1e-9))
            cost = (
                args.w_download * download_cost
                + args.w_load * load_cost
                + args.w_resource * pressure
                + args.w_cache * cache_pressure
            )
            key = (-cost, -queue_finish[nid], -queue_len[nid], nid)

        elif algo == "orr":
            # Immediate orchestration cost: queue delay + pull delay + mild resource pressure.
            cost = queue_finish[nid] + missing / bw + run_time(c) + args.w_resource * pressure + 0.05 * queue_len[nid]
            key = (-cost, -queue_finish[nid], -queue_len[nid], nid)

        elif algo == "ilrsa":
            # One-step delay-aware placement close to ILR-style phase-1 objective.
            est_finish = queue_finish[nid] + missing / bw + run_time(c)
            cost = args.alpha * est_finish + (1.0 - args.alpha) * max(queue_finish[nid], est_finish)
            key = (-cost, reused, -missing, -queue_len[nid], nid)

        elif algo == "lasa":
            # LCAA-style: smaller incremental layer pulling with layer-locality term.
            score = ((1.0 - args.alpha) * missing + args.alpha * reused) / bw
            key = (-score, -missing, reused, -queue_len[nid], nid)

        else:
            raise ValueError(algo)

        if best is None or key > best[0]:
            best = (key, nid)

    return None if best is None else best[1]

def assign(case, args):
    containers = case.get("containers", [])
    nodes = case.get("nodes", [])
    layer_sizes = load_layer_sizes(case)

    c_by_id = {cid_of(c, i): c for i, c in enumerate(containers)}
    nodes_by_id = {node_id(n, i): n for i, n in enumerate(nodes)}

    layer_pop = Counter()
    for c in containers:
        for l in c_layers(c):
            layer_pop[l] += 1

    caches = {
        nid: set(n.get("initial_cache", []) or [])
        for nid, n in nodes_by_id.items()
    }
    used = {
        nid: defaultdict(float)
        for nid in nodes_by_id
    }
    queue_finish = {nid: 0.0 for nid in nodes_by_id}
    queue_len = {nid: 0 for nid in nodes_by_id}

    assignment = {nid: [] for nid in nodes_by_id}
    assignment[CA_EID] = []

    for i, c in enumerate(containers):
        cid = cid_of(c, i)

        feasible = [
            nid for nid, n in nodes_by_id.items()
            if k8s_feasible(c, n, used[nid])
        ]

        if not feasible:
            assignment[CA_EID].append(cid)
            continue

        best = choose_node(
            args.algo, c, feasible, nodes_by_id,
            caches, used, queue_finish, queue_len,
            layer_sizes, layer_pop, args
        )

        if best is None:
            assignment[CA_EID].append(cid)
            continue

        assignment[best].append(cid)

        for q in ["cpu", "mem", "disk"]:
            used[best][q] += c_res(c, q)

        reused, missing = reused_missing_mb(c, caches[best], layer_sizes)
        queue_finish[best] += missing / max(bandwidth(nodes_by_id[best]), 1e-9) + run_time(c)
        queue_len[best] += 1

        miss_layers = set(c_layers(c)) - caches[best]
        caches[best] = add_to_cache(caches[best], miss_layers, repo_cap(nodes_by_id[best]), layer_sizes, layer_pop)

    return assignment, c_by_id, nodes_by_id, layer_sizes, layer_pop

def simulate(case, assignment, c_by_id, nodes_by_id, layer_sizes, layer_pop, args):
    node_details = {}
    ordered_queues = {}
    node_step_logs = []

    total_completion_sum = 0.0
    total_makespan = 0.0
    total_scheduled = 0
    total_downloaded = 0.0
    total_reused = 0.0

    for nid, cids in assignment.items():
        if nid == CA_EID:
            continue

        n = nodes_by_id[nid]
        init_cache = set(n.get("initial_cache", []) or [])

        if args.order_policy == "cache_greedy":
            ordered = greedy_cache_order(cids, c_by_id, init_cache, layer_sizes)
        else:
            ordered = list(cids)

        ordered_queues[nid] = ordered

        cache = set(init_cache)
        t = 0.0
        node_downloaded = 0.0
        node_reused = 0.0
        steps = []

        for cid in ordered:
            c = c_by_id[cid]
            reused, missing = reused_missing_mb(c, cache, layer_sizes)
            pull_time = missing / max(bandwidth(n), 1e-9)
            start = t
            finish = start + pull_time + run_time(c)

            node_downloaded += missing
            node_reused += reused
            t = finish

            miss_layers = set(c_layers(c)) - cache
            cache = add_to_cache(cache, miss_layers, repo_cap(n), layer_sizes, layer_pop)

            steps.append({
                "cid": cid,
                "node": nid,
                "start_time": start,
                "finish_time": finish,
                "downloaded_mb": missing,
                "reused_mb": reused,
            })

        total_completion_sum += sum(x["finish_time"] for x in steps)
        total_makespan += t
        total_scheduled += len(ordered)
        total_downloaded += node_downloaded
        total_reused += node_reused
        node_step_logs.extend(steps)

        node_details[nid] = {
            "num_containers": len(ordered),
            "finish_time": t,
            "downloaded_mb": node_downloaded,
            "reused_mb": node_reused,
        }

    failed = len(assignment.get(CA_EID, []))
    total_requests = len(case.get("containers", []))

    ACT = total_completion_sum / max(total_scheduled, 1)
    AMS = total_makespan / max(len(nodes_by_id), 1)
    objective_without_ca = args.alpha_obj * ACT + (1.0 - args.alpha_obj) * AMS

    ca_penalty = args.ca_penalty * failed / max(total_requests, 1)
    objective = objective_without_ca + ca_penalty

    return {
        "assignment": assignment,
        "ordered_queues": ordered_queues,
        "node_details": node_details,
        "node_step_logs": node_step_logs,
        "summary": {
            "algo": f"{args.algo}-K8sCA",
            "num_containers": total_requests,
            "num_scheduled_containers": total_scheduled,
            "failed_deployments": failed,
            "ca_penalty": ca_penalty,
            "ca_fixed_penalty": args.ca_penalty,
            "num_nodes": len(nodes_by_id),
            "ACT": ACT,
            "AMS": AMS,
            "downloaded_mb": total_downloaded,
            "reused_mb": total_reused,
            "reuse_rate": total_reused / max(total_reused + total_downloaded, 1e-9),
            "objective_without_ca_penalty": objective_without_ca,
            "objective": objective,
        },
        "config": vars(args),
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--algo", required=True, choices=["ilrsa", "lrscheduler", "gahrl", "orr", "lasa"])

    ap.add_argument("--alpha-obj", type=float, default=0.5)
    ap.add_argument("--ca-penalty", type=float, default=1000.0)
    ap.add_argument("--order-policy", choices=["arrival", "cache_greedy"], default="cache_greedy")

    # Shared score weights. Different algos use relevant subsets.
    ap.add_argument("--alpha", type=float, default=0.5)
    ap.add_argument("--w-layer", type=float, default=1.0)
    ap.add_argument("--w-resource", type=float, default=0.3)
    ap.add_argument("--w-load", type=float, default=0.3)
    ap.add_argument("--w-download", type=float, default=1.0)
    ap.add_argument("--w-cache", type=float, default=0.1)

    args = ap.parse_args()

    with open(args.case, "r", encoding="utf-8") as f:
        case = json.load(f)

    assignment, c_by_id, nodes_by_id, layer_sizes, layer_pop = assign(case, args)
    res = simulate(case, assignment, c_by_id, nodes_by_id, layer_sizes, layer_pop, args)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2, ensure_ascii=False)

    print(json.dumps(res["summary"], indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
