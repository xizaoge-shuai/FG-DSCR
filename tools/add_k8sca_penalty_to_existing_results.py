#!/usr/bin/env python3
import argparse
import json
import os
import glob
import re
from collections import defaultdict
from pathlib import Path

Q = ["cpu", "mem", "disk"]

def load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(obj, p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)

def get_cid(c, i):
    return str(c.get("cid", c.get("id", c.get("name", f"c{i}"))))

def get_eid(n, i):
    return str(n.get("eid", n.get("id", n.get("nid", n.get("name", f"edge-{i+1}")))))

def get_node_to_cids(res):
    """
    Prefer ordered_queues because CA/overflow replay needs a deterministic order.
    Fall back to assignment.
    Supports:
      node -> [cid, cid, ...]
      cid -> node
    """
    if isinstance(res.get("ordered_queues"), dict) and res["ordered_queues"]:
        oq = res["ordered_queues"]
        if all(isinstance(v, list) for v in oq.values()):
            return {str(k): [str(x) for x in v] for k, v in oq.items()}

    ass = res.get("assignment", {})
    if not isinstance(ass, dict):
        return {}

    if all(isinstance(v, list) for v in ass.values()):
        return {str(k): [str(x) for x in v] for k, v in ass.items()}

    node_to_cids = defaultdict(list)
    for cid, eid in ass.items():
        node_to_cids[str(eid)].append(str(cid))
    return dict(node_to_cids)

def compute_k8sca_failures(case, res):
    containers = {get_cid(c, i): c for i, c in enumerate(case.get("containers", []))}
    nodes = {get_eid(n, i): n for i, n in enumerate(case.get("nodes", []))}
    node_to_cids = get_node_to_cids(res)

    used = {eid: defaultdict(float) for eid in nodes}
    failed_cids = []
    feasible_cids = []
    unknown_cids = []

    for eid, cids in node_to_cids.items():
        if eid not in nodes:
            continue

        nres = nodes[eid].get("resources", {}) or {}

        for cid in cids:
            if cid not in containers:
                unknown_cids.append(cid)
                continue

            cres = containers[cid].get("resources", {}) or {}

            ok = True
            for q in Q:
                req = float(cres.get(q, 0.0))
                cap = float(nres.get(q, 0.0))
                cur = float(used[eid].get(q, 0.0))
                if cur + req > cap + 1e-9:
                    ok = False
                    break

            if ok:
                feasible_cids.append(cid)
                for q in Q:
                    used[eid][q] += float(cres.get(q, 0.0))
            else:
                failed_cids.append(cid)

    total = len(case.get("containers", []))
    return {
        "total": total,
        "edge_feasible": len(feasible_cids),
        "failed": len(failed_cids),
        "failed_cids": failed_cids,
        "unknown_cids": unknown_cids,
        "used_resource_after_filter": {
            eid: {q: float(v) for q, v in used[eid].items()}
            for eid in used
        },
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case-dir", required=True)
    ap.add_argument("--in-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--ca-penalty", type=float, default=1000.0)
    ap.add_argument("--pattern", default="drtp_img88_cacheonly_*.json")
    args = ap.parse_args()

    in_files = sorted(glob.glob(os.path.join(args.in_dir, args.pattern)))
    if not in_files:
        raise SystemExit(f"No result json found in {args.in_dir}")

    os.makedirs(args.out_dir, exist_ok=True)

    for rp in in_files:
        base = os.path.basename(rp)
        cp = os.path.join(args.case_dir, base)
        if not os.path.exists(cp):
            print(f"[SKIP missing case] {base}")
            continue

        case = load_json(cp)
        res = load_json(rp)

        if "summary" not in res:
            print(f"[SKIP missing summary] {base}")
            continue

        s = res["summary"]
        ca = compute_k8sca_failures(case, res)

        old_obj = float(s.get("objective", 0.0))
        total = int(ca["total"])
        failed = int(ca["failed"])
        ca_pen = args.ca_penalty * failed / max(total, 1)
        new_obj = old_obj + ca_pen

        # 不改原来的 ACT/AMS/download/reuse，只追加资源不足惩罚
        s["objective_without_ca_penalty"] = old_obj
        s["ca_penalty"] = ca_pen
        s["objective"] = new_obj

        s["num_containers"] = total
        s["num_edge_feasible_containers"] = int(ca["edge_feasible"])
        s["failed_deployments"] = failed
        s["fail_rate"] = failed / max(total, 1)

        s["ca_eval_mode"] = "posthoc_penalty_preserve_original_metrics"
        s["ca_fixed_penalty"] = args.ca_penalty

        res["k8sca_posthoc"] = ca

        out = os.path.join(args.out_dir, base)
        save_json(res, out)

        print(
            f"[OK] {base} total={total} edge_feasible={ca['edge_feasible']} "
            f"failed={failed} ca_penalty={ca_pen:.3f} "
            f"old_obj={old_obj:.3f} new_obj={new_obj:.3f}"
        )

if __name__ == "__main__":
    main()
