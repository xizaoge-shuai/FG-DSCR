import os
import re
import json
import argparse
import subprocess
from pathlib import Path

import optuna


def read_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def fg_help_text():
    try:
        r = subprocess.run(
            ["python3", "scripts/fg_dscr.py", "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        return r.stdout
    except Exception:
        return ""


def has_arg(help_text, name):
    return name in help_text


def safe_name(case_path):
    return Path(case_path).stem


def run_one_case(case, out, log, params, help_text):
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
        "--algo-name", "FG-DSCR-GC-Optuna",
    ]

    if has_arg(help_text, "--theta-cong-count"):
        cmd += ["--theta-cong-count", str(params["theta_cong_count"])]

    # 明确关闭之前诊断用的 CacheCore，不把补丁方案混进主线
    if has_arg(help_text, "--lambda-cache-core"):
        cmd += ["--lambda-cache-core", "0.0"]
    if has_arg(help_text, "--cache-core-ratio"):
        cmd += ["--cache-core-ratio", "0.90"]

    # 保持带宽相关项为当前主线默认/中性设置
    if has_arg(help_text, "--bw-gamma"):
        cmd += ["--bw-gamma", "1.0"]
    if has_arg(help_text, "--cache-bw-eta"):
        cmd += ["--cache-bw-eta", "0.0", "--cache-bw-ref", "100"]

    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(log).parent.mkdir(parents=True, exist_ok=True)

    with open(log, "w", encoding="utf-8") as lf:
        r = subprocess.run(
            cmd,
            stdout=lf,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )

    if r.returncode != 0:
        raise RuntimeError(f"fg_dscr failed, returncode={r.returncode}, log={log}")

    obj = read_json(out)
    summary = obj.get("summary", obj)

    if "objective" not in summary:
        raise RuntimeError(f"missing objective in {out}")

    return {
        "objective": float(summary.get("objective", 1e18)),
        "ACT": float(summary.get("ACT", 0.0)),
        "AMS": float(summary.get("AMS", 0.0)),
        "reuse_rate": float(summary.get("reuse_rate", 0.0)),
        "downloaded_mb": float(summary.get("downloaded_mb", 0.0)),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case-list", required=True)
    ap.add_argument("--out-root", default="results/drtp/optuna/fg_phase1_overall_runs")
    ap.add_argument("--study-name", default="fg_phase1_overall")
    ap.add_argument("--storage", default="sqlite:///results/drtp/optuna/fg_phase1_overall.db")
    ap.add_argument("--n-trials", type=int, default=30)
    ap.add_argument("--timeout", type=int, default=None)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    cases = [x.strip() for x in open(args.case_list, encoding="utf-8") if x.strip()]
    if not cases:
        raise SystemExit("empty case list")

    help_text = fg_help_text()

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
        params = {
            # 一阶段势函数权重
            "lambda_cong": trial.suggest_float("lambda_cong", 0.2, 3.0, log=True),
            "lambda_frag": trial.suggest_float("lambda_frag", 0.0, 1.0),
            "lambda_aff": trial.suggest_float("lambda_aff", 0.0, 2.0),
            "lambda_task_load": trial.suggest_float("lambda_task_load", 0.0, 1.0),

            # 负载约束/候选节点筛选强度
            "greedy_load_factor": trial.suggest_float("greedy_load_factor", 0.5, 1.3),

            # 如果代码支持，则调该项；否则不会传入
            "theta_cong_count": trial.suggest_float("theta_cong_count", 0.0, 1.0),
        }

        trial_dir = out_root / f"trial_{trial.number:04d}"
        log_dir = trial_dir / "logs"
        trial_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)

        metrics = []

        for idx, case in enumerate(cases):
            base = safe_name(case)
            out = str(trial_dir / f"{base}.json")
            log = str(log_dir / f"log_{base}.txt")

            try:
                m = run_one_case(case, out, log, params, help_text)
            except Exception as e:
                trial.set_user_attr("failed_case", case)
                trial.set_user_attr("error", str(e))
                raise optuna.exceptions.TrialPruned(str(e))

            metrics.append(m)

            avg_obj_now = sum(x["objective"] for x in metrics) / len(metrics)
            trial.report(avg_obj_now, idx)
            if trial.should_prune():
                raise optuna.exceptions.TrialPruned()

        avg_obj = sum(x["objective"] for x in metrics) / len(metrics)
        avg_act = sum(x["ACT"] for x in metrics) / len(metrics)
        avg_ams = sum(x["AMS"] for x in metrics) / len(metrics)
        avg_reuse = sum(x["reuse_rate"] for x in metrics) / len(metrics)
        avg_download = sum(x["downloaded_mb"] for x in metrics) / len(metrics)

        trial.set_user_attr("avg_objective", avg_obj)
        trial.set_user_attr("avg_ACT", avg_act)
        trial.set_user_attr("avg_AMS", avg_ams)
        trial.set_user_attr("avg_reuse_rate", avg_reuse)
        trial.set_user_attr("avg_downloaded_mb", avg_download)

        # 保存参数
        with open(trial_dir / "params.json", "w", encoding="utf-8") as f:
            json.dump(params, f, indent=2)

        with open(trial_dir / "metrics.json", "w", encoding="utf-8") as f:
            json.dump({
                "avg_objective": avg_obj,
                "avg_ACT": avg_act,
                "avg_AMS": avg_ams,
                "avg_reuse_rate": avg_reuse,
                "avg_downloaded_mb": avg_download,
                "params": params,
            }, f, indent=2)

        return avg_obj

    study.optimize(objective, n_trials=args.n_trials, timeout=args.timeout)

    print("=" * 100)
    print("[BEST]")
    print("best_value =", study.best_value)
    print("best_params =")
    print(json.dumps(study.best_params, indent=2))

    best_path = out_root / "best_params.json"
    with open(best_path, "w", encoding="utf-8") as f:
        json.dump(study.best_params, f, indent=2)
    print("[OK] wrote", best_path)


if __name__ == "__main__":
    main()
