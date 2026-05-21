import json
import re
from pathlib import Path

ROOT = Path("results/drtp/k8s_same_scale")
RES_DIR = ROOT / "fig4_network_equalmean_prefix"
CASE_DIR = ROOT / "cases" / "fig4_network_equalmean_prefix"
OUT = ROOT / "tables_norm_final_fig4_equalmean_prefix"
OUT.mkdir(parents=True, exist_ok=True)

ENVS = ["homo_good", "homo_bad", "hetero_good", "hetero_bad"]

def load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

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
                if c is not None:
                    used += float(c.get("resources", {}).get(r, 0.0))

            pressures.append(used / cap)

        if pressures:
            avg_p = sum(pressures) / len(pressures)
            vals.append(sum(abs(x - avg_p) for x in pressures) / len(pressures))

    return sum(vals) / len(vals) if vals else 0.0

def mean_positive(rows, key):
    vals = [float(r[key]) for r in rows if float(r[key]) > 0]
    return sum(vals) / len(vals) if vals else 1.0

rows = []

for p in sorted(RES_DIR.glob("*.json")):
    env = None
    for e in ENVS:
        if e in p.name:
            env = e
            break

    m1 = re.search(r"cache(\d+)", p.name)
    m2 = re.search(r"req(\d+)", p.name)

    if not env or not m1 or not m2:
        continue

    cache = int(m1.group(1))
    req = int(m2.group(1))

    case_path = CASE_DIR / f"case_{env}_cache{cache}_req{req}.json"
    if not case_path.exists():
        print("[MISSING CASE]", case_path)
        continue

    result = load_json(p)
    case = load_json(case_path)
    s = result.get("summary", {})

    act = float(s.get("ACT", 0.0))
    ams = float(s.get("AMS", 0.0))
    delay_raw = 0.5 * act + 0.5 * ams

    ca = float(s.get("ca_triggered", s.get("num_failed", 0.0)))
    if "ca_rate" in s:
        ca_rate = float(s["ca_rate"])
    elif "fail_rate" in s:
        ca_rate = float(s["fail_rate"])
    else:
        n = float(s.get("num_containers", 0.0))
        ca_rate = ca / n if n > 0 else 0.0

    rows.append({
        "scenario": env,
        "cache_mb": cache,
        "requests": req,
        "Delay_raw": delay_raw,
        "CA": ca,
        "CA_rate": ca_rate,
        "frag_score": fragmentation_score(case, result),
        "downloaded_mb": float(s.get("downloaded_mb", 0.0)),
        "reuse_rate": float(s.get("reuse_rate", 0.0)),
    })

mean_delay = mean_positive(rows, "Delay_raw")
mean_ca = mean_positive(rows, "CA_rate")
mean_frag = mean_positive(rows, "frag_score")

for r in rows:
    r["Delay_norm"] = r["Delay_raw"] / mean_delay if mean_delay > 0 else 0.0
    r["CA_norm"] = r["CA_rate"] / mean_ca if mean_ca > 0 else 0.0
    r["Frag_norm"] = r["frag_score"] / mean_frag if mean_frag > 0 else 0.0
    r["Obj_Final"] = r["Delay_norm"] + r["CA_norm"] + r["Frag_norm"]

def write(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("written", path)

def write_network_matrix(key, title, filename, fmt="{:.3f}"):
    caches = sorted(set(r["cache_mb"] for r in rows))
    reqs = sorted(set(r["requests"] for r in rows))

    lines = [f"## {title}", ""]
    for env in ENVS:
        lines += [f"### scenario = {env}", ""]
        lines.append("| cache_mb | " + " | ".join(str(x) for x in reqs) + " |")
        lines.append("|---:|" + "|".join(["---:"] * len(reqs)) + "|")

        for c in caches:
            vals = []
            for req in reqs:
                hit = [
                    r[key]
                    for r in rows
                    if r["scenario"] == env and r["cache_mb"] == c and r["requests"] == req
                ]
                vals.append("" if not hit else fmt.format(hit[0]))
            lines.append(f"| {c} | " + " | ".join(vals) + " |")
        lines.append("")

    write(OUT / filename, lines)

write_network_matrix("Obj_Final", "Fig.4 Network Equal-Mean: Normalized Final Objective", "fig4_equalmean_norm_obj_matrix.md")
write_network_matrix("Delay_norm", "Fig.4 Network Equal-Mean: Delay_norm", "fig4_equalmean_delay_norm_matrix.md")
write_network_matrix("CA_norm", "Fig.4 Network Equal-Mean: CA_norm", "fig4_equalmean_ca_norm_matrix.md")
write_network_matrix("Frag_norm", "Fig.4 Network Equal-Mean: Frag_norm", "fig4_equalmean_frag_norm_matrix.md")
write_network_matrix("reuse_rate", "Fig.4 Network Equal-Mean: reuse_rate", "fig4_equalmean_reuse_matrix.md", "{:.6f}")
write_network_matrix("downloaded_mb", "Fig.4 Network Equal-Mean: downloaded_mb", "fig4_equalmean_downloaded_matrix.md", "{:.0f}")

lines = [
    "## Fig.4 Network Equal-Mean Detail",
    "",
    "| scenario | cache_mb | requests | Delay_raw | Delay_norm | CA | CA_rate | CA_norm | frag_score | Frag_norm | Obj_Final | downloaded_mb | reuse_rate |",
    "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
]
for r in sorted(rows, key=lambda x: (ENVS.index(x["scenario"]), x["cache_mb"], x["requests"])):
    lines.append(
        f"| {r['scenario']} | {r['cache_mb']} | {r['requests']} | "
        f"{r['Delay_raw']:.3f} | {r['Delay_norm']:.3f} | "
        f"{r['CA']:.0f} | {r['CA_rate']:.4f} | {r['CA_norm']:.3f} | "
        f"{r['frag_score']:.6f} | {r['Frag_norm']:.3f} | "
        f"{r['Obj_Final']:.3f} | {r['downloaded_mb']:.0f} | {r['reuse_rate']:.6f} |"
    )
write(OUT / "fig4_equalmean_detail.md", lines)

combined = []
for fn in [
    "fig4_equalmean_norm_obj_matrix.md",
    "fig4_equalmean_delay_norm_matrix.md",
    "fig4_equalmean_ca_norm_matrix.md",
    "fig4_equalmean_frag_norm_matrix.md",
    "fig4_equalmean_reuse_matrix.md",
    "fig4_equalmean_downloaded_matrix.md",
    "fig4_equalmean_detail.md",
]:
    p = OUT / fn
    if p.exists():
        combined.append(p.read_text(encoding="utf-8"))

write(OUT / "fig4_equalmean_all_tables.md", ["\n\n".join(combined)])

print()
print("normalization scale:")
print("mean_delay =", mean_delay)
print("mean_ca =", mean_ca)
print("mean_frag =", mean_frag)
print("output_dir =", OUT)
