import os
import re
import json
import argparse
from collections import defaultdict

EPS = 1e-9

def load_summary_obj(path):
    obj = json.load(open(path, "r", encoding="utf-8"))
    summary = obj.get("summary", obj)
    return obj, summary

def get_metric(summary, keys, default=0.0):
    for k in keys:
        if k in summary and summary[k] is not None:
            return float(summary[k])
    return default

def final_phase1(obj):
    ph = obj.get("phase1_history", [])
    if ph:
        return ph[-1]
    return {}

def frag_score(obj):
    """
    Use the same phase-1 fragmentation signal as the algorithm.
    Prefer final phase1_history[-1].node_components[*].Frag_j.
    """
    ph = final_phase1(obj)
    comps = ph.get("node_components", {})
    vals = []
    for _, c in comps.items():
        if "Frag_j" in c:
            vals.append(float(c["Frag_j"]))
        elif "raw_frag_term" in c:
            vals.append(float(c["raw_frag_term"]))
    if vals:
        return sum(vals) / len(vals)

    # fallback: use top-level fragmented resource amounts if available
    fs = []
    for k in ["fragmented_cpu", "fragmented_mem", "fragmented_disk"]:
        if k in ph:
            fs.append(float(ph[k]))
    return sum(fs) / len(fs) if fs else 0.0

def load_var_score(obj):
    ph = final_phase1(obj)
    if "load_var" in ph:
        return float(ph["load_var"])

    summary = obj.get("summary", obj)
    return float(summary.get("load_var", 0.0))

def req_of_name(name):
    m = re.search(r"_(\d+)mb_(\d+)\.json$", name)
    return int(m.group(2)) if m else None

def collect_case_metrics(path):
    obj, s = load_summary_obj(path)
    old_obj = get_metric(s, ["objective", "obj", "Obj"])
    act = get_metric(s, ["ACT", "act", "avg_completion_time"])
    ams = get_metric(s, ["AMS", "ams", "avg_makespan"])
    downloaded = get_metric(s, ["downloaded_mb", "download_mb", "total_downloaded_mb"])
    reuse = get_metric(s, ["reuse_rate"])
    frag = frag_score(obj)
    load_var = load_var_score(obj)
    return {
        "old_obj": old_obj,
        "ACT": act,
        "AMS": ams,
        "downloaded": downloaded,
        "reuse": reuse,
        "frag": frag,
        "load_var": load_var,
    }

def avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case-list", default="/tmp/cacheonly_online_norm_full81_cases.txt")
    ap.add_argument("--w-time", type=float, default=0.50)
    ap.add_argument("--w-frag", type=float, default=0.20)
    ap.add_argument("--w-download", type=float, default=0.20)
    ap.add_argument("--w-load", type=float, default=0.10)
    args = ap.parse_args()

    methods = {
        "FG-base": "results/drtp/final_exp/cacheonly_dg_0_1024_fg",
        "OnlineNorm-trial2": "results/drtp/final_exp/cacheonly_fg_online_norm_trial2_full81",
        "OnlineNorm-trial12": "results/drtp/final_exp/cacheonly_fg_online_norm_trial12_full81",
    }

    cases = [x.strip() for x in open(args.case_list, encoding="utf-8") if x.strip()]

    # Load per-case baseline denominators
    base_by_case = {}
    for case in cases:
        name = os.path.basename(case)
        p = os.path.join(methods["FG-base"], name)
        if not os.path.exists(p):
            continue
        base_by_case[name] = collect_case_metrics(p)

    def eval_method(method_name, root, switch_threshold=None, switch_source=None):
        vals = defaultdict(list)
        missing = 0

        for case in cases:
            name = os.path.basename(case)
            req = req_of_name(name)

            use_root = root
            if switch_threshold is not None and switch_source is not None:
                use_root = methods[switch_source] if req >= switch_threshold else methods["FG-base"]

            p = os.path.join(use_root, name)
            if not os.path.exists(p) or name not in base_by_case:
                missing += 1
                continue

            m = collect_case_metrics(p)
            b = base_by_case[name]

            time_norm = m["old_obj"] / max(b["old_obj"], EPS)
            frag_norm = m["frag"] / max(b["frag"], EPS)
            download_norm = m["downloaded"] / max(b["downloaded"], EPS)
            load_norm = m["load_var"] / max(b["load_var"], EPS)

            j_aug = (
                args.w_time * time_norm
                + args.w_frag * frag_norm
                + args.w_download * download_norm
                + args.w_load * load_norm
            )

            vals["J_aug"].append(j_aug)
            vals["old_obj"].append(m["old_obj"])
            vals["frag"].append(m["frag"])
            vals["downloaded"].append(m["downloaded"])
            vals["reuse"].append(m["reuse"])
            vals["ACT"].append(m["ACT"])
            vals["AMS"].append(m["AMS"])
            vals["load_var"].append(m["load_var"])

        return {
            "cases": len(vals["J_aug"]),
            "missing": missing,
            "J_aug": avg(vals["J_aug"]),
            "old_obj": avg(vals["old_obj"]),
            "frag": avg(vals["frag"]),
            "downloaded": avg(vals["downloaded"]),
            "reuse": avg(vals["reuse"]),
            "ACT": avg(vals["ACT"]),
            "AMS": avg(vals["AMS"]),
            "load_var": avg(vals["load_var"]),
        }

    rows = []
    rows.append(("FG-base", eval_method("FG-base", methods["FG-base"])))
    rows.append(("OnlineNorm-trial2-all", eval_method("OnlineNorm-trial2", methods["OnlineNorm-trial2"])))
    rows.append(("OnlineNorm-trial12-all", eval_method("OnlineNorm-trial12", methods["OnlineNorm-trial12"])))

    for th in [600, 700, 800, 900, 1000]:
        rows.append((f"switch-trial12-req>={th}", eval_method(
            f"switch-trial12-req>={th}",
            methods["FG-base"],
            switch_threshold=th,
            switch_source="OnlineNorm-trial12",
        )))

    base = rows[0][1]

    print(f"# Augmented objective weights: time={args.w_time}, frag={args.w_frag}, download={args.w_download}, load={args.w_load}")
    print()
    print("| method | cases | missing | J_aug | J_aug_impr | old_obj | old_obj_impr | frag | frag_impr | downloaded | download_impr | reuse | ACT | AMS | load_var |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")

    for name, r in rows:
        j_impr = 0.0 if name == "FG-base" else (base["J_aug"] - r["J_aug"]) / max(base["J_aug"], EPS) * 100
        obj_impr = 0.0 if name == "FG-base" else (base["old_obj"] - r["old_obj"]) / max(base["old_obj"], EPS) * 100
        frag_impr = 0.0 if name == "FG-base" else (base["frag"] - r["frag"]) / max(base["frag"], EPS) * 100
        down_impr = 0.0 if name == "FG-base" else (base["downloaded"] - r["downloaded"]) / max(base["downloaded"], EPS) * 100

        print("| {} | {} | {} | {:.6f} | {:.2f}% | {:.3f} | {:.2f}% | {:.6f} | {:.2f}% | {:.3f} | {:.2f}% | {:.6f} | {:.3f} | {:.3f} | {:.3f} |".format(
            name,
            r["cases"],
            r["missing"],
            r["J_aug"],
            j_impr,
            r["old_obj"],
            obj_impr,
            r["frag"],
            frag_impr,
            r["downloaded"],
            down_impr,
            r["reuse"],
            r["ACT"],
            r["AMS"],
            r["load_var"],
        ))

if __name__ == "__main__":
    main()
