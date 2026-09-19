#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许直接 python coded_delivery_gate/run_gate.py
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from gate.experiment import GateExperiment, load_scheduler_config


def main():
    p = argparse.ArgumentParser(
        description="FG-DSCR -> 历史异构缓存 -> Unicast/MCAST/strict-XOR2 第一阶段门禁实验"
    )
    p.add_argument("--repo-root", default=".", help="FG-DSCR 仓库根目录")
    p.add_argument("--case", required=True, help="上一篇实验 case JSON")
    p.add_argument("--out", default="coded_delivery_gate_result.json")
    p.add_argument("--placement", default="fg_dscr", choices=["fg_dscr", "round_robin", "random"])
    p.add_argument("--warmup-fraction", type=float, default=0.5)
    p.add_argument("--target-fraction", type=float, default=0.5)
    p.add_argument("--split-mode", default="sequential", choices=["sequential", "random"])
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--scheduler-config", default=None,
                   help="可选：JSON，覆盖当前分支 FGDscrScheduler 支持的参数；未列参数沿用分支默认")
    p.add_argument("--dump-state", action="store_true", help="把 H_n/D_n/W_n 写进结果 JSON")
    args = p.parse_args()

    cfg = load_scheduler_config(args.scheduler_config)
    exp = GateExperiment(args.repo_root, scheduler_config=cfg)
    res = exp.run(
        case_path=args.case,
        placement=args.placement,
        warmup_fraction=args.warmup_fraction,
        target_fraction=args.target_fraction,
        split_mode=args.split_mode,
        seed=args.seed,
        dump_state=args.dump_state,
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)

    m = res["metrics"]
    summary = {
        "case": res["case"],
        "placement": res["placement"],
        "num_nodes": res["num_nodes"],
        "warmup": res["num_warmup_containers"],
        "target": res["num_target_containers"],
        "assigned": res["num_target_assigned"],
        "unicast_mb": round(m["unicast_mb"], 3),
        "mcast_mb": round(m["mcast_mb"], 3),
        "xor2_mb": round(m["xor2_mb"], 3),
        "multicast_gain": round(m["multicast_gain"], 6),
        "xor2_gain_over_mcast": round(m["xor2_gain_over_mcast"], 6),
        "coding_density": round(m["coding_density"], 8),
        "cache_heterogeneity": round(m["cache_heterogeneity"], 6),
        "peer_coverage_ratio": round(m["peer_coverage_ratio"], 6),
        "strict_xor2_pairs": m["strict_xor2_matching_pairs"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n[OK] full result -> {out}")


if __name__ == "__main__":
    main()
