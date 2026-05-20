import json
import re
from pathlib import Path
from collections import defaultdict

ROOT = Path("results/drtp/k8s_same_scale")
OUT = ROOT / "tables"
OUT.mkdir(parents=True, exist_ok=True)

def load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def get_summary(p):
    r = load_json(p)
    return r.get("summary", {}), r

def metric(s, *keys, default=0.0):
    for k in keys:
        if k in s and s[k] is not None:
            return s[k]
    return default

def method_name(file_name, summary):
    algo = summary.get("algo", "")
    if algo:
        if "selected" in algo:
            return "FG-DSCR-GC-selected"
        if "orig" in algo:
            return "FG-DSCR-GC-orig"
        if "w-o-frag" in algo or "wo-frag" in algo:
            return "FG-w/o-frag"
        if "ILR" in algo:
            return "ILR-SA"
        if "LRScheduler" in algo:
            return "LRScheduler-inspired"
        if "GAHRL" in algo:
            return "GAHRL-inspired"
        if "ORR" in algo:
            return "ORR-inspired"
        if "LASA" in algo:
            return "LASA-reimpl"
    if file_name.startswith("ilrsa"):
        return "ILR-SA"
    if file_name.startswith("lrs"):
        return "LRScheduler-inspired"
    if file_name.startswith("gahrl"):
        return "GAHRL-inspired"
    if file_name.startswith("orr"):
        return "ORR-inspired"
    if file_name.startswith("lasa"):
        return "LASA-reimpl"
    if file_name.startswith("fg_orig"):
        return "FG-DSCR-GC-orig"
    if file_name.startswith("fg_") or file_name.startswith("fg."):
        return "FG-DSCR-GC-selected"
    if file_name.startswith("wo_frag"):
        return "FG-w/o-frag"
    return file_name

def row_from_json(p):
    s, r = get_summary(p)
    fn = p.name
    row = {
        "file": fn,
        "method": method_name(fn, s),
        "ACT": float(metric(s, "ACT")),
        "AMS": float(metric(s, "AMS")),
        "Obj_base": float(metric(s, "objective_without_ca_penalty", "objective_base", default=metric(s, "objective"))),
        "CA": float(metric(s, "ca_triggered", "num_failed")),
        "CA_rate": float(metric(s, "ca_rate", "fail_rate")),
        "Obj_K8s": float(metric(s, "objective_ca", "objective")),
        "downloaded_mb": float(metric(s, "downloaded_mb")),
        "reused_mb": float(metric(s, "reused_mb")),
        "reuse_rate": float(metric(s, "reuse_rate")),
    }
    return row, r

def avg(rows, key):
    if not rows:
        return 0.0
    return sum(float(x.get(key, 0.0)) for x in rows) / len(rows)

def write_md(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("written", path)

def f3(x):
    return f"{float(x):.3f}"

def f4(x):
    return f"{float(x):.4f}"

def f6(x):
    return f"{float(x):.6f}"

# Fig5 overall average
fig5_dir = ROOT / "fig5_overall"
rows = []
for p in sorted(fig5_dir.glob("*.json")):
    row, _ = row_from_json(p)
    m = re.search(r"req(\d+)", p.name)
    row["requests"] = int(m.group(1)) if m else None
    rows.append(row)

by = defaultdict(list)
for r in rows:
    by[r["method"]].append(r)

summary = []
for method, rs in by.items():
    summary.append([
        method,
        avg(rs, "Obj_K8s"),
        avg(rs, "Obj_base"),
        avg(rs, "CA"),
        avg(rs, "CA_rate"),
        avg(rs, "downloaded_mb"),
        avg(rs, "reuse_rate"),
        len(rs),
    ])
summary.sort(key=lambda x: x[1])

lines = [
    "## Fig.5 Overall Performance",
    "",
    "| Method | Avg. Obj_K8s ↓ | Avg. Obj_base ↓ | Avg. CA ↓ | Avg. CA_rate ↓ | Avg. downloaded MB ↓ | Avg. reuse rate ↑ | n |",
    "|---|---:|---:|---:|---:|---:|---:|---:|",
]
for x in summary:
    pass
# rewrite cleanly
lines = [
    "## Fig.5 Overall Performance",
    "",
    "| Method | Avg. Obj_K8s ↓ | Avg. Obj_base ↓ | Avg. CA ↓ | Avg. CA_rate ↓ | Avg. downloaded MB ↓ | Avg. reuse rate ↑ | n |",
    "|---|---:|---:|---:|---:|---:|---:|---:|",
]
for x in summary:
    lines.append(f"| {x[0]} | {x[1]:.3f} | {x[2]:.3f} | {x[3]:.3f} | {x[4]:.4f} | {x[5]:.1f} | {x[6]:.6f} | {x[7]} |")
write_md(OUT / "fig5_overall_avg.md", lines)

# Fig5 detailed
lines = [
    "## Fig.5 Overall Performance Detail",
    "",
    "| Requests | Method | ACT | AMS | Obj_base | CA | CA_rate | Obj_K8s | downloaded MB | reused MB | reuse rate |",
    "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
]
for r in sorted(rows, key=lambda x: (x["requests"], x["method"])):
    lines.append(
        f"| {r['requests']} | {r['method']} | {r['ACT']:.3f} | {r['AMS']:.3f} | {r['Obj_base']:.3f} | "
        f"{r['CA']:.0f} | {r['CA_rate']:.4f} | {r['Obj_K8s']:.3f} | "
        f"{r['downloaded_mb']:.0f} | {r['reused_mb']:.0f} | {r['reuse_rate']:.6f} |"
    )
write_md(OUT / "fig5_overall_detail.md", lines)

# Fig1 phase potential terms
fig1_dir = ROOT / "fig1_phase1"
phase_rows = []
for p in sorted(fig1_dir.glob("*.json")):
    row, r = row_from_json(p)
    m = re.search(r"req(\d+)", p.name)
    row["requests"] = int(m.group(1)) if m else None

    hist = r.get("phase1_history", [])
    if hist:
        last = hist[-1]
        comps = last.get("node_components", {})
        row["phi_total"] = float(last.get("potential", 0.0))
        row["delay_term"] = sum(float(v.get("cong_term", 0.0)) for v in comps.values())
        row["frag_term"] = sum(float(v.get("frag_term", 0.0)) for v in comps.values())
        row["aff_reward_term"] = sum(float(v.get("aff_term", 0.0)) for v in comps.values())
        row["load_term"] = sum(float(v.get("task_load_term", 0.0)) for v in comps.values())
    else:
        row["phi_total"] = row["delay_term"] = row["frag_term"] = row["aff_reward_term"] = row["load_term"] = 0.0

    phase_rows.append(row)

lines = [
    "## Fig.1 Phase-1 Potential Terms",
    "",
    "| Requests | Method | phi_total | delay_term | frag_term | aff_reward_term | load_term | CA | CA_rate | Obj_K8s |",
    "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
]
for r in sorted(phase_rows, key=lambda x: (x["requests"], x["method"])):
    lines.append(
        f"| {r['requests']} | {r['method']} | {r['phi_total']:.3f} | {r['delay_term']:.3f} | "
        f"{r['frag_term']:.3f} | {r['aff_reward_term']:.3f} | {r['load_term']:.3f} | "
        f"{r['CA']:.0f} | {r['CA_rate']:.4f} | {r['Obj_K8s']:.3f} |"
    )
write_md(OUT / "fig1_phase1_terms.md", lines)

# Fig2 cache summary by cache/method
fig2_dir = ROOT / "fig2_cache"
rows = []
for p in sorted(fig2_dir.glob("*.json")):
    row, _ = row_from_json(p)
    m1 = re.search(r"cache(\d+)", p.name)
    m2 = re.search(r"req(\d+)", p.name)
    row["cache"] = int(m1.group(1)) if m1 else None
    row["requests"] = int(m2.group(1)) if m2 else None
    rows.append(row)

by = defaultdict(list)
for r in rows:
    by[(r["cache"], r["method"])].append(r)

summary = []
for (cache, method), rs in by.items():
    summary.append([cache, method, avg(rs, "Obj_K8s"), avg(rs, "CA_rate"), avg(rs, "downloaded_mb"), avg(rs, "reuse_rate"), len(rs)])
summary.sort(key=lambda x: (x[0], x[2]))

lines = [
    "## Fig.2 Cache Sensitivity",
    "",
    "| Cache mean MB | Method | Avg. Obj_K8s ↓ | Avg. CA_rate ↓ | Avg. downloaded MB ↓ | Avg. reuse rate ↑ | n |",
    "|---:|---|---:|---:|---:|---:|---:|",
]
for x in summary:
    lines.append(f"| {x[0]} | {x[1]} | {x[2]:.3f} | {x[3]:.4f} | {x[4]:.1f} | {x[5]:.6f} | {x[6]} |")
write_md(OUT / "fig2_cache_avg.md", lines)

# Fig3 scale: matrices for Obj_K8s and CA_rate
fig3_dir = ROOT / "fig3_scale"
rows = []
for p in sorted(fig3_dir.glob("*.json")):
    row, _ = row_from_json(p)
    m = re.search(r"cat(\d+)_edge(\d+)_req(\d+)", p.name)
    if not m:
        continue
    row["catalog"] = int(m.group(1))
    row["edge_nodes"] = int(m.group(2))
    row["requests"] = int(m.group(3))
    rows.append(row)

cats = sorted(set(r["catalog"] for r in rows))
edges = sorted(set(r["edge_nodes"] for r in rows))
reqs = sorted(set(r["requests"] for r in rows))

def matrix_lines(rows, value_key, title):
    lines = [f"## {title}", ""]
    for cat in cats:
        lines.append(f"### Catalog size = {cat}")
        lines.append("")
        lines.append("| Edge nodes | " + " | ".join(str(x) for x in reqs) + " |")
        lines.append("|---:|" + "|".join(["---:"] * len(reqs)) + "|")
        for e in edges:
            vals = []
            for req in reqs:
                matched = [r for r in rows if r["catalog"] == cat and r["edge_nodes"] == e and r["requests"] == req]
                vals.append(f"{matched[0][value_key]:.3f}" if matched else "")
            lines.append(f"| {e} | " + " | ".join(vals) + " |")
        lines.append("")
    return lines

write_md(OUT / "fig3_scale_obj_k8s_matrix.md", matrix_lines(rows, "Obj_K8s", "Fig.3 Scale Impact: Obj_K8s"))
write_md(OUT / "fig3_scale_ca_rate_matrix.md", matrix_lines(rows, "CA_rate", "Fig.3 Scale Impact: CA_rate"))

# Fig4 network matrices
fig4_dir = ROOT / "fig4_network"
rows = []
for p in sorted(fig4_dir.glob("*.json")):
    row, _ = row_from_json(p)
    env = None
    for x in ["homo_good", "homo_bad", "hetero_good", "hetero_bad"]:
        if x in p.name:
            env = x
            break
    m1 = re.search(r"cache(\d+)", p.name)
    m2 = re.search(r"req(\d+)", p.name)
    row["bandwidth_env"] = env
    row["cache"] = int(m1.group(1)) if m1 else None
    row["requests"] = int(m2.group(1)) if m2 else None
    rows.append(row)

envs = ["homo_good", "homo_bad", "hetero_good", "hetero_bad"]
caches = sorted(set(r["cache"] for r in rows if r["cache"] is not None))
reqs = sorted(set(r["requests"] for r in rows if r["requests"] is not None))

def network_matrix(value_key, title):
    lines = [f"## {title}", ""]
    for env in envs:
        lines.append(f"### Network env = {env}")
        lines.append("")
        lines.append("| Cache mean MB | " + " | ".join(str(x) for x in reqs) + " |")
        lines.append("|---:|" + "|".join(["---:"] * len(reqs)) + "|")
        for c in caches:
            vals = []
            for req in reqs:
                matched = [r for r in rows if r["bandwidth_env"] == env and r["cache"] == c and r["requests"] == req]
                vals.append(f"{matched[0][value_key]:.3f}" if matched else "")
            lines.append(f"| {c} | " + " | ".join(vals) + " |")
        lines.append("")
    return lines

write_md(OUT / "fig4_network_obj_k8s_matrix.md", network_matrix("Obj_K8s", "Fig.4 Network Environment: Obj_K8s"))
write_md(OUT / "fig4_network_reuse_matrix.md", network_matrix("reuse_rate", "Fig.4 Network Environment: Reuse Rate"))

print("\nAll markdown tables written to:", OUT)
