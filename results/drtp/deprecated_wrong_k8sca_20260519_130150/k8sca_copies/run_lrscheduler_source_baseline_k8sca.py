import argparse
import json
import os
from collections import Counter


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


def c_layers(c):
    return list(c.get("layers", []) or [])


def c_res(c, key):
    return float((c.get("resources", {}) or {}).get(key, 0.0))


def n_res(n, key):
    return float((n.get("resources", {}) or {}).get(key, 0.0))

CA_EID = "__CA_GUARANTEE__"

def k8s_feasible(c, n, used_res):
    """K8s-style cumulative resource filter."""
    for q in ["cpu", "mem", "disk"]:
        if used_res.get(q, 0.0) + c_res(c, q) > n_res(n, q) + 1e-9:
            return False
    return True


def container_size(c, layer_sizes):
    return sum(layer_mb(layer_sizes, l) for l in c_layers(c))


def can_run_single(c, n):
    for k in ["cpu", "mem", "disk"]:
        if c_res(c, k) > n_res(n, k):
            return False
    return True


def build_layer_pop(containers):
    pop = Counter()
    for c in containers:
        for l in set(c_layers(c)):
            pop[l] += 1
    return pop


def cache_size(cache, layer_sizes):
    return sum(layer_mb(layer_sizes, l) for l in cache)


def evict_to_fit(cache, cap, layer_sizes, layer_pop, protected=None):
    protected = set(protected or [])
    if cache_size(cache, layer_sizes) <= cap:
        return cache

    def key(l):
        # 低频优先淘汰；频率相同时大层优先淘汰
        return (layer_pop.get(l, 0), -layer_mb(layer_sizes, l))

    removable = [l for l in cache if l not in protected]
    removable.sort(key=key)

    for l in removable:
        if cache_size(cache, layer_sizes) <= cap:
            break
        cache.remove(l)

    if cache_size(cache, layer_sizes) > cap:
        rest = list(cache)
        rest.sort(key=key)
        for l in rest:
            if cache_size(cache, layer_sizes) <= cap:
                break
            cache.remove(l)

    return cache


def add_to_cache(cache, layers, cap, layer_sizes, layer_pop):
    layers = list(layers)
    for l in layers:
        cache.add(l)
    return evict_to_fit(cache, cap, layer_sizes, layer_pop, protected=layers)


def reused_missing_mb(c, cache, layer_sizes):
    reused = 0.0
    missing = 0.0
    for l in c_layers(c):
        if l in cache:
            reused += layer_mb(layer_sizes, l)
        else:
            missing += layer_mb(layer_sizes, l)
    return reused, missing


def assign_lrscheduler_source(case, args):
    layer_sizes = case["layer_sizes_mb"]
    containers = case["containers"]
    nodes = case["nodes"]

    layer_pop = build_layer_pop(containers)

    caches = {}
    used = {}
    assignment = {}

    for n in nodes:
        nid = node_id(n)
        caches[nid] = set(n.get("initial_cache", []) or [])
        used[nid] = {"cpu": 0.0, "mem": 0.0, "disk": 0.0}
        assignment[nid] = []

    assignment[CA_EID] = []

    for c in containers:
        best_nid = None
        best_score = -1e100

        for n in nodes:
            nid = node_id(n)

            if not k8s_feasible(c, n, used[nid]):
                continue
            if not can_run_single(c, n):
                continue

            total_mb = max(container_size(c, layer_sizes), 1e-9)
            exist_mb, missing_mb = reused_missing_mb(c, caches[nid], layer_sizes)

            # 对应源码 ComputeLayerScore:
            # resScore = existing_layer_size / requested_layer_size * 100 / 2
            res_score = exist_mb / total_mb * 100.0 / 2.0

            # 对应源码 computeWeight 里的 OccuCPU / OccuMem / std
            cpu_cap = max(n_res(n, "cpu"), 1e-9)
            mem_cap = max(n_res(n, "mem"), 1e-9)

            occu_cpu = (used[nid]["cpu"] + c_res(c, "cpu")) / cpu_cap
            occu_mem = (used[nid]["mem"] + c_res(c, "mem")) / mem_cap

            std = abs((occu_cpu - occu_mem) / 2.0)

            # 对应源码:
            # if layerSizeMB > 10 && std < 0.16 && OccuCPU < 0.6 return 2 else 0.5
            if exist_mb > args.layer_mb_threshold and std < args.std_threshold and occu_cpu < args.cpu_threshold:
                dynamic_weight = args.high_weight
            else:
                dynamic_weight = args.low_weight

            layer_plugin_score = res_score * dynamic_weight

            # 近似 NodeResourcesBalancedAllocation，越均衡越高
            resource_balance_score = max(0.0, 1.0 - 2.0 * std) * 100.0

            # 对应 scheduler-config:
            # LayerPro weight = 2
            # NodeResourcesBalancedAllocation weight = 1
            final_score = args.plugin_weight_layer * layer_plugin_score + args.plugin_weight_resource * resource_balance_score

            if final_score > best_score:
                best_score = final_score
                best_nid = nid

        if best_nid is None:
            assignment[CA_EID].append(c["cid"])
            continue

        assignment[best_nid].append(c["cid"])

        # 更新调度状态：用于后续 score 计算
        used[best_nid]["cpu"] += c_res(c, "cpu")
        used[best_nid]["mem"] += c_res(c, "mem")
        used[best_nid]["disk"] += c_res(c, "disk")

        n = next(x for x in nodes if node_id(x) == best_nid)
        cap = float(n.get("repo_capacity_mb", 1024))
        miss_layers = [l for l in c_layers(c) if l not in caches[best_nid]]
        caches[best_nid] = add_to_cache(caches[best_nid], miss_layers, cap, layer_sizes, layer_pop)

    return assignment


def simulate(case, assignment, args):
    layer_sizes = case["layer_sizes_mb"]
    containers = case["containers"]
    nodes = case["nodes"]

    c_by_id = {c["cid"]: c for c in containers}
    n_by_id = {node_id(n): n for n in nodes}
    layer_pop = build_layer_pop(containers)

    ordered_queues = {}
    node_details = {}
    container_metrics = {}

    all_finish = []
    total_downloaded = 0.0
    total_reused = 0.0

    for nid, cids in assignment.items():
        if nid == CA_EID:
            continue
        n = n_by_id[nid]
        cache = set(n.get("initial_cache", []) or [])
        cap = float(n.get("repo_capacity_mb", 1024))
        bw = float(n.get("bandwidth_mb_s", 1.0))

        ordered_queues[nid] = list(cids)

        t = 0.0
        node_downloaded = 0.0
        node_reused = 0.0

        for cid in cids:
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
            all_finish.append(finish)

            container_metrics[cid] = {
                "node": nid,
                "start_time": start,
                "finish_time": finish,
                "downloaded_mb": missing,
                "reused_mb": reused,
                "download_time": download_time,
                "run_time": run_time,
            }

            miss_layers = [l for l in c_layers(c) if l not in cache]
            cache = add_to_cache(cache, miss_layers, cap, layer_sizes, layer_pop)

        node_details[nid] = {
            "num_containers": len(cids),
            "finish_time": t,
            "downloaded_mb": node_downloaded,
            "reused_mb": node_reused,
            "final_cache_mb": cache_size(cache, layer_sizes),
            "final_cache_layers": len(cache),
        }

    ACT = sum(all_finish) / max(len(all_finish), 1)
    AMS = max(all_finish) if all_finish else 0.0
    objective = args.alpha_obj * ACT + (1.0 - args.alpha_obj) * AMS

    denom = total_downloaded + total_reused
    reuse_rate = total_reused / denom if denom > 0 else 0.0

    return {
        "assignment": assignment,
        "ordered_queues": ordered_queues,
        "summary": {
            "algo": "LRScheduler-source-inspired",
            "num_containers": len(containers),
            "num_nodes": len(nodes),
            "ACT": ACT,
            "AMS": AMS,
            "downloaded_mb": int(round(total_downloaded)),
            "reused_mb": int(round(total_reused)),
            "reuse_rate": reuse_rate,
            "objective": objective,
            "params": {
                "layer_mb_threshold": args.layer_mb_threshold,
                "std_threshold": args.std_threshold,
                "cpu_threshold": args.cpu_threshold,
                "high_weight": args.high_weight,
                "low_weight": args.low_weight,
                "plugin_weight_layer": args.plugin_weight_layer,
                "plugin_weight_resource": args.plugin_weight_resource,
            },
        },
        "node_details": node_details,
        "container_metrics": container_metrics,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--out", required=True)

    ap.add_argument("--alpha-obj", type=float, default=0.5)
    ap.add_argument("--ca-penalty", type=float, default=1000.0)

    # 来自 LRScheduler 源码 computeWeight()
    ap.add_argument("--layer-mb-threshold", type=float, default=10.0)
    ap.add_argument("--std-threshold", type=float, default=0.16)
    ap.add_argument("--cpu-threshold", type=float, default=0.6)
    ap.add_argument("--high-weight", type=float, default=2.0)
    ap.add_argument("--low-weight", type=float, default=0.5)

    # 来自 scheduler-config.yaml
    ap.add_argument("--plugin-weight-layer", type=float, default=2.0)
    ap.add_argument("--plugin-weight-resource", type=float, default=1.0)

    args = ap.parse_args()

    case = load_json(args.case)
    assignment = assign_lrscheduler_source(case, args)
    res = simulate(case, assignment, args)
    save_json(res, args.out)

    print(json.dumps(res["summary"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
