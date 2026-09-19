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


def container_size(c, layer_sizes):
    return sum(layer_mb(layer_sizes, l) for l in c_layers(c))


def can_run_single(c, n):
    for k in ["cpu", "mem", "disk"]:
        if c_res(c, k) > n_res(n, k):
            return False
    return True

def can_fit_cumulative(c, n, used_n):
    for k in ["cpu", "mem", "disk"]:
        if used_n.get(k, 0.0) + c_res(c, k) > n_res(n, k) + 1e-9:
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


def resource_pressure(c, n, used):
    """
    近似 GAHRL 里的 computational resource allocation / resource distribution 影响。
    这里不训练连续资源分配网络，而是用放置后的资源占用压力和资源不均衡作为代价项。
    """
    ratios = []
    for k in ["cpu", "mem", "disk"]:
        cap = max(n_res(n, k), 1e-9)
        ratios.append((used[k] + c_res(c, k)) / cap)

    avg = sum(ratios) / len(ratios)
    max_ratio = max(ratios)
    imbalance = sum(abs(x - avg) for x in ratios) / len(ratios)

    return max_ratio, imbalance


def assign_gahrl_objective_greedy(case, args):
    """
    GAHRL-objective-greedy.

    不是复现 GAHRL 的 GCN/FM/DDPG/Dueling-DQN 训练，
    而是抽取论文里的核心优化目标：
      1. service latency
      2. storage/download cost from missing image layers
      3. computational resource pressure
      4. layer sharing benefit

    对每个服务请求，选择即时综合代价最小的节点。
    """
    layer_sizes = case["layer_sizes_mb"]
    containers = case["containers"]
    nodes = case["nodes"]

    layer_pop = build_layer_pop(containers)

    caches = {}
    used = {}
    queue_finish = {}
    queue_len = {}
    assignment = {}

    for n in nodes:
        nid = node_id(n)
        caches[nid] = set(n.get("initial_cache", []) or [])
        used[nid] = {"cpu": 0.0, "mem": 0.0, "disk": 0.0}
        queue_finish[nid] = 0.0
        queue_len[nid] = 0
        assignment[nid] = []

    # 用于归一化，避免不同量纲权重太敏感
    avg_container_mb = sum(container_size(c, layer_sizes) for c in containers) / max(len(containers), 1)
    avg_runtime = sum(float(c.get("run_time", 0.0)) for c in containers) / max(len(containers), 1)
    avg_bw = sum(float(n.get("bandwidth_mb_s", 1.0)) for n in nodes) / max(len(nodes), 1)
    norm_time = max(avg_runtime + avg_container_mb / max(avg_bw, 1e-9), 1e-9)
    norm_storage = max(avg_container_mb, 1e-9)

    ca_triggered = []

    for c in containers:
        best_nid = None
        best_cost = 1e100

        for n in nodes:
            nid = node_id(n)
            if (not can_run_single(c, n)) or (not can_fit_cumulative(c, n, used[nid])):
                continue

            bw = float(n.get("bandwidth_mb_s", 1.0))
            reused, missing = reused_missing_mb(c, caches[nid], layer_sizes)
            total_mb = max(reused + missing, 1e-9)

            # 对应论文 storage cost: 需要下载/存储的缺失 layer 总大小
            storage_cost = missing

            # 对应 startup time: missing layer size / download speed
            startup_time = missing / max(bw, 1e-9)

            # 用 queue finish 近似已有排队等待；run_time 近似服务处理时间
            service_latency = queue_finish[nid] + startup_time + float(c.get("run_time", 0.0))

            # 资源压力 / 资源分布
            max_pressure, imbalance = resource_pressure(c, n, used[nid])

            # layer sharing benefit
            layer_hit_ratio = reused / total_mb

            # cache pressure：放入该容器缺失层后是否可能超过 repo capacity
            cap = float(n.get("repo_capacity_mb", 1024))
            new_layers = [l for l in c_layers(c) if l not in caches[nid]]
            new_cache_mb = cache_size(caches[nid], layer_sizes) + sum(layer_mb(layer_sizes, l) for l in new_layers)
            cache_pressure = max(0.0, new_cache_mb - cap) / max(cap, 1e-9)

            # GAHRL 论文里有 lambda 权衡 latency cost 和 storage cost。
            # 这里 args.lambda_latency 对应更重视 latency；
            # 1 - lambda_latency 对应更重视 storage/download cost。
            latency_term = service_latency / norm_time
            storage_term = storage_cost / norm_storage

            cost = (
                args.lambda_latency * latency_term
                + (1.0 - args.lambda_latency) * storage_term
                + args.w_resource * max_pressure
                + args.w_imbalance * imbalance
                + args.w_cache_pressure * cache_pressure
                - args.w_layer_hit * layer_hit_ratio
            )

            if cost < best_cost:
                best_cost = cost
                best_nid = nid

        if best_nid is None:
            ca_triggered.append(c["cid"])
            continue

        assignment[best_nid].append(c["cid"])

        n = next(x for x in nodes if node_id(x) == best_nid)
        bw = float(n.get("bandwidth_mb_s", 1.0))
        cap = float(n.get("repo_capacity_mb", 1024))

        reused, missing = reused_missing_mb(c, caches[best_nid], layer_sizes)
        queue_finish[best_nid] += missing / max(bw, 1e-9) + float(c.get("run_time", 0.0))
        queue_len[best_nid] += 1

        for k in ["cpu", "mem", "disk"]:
            used[best_nid][k] += c_res(c, k)

        miss_layers = [l for l in c_layers(c) if l not in caches[best_nid]]
        caches[best_nid] = add_to_cache(caches[best_nid], miss_layers, cap, layer_sizes, layer_pop)

    return assignment, ca_triggered


def greedy_cache_order(cids, c_by_id, node, initial_cache, layer_sizes, layer_pop):
    """
    可选的 cache-aware ordering。
    如果 --order cache，会让 baseline 更强；
    如果 --order arrival，则完全按请求到达顺序模拟。
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
            key = (hit, -missing)
            if best_key is None or key > best_key:
                best_key = key
                best_idx = idx

        cid = remaining.pop(best_idx)
        order.append(cid)

        c = c_by_id[cid]
        miss_layers = [l for l in c_layers(c) if l not in cache]
        cache = add_to_cache(cache, miss_layers, cap, layer_sizes, layer_pop)

    return order


def simulate(case, assignment, args, ca_triggered=None):
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
        n = n_by_id[nid]
        init_cache = set(n.get("initial_cache", []) or [])
        cap = float(n.get("repo_capacity_mb", 1024))
        bw = float(n.get("bandwidth_mb_s", 1.0))

        if args.order == "cache":
            q = greedy_cache_order(cids, c_by_id, n, init_cache, layer_sizes, layer_pop)
        else:
            q = list(cids)

        ordered_queues[nid] = q

        cache = set(init_cache)
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
            "num_containers": len(q),
            "finish_time": t,
            "downloaded_mb": node_downloaded,
            "reused_mb": node_reused,
            "final_cache_mb": cache_size(cache, layer_sizes),
            "final_cache_layers": len(cache),
        }

    ACT = sum(all_finish) / max(len(all_finish), 1)
    AMS = max(all_finish) if all_finish else 0.0
    objective_base = args.alpha_obj * ACT + (1.0 - args.alpha_obj) * AMS
    ca_triggered = list(ca_triggered or [])
    ca_count = len(ca_triggered)
    ca_rate = ca_count / max(len(containers), 1)
    ca_penalty = args.lambda_ca * ca_rate
    objective = objective_base + ca_penalty

    denom = total_downloaded + total_reused
    reuse_rate = total_reused / denom if denom > 0 else 0.0

    return {
        "assignment": assignment,
        "ordered_queues": ordered_queues,
        "summary": {
            "algo": "GAHRL-objective-greedy",
            "num_containers": len(containers),
            "num_scheduled_containers": len(containers) - ca_count,
            "num_assigned": len(containers) - ca_count,
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
            "params": {
                "lambda_latency": args.lambda_latency,
                "w_resource": args.w_resource,
                "w_imbalance": args.w_imbalance,
                "w_cache_pressure": args.w_cache_pressure,
                "w_layer_hit": args.w_layer_hit,
                "order": args.order,
            },
        },
        "ca_triggered_containers": ca_triggered,
        "node_details": node_details,
        "container_metrics": container_metrics,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--alpha-obj", type=float, default=0.5)
    ap.add_argument("--lambda-ca", type=float, default=1000.0)

    # GAHRL 论文里 lambda 用于权衡 latency 和 storage cost。
    # 这里 lambda_latency 越大，越重视延迟；越小，越重视下载/存储。
    ap.add_argument("--lambda-latency", type=float, default=0.5)

    # 资源压力和资源不均衡，用来近似 computational resource allocation 的影响。
    ap.add_argument("--w-resource", type=float, default=0.30)
    ap.add_argument("--w-imbalance", type=float, default=0.20)

    # 缓存压力项。
    ap.add_argument("--w-cache-pressure", type=float, default=0.20)

    # layer hit benefit。
    ap.add_argument("--w-layer-hit", type=float, default=0.20)

    # arrival 更贴近原论文按请求顺序部署；cache 会让 baseline 更强。
    ap.add_argument("--order", choices=["arrival", "cache"], default="cache")

    args = ap.parse_args()

    case = load_json(args.case)
    assignment, ca_triggered = assign_gahrl_objective_greedy(case, args)
    res = simulate(case, assignment, args, ca_triggered=ca_triggered)
    save_json(res, args.out)

    print(json.dumps(res["summary"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
