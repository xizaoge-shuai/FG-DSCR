import argparse
import copy
import json
from pathlib import Path

def load(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def save(obj, p):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)

def set_cache(nodes, mean_cache, cache_mode):
    if cache_mode == "homo":
        factors = [1.0] * 8
    elif cache_mode == "hetero":
        factors = [0.5, 0.75, 0.75, 1.0, 1.0, 1.25, 1.25, 1.5]
    else:
        raise ValueError(cache_mode)

    for i, n in enumerate(nodes):
        n["repo_capacity_mb"] = int(round(float(mean_cache) * factors[i % len(factors)]))
    return nodes

def set_bandwidth(nodes, env):
    if env == "default":
        return nodes

    if env == "homo_good":
        base = [150.0] * 8
    elif env == "homo_bad":
        base = [50.0] * 8
    elif env == "hetero_good":
        base = [150.0, 150.0, 120.0, 120.0, 150.0, 120.0, 150.0, 120.0]
    elif env == "hetero_bad":
        base = [80.0, 80.0, 50.0, 50.0, 80.0, 50.0, 80.0, 50.0]
    else:
        raise ValueError(env)

    for i, n in enumerate(nodes):
        n["bandwidth_mb_s"] = base[i % len(base)]
    return nodes

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--hetero-cache-mean", type=int, default=None)
    ap.add_argument("--cache-mode", default="hetero", choices=["homo", "hetero"])
    ap.add_argument("--bandwidth-env", default="default",
                    choices=["default", "homo_good", "homo_bad", "hetero_good", "hetero_bad"])
    args = ap.parse_args()

    obj = load(args.base)
    obj["nodes"] = copy.deepcopy(obj["nodes"])

    if args.hetero_cache_mean is not None:
        obj["nodes"] = set_cache(obj["nodes"], args.hetero_cache_mean, args.cache_mode)

    obj["nodes"] = set_bandwidth(obj["nodes"], args.bandwidth_env)
    obj["variant_meta"] = {
        "base": args.base,
        "cache_mean": args.hetero_cache_mean,
        "cache_mode": args.cache_mode,
        "bandwidth_env": args.bandwidth_env,
    }

    save(obj, args.out)

if __name__ == "__main__":
    main()
