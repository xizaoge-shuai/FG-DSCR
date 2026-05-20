import json
import re
from pathlib import Path
from collections import defaultdict

ROOT = Path("results/drtp/k8s_same_scale/fig2_cache_homohetero")
OUT = Path("results/drtp/k8s_same_scale/tables_delay_frag")
OUT.mkdir(parents=True, exist_ok=True)

METHODS = ["ILR-SA", "LRScheduler", "GAHRL", "ORR", "LASA", "FG-orig", "FG-selected"]

def method_name(fn, s):
    algo = str(s.get("algo", ""))
    if "selected" in algo:
        return "FG-selected"
    if "orig" in algo:
        return "FG-orig"
    if "ILR" in algo:
        return "ILR-SA"
    if "LRScheduler" in algo:
        return "LRScheduler"
    if "GAHRL" in algo:
        return "GAHRL"
    if "ORR" in algo:
        return "ORR"
    if "LASA" in algo:
        return "LASA"
    if fn.startswith("ilrsa"):
        return "ILR-SA"
    if fn.startswith("lrs"):
        return "LRScheduler"
    if fn.startswith("gahrl"):
        return "GAHRL"
    if fn.startswith("orr"):
        return "ORR"
    if fn.startswith("lasa"):
        return "LASA"
    if fn.startswith("fg_orig"):
        return "FG-orig"
    if fn.startswith("fg_"):
        return "FG-selected"
    return fn

rows = []
for p in sorted(ROOT.glob("*.json")):
    m = re.search(r"_(homo|hetero)_cache(\d+)_req(\d+)", p.name)
    if not m:
        continue

    mode, cache, req = m.group(1), int(m.group(2)), int(m.group(3))
    r = json.load(open(p, "r", encoding="utf-8"))
    s = r["summary"]

    rows.append({
        "mode": mode,
        "cache": cache,
        "req": req,
        "method": method_name(p.name, s),
        "Delay": 0.5 * float(s.get("ACT", 0)) + 0.5 * float(s.get("AMS", 0)),
        "Obj_Final": float(s.get("objective", s.get("objective_ca", 0))),
        "CA_rate": float(s.get("ca_rate", s.get("fail_rate", 0))),
        "downloaded_mb": float(s.get("downloaded_mb", 0)),
        "reuse_rate": float(s.get("reuse_rate", 0)),
    })

def avg(xs):
    return sum(xs) / len(xs) if xs else None

def write_matrix(mode, key, title, out_name, fmt):
    caches = sorted(set(x["cache"] for x in rows if x["mode"] == mode))
    lines = [f"## {title} ({mode})", ""]
    lines.append("| cache_mb | " + " | ".join(METHODS) + " |")
    lines.append("|---:|" + "|".join(["---:"] * len(METHODS)) + "|")

    for c in caches:
        vals = []
        for m in METHODS:
            sub = [x[key] for x in rows if x["mode"] == mode and x["cache"] == c and x["method"] == m]
            v = avg(sub)
            vals.append("" if v is None else fmt.format(v))
        lines.append(f"| {c} | " + " | ".join(vals) + " |")

    p = OUT / out_name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("written", p)

for mode in ["homo", "hetero"]:
    write_matrix(mode, "Obj_Final", "Fig.2 Cache Sensitivity: Final Objective", f"fig2_{mode}_final_obj_matrix.md", "{:.3f}")
    write_matrix(mode, "CA_rate", "Fig.2 Cache Sensitivity: CA_rate", f"fig2_{mode}_ca_rate_matrix.md", "{:.4f}")
    write_matrix(mode, "downloaded_mb", "Fig.2 Cache Sensitivity: downloaded_mb", f"fig2_{mode}_downloaded_matrix.md", "{:.1f}")
    write_matrix(mode, "reuse_rate", "Fig.2 Cache Sensitivity: reuse_rate", f"fig2_{mode}_reuse_matrix.md", "{:.6f}")
