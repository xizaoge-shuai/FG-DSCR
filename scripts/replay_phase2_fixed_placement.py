import argparse
import json
import os
import random
from collections import defaultdict

def load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(obj, p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)

def node_id(n, i):
    return n.get("eid") or n.get("id") or n.get("name") or n.get("node_id") or f"edge-{i+1}"

def layer_size_sum(layers, layer_sizes):
    return sum(float(layer_sizes.get(x, 0.0)) for x in layers)

def get_assignment(res):
    if "assignment" in res:
        return {k: list(v) for k, v in res["assignment"].items()}
    if "ordered_queues" in res:
        return {k: list(v) for k, v in res["ordered_queues"].items()}
    raise RuntimeError("No assignment / ordered_queues in placement result.")

def future_count(cids, containers):
    cnt = defaultdict(int)
    for cid in cids:
        for l in containers[cid]["layers"]:
            cnt[l] += 1
    return cnt

def order_fifo(cids, case_order):
    return sorted(cids, key=lambda x: case_order[x])

def order_random(cids, seed):
    q = list(cids)
    random.Random(seed).shuffle(q)
    return q

def order_static(cids, containers, layer_sizes):
    remain = list(cids)
    ordered = []
    seen = set()
    while remain:
        best = None
        for cid in remain:
            layers = containers[cid]["layers"]
            hit = layers & seen
            miss = layers - seen
            score = layer_size_sum(hit, layer_sizes) - 0.2 * layer_size_sum(miss, layer_sizes)
            key = (-score, layer_size_sum(miss, layer_sizes), cid)
            if best is None or key < best[0]:
                best = (key, cid)
        cid = best[1]
        ordered.append(cid)
        seen |= containers[cid]["layers"]
        remain.remove(cid)
    return ordered

def order_dynamic(cids, containers, layer_sizes, use_future=True):
    remain = list(cids)
    ordered = []
    cache_proxy = set()

    while remain:
        fcnt = future_count(remain, containers) if use_future else {}
        best = None

        for cid in remain:
            layers = containers[cid]["layers"]
            hit = layers & cache_proxy
            miss = layers - cache_proxy
            reuse_mb = layer_size_sum(hit, layer_sizes)
            miss_mb = layer_size_sum(miss, layer_sizes)
            future_mb = 0.0
            if use_future:
                future_mb = sum(float(layer_sizes.get(l, 0.0)) * max(fcnt.get(l, 0) - 1, 0) for l in layers)
            score = reuse_mb - 0.5 * miss_mb + 0.05 * future_mb
            key = (-score, miss_mb, cid)
            if best is None or key < best[0]:
                best = (key, cid)

        cid = best[1]
        ordered.append(cid)
        cache_proxy |= containers[cid]["layers"]
        remain.remove(cid)

    return ordered

def evict(cache, layer_sizes, cap, policy, last_used, freq, future_cnt, protected):
    evicted_mb = 0.0
    while layer_size_sum(cache, layer_sizes) > cap + 1e-9:
        candidates = [l for l in cache if l not in protected]
        if not candidates:
            break

        if policy == "lru":
            victim = min(candidates, key=lambda l: (last_used.get(l, -1), l))
        elif policy == "lfu":
            victim = min(candidates, key=lambda l: (freq.get(l, 0), last_used.get(l, -1), l))
        else:
            # PGDSF-like: evict low future value, low freq, old layer, large layer
            victim = min(
                candidates,
                key=lambda l: (
                    future_cnt.get(l, 0),
                    freq.get(l, 0),
                    last_used.get(l, -1),
                    -float(layer_sizes.get(l, 0.0)),
                    l,
                )
            )

        evicted_mb += float(layer_sizes.get(victim, 0.0))
        cache.remove(victim)

    return evicted_mb

def simulate(case, assignment, variant, cache_policy, seed):
    layer_sizes = case.get("layer_sizes_mb", {})
    containers = {}
    case_order = {}
    for idx, c in enumerate(case["containers"]):
        containers[c["cid"]] = {
            "layers": set(c.get("layers", [])),
            "run_time": float(c.get("run_time", c.get("runtime", 20.0))),
        }
        case_order[c["cid"]] = idx

    nodes = {node_id(n, i): n for i, n in enumerate(case["nodes"])}

    ordered_queues = {}
    for eid, cids in assignment.items():
        if variant == "fifo":
            ordered_queues[eid] = order_fifo(cids, case_order)
        elif variant == "random":
            ordered_queues[eid] = order_random(cids, seed + hash(eid) % 10000)
        elif variant == "static":
            ordered_queues[eid] = order_static(cids, containers, layer_sizes)
        elif variant == "dynamic_no_future":
            ordered_queues[eid] = order_dynamic(cids, containers, layer_sizes, use_future=False)
        elif variant == "dynamic":
            ordered_queues[eid] = order_dynamic(cids, containers, layer_sizes, use_future=True)
        else:
            raise ValueError(variant)

    total_downloaded = 0.0
    total_reused = 0.0
    total_pull_time = 0.0
    total_evicted = 0.0
    completion_times = []
    node_details = {}
    container_metrics = {}
    clock = 0

    for eid, q in ordered_queues.items():
        n = nodes[eid]
        bw = float(n.get("bandwidth_mb_s", n.get("bandwidth", 60.0)))
        cap = float(n.get("repo_capacity_mb", 1024))
        cache = set(n.get("initial_cache", []))
        last_used = {l: 0 for l in cache}
        freq = defaultdict(int)
        t = 0.0
        node_downloaded = 0.0
        node_reused = 0.0
        node_evicted = 0.0
        node_pull = 0.0

        for pos, cid in enumerate(q):
            clock += 1
            layers = containers[cid]["layers"]
            remain_after = q[pos+1:]
            fcnt = future_count(remain_after, containers)

            hit = layers & cache
            miss = layers - cache

            reused = layer_size_sum(hit, layer_sizes)
            downloaded = layer_size_sum(miss, layer_sizes)
            pull_time = downloaded / max(bw, 1e-9)
            run_time = containers[cid]["run_time"]
            finish = t + pull_time + run_time

            for l in hit:
                last_used[l] = clock
                freq[l] += 1

            for l in miss:
                cache.add(l)
                last_used[l] = clock
                freq[l] += 1

            evicted = evict(cache, layer_sizes, cap, cache_policy, last_used, freq, fcnt, protected=layers)

            total_downloaded += downloaded
            total_reused += reused
            total_pull_time += pull_time
            total_evicted += evicted
            node_downloaded += downloaded
            node_reused += reused
            node_pull += pull_time
            node_evicted += evicted

            container_metrics[cid] = {
                "node": eid,
                "position": pos,
                "pull_time": pull_time,
                "downloaded_mb": downloaded,
                "reused_mb": reused,
                "cache_hit_mb": reused,
                "evicted_mb": evicted,
                "finish_time": finish,
            }

            completion_times.append(finish)
            t = finish

        node_details[eid] = {
            "num_containers": len(q),
            "finish_time": t,
            "downloaded_mb": node_downloaded,
            "reused_mb": node_reused,
            "cache_hit_mb": node_reused,
            "pull_time": node_pull,
            "evicted_mb": node_evicted,
        }

    act = sum(completion_times) / max(len(completion_times), 1)
    ams = max([d["finish_time"] for d in node_details.values()] or [0.0])
    obj = 0.5 * act + 0.5 * ams
    reuse_rate = total_reused / max(total_reused + total_downloaded, 1e-9)

    return {
        "ordered_queues": ordered_queues,
        "node_details": node_details,
        "container_metrics": container_metrics,
        "summary": {
            "num_containers": len(completion_times),
            "num_nodes": len(nodes),
            "ACT": act,
            "AMS": ams,
            "objective": obj,
            "pull_time": total_pull_time,
            "downloaded_mb": int(round(total_downloaded)),
            "reused_mb": int(round(total_reused)),
            "cache_hit_mb": int(round(total_reused)),
            "evicted_mb": int(round(total_evicted)),
            "reuse_rate": reuse_rate,
        }
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--placement-result", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--variant", required=True, choices=["fifo", "random", "static", "dynamic_no_future", "dynamic"])
    ap.add_argument("--cache-policy", default="pgdsf", choices=["lru", "lfu", "pgdsf"])
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--algo-name", default="")
    args = ap.parse_args()

    case = load_json(args.case)
    placement = load_json(args.placement_result)
    assignment = get_assignment(placement)

    res = simulate(case, assignment, args.variant, args.cache_policy, args.seed)
    res["assignment"] = assignment
    res["summary"]["algo"] = args.algo_name or f"{args.variant}-{args.cache_policy}"
    res["meta"] = {
        "fixed_placement_result": args.placement_result,
        "variant": args.variant,
        "cache_policy": args.cache_policy,
        "seed": args.seed,
    }
    save_json(res, args.out)
    print(json.dumps(res["summary"], indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
