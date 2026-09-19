#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import csv
import glob
import json
import sys
from pathlib import Path
from typing import Dict, Any, List

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from gate.experiment import GateExperiment, load_scheduler_config


def flatten_result(res: Dict[str, Any]) -> Dict[str, Any]:
    m = res["metrics"]
    row = {
        "case": res["case"],
        "seed": res["seed"],
        "split_mode": res["split_mode"],
        "placement": res["placement"],
        "num_nodes": res["num_nodes"],
        "num_case_containers": res["num_case_containers"],
        "num_warmup_containers": res["num_warmup_containers"],
        "num_target_containers": res["num_target_containers"],
        "num_target_assigned": res["num_target_assigned"],
        "unicast_mb": m["unicast_mb"],
        "mcast_mb": m["mcast_mb"],
        "xor2_mb": m["xor2_mb"],
        "multicast_gain": m["multicast_gain"],
        "xor2_gain_over_mcast": m["xor2_gain_over_mcast"],
        "total_saving_vs_unicast": m["total_saving_vs_unicast"],
        "coding_density": m["coding_density"],
        "num_reciprocal_edges": m["num_reciprocal_edges"],
        "reciprocal_node_pair_rate": m["reciprocal_node_pair_rate"],
        "strict_xor2_matching_pairs": m["strict_xor2_matching_pairs"],
        "strict_xor2_saving_mb": m["strict_xor2_saving_mb"],
        "peer_coverage_ratio": m["peer_coverage_ratio"],
        "avg_cache_jaccard": m["avg_cache_jaccard"],
        "cache_heterogeneity": m["cache_heterogeneity"],
        "cache_byte_heterogeneity": m["cache_byte_heterogeneity"],
    }
    return row


def expand_patterns(patterns: List[str]) -> List[str]:
    files = []
    seen = set()
    for pat in patterns:
        matches = glob.glob(pat, recursive=True)
        if not matches and Path(pat).is_file():
            matches = [pat]
        for x in sorted(matches):
            if x.endswith(".json") and x not in seen:
                seen.add(x)
                files.append(x)
    return files


def main():
    p = argparse.ArgumentParser(description="批量运行编码分发门禁实验")
    p.add_argument("--repo-root", default=".")
    p.add_argument("--cases", nargs="+", required=True,
                   help="case 文件或 glob，例如 cases/drtp_scale_nodes/**/*.json")
    p.add_argument("--placements", nargs="+", default=["fg_dscr"],
                   choices=["fg_dscr", "round_robin", "random"])
    p.add_argument("--seeds", nargs="+", type=int, default=[1, 2, 3, 4, 5])
    p.add_argument("--warmup-fraction", type=float, default=0.5)
    p.add_argument("--target-fraction", type=float, default=0.5)
    p.add_argument("--split-mode", default="random", choices=["sequential", "random"])
    p.add_argument("--scheduler-config", default=None)
    p.add_argument("--max-cases", type=int, default=0, help="0 表示不限")
    p.add_argument("--out-csv", default="coded_delivery_gate_sweep.csv")
    p.add_argument("--out-jsonl", default="coded_delivery_gate_sweep.jsonl")
    args = p.parse_args()

    cases = expand_patterns(args.cases)
    if args.max_cases > 0:
        cases = cases[:args.max_cases]
    if not cases:
        raise SystemExit("没有匹配到 case JSON")

    cfg = load_scheduler_config(args.scheduler_config)
    exp = GateExperiment(args.repo_root, scheduler_config=cfg)

    rows = []
    jsonl_path = Path(args.out_jsonl)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    with open(jsonl_path, "w", encoding="utf-8") as jf:
        total = len(cases) * len(args.placements) * len(args.seeds)
        idx = 0
        for case in cases:
            for placement in args.placements:
                for seed in args.seeds:
                    idx += 1
                    print(f"[{idx}/{total}] {case} placement={placement} seed={seed}", flush=True)
                    try:
                        res = exp.run(
                            case_path=case,
                            placement=placement,
                            warmup_fraction=args.warmup_fraction,
                            target_fraction=args.target_fraction,
                            split_mode=args.split_mode,
                            seed=seed,
                            dump_state=False,
                        )
                        jf.write(json.dumps(res, ensure_ascii=False) + "\n")
                        jf.flush()
                        rows.append(flatten_result(res))
                    except Exception as e:
                        print(f"[FAIL] {type(e).__name__}: {e}", file=sys.stderr, flush=True)

    if not rows:
        raise SystemExit("所有实验都失败，没有可写 CSV 的结果")

    csv_path = Path(args.out_csv)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print(f"[OK] CSV   -> {csv_path}")
    print(f"[OK] JSONL -> {jsonl_path}")


if __name__ == "__main__":
    main()
