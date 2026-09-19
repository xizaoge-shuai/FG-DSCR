import os
import json
import argparse
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from collections import defaultdict

import optuna


def read_summary(p):
    with open(p, "r", encoding="utf-8") as f:
        obj = json.load(f)
    return obj.get("summary", obj)


def avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def case_base_name(case):
    return os.path.basename(case)


def get_metric(summary, names, default=None):
    for k in names:
        if k in summary and summary[k] is not None:
            return float(summary[k])
    return default


def collect_base_metrics(case_list, base_dir):
    vals = defaultdict(list)
    missing = []

    for case in case_list:
        base = case_base_name(case)
        p = os.path.join(base_dir, base)

        if not os.path.exists(p):
            missing.append(p)
            continue

        s = read_summary(p)

        vals["objective"].append(get_metric(s, ["objective", "obj", "Obj"], 0.0))
        vals["reuse_rate"].append(get_metric(s, ["reuse_rate"], 0.0))
        vals["downloaded_mb"].append(get_metric(s, ["downloaded_mb", "download_mb", "total_downloaded_mb"], 0.0))
        vals["ACT"].append(get_metric(s, ["ACT", "act", "avg_completion_time"], 0.0))
        vals["AMS"].append(get_metric(s, ["AMS", "ams", "avg_makespan"], 0.0))

    if missing:
        print("[WARN] missing base outputs:")
        for x in missing[:20]:
            print(" ", x)
        if len(missing) > 20:
            print(" ...", len(missing) - 20, "more")

    return {k: avg(v) for k, v in vals.items()}


def run_one(case, out, log, params):
    cmd = [
        "python3", "-u", "scripts/fg_dscr_online_norm.py",
        "--case", case,
        "--out", out,
        "--beam", "1",

        "--lambda-cong", str(params["lambda_cong"]),
        "--lambda-frag", str(params["lambda_frag"]),
        "--lambda-aff", str(params["lambda_aff"]),
        "--lambda-task-load", str(params["lambda_task_load"]),

        "--alpha1-reuse", str(params["alpha1_reuse"]),
        "--alpha2-future", str(params["alpha2_future"]),
        "--alpha3-pull", str(params["alpha3_pull"]),
        "--alpha4-evict", str(params["alpha4_evict"]),

        "--cache-policy", "pgdsf",
        "--order-policy", "dynamic_state",
        "--greedy-load-factor", str(params["greedy_load_factor"]),
        "--algo-name", "FG-DSCR-GC-OnlineNorm-Optuna",
        "--online-norm",
    ]

    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(log).parent.mkdir(parents=True, exist_ok=True)

    with open(log, "w", encoding="utf-8") as f:
        r = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, text=True)

    if r.returncode != 0:
        raise RuntimeError(f"run failed: case={case}, log={log}")


def collect_trial_metrics(case_list, out_dir):
    vals = defaultdict(list)
    missing = []

    for case in case_list:
        base = case_base_name(case)
        p = os.path.join(out_dir, base)

        if not os.path.exists(p):
            missing.append(p)
            continue

        s = read_summary(p)

        vals["objective"].append(get_metric(s, ["objective", "obj", "Obj"], 0.0))
        vals["reuse_rate"].append(get_metric(s, ["reuse_rate"], 0.0))
        vals["downloaded_mb"].append(get_metric(s, ["downloaded_mb", "download_mb", "total_downloaded_mb"], 0.0))
        vals["ACT"].append(get_metric(s, ["ACT", "act", "avg_completion_time"], 0.0))
        vals["AMS"].append(get_metric(s, ["AMS", "ams", "avg_makespan"], 0.0))

    if missing:
        print("[WARN] missing trial outputs:")
        for x in missing[:20]:
            print(" ", x)
        if len(missing) > 20:
            print(" ...", len(missing) - 20, "more")

    return {k: avg(v) for k, v in vals.items()}


def suggest_params(trial):
    return {
        # Phase 1
        "lambda_cong": trial.suggest_float("lambda_cong", 0.2, 2.0, log=True),
        "lambda_frag": trial.suggest_float("lambda_frag", 0.01, 1.0, log=True),
        "lambda_aff": trial.suggest_float("lambda_aff", 0.05, 2.0, log=True),
        "lambda_task_load": trial.suggest_float("lambda_task_load", 0.0, 0.20),

        # Phase 2 normalized weights
        "alpha1_reuse": trial.suggest_float("alpha1_reuse", 0.05, 0.70),
        "alpha2_future": trial.suggest_float("alpha2_future", 0.05, 0.70),
        "alpha3_pull": trial.suggest_float("alpha3_pull", 0.05, 0.70),
        "alpha4_evict": trial.suggest_float("alpha4_evict", 0.00, 0.30),

        # greedy initialization
        "greedy_load_factor": trial.suggest_float("greedy_load_factor", 1.05, 2.50),
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
    ap.add_argument("--reuse-penalty", type=float, default=800.0)
    ap.add_argument("--download-penalty", type=float, default=120.0)
    ap.add_argument("--jobs-per-trial", type=int, default=1)
    args = ap.parse_args()

    with open(args.case_list, "r", encoding="utf-8") as f:
        case_list = [x.strip() for x in f if x.strip()]

    print("[INFO] cases =", len(case_list))
    print("[INFO] base_dir =", args.base_dir)
    print("[INFO] out_root =", args.out_root)
    print("[INFO] study_name =", args.study_name)
    print("[INFO] storage =", args.storage)
    print("[INFO] reuse_penalty =", args.reuse_penalty)
    print("[INFO] download_penalty =", args.download_penalty)

    base_metrics = collect_base_metrics(case_list, args.base_dir)
    base_obj = base_metrics["objective"]
    base_reuse = base_metrics["reuse_rate"]
    base_download = base_metrics["downloaded_mb"]

    print("[BASE]", json.dumps(base_metrics, indent=2))

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
        out_dir = trial_dir / "outputs"
        log_dir = trial_dir / "logs"
        trial_dir.mkdir(parents=True, exist_ok=True)

        with open(trial_dir / "params.json", "w", encoding="utf-8") as f:
            json.dump(params, f, indent=2)

        jobs = []
        for case in case_list:
            base = case_base_name(case)
            out = str(out_dir / base)
            log = str(log_dir / (base + ".log"))

            if os.path.exists(out):
                continue

            jobs.append((case, out, log))

        if args.jobs_per_trial <= 1:
            for case, out, log in jobs:
                run_one(case, out, log, params)
        else:
            with ThreadPoolExecutor(max_workers=args.jobs_per_trial) as ex:
                futs = [
                    ex.submit(run_one, case, out, log, params)
                    for case, out, log in jobs
                ]
                for fut in as_completed(futs):
                    fut.result()

        m = collect_trial_metrics(case_list, str(out_dir))

        avg_obj = m["objective"]
        avg_reuse = m["reuse_rate"]
        avg_download = m["downloaded_mb"]
        avg_act = m["ACT"]
        avg_ams = m["AMS"]

        reuse_drop = max(0.0, base_reuse - avg_reuse)
        download_inc_ratio = max(0.0, (avg_download - base_download) / max(base_download, 1e-8))

        score = (
            avg_obj
            + args.reuse_penalty * reuse_drop
            + args.download_penalty * download_inc_ratio
        )

        trial.set_user_attr("avg_objective", avg_obj)
        trial.set_user_attr("avg_reuse_rate", avg_reuse)
        trial.set_user_attr("avg_downloaded_mb", avg_download)
        trial.set_user_attr("avg_ACT", avg_act)
        trial.set_user_attr("avg_AMS", avg_ams)
        trial.set_user_attr("base_objective", base_obj)
        trial.set_user_attr("base_reuse_rate", base_reuse)
        trial.set_user_attr("base_downloaded_mb", base_download)
        trial.set_user_attr("reuse_drop", reuse_drop)
        trial.set_user_attr("download_inc_ratio", download_inc_ratio)

        with open(trial_dir / "metrics.json", "w", encoding="utf-8") as f:
            json.dump({
                "score_with_penalty": score,
                "avg_objective": avg_obj,
                "avg_reuse_rate": avg_reuse,
                "avg_downloaded_mb": avg_download,
                "avg_ACT": avg_act,
                "avg_AMS": avg_ams,
                "base_objective": base_obj,
                "base_reuse_rate": base_reuse,
                "base_downloaded_mb": base_download,
                "reuse_drop": reuse_drop,
                "download_inc_ratio": download_inc_ratio,
                "params": params,
            }, f, indent=2)

        print("=" * 100)
        print("[TRIAL]", trial.number)
        print("score =", score)
        print("metrics =", json.dumps({
            "avg_objective": avg_obj,
            "avg_reuse_rate": avg_reuse,
            "avg_downloaded_mb": avg_download,
            "avg_ACT": avg_act,
            "avg_AMS": avg_ams,
            "reuse_drop": reuse_drop,
            "download_inc_ratio": download_inc_ratio,
        }, indent=2))
        print("params =", json.dumps(params, indent=2))

        return score

    study.optimize(objective, n_trials=args.n_trials)

    print("=" * 100)
    print("[BEST]")
    print("best_score =", study.best_value)
    print("best_params =")
    print(json.dumps(study.best_params, indent=2))

    best_path = out_root / "best_params.json"
    with open(best_path, "w", encoding="utf-8") as f:
        json.dump(study.best_params, f, indent=2)
    print("[OK] wrote", best_path)


if __name__ == "__main__":
    main()
