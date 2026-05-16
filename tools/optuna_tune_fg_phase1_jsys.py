import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from collections import defaultdict

import optuna

EPS = 1e-9

def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def safe_stem(path):
    return Path(path).stem

def summary_of(obj):
    return obj.get("summary", obj)

def get_metric(summary, keys, default=0.0):
    for k in keys:
        if k in summary and summary[k] is not None:
            return float(summary[k])
    return default

def final_phase1(obj):
    ph = obj.get("phase1_history", [])
    return ph[-1] if ph else {}

def avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None

def as_items(x):
    if isinstance(x, dict):
        return list(x.items())
    if isinstance(x, list):
        return [(str(i), v) for i, v in enumerate(x)]
    return []

def pick_id(item, fallback):
    if not isinstance(item, dict):
        return fallback
    for k in ["id", "name", "node_id", "server_id", "edge_id", "container_id", "cid"]:
        if k in item:
            return str(item[k])
    return fallback

def pick_num(d, keys):
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d and d[k] is not None:
            try:
                return float(d[k])
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
    Supports both node->containers and container->node formats.
    """
    if not isinstance(assign, dict):
        return {}

    # node -> list
    if all(isinstance(v, list) for v in assign.values()):
        return {str(k): [str(x) for x in v] for k, v in assign.items()}

    # container -> node
    inv = defaultdict(list)
    for c, n in assign.items():
        if isinstance(n, str):
            inv[n].append(str(c))
        elif isinstance(n, dict):
            for nk in ["node", "node_id", "edge", "edge_id", "server"]:
                if nk in n:
                    inv[str(n[nk])].append(str(c))
                    break
    return dict(inv)

def pressure_frag_from_case(case_path, out_obj):
    """
    Paper-style resource fragmentation:
    P_v^r = usage/capacity, Frag_v = mean_r |P_v^r - mean(P_v)|.
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

    # Prefer explicit fragmented resource fields if they are non-zero.
    vals = []
    for k in ["fragmented_cpu", "fragmented_mem", "fragmented_disk"]:
        if k in ph:
            vals.append(float(ph[k]))
    if vals and any(abs(v) > EPS for v in vals):
        return sum(abs(v) for v in vals) / len(vals)

    # Fallback: average Frag_j from phase1 node components.
    comps = ph.get("node_components", {})
    fvals = []
    for c in comps.values():
        if "Frag_j" in c:
            fvals.append(float(c["Frag_j"]))
        elif "raw_frag_term" in c:
            fvals.append(float(c["raw_frag_term"]))
    if fvals:
        return sum(fvals) / len(fvals)

    return 0.0

def extract_metrics(case_path, out_path):
    out_obj = read_json(out_path)
    s = summary_of(out_obj)

    act = get_metric(s, ["ACT", "act", "avg_completion_time"])
    ams = get_metric(s, ["AMS", "ams", "avg_makespan"])
    downloaded = get_metric(s, ["downloaded_mb", "download_mb", "total_downloaded_mb"])
    old_obj = get_metric(s, ["objective", "obj", "Obj"], 0.5 * act + 0.5 * ams)
    reuse = get_metric(s, ["reuse_rate"])

    ph = final_phase1(out_obj)
    load_var = float(ph.get("load_var", s.get("load_var", 0.0)))

    frag = pressure_frag_from_case(case_path, out_obj)
    if frag is None:
        frag = fallback_frag(out_obj)

    return {
        "ACT": act,
        "AMS": ams,
        "old_obj": old_obj,
        "downloaded": downloaded,
        "reuse": reuse,
        "frag": frag,
        "load_var": load_var,
    }

def collect_base_means(case_list, base_dir):
    vals = defaultdict(list)

    for case in case_list:
        name = os.path.basename(case)
        p = os.path.join(base_dir, name)
        if not os.path.exists(p):
            raise FileNotFoundError(f"missing base output: {p}")

        m = extract_metrics(case, p)
        for k, v in m.items():
            vals[k].append(v)

    return {k: avg(v) for k, v in vals.items()}

def jsys_from_metrics(metrics, base, weights):
    vals = defaultdict(list)
    for m in metrics:
        for k, v in m.items():
            vals[k].append(v)

    mean = {k: avg(v) for k, v in vals.items()}

    act_ratio = mean["ACT"] / max(base["ACT"], EPS)
    ams_ratio = mean["AMS"] / max(base["AMS"], EPS)
    frag_ratio = mean["frag"] / max(base["frag"], EPS)
    download_ratio = mean["downloaded"] / max(base["downloaded"], EPS)
    load_ratio = mean["load_var"] / max(base["load_var"], EPS)

    J = (
        weights["act"] * act_ratio
        + weights["ams"] * ams_ratio
        + weights["frag"] * frag_ratio
        + weights["download"] * download_ratio
        + weights["load"] * load_ratio
    )

    return J, {
        "avg_ACT": mean["ACT"],
        "avg_AMS": mean["AMS"],
        "avg_old_obj": mean["old_obj"],
        "avg_frag": mean["frag"],
        "avg_downloaded": mean["downloaded"],
        "avg_reuse": mean["reuse"],
        "avg_load_var": mean["load_var"],
        "ratio_ACT": act_ratio,
        "ratio_AMS": ams_ratio,
        "ratio_frag": frag_ratio,
        "ratio_downloaded": download_ratio,
        "ratio_load_var": load_ratio,
        "J_sys": J,
    }

def run_one(case, out, log, params):
    cmd = [
        "python3", "-u", "scripts/fg_dscr.py",
        "--case", case,
        "--out", out,
        "--beam", "1",
        "--lambda-cong", str(params["lambda_cong"]),
        "--lambda-frag", str(params["lambda_frag"]),
        "--lambda-aff", str(params["lambda_aff"]),
        "--lambda-task-load", str(params["lambda_task_load"]),
        "--cache-policy", "pgdsf",
        "--order-policy", "dynamic_state",
        "--greedy-load-factor", str(params["greedy_load_factor"]),
        "--algo-name", "FG-DSCR-GC-JSysOptuna",
        "--theta-cong-count", str(params["theta_cong_count"]),
        "--lambda-cache-core", "0.0",
        "--cache-core-ratio", "0.90",
        "--bw-gamma", "1.0",
        "--cache-bw-eta", "0.0",
        "--cache-bw-ref", "100",
    ]

    with open(log, "w", encoding="utf-8") as f:
        r = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, text=True)

    if r.returncode != 0:
        raise RuntimeError(f"fg_dscr failed: {case}, log={log}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case-list", required=True)
    ap.add_argument("--base-dir", required=True)
    ap.add_argument("--out-root", required=True)
    ap.add_argument("--study-name", required=True)
    ap.add_argument("--storage", required=True)
    ap.add_argument("--n-trials", type=int, default=20)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--timeout", type=int, default=None)

    ap.add_argument("--w-act", type=float, default=0.30)
    ap.add_argument("--w-ams", type=float, default=0.20)
    ap.add_argument("--w-frag", type=float, default=0.20)
    ap.add_argument("--w-download", type=float, default=0.20)
    ap.add_argument("--w-load", type=float, default=0.10)

    args = ap.parse_args()

    case_list = [x.strip() for x in open(args.case_list, encoding="utf-8") if x.strip()]
    os.makedirs(args.out_root, exist_ok=True)

    weights = {
        "act": args.w_act,
        "ams": args.w_ams,
        "frag": args.w_frag,
        "download": args.w_download,
        "load": args.w_load,
    }

    print("[INFO] cases =", len(case_list), flush=True)
    print("[INFO] weights =", weights, flush=True)
    print("[INFO] base_dir =", args.base_dir, flush=True)

    base = collect_base_means(case_list, args.base_dir)
    print("[BASE]", json.dumps(base, indent=2), flush=True)

    sampler = optuna.samplers.TPESampler(seed=args.seed)
    study = optuna.create_study(
        study_name=args.study_name,
        storage=args.storage,
        direction="minimize",
        sampler=sampler,
        load_if_exists=True,
    )

    def objective(trial):
        params = {
            "lambda_cong": trial.suggest_float("lambda_cong", 0.0, 2.0),
            "lambda_frag": trial.suggest_float("lambda_frag", 0.0, 2.0),
            "lambda_aff": trial.suggest_float("lambda_aff", 0.0, 2.0),
            "lambda_task_load": trial.suggest_float("lambda_task_load", 0.0, 1.0),
            "greedy_load_factor": trial.suggest_float("greedy_load_factor", 0.5, 2.5),
            "theta_cong_count": trial.suggest_float("theta_cong_count", 0.0, 1.0),
        }

        trial_dir = Path(args.out_root) / f"trial_{trial.number:04d}"
        log_dir = trial_dir / "logs"
        trial_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)

        with open(trial_dir / "params.json", "w", encoding="utf-8") as f:
            json.dump(params, f, indent=2)

        metrics = []

        for idx, case in enumerate(case_list):
            base_name = safe_stem(case)
            out = str(trial_dir / f"{base_name}.json")
            log = str(log_dir / f"log_{base_name}.txt")

            if not os.path.exists(out):
                print(f"[Trial {trial.number}] case {idx+1}/{len(case_list)} {base_name}", flush=True)
                run_one(case, out, log, params)

            m = extract_metrics(case, out)
            metrics.append(m)

            # Report interim J using currently finished cases.
            if len(metrics) >= 5:
                J_now, attrs_now = jsys_from_metrics(metrics, base, weights)
                trial.report(J_now, step=len(metrics))
                if trial.should_prune():
                    raise optuna.TrialPruned()

        J, attrs = jsys_from_metrics(metrics, base, weights)

        for k, v in attrs.items():
            trial.set_user_attr(k, float(v))

        with open(trial_dir / "metrics_jsys.json", "w", encoding="utf-8") as f:
            json.dump({"J_sys": J, "attrs": attrs, "params": params}, f, indent=2)

        print("[TRIAL DONE]", trial.number, "J_sys =", J, "attrs =", attrs, flush=True)
        return J

    study.optimize(objective, n_trials=args.n_trials, timeout=args.timeout)

    print("best_trial =", study.best_trial.number)
    print("best_value =", study.best_value)
    print("best_params =", json.dumps(study.best_params, indent=2))

if __name__ == "__main__":
    main()
