import argparse
import json
from collections import defaultdict
from pathlib import Path

PATTERNS = [
    ("cpu_heavy",  {"cpu": 1.70, "mem": 1.20, "disk": 2.20}),
    ("mem_heavy",  {"cpu": 0.45, "mem": 4.40, "disk": 2.20}),
    ("disk_heavy", {"cpu": 0.45, "mem": 1.20, "disk": 9.00}),
    ("balanced",   {"cpu": 0.90, "mem": 2.50, "disk": 4.50}),
]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--n", type=int, default=200)
    args = ap.parse_args()

    obj = json.load(open(args.base, "r", encoding="utf-8"))

    containers = obj["containers"][:args.n]
    containers = sorted(containers, key=lambda x: x["cid"])

    for idx, c in enumerate(containers):
        tag, res = PATTERNS[idx % len(PATTERNS)]
        c["resources"] = {
            q: round(v * args.scale, 4)
            for q, v in res.items()
        }
        c["resource_type"] = tag
        c["service_type"] = c.get("service_type", c.get("image_type", "default")) + "_" + tag

    obj["containers"] = containers
    obj["stress_info"] = {
        "type": "k8s_resource_fragmentation_stress",
        "scale": args.scale,
        "n": len(containers),
        "patterns": PATTERNS,
    }

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    json.dump(obj, open(args.out, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

    total_req = defaultdict(float)
    for c in containers:
        for q, v in c["resources"].items():
            total_req[q] += float(v)

    total_cap = defaultdict(float)
    for node in obj["nodes"]:
        for q, v in node["resources"].items():
            total_cap[q] += float(v)

    print("written:", args.out)
    print("| resource | total_req | total_cap | req/cap |")
    print("|---|---:|---:|---:|")
    for q in sorted(total_cap):
        print(f"| {q} | {total_req[q]:.3f} | {total_cap[q]:.3f} | {total_req[q]/total_cap[q]:.3f} |")

if __name__ == "__main__":
    main()
