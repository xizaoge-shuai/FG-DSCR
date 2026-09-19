import json
import re
import os
from pathlib import Path
from collections import defaultdict

ROOT = Path("results/drtp/k8s_same_scale")
OUT = ROOT / "tables_delay_frag"
OUT.mkdir(parents=True, exist_ok=True)

ALPHA = float(os.environ.get("ALPHA", "0.5"))
LAMBDA_CA = float(os.environ.get("LAMBDA_CA", "1000"))
LAMBDA_FRAG = float(os.environ.get("LAMBDA_FRAG", "100"))

METHODS = [
    "ILR-SA",
    "LRScheduler",
    "GAHRL",
    "ORR",
    "LASA",
    "FG-orig",
    "FG-selected",
]

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
    if fn.startswith("fg_") or fn.startswith("fg."):
        return "FG-selected"
    if fn.startswith("wo_frag"):
        return "FG-w/o-frag"

    return fn

def get_case_for_result(fig, p):
    name = p.name

    if fig in ("fig5", "fig1"):
        m = re.search(r"req(\d+)", name)
        if not m:
            return None
        req = int(m.group(1))
        return Path(f"cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_{req}.json")

    if fig == "fig2":
        m1 = re.search(r"cache(\d+)", name)
        m2 = re.search(r"req(\d+)", name)
        if not m1 or not m2:
            return None
        cache = int(m1.group(1))
        req = int(m2.group(1))
        return ROOT / f"cases/fig2_cache/case_cache{cache}_req{req}.json"

    if fig == "fig3":
        m = re.search(r"cat(\d+)_edge(\d+)_req(\d+)", name)
        if not m:
            return None
        cat, edge, req = map(int, m.groups())
        return Path(f"cases/drtp_scale_nodes/drtp_img{cat}_nodes{edge}_cache1024mb_{req}.json")

    if fig == "fig4":
        env = None
        for x in ["homo_good", "homo_bad", "hetero_good", "hetero_bad"]:
            if x in name:
                env = x
                break
        m1 = re.search(r"cache(\d+)", name)
        m2 = re.search(r"req(\d+)", name)
        if not env or not m1 or not m2:
            return None
        cache = int(m1.group(1))
        req = int(m2.group(1))
        return ROOT / f"cases/fig4_network/case_{env}_cache{cache}_req{req}.json"

    return None

def fragmentation_score(case, result):
    assignment = result.get("assignment", {})
    if not assignment:
        return 0.0

    containers = {c.get("cid"): c for c in case.get("containers", [])}
    nodes = {n.get("eid"): n for n in case.get("nodes", [])}

    vals = []

    for eid, cids in assignment.items():
        if eid not in nodes:
            continue

        node = nodes[eid]
        caps = node.get("resources", {})
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
                if not c:
                    continue
                used += float(c.get("resources", {}).get(r, 0.0))

            pressures.append(used / cap)

        if not pressures:
            continue

        avg_p = sum(pressures) / len(pressures)
        frag_v = sum(abs(x - avg_p) for x in pressures) / len(pressures)
        vals.append(frag_v)

    return sum(vals) / len(vals) if vals else 0.0

def parse_common(fig, p):
    result = load_json(p)
    s = result.get("summary", {})
    case_path = get_case_for_result(fig, p)

    if case_path is not None and case_path.exists():
        case = load_json(case_path)
        frag = fragmentation_score(case, result)
    else:
        frag = 0.0

    act = float(s.get("ACT", 0.0))
    ams = float(s.get("AMS", 0.0))
    delay = ALPHA * act + (1.0 - ALPHA) * ams

    num_containers = float(s.get("num_containers", 0.0))
    ca = float(s.get("ca_triggered", s.get("num_failed", 0.0)))
    if "ca_rate" in s:
        ca_rate = float(s["ca_rate"])
    elif "fail_rate" in s:
        ca_rate = float(s["fail_rate"])
    elif num_containers > 0:
        ca_rate = ca / num_containers
    else:
        ca_rate = 0.0

    return {
        "file": p.name,
        "method": method_name(p.name, s),
        "ACT": act,
        "AMS": ams,
        "Delay": delay,
        "CA": ca,
        "CA_rate": ca_rate,
        "frag_score": frag,
        "downloaded_mb": float(s.get("downloaded_mb", 0.0)),
        "reused_mb": float(s.get("reused_mb", 0.0)),
        "reuse_rate": float(s.get("reuse_rate", 0.0)),
    }

def normalize_and_final(rows):
    nonzero = [r["frag_score"] for r in rows if r["frag_score"] > 0]
    mean_frag = sum(nonzero) / len(nonzero) if nonzero else 1.0

    for r in rows:
        r["frag_norm"] = r["frag_score"] / mean_frag if mean_frag > 0 else 0.0
        r["Obj_Final"] = (
            r["Delay"]
            + LAMBDA_CA * r["CA_rate"]
            + LAMBDA_FRAG * r["frag_norm"]
        )

    return rows

def write(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("written", path)

def avg(xs):
    return sum(xs) / len(xs) if xs else 0.0

def method_sort(m):
    return METHODS.index(m) if m in METHODS else 99

def write_method_matrix(rows, row_key, value_key, title, path, fmt="{:.3f}"):
    row_vals = sorted(set(r[row_key] for r in rows))
    methods = [m for m in METHODS if any(r["method"] == m for r in rows)]

    lines = [f"## {title}", ""]
    lines.append(f"| {row_key} | " + " | ".join(methods) + " |")
    lines.append("|---:|" + "|".join(["---:"] * len(methods)) + "|")

    for rv in row_vals:
        vals = []
        for m in methods:
            sub = [r[value_key] for r in rows if r[row_key] == rv and r["method"] == m]
            vals.append("" if not sub else fmt.format(avg(sub)))
        lines.append(f"| {rv} | " + " | ".join(vals) + " |")

    write(path, lines)

# ===================== Fig5 =====================
fig5_rows = []
for p in sorted((ROOT / "fig5_overall").glob("*.json")):
    r = parse_common("fig5", p)
    m = re.search(r"req(\d+)", p.name)
    if not m:
        continue
    r["requests"] = int(m.group(1))
    fig5_rows.append(r)

fig5_rows = normalize_and_final(fig5_rows)

write_method_matrix(
    fig5_rows,
    "requests",
    "Obj_Final",
    "Fig.5 Overall: Final Objective = Delay + CA + Fragmentation",
    OUT / "fig5_final_obj_matrix.md",
    "{:.3f}",
)

write_method_matrix(
    fig5_rows,
    "requests",
    "frag_score",
    "Fig.5 Overall: fragmentation_score",
    OUT / "fig5_frag_score_matrix.md",
    "{:.6f}",
)

write_method_matrix(
    fig5_rows,
    "requests",
    "CA_rate",
    "Fig.5 Overall: CA_rate",
    OUT / "fig5_ca_rate_matrix.md",
    "{:.4f}",
)

lines = [
    "## Fig.5 Overall Detail: Delay + CA + Fragmentation",
    "",
    "| requests | method | Delay | CA | CA_rate | frag_score | frag_norm | Obj_Final | downloaded_mb | reuse_rate |",
    "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
]
for r in sorted(fig5_rows, key=lambda x: (x["requests"], method_sort(x["method"]))):
    lines.append(
        f"| {r['requests']} | {r['method']} | {r['Delay']:.3f} | {r['CA']:.0f} | "
        f"{r['CA_rate']:.4f} | {r['frag_score']:.6f} | {r['frag_norm']:.3f} | "
        f"{r['Obj_Final']:.3f} | {r['downloaded_mb']:.0f} | {r['reuse_rate']:.6f} |"
    )
write(OUT / "fig5_final_detail.md", lines)

# average improvement
by_method = defaultdict(list)
for r in fig5_rows:
    by_method[r["method"]].append(r)

avg_rows = []
for m, rs in by_method.items():
    avg_rows.append({
        "method": m,
        "Obj_Final": avg([x["Obj_Final"] for x in rs]),
        "Delay": avg([x["Delay"] for x in rs]),
        "CA_rate": avg([x["CA_rate"] for x in rs]),
        "frag_score": avg([x["frag_score"] for x in rs]),
        "downloaded_mb": avg([x["downloaded_mb"] for x in rs]),
        "reuse_rate": avg([x["reuse_rate"] for x in rs]),
        "n": len(rs),
    })

avg_rows.sort(key=lambda x: x["Obj_Final"])
fg = next((x for x in avg_rows if x["method"] == "FG-selected"), avg_rows[0])
fg_obj = fg["Obj_Final"]

lines = [
    "## Fig.5 Average Final Objective and Improvement",
    "",
    "| method | Avg Obj_Final | Avg Delay | Avg CA_rate | Avg frag_score | Avg downloaded_mb | Avg reuse_rate | improvement vs FG-selected | n |",
    "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
]
for x in avg_rows:
    if x["method"] == "FG-selected":
        imp = "0.00%"
    else:
        imp_val = (x["Obj_Final"] - fg_obj) / x["Obj_Final"] * 100 if x["Obj_Final"] else 0
        imp = f"{imp_val:.2f}%"
    lines.append(
        f"| {x['method']} | {x['Obj_Final']:.3f} | {x['Delay']:.3f} | "
        f"{x['CA_rate']:.4f} | {x['frag_score']:.6f} | {x['downloaded_mb']:.1f} | "
        f"{x['reuse_rate']:.6f} | {imp} | {x['n']} |"
    )
write(OUT / "fig5_average_improvement.md", lines)

# ===================== Fig2 =====================
fig2_rows = []
for p in sorted((ROOT / "fig2_cache").glob("*.json")):
    r = parse_common("fig2", p)
    m1 = re.search(r"cache(\d+)", p.name)
    m2 = re.search(r"req(\d+)", p.name)
    if not m1 or not m2:
        continue
    r["cache_mb"] = int(m1.group(1))
    r["requests"] = int(m2.group(1))
    fig2_rows.append(r)

fig2_rows = normalize_and_final(fig2_rows)

write_method_matrix(
    fig2_rows,
    "cache_mb",
    "Obj_Final",
    "Fig.2 Cache Sensitivity: Final Objective",
    OUT / "fig2_final_obj_matrix.md",
    "{:.3f}",
)

write_method_matrix(
    fig2_rows,
    "cache_mb",
    "frag_score",
    "Fig.2 Cache Sensitivity: fragmentation_score",
    OUT / "fig2_frag_score_matrix.md",
    "{:.6f}",
)

write_method_matrix(
    fig2_rows,
    "cache_mb",
    "reuse_rate",
    "Fig.2 Cache Sensitivity: reuse_rate",
    OUT / "fig2_reuse_matrix.md",
    "{:.6f}",
)

# ===================== Fig3 =====================
fig3_rows = []
for p in sorted((ROOT / "fig3_scale").glob("*.json")):
    r = parse_common("fig3", p)
    m = re.search(r"cat(\d+)_edge(\d+)_req(\d+)", p.name)
    if not m:
        continue
    r["catalog_size"] = int(m.group(1))
    r["edge_nodes"] = int(m.group(2))
    r["requests"] = int(m.group(3))
    fig3_rows.append(r)

fig3_rows = normalize_and_final(fig3_rows)

cats = sorted(set(r["catalog_size"] for r in fig3_rows))
edges = sorted(set(r["edge_nodes"] for r in fig3_rows))
reqs = sorted(set(r["requests"] for r in fig3_rows))

def write_scale_matrix(value_key, title, path, fmt="{:.3f}"):
    lines = [f"## {title}", ""]
    for cat in cats:
        lines += [f"### catalog_size = {cat}", ""]
        lines.append("| edge_nodes | " + " | ".join(str(x) for x in reqs) + " |")
        lines.append("|---:|" + "|".join(["---:"] * len(reqs)) + "|")
        for e in edges:
            vals = []
            for req in reqs:
                sub = [
                    r[value_key]
                    for r in fig3_rows
                    if r["catalog_size"] == cat and r["edge_nodes"] == e and r["requests"] == req
                ]
                vals.append("" if not sub else fmt.format(sub[0]))
            lines.append(f"| {e} | " + " | ".join(vals) + " |")
        lines.append("")
    write(path, lines)

write_scale_matrix(
    "Obj_Final",
    "Fig.3 Scale Impact: Final Objective",
    OUT / "fig3_final_obj_matrix.md",
    "{:.3f}",
)

write_scale_matrix(
    "frag_score",
    "Fig.3 Scale Impact: fragmentation_score",
    OUT / "fig3_frag_score_matrix.md",
    "{:.6f}",
)

write_scale_matrix(
    "CA_rate",
    "Fig.3 Scale Impact: CA_rate",
    OUT / "fig3_ca_rate_matrix.md",
    "{:.4f}",
)

# ===================== Fig4 =====================
fig4_rows = []
for p in sorted((ROOT / "fig4_network").glob("*.json")):
    r = parse_common("fig4", p)

    env = None
    for x in ["homo_good", "homo_bad", "hetero_good", "hetero_bad"]:
        if x in p.name:
            env = x
            break

    m1 = re.search(r"cache(\d+)", p.name)
    m2 = re.search(r"req(\d+)", p.name)
    if not env or not m1 or not m2:
        continue

    r["scenario"] = env
    r["cache_mb"] = int(m1.group(1))
    r["requests"] = int(m2.group(1))
    fig4_rows.append(r)

fig4_rows = normalize_and_final(fig4_rows)

envs = ["homo_good", "homo_bad", "hetero_good", "hetero_bad"]
caches = sorted(set(r["cache_mb"] for r in fig4_rows))
reqs = sorted(set(r["requests"] for r in fig4_rows))

def write_network_matrix(value_key, title, path, fmt="{:.3f}"):
    lines = [f"## {title}", ""]
    for env in envs:
        lines += [f"### scenario = {env}", ""]
        lines.append("| cache_mb | " + " | ".join(str(x) for x in reqs) + " |")
        lines.append("|---:|" + "|".join(["---:"] * len(reqs)) + "|")
        for c in caches:
            vals = []
            for req in reqs:
                sub = [
                    r[value_key]
                    for r in fig4_rows
                    if r["scenario"] == env and r["cache_mb"] == c and r["requests"] == req
                ]
                vals.append("" if not sub else fmt.format(sub[0]))
            lines.append(f"| {c} | " + " | ".join(vals) + " |")
        lines.append("")
    write(path, lines)

write_network_matrix(
    "Obj_Final",
    "Fig.4 Network Environment: Final Objective",
    OUT / "fig4_final_obj_matrix.md",
    "{:.3f}",
)

write_network_matrix(
    "frag_score",
    "Fig.4 Network Environment: fragmentation_score",
    OUT / "fig4_frag_score_matrix.md",
    "{:.6f}",
)

write_network_matrix(
    "reuse_rate",
    "Fig.4 Network Environment: reuse_rate",
    OUT / "fig4_reuse_matrix.md",
    "{:.6f}",
)

# ===================== all tables =====================
all_files = [
    "fig5_average_improvement.md",
    "fig5_final_obj_matrix.md",
    "fig5_ca_rate_matrix.md",
    "fig5_frag_score_matrix.md",
    "fig5_final_detail.md",
    "fig2_final_obj_matrix.md",
    "fig2_frag_score_matrix.md",
    "fig2_reuse_matrix.md",
    "fig3_final_obj_matrix.md",
    "fig3_ca_rate_matrix.md",
    "fig3_frag_score_matrix.md",
    "fig4_final_obj_matrix.md",
    "fig4_frag_score_matrix.md",
    "fig4_reuse_matrix.md",
]

combined = []
for fn in all_files:
    p = OUT / fn
    if p.exists():
        combined.append(p.read_text(encoding="utf-8"))

write(OUT / "all_delay_frag_tables.md", ["\n\n".join(combined)])

print()
print("ALPHA =", ALPHA)
print("LAMBDA_CA =", LAMBDA_CA)
print("LAMBDA_FRAG =", LAMBDA_FRAG)
print("output_dir =", OUT)
