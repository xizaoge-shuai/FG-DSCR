import os
import re
import json
import glob
import argparse
from collections import defaultdict

EPS = 1e-9

# 如果某个目录和你本地实际名字不一样，就改这里。
METHOD_DIRS = {
    "FG-DSCR-GC": [
        "results/drtp/final_exp/cacheonly_dg_0_1024_fg",
    ],
    "GAHRL-inspired": [
        "results/drtp/final_exp/cacheonly_dg_0_1024_gahrl",
    ],
    "ORR-inspired": [
        "results/drtp/final_exp/cacheonly_dg_0_1024_orr",
    ],
    "ILR-SA": [
        "results/drtp/final_exp/cacheonly_dg_0_1024_ilrsa",
    ],
    "LASA-reimpl": [
        "results/drtp/final_exp/cacheonly_dg_0_1024_lasa",
    ],
    "OnlineNorm-trial12": [
        "results/drtp/final_exp/cacheonly_fg_online_norm_trial12_full81",
    ],
    "Joint-Jsys-trial11": [
        "results/drtp/optuna/fg_phase1_joint_jsys_full81_runs/trial_0011",
    ],
    "Joint-Jsys-trial6": [
        "results/drtp/optuna/fg_phase1_joint_jsys_full81_runs/trial_0006",
    ],
}

def first_existing(paths):
    for p in paths:
        if os.path.isdir(p):
            return p
    return None

def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def summary_of(obj):
    return obj.get("summary", obj)

def get_metric(summary, keys, default=0.0):
    for k in keys:
        if k in summary and summary[k] is not None:
            try:
                return float(summary[k])
            except Exception:
                pass
    return default

def avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None

def parse_key_from_name(name):
    """
    Parse cache/request from filename.
    Supports names like:
    drtp_img88_cacheonly_1024mb_900.json
    xxx_1024mb_900.json
    """
    m = re.search(r"_(\d+)mb_(\d+)\.json$", name)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return None

def build_index(root):
    """
    Build both exact-name and (cache, req) index.
    """
    exact = {}
    by_key = {}

    for p in glob.glob(os.path.join(root, "**", "*.json"), recursive=True):
        b = os.path.basename(p)

        # skip optuna side files
        if b in {"params.json", "metrics.json", "metrics_jsys.json", "best_params.json"}:
            continue

        exact[b] = p
        k = parse_key_from_name(b)
        if k is not None:
            by_key[k] = p

    return exact, by_key

def req_of_case(case_path):
    k = parse_key_from_name(os.path.basename(case_path))
    return k[1] if k else None

def case_key(case_path):
    return parse_key_from_name(os.path.basename(case_path))

def final_phase1(obj):
    ph = obj.get("phase1_history", [])
    return ph[-1] if ph else {}

def as_items(x):
    if isinstance(x, dict):
        return list(x.items())
    if isinstance(x, list):
        return [(str(i), v) for i, v in enumerate(x)]
    return []

def pick_id(item, fallback):
    if not isinstance(item, dict):
        return fallback
    for k in ["id", "eid", "name", "node_id", "server_id", "edge_id", "container_id", "cid"]:
        if k in item:
            return str(item[k])
    return fallback

def pick_num(d, keys):
    if not isinstance(d, dict):
        return None

    # top-level fields
    for k in keys:
        if k in d and d[k] is not None:
            try:
                return float(d[k])
            except Exception:
                pass

    # nested resource fields, e.g., {"resources": {"cpu": 24, "mem": 64, "disk": 128}}
    for rk in ["resources", "resource", "res"]:
        r = d.get(rk)
        if isinstance(r, dict):
            for k in keys:
                if k in r and r[k] is not None:
                    try:
                        return float(r[k])
                    except Exception:
                        pass

    return None

def get_case_nodes(case_obj):
    for key in ["nodes", "edge_nodes", "servers", "edge_servers"]:
        if key in case_obj:
            nodes = {}
            for fallback, item in as_items(case_obj[key]):
                nid = pick_id(item, str(fallback))
                nodes[nid] = item
            return nodes
    return {}

def get_case_containers(case_obj):
    for key in ["containers", "tasks", "requests", "workloads"]:
        if key in case_obj:
            cs = {}
            for fallback, item in as_items(case_obj[key]):
                cid = pick_id(item, str(fallback))
                cs[cid] = item
            return cs
    return {}

def normalize_assignment(assign):
    """
    Returns node_id -> [container_id].
    Supports node->containers and container->node.
    """
    if not isinstance(assign, dict):
        return {}

    if all(isinstance(v, list) for v in assign.values()):
        return {str(k): [str(x) for x in v] for k, v in assign.items()}

    inv = defaultdict(list)
    for c, n in assign.items():
        if isinstance(n, str):
            inv[str(n)].append(str(c))
        elif isinstance(n, dict):
            for nk in ["node", "node_id", "edge", "edge_id", "server"]:
                if nk in n:
                    inv[str(n[nk])].append(str(c))
                    break
    return dict(inv)

def pressure_frag_from_case(case_path, out_obj):
    """
    Paper-style fragmentation:
    P_v^r = usage/capacity
    Frag_v = mean_r |P_v^r - mean(P_v)|
    Frag = average_v Frag_v
    """
    try:
        case_obj = read_json(case_path)
    except Exception:
        return None

    nodes = get_case_nodes(case_obj)
    containers = get_case_containers(case_obj)
    assign = normalize_assignment(out_obj.get("assignment", {}))

    if not nodes or not containers or not assign:
        return None

    cpu_cap_keys = ["cpu_capacity", "cpu_cap", "capacity_cpu", "cpu"]
    mem_cap_keys = ["mem_capacity", "memory_capacity", "mem_cap", "memory_cap", "mem", "memory"]
    disk_cap_keys = ["disk_capacity", "storage_capacity", "disk_cap", "storage_cap", "disk", "storage"]

    cpu_req_keys = ["cpu", "cpu_req", "cpu_demand", "cpu_need"]
    mem_req_keys = ["mem", "memory", "mem_req", "memory_req", "mem_mb", "memory_mb"]
    disk_req_keys = ["disk", "storage", "disk_req", "storage_req", "disk_mb", "storage_mb"]

    frag_vals = []

    for nid, cids in assign.items():
        node = nodes.get(str(nid))
        if node is None:
            continue

        caps = [
            pick_num(node, cpu_cap_keys),
            pick_num(node, mem_cap_keys),
            pick_num(node, disk_cap_keys),
        ]
        if any(x is None or x <= 0 for x in caps):
            continue

        uses = [0.0, 0.0, 0.0]
        for cid in cids:
            c = containers.get(str(cid))
            if c is None:
                continue
            vals = [
                pick_num(c, cpu_req_keys),
                pick_num(c, mem_req_keys),
                pick_num(c, disk_req_keys),
            ]
            for i, v in enumerate(vals):
                if v is not None:
                    uses[i] += v

        pressures = [uses[i] / max(caps[i], EPS) for i in range(3)]
        mean_p = sum(pressures) / 3.0
        frag_v = sum(abs(p - mean_p) for p in pressures) / 3.0
        frag_vals.append(frag_v)

    if frag_vals:
        return sum(frag_vals) / len(frag_vals)
    return None

def fallback_frag(out_obj):
    ph = final_phase1(out_obj)

    vals = []
    for k in ["fragmented_cpu", "fragmented_mem", "fragmented_disk"]:
        if k in ph:
            try:
                vals.append(float(ph[k]))
            except Exception:
                pass
    if vals and any(abs(v) > EPS for v in vals):
        return sum(abs(v) for v in vals) / len(vals)

    comps = ph.get("node_components", {})
    fvals = []
    if isinstance(comps, dict):
        for c in comps.values():
            if isinstance(c, dict):
                if "Frag_j" in c:
                    fvals.append(float(c["Frag_j"]))
                elif "raw_frag_term" in c:
                    fvals.append(float(c["raw_frag_term"]))
    if fvals:
        return sum(fvals) / len(fvals)

    return None

def load_var_from_output(out_obj):
    # Fair for all methods: compute load variance from assignment.
    assign = normalize_assignment(out_obj.get("assignment", {}))
    if assign:
        counts = [len(v) for v in assign.values()]
        if counts:
            m = sum(counts) / len(counts)
            return sum((x - m) ** 2 for x in counts) / len(counts)

    # Fallback only when assignment is unavailable.
    ph = final_phase1(out_obj)
    if "load_var" in ph:
        return float(ph["load_var"])

    s = summary_of(out_obj)
    return float(s.get("load_var", 0.0))

def extract_metrics(case_path, out_path):
    out_obj = read_json(out_path)
    s = summary_of(out_obj)

    act = get_metric(s, ["ACT", "act", "avg_completion_time"])
    ams = get_metric(s, ["AMS", "ams", "avg_makespan"])
    old_obj = get_metric(s, ["objective", "obj", "Obj"], 0.5 * act + 0.5 * ams)
    downloaded = get_metric(s, ["downloaded_mb", "download_mb", "total_downloaded_mb"])
    reuse = get_metric(s, ["reuse_rate"])

    frag = pressure_frag_from_case(case_path, out_obj)
    if frag is None:
        frag = fallback_frag(out_obj)

    if frag is None:
        raise RuntimeError("fragmentation metric is unavailable for this output")

    load_var = load_var_from_output(out_obj)

    return {
        "ACT": act,
        "AMS": ams,
        "old_obj": old_obj,
        "downloaded": downloaded,
        "reuse": reuse,
        "frag": frag,
        "load_var": load_var,
    }

def collect_method(method_name, case_list, exact, by_key, switch_threshold=None, fg_index=None, on_index=None):
    vals = defaultdict(list)
    missing = []

    for case in case_list:
        b = os.path.basename(case)
        k = case_key(case)
        req = req_of_case(case)

        if switch_threshold is not None:
            # OnlineNorm switch: low-load uses FG, high-load uses OnlineNorm-trial12.
            if req is not None and req >= switch_threshold:
                ex, bk = on_index
            else:
                ex, bk = fg_index
        else:
            ex, bk = exact, by_key

        p = ex.get(b)
        if p is None and k is not None:
            p = bk.get(k)

        if p is None or not os.path.exists(p):
            missing.append(b)
            continue

        try:
            m = extract_metrics(case, p)
        except Exception as e:
            missing.append(b + f" [ERR {e}]")
            continue

        for kk, vv in m.items():
            vals[kk].append(vv)

    return {
        "cases": len(vals["old_obj"]),
        "missing": missing,
        "ACT": avg(vals["ACT"]),
        "AMS": avg(vals["AMS"]),
        "old_obj": avg(vals["old_obj"]),
        "downloaded": avg(vals["downloaded"]),
        "reuse": avg(vals["reuse"]),
        "frag": avg(vals["frag"]),
        "load_var": avg(vals["load_var"]),
    }

def calc_jsys(row, base, weights):
    ratios = {
        "ACT": row["ACT"] / max(base["ACT"], EPS),
        "AMS": row["AMS"] / max(base["AMS"], EPS),
        "frag": row["frag"] / max(base["frag"], EPS),
        "downloaded": row["downloaded"] / max(base["downloaded"], EPS),
        "load_var": row["load_var"] / max(base["load_var"], EPS),
    }

    J = (
        weights["ACT"] * ratios["ACT"]
        + weights["AMS"] * ratios["AMS"]
        + weights["frag"] * ratios["frag"]
        + weights["downloaded"] * ratios["downloaded"]
        + weights["load_var"] * ratios["load_var"]
    )
    return J, ratios

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case-list", default="/tmp/cacheonly_full81_cases.txt")
    ap.add_argument("--switch-threshold", type=int, default=700)
    ap.add_argument("--w-act", type=float, default=0.30)
    ap.add_argument("--w-ams", type=float, default=0.20)
    ap.add_argument("--w-frag", type=float, default=0.20)
    ap.add_argument("--w-download", type=float, default=0.20)
    ap.add_argument("--w-load", type=float, default=0.10)
    args = ap.parse_args()

    case_list = [x.strip() for x in open(args.case_list, encoding="utf-8") if x.strip()]
    weights = {
        "ACT": args.w_act,
        "AMS": args.w_ams,
        "frag": args.w_frag,
        "downloaded": args.w_download,
        "load_var": args.w_load,
    }

    print("# J_sys full81 evaluation")
    print()
    print("weights:", weights)
    print("case_list:", args.case_list, "cases:", len(case_list))
    print()

    resolved = {}
    for name, paths in METHOD_DIRS.items():
        root = first_existing(paths)
        resolved[name] = root

    print("# Resolved directories")
    for name, root in resolved.items():
        print(f"{name}: {root if root else 'MISSING'}")
    print()

    if resolved["FG-DSCR-GC"] is None:
        raise SystemExit("FG-DSCR-GC directory missing. Please edit METHOD_DIRS.")

    indexes = {}
    for name, root in resolved.items():
        if root:
            indexes[name] = build_index(root)

    rows = {}

    for name in ["FG-DSCR-GC", "GAHRL-inspired", "ORR-inspired", "ILR-SA", "LASA-reimpl", "OnlineNorm-switch-req>=700", "Joint-Jsys-trial11", "Joint-Jsys-trial6"]:
        root = resolved.get(name)
        if not root:
            rows[name] = None
            continue
        exact, by_key = indexes[name]
        rows[name] = collect_method(name, case_list, exact, by_key)

    if resolved.get("OnlineNorm-trial12"):
        rows[f"OnlineNorm-switch-req>={args.switch_threshold}"] = collect_method(
            f"OnlineNorm-switch-req>={args.switch_threshold}",
            case_list,
            None,
            None,
            switch_threshold=args.switch_threshold,
            fg_index=indexes["FG-DSCR-GC"],
            on_index=indexes["OnlineNorm-trial12"],
        )
    else:
        rows[f"OnlineNorm-switch-req>={args.switch_threshold}"] = None

    base = rows["FG-DSCR-GC"]

    print("| method | cases | missing | J_sys | J_sys_impr | ACT_ratio | AMS_ratio | frag_ratio | download_ratio | load_ratio | old_obj | ACT | AMS | frag | downloaded | reuse | load_var |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")

    for name, r in rows.items():
        if r is None or not r["cases"]:
            print(f"| {name} | 0 | MISSING | NA | NA | NA | NA | NA | NA | NA | NA | NA | NA | NA | NA | NA | NA |")
            continue

        J, ratios = calc_jsys(r, base, weights)
        impr = (1.0 - J) * 100.0

        print("| {} | {} | {} | {:.6f} | {:.2f}% | {:.6f} | {:.6f} | {:.6f} | {:.6f} | {:.6f} | {:.3f} | {:.3f} | {:.3f} | {:.6f} | {:.3f} | {:.6f} | {:.3f} |".format(
            name,
            r["cases"],
            len(r["missing"]),
            J,
            impr,
            ratios["ACT"],
            ratios["AMS"],
            ratios["frag"],
            ratios["downloaded"],
            ratios["load_var"],
            r["old_obj"],
            r["ACT"],
            r["AMS"],
            r["frag"],
            r["downloaded"],
            r["reuse"],
            r["load_var"],
        ))

    print()
    print("# Missing detail")
    for name, r in rows.items():
        if r and r["missing"]:
            print(name, "missing", len(r["missing"]), "examples:", r["missing"][:10])

if __name__ == "__main__":
    main()
