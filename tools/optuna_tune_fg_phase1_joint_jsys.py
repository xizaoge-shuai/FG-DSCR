import os
import json
import math
import argparse
import subprocess
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import optuna


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_summary(obj):
    return obj.get("summary", obj)


def metric(summary, keys, default=None):
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


def safe_stem(case):
    return Path(case).stem


def extract_phase1_frag(obj):
    """
    Prefer the Phase-1 potential component:
      phase1_history[-1].node_components[edge].Frag_j
    """
    hist = obj.get("phase1_history", [])
    if hist:
        last = hist[-1]
        comps = last.get("node_components", {})
        vals = []
        for _, c in comps.items():
            if isinstance(c, dict):
                v = c.get("Frag_j", c.get("raw_frag_term", None))
                if v is not None:
                    vals.append(float(v))
        if vals:
            return sum(vals) / len(vals)

    # fallback: recursively search all Frag_j
    vals = []
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k == "Frag_j" and v is not None:
                    try:
                        vals.append(float(v))
                    except Exception:
                        pass
                else:
                    walk(v)
        elif isinstance(x, list):
            for y in x:
                walk(y)
    walk(obj)
    return avg(vals)


def maybe_edge_assignment_dict(d):
    if not isinstance(d, dict) or not d:
        return False
    keys = list(d.keys())
    if not all(isinstance(k, str) and k.startswith("edge-") for k in keys):
        return False
    vals = list(d.values())
    return all(isinstance(v, list) for v in vals)


def extract_load_var(obj):
    s = get_summary(obj)
    v = metric(s, ["load_var", "node_load_var", "container_load_var"])
    if v is not None:
        return v

    found = []

    def walk(x):
        if isinstance(x, dict):
            if maybe_edge_assignment_dict(x):
                counts = [len(v) for v in x.values()]
                if counts:
                    m = sum(counts) / len(counts)
                    var = sum((c - m) ** 2 for c in counts) / len(counts)
                    found.append(var)
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)
    if found:
        return found[-1]
    return None


def read_metrics(path):
    obj = load_json(path)
    s = get_summary(obj)

    return {
        "ACT": metric(s, ["ACT", "act", "avg_completion_time"]),
        "AMS": metric(s, ["AMS", "ams", "avg_makespan"]),
        "old_obj": metric(s, ["objective", "obj", "Obj"]),
        "downloaded": metric(s, ["downloaded_mb", "download_mb", "total_downloaded_mb"]),
        "reuse": metric(s, ["reuse_rate"]),
        "frag": extract_phase1_frag(obj),
        "load_var": extract_load_var(obj),
    }


def collect_base(case_list, base_dir):
    vals = defaultdict(list)
    missing = []

    for case in case_list:
        name = os.path.basename(case)
        p = os.path.join(base_dir, name)
        if not os.path.exists(p):
            missing.append(name)
            continue

        m = read_metrics(p)
        for k, v in m.items():
            vals[k].append(v)

    base = {k: avg(vs) for k, vs in vals.items()}
    base["missing"] = missing
    return base


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
        "--algo-name", "FG-DSCR-GC-JointJsysOptuna",
        "--theta-cong-count", str(params["theta_cong_count"]),

        "--lambda-cache-core", "0.0",
        "--cache-core-ratio", "0.90",
        "--bw-gamma", "1.0",
        "--cache-bw-eta", "0.0",
        "--cache-bw-ref", "100",
    ]

    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(log).parent.mkdir(parents=True, exist_ok=True)

    with open(log, "w", encoding="utf-8") as f:
        r = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, text=True)

    if r.returncode != 0:
        raise RuntimeError(f"fg_dscr failed, returncode={r.returncode}, case={case}, log={log}")


def sample_weights(trial):
    """
    Jointly tune outer J_sys weights.

    Load-var is special:
      - exact zero is explicitly allowed;
      - otherwise it can be searched in a wider range up to 0.40.
    Other weights are normalized to the remaining mass.
    """
    load_mode = trial.suggest_categorical(
        "w_load_mode",
        ["zero", "small", "medium", "large"]
    )

    if load_mode == "zero":
        w_load = 0.0
    elif load_mode == "small":
        w_load = trial.suggest_float("w_load_value", 0.02, 0.10)
    elif load_mode == "medium":
        w_load = trial.suggest_float("w_load_value", 0.10, 0.25)
    else:
        w_load = trial.suggest_float("w_load_value", 0.25, 0.40)

    remain = max(1.0 - w_load, 1e-9)

    raw_act = trial.suggest_float("w_act_raw", 0.10, 1.50)
    raw_ams = trial.suggest_float("w_ams_raw", 0.10, 1.50)
    raw_frag = trial.suggest_float("w_frag_raw", 0.05, 1.50)
    raw_download = trial.suggest_float("w_download_raw", 0.05, 1.50)

    total = raw_act + raw_ams + raw_frag + raw_download

    w_act = remain * raw_act / total
    w_ams = remain * raw_ams / total
    w_frag = remain * raw_frag / total
    w_download = remain * raw_download / total

    return {
        "w_act": w_act,
        "w_ams": w_ams,
        "w_frag": w_frag,
        "w_download": w_download,
        "w_load": w_load,
    }


def sample_phase1_params(trial):
    """
    Broad search space.
    We intentionally keep it broad because this joint search also changes J_sys weights.
    """
    return {
        "lambda_cong": trial.suggest_float("lambda_cong", 0.0, 2.5),
        "lambda_frag": trial.suggest_float("lambda_frag", 0.0, 2.0),
        "lambda_aff": trial.suggest_float("lambda_aff", 0.0, 2.5),
        "lambda_task_load": trial.suggest_float("lambda_task_load", 0.0, 1.2),
        "greedy_load_factor": trial.suggest_float("greedy_load_factor", 0.70, 2.60),
        "theta_cong_count": trial.suggest_float("theta_cong_count", 0.0, 1.2),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case-list", required=True)
    ap.add_argument("--base-dir", required=True)
    ap.add_argument("--out-root", required=True)
    ap.add_argument("--study-name", required=True)
    ap.add_argument("--storage", required=True)
    ap.add_argument("--n-trials", type=int, default=20)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--jobs-per-trial", type=int, default=1)
    args = ap.parse_args()

    with open(args.case_list, "r", encoding="utf-8") as f:
        case_list = [x.strip() for x in f if x.strip()]

    base = collect_base(case_list, args.base_dir)

    print("[INFO] cases =", len(case_list))
    print("[INFO] base_dir =", args.base_dir)
    print("[INFO] base =", json.dumps(base, indent=2))
    print("[INFO] jobs_per_trial =", args.jobs_per_trial)

    required = ["ACT", "AMS", "frag", "downloaded", "load_var"]
    for k in required:
        if base.get(k) is None:
            raise RuntimeError(f"Base metric {k} is None. Check base-dir outputs.")

    sampler = optuna.samplers.TPESampler(seed=args.seed, multivariate=True, group=True)
    study = optuna.create_study(
        study_name=args.study_name,
        storage=args.storage,
        direction="minimize",
        sampler=sampler,
        load_if_exists=True,
    )

    out_root = Path(args.out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    def objective(trial):
        weights = sample_weights(trial)
        params = sample_phase1_params(trial)

        trial_dir = out_root / f"trial_{trial.number:04d}"
        log_dir = trial_dir / "logs"
        trial_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)

        with open(trial_dir / "params_and_weights.json", "w", encoding="utf-8") as f:
            json.dump({"weights": weights, "params": params}, f, indent=2)

        jobs = []
        for idx, case in enumerate(case_list):
            name = safe_stem(case)
            out = str(trial_dir / f"{name}.json")
            log = str(log_dir / f"{name}.log")
            if not os.path.exists(out):
                jobs.append((idx, case, out, log))

        if args.jobs_per_trial <= 1:
            for idx, case, out, log in jobs:
                print(f"[Trial {trial.number}] run {idx+1}/{len(case_list)} {safe_stem(case)}", flush=True)
                run_one(case, out, log, params)
        else:
            with ThreadPoolExecutor(max_workers=args.jobs_per_trial) as ex:
                futs = []
                for idx, case, out, log in jobs:
                    print(f"[Trial {trial.number}] submit {idx+1}/{len(case_list)} {safe_stem(case)}", flush=True)
                    futs.append(ex.submit(run_one, case, out, log, params))
                for fut in as_completed(futs):
                    fut.result()

        metrics = []
        for case in case_list:
            name = safe_stem(case)
            out = trial_dir / f"{name}.json"
            if not out.exists():
                raise RuntimeError(f"missing output: {out}")
            metrics.append(read_metrics(str(out)))

        avg_ACT = avg([m["ACT"] for m in metrics])
        avg_AMS = avg([m["AMS"] for m in metrics])
        avg_old_obj = avg([m["old_obj"] for m in metrics])
        avg_frag = avg([m["frag"] for m in metrics])
        avg_downloaded = avg([m["downloaded"] for m in metrics])
        avg_reuse = avg([m["reuse"] for m in metrics])
        avg_load_var = avg([m["load_var"] for m in metrics])

        ratio_ACT = avg_ACT / max(base["ACT"], 1e-9)
        ratio_AMS = avg_AMS / max(base["AMS"], 1e-9)
        ratio_frag = avg_frag / max(base["frag"], 1e-9)
        ratio_downloaded = avg_downloaded / max(base["downloaded"], 1e-9)
        ratio_load_var = avg_load_var / max(base["load_var"], 1e-9)

        score = (
            weights["w_act"] * ratio_ACT
            + weights["w_ams"] * ratio_AMS
            + weights["w_frag"] * ratio_frag
            + weights["w_download"] * ratio_downloaded
            + weights["w_load"] * ratio_load_var
        )

        attrs = {
            "score": score,
            "avg_ACT": avg_ACT,
            "avg_AMS": avg_AMS,
            "avg_old_obj": avg_old_obj,
            "avg_frag": avg_frag,
            "avg_downloaded": avg_downloaded,
            "avg_reuse": avg_reuse,
            "avg_load_var": avg_load_var,
            "ratio_ACT": ratio_ACT,
            "ratio_AMS": ratio_AMS,
            "ratio_frag": ratio_frag,
            "ratio_downloaded": ratio_downloaded,
            "ratio_load_var": ratio_load_var,
            **weights,
        }

        for k, v in attrs.items():
            trial.set_user_attr(k, v)

        with open(trial_dir / "metrics.json", "w", encoding="utf-8") as f:
            json.dump({
                "score": score,
                "weights": weights,
                "params": params,
                "metrics": attrs,
                "base": base,
            }, f, indent=2)

        print("=" * 100)
        print("[TRIAL]", trial.number)
        print("score =", score)
        print("weights =", json.dumps(weights, indent=2))
        print("params =", json.dumps(params, indent=2))
        print("ratios =", json.dumps({
            "ratio_ACT": ratio_ACT,
            "ratio_AMS": ratio_AMS,
            "ratio_frag": ratio_frag,
            "ratio_downloaded": ratio_downloaded,
            "ratio_load_var": ratio_load_var,
        }, indent=2))
        print("avgs =", json.dumps({
            "avg_ACT": avg_ACT,
            "avg_AMS": avg_AMS,
            "avg_old_obj": avg_old_obj,
            "avg_frag": avg_frag,
            "avg_downloaded": avg_downloaded,
            "avg_reuse": avg_reuse,
            "avg_load_var": avg_load_var,
        }, indent=2))

        return score

    study.optimize(objective, n_trials=args.n_trials)

    print("=" * 100)
    print("[BEST]")
    print("best_score =", study.best_value)
    print("best_trial =", study.best_trial.number)
    print("best_params =", json.dumps(study.best_params, indent=2))
    print("best_user_attrs =", json.dumps(study.best_trial.user_attrs, indent=2))


if __name__ == "__main__":
    main()
