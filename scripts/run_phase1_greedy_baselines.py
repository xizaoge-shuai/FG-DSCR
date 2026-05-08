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

def resource_sum(res):
    return float(res.get("cpu", 0.0)), float(res.get("mem", 0.0)), float(res.get("disk", 0.0))

def simulate(case, assignment, order_policy="arrival"):
    layer_sizes = case.get("layer_sizes_mb", {})
    containers = {c["cid"]: c for c in case["containers"]}
    nodes = {node_id(n, i): n for i, n in enumerate(case["nodes"])}

    ordered_queues = {}
    case_order = {c["cid"]: idx for idx, c in enumerate(case["containers"])}

    for eid, cids in assignment.items():
        if order_policy == "arrival":
            ordered_queues[eid] = sorted(cids, key=lambda x: case_order[x])
        else:
            ordered_queues[eid] = list(cids)

    total_downloaded = 0.0
    total_reused = 0.0
    container_metrics = {}
    node_details = {}
    completion_times = []

    for eid, q in ordered_queues.items():
        n = nodes[eid]
        bw = float(n.get("bandwidth_mb_s", n.get("bandwidth", 60.0)))
        cap = float(n.get("repo_capacity_mb", 1024))
        cache = set(n.get("initial_cache", []))
        cache_order = []
        t = 0.0
        node_downloaded = 0.0
        node_reused = 0.0

        for pos, cid in enumerate(q):
            c = containers[cid]
            layers = set(c.get("layers", []))
            hit = layers & cache
            miss = layers - cache

            reused = layer_size_sum(hit, layer_sizes)
            downloaded = layer_size_sum(miss, layer_sizes)
            pull_time = downloaded / max(bw, 1e-9)
            run_time = float(c.get("run_time", c.get("runtime", 20.0)))
            start = t + pull_time
            finish = start + run_time

            total_downloaded += downloaded
            total_reused += reused
            node_downloaded += downloaded
            node_reused += reused

            for l in miss:
                cache.add(l)
                cache_order.append(l)

            while layer_size_sum(cache, layer_sizes) > cap and cache_order:
                victim = cache_order.pop(0)
                if victim in cache and victim not in layers:
                    cache.remove(victim)

            container_metrics[cid] = {
                "node": eid,
                "position": pos,
                "pull_time": pull_time,
                "downloaded_mb": downloaded,
                "reused_mb": reused,
                "start_time": start,
                "finish_time": finish,
            }

            completion_times.append(finish)
            t = finish

        node_details[eid] = {
            "num_containers": len(q),
            "finish_time": t,
            "downloaded_mb": node_downloaded,
            "reused_mb": node_reused,
        }

    n_total = len(completion_times)
    act = sum(completion_times) / max(n_total, 1)
    ams = max([d["finish_time"] for d in node_details.values()] or [0.0])
    objective = 0.5 * act + 0.5 * ams
    reuse_rate = total_reused / max(total_downloaded + total_reused, 1e-9)

    return {
        "ordered_queues": ordered_queues,
        "node_details": node_details,
        "container_metrics": container_metrics,
        "summary": {
            "num_containers": n_total,
            "num_nodes": len(nodes),
            "ACT": act,
            "AMS": ams,
            "objective": objective,
            "downloaded_mb": int(round(total_downloaded)),
            "reused_mb": int(round(total_reused)),
            "reuse_rate": reuse_rate,
        }
    }

def resource_greedy(case):
    nodes = {node_id(n, i): n for i, n in enumerate(case["nodes"])}
    assignment = {eid: [] for eid in nodes}
    used = {eid: defaultdict(float) for eid in nodes}

    for c in case["containers"]:
        cid = c["cid"]
        cres = c.get("resources", {})
        best = None

        for eid, n in nodes.items():
            nres = n.get("resources", {})
            score = 0.0
            feasible = True
            for r in ["cpu", "mem", "disk"]:
                cap = float(nres.get(r, 1e9))
                req = float(cres.get(r, 0.0))
                u = used[eid][r]
                if u + req > cap:
                    feasible = False
                score += (u + req) / max(cap, 1e-9)
            score += 0.05 * len(assignment[eid])

            if feasible:
                key = (score, len(assignment[eid]), eid)
            else:
                key = (1e9 + score, len(assignment[eid]), eid)

            if best is None or key < best[0]:
                best = (key, eid)

        eid = best[1]
        assignment[eid].append(cid)
        for r, v in cres.items():
            try:
                used[eid][r] += float(v)
            except Exception:
                pass

    return assignment

def layer_locality_greedy(case):
    nodes = {node_id(n, i): n for i, n in enumerate(case["nodes"])}
    layer_sizes = case.get("layer_sizes_mb", {})
    assignment = {eid: [] for eid in nodes}
    node_layers = {eid: set(n.get("initial_cache", [])) for eid, n in nodes.items()}

    for c in case["containers"]:
        cid = c["cid"]
        layers = set(c.get("layers", []))
        best = None

        for eid, n in nodes.items():
            hit = layers & node_layers[eid]
            miss = layers - node_layers[eid]
            hit_mb = layer_size_sum(hit, layer_sizes)
            miss_mb = layer_size_sum(miss, layer_sizes)

            # 只追求层命中/少下载，不显式考虑负载
            key = (miss_mb, -hit_mb, len(assignment[eid]), eid)
            if best is None or key < best[0]:
                best = (key, eid)

        eid = best[1]
        assignment[eid].append(cid)
        node_layers[eid] |= layers

    return assignment

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--algo", required=True, choices=["resource_greedy", "layer_locality_greedy"])
    ap.add_argument("--order-policy", default="arrival", choices=["arrival"])
    args = ap.parse_args()

    case = load_json(args.case)

    if args.algo == "resource_greedy":
        assignment = resource_greedy(case)
        algo_name = "Resource-Greedy"
    else:
        assignment = layer_locality_greedy(case)
        algo_name = "LayerLocality-Greedy"

    result = simulate(case, assignment, order_policy=args.order_policy)
    result["assignment"] = assignment
    result["summary"]["algo"] = algo_name

    save_json(result, args.out)
    print(json.dumps(result["summary"], indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
