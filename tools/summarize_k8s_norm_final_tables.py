import json
import re
import os
from pathlib import Path
from collections import defaultdict

ROOT = Path("results/drtp/k8s_same_scale")
OUT = ROOT / "tables_norm_final"
OUT.mkdir(parents=True, exist_ok=True)

W_DELAY = float(os.environ.get("W_DELAY", "1.0"))
W_CA = float(os.environ.get("W_CA", "1.0"))
W_FRAG = float(os.environ.get("W_FRAG", "1.0"))

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
    if fn.startswith("fg_"):
        return "FG-selected"

    return fn

def get_assignment(result):
    if isinstance(result.get("assignment"), dict):
        return result["assignment"]
    if isinstance(result.get("assignments"), dict):
        return result["assignments"]
    return {}

def fragmentation_score(case, result):
    assignment = get_assignment(result)
    containers = {str(c.get("cid")): c for c in case.get("containers", [])}
    nodes = {str(n.get("eid")): n for n in case.get("nodes", [])}

    vals = []

    for eid, cids in assignment.items():
        eid = str(eid)
        if eid not in nodes:
            continue

        caps = nodes[eid].get("resources", {})
        pressures = []

        for r, cap in caps.items():
            cap = float(cap)
            if cap <= 0:
                continue

            used = 0.0
            for cid in cids:
                c = containers.get(str(cid))
                if c is None:
                    continue
                used += float(c.get("resources", {}).get(r, 0.0))

            pressures.append(used / cap)

        if not pressures:
            continue

        avg_p = sum(pressures) / len(pressures)
        frag_v = sum(abs(x - avg_p) for x in pressures) / len(pressures)
        vals.append(frag_v)

    return sum(vals) / len(vals) if vals else 0.0

def case_for_fig5_or_fig1(req):
    return Path(f"cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_{req}.json")

def case_for_fig3(cat, edge, req):
    return Path(f"cases/drtp_scale_nodes/drtp_img{cat}_nodes{edge}_cache1024mb_{req}.json")

def case_for_fig4(env, cache, req):
    return ROOT / "cases" / "fig4_network" / f"case_{env}_cache{cache}_req{req}.json"

def parse_summary(result):
    s = result.get("summary", {})
    act = float(s.get("ACT", 0.0))
    ams = float(s.get("AMS", 0.0))
    delay = 0.5 * act + 0.5 * ams

    ca = float(s.get("ca_triggered", s.get("num_failed", 0.0)))
    if "ca_rate" in s:
        ca_rate = float(s["ca_rate"])
    elif "fail_rate" in s:
        ca_rate = float(s["fail_rate"])
    else:
        n = float(s.get("num_containers", 0.0))
        ca_rate = ca / n if n > 0 else 0.0

    return {
        "Delay_raw": delay,
        "CA": ca,
        "CA_rate": ca_rate,
        "downloaded_mb": float(s.get("downloaded_mb", 0.0)),
        "reuse_rate": float(s.get("reuse_rate", 0.0)),
    }

def mean_positive(rows, key):
    vals = [float(r[key]) for r in rows if float(r[key]) > 0]
    return sum(vals) / len(vals) if vals else 1.0

def normalize_rows(rows):
    mean_delay = mean_positive(rows, "Delay_raw")
    mean_ca = mean_positive(rows, "CA_rate")
    mean_frag = mean_positive(rows, "frag_score")

    for r in rows:
        r["Delay_norm"] = r["Delay_raw"] / mean_delay if mean_delay > 0 else 0.0
        r["CA_norm"] = r["CA_rate"] / mean_ca if mean_ca > 0 else 0.0
        r["Frag_norm"] = r["frag_score"] / mean_frag if mean_frag > 0 else 0.0
        r["Obj_Final"] = (
            W_DELAY * r["Delay_norm"]
            + W_CA * r["CA_norm"]
            + W_FRAG * r["Frag_norm"]
        )

    return rows, {
        "mean_delay": mean_delay,
        "mean_ca": mean_ca,
        "mean_frag": mean_frag,
    }

def write(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("written", path)

def avg(xs):
    return sum(xs) / len(xs) if xs else 0.0

def method_order(m):
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

# =========================
# Fig.5 Overall
# =========================
fig5_rows = []

for p in sorted((ROOT / "fig5_overall").glob("*.json")):
    m = re.search(r"req(\d+)", p.name)
    if not m:
        continue

    req = int(m.group(1))
    result = load_json(p)
    s = result.get("summary", {})
    case_path = case_for_fig5_or_fig1(req)

    if not case_path.exists():
        print("[MISSING CASE]", case_path)
        continue

    case = load_json(case_path)

    r = parse_summary(result)
    r.update({
        "requests": req,
        "method": method_name(p.name, s),
        "frag_score": fragmentation_score(case, result),
    })
    fig5_rows.append(r)

fig5_rows, fig5_scale = normalize_rows(fig5_rows)

write_method_matrix(
    fig5_rows,
    "requests",
    "Obj_Final",
    "Fig.5 Overall: Normalized Final Objective",
    OUT / "fig5_norm_obj_matrix.md",
    "{:.3f}",
)

write_method_matrix(
    fig5_rows,
    "requests",
    "Delay_norm",
    "Fig.5 Overall: Delay_norm",
    OUT / "fig5_delay_norm_matrix.md",
    "{:.3f}",
)

write_method_matrix(
    fig5_rows,
    "requests",
    "CA_norm",
    "Fig.5 Overall: CA_norm",
    OUT / "fig5_ca_norm_matrix.md",
    "{:.3f}",
)

write_method_matrix(
    fig5_rows,
    "requests",
    "Frag_norm",
    "Fig.5 Overall: Frag_norm",
    OUT / "fig5_frag_norm_matrix.md",
    "{:.3f}",
)

# Fig.5 detail
lines = [
    "## Fig.5 Overall Detail: Normalized Final Objective",
    "",
    "| requests | method | edge_scheduled | edge_scheduled_rate | Delay_raw | Delay_norm | CA | CA_rate | CA_norm | frag_score | Frag_norm | Obj_Final | downloaded_mb | reuse_rate |",
    "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
]

for r in sorted(fig5_rows, key=lambda x: (x["requests"], method_order(x["method"]))):
    edge_scheduled = r["requests"] - r["CA"]
    edge_scheduled_rate = edge_scheduled / r["requests"] if r["requests"] > 0 else 0.0

    lines.append(
        f"| {r['requests']} | {r['method']} | {edge_scheduled:.0f} | {edge_scheduled_rate:.4f} | "
        f"{r['Delay_raw']:.3f} | {r['Delay_norm']:.3f} | {r['CA']:.0f} | {r['CA_rate']:.4f} | "
        f"{r['CA_norm']:.3f} | {r['frag_score']:.6f} | {r['Frag_norm']:.3f} | "
        f"{r['Obj_Final']:.3f} | {r['downloaded_mb']:.0f} | {r['reuse_rate']:.6f} |"
    )

write(OUT / "fig5_norm_detail.md", lines)

# Fig.5 average improvement
acc = defaultdict(list)
for r in fig5_rows:
    acc[r["method"]].append(r)

avg_rows = []
for method, rs in acc.items():
    avg_rows.append({
        "method": method,
        "Obj_Final": avg([x["Obj_Final"] for x in rs]),
        "Delay_norm": avg([x["Delay_norm"] for x in rs]),
        "CA_norm": avg([x["CA_norm"] for x in rs]),
        "Frag_norm": avg([x["Frag_norm"] for x in rs]),
        "Delay_raw": avg([x["Delay_raw"] for x in rs]),
        "CA_rate": avg([x["CA_rate"] for x in rs]),
        "frag_score": avg([x["frag_score"] for x in rs]),
        "edge_scheduled_rate": avg([(x["requests"] - x["CA"]) / x["requests"] for x in rs]),
        "downloaded_mb": avg([x["downloaded_mb"] for x in rs]),
        "reuse_rate": avg([x["reuse_rate"] for x in rs]),
        "n": len(rs),
    })

avg_rows.sort(key=lambda x: x["Obj_Final"])
fg = next((x for x in avg_rows if x["method"] == "FG-selected"), avg_rows[0])
fg_obj = fg["Obj_Final"]

lines = [
    "## Fig.5 Average: Normalized Final Objective and Improvement",
    "",
    "| method | Avg Obj_Final ↓ | Avg Delay_norm ↓ | Avg CA_norm ↓ | Avg Frag_norm ↓ | Avg Delay_raw | Avg CA_rate | Avg frag_score | Avg edge_scheduled_rate | Avg downloaded_mb | Avg reuse_rate | improvement vs FG-selected | n |",
    "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
]

for x in avg_rows:
    if x["method"] == "FG-selected":
        imp = "0.00%"
    else:
        imp_val = (x["Obj_Final"] - fg_obj) / x["Obj_Final"] * 100 if x["Obj_Final"] > 0 else 0.0
        imp = f"{imp_val:.2f}%"

    lines.append(
        f"| {x['method']} | {x['Obj_Final']:.3f} | {x['Delay_norm']:.3f} | {x['CA_norm']:.3f} | "
        f"{x['Frag_norm']:.3f} | {x['Delay_raw']:.3f} | {x['CA_rate']:.4f} | "
        f"{x['frag_score']:.6f} | {x['edge_scheduled_rate']:.4f} | "
        f"{x['downloaded_mb']:.1f} | {x['reuse_rate']:.6f} | {imp} | {x['n']} |"
    )

write(OUT / "fig5_norm_average_improvement.md", lines)

# =========================
# Fig.3 Scale
# =========================
fig3_rows = []

for p in sorted((ROOT / "fig3_scale").glob("*.json")):
    m = re.search(r"cat(\d+)_edge(\d+)_req(\d+)", p.name)
    if not m:
        continue

    cat, edge, req = map(int, m.groups())
    result = load_json(p)
    s = result.get("summary", {})
    case_path = case_for_fig3(cat, edge, req)

    if not case_path.exists():
        print("[MISSING CASE]", case_path)
        continue

    case = load_json(case_path)

    r = parse_summary(result)
    r.update({
        "catalog_size": cat,
        "edge_nodes": edge,
        "requests": req,
        "method": method_name(p.name, s),
        "frag_score": fragmentation_score(case, result),
    })
    fig3_rows.append(r)

fig3_rows, fig3_scale = normalize_rows(fig3_rows)

def write_scale_matrix(rows, value_key, title, path, fmt="{:.3f}"):
    cats = sorted(set(r["catalog_size"] for r in rows))
    edges = sorted(set(r["edge_nodes"] for r in rows))
    reqs = sorted(set(r["requests"] for r in rows))

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
                    for r in rows
                    if r["catalog_size"] == cat and r["edge_nodes"] == e and r["requests"] == req
                ]
                vals.append("" if not sub else fmt.format(sub[0]))
            lines.append(f"| {e} | " + " | ".join(vals) + " |")

        lines.append("")

    write(path, lines)

write_scale_matrix(
    fig3_rows,
    "Obj_Final",
    "Fig.3 Scale Impact: Normalized Final Objective",
    OUT / "fig3_norm_obj_matrix.md",
    "{:.3f}",
)

write_scale_matrix(
    fig3_rows,
    "Delay_norm",
    "Fig.3 Scale Impact: Delay_norm",
    OUT / "fig3_delay_norm_matrix.md",
    "{:.3f}",
)

write_scale_matrix(
    fig3_rows,
    "CA_norm",
    "Fig.3 Scale Impact: CA_norm",
    OUT / "fig3_ca_norm_matrix.md",
    "{:.3f}",
)

write_scale_matrix(
    fig3_rows,
    "Frag_norm",
    "Fig.3 Scale Impact: Frag_norm",
    OUT / "fig3_frag_norm_matrix.md",
    "{:.3f}",
)

# =========================
# Fig.4 Network
# =========================
fig4_rows = []

for p in sorted((ROOT / "fig4_network").glob("*.json")):
    env = None
    for x in ["homo_good", "homo_bad", "hetero_good", "hetero_bad"]:
        if x in p.name:
            env = x
            break

    m1 = re.search(r"cache(\d+)", p.name)
    m2 = re.search(r"req(\d+)", p.name)

    if not env or not m1 or not m2:
        continue

    cache = int(m1.group(1))
    req = int(m2.group(1))

    # 当前图四只保留 0-1024
    if cache > 1024:
        continue

    result = load_json(p)
    s = result.get("summary", {})
    case_path = case_for_fig4(env, cache, req)

    if not case_path.exists():
        print("[MISSING CASE]", case_path)
        continue

    case = load_json(case_path)

    r = parse_summary(result)
    r.update({
        "scenario": env,
        "cache_mb": cache,
        "requests": req,
        "method": method_name(p.name, s),
        "frag_score": fragmentation_score(case, result),
    })
    fig4_rows.append(r)

fig4_rows, fig4_scale = normalize_rows(fig4_rows)

def write_network_matrix(rows, value_key, title, path, fmt="{:.3f}"):
    envs = ["homo_good", "homo_bad", "hetero_good", "hetero_bad"]
    caches = sorted(set(r["cache_mb"] for r in rows))
    reqs = sorted(set(r["requests"] for r in rows))

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
                    for r in rows
                    if r["scenario"] == env and r["cache_mb"] == c and r["requests"] == req
                ]
                vals.append("" if not sub else fmt.format(sub[0]))
            lines.append(f"| {c} | " + " | ".join(vals) + " |")

        lines.append("")

    write(path, lines)

write_network_matrix(
    fig4_rows,
    "Obj_Final",
    "Fig.4 Network Environment: Normalized Final Objective",
    OUT / "fig4_norm_obj_matrix.md",
    "{:.3f}",
)

write_network_matrix(
    fig4_rows,
    "Delay_norm",
    "Fig.4 Network Environment: Delay_norm",
    OUT / "fig4_delay_norm_matrix.md",
    "{:.3f}",
)

write_network_matrix(
    fig4_rows,
    "CA_norm",
    "Fig.4 Network Environment: CA_norm",
    OUT / "fig4_ca_norm_matrix.md",
    "{:.3f}",
)

write_network_matrix(
    fig4_rows,
    "Frag_norm",
    "Fig.4 Network Environment: Frag_norm",
    OUT / "fig4_frag_norm_matrix.md",
    "{:.3f}",
)

write_network_matrix(
    fig4_rows,
    "reuse_rate",
    "Fig.4 Network Environment: reuse_rate",
    OUT / "fig4_reuse_matrix.md",
    "{:.6f}",
)

# =========================
# Combined
# =========================
all_files = [
    "fig5_norm_average_improvement.md",
    "fig5_norm_obj_matrix.md",
    "fig5_delay_norm_matrix.md",
    "fig5_ca_norm_matrix.md",
    "fig5_frag_norm_matrix.md",
    "fig5_norm_detail.md",
    "fig3_norm_obj_matrix.md",
    "fig3_delay_norm_matrix.md",
    "fig3_ca_norm_matrix.md",
    "fig3_frag_norm_matrix.md",
    "fig4_norm_obj_matrix.md",
    "fig4_delay_norm_matrix.md",
    "fig4_ca_norm_matrix.md",
    "fig4_frag_norm_matrix.md",
    "fig4_reuse_matrix.md",
]

combined = []
for fn in all_files:
    p = OUT / fn
    if p.exists():
        combined.append(p.read_text(encoding="utf-8"))

write(OUT / "all_norm_final_tables.md", ["\n\n".join(combined)])

print()
print("Weights:")
print("  W_DELAY =", W_DELAY)
print("  W_CA    =", W_CA)
print("  W_FRAG  =", W_FRAG)
print()
print("Fig5 normalization scale:", fig5_scale)
print("Fig3 normalization scale:", fig3_scale)
print("Fig4 normalization scale:", fig4_scale)
print()
print("output_dir =", OUT)
