import json, os, glob, argparse
from collections import defaultdict

def load(p):
    return json.load(open(p, encoding="utf-8"))

def save(obj, p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(obj, open(p, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src-dir", default="cases/drtp_cache_only_sweep_88")
    ap.add_argument("--out-dir", default="cases/drtp_cache_only_sweep_88_k8s_feasible")
    ap.add_argument("--target-util", type=float, default=0.75)
    args = ap.parse_args()

    for p in sorted(glob.glob(os.path.join(args.src_dir, "*.json"))):
        obj = load(p)

        total_req = defaultdict(float)
        total_cap = defaultdict(float)

        for c in obj["containers"]:
            for q, v in c.get("resources", {}).items():
                total_req[q] += float(v)

        for n in obj["nodes"]:
            for q, v in n.get("resources", {}).items():
                total_cap[q] += float(v)

        scale = {}
        for q in ["cpu", "mem", "disk"]:
            req = total_req[q]
            cap = total_cap[q]
            if req <= 0 or cap <= 0:
                scale[q] = 1.0
            else:
                scale[q] = min(1.0, args.target_util * cap / req)

        for c in obj["containers"]:
            for q in ["cpu", "mem", "disk"]:
                if q in c.get("resources", {}):
                    c["resources"][q] = float(c["resources"][q]) * scale[q]

        out = os.path.join(args.out_dir, os.path.basename(p))
        save(obj, out)
        print("[OK]", out, "scale=", scale)

if __name__ == "__main__":
    main()
