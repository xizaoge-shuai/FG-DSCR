#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import argparse
import inspect
import importlib.util
from pathlib import Path


def load_fg_module():
    p = Path("scripts/fg_dscr.py")
    spec = importlib.util.spec_from_file_location("fg_dscr_mod", str(p))
    mod = importlib.util.module_from_spec(spec)

    # Required for dataclass/type-resolution during dynamic import.
    # Without this, @dataclass may fail with:
    # AttributeError: 'NoneType' object has no attribute '__dict__'
    sys.modules[spec.name] = mod

    spec.loader.exec_module(mod)
    return mod


def find_scheduler_class(mod):
    for name, obj in vars(mod).items():
        if inspect.isclass(obj) and hasattr(obj, "potential_components"):
            return obj
    raise RuntimeError("Cannot find scheduler class with potential_components() in scripts/fg_dscr.py")


def instantiate_scheduler(cls, containers, nodes, layer_sizes, args):
    sig = inspect.signature(cls)

    common = {
        "containers": containers,
        "nodes": nodes,
        "layer_sizes": layer_sizes,
        "layer_sizes_mb": layer_sizes,
        "lambda_cong": args.lambda_cong,
        "lambda_frag": args.lambda_frag,
        "lambda_aff": args.lambda_aff,
        "lambda_task_load": args.lambda_task_load,
        "task_load_factor": args.task_load_factor,
        "task_load_power": args.task_load_power,
        "beam_width": args.beam,
        "beam": args.beam,
        "k_pin": args.k_pin,
        "cache_policy": args.cache_policy,
        "order_policy": args.order_policy,
        "greedy_load_factor": args.greedy_load_factor,
        "alpha_obj": args.alpha_obj,
    }

    kwargs = {}
    required_missing = []

    for name, p in sig.parameters.items():
        if name == "self":
            continue
        if name in common:
            kwargs[name] = common[name]
        elif p.default is inspect._empty and p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL):
            required_missing.append(name)

    if required_missing:
        # fallback: try positional core arguments
        extra_kwargs = {k: v for k, v in kwargs.items() if k not in ["containers", "nodes", "layer_sizes", "layer_sizes_mb"]}
        try:
            return cls(containers, nodes, layer_sizes, **extra_kwargs)
        except Exception as e:
            raise RuntimeError(f"Cannot instantiate scheduler. missing={required_missing}, signature={sig}, error={e}")

    return cls(**kwargs)


def get_summary(obj):
    if isinstance(obj, dict) and "summary" in obj and isinstance(obj["summary"], dict):
        return obj["summary"]
    return obj if isinstance(obj, dict) else {}


def extract_assignment(obj):
    """
    Extract assignment as cid -> eid.

    Different result files may store placement in different formats:
    1) cid -> eid
       {"c001": "edge-1", ...}

    2) eid -> [cid, cid, ...]
       {"edge-1": ["c001", "c002"], ...}

    3) ordered_queues / node_details
       {"ordered_queues": {"edge-1": ["c001", ...]}}
    """
    if not isinstance(obj, dict):
        return None

    def is_node_id(x):
        x = str(x)
        return x.startswith("edge-") or x.startswith("node-") or x.startswith("e")

    def is_container_id(x):
        x = str(x)
        return x.startswith("c")

    def item_to_cid(item):
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            return (
                item.get("cid")
                or item.get("container_id")
                or item.get("container")
                or item.get("id")
                or item.get("name")
            )
        return None

    def invert_node_to_items(d):
        if not isinstance(d, dict):
            return None

        out = {}

        for eid, value in d.items():
            # eid -> [cid, cid, ...]
            if isinstance(value, list):
                for item in value:
                    cid = item_to_cid(item)
                    if cid is not None:
                        out[str(cid)] = str(eid)

            # eid -> {"containers": [...]} or {"queue": [...]}
            elif isinstance(value, dict):
                for key in [
                    "containers",
                    "container_ids",
                    "assigned_containers",
                    "queue",
                    "ordered_queue",
                    "ordered_containers",
                ]:
                    q = value.get(key)
                    if isinstance(q, list):
                        for item in q:
                            cid = item_to_cid(item)
                            if cid is not None:
                                out[str(cid)] = str(eid)

        return out if out else None

    # 1. Prefer ordered_queues if available.
    oq = obj.get("ordered_queues")
    inv = invert_node_to_items(oq)
    if inv:
        return inv

    # 2. Try node_details.
    nd = obj.get("node_details")
    inv = invert_node_to_items(nd)
    if inv:
        return inv

    if isinstance(nd, list):
        out = {}
        for info in nd:
            if not isinstance(info, dict):
                continue

            eid = (
                info.get("eid")
                or info.get("node_id")
                or info.get("node")
                or info.get("id")
                or info.get("name")
            )
            if eid is None:
                continue

            for key in [
                "containers",
                "container_ids",
                "assigned_containers",
                "queue",
                "ordered_queue",
                "ordered_containers",
            ]:
                q = info.get(key)
                if isinstance(q, list):
                    for item in q:
                        cid = item_to_cid(item)
                        if cid is not None:
                            out[str(cid)] = str(eid)
        if out:
            return out

    # 3. Try common top-level placement keys.
    for k in [
        "assignment",
        "assignment_map",
        "placement",
        "container_to_node",
        "node_assignment",
    ]:
        v = obj.get(k)
        if not isinstance(v, dict):
            continue

        # Case A: eid -> [cid, cid, ...]
        inv = invert_node_to_items(v)
        if inv:
            return inv

        # Case B: cid -> eid or eid -> cid
        if all(not isinstance(x, (list, dict)) for x in v.values()):
            keys = list(v.keys())
            vals = list(v.values())

            # cid -> eid
            if any(is_container_id(x) for x in keys) and any(is_node_id(x) for x in vals):
                return {str(cid): str(eid) for cid, eid in v.items()}

            # eid -> cid
            if any(is_node_id(x) for x in keys) and any(is_container_id(x) for x in vals):
                return {str(cid): str(eid) for eid, cid in v.items()}

            # fallback: assume cid -> eid
            return {str(cid): str(eid) for cid, eid in v.items()}

    # 4. Try container_metrics if it stores node information.
    cm = obj.get("container_metrics")
    if isinstance(cm, dict):
        out = {}
        for cid, info in cm.items():
            if not isinstance(info, dict):
                continue
            eid = (
                info.get("eid")
                or info.get("node_id")
                or info.get("node")
                or info.get("assigned_node")
            )
            if eid is not None:
                out[str(cid)] = str(eid)
        if out:
            return out

    return None


def canonicalize_assignment(assignment, containers, nodes):
    """
    Convert extracted assignment into keys compatible with fg_dscr.Scheduler.

    Some result files store container ids as c183, while the current case loader
    may use a different internal key. This function builds aliases from both the
    dict keys and the Container.cid / Node.eid attributes, then maps the result
    assignment back to the current case's ids.
    """
    container_alias = {}
    for k, c in containers.items():
        aliases = {str(k)}
        cid_attr = getattr(c, "cid", None)
        if cid_attr is not None:
            aliases.add(str(cid_attr))

        # numeric aliases: c183, c0183, c00183, etc.
        for x in list(aliases):
            m = re.match(r"^c0*(\d+)$", str(x))
            if m:
                num = int(m.group(1))
                aliases.update({
                    f"c{num}",
                    f"c{num:03d}",
                    f"c{num:04d}",
                    f"c{num:05d}",
                })

        for a in aliases:
            container_alias[str(a)] = k

    node_alias = {}
    for k, node in nodes.items():
        aliases = {str(k)}
        eid_attr = getattr(node, "eid", None)
        if eid_attr is not None:
            aliases.add(str(eid_attr))
        name_attr = getattr(node, "name", None)
        if name_attr is not None:
            aliases.add(str(name_attr))
        for a in aliases:
            node_alias[str(a)] = k

    normalized = {}
    bad_cids = []
    bad_eids = []

    for cid, eid in assignment.items():
        cid_key = container_alias.get(str(cid))
        eid_key = node_alias.get(str(eid))

        if cid_key is None:
            bad_cids.append(str(cid))
            continue

        if eid_key is None:
            bad_eids.append(str(eid))
            # fallback: keep original eid, but usually this should not happen
            eid_key = str(eid)

        normalized[cid_key] = eid_key

    missing = [str(k) for k in containers.keys() if k not in normalized]

    return normalized, bad_cids, bad_eids, missing

def sum_components(comps):
    keys = [
        "cong_term",
        "frag_term",
        "aff_term",
        "task_load_term",
        "raw_cong_term",
        "raw_frag_term",
        "raw_aff_term",
        "raw_load_term",
        "cong_norm",
        "frag_norm",
        "aff_norm",
        "load_norm",
    ]
    out = {k: 0.0 for k in keys}

    for _, c in comps.items():
        if not isinstance(c, dict):
            continue
        for k in keys:
            out[k] += float(c.get(k, 0.0))

    return out


def load_case_with_fg(mod, path):
    if hasattr(mod, "load_case"):
        ret = mod.load_case(path)
        if isinstance(ret, tuple) and len(ret) >= 3:
            return ret[0], ret[1], ret[2]
    raise RuntimeError("scripts/fg_dscr.py does not expose compatible load_case()")


def load_obj_value(summary):
    if "objective" in summary:
        return float(summary["objective"])
    if "Obj" in summary:
        return float(summary["Obj"])
    if "obj" in summary:
        return float(summary["obj"])
    act = float(summary.get("ACT", summary.get("act", 0.0)))
    ams = float(summary.get("AMS", summary.get("ams", 0.0)))
    return 0.5 * act + 0.5 * ams


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", required=True)

    ap.add_argument("--lambda-cong", type=float, default=1.0)
    ap.add_argument("--lambda-frag", type=float, default=0.1)
    ap.add_argument("--lambda-aff", type=float, default=0.2)
    ap.add_argument("--lambda-task-load", type=float, default=0.03)

    ap.add_argument("--task-load-factor", type=float, default=0.9)
    ap.add_argument("--task-load-power", type=float, default=2.0)
    ap.add_argument("--beam", type=int, default=1)
    ap.add_argument("--k-pin", type=int, default=6)
    ap.add_argument("--cache-policy", default="pgdsf")
    ap.add_argument("--order-policy", default="dynamic_state")
    ap.add_argument("--greedy-load-factor", type=float, default=0.9)
    ap.add_argument("--alpha-obj", type=float, default=0.5)

    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)

    mod = load_fg_module()
    cls = find_scheduler_class(mod)

    reqs = [200,300,400,500,600,700,800,900,1000]

    methods = [
        ("ILR-SA", "results/drtp/final_exp/phase1_baselines88_req200_1000_lfrag01/ilrsa/ilrsa_{n}.json"),
        ("LRScheduler-inspired", "results/drtp/final_exp/phase1_baselines88_req200_1000_lfrag01/lrscheduler_source/lrs_{n}.json"),
        ("GAHRL-inspired", "results/drtp/final_exp/phase1_baselines88_req200_1000_lfrag01/gahrl/gahrl_{n}.json"),
        ("ORR-inspired", "results/drtp/final_exp/phase1_baselines88_req200_1000_lfrag01/orr/orr_{n}.json"),
        ("LASA-paper-reimpl", "results/drtp/final_exp/phase1_baselines88_req200_1000_lfrag01/lasa/lasa_{n}.json"),
        ("FG-DSCR-GC", "results/drtp/final_exp/phase1_potential_history88_req200_1000_lfrag01/FG_DSCR_GC/fg_phase1_{n}.json"),
    ]

    rows = []

    for n in reqs:
        case_path = f"cases/drtp_cache_sweep_88/drtp_img88_cache_1024mb_{n}.json"
        if not os.path.exists(case_path):
            print("[MISS CASE]", case_path)
            continue

        containers, nodes, layer_sizes = load_case_with_fg(mod, case_path)

        for method, tmpl in methods:
            res_path = tmpl.format(n=n)
            if not os.path.exists(res_path):
                print("[MISS RESULT]", method, n, res_path)
                continue

            obj = json.load(open(res_path, "r", encoding="utf-8"))
            summary = get_summary(obj)
            assignment = extract_assignment(obj)

            if not assignment:
                print("[NO ASSIGNMENT]", method, n, res_path)
                continue

            scheduler = instantiate_scheduler(cls, containers, nodes, layer_sizes, args)

            assignment, bad_cids, bad_eids, missing_cids = canonicalize_assignment(
                assignment, scheduler.containers, scheduler.nodes
            )

            if bad_cids:
                print(f"[WARN BAD_CIDS] method={method} n={n} count={len(bad_cids)} sample={bad_cids[:10]}")
            if bad_eids:
                print(f"[WARN BAD_EIDS] method={method} n={n} count={len(bad_eids)} sample={bad_eids[:10]}")
            if missing_cids:
                print(f"[WARN MISSING_ASSIGNED] method={method} n={n} count={len(missing_cids)} sample={missing_cids[:10]}")

            if not assignment:
                print("[EMPTY ASSIGNMENT AFTER CANONICALIZE]", method, n, res_path)
                continue

            potential, comps = scheduler.potential_components(assignment)
            sums = sum_components(comps)

            rows.append({
                "requests": n,
                "method": method,
                "Obj": load_obj_value(summary),
                "ACT": float(summary.get("ACT", summary.get("act", 0.0))),
                "AMS": float(summary.get("AMS", summary.get("ams", 0.0))),
                "downloaded_mb": int(float(summary.get("downloaded_mb", 0))),
                "reused_mb": int(float(summary.get("reused_mb", 0))),
                "reuse_rate": float(summary.get("reuse_rate", 0.0)),
                "potential": float(potential),
                **sums,
            })

    # detailed table
    detailed = os.path.join(args.outdir, "summary_phase1_unified_potential_detailed.md")
    with open(detailed, "w", encoding="utf-8") as f:
        f.write("| requests | method | Obj | potential | cong_term | frag_term | aff_term | task_load_term | raw_cong_term | raw_frag_term | raw_aff_term | raw_load_term |\n")
        f.write("|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n")
        for r in sorted(rows, key=lambda x: (x["requests"], x["method"])):
            f.write(
                "| {requests} | {method} | {Obj:.3f} | {potential:.3f} | {cong_term:.3f} | {frag_term:.3f} | {aff_term:.3f} | {task_load_term:.3f} | {raw_cong_term:.3f} | {raw_frag_term:.6f} | {raw_aff_term:.3f} | {raw_load_term:.6f} |\n".format(**r)
            )

    # potential line table
    line = os.path.join(args.outdir, "summary_phase1_phi_lines.md")
    with open(line, "w", encoding="utf-8") as f:
        f.write("| method | " + " | ".join(map(str, reqs)) + " |\n")
        f.write("|---|" + "|".join(["---:"] * len(reqs)) + "|\n")
        for method, _ in methods:
            vals = []
            for n in reqs:
                hit = [r for r in rows if r["method"] == method and r["requests"] == n]
                vals.append(f"{hit[0]['potential']:.3f}" if hit else "MISSING")
            f.write("| {} | {} |\n".format(method, " | ".join(vals)))

    # relative potential table: per request divided by best/lower-bound among methods
    ratio = os.path.join(args.outdir, "summary_phase1_phi_ratio_lines.md")
    with open(ratio, "w", encoding="utf-8") as f:
        f.write("| method | " + " | ".join(map(str, reqs)) + " |\n")
        f.write("|---|" + "|".join(["---:"] * len(reqs)) + "|\n")
        for method, _ in methods:
            vals = []
            for n in reqs:
                group = [r for r in rows if r["requests"] == n]
                hit = [r for r in group if r["method"] == method]
                if not group or not hit:
                    vals.append("MISSING")
                else:
                    best = min(r["potential"] for r in group)
                    vals.append(f"{hit[0]['potential'] / best:.4f}")
            f.write("| {} | {} |\n".format(method, " | ".join(vals)))

    print("[OK]", detailed)
    print("[OK]", line)
    print("[OK]", ratio)
    print(open(line, "r", encoding="utf-8").read())


if __name__ == "__main__":
    main()
