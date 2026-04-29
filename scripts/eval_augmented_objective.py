import argparse
import csv
import json
import math
from pathlib import Path
from collections import defaultdict

RESOURCE_KEYS = ["cpu", "mem", "memory", "disk"]

def load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def get_container_id(c):
    return c.get("cid") or c.get("id") or c.get("name")

def get_node_id(n):
    return n.get("id") or n.get("nid") or n.get("name") or n.get("node_id")

def get_resources(x):
    if "resources" in x and isinstance(x["resources"], dict):
        return dict(x["resources"])

    out = {}
    for k in RESOURCE_KEYS:
        if k in x:
            key = "mem" if k == "memory" else k
            out[key] = x[k]
    return out

def normalize_res_keys(res):
    out = {}
    for k, v in res.items():
        kk = "mem" if k == "memory" else k
        try:
            out[kk] = float(v)
        except Exception:
            pass
    return out

def looks_like_assignment_dict(d):
    if not isinstance(d, dict):
        return False
    good = 0
    for k, v in d.items():
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            good += 1
    return good >= 2

def find_assignment(obj):
    # 常见字段优先
    for key in ["assignment", "assignments", "placement", "placements", "node_assignments", "solution"]:
        if key in obj and looks_like_assignment_dict(obj[key]):
            return obj[key]

    # 递归搜索 node -> [cid] 结构
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if looks_like_assignment_dict(cur):
                return cur
            for v in cur.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            for v in cur:
                if isinstance(v, (dict, list)):
                    stack.append(v)

    raise RuntimeError("Cannot find assignment dict in result json")

def compute_fragmentation(case, result):
    assignment = find_assignment(result)

    containers = {
        get_container_id(c): c
        for c in case["containers"]
        if get_container_id(c) is not None
    }

    nodes = {
        get_node_id(n): n
        for n in case["nodes"]
        if get_node_id(n) is not None
    }

    total_assigned = sum(len(v) for v in assignment.values())
    if total_assigned <= 0:
        return float("nan")

    frag_total = 0.0

    for node_id, cids in assignment.items():
        if not cids:
            continue

        if node_id not in nodes:
            # 有些结果里的节点名可能和 case 不完全一致，跳过
            continue

        cap = normalize_res_keys(get_resources(nodes[node_id]))
        if not cap:
            continue

        dims = [d for d in ["cpu", "mem", "disk"] if d in cap and cap[d] > 0]
        if not dims:
            continue

        used = {d: 0.0 for d in dims}

        for cid in cids:
            if cid not in containers:
                continue
            cres = normalize_res_keys(get_resources(containers[cid]))
            for d in dims:
                used[d] += float(cres.get(d, 0.0))

        remain_ratios = []
        for d in dims:
            rem = max(cap[d] - used[d], 0.0)
            remain_ratios.append(rem / cap[d])

        mean_r = sum(remain_ratios) / len(remain_ratios)
        frag_j = sum((x - mean_r) ** 2 for x in remain_ratios) / len(remain_ratios)

        weight = len(cids) / total_assigned
        frag_total += weight * frag_j

    return frag_total

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", required=True, help="CSV with columns: setting,size,case,ilrsa,gc")
    ap.add_argument("--weights", nargs="+", type=float, default=[0.0, 0.1, 0.2, 0.3])
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.rows, "r", encoding="utf-8")))

    print("| setting | size | w_frag | method | ACT | AMS | DelayObj | Frag | AugScore | reduction_vs_ILRSA_% |")
    print("|---|---:|---:|---|---:|---:|---:|---:|---:|---:|")

    for row in rows:
        setting = row["setting"]
        size = row["size"]
        case = load_json(row["case"])
        il = load_json(row["ilrsa"])
        gc = load_json(row["gc"])

        methods = {
            "ILRSA": il,
            "FG-DSCR-GC": gc,
        }

        metrics = {}
        for name, obj in methods.items():
            s = obj["summary"]
            frag = compute_fragmentation(case, obj)
            metrics[name] = {
                "ACT": float(s["ACT"]),
                "AMS": float(s["AMS"]),
                "DelayObj": 0.5 * float(s["ACT"]) + 0.5 * float(s["AMS"]),
                "Frag": frag,
            }

        ref = metrics["ILRSA"]
        eps = 1e-12

        for w in args.weights:
            scores = {}
            for name, m in metrics.items():
                delay_norm = 0.5 * m["ACT"] / max(ref["ACT"], eps) + 0.5 * m["AMS"] / max(ref["AMS"], eps)
                frag_norm = m["Frag"] / max(ref["Frag"], eps)
                aug = (1.0 - w) * delay_norm + w * frag_norm
                scores[name] = aug

            for name, m in metrics.items():
                reduction = (scores["ILRSA"] - scores[name]) / max(scores["ILRSA"], eps) * 100.0
                print("| {} | {} | {:.2f} | {} | {:.3f} | {:.3f} | {:.3f} | {:.6f} | {:.6f} | {:.2f} |".format(
                    setting, size, w, name,
                    m["ACT"], m["AMS"], m["DelayObj"], m["Frag"], scores[name], reduction
                ))

if __name__ == "__main__":
    main()
