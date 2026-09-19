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

def resize_nodes(nodes, m):
    nodes = copy.deepcopy(nodes)
    if len(nodes) >= m:
        out = nodes[:m]
    else:
        out = nodes[:]
        base = nodes[-1]
        for i in range(len(nodes), m):
            n = copy.deepcopy(base)
            n["eid"] = f"edge-{i+1}"
            out.append(n)
    for i, n in enumerate(out):
        n["eid"] = f"edge-{i+1}"
    return out

def set_cache(nodes, cache):
    for n in nodes:
        n["repo_capacity_mb"] = int(cache)
    return nodes

def set_hetero_cache_mean(nodes, mean_cache):
    factors = [0.5, 0.75, 0.75, 1.0, 1.0, 1.25, 1.25, 1.5]
    vals = [int(round(mean_cache * factors[i % len(factors)])) for i in range(len(nodes))]
    for n, v in zip(nodes, vals):
        n["repo_capacity_mb"] = v
    return nodes

def set_bandwidth(nodes, env):
    m = len(nodes)

    if env == "homo_good":
        base = [150.0] * 8
    elif env == "homo_bad":
        base = [50.0] * 8
    elif env == "hetero_good":
        base = [150.0, 150.0, 120.0, 120.0, 150.0, 120.0, 150.0, 120.0]
    elif env == "hetero_bad":
        base = [80.0, 80.0, 50.0, 50.0, 80.0, 50.0, 80.0, 50.0]
    elif env == "default":
        return nodes
    else:
        raise ValueError(env)

    vals = [base[i % len(base)] for i in range(m)]
    for n, bw in zip(nodes, vals):
        n["bandwidth_mb_s"] = bw
    return nodes

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--edge-nodes", type=int, default=None)
    ap.add_argument("--cache", type=int, default=None)
    ap.add_argument("--hetero-cache-mean", type=int, default=None)
    ap.add_argument("--bandwidth-env", default="default",
                    choices=["default", "homo_good", "homo_bad", "hetero_good", "hetero_bad"])
    args = ap.parse_args()

    obj = load(args.base)

    if args.edge_nodes is not None:
        obj["nodes"] = resize_nodes(obj["nodes"], args.edge_nodes)

    if args.hetero_cache_mean is not None:
        obj["nodes"] = set_hetero_cache_mean(obj["nodes"], args.hetero_cache_mean)
    elif args.cache is not None:
        obj["nodes"] = set_cache(obj["nodes"], args.cache)

    obj["nodes"] = set_bandwidth(obj["nodes"], args.bandwidth_env)

    obj.setdefault("variant_meta", {})
    obj["variant_meta"].update({
        "base": args.base,
        "edge_nodes": args.edge_nodes,
        "cache": args.cache,
        "hetero_cache_mean": args.hetero_cache_mean,
        "bandwidth_env": args.bandwidth_env,
    })

    save(obj, args.out)
    print(args.out)

if __name__ == "__main__":
    main()
