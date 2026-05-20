import argparse
import json
import re
from pathlib import Path

def read_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def metric(s, *names, default=0):
    for n in names:
        if n in s:
            return s[n]
    return default

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    root = Path(args.root)
    rows = []

    for p in sorted(root.rglob("*.json")):
        try:
            r = read_json(p)
        except Exception:
            continue

        s = r.get("summary", {})
        rel = str(p.relative_to(root))

        method = metric(s, "algo", default=p.stem)

        req = None
        cache = None
        catalog = None
        edge = None
        env = None

        for pat in [r"cache(\d+)_req(\d+)", r"_(\d+)mb_(\d+)", r"cache(\d+)_(\d+)"]:
            m = re.search(pat, rel)
            if m:
                cache = int(m.group(1))
                req = int(m.group(2))
                break

        m = re.search(r"req(\d+)", rel)
        if m:
            req = int(m.group(1))

        m = re.search(r"cat(\d+)", rel)
        if m:
            catalog = int(m.group(1))

        m = re.search(r"edge(\d+)", rel)
        if m:
            edge = int(m.group(1))

        for e in ["homo_good", "homo_bad", "hetero_good", "hetero_bad"]:
            if e in rel:
                env = e

        rows.append({
            "file": rel,
            "method": method,
            "catalog": catalog,
            "edge_nodes": edge,
            "cache": cache,
            "requests": req,
            "bandwidth_env": env,
            "ACT": metric(s, "ACT", default=0),
            "AMS": metric(s, "AMS", default=0),
            "objective_base": metric(s, "objective_base", "objective_without_ca_penalty", default=metric(s, "objective", default=0)),
            "CA": metric(s, "ca_triggered", "num_failed", default=0),
            "CA_rate": metric(s, "ca_rate", "fail_rate", default=0),
            "objective_CA": metric(s, "objective_ca", "objective", default=0),
            "downloaded_mb": metric(s, "downloaded_mb", default=0),
            "reused_mb": metric(s, "reused_mb", default=0),
            "reuse_rate": metric(s, "reuse_rate", default=0),
        })

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    cols = [
        "file", "method", "catalog", "edge_nodes", "cache", "requests", "bandwidth_env",
        "ACT", "AMS", "objective_base", "CA", "CA_rate", "objective_CA",
        "downloaded_mb", "reused_mb", "reuse_rate",
    ]

    with open(out, "w", encoding="utf-8") as f:
        f.write(",".join(cols) + "\n")
        for r in rows:
            f.write(",".join(str(r.get(c, "")) for c in cols) + "\n")

    print("written", out, "rows", len(rows))

if __name__ == "__main__":
    main()
