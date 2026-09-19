import argparse
import json
import os
import math
from collections import defaultdict, Counter


def load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(obj, p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def node_id(n):
    return n.get("eid") or n.get("id") or n.get("nid") or n.get("name") or n.get("node_id")


def layer_mb(layer_sizes, l):
    v = layer_sizes.get(l, 0)
    try:
        v = float(v)
    except Exception:
        v = 0.0
    if v <= 0:
        v = 1.0
    return v


def container_layers(c):
    return list(c.get("layers", []))


def container_layer_set(c):
    return set(container_layers(c))


def container_size_mb(c, layer_sizes):
    return sum(layer_mb(layer_sizes, l) for l in container_layers(c))


def can_run_on_node(c, n):
    cres = c.get("resources", {}) or {}
    nres = n.get("resources", {}) or {}
    for k, v in cres.items():
        if float(v) > float(nres.get(k, 0)):
            return False
    return True

def c_res(c, key):
    return float((c.get("resources", {}) or {}).get(key, 0.0))

def n_res(n, key):
    return float((n.get("resources", {}) or {}).get(key, 0.0))

def can_fit_cumulative(c, n, used_n):
    for k in ["cpu", "mem", "disk"]:
        if used_n.get(k, 0.0) + c_res(c, k) > n_res(n, k) + 1e-9:
            return False
    return True

def add_used(used_n, c):
    for k in ["cpu", "mem", "disk"]:
        used_n[k] = used_n.get(k, 0.0) + c_res(c, k)


def cache_size(cache, layer_sizes):
    return sum(layer_mb(layer_sizes, l) for l in cache)


def evict_by_popularity(cache, protected, cap, layer_sizes, layer_pop):
    """
    BREAK-inspired eviction:
    keep current container layers if possible;
    evict low-popularity / large layers first.
    """
    protected = set(protected)
    if cache_size(cache, layer_sizes) <= cap:
        return cache

    removable = [l for l in cache if l not in protected]

    def evict_key(l):
        pop = layer_pop.get(l, 0)
        size = layer_mb(layer_sizes, l)
        # low pop first; if equal, large layer first
        return (pop, -size)

    removable.sort(key=evict_key)

    for l in removable:
        if cache_size(cache, layer_sizes) <= cap:
            break
        cache.remove(l)

    # If still overflow, evict protected layers only as last resort.
    if cache_size(cache, layer_sizes) > cap:
        rest = list(cache)
        rest.sort(key=evict_key)
        for l in rest:
            if cache_size(cache, layer_sizes) <= cap:
                break
            cache.remove(l)

    return cache


def add_layers_to_cache(cache, layers, cap, layer_sizes, layer_pop):
    for l in layers:
        cache.add(l)
    return evict_by_popularity(cache, protected=layers, cap=cap, layer_sizes=layer_sizes, layer_pop=layer_pop)


def missing_layers(c, cache):
    return [l for l in container_layers(c) if l not in cache]


def reused_missing_mb(c, cache, layer_sizes):
    reused = 0.0
    missing = 0.0
    for l in container_layers(c):
        if l in cache:
            reused += layer_mb(layer_sizes, l)
        else:
            missing += layer_mb(layer_sizes, l)
    return reused, missing


def build_layer_popularity(containers):
    pop = Counter()
    for c in containers:
        for l in set(container_layers(c)):
            pop[l] += 1
    return pop


def init_state(case, args):
    layer_sizes = case.get("layer_sizes_mb", {})
    containers = case.get("containers", [])
    nodes = case.get("nodes", [])

    c_by_id = {c["cid"]: c for c in containers}
    n_by_id = {node_id(n): n for n in nodes}
    layer_pop = build_layer_popularity(containers)

    caches = {}
    for n in nodes:
        nid = node_id(n)
        caches[nid] = set(n.get("initial_cache", []) or [])

    assignment = {node_id(n): [] for n in nodes}
    queue_finish = {node_id(n): 0.0 for n in nodes}
    queue_len = {node_id(n): 0 for n in nodes}

    return layer_sizes, containers, nodes, c_by_id, n_by_id, layer_pop, caches, assignment, queue_finish, queue_len


def lrscheduler_assign(case, args):
    """
    LRScheduler-inspired baseline.

    Core idea:
    Score(n,c) = omega(n,c) * LayerScore(n,c) + (1 - omega(n,c)) * LoadScore(n)

    LayerScore: fraction of requested layer bytes already cached on node.
    LoadScore: prefer less loaded nodes.
    Dynamic omega:
        if node is lightly loaded and has useful layer locality, use omega1;
        otherwise use omega2.
    """
    layer_sizes, containers, nodes, c_by_id, n_by_id, layer_pop, caches, assignment, queue_finish, queue_len = init_state(case, args)

    total_prefetch_mb = 0.0
    used = {node_id(n): {"cpu": 0.0, "mem": 0.0, "disk": 0.0} for n in nodes}
    ca_triggered = []

    for c in containers:
        best = None
        best_score = -1e18

        c_total = max(container_size_mb(c, layer_sizes), 1e-9)

        max_finish = max(queue_finish.values()) if queue_finish else 1.0
        if max_finish <= 0:
            max_finish = 1.0

        for n in nodes:
            nid = node_id(n)
            if (not can_run_on_node(c, n)) or (not can_fit_cumulative(c, n, used[nid])):
                continue

            reused, missing = reused_missing_mb(c, caches[nid], layer_sizes)
            layer_score = reused / c_total

            # Load score: smaller current queue finish is better.
            load_score = 1.0 - (queue_finish[nid] / max_finish)
            load_score = max(0.0, min(1.0, load_score))

            # Dynamic weight inspired by LRScheduler.
            # If the node is not heavily loaded and has non-trivial layer reuse,
            # emphasize layer sharing; otherwise emphasize load balance.
            avg_finish = sum(queue_finish.values()) / max(len(queue_finish), 1)
            lightly_loaded = queue_finish[nid] <= max(avg_finish * args.lr_load_threshold, 1e-9)
            has_layer_gain = layer_score >= args.lr_layer_threshold

            omega = args.lr_omega1 if (lightly_loaded and has_layer_gain) else args.lr_omega2

            score = omega * layer_score + (1.0 - omega) * load_score

            if score > best_score:
                best_score = score
                best = nid

        if best is None:
            ca_triggered.append(c["cid"])
            continue

        assignment[best].append(c["cid"])
        add_used(used[best], c)

        # Online cache state update after this deployment.
        n = n_by_id[best]
        cap = float(n.get("repo_capacity_mb", 1024))
        miss = missing_layers(c, caches[best])
        caches[best] = add_layers_to_cache(caches[best], miss, cap, layer_sizes, layer_pop)

        bw = float(n.get("bandwidth_mb_s", 1.0))
        _, miss_mb = reused_missing_mb(c, caches[best] - set(miss), layer_sizes)
        queue_finish[best] += miss_mb / max(bw, 1e-9) + float(c.get("run_time", 0.0))
        queue_len[best] += 1

    return assignment, total_prefetch_mb, ca_triggered


def break_prefetch(case, nodes, caches, layer_sizes, layer_pop, args):
    """
    BREAK-inspired proactive layer-level cache prefetching.

    This is not full BREAK. It only captures:
    - hot layer prefetching
    - layer-level cache
    - popularity-aware cache replacement

    The prefetch cost is recorded separately as prefetch_mb.
    If --count-prefetch-in-downloaded is set, it is also added to downloaded_mb in summary.
    """
    total_prefetch_mb = 0.0
    if args.break_prefetch_ratio <= 0:
        return caches, total_prefetch_mb

    # Popularity density: frequent and large layers are useful, but overly huge layers are penalized.
    layers = list(layer_pop.keys())

    def hot_key(l):
        pop = layer_pop.get(l, 0)
        size = layer_mb(layer_sizes, l)
        return (pop, pop / max(size, 1e-9))

    layers.sort(key=hot_key, reverse=True)

    for n in nodes:
        nid = node_id(n)
        cap = float(n.get("repo_capacity_mb", 1024))
        budget = cap * args.break_prefetch_ratio
        used = cache_size(caches[nid], layer_sizes)

        for l in layers:
            s = layer_mb(layer_sizes, l)
            if l in caches[nid]:
                continue
            if used + s > budget:
                continue
            caches[nid].add(l)
            used += s
            total_prefetch_mb += s

    return caches, total_prefetch_mb


def break_assign(case, args):
    """
    BREAK-inspired baseline.

    Core idea:
    1. Prefetch globally hot layers into node-level layer caches.
    2. Schedule each container to node with low estimated deployment cost:
       missing download time + queue finish time - cache benefit.
    3. Use popularity-aware cache replacement.
    """
    layer_sizes, containers, nodes, c_by_id, n_by_id, layer_pop, caches, assignment, queue_finish, queue_len = init_state(case, args)

    caches, total_prefetch_mb = break_prefetch(case, nodes, caches, layer_sizes, layer_pop, args)
    used = {node_id(n): {"cpu": 0.0, "mem": 0.0, "disk": 0.0} for n in nodes}
    ca_triggered = []

    for c in containers:
        best = None
        best_cost = 1e18

        for n in nodes:
            nid = node_id(n)
            if (not can_run_on_node(c, n)) or (not can_fit_cumulative(c, n, used[nid])):
                continue

            bw = float(n.get("bandwidth_mb_s", 1.0))
            reused, missing = reused_missing_mb(c, caches[nid], layer_sizes)

            download_time = missing / max(bw, 1e-9)
            total_mb = max(reused + missing, 1e-9)
            cache_hit = reused / total_mb

            # BREAK-style: prefer local cached layers and lower queue finish.
            cost = (
                args.break_w_finish * queue_finish[nid]
                + args.break_w_download * download_time
                - args.break_w_cachehit * cache_hit
            )

            if cost < best_cost:
                best_cost = cost
                best = nid

        if best is None:
            ca_triggered.append(c["cid"])
            continue

        assignment[best].append(c["cid"])
        add_used(used[best], c)

        n = n_by_id[best]
        cap = float(n.get("repo_capacity_mb", 1024))
        bw = float(n.get("bandwidth_mb_s", 1.0))

        reused, missing = reused_missing_mb(c, caches[best], layer_sizes)
        miss = missing_layers(c, caches[best])
        caches[best] = add_layers_to_cache(caches[best], miss, cap, layer_sizes, layer_pop)

        queue_finish[best] += missing / max(bw, 1e-9) + float(c.get("run_time", 0.0))
        queue_len[best] += 1

    return assignment, total_prefetch_mb, ca_triggered


def orr_assign(case, args):
    """
    ORR-inspired / LGreedy baseline.

    This is not the full ORR with convex relaxation and rounding.
    It keeps the key online idea:
    choose feasible node by minimizing immediate orchestration cost.

    Simplified cost components:
    - estimated finish time
    - missing layer download time
    - storage/cache pressure
    - load penalty
    - regularization term from previous cache state
    """
    layer_sizes, containers, nodes, c_by_id, n_by_id, layer_pop, caches, assignment, queue_finish, queue_len = init_state(case, args)

    total_prefetch_mb = 0.0
    used = {node_id(n): {"cpu": 0.0, "mem": 0.0, "disk": 0.0} for n in nodes}
    ca_triggered = []

    for c in containers:
        best = None
        best_cost = 1e18

        avg_queue_len = sum(queue_len.values()) / max(len(queue_len), 1)
        if avg_queue_len <= 0:
            avg_queue_len = 1.0

        for n in nodes:
            nid = node_id(n)
            if (not can_run_on_node(c, n)) or (not can_fit_cumulative(c, n, used[nid])):
                continue

            cap = float(n.get("repo_capacity_mb", 1024))
            bw = float(n.get("bandwidth_mb_s", 1.0))

            reused, missing = reused_missing_mb(c, caches[nid], layer_sizes)
            download_time = missing / max(bw, 1e-9)
            run_time = float(c.get("run_time", 0.0))
            estimated_finish = queue_finish[nid] + download_time + run_time

            current_cache_mb = cache_size(caches[nid], layer_sizes)
            new_layers = set(missing_layers(c, caches[nid]))
            new_cache_mb = current_cache_mb + sum(layer_mb(layer_sizes, l) for l in new_layers)
            storage_pressure = max(0.0, new_cache_mb - cap) / max(cap, 1e-9)

            load_penalty = queue_len[nid] / avg_queue_len

            # Regularization: avoid large cache-state changes.
            regularization = len(new_layers) / max(len(container_layers(c)), 1)

            cost = (
                args.orr_w_finish * estimated_finish
                + args.orr_w_download * download_time
                + args.orr_w_storage * storage_pressure
                + args.orr_w_load * load_penalty
                + args.orr_w_reg * regularization
            )

            if cost < best_cost:
                best_cost = cost
                best = nid

        if best is None:
            ca_triggered.append(c["cid"])
            continue

        assignment[best].append(c["cid"])
        add_used(used[best], c)

        n = n_by_id[best]
        cap = float(n.get("repo_capacity_mb", 1024))
        bw = float(n.get("bandwidth_mb_s", 1.0))

        reused, missing = reused_missing_mb(c, caches[best], layer_sizes)
        miss = missing_layers(c, caches[best])
        caches[best] = add_layers_to_cache(caches[best], miss, cap, layer_sizes, layer_pop)

        queue_finish[best] += missing / max(bw, 1e-9) + float(c.get("run_time", 0.0))
        queue_len[best] += 1

    return assignment, total_prefetch_mb, ca_triggered


def greedy_cache_order(cids, c_by_id, node, initial_cache, layer_sizes, layer_pop):
    """
    Generic cache-aware order used by baselines after placement.
    This makes baselines stronger than pure arrival order.
    """
    remaining = list(cids)
    order = []
    cache = set(initial_cache)
    cap = float(node.get("repo_capacity_mb", 1024))

    while remaining:
        best_idx = 0
        best_key = None

        for idx, cid in enumerate(remaining):
            c = c_by_id[cid]
            reused, missing = reused_missing_mb(c, cache, layer_sizes)
            total = max(reused + missing, 1e-9)
            hit = reused / total
            # Prefer high hit, then low missing.
            key = (hit, -missing)
            if best_key is None or key > best_key:
                best_key = key
                best_idx = idx

        cid = remaining.pop(best_idx)
        order.append(cid)

        c = c_by_id[cid]
        miss = missing_layers(c, cache)
        cache = add_layers_to_cache(cache, miss, cap, layer_sizes, layer_pop)

    return order


def simulate(case, assignment, args, total_prefetch_mb=0.0, ca_triggered=None):
    layer_sizes = case.get("layer_sizes_mb", {})
    containers = case.get("containers", [])
    nodes = case.get("nodes", [])
    c_by_id = {c["cid"]: c for c in containers}
    n_by_id = {node_id(n): n for n in nodes}
    layer_pop = build_layer_popularity(containers)

    ordered_queues = {}
    node_details = {}
    container_metrics = {}

    all_completion = []
    total_downloaded = 0.0
    total_reused = 0.0

    if args.count_prefetch_in_downloaded:
        total_downloaded += total_prefetch_mb

    for nid, cids in assignment.items():
        node = n_by_id[nid]
        init_cache = set(node.get("initial_cache", []) or [])

        if args.algo == "break":
            # Rebuild BREAK prefetch cache for simulation.
            caches_tmp = {nid: set(init_cache)}
            caches_tmp, _ = break_prefetch(case, [node], caches_tmp, layer_sizes, layer_pop, args)
            init_cache = caches_tmp[nid]

        if args.order == "cache":
            q = greedy_cache_order(cids, c_by_id, node, init_cache, layer_sizes, layer_pop)
        else:
            q = list(cids)

        ordered_queues[nid] = q

        cache = set(init_cache)
        cap = float(node.get("repo_capacity_mb", 1024))
        bw = float(node.get("bandwidth_mb_s", 1.0))

        t = 0.0
        node_downloaded = 0.0
        node_reused = 0.0

        for cid in q:
            c = c_by_id[cid]
            reused, missing = reused_missing_mb(c, cache, layer_sizes)
            download_time = missing / max(bw, 1e-9)
            run_time = float(c.get("run_time", 0.0))

            start = t
            finish = t + download_time + run_time
            t = finish

            total_downloaded += missing
            total_reused += reused
            node_downloaded += missing
            node_reused += reused
            all_completion.append(finish)

            container_metrics[cid] = {
                "node": nid,
                "start_time": start,
                "finish_time": finish,
                "downloaded_mb": missing,
                "reused_mb": reused,
                "download_time": download_time,
                "run_time": run_time,
            }

            miss = missing_layers(c, cache)
            cache = add_layers_to_cache(cache, miss, cap, layer_sizes, layer_pop)

        node_details[nid] = {
            "num_containers": len(q),
            "finish_time": t,
            "downloaded_mb": node_downloaded,
            "reused_mb": node_reused,
            "final_cache_mb": cache_size(cache, layer_sizes),
            "final_cache_layers": len(cache),
        }

    n = len(containers)
    ACT = sum(all_completion) / max(len(all_completion), 1)
    AMS = max(all_completion) if all_completion else 0.0

    objective_base = args.alpha_obj * ACT + (1.0 - args.alpha_obj) * AMS
    ca_triggered = list(ca_triggered or [])
    ca_count = len(ca_triggered)
    ca_rate = ca_count / max(n, 1)
    ca_penalty = args.lambda_ca * ca_rate
    objective = objective_base + ca_penalty
    denom = total_downloaded + total_reused
    reuse_rate = total_reused / denom if denom > 0 else 0.0

    summary = {
        "algo": args.algo_name,
        "num_containers": n,
        "num_scheduled_containers": n - ca_count,
        "num_assigned": n - ca_count,
        "ca_triggered": ca_count,
        "ca_rate": ca_rate,
        "ca_penalty": ca_penalty,
        "lambda_ca": args.lambda_ca,
        "num_nodes": len(nodes),
        "ACT": ACT,
        "AMS": AMS,
        "downloaded_mb": int(round(total_downloaded)),
        "reused_mb": int(round(total_reused)),
        "reuse_rate": reuse_rate,
        "objective_without_ca_penalty": objective_base,
        "objective_ca": objective,
        "objective": objective,
        "prefetch_mb": int(round(total_prefetch_mb)),
        "count_prefetch_in_downloaded": bool(args.count_prefetch_in_downloaded),
    }

    return {
        "assignment": assignment,
        "ordered_queues": ordered_queues,
        "summary": summary,
        "ca_triggered_containers": ca_triggered,
        "node_details": node_details,
        "container_metrics": container_metrics,
    }


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument("--case", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--algo", required=True, choices=["lrscheduler", "break", "orr"])
    ap.add_argument("--order", default="cache", choices=["arrival", "cache"])
    ap.add_argument("--alpha-obj", type=float, default=0.5)
    ap.add_argument("--lambda-ca", type=float, default=1000.0)

    # LRScheduler-inspired parameters
    ap.add_argument("--lr-omega1", type=float, default=0.80)
    ap.add_argument("--lr-omega2", type=float, default=0.25)
    ap.add_argument("--lr-load-threshold", type=float, default=1.20)
    ap.add_argument("--lr-layer-threshold", type=float, default=0.05)

    # BREAK-inspired parameters
    ap.add_argument("--break-prefetch-ratio", type=float, default=0.25)
    ap.add_argument("--break-w-finish", type=float, default=0.30)
    ap.add_argument("--break-w-download", type=float, default=1.00)
    ap.add_argument("--break-w-cachehit", type=float, default=5.00)
    ap.add_argument("--count-prefetch-in-downloaded", action="store_true")

    # ORR-inspired parameters
    ap.add_argument("--orr-w-finish", type=float, default=1.00)
    ap.add_argument("--orr-w-download", type=float, default=2.00)
    ap.add_argument("--orr-w-storage", type=float, default=30.00)
    ap.add_argument("--orr-w-load", type=float, default=3.00)
    ap.add_argument("--orr-w-reg", type=float, default=1.00)

    args = ap.parse_args()

    case = load_json(args.case)

    if args.algo == "lrscheduler":
        args.algo_name = "LRScheduler-inspired"
        assignment, prefetch_mb, ca_triggered = lrscheduler_assign(case, args)
    elif args.algo == "break":
        args.algo_name = "BREAK-inspired"
        assignment, prefetch_mb, ca_triggered = break_assign(case, args)
    elif args.algo == "orr":
        args.algo_name = "ORR-inspired-LGreedy"
        assignment, prefetch_mb, ca_triggered = orr_assign(case, args)
    else:
        raise ValueError(args.algo)

    result = simulate(case, assignment, args, total_prefetch_mb=prefetch_mb, ca_triggered=ca_triggered)
    save_json(result, args.out)

    print(json.dumps(result["summary"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
