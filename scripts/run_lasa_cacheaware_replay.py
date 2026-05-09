import argparse
import json
import os
import subprocess
import tempfile
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


def get_service_time(c):
    for k in ["service_time", "runtime", "exec_time", "compute_time", "duration", "task_time"]:
        if k in c:
            return float(c[k])
    return 0.0


def layer_size(layer_sizes, l):
    return float(layer_sizes.get(str(l), layer_sizes.get(l, 0.0)))


def layers_mb(layer_sizes, layers):
    return sum(layer_size(layer_sizes, l) for l in layers)


def cache_used_mb(cache, layer_sizes):
    return sum(layer_size(layer_sizes, l) for l in cache.keys())


def admit_layer_lru(cache, layer_sizes, layer, cap):
    """
    cache 表示额外复用缓存，不表示 K8s 原生运行镜像所需磁盘。
    单层大于 cap：本次可以拉取运行，但不保留。
    cap=0：no-cache，不保留任何层。
    """
    cap = float(cap)
    if cap <= 0:
        return 0.0

    sz = layer_size(layer_sizes, layer)
    if sz > cap:
        return 0.0

    if layer in cache:
        cache.move_to_end(layer)
        return 0.0

    evicted_mb = 0.0
    while cache_used_mb(cache, layer_sizes) + sz > cap and cache:
        victim, _ = cache.popitem(last=False)
        evicted_mb += layer_size(layer_sizes, victim)

    if cache_used_mb(cache, layer_sizes) + sz <= cap:
        cache[layer] = True

    return evicted_mb


def list_to_cids(v):
    out = []
    for x in v:
        if isinstance(x, (str, int)):
            out.append(str(x))
        elif isinstance(x, dict):
            for k in ["cid", "id", "container_id", "container", "name"]:
                if k in x:
                    out.append(str(x[k]))
                    break
    return out


def dict_looks_like_node_queues(d):
    if not isinstance(d, dict) or not d:
        return False
    ok = 0
    for _, v in d.items():
        if isinstance(v, list):
            ok += 1
    return ok > 0


def extract_ordered_queues(obj):
    """
    尽量兼容不同 baseline 输出格式。
    优先读取 node -> [cid] 的队列。
    """
    if isinstance(obj, dict):
        for key in [
            "ordered_queues",
            "node_queues",
            "queues",
            "assignment",
            "assignments",
            "node_to_containers",
            "placement_by_node",
            "placements_by_node",
        ]:
            if key in obj and dict_looks_like_node_queues(obj[key]):
                return {str(k): list_to_cids(v) for k, v in obj[key].items()}

        # container -> node 映射，转成 node 队列
        for key in ["container_to_node", "cid_to_node", "placement_map"]:
            if key in obj and isinstance(obj[key], dict):
                q = defaultdict(list)
                for cid, nid in obj[key].items():
                    q[str(nid)].append(str(cid))
                return dict(q)

        # list placements
        for key in ["placements", "placement", "schedule", "deployments"]:
            if key in obj and isinstance(obj[key], list):
                q = defaultdict(list)
                for item in obj[key]:
                    if not isinstance(item, dict):
                        continue
                    cid = None
                    nid = None
                    for ck in ["cid", "id", "container_id", "container", "name"]:
                        if ck in item:
                            cid = item[ck]
                            break
                    for nk in ["node", "node_id", "eid", "edge", "host"]:
                        if nk in item:
                            nid = item[nk]
                            break
                    if cid is not None and nid is not None:
                        q[str(nid)].append(str(cid))
                if q:
                    return dict(q)

        for v in obj.values():
            got = extract_ordered_queues(v)
            if got:
                return got

    elif isinstance(obj, list):
        for v in obj:
            got = extract_ordered_queues(v)
            if got:
                return got

    return None


def align_node_ids(queues, node_ids):
    node_ids = list(node_ids)
    node_set = set(node_ids)
    out = defaultdict(list)

    for k, cids in queues.items():
        kk = str(k)

        if kk in node_set:
            out[kk].extend(cids)
            continue

        try:
            idx = int(kk)
            if 0 <= idx < len(node_ids):
                out[node_ids[idx]].extend(cids)
                continue
            if 1 <= idx <= len(node_ids):
                out[node_ids[idx - 1]].extend(cids)
                continue
        except Exception:
            pass

        # 如果无法对齐，保留原 key，后面会跳过不存在节点
        out[kk].extend(cids)

    for nid in node_ids:
        out.setdefault(nid, [])

    return dict(out)


def run_original_lasa(case_path, tmp_out, alpha, original_script):
    cmd = [
        "python3",
        original_script,
        "--case", case_path,
        "--out", tmp_out,
        "--alpha", str(alpha),
        "--algo-name", "LASA-placement-only",
    ]
    subprocess.run(cmd, check=True)


def replay(case, lasa_result):
    containers = case["containers"]
    nodes_list = case["nodes"]
    layer_sizes = case.get("layer_sizes_mb", case.get("layer_sizes", {}))

    cid_to_container = {}
    for i, c in enumerate(containers):
        cid_to_container[get_cid(c, i)] = c

    nodes = {}
    for i, n in enumerate(nodes_list):
        nodes[get_node_id(n, i)] = n

    queues = extract_ordered_queues(lasa_result)
    if not queues:
        raise RuntimeError("Cannot find ordered queues / assignment from original LASA output.")

    queues = align_node_ids(queues, nodes.keys())

    caches = {eid: OrderedDict() for eid in nodes}
    node_available = {eid: 0.0 for eid in nodes}

    total_downloaded = 0.0
    total_reused = 0.0
    total_cache_hit = 0.0
    total_evicted = 0.0
    total_pull_time = 0.0

    finish_times = []
    container_metrics = []
    ordered_queues = {eid: [] for eid in nodes}

    for eid, q in queues.items():
        if eid not in nodes:
            continue

        node = nodes[eid]
        cap = get_cache_cap(node)

        for cid in q:
            cid = str(cid)
            if cid not in cid_to_container:
                continue

            c = cid_to_container[cid]
            layers = get_layers(c)

            hit_layers = [l for l in layers if l in caches[eid]]
            miss_layers = [l for l in layers if l not in caches[eid]]

            reuse_mb = layers_mb(layer_sizes, hit_layers)
            miss_mb = layers_mb(layer_sizes, miss_layers)
            bw = max(get_bw(node), 1e-9)
            pull_time = miss_mb / bw

            # 命中层刷新 LRU
            for l in hit_layers:
                caches[eid].move_to_end(l)

            # 缺失层：本次可以拉取运行；只有装得下才保留到额外 cache
            evicted_mb = 0.0
            for l in miss_layers:
                evicted_mb += admit_layer_lru(caches[eid], layer_sizes, l, cap)

            service_time = get_service_time(c)
            start_time = node_available[eid]
            finish_time = start_time + pull_time + service_time
            node_available[eid] = finish_time

            total_downloaded += miss_mb
            total_reused += reuse_mb
            total_cache_hit += reuse_mb
            total_evicted += evicted_mb
            total_pull_time += pull_time

            finish_times.append(finish_time)
            ordered_queues[eid].append(cid)

            container_metrics.append({
                "cid": cid,
                "node": eid,
                "reuse_mb": reuse_mb,
                "downloaded_mb": miss_mb,
                "pull_time": pull_time,
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
        "algo": "LASA-cacheaware-replay",
        "num_containers": len(finish_times),
        "num_nodes": len(nodes),
        "ACT": ACT,
        "AMS": AMS,
        "downloaded_mb": int(round(total_downloaded)),
        "reused_mb": int(round(total_reused)),
        "cache_hit_mb": int(round(total_cache_hit)),
        "evicted_mb": int(round(total_evicted)),
        "pull_time": total_pull_time,
        "reuse_rate": reuse_rate,
        "objective": objective,
    }

    return {
        "summary": summary,
        "ordered_queues": ordered_queues,
        "container_metrics": container_metrics,
        "note": (
            "LASA placement/order is generated by the original LASA script. "
            "Metrics are recomputed by cache-aware replay where cache is extra reusable layer storage."
        ),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--alpha", type=float, default=0.5)
    ap.add_argument("--original-lasa-script", default="scripts/run_lasa_reimpl.py")
    ap.add_argument("--keep-temp", action="store_true")
    args = ap.parse_args()

    case = load_json(args.case)

    tmp_dir = tempfile.mkdtemp(prefix="lasa_cacheaware_")
    tmp_out = os.path.join(tmp_dir, "original_lasa_output.json")

    run_original_lasa(
        case_path=args.case,
        tmp_out=tmp_out,
        alpha=args.alpha,
        original_script=args.original_lasa_script,
    )

    lasa_result = load_json(tmp_out)
    replay_result = replay(case, lasa_result)

    replay_result["original_lasa_tmp_output"] = tmp_out if args.keep_temp else None

    save_json(replay_result, args.out)
    print(json.dumps(replay_result["summary"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
