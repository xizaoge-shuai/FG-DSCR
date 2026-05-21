import json
import math
import re
from pathlib import Path
from collections import defaultdict, deque

ROOT = Path("results/drtp/k8s_same_scale")
RES_DIR = ROOT / "fig3_scale_prefix"
OUT_DIR = ROOT / "tables_fig3_scale_prefix"
CASE_DIR = Path("cases/drtp_scale_prefix")

OUT_DIR.mkdir(parents=True, exist_ok=True)

CATS = [16, 50, 68, 88]
EDGES = [4, 6, 8, 10, 12, 14, 16, 18]
REQS = [200, 400, 600, 800, 1000, 1200, 1500, 2000]

CA_PROVISION_SEC = 300.0

def load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def get_assignment(result):
    return result.get("assignment") or result.get("assignments") or {}

def get_nodes(case):
    return {str(n.get("eid")): n for n in case.get("nodes", [])}

def normalize_container_metrics(raw):
    out = []
    if isinstance(raw, list):
        for x in raw:
            if isinstance(x, dict):
                out.append(dict(x))
            elif isinstance(x, str):
                out.append({"cid": x})
        return out
    if isinstance(raw, dict):
        for k, v in raw.items():
            key = str(k)
            if isinstance(v, dict):
                item = dict(v)
                if "cid" not in item and not key.startswith("edge-"):
                    item["cid"] = key
                if "node_id" not in item and key.startswith("edge-"):
                    item["node_id"] = key
                out.append(item)
            elif isinstance(v, list):
                for e in v:
                    if isinstance(e, dict):
                        item = dict(e)
                        if "node_id" not in item:
                            item["node_id"] = key
                        out.append(item)
                    elif isinstance(e, str):
                        out.append({"node_id": key, "cid": e})
            elif isinstance(v, str):
                out.append({"cid": key, "node_id": v})
    return out

def percentile(vals, q):
    if not vals:
        return 0.0
    xs = sorted(vals)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q / 100.0
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return xs[lo]
    w = pos - lo
    return xs[lo] * (1 - w) + xs[hi] * w

def edge_completion_times(result):
    metrics = normalize_container_metrics(result.get("container_metrics", []))
    times = {}
    direct_keys = ["completion_time", "finish_time", "end_time"]

    for m in metrics:
        cid = m.get("cid")
        if cid is None:
            continue
        for k in direct_keys:
            if k in m:
                try:
                    times[str(cid)] = float(m[k])
                    break
                except:
                    pass

    # 如果没有 per-container 完成时间，就 fallback 成 summary ACT
    return times

def fragmentation_score(case, result):
    assignment = get_assignment(result)
    containers = {str(c.get("cid")): c for c in case.get("containers", [])}
    nodes = get_nodes(case)

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

def compute_allrequest(case, result):
    containers = case.get("containers", [])
    all_cids = [str(c.get("cid")) for c in containers if c.get("cid") is not None]
    n_total = len(all_cids)

    assignment = get_assignment(result)
    edge_cids = set()
    for _, cids in assignment.items():
        for cid in cids:
            edge_cids.add(str(cid))

    ca_cids = [cid for cid in all_cids if cid not in edge_cids]

    edge_times = edge_completion_times(result)
    summary_act = float(result.get("summary", {}).get("ACT", 0.0))

    edge_ref_values = []
    for cid in edge_cids:
        if cid in edge_times:
            edge_ref_values.append(float(edge_times[cid]))

    if not edge_ref_values:
        edge_ref_values = [summary_act]

    edge_ref_values = sorted(x for x in edge_ref_values if x >= 0)
    if not edge_ref_values:
        edge_ref_values = [0.0]

    n_ca = max(1, len(ca_cids))
    ca_idx = 0

    def edge_like_time_for_ca(idx):
        if len(edge_ref_values) == 1:
            return edge_ref_values[0]
        q = (idx + 0.5) / n_ca
        pos = q * (len(edge_ref_values) - 1)
        lo = int(pos)
        hi = min(lo + 1, len(edge_ref_values) - 1)
        w = pos - lo
        return edge_ref_values[lo] * (1 - w) + edge_ref_values[hi] * w

    completion_times = []

    for cid in all_cids:
        if cid in edge_cids:
            completion_times.append(float(edge_times.get(cid, summary_act)))
        else:
            completion_times.append(CA_PROVISION_SEC + edge_like_time_for_ca(ca_idx))
            ca_idx += 1

    act_all = sum(completion_times) / len(completion_times) if completion_times else 0.0
    p95_all = percentile(completion_times, 95)
    cms_all = max(completion_times) if completion_times else 0.0
    delay_all = 0.5 * act_all + 0.5 * p95_all

    ca_rate = len(ca_cids) / n_total if n_total else 0.0
    edge_rate = len(edge_cids) / n_total if n_total else 0.0

    return act_all, p95_all, cms_all, delay_all, ca_rate, edge_rate

rows = []

for p in sorted(RES_DIR.glob("*.json")):
    m = re.search(r"fg_cat(\d+)_edge(\d+)_req(\d+)\.json", p.name)
    if not m:
        continue

    cat = int(m.group(1))
    edge = int(m.group(2))
    req = int(m.group(3))

    case_path = CASE_DIR / f"drtp_img{cat}_nodes{edge}_cache1024mb_{req}.json"
    if not case_path.exists():
        print("[MISSING CASE]", case_path)
        continue

    case = load_json(case_path)
    result = load_json(p)

    act_all, p95_all, cms_all, delay_all, ca_rate, edge_rate = compute_allrequest(case, result)
    frag = fragmentation_score(case, result)

    rows.append({
        "catalog": cat,
        "edge": edge,
        "requests": req,
        "ACT_all": act_all,
        "P95_all": p95_all,
        "CMS_all": cms_all,
        "Delay_all": delay_all,
        "CA_rate": ca_rate,
        "edge_scheduled_rate": edge_rate,
        "frag_score": frag,
    })

def mean_positive(vals):
    xs = [float(x) for x in vals if float(x) > 0]
    return sum(xs) / len(xs) if xs else 1.0

mean_delay = mean_positive([r["Delay_all"] for r in rows])
mean_ca = mean_positive([r["CA_rate"] for r in rows])
mean_frag = mean_positive([r["frag_score"] for r in rows])

for r in rows:
    r["Delay_norm"] = r["Delay_all"] / mean_delay if mean_delay else 0.0
    r["CA_norm"] = r["CA_rate"] / mean_ca if mean_ca else 0.0
    r["Frag_norm"] = r["frag_score"] / mean_frag if mean_frag else 0.0
    r["Obj_Final"] = r["Delay_norm"] + r["CA_norm"] + r["Frag_norm"]

def write_heatmap(metric, title, filename, fmt="{:.3f}"):
    lines = [f"## {title}", ""]
    for cat in CATS:
        lines.append(f"### catalog_size = {cat}")
        lines.append("")
        lines.append("| edge_nodes \\ requests | " + " | ".join(str(x) for x in REQS) + " |")
        lines.append("|---:|" + "|".join(["---:"] * len(REQS)) + "|")

        for edge in EDGES:
            vals = []
            for req in REQS:
                hit = [r for r in rows if r["catalog"] == cat and r["edge"] == edge and r["requests"] == req]
                vals.append("" if not hit else fmt.format(hit[0][metric]))
            lines.append(f"| {edge} | " + " | ".join(vals) + " |")

        lines.append("")

    out = OUT_DIR / filename
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("written", out)

write_heatmap("Obj_Final", "Fig.3 Scale Prefix: Obj_Final", "fig3_scale_prefix_obj_final_matrix.md")
write_heatmap("CA_rate", "Fig.3 Scale Prefix: CA_rate", "fig3_scale_prefix_ca_rate_matrix.md", fmt="{:.4f}")
write_heatmap("edge_scheduled_rate", "Fig.3 Scale Prefix: edge_scheduled_rate", "fig3_scale_prefix_edge_rate_matrix.md", fmt="{:.4f}")
write_heatmap("frag_score", "Fig.3 Scale Prefix: frag_score", "fig3_scale_prefix_frag_score_matrix.md", fmt="{:.6f}")
write_heatmap("Delay_all", "Fig.3 Scale Prefix: Delay_all", "fig3_scale_prefix_delay_all_matrix.md", fmt="{:.3f}")

print("rows =", len(rows))
print("expected =", len(CATS) * len(EDGES) * len(REQS))
print("mean_delay =", mean_delay)
print("mean_ca =", mean_ca)
print("mean_frag =", mean_frag)
