import argparse
import csv
import json
from collections import deque

ALIASES = {
    "cpu":  ["cpu", "CPU", "vcpu", "vcpus"],
    "mem":  ["mem", "memory", "Memory", "ram", "RAM"],
    "disk": ["disk", "Disk", "storage", "Storage"],
}

def load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def cid(c):
    return c.get("cid") or c.get("id") or c.get("name")

def nid(n):
    return n.get("eid") or n.get("id") or n.get("nid") or n.get("name") or n.get("node_id")

def normalize_res(d):
    out = {}
    if not isinstance(d, dict):
        return out

    # nested resource dicts
    for key in ["resources", "resource", "capacity", "capacities", "res", "demand", "demands", "resource_demands"]:
        if key in d and isinstance(d[key], dict):
            out.update(normalize_res(d[key]))

    # direct keys
    for std, keys in ALIASES.items():
        for k in keys:
            if k in d:
                try:
                    out[std] = float(d[k])
                except Exception:
                    pass
                break

    return out

def looks_like_assignment(d):
    if not isinstance(d, dict):
        return False
    cnt = 0
    total = 0
    for k, v in d.items():
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            cnt += 1
            total += len(v)
    return cnt >= 2 and total > 0

def find_assignment(obj):
    # preferred keys
    for key in ["assignment", "assignments", "placement", "placements", "node_assignments", "final_assignment"]:
        if key in obj and looks_like_assignment(obj[key]):
            return obj[key], key

    # recursive search
    q = deque([("", obj)])
    cands = []
    while q:
        path, cur = q.popleft()
        if isinstance(cur, dict):
            if looks_like_assignment(cur):
                total = sum(len(v) for v in cur.values())
                cands.append((total, path or "<root>", cur))
            for k, v in cur.items():
                if isinstance(v, (dict, list)):
                    q.append((f"{path}.{k}" if path else k, v))
        elif isinstance(cur, list):
            for i, v in enumerate(cur):
                if isinstance(v, (dict, list)):
                    q.append((f"{path}[{i}]", v))

    if not cands:
        raise RuntimeError("Cannot find assignment dict in result JSON")

    cands.sort(reverse=True, key=lambda x: x[0])
    return cands[0][2], cands[0][1]

def compute_frag(case, result):
    assignment, assign_path = find_assignment(result)

    containers = {cid(c): c for c in case["containers"] if cid(c)}
    nodes = {nid(n): n for n in case["nodes"] if nid(n)}

    matched_nodes = 0
    total_assigned = sum(len(v) for v in assignment.values())

    if total_assigned <= 0:
        raise RuntimeError("Empty assignment")

    frag_var_total = 0.0
    frag_range_total = 0.0
    active_nodes = 0
    load_counts = []

    for node_id, cids in assignment.items():
        if not cids:
            continue

        if node_id not in nodes:
            continue

        cap = normalize_res(nodes[node_id])
        dims = [d for d in ["cpu", "mem", "disk"] if d in cap and cap[d] > 0]

        if len(dims) < 2:
            continue

        used = {d: 0.0 for d in dims}

        for x in cids:
            if x not in containers:
                continue
            req = normalize_res(containers[x])
            for d in dims:
                used[d] += req.get(d, 0.0)

        remain = []
        for d in dims:
            r = max(cap[d] - used[d], 0.0) / cap[d]
            remain.append(r)

        mean = sum(remain) / len(remain)
        frag_var = sum((x - mean) ** 2 for x in remain) / len(remain)
        frag_range = max(remain) - min(remain)

        w = len(cids) / total_assigned
        frag_var_total += w * frag_var
        frag_range_total += w * frag_range

        matched_nodes += 1
        active_nodes += 1
        load_counts.append(len(cids))

    if matched_nodes == 0:
        raise RuntimeError(
            "No matched nodes/resources. "
            f"assignment_path={assign_path}, assignment_keys={list(assignment.keys())[:5]}, "
            f"case_nodes={list(nodes.keys())[:5]}"
        )

    mean_load = sum(load_counts) / len(load_counts)
    load_var = sum((x - mean_load) ** 2 for x in load_counts) / len(load_counts)

    return {
        "FragVar": frag_var_total,
        "FragRange": frag_range_total,
        "LoadVar": load_var,
        "active_nodes": active_nodes,
        "assignment_path": assign_path,
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", required=True)
    ap.add_argument("--weights", nargs="+", type=float, default=[0.0, 0.1, 0.2, 0.3])
    ap.add_argument("--frag-metric", choices=["FragVar", "FragRange", "LoadVar"], default="FragRange")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.rows, "r", encoding="utf-8")))

    print("| setting | size | w_frag | method | ACT | AMS | DelayNorm | FragMetric | FragNorm | AugScore | reduction_vs_ILRSA_% |")
    print("|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|")

    for row in rows:
        setting = row["setting"]
        size = row["size"]
        case = load_json(row["case"])
        il = load_json(row["ilrsa"])
        gc = load_json(row["gc"])

        data = {}
        for name, obj in [("ILRSA", il), ("FG-DSCR-GC", gc)]:
            s = obj["summary"]
            frag = compute_frag(case, obj)
            data[name] = {
                "ACT": float(s["ACT"]),
                "AMS": float(s["AMS"]),
                "FragMetric": float(frag[args.frag_metric]),
            }

        ref = data["ILRSA"]
        eps = 1e-12

        for name, m in data.items():
            m["DelayNorm"] = 0.5 * m["ACT"] / ref["ACT"] + 0.5 * m["AMS"] / ref["AMS"]
            m["FragNorm"] = (m["FragMetric"] + eps) / (ref["FragMetric"] + eps)

        for w in args.weights:
            scores = {}
            for name, m in data.items():
                scores[name] = (1 - w) * m["DelayNorm"] + w * m["FragNorm"]

            for name, m in data.items():
                red = (scores["ILRSA"] - scores[name]) / max(scores["ILRSA"], eps) * 100.0
                print("| {} | {} | {:.2f} | {} | {:.3f} | {:.3f} | {:.6f} | {:.6f} | {:.6f} | {:.6f} | {:.2f} |".format(
                    setting, size, w, name,
                    m["ACT"], m["AMS"], m["DelayNorm"],
                    m["FragMetric"], m["FragNorm"],
                    scores[name], red
                ))

if __name__ == "__main__":
    main()
