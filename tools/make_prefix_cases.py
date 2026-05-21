import json
import copy
import argparse
from pathlib import Path

def load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(obj, p):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def patch_cache(nodes, cache_mb):
    for n in nodes:
        hit = False
        for k in ["repo_capacity_mb", "cache_capacity_mb", "cache_size_mb", "cache_mb"]:
            if k in n:
                n[k] = int(cache_mb)
                hit = True
        if not hit:
            n["repo_capacity_mb"] = int(cache_mb)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--catalog-size", type=int, default=88)
    ap.add_argument("--cache-mb", type=int, default=1024)
    ap.add_argument("--reqs", nargs="+", type=int, required=True)
    args = ap.parse_args()

    base = load_json(args.base)
    containers = base["containers"]

    max_req = max(args.reqs)
    if len(containers) < max_req:
        raise RuntimeError(f"base has only {len(containers)} containers, but max_req={max_req}")

    for req in args.reqs:
        obj = copy.deepcopy(base)
        obj["containers"] = copy.deepcopy(containers[:req])

        for k in ["num_containers", "n_containers", "requests", "num_requests", "n_requests"]:
            if k in obj:
                obj[k] = req

        if "nodes" in obj:
            patch_cache(obj["nodes"], args.cache_mb)

        obj["prefix_meta"] = {
            "source_base": args.base,
            "prefix_req": req,
            "cache_mb": args.cache_mb,
            "catalog_size": args.catalog_size,
            "strict_prefix": True
        }

        out = Path(args.out_dir) / f"drtp_img{args.catalog_size}_cacheonly_{args.cache_mb}mb_{req}.json"
        save_json(obj, out)
        print("written", out)

if __name__ == "__main__":
    main()
