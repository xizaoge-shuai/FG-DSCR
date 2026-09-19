import argparse
import copy
import json
from pathlib import Path

ENV_BW = {
    # 平均 150
    "homo_good":   [150.0,150.0,150.0,150.0,150.0,150.0,150.0,150.0],
    "hetero_good": [180.0,180.0,120.0,120.0,180.0,120.0,180.0,120.0],

    # 平均 50
    "homo_bad":    [50.0,50.0,50.0,50.0,50.0,50.0,50.0,50.0],
    "hetero_bad":  [70.0,70.0,30.0,30.0,70.0,30.0,70.0,30.0],
}

def load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(obj, p):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache-mb", type=int, required=True)
    ap.add_argument("--env", required=True, choices=list(ENV_BW.keys()))
    args = ap.parse_args()

    obj = load_json(args.base)
    obj["nodes"] = copy.deepcopy(obj["nodes"])

    bw = ENV_BW[args.env]

    for i, n in enumerate(obj["nodes"]):
        n["repo_capacity_mb"] = int(args.cache_mb)
        n["bandwidth_mb_s"] = float(bw[i % len(bw)])

    obj["variant_meta"] = {
        "fig": "fig4_network_equalmean",
        "base": args.base,
        "cache_mb": args.cache_mb,
        "env": args.env,
        "bandwidth": bw,
        "mean_bandwidth": sum(bw) / len(bw),
        "cache_mode": "homo",
    }

    save_json(obj, args.out)

if __name__ == "__main__":
    main()
