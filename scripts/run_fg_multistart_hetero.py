#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import argparse
import subprocess
from pathlib import Path

CONFIGS = [
    {
        "name": "pgdsf_dyn_base",
        "cache_policy": "pgdsf",
        "order_policy": "dynamic_state",
        "lambda_cong": "1.0",
        "lambda_frag": "0.1",
        "lambda_aff": "0.6",
        "lambda_task_load": "0.10",
        "greedy_load_factor": "0.9",
    },
    {
        "name": "lfu_dyn_bwcache",
        "cache_policy": "lfu",
        "order_policy": "dynamic_state",
        "lambda_cong": "1.0",
        "lambda_frag": "0.1",
        "lambda_aff": "0.6",
        "lambda_task_load": "0.10",
        "greedy_load_factor": "0.9",
    },
    {
        "name": "lfu_static_highreuse",
        "cache_policy": "lfu",
        "order_policy": "static_ilrsa",
        "lambda_cong": "1.0",
        "lambda_frag": "0.1",
        "lambda_aff": "1.0",
        "lambda_task_load": "0.10",
        "greedy_load_factor": "0.9",
    },
    {
        "name": "bwplace_cong13",
        "cache_policy": "lfu",
        "order_policy": "dynamic_state",
        "lambda_cong": "1.3",
        "lambda_frag": "0.1",
        "lambda_aff": "0.4",
        "lambda_task_load": "0.10",
        "greedy_load_factor": "0.9",
    },
    {
        "name": "bwplace_cong15",
        "cache_policy": "lfu",
        "order_policy": "dynamic_state",
        "lambda_cong": "1.5",
        "lambda_frag": "0.1",
        "lambda_aff": "0.4",
        "lambda_task_load": "0.15",
        "greedy_load_factor": "0.9",
    },
]

def load_summary(path: str):
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)
    return obj.get("summary", obj)

def get_obj(summary):
    for k in ["objective", "Obj", "obj"]:
        if k in summary:
            return float(summary[k])
    return 0.5 * float(summary.get("ACT", 0)) + 0.5 * float(summary.get("AMS", 0))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--logdir", required=True)
    ap.add_argument("--python", default="python3")
    args = ap.parse_args()

    Path(args.workdir).mkdir(parents=True, exist_ok=True)
    Path(args.logdir).mkdir(parents=True, exist_ok=True)
    Path(os.path.dirname(args.out)).mkdir(parents=True, exist_ok=True)

    base = Path(args.case).stem

    candidates = []

    for cfg in CONFIGS:
        cand_out = os.path.join(args.workdir, f"{base}__{cfg['name']}.json")
        cand_log = os.path.join(args.logdir, f"{base}__{cfg['name']}.txt")

        cmd = [
            args.python, "-u", "scripts/fg_dscr.py",
            "--case", args.case,
            "--out", cand_out,
            "--beam", "1",
            "--lambda-cong", cfg["lambda_cong"],
            "--lambda-frag", cfg["lambda_frag"],
            "--lambda-aff", cfg["lambda_aff"],
            "--lambda-task-load", cfg["lambda_task_load"],
            "--cache-policy", cfg["cache_policy"],
            "--order-policy", cfg["order_policy"],
            "--greedy-load-factor", cfg["greedy_load_factor"],
            "--algo-name", f"FG-DSCR-GC-MultiStart-{cfg['name']}",
        ]

        print("=" * 100)
        print("[MULTISTART RUN]", cfg["name"])
        print(" ".join(cmd))
        print("=" * 100)

        with open(cand_log, "w", encoding="utf-8") as lf:
            ret = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT)

        if ret.returncode != 0:
            print("[WARN] candidate failed:", cfg["name"])
            continue

        if not os.path.exists(cand_out):
            print("[WARN] missing candidate output:", cand_out)
            continue

        summary = load_summary(cand_out)
        obj = get_obj(summary)

        candidates.append({
            "config": cfg["name"],
            "objective": obj,
            "output": cand_out,
            "summary": summary,
        })

    if not candidates:
        raise RuntimeError("No successful candidate.")

    best = min(candidates, key=lambda x: x["objective"])

    with open(best["output"], "r", encoding="utf-8") as f:
        best_obj = json.load(f)

    if isinstance(best_obj, dict):
        best_obj["multistart_selected_config"] = best["config"]
        best_obj["multistart_candidates"] = [
            {
                "config": c["config"],
                "objective": c["objective"],
                "output": c["output"],
            }
            for c in sorted(candidates, key=lambda x: x["objective"])
        ]
        if "summary" in best_obj and isinstance(best_obj["summary"], dict):
            best_obj["summary"]["algo"] = "FG-DSCR-GC-MultiStart"
            best_obj["summary"]["selected_config"] = best["config"]

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(best_obj, f, indent=2)

    print("[OK] selected:", best["config"], "objective=", best["objective"])
    print("[OK] written:", args.out)

if __name__ == "__main__":
    main()
