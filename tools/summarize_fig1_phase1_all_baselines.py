import json
import re
import math
from pathlib import Path
from collections import defaultdict

ROOT = Path("results/drtp/k8s_same_scale")
RES_DIR = ROOT / "fig5_overall"
OUT = ROOT / "tables_delay_frag"
OUT.mkdir(parents=True, exist_ok=True)

METHODS = ["ILR-SA", "LRScheduler", "GAHRL", "ORR", "LASA", "FG-orig", "FG-selected"]

LAMBDA_DELAY = 1.0
LAMBDA_FRAG = 1.0
LAMBDA_AFF = 0.2
LAMBDA_LOAD = 1.0

def load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

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

def case_path_from_req(req):
    return Path(f"cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_{req}.json")

def get_assignment(result):
    if isinstance(result.get("assignment"), dict):
        return result["assignment"]
    if isinstance(result.get("assignments"), dict):
        return result["assignments"]
    return {}

def resource_fragmentation_and_load(case, result):
    assignment = get_assignment(result)

    containers = {c.get("cid"): c for c in case.get("containers", [])}
    nodes = {n.get("eid"): n for n in case.get("nodes", [])}

    node_frag_scores = []
    node_loads = []

    for eid, cids in assignment.items():
        if eid not in nodes:
            continue

        caps = nodes[eid].get("resources", {})
        if not caps:
            continue

        pressures = []

        for r, cap in caps.items():
            cap = float(cap)
            if cap <= 0:
                continue

            used = 0.0
            for cid in cids:
                c = containers.get(cid)
                if c is None:
                    continue
                used += float(c.get("resources", {}).get(r, 0.0))

            pressures.append(used / cap)

        if not pressures:
            continue

        avg_pressure = sum(pressures) / len(pressures)

        # 节点内部多维资源碎片：CPU/mem/disk 压力越不均衡，碎片越高
        frag_v = sum(abs(x - avg_pressure) for x in pressures) / len(pressures)
        node_frag_scores.append(frag_v)

        # 节点整体负载压力
        node_loads.append(avg_pressure)

    frag_score = sum(node_frag_scores) / len(node_frag_scores) if node_frag_scores else 0.0

    if len(node_loads) <= 1:
        load_imbalance = 0.0
    else:
        mean_load = sum(node_loads) / len(node_loads)
        load_imbalance = sum((x - mean_load) ** 2 for x in node_loads) / len(node_loads)

    return frag_score, load_imbalance

def safe_mean_positive(rows, key):
    vals = [float(r[key]) for r in rows if float(r[key]) > 0]
    return sum(vals) / len(vals) if vals else 1.0

rows = []

for p in sorted(RES_DIR.glob("*.json")):
    m = re.search(r"req(\d+)", p.name)
    if not m:
        continue

    req = int(m.group(1))
    result = load_json(p)
    s = result.get("summary", {})

    cp = case_path_from_req(req)
    if not cp.exists():
        print("[MISSING CASE]", cp)
        continue

    case = load_json(cp)

    frag_score, load_imbalance = resource_fragmentation_and_load(case, result)

    act = float(s.get("ACT", 0.0))
    ams = float(s.get("AMS", 0.0))
    delay_raw = 0.5 * act + 0.5 * ams

    ca = float(s.get("ca_triggered", s.get("num_failed", 0.0)))
    ca_rate = float(s.get("ca_rate", s.get("fail_rate", 0.0)))

    rows.append({
        "requests": req,
        "method": method_name(p.name, s),
        "delay_raw": delay_raw,
        "frag_raw": frag_score,
        "aff_raw": float(s.get("reuse_rate", 0.0)),
        "load_raw": load_imbalance,
        "CA": ca,
        "CA_rate": ca_rate,
        "downloaded_mb": float(s.get("downloaded_mb", 0.0)),
        "reuse_rate": float(s.get("reuse_rate", 0.0)),
    })

# 全局归一化：保证不同项在同一量纲下进入势函数
mean_delay = safe_mean_positive(rows, "delay_raw")
mean_frag = safe_mean_positive(rows, "frag_raw")
mean_aff = safe_mean_positive(rows, "aff_raw")
mean_load = safe_mean_positive(rows, "load_raw")

for r in rows:
    r["delay_term"] = r["delay_raw"] / mean_delay
    r["frag_term"] = r["frag_raw"] / mean_frag
    r["aff_reward_term"] = r["aff_raw"] / mean_aff
    r["load_term"] = r["load_raw"] / mean_load if mean_load > 0 else 0.0

    r["phi_total"] = (
        LAMBDA_DELAY * r["delay_term"]
        + LAMBDA_FRAG * r["frag_term"]
        - LAMBDA_AFF * r["aff_reward_term"]
        + LAMBDA_LOAD * r["load_term"]
    )

def method_order(m):
    return METHODS.index(m) if m in METHODS else 99

def write(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("written", path)

# 详细表
lines = [
    "## Fig.1 Phase-1 Potential Terms: All Baselines",
    "",
    "| requests | method | phi_total | delay_term | frag_term | aff_reward_term | load_term | delay_raw | frag_raw | load_raw | CA | CA_rate | downloaded_mb | reuse_rate |",
    "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
]

for r in sorted(rows, key=lambda x: (x["requests"], method_order(x["method"]))):
    lines.append(
        f"| {r['requests']} | {r['method']} | "
        f"{r['phi_total']:.6f} | {r['delay_term']:.6f} | {r['frag_term']:.6f} | "
        f"{r['aff_reward_term']:.6f} | {r['load_term']:.6f} | "
        f"{r['delay_raw']:.3f} | {r['frag_raw']:.6f} | {r['load_raw']:.6f} | "
        f"{r['CA']:.0f} | {r['CA_rate']:.4f} | "
        f"{r['downloaded_mb']:.0f} | {r['reuse_rate']:.6f} |"
    )

write(OUT / "fig1_phase1_all_baselines_detail.md", lines)

# 矩阵表
def write_matrix(key, title, filename, fmt="{:.6f}"):
    reqs = sorted(set(r["requests"] for r in rows))
    methods = [m for m in METHODS if any(r["method"] == m for r in rows)]

    lines = [f"## {title}", ""]
    lines.append("| requests | " + " | ".join(methods) + " |")
    lines.append("|---:|" + "|".join(["---:"] * len(methods)) + "|")

    for req in reqs:
        vals = []
        for m in methods:
            hit = [r for r in rows if r["requests"] == req and r["method"] == m]
            vals.append("" if not hit else fmt.format(hit[0][key]))
        lines.append(f"| {req} | " + " | ".join(vals) + " |")

    write(OUT / filename, lines)

write_matrix("phi_total", "Fig.1 Phase-1 Potential: phi_total", "fig1_phi_total_matrix.md")
write_matrix("delay_term", "Fig.1 Phase-1 Potential: delay_term", "fig1_delay_term_matrix.md")
write_matrix("frag_term", "Fig.1 Phase-1 Potential: frag_term", "fig1_frag_term_matrix.md")
write_matrix("aff_reward_term", "Fig.1 Phase-1 Potential: aff_reward_term", "fig1_aff_reward_term_matrix.md")
write_matrix("load_term", "Fig.1 Phase-1 Potential: load_term", "fig1_load_term_matrix.md")
write_matrix("CA_rate", "Fig.1 Phase-1 Potential: CA_rate", "fig1_ca_rate_matrix.md", "{:.4f}")

# 平均表
acc = defaultdict(list)
for r in rows:
    acc[r["method"]].append(r)

avg_rows = []
for m, rs in acc.items():
    avg_rows.append({
        "method": m,
        "phi_total": sum(x["phi_total"] for x in rs) / len(rs),
        "delay_term": sum(x["delay_term"] for x in rs) / len(rs),
        "frag_term": sum(x["frag_term"] for x in rs) / len(rs),
        "aff_reward_term": sum(x["aff_reward_term"] for x in rs) / len(rs),
        "load_term": sum(x["load_term"] for x in rs) / len(rs),
        "CA_rate": sum(x["CA_rate"] for x in rs) / len(rs),
        "n": len(rs),
    })

avg_rows.sort(key=lambda x: x["phi_total"])

lines = [
    "## Fig.1 Phase-1 Potential Average: All Baselines",
    "",
    "| method | avg_phi_total ↓ | avg_delay_term ↓ | avg_frag_term ↓ | avg_aff_reward_term ↑ | avg_load_term ↓ | avg_CA_rate ↓ | n |",
    "|---|---:|---:|---:|---:|---:|---:|---:|",
]
for x in avg_rows:
    lines.append(
        f"| {x['method']} | {x['phi_total']:.6f} | {x['delay_term']:.6f} | "
        f"{x['frag_term']:.6f} | {x['aff_reward_term']:.6f} | "
        f"{x['load_term']:.6f} | {x['CA_rate']:.4f} | {x['n']} |"
    )

write(OUT / "fig1_phase1_all_baselines_avg.md", lines)

print()
print("mean_delay =", mean_delay)
print("mean_frag =", mean_frag)
print("mean_aff =", mean_aff)
print("mean_load =", mean_load)
