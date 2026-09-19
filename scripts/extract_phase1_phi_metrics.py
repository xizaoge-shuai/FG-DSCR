#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import argparse
import importlib.util
from pathlib import Path


REQS = [200,300,400,500,600,700,800,900,1000]

METHODS = [
    ("ILR-SA", "results/drtp/final_exp/phase1_baselines88_req200_1000_lfrag01/ilrsa/ilrsa_{n}.json"),
    ("LRScheduler-inspired", "results/drtp/final_exp/phase1_baselines88_req200_1000_lfrag01/lrscheduler_source/lrs_{n}.json"),
    ("GAHRL-inspired", "results/drtp/final_exp/phase1_baselines88_req200_1000_lfrag01/gahrl/gahrl_{n}.json"),
    ("ORR-inspired", "results/drtp/final_exp/phase1_baselines88_req200_1000_lfrag01/orr/orr_{n}.json"),
    ("LASA-paper-reimpl", "results/drtp/final_exp/phase1_baselines88_req200_1000_lfrag01/lasa/lasa_{n}.json"),
    ("FG-DSCR-GC", "results/drtp/final_exp/phase1_potential_history88_req200_1000_lfrag01/FG_DSCR_GC/fg_phase1_{n}.json"),
]


def import_fg():
    p = Path("scripts/fg_dscr.py")
    spec = importlib.util.spec_from_file_location("fg_dscr_mod", str(p))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def get_id(x, keys):
    if isinstance(x, dict):
        for k in keys:
            if k in x:
                return str(x[k])
        return None
    for k in keys:
        if hasattr(x, k):
            return str(getattr(x, k))
    return None


def get_layers(x):
    if isinstance(x, dict):
        return x.get("layers", [])
    return getattr(x, "layers", [])


def rebuild_container_dict(containers):
    """
    Make scheduler.containers addressable by c001/c002/... ids.
    """
    if isinstance(containers, dict):
        values = list(containers.values())
    else:
        values = list(containers)

    out = {}
    for i, c in enumerate(values):
        cid = get_id(c, ["cid", "container_id", "id", "name"])
        if cid is None:
            cid = f"c{i+1:03d}"
        out[str(cid)] = c
    return out


def rebuild_node_dict(nodes):
    """
    Make scheduler.nodes addressable by edge-1/edge-2/... ids.
    """
    if isinstance(nodes, dict):
        values = list(nodes.values())
    else:
        values = list(nodes)

    out = {}
    for i, nd in enumerate(values):
        eid = get_id(nd, ["eid", "node_id", "id", "name"])
        if eid is None:
            eid = f"edge-{i+1}"
        out[str(eid)] = nd
    return out


def extract_assignment(obj):
    """
    Return cid -> eid.
    Handles:
    - assignment: cid -> eid
    - assignment: eid -> [cid]
    - ordered_queues: eid -> [cid]
    - node_details: eid -> {containers: [...]}
    """

    def item_to_cid(item):
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            return item.get("cid") or item.get("container_id") or item.get("id") or item.get("name")
        return None

    def is_container(x):
        return str(x).startswith("c")

    def is_node(x):
        return str(x).startswith("edge") or str(x).startswith("node") or str(x).startswith("e")

    def invert_eid_to_list(d):
        if not isinstance(d, dict):
            return None
        out = {}
        for eid, v in d.items():
            if isinstance(v, list):
                for item in v:
                    cid = item_to_cid(item)
                    if cid is not None:
                        out[str(cid)] = str(eid)
            elif isinstance(v, dict):
                for key in ["containers", "container_ids", "assigned_containers", "queue", "ordered_queue", "ordered_containers"]:
                    q = v.get(key)
                    if isinstance(q, list):
                        for item in q:
                            cid = item_to_cid(item)
                            if cid is not None:
                                out[str(cid)] = str(eid)
        return out if out else None

    # preferred: ordered_queues
    for key in ["ordered_queues", "node_details"]:
        inv = invert_eid_to_list(obj.get(key))
        if inv:
            return inv

    # node_details as list
    nd = obj.get("node_details")
    if isinstance(nd, list):
        out = {}
        for info in nd:
            if not isinstance(info, dict):
                continue
            eid = info.get("eid") or info.get("node_id") or info.get("id") or info.get("name")
            if eid is None:
                continue
            for key in ["containers", "container_ids", "assigned_containers", "queue", "ordered_queue", "ordered_containers"]:
                q = info.get(key)
                if isinstance(q, list):
                    for item in q:
                        cid = item_to_cid(item)
                        if cid is not None:
                            out[str(cid)] = str(eid)
        if out:
            return out

    # direct assignment-like fields
    for key in ["assignment", "assignment_map", "placement", "container_to_node", "node_assignment"]:
        v = obj.get(key)
        if not isinstance(v, dict):
            continue

        inv = invert_eid_to_list(v)
        if inv:
            return inv

        keys = list(v.keys())
        vals = list(v.values())

        # cid -> eid
        if keys and is_container(keys[0]):
            return {str(cid): str(eid) for cid, eid in v.items()}

        # eid -> cid
        if keys and is_node(keys[0]) and vals and is_container(vals[0]):
            return {str(cid): str(eid) for eid, cid in v.items()}

    return None


def summary_of(obj):
    if isinstance(obj, dict) and isinstance(obj.get("summary"), dict):
        return obj["summary"]
    return obj


def get_obj(s):
    if "objective" in s:
        return float(s["objective"])
    if "Obj" in s:
        return float(s["Obj"])
    if "obj" in s:
        return float(s["obj"])
    return 0.5 * float(s.get("ACT", s.get("act", 0.0))) + 0.5 * float(s.get("AMS", s.get("ams", 0.0)))


def sum_components(comps):
    out = {
        "delay_term": 0.0,
        "frag_term": 0.0,
        "aff_reward_term": 0.0,
        "load_term": 0.0,
    }

    for _, c in comps.items():
        out["delay_term"] += float(c.get("cong_term", c.get("delay_term", 0.0)))
        out["frag_term"] += float(c.get("frag_term", 0.0))

        # fg_dscr.py 中 aff_term 是负数；表里按 reward 正数展示
        if "aff_reward_term" in c:
            out["aff_reward_term"] += float(c["aff_reward_term"])
        else:
            out["aff_reward_term"] += -float(c.get("aff_term", 0.0))

        out["load_term"] += float(c.get("task_load_term", c.get("load_term", 0.0)))

    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--lambda-cong", type=float, default=1.0)
    ap.add_argument("--lambda-frag", type=float, default=0.1)
    ap.add_argument("--lambda-aff", type=float, default=0.2)
    ap.add_argument("--lambda-task-load", type=float, default=0.03)
    ap.add_argument("--beam", type=int, default=1)
    ap.add_argument("--cache-policy", default="pgdsf")
    ap.add_argument("--order-policy", default="dynamic_state")
    ap.add_argument("--greedy-load-factor", type=float, default=0.9)
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)

    fg = import_fg()

    rows = []

    for n in REQS:
        case_path = f"cases/drtp_cache_sweep_88/drtp_img88_cache_1024mb_{n}.json"
        containers, nodes, layer_sizes = fg.load_case(case_path)

        for method, tmpl in METHODS:
            res_path = tmpl.format(n=n)
            if not os.path.exists(res_path):
                print("[MISS]", method, n, res_path)
                continue

            obj = json.load(open(res_path, "r", encoding="utf-8"))
            s = summary_of(obj)
            assignment = extract_assignment(obj)

            if not assignment:
                print("[NO_ASSIGNMENT]", method, n, res_path)
                continue

            scheduler = fg.FGDscrScheduler(
                containers=containers,
                nodes=nodes,
                layer_sizes_mb=layer_sizes,
                lambda_cong=args.lambda_cong,
                lambda_frag=args.lambda_frag,
                lambda_aff=args.lambda_aff,
                lambda_task_load=args.lambda_task_load,
                beam_width=args.beam,
                cache_policy=args.cache_policy,
                order_policy=args.order_policy,
                greedy_load_factor=args.greedy_load_factor,
            )

            # 关键：把 scheduler 内部 dict 重建成 c001/edge-1 可索引
            scheduler.containers = rebuild_container_dict(scheduler.containers)
            scheduler.nodes = rebuild_node_dict(scheduler.nodes)

            # 只保留当前 case 中存在的 container
            valid_cids = set(scheduler.containers.keys())
            valid_eids = set(scheduler.nodes.keys())

            assignment2 = {}
            bad = 0
            for cid, eid in assignment.items():
                cid = str(cid)
                eid = str(eid)
                if cid not in valid_cids:
                    bad += 1
                    continue
                if eid not in valid_eids:
                    continue
                assignment2[cid] = eid

            if not assignment2:
                print(f"[EMPTY_AFTER_FILTER] {method} n={n} bad={bad} valid_cids_sample={list(valid_cids)[:5]} assign_sample={list(assignment.items())[:5]}")
                continue

            phi_total, comps = scheduler.potential_components(assignment2)
            terms = sum_components(comps)

            row = {
                "requests": n,
                "method": method,
                "ACT": float(s.get("ACT", s.get("act", 0.0))),
                "AMS": float(s.get("AMS", s.get("ams", 0.0))),
                "Obj": get_obj(s),
                "downloaded_mb": int(float(s.get("downloaded_mb", 0))),
                "reused_mb": int(float(s.get("reused_mb", 0))),
                "reuse_rate": float(s.get("reuse_rate", 0.0)),
                "phi_total": float(phi_total),
                **terms,
            }
            rows.append(row)

    detailed = os.path.join(args.outdir, "summary_phase1_phi_metrics_detailed.md")
    with open(detailed, "w", encoding="utf-8") as f:
        f.write("| requests | method | Obj | ACT | AMS | downloaded_mb | reuse_rate | phi_total | delay_term | frag_term | aff_reward_term | load_term |\n")
        f.write("|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n")
        for r in sorted(rows, key=lambda x: (x["requests"], x["method"])):
            f.write("| {requests} | {method} | {Obj:.3f} | {ACT:.3f} | {AMS:.3f} | {downloaded_mb} | {reuse_rate:.6f} | {phi_total:.3f} | {delay_term:.3f} | {frag_term:.3f} | {aff_reward_term:.3f} | {load_term:.3f} |\n".format(**r))

    line = os.path.join(args.outdir, "summary_phase1_phi_lines.md")
    with open(line, "w", encoding="utf-8") as f:
        f.write("| method | " + " | ".join(map(str, REQS)) + " |\n")
        f.write("|---|" + "|".join(["---:"] * len(REQS)) + "|\n")
        for method, _ in METHODS:
            vals = []
            for n in REQS:
                hit = [r for r in rows if r["method"] == method and r["requests"] == n]
                vals.append(f"{hit[0]['phi_total']:.3f}" if hit else "MISSING")
            f.write("| {} | {} |\n".format(method, " | ".join(vals)))

    print("[OK]", detailed)
    print("[OK]", line)
    print(open(detailed, encoding="utf-8").read())
    print(open(line, encoding="utf-8").read())


if __name__ == "__main__":
    main()
