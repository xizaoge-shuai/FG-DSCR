import argparse
import json
import math
import re
from pathlib import Path
from collections import defaultdict

METHOD = "FG-DSCR-GC"

def load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def get_assignment(result):
    if isinstance(result.get("assignment"), dict):
        return result["assignment"]
    if isinstance(result.get("assignments"), dict):
        return result["assignments"]
    return {}

def get_nodes(case):
    return {str(n.get("eid")): n for n in case.get("nodes", [])}

def node_bandwidth(n):
    for k in ["bandwidth_mb_s", "bandwidth", "bw", "net_bw", "network_bw"]:
        if k in n:
            try:
                v = float(n[k])
                if v > 0:
                    return v
            except Exception:
                pass
    return 100.0

def normalize_container_metrics(raw):
    out = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                out.append(item)
            elif isinstance(item, str):
                out.append({"cid": item})
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
                for elem in v:
                    if isinstance(elem, dict):
                        item = dict(elem)
                        if "node_id" not in item:
                            item["node_id"] = key
                        out.append(item)
                    elif isinstance(elem, str):
                        out.append({"node_id": key, "cid": elem})
            elif isinstance(v, str):
                out.append({"cid": key, "node_id": v})
    return out

def service_time_from_metric(m, bw):
    for k in ["completion_time", "finish_time", "end_time", "service_time", "duration", "latency", "pull_time", "download_time", "time"]:
        if k in m:
            try:
                v = float(m[k])
                if v >= 0:
                    return v
            except Exception:
                pass
    d = float(m.get("downloaded_mb", 0.0))
    return d / bw if bw > 0 else 0.0

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

def edge_completion_times(case, result):
    nodes = get_nodes(case)
    bw_map = {eid: node_bandwidth(n) for eid, n in nodes.items()}
    metrics = normalize_container_metrics(result.get("container_metrics", []))

    times = {}
    direct_keys = ["completion_time", "finish_time", "end_time"]
    has_direct = False

    for m in metrics:
        cid = m.get("cid")
        if cid is None:
            continue
        for k in direct_keys:
            if k in m:
                try:
                    times[str(cid)] = float(m[k])
                    has_direct = True
                    break
                except Exception:
                    pass
    if has_direct and times:
        return times

    by_node = defaultdict(list)
    for m in metrics:
        eid = str(m.get("node_id") or m.get("eid") or m.get("node") or "")
        cid = m.get("cid")
        if not eid or cid is None:
            continue
        by_node[eid].append(m)

    for eid, ms in by_node.items():
        cur = 0.0
        bw = bw_map.get(eid, 100.0)
        for m in ms:
            cid = str(m.get("cid"))
            cur += service_time_from_metric(m, bw)
            times[cid] = cur

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

def compute_one(case, result, ca_provision):
    all_cids = [str(c.get("cid")) for c in case.get("containers", []) if c.get("cid") is not None]
    n_total = len(all_cids)

    assignment = get_assignment(result)
    edge_cids = set()
    for _, cids in assignment.items():
        for cid in cids:
            edge_cids.add(str(cid))

    ca_cids = [cid for cid in all_cids if cid not in edge_cids]
    edge_times = edge_completion_times(case, result)

    edge_ref_values = []
    for cid in edge_cids:
        if cid in edge_times:
            try:
                edge_ref_values.append(float(edge_times[cid]))
            except Exception:
                pass

    if not edge_ref_values:
        edge_ref_values = [float(result.get("summary", {}).get("ACT", 0.0))]
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
            completion_times.append(float(edge_times.get(cid, result.get("summary", {}).get("ACT", 0.0))))
        else:
            completion_times.append(float(ca_provision) + edge_like_time_for_ca(ca_idx))
            ca_idx += 1

    act_all = sum(completion_times) / len(completion_times) if completion_times else 0.0
    p95_all = percentile(completion_times, 95)
    cms_all = max(completion_times) if completion_times else 0.0
    delay_all = 0.5 * act_all + 0.5 * p95_all

    ca_rate = len(ca_cids) / n_total if n_total > 0 else 0.0
    frag = fragmentation_score(case, result)

    s = result.get("summary", {})
    return {
        "ACT_all": act_all,
        "P95_all": p95_all,
        "CMS_all": cms_all,
        "Delay_all": delay_all,
        "CA_rate": ca_rate,
        "frag_score": frag,
        "edge_scheduled_rate": len(edge_cids) / n_total if n_total else 0.0,
        "downloaded_mb": float(s.get("downloaded_mb", 0.0)),
        "reuse_rate": float(s.get("reuse_rate", 0.0)),
    }

def mean_positive(vals):
    xs = [float(x) for x in vals if float(x) > 0]
    return sum(xs) / len(xs) if xs else 1.0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ts", nargs="+", type=int, default=[120, 300, 600])
    ap.add_argument("--weights", nargs="+", type=float, default=[0.2, 0.5, 0.3])
    args = ap.parse_args()

    assert len(args.ts) == len(args.weights)

    root = Path(args.root)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    records = []
    for tag_dir in sorted(root.iterdir()):
        if not tag_dir.is_dir():
            continue
        tag = tag_dir.name

        for p in sorted(tag_dir.glob("*.json")):
            m = re.search(r"req(\d+)", p.name)
            if not m:
                continue
            req = int(m.group(1))
            case_path = Path(f"cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_{req}.json")
            if not case_path.exists():
                continue

            case = load_json(case_path)
            result = load_json(p)

            for T in args.ts:
                met = compute_one(case, result, T)
                records.append({"tag": tag, "req": req, "T": T, **met})

    if not records:
        raise SystemExit("No records found.")

    # 对每个 T 全局归一化
    for T in args.ts:
        sub = [r for r in records if r["T"] == T]
        md = mean_positive([r["Delay_all"] for r in sub])
        mc = mean_positive([r["CA_rate"] for r in sub])
        mf = mean_positive([r["frag_score"] for r in sub])
        for r in sub:
            r["Delay_norm"] = r["Delay_all"] / md if md > 0 else 0.0
            r["CA_norm"] = r["CA_rate"] / mc if mc > 0 else 0.0
            r["Frag_norm"] = r["frag_score"] / mf if mf > 0 else 0.0
            r["Obj_Final"] = r["Delay_norm"] + r["CA_norm"] + r["Frag_norm"]

    by_tag_T = defaultdict(list)
    for r in records:
        by_tag_T[(r["tag"], r["T"])].append(r)

    tag_rows = []
    tags = sorted(set(r["tag"] for r in records))
    for tag in tags:
        row = {"tag": tag}
        robust_score = 0.0
        ok = True

        for T, w in zip(args.ts, args.weights):
            rs = by_tag_T.get((tag, T), [])
            if not rs:
                ok = False
                break

            avg_obj = sum(x["Obj_Final"] for x in rs) / len(rs)
            row[f"obj_ca{T}"] = avg_obj
            row[f"delay_ca{T}"] = sum(x["Delay_norm"] for x in rs) / len(rs)
            row[f"ca_ca{T}"] = sum(x["CA_norm"] for x in rs) / len(rs)
            row[f"frag_ca{T}"] = sum(x["Frag_norm"] for x in rs) / len(rs)
            robust_score += w * avg_obj

        if not ok:
            continue

        tag_records = [r for r in records if r["tag"] == tag]
        row["robust_score"] = robust_score
        row["avg_ca_rate"] = sum(x["CA_rate"] for x in tag_records) / len(tag_records)
        row["avg_frag_score"] = sum(x["frag_score"] for x in tag_records) / len(tag_records)
        row["avg_edge_scheduled_rate"] = sum(x["edge_scheduled_rate"] for x in tag_records) / len(tag_records)
        row["avg_downloaded"] = sum(x["downloaded_mb"] for x in tag_records) / len(tag_records)
        row["avg_reuse"] = sum(x["reuse_rate"] for x in tag_records) / len(tag_records)
        tag_rows.append(row)

    tag_rows.sort(key=lambda x: x["robust_score"])

    header = [
        "rank", "tag", "robust_score",
        *[f"obj_ca{T}" for T in args.ts],
        *[f"delay_ca{T}" for T in args.ts],
        "avg_CA_rate", "avg_frag_score", "avg_edge_scheduled_rate", "avg_downloaded", "avg_reuse",
    ]

    lines = ["## FG Edge-like CA Parameter Sweep Ranking", ""]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---:" if h != "tag" else "---" for h in header]) + "|")

    for i, r in enumerate(tag_rows, 1):
        vals = [
            str(i),
            r["tag"],
            f"{r['robust_score']:.6f}",
            *[f"{r[f'obj_ca{T}']:.6f}" for T in args.ts],
            *[f"{r[f'delay_ca{T}']:.6f}" for T in args.ts],
            f"{r['avg_ca_rate']:.6f}",
            f"{r['avg_frag_score']:.6f}",
            f"{r['avg_edge_scheduled_rate']:.6f}",
            f"{r['avg_downloaded']:.1f}",
            f"{r['avg_reuse']:.6f}",
        ]
        lines.append("| " + " | ".join(vals) + " |")

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("written", out)

    top_tags = out.parent / "top_tags.txt"
    top_tags.write_text("\n".join(r["tag"] for r in tag_rows[:30]) + "\n", encoding="utf-8")
    print("written", top_tags)

if __name__ == "__main__":
    main()
