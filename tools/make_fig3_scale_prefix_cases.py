import json
import copy
import glob
import argparse
from pathlib import Path

CATS = [16, 50, 68, 88]
EDGES = [4, 6, 8, 10, 12, 14, 16, 18]
REQS = [200, 400, 600, 800, 1000, 1200, 1500, 2000]

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

def find_base_case(cat, edge, max_req=2000):
    patterns = [
        f"cases/**/*img{cat}*nodes{edge}*cache1024*_{max_req}.json",
        f"cases/**/*img{cat}*nodes{edge}*1024*{max_req}.json",
        f"cases/**/*img{cat}*edge{edge}*cache1024*_{max_req}.json",
        f"cases/**/*img{cat}*edge{edge}*1024*{max_req}.json",
    ]

    hits = []
    for pat in patterns:
        hits.extend(glob.glob(pat, recursive=True))

    hits = sorted(set(hits))

    # 排除我们自己新生成的目录，避免二次递归污染
    hits = [h for h in hits if "drtp_scale_prefix" not in h]

    if not hits:
        return None

    # 优先用 scale_nodes / runtime_scale 这类更接近规模实验的 case
    priority = []
    for h in hits:
        score = 0
        if "drtp_scale_nodes" in h:
            score -= 100
        if "drtp_runtime_scale" in h:
            score -= 50
        if f"nodes{edge}" in h:
            score -= 20
        priority.append((score, h))

    priority.sort()
    return priority[0][1]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="cases/drtp_scale_prefix")
    ap.add_argument("--cache-mb", type=int, default=1024)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)

    print("| catalog | edge | base | status |")
    print("|---:|---:|---|---|")

    missing = []

    for cat in CATS:
        for edge in EDGES:
            base_path = find_base_case(cat, edge, max_req=2000)

            if base_path is None:
                print(f"| {cat} | {edge} |  | MISSING_BASE |")
                missing.append((cat, edge))
                continue

            base = load_json(base_path)
            containers = base.get("containers", [])

            if len(containers) < max(REQS):
                print(f"| {cat} | {edge} | {base_path} | BAD_BASE_ONLY_{len(containers)} |")
                missing.append((cat, edge))
                continue

            for req in REQS:
                obj = copy.deepcopy(base)
                obj["containers"] = copy.deepcopy(containers[:req])

                for k in ["num_containers", "n_containers", "requests", "num_requests", "n_requests"]:
                    if k in obj:
                        obj[k] = req

                if "nodes" in obj:
                    obj["nodes"] = copy.deepcopy(obj["nodes"][:edge])
                    patch_cache(obj["nodes"], args.cache_mb)

                obj["prefix_meta"] = {
                    "source_base": base_path,
                    "catalog_size": cat,
                    "edge_nodes": edge,
                    "prefix_req": req,
                    "cache_mb": args.cache_mb,
                    "strict_prefix": True,
                    "note": "Fig.3 scale prefix case generated from the same 2000-request base trace for each catalog-edge pair."
                }

                out = out_dir / f"drtp_img{cat}_nodes{edge}_cache{args.cache_mb}mb_{req}.json"
                save_json(obj, out)

            print(f"| {cat} | {edge} | {base_path} | OK |")

    if missing:
        print("\n[MISSING_OR_BAD_BASE]", missing)
        raise SystemExit(1)

if __name__ == "__main__":
    main()
