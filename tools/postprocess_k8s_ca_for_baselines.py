import argparse
import json
import re
from pathlib import Path
from collections import defaultdict

CA_EID = "__CA_GUARANTEE__"

def load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(obj, p):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)

def container_map(case):
    return {c["cid"]: c for c in case["containers"]}

def node_map(case):
    return {n["eid"]: n for n in case["nodes"]}

def normalize_assignment(res):
    """
    支持两种常见格式：
    1) assignment: {edge-1: [cid...], edge-2: [cid...]}
    2) assignment: {cid: edge-1, cid2: edge-2}
    返回 node_to_cids。
    """
    a = res.get("assignment", {})
    if not a:
        return {}

    vals = list(a.values())
    if vals and isinstance(vals[0], list):
        return {str(k): list(v) for k, v in a.items()}

    node_to_cids = defaultdict(list)
    for cid, eid in a.items():
        node_to_cids[str(eid)].append(str(cid))
    return dict(node_to_cids)

def ordered_node_to_cids(res):
    """
    优先使用 ordered_queues，因为它代表真实执行顺序。
    没有 ordered_queues 时退回 assignment。
    """
    oq = res.get("ordered_queues", {})
    if oq:
        return {str(k): list(v) for k, v in oq.items()}
    return normalize_assignment(res)

def audit_k8s_ca(case, res):
    containers = container_map(case)
    nodes = node_map(case)
    node_to_cids = ordered_node_to_cids(res)

    kept = {eid: [] for eid in nodes}
    ca_cids = []
    ca_reason = {}

    used = {
        eid: defaultdict(float)
        for eid in nodes
    }

    for eid, cids in node_to_cids.items():
        if eid not in nodes:
            continue

        node = nodes[eid]

        for cid in cids:
            if cid not in containers:
                continue

            c = containers[cid]
            ok = True
            bad_q = None
            for q, demand in c.get("resources", {}).items():
                cur = used[eid].get(q, 0.0)
                cap = float(node["resources"].get(q, 0.0))
                if cur + float(demand) > cap + 1e-9:
                    ok = False
                    bad_q = q
                    break

            if ok:
                kept[eid].append(cid)
                for q, demand in c.get("resources", {}).items():
                    used[eid][q] += float(demand)
            else:
                ca_cids.append(cid)
                ca_reason[cid] = {
                    "original_node": eid,
                    "resource": bad_q,
                    "mode": "cumulative_resource_overflow",
                }

    # 如果某些结果没有 assignment/ordered_queues，则无法逐容器审计，只能返回 0
    total_requests = len(case.get("containers", []))
    return kept, ca_cids, ca_reason, total_requests

def infer_case_from_result(result_path):
    """
    从常见文件名推断 case：
    xxx_1024mb_200.json -> cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_200.json
    xxx_cache1024_200.json -> 同上
    """
    name = Path(result_path).name

    m = re.search(r"(\d+)mb_(\d+)", name)
    if m:
        cache, req = m.group(1), m.group(2)
        return f"cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_{cache}mb_{req}.json"

    m = re.search(r"cache(\d+)_(\d+)", name)
    if m:
        cache, req = m.group(1), m.group(2)
        return f"cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_{cache}mb_{req}.json"

    m = re.search(r"_(\d+)_(\d+)\.json$", name)
    if m:
        cache, req = m.group(1), m.group(2)
        return f"cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_{cache}mb_{req}.json"

    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--result", required=True)
    ap.add_argument("--case", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--lambda-ca", type=float, default=1000.0)
    ap.add_argument("--method", default=None)
    args = ap.parse_args()

    case_path = args.case or infer_case_from_result(args.result)
    if not case_path or not Path(case_path).exists():
        raise FileNotFoundError(f"cannot infer/find case for result={args.result}; pass --case explicitly")

    case = load_json(case_path)
    res = load_json(args.result)

    kept, ca_cids, ca_reason, total_requests = audit_k8s_ca(case, res)

    s = dict(res.get("summary", {}))
    method = args.method or s.get("algo") or s.get("method") or Path(args.result).stem

    base_obj = float(
        s.get("objective_without_ca_penalty",
        s.get("objective_base",
        s.get("objective", 0.0)))
    )
    ca_count = len(ca_cids)
    ca_rate = ca_count / max(total_requests, 1)
    ca_penalty = args.lambda_ca * ca_rate
    objective_ca = base_obj + ca_penalty

    s.update({
        "algo": method,
        "num_containers": total_requests,
        "num_scheduled_containers": total_requests - ca_count,
        "num_assigned": total_requests - ca_count,
        "ca_triggered": ca_count,
        "ca_rate": ca_rate,
        "ca_penalty": ca_penalty,
        "lambda_ca": args.lambda_ca,
        "objective_without_ca_penalty": base_obj,
        "objective_ca": objective_ca,

        # 兼容旧字段
        "failed_deployments": ca_count,
        "num_failed": ca_count,
        "fail_rate": ca_rate,
        "fail_penalty": ca_penalty,
        "objective": objective_ca,
    })

    res["summary"] = s
    res["k8s_ca_assignment"] = kept
    res["ca_triggered_containers"] = ca_cids
    res["ca_triggered_reason"] = ca_reason
    res["k8s_ca_source_result"] = args.result
    res["k8s_ca_source_case"] = case_path

    save_json(res, args.out)

    print(json.dumps({
        "method": method,
        "case": case_path,
        "result": args.result,
        "out": args.out,
        "num_containers": total_requests,
        "num_scheduled_containers": total_requests - ca_count,
        "ca_triggered": ca_count,
        "ca_rate": ca_rate,
        "objective_without_ca_penalty": base_obj,
        "ca_penalty": ca_penalty,
        "objective_ca": objective_ca,
    }, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
