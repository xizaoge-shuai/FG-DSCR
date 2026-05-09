import argparse
import json
import os
from collections import OrderedDict, defaultdict

def load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(obj, p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)

def get_node_id(node, i):
    for k in ["eid", "id", "name", "node_id"]:
        if k in node:
            return str(node[k])
    return f"edge-{i+1}"

def get_cid(c, i):
    for k in ["cid", "id", "container_id", "name"]:
        if k in c:
            return str(c[k])
    return f"c{i:03d}"

def get_layers(c):
    return [str(x) for x in c.get("layers", c.get("image_layers", []))]

def get_bw(node):
    for k in ["bandwidth_mb_s", "bandwidth", "download_bandwidth", "pull_bandwidth", "bw"]:
        if k in node:
            return float(node[k])
    return 100.0

def get_cache_cap(node):
    for k in ["repo_capacity_mb", "cache_capacity_mb", "cache_mb"]:
        if k in node:
            return float(node[k])
    return 0.0

def layer_size(layer_sizes, l):
    return float(layer_sizes.get(str(l), layer_sizes.get(l, 0.0)))

def layers_mb(layer_sizes, layers):
    return sum(layer_size(layer_sizes, l) for l in layers)

def get_service_time(c):
    for k in ["service_time", "runtime", "exec_time", "compute_time", "duration"]:
        if k in c:
            return float(c[k])
    return 0.0

def cache_used_mb(cache, layer_sizes):
    return sum(layer_size(layer_sizes, l) for l in cache.keys())

def touch_cache(cache, l):
    if l in cache:
        cache.move_to_end(l)

def admit_layer(cache, layer_sizes, l, cap):
    """
    cache 只表示额外复用缓存。
    单层大于 cap 时：本次可下载使用，但不保留。
    cap=0 时：不保留任何层。
    """
    cap = float(cap)
    if cap <= 0:
        return 0.0

    sz = layer_size(layer_sizes, l)
    if sz > cap:
        return 0.0

    if l in cache:
        cache.move_to_end(l)
        return 0.0

    evicted_mb = 0.0
    while cache_used_mb(cache, layer_sizes) + sz > cap and cache:
        victim, _ = cache.popitem(last=False)
        evicted_mb += layer_size(layer_sizes, victim)

    if cache_used_mb(cache, layer_sizes) + sz <= cap:
        cache[l] = True

    return evicted_mb

def score_node_for_lasa(c_layers, node_id, node, cache, layer_sizes, node_available, alpha):
    hit = [l for l in c_layers if l in cache]
    miss = [l for l in c_layers if l not in cache]

    reuse_mb = layers_mb(layer_sizes, hit)
    miss_mb = layers_mb(layer_sizes, miss)
    bw = max(get_bw(node), 1e-9)
    pull_time = miss_mb / bw

    # LASA-style locality-aware score:
    # 偏向层复用，同时避免把请求堆到已经很忙的节点。
    score = alpha * reuse_mb - (1.0 - alpha) * pull_time - 0.05 * node_available[node_id]
    return score, reuse_mb, miss_mb, pull_time

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--alpha", type=float, default=0.5)
    ap.add_argument("--algo-name", default="LASA-cacheaware")
    args = ap.parse_args()

    case = load_json(args.case)
    containers = case["containers"]
    nodes_list = case["nodes"]
    layer_sizes = case.get("layer_sizes_mb", case.get("layer_sizes", {}))

    nodes = {}
    for i, n in enumerate(nodes_list):
        nodes[get_node_id(n, i)] = n

    # 每个节点独立缓存，OrderedDict 作为 LRU cache。
    caches = {eid: OrderedDict() for eid in nodes}
    node_available = {eid: 0.0 for eid in nodes}
    ordered_queues = {eid: [] for eid in nodes}

    total_downloaded = 0.0
    total_reused = 0.0
    total_evicted = 0.0
    total_cache_hit_mb = 0.0
    total_pull_time = 0.0
    finish_times = []
    container_metrics = []

    for i, c in enumerate(containers, start=1):
        cid = get_cid(c, i)
        c_layers = get_layers(c)

        best = None
        for eid, node in nodes.items():
            score, reuse_mb, miss_mb, pull_time = score_node_for_lasa(
                c_layers=c_layers,
                node_id=eid,
                node=node,
                cache=caches[eid],
                layer_sizes=layer_sizes,
                node_available=node_available,
                alpha=args.alpha,
            )
            if best is None or score > best["score"]:
                best = {
                    "eid": eid,
                    "score": score,
                    "reuse_mb": reuse_mb,
                    "miss_mb": miss_mb,
                    "pull_time": pull_time,
                }

        eid = best["eid"]
        node = nodes[eid]
        cap = get_cache_cap(node)

        # 命中层刷新 LRU。
        for l in c_layers:
            if l in caches[eid]:
                touch_cache(caches[eid], l)

        # 缺失层下载，用于当前部署；只有 cache 装得下才保留。
        evicted_mb = 0.0
        for l in c_layers:
            if l not in caches[eid]:
                evicted_mb += admit_layer(caches[eid], layer_sizes, l, cap)

        service_time = get_service_time(c)
        start_time = node_available[eid]
        finish_time = start_time + best["pull_time"] + service_time
        node_available[eid] = finish_time
        finish_times.append(finish_time)
        ordered_queues[eid].append(cid)

        total_downloaded += best["miss_mb"]
        total_reused += best["reuse_mb"]
        total_cache_hit_mb += best["reuse_mb"]
        total_pull_time += best["pull_time"]
        total_evicted += evicted_mb

        container_metrics.append({
            "cid": cid,
            "node": eid,
            "reuse_mb": best["reuse_mb"],
            "downloaded_mb": best["miss_mb"],
            "pull_time": best["pull_time"],
            "evicted_mb": evicted_mb,
            "cache_size_after_mb": cache_used_mb(caches[eid], layer_sizes),
            "start_time": start_time,
            "finish_time": finish_time,
        })

    ACT = sum(finish_times) / len(finish_times) if finish_times else 0.0
    AMS = max(node_available.values()) if node_available else 0.0
    objective = 0.5 * ACT + 0.5 * AMS
    reuse_rate = total_reused / max(total_reused + total_downloaded, 1e-9)

    summary = {
        "algo": args.algo_name,
        "num_containers": len(containers),
        "num_nodes": len(nodes),
        "ACT": ACT,
        "AMS": AMS,
        "downloaded_mb": int(round(total_downloaded)),
        "reused_mb": int(round(total_reused)),
        "cache_hit_mb": int(round(total_cache_hit_mb)),
        "evicted_mb": int(round(total_evicted)),
        "pull_time": total_pull_time,
        "reuse_rate": reuse_rate,
        "objective": objective,
        "alpha": args.alpha,
    }

    out = {
        "summary": summary,
        "ordered_queues": ordered_queues,
        "container_metrics": container_metrics,
    }

    save_json(out, args.out)
    print(json.dumps(summary, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
