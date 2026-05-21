import argparse
import copy
import json
from pathlib import Path

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
    args = ap.parse_args()

    obj = load_json(args.base)
    obj["nodes"] = copy.deepcopy(obj["nodes"])

    # Fig.2 当前口径：不跑异构，每个节点 cache 都设为 M
    for n in obj["nodes"]:
        n["repo_capacity_mb"] = int(args.cache_mb)

    obj["variant_meta"] = {
        "base": args.base,
        "cache_mode": "homo",
        "cache_mb": args.cache_mb,
        "fig": "fig2_dynamic_gain"
    }

    save_json(obj, args.out)

if __name__ == "__main__":
    main()
