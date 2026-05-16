import os
import json
import argparse
import subprocess
from pathlib import Path
from collections import defaultdict

import optuna


def safe_name(case_path):
    return Path(case_path).stem


def load_json(path):
    return json.load(open(path, "r", encoding="utf-8"))


def get_summary(obj):
    return obj.get("summary", obj)


def metric(summary, keys, default=None):
    for k in keys:
        if k in summary and summary[k] is not None:
            return float(summary[k])
    return default


def avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def extract_phase1_frag(obj):
    """
    Use the final Phase-1 potential components:
        phase1_history[-1].node_components[edge].Frag_j

    This is aligned with the paper's Phase-1 potential function.
    """
    hist = obj.get("phase1_history", [])
    if not hist:
        return None

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

    # fallback: if only fragmented_cpu/mem/disk exist
    fs = []
    for k in ["fragmented_cpu", "fragmented_mem", "fragmented_disk"]:
        if k in last and last[k] is not None:
            fs.append(float(last[k]))
    if fs:
        return sum(fs) / len(fs)

    return None


def collect_base(case_list, base_dir):
    vals = defaultdict(list)

    for case in case_list:
        name = os.path.basename(case)
        p = os.path.join(base_dir, name)

        if not os.path.exists(p):
            print("[WARN] missing base:", p)
            continue

        obj = load_json(p)
        s = get_summary(obj)

        vals["obj"].append(metric(s, ["objective", "obj", "Obj"]))
        vals["downloaded"].append(metric(s, ["downloaded_mb", "download_mb", "total_downloaded_mb"]))
        vals["frag"].append(extract_phase1_frag(obj))

    return {
        "obj": avg(vals["obj"]),
        "downloaded": avg(vals["downloaded"]),
        "frag": avg(vals["frag"]),
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
        "--algo-name", "FG-DSCR-GC-FragAwareOptunaV2",
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
        raise RuntimeError(f"fg_dscr failed: case={case}, log={log}")


def read_metrics(out):
    obj = load_json(out)
    s = get_summary(obj)

    time_obj = metric(s, ["objective", "obj", "Obj"])
    act = metric(s, ["ACT", "act", "avg_completion_time"])
    ams = metric(s, ["AMS", "ams", "avg_makespan"])
    reuse = metric(s, ["reuse_rate"])
    downloaded = metric(s, ["downloaded_mb", "download_mb", "total_downloaded_mb"])
    frag = extract_phase1_frag(obj)

    if time_obj is None:
        raise RuntimeError(f"missing objective in {out}")
    if frag is None:
        raise RuntimeError(f"missing phase1 fragmentation from phase1_history in {out}")

    return {
        "objective": time_obj,
        "ACT": act,
        "AMS": ams,
        "reuse_rate": reuse,
        "downloaded_mb": downloaded,
        "fragmentation_score": frag,
    }


def suggest_params(trial):
    return {
        "lambda_cong": trial.suggest_float("lambda_cong", 0.2, 2.0),
        "lambda_frag": trial.suggest_float("lambda_frag", 0.0, 1.5),
        "lambda_aff": trial.suggest_float("lambda_aff", 0.2, 2.0),
        "lambda_task_load": trial.suggest_float("lambda_task_load", 0.0, 0.8),
        "greedy_load_factor": trial.suggest_float("greedy_load_factor", 0.5, 1.2),
        "theta_cong_count": trial.suggest_float("theta_cong_count", 0.0, 1.0),
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
    ap.add_argument("--frag-penalty", type=float, default=0.2)
    ap.add_argument("--download-penalty", type=float, default=0.1)
    args = ap.parse_args()

    with open(args.case_list, "r", encoding="utf-8") as f:
        case_list = [x.strip() for x in f if x.strip()]

    base = collect_base(case_list, args.base_dir)
    base_obj = base["obj"]
    base_downloaded = base["downloaded"]
    base_frag = base["frag"]

    if base_obj is None:
        raise RuntimeError("base_obj is None. Check base-dir and case-list.")
    if base_downloaded is None:
        raise RuntimeError("base_downloaded is None. Check base-dir and case-list.")
    if base_frag is None:
        raise RuntimeError("base_frag is None. Base outputs may not contain phase1_history.")

    print("[INFO] cases =", len(case_list))
    print("[INFO] base_dir =", args.base_dir)
    print("[INFO] base_obj =", base_obj)
    print("[INFO] base_downloaded =", base_downloaded)
    print("[INFO] base_frag =", base_frag)
    print("[INFO] frag_penalty =", args.frag_penalty)
    print("[INFO] download_penalty =", args.download_penalty)

    sampler = optuna.samplers.TPESampler(seed=args.seed)
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
        params = suggest_params(trial)

        trial_dir = out_root / f"trial_{trial.number:04d}"
        log_dir = trial_dir / "logs"
        trial_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)

        with open(trial_dir / "params.json", "w", encoding="utf-8") as f:
            json.dump(params, f, indent=2)

        metrics = []

        for idx, case in enumerate(case_list):
            name = safe_name(case)
            out = str(trial_dir / f"{name}.json")
            log = str(log_dir / f"log_{name}.txt")

            print(f"[Trial {trial.number}] case {idx+1}/{len(case_list)}: {name}", flush=True)

            if not os.path.exists(out):
                run_one(case, out, log, params)

            m = read_metrics(out)
            metrics.append(m)

            print(
                f"[Trial {trial.number}] done {idx+1}/{len(case_list)}: "
                f"obj={m['objective']:.3f}, frag={m['fragmentation_score']:.6f}, "
                f"reuse={m['reuse_rate']:.6f}, download={m['downloaded_mb']:.1f}",
                flush=True,
            )

        avg_obj = avg([m["objective"] for m in metrics])
        avg_act = avg([m["ACT"] for m in metrics])
        avg_ams = avg([m["AMS"] for m in metrics])
        avg_reuse = avg([m["reuse_rate"] for m in metrics])
        avg_downloaded = avg([m["downloaded_mb"] for m in metrics])
        avg_frag = avg([m["fragmentation_score"] for m in metrics])

        norm_obj = avg_obj / max(base_obj, 1e-9)
        norm_frag = avg_frag / max(base_frag, 1e-9)
        norm_download = avg_downloaded / max(base_downloaded, 1e-9)
        download_inc = max(0.0, norm_download - 1.0)

        # Resource-aware objective:
        #   delay term:   normalized deployment-time objective
        #   resource term: normalized resource-fragmentation score
        #
        # This matches the paper logic: Phase 1 optimizes a trade-off between
        # deployment delay and resource fragmentation, rather than delay alone.
        delay_weight = 0.8
        resource_weight = 0.2
        resource_score = norm_frag

        score = delay_weight * norm_obj + resource_weight * resource_score

        trial.set_user_attr("score_fragaware", score)
        trial.set_user_attr("avg_objective", avg_obj)
        trial.set_user_attr("avg_ACT", avg_act)
        trial.set_user_attr("avg_AMS", avg_ams)
        trial.set_user_attr("avg_reuse_rate", avg_reuse)
        trial.set_user_attr("avg_downloaded_mb", avg_downloaded)
        trial.set_user_attr("avg_fragmentation_score", avg_frag)
        trial.set_user_attr("norm_objective", norm_obj)
        trial.set_user_attr("norm_fragmentation", norm_frag)
        trial.set_user_attr("norm_downloaded", norm_download)
        trial.set_user_attr("download_inc_ratio", download_inc)
        trial.set_user_attr("delay_weight", delay_weight)
        trial.set_user_attr("resource_weight", resource_weight)
        trial.set_user_attr("resource_score", resource_score)
        trial.set_user_attr("base_objective", base_obj)
        trial.set_user_attr("base_fragmentation_score", base_frag)
        trial.set_user_attr("base_downloaded_mb", base_downloaded)

        with open(trial_dir / "metrics.json", "w", encoding="utf-8") as f:
            json.dump({
                "score_fragaware": score,
                "avg_objective": avg_obj,
                "avg_ACT": avg_act,
                "avg_AMS": avg_ams,
                "avg_reuse_rate": avg_reuse,
                "avg_downloaded_mb": avg_downloaded,
                "avg_fragmentation_score": avg_frag,
                "norm_objective": norm_obj,
                "norm_fragmentation": norm_frag,
                "norm_downloaded": norm_download,
                "download_inc_ratio": download_inc,
                "delay_weight": delay_weight,
                "resource_weight": resource_weight,
                "resource_score": resource_score,
                "params": params,
            }, f, indent=2)

        print("=" * 100)
        print("[TRIAL]", trial.number)
        print("score_fragaware =", score)
        print("avg_objective =", avg_obj)
        print("avg_fragmentation_score =", avg_frag)
        print("norm_objective =", norm_obj)
        print("norm_fragmentation =", norm_frag)
        print("avg_downloaded_mb =", avg_downloaded)
        print("avg_reuse_rate =", avg_reuse)
        print("params =", json.dumps(params, indent=2))

        return score

    study.optimize(objective, n_trials=args.n_trials)

    print("=" * 100)
    print("[BEST]")
    print("best_score =", study.best_value)
    print("best_trial =", study.best_trial.number)
    print("best_params =")
    print(json.dumps(study.best_params, indent=2))


if __name__ == "__main__":
    main()
