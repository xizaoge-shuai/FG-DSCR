import argparse
import json
import math
import re
from pathlib import Path
from collections import defaultdict, Counter, deque

METHODS = ["ILR-SA", "LRScheduler", "GAHRL", "ORR", "LASA", "FG-DSCR-GC"]

def load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def save_text(path, lines):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("written", path)

def method_name(path, summary):
    fn = Path(path).name.lower()
    algo = str(summary.get("algo", "")).lower()

    if "ilr" in algo or fn.startswith("ilrsa"):
        return "ILR-SA"
    if "lrscheduler" in algo or fn.startswith("lrs"):
        return "LRScheduler"
    if "gahrl" in algo or fn.startswith("gahrl"):
        return "GAHRL"
    if "orr" in algo or fn.startswith("orr"):
        return "ORR"
    if "lasa" in algo or fn.startswith("lasa"):
        return "LASA"
    if "fg-orig" in algo or "fg_orig" in fn:
        return "FG-orig"
    if "fg" in algo or fn.startswith("fg"):
        return "FG-DSCR-GC"

    return Path(path).stem

def get_assignment(result):
    if isinstance(result.get("assignment"), dict):
        return result["assignment"]
    if isinstance(result.get("assignments"), dict):
        return result["assignments"]
    return {}

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

def selected_steps(result):
    metrics = normalize_container_metrics(result.get("container_metrics", []))
    steps = []

    for m in metrics:
        cid = m.get("cid")
        eid = m.get("node_id") or m.get("eid") or m.get("node")
        if cid is not None and eid is not None:
            steps.append((str(cid), str(eid)))

    if steps:
        return steps

    # fallback：没有 container_metrics 时，只能按 assignment 展开，顺序不如真实过程精确
    assignment = get_assignment(result)
    for eid, cids in assignment.items():
        for cid in cids:
            steps.append((str(cid), str(eid)))
    return steps

def nodes_map(case):
    return {str(n.get("eid")): n for n in case.get("nodes", [])}

def containers_map(case):
    return {str(c.get("cid")): c for c in case.get("containers", [])}


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


def get_node_cache_cap(n, override_cache=None):
    if override_cache is not None:
        return float(override_cache)

    for k in ["repo_capacity_mb", "cache_capacity_mb", "cache_size_mb", "cache_mb"]:
        if k in n:
            try:
                return float(n[k])
            except Exception:
                pass
    return 1024.0

def get_layer_sizes(case):
    sizes = {}

    for key in ["layer_sizes_mb", "layer_sizes", "layers_size", "layer_size"]:
        obj = case.get(key)
        if isinstance(obj, dict):
            for k, v in obj.items():
                try:
                    sizes[str(k)] = float(v)
                except Exception:
                    pass

    layers = case.get("layers")
    if isinstance(layers, list):
        for x in layers:
            if isinstance(x, dict):
                lid = x.get("id") or x.get("layer_id") or x.get("lid") or x.get("name")
                sz = x.get("size_mb") or x.get("size") or x.get("mb")
                if lid is not None and sz is not None:
                    try:
                        sizes[str(lid)] = float(sz)
                    except Exception:
                        pass
    return sizes

def get_image_catalog(case):
    cats = {}
    for key in ["images", "image_catalog", "catalog"]:
        obj = case.get(key)
        if isinstance(obj, dict):
            for k, v in obj.items():
                cats[str(k)] = v
    return cats

def container_layers(case, c):
    for key in ["layers", "layer_ids", "image_layers", "required_layers"]:
        if isinstance(c.get(key), list):
            return [str(x.get("id") if isinstance(x, dict) else x) for x in c[key]]

    img = c.get("image") or c.get("image_id") or c.get("repo") or c.get("image_name")
    cats = get_image_catalog(case)

    if img is not None and str(img) in cats:
        im = cats[str(img)]
        if isinstance(im, dict):
            for key in ["layers", "layer_ids", "image_layers"]:
                if isinstance(im.get(key), list):
                    return [str(x.get("id") if isinstance(x, dict) else x) for x in im[key]]
        elif isinstance(im, list):
            return [str(x.get("id") if isinstance(x, dict) else x) for x in im]

    return []

def res_of(c):
    return {str(k): float(v) for k, v in c.get("resources", {}).items()}

def feasible(used, node, c_res):
    caps = node.get("resources", {})
    for r, req in c_res.items():
        cap = float(caps.get(r, 0.0))
        if used[r] + req > cap + 1e-9:
            return False
    return True

def minmax(vals, higher_is_better=True):
    if not vals:
        return {}
    xs = list(vals.values())
    mn, mx = min(xs), max(xs)

    if abs(mx - mn) < 1e-12:
        return {k: 0.5 for k in vals}

    out = {}
    for k, v in vals.items():
        z = (v - mn) / (mx - mn)
        out[k] = z if higher_is_better else 1.0 - z
    return out

def cost_minmax(vals):
    # 成本项：越小越好，但作为 term 进入 phi 时越大越差
    if not vals:
        return {}
    xs = list(vals.values())
    mn, mx = min(xs), max(xs)
    if abs(mx - mn) < 1e-12:
        return {k: 0.5 for k in vals}
    return {k: (v - mn) / (mx - mn) for k, v in vals.items()}

def benefit_minmax(vals):
    # 收益项：越大越好
    return minmax(vals, higher_is_better=True)

def update_cache(cache_layers, cache_order, cache_mb, cap, layers, layer_sizes):
    for lid in layers:
        if lid not in cache_layers:
            cache_layers.add(lid)
            cache_order.append(lid)
            cache_mb += float(layer_sizes.get(lid, 0.0))
        else:
            # 简单 LRU 更新
            try:
                cache_order.remove(lid)
            except ValueError:
                pass
            cache_order.append(lid)

    while cache_mb > cap + 1e-9 and cache_order:
        old = cache_order.popleft()
        if old in cache_layers:
            cache_layers.remove(old)
            cache_mb -= float(layer_sizes.get(old, 0.0))

    return max(cache_mb, 0.0)

def infer_req_cache(path):
    fn = Path(path).name
    m_req = re.search(r"req(\d+)", fn)
    if not m_req:
        m_req = re.search(r"_(\d+)\.json$", fn)

    m_cache = re.search(r"cache(\d+)", fn)
    req = int(m_req.group(1)) if m_req else None
    cache = int(m_cache.group(1)) if m_cache else None
    return req, cache

def load_case(case_template, req, cache):
    candidates = []

    if case_template:
        try:
            candidates.append(Path(case_template.format(req=req, cache=cache if cache is not None else 1024)))
        except Exception:
            pass

    if cache is not None:
        candidates.append(Path(f"cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_{cache}mb_{req}.json"))

    candidates.append(Path(f"cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_{req}.json"))

    for p in candidates:
        if p.exists():
            case = load_json(p)
            return case

    raise FileNotFoundError(f"Cannot find case for req={req}, cache={cache}, tried={candidates}")

def process_one(case, result, req, cache_override=None):
    nodes = nodes_map(case)
    containers = containers_map(case)
    layer_sizes = get_layer_sizes(case)

    # 初始化状态
    used = {eid: defaultdict(float) for eid in nodes}
    load_cnt = defaultdict(int)
    cache_layers = {eid: set() for eid in nodes}
    cache_order = {eid: deque() for eid in nodes}
    cache_mb = defaultdict(float)

    # process-level queue-time approximation for delay term
    queue_time = defaultdict(float)

    # 未来层频率
    steps = selected_steps(result)
    future = Counter()
    cid_to_layers = {}

    for cid, _ in steps:
        c = containers.get(str(cid))
        if c is None:
            continue
        ls = container_layers(case, c)
        cid_to_layers[str(cid)] = ls
        for lid in ls:
            future[lid] += 1

    fig1_rows = []
    fig2_rows = []

    for step_idx, (cid, chosen_eid) in enumerate(steps):
        c = containers.get(str(cid))
        if c is None or chosen_eid not in nodes:
            continue

        layers = cid_to_layers.get(cid)
        if layers is None:
            layers = container_layers(case, c)
            cid_to_layers[cid] = layers

        # 当前容器不算未来
        for lid in layers:
            future[lid] -= 1
            if future[lid] <= 0:
                del future[lid]

        c_res = res_of(c)

        candidates = []
        for eid, n in nodes.items():
            if feasible(used[eid], n, c_res):
                candidates.append(eid)

        # 如果 chosen 不在 feasible 里，仍加入，方便诊断
        if chosen_eid not in candidates:
            candidates.append(chosen_eid)

        if not candidates:
            continue

        delay_raw = {}
        frag_raw = {}
        aff_raw = {}
        load_raw = {}

        reuse_raw = {}
        future_raw = {}
        pull_raw = {}
        evict_raw = {}

        layer_total = sum(float(layer_sizes.get(lid, 0.0)) for lid in layers)

        for eid in candidates:
            n = nodes[eid]
            caps = n.get("resources", {})

            # cache affinity and missing-layer pull time
            aff = sum(float(layer_sizes.get(lid, 0.0)) for lid in layers if lid in cache_layers[eid])
            missing = max(0.0, layer_total - aff)
            bw = node_bandwidth(n)
            pull_time = missing / bw if bw > 0 else missing

            # Fig.1 phase-1 terms
            # delay_term: projected completion pressure, separated from load
            delay_raw[eid] = float(queue_time[eid] + pull_time)

            projected_utils = []
            for r, cap in caps.items():
                cap = float(cap)
                if cap <= 0:
                    continue
                u = used[eid][r] + c_res.get(r, 0.0)
                projected_utils.append(u / cap)

            if projected_utils:
                avg_u = sum(projected_utils) / len(projected_utils)
                frag_raw[eid] = sum(abs(x - avg_u) for x in projected_utils) / len(projected_utils)
                # load_term: projected average resource utilization
                load_raw[eid] = avg_u
            else:
                frag_raw[eid] = 0.0
                load_raw[eid] = 0.0

            aff_raw[eid] = aff

            # Fig.2 dynamic gain terms
            reuse_raw[eid] = aff

            # simulate post-admission cache under simple LRU
            cap_mb = get_node_cache_cap(n, override_cache=cache_override)
            sim_layers = set(cache_layers[eid])
            sim_order = deque(cache_order[eid])
            sim_mb = float(cache_mb[eid])

            for lid in layers:
                sz = float(layer_sizes.get(lid, 0.0))
                if lid not in sim_layers:
                    sim_layers.add(lid)
                    sim_order.append(lid)
                    sim_mb += sz
                else:
                    try:
                        sim_order.remove(lid)
                    except ValueError:
                        pass
                    sim_order.append(lid)

            evicted_future_value = 0.0
            while sim_mb > cap_mb + 1e-9 and sim_order:
                old_lid = sim_order.popleft()
                if old_lid in sim_layers:
                    sim_layers.remove(old_lid)
                    sz = float(layer_sizes.get(old_lid, 0.0))
                    sim_mb -= sz
                    evicted_future_value += sz * float(future.get(old_lid, 0))

            # future_t: future utility preserved after this cache decision
            fut = 0.0
            for lid in sim_layers:
                fut += float(layer_sizes.get(lid, 0.0)) * float(future.get(lid, 0))
            future_raw[eid] = fut

            # pull_t: use transfer time, not just MB
            pull_raw[eid] = pull_time

            # evict_t: future value lost by eviction
            evict_raw[eid] = evicted_future_value

        delay_n = cost_minmax(delay_raw)
        frag_n = cost_minmax(frag_raw)
        aff_n = benefit_minmax(aff_raw)
        load_n = cost_minmax(load_raw)

        reuse_n = benefit_minmax(reuse_raw)
        future_n = benefit_minmax(future_raw)
        pull_cost_n = cost_minmax(pull_raw)
        evict_cost_n = cost_minmax(evict_raw)

        # saving 越大越好
        pull_saving_n = {k: 1.0 - v for k, v in pull_cost_n.items()}
        evict_saving_n = {k: 1.0 - v for k, v in evict_cost_n.items()}

        eid = chosen_eid

        d = delay_n.get(eid, 0.5)
        f = frag_n.get(eid, 0.5)
        a = aff_n.get(eid, 0.5)
        l = load_n.get(eid, 0.5)
        phi = d + f - a + l

        fig1_rows.append({
            "step": step_idx,
            "delay_term": d,
            "frag_term": f,
            "aff_reward_term": a,
            "load_term": l,
            "phi_total": phi,
        })

        r = reuse_n.get(eid, 0.5)
        fu = future_n.get(eid, 0.5)
        ps = pull_saving_n.get(eid, 0.5)
        es = evict_saving_n.get(eid, 0.5)
        gain = 0.25 * (r + fu + ps + es)

        fig2_rows.append({
            "step": step_idx,
            "dynamic_gain": gain,
            "reuse_t_norm": r,
            "future_t_norm": fu,
            "pull_t_saving_norm": ps,
            "evict_t_saving_norm": es,
        })

        # 更新状态
        selected_aff = sum(float(layer_sizes.get(lid, 0.0)) for lid in layers if lid in cache_layers[eid])
        selected_missing = max(0.0, layer_total - selected_aff)
        selected_bw = node_bandwidth(nodes[eid])
        selected_service_time = selected_missing / selected_bw if selected_bw > 0 else selected_missing
        queue_time[eid] += selected_service_time

        for rr, vv in c_res.items():
            used[eid][rr] += vv
        load_cnt[eid] += 1

        cap_mb = get_node_cache_cap(nodes[eid], override_cache=cache_override)
        cache_mb[eid] = update_cache(cache_layers[eid], cache_order[eid], cache_mb[eid], cap_mb, layers, layer_sizes)

    return fig1_rows, fig2_rows

def avg(xs):
    return sum(xs) / len(xs) if xs else 0.0

def write_matrix(rows, x_key, method_key, value_key, title, path, fmt="{:.6f}"):
    xs = sorted(set(r[x_key] for r in rows))
    methods = [m for m in METHODS if any(r[method_key] == m for r in rows)]

    lines = [f"## {title}", ""]
    lines.append(f"| {x_key} | " + " | ".join(methods) + " |")
    lines.append("|---:|" + "|".join(["---:"] * len(methods)) + "|")

    for x in xs:
        vals = []
        for m in methods:
            hit = [r[value_key] for r in rows if r[x_key] == x and r[method_key] == m]
            vals.append("" if not hit else fmt.format(avg(hit)))
        lines.append(f"| {x} | " + " | ".join(vals) + " |")

    save_text(path, lines)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--res-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--case-template", default="")
    ap.add_argument("--mode", choices=["fig1", "fig2", "both"], default="both")
    ap.add_argument("--cache-override", type=int, default=None)
    args = ap.parse_args()

    res_dir = Path(args.res_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    fig1_agg = []
    fig2_agg = []
    skipped = 0

    for p in sorted(res_dir.glob("*.json")):
        req, cache = infer_req_cache(p)
        if req is None:
            skipped += 1
            continue

        try:
            case = load_case(args.case_template, req, cache)
        except Exception as e:
            print("[SKIP case]", p, e)
            skipped += 1
            continue

        result = load_json(p)
        summary = result.get("summary", {})
        method = method_name(p, summary)

        if method == "FG-orig":
            continue
        if method not in METHODS:
            continue

        cache_use = args.cache_override if args.cache_override is not None else cache

        fig1_rows, fig2_rows = process_one(case, result, req=req, cache_override=cache_use)

        if fig1_rows:
            rec = {
                "requests": req,
                "cache_mb": cache_use if cache_use is not None else 1024,
                "method": method,
            }
            for k in ["phi_total", "delay_term", "frag_term", "aff_reward_term", "load_term"]:
                rec[k] = avg([x[k] for x in fig1_rows])
            fig1_agg.append(rec)

        if fig2_rows:
            rec = {
                "requests": req,
                "cache_mb": cache_use if cache_use is not None else 1024,
                "method": method,
            }
            for k in ["dynamic_gain", "reuse_t_norm", "future_t_norm", "pull_t_saving_norm", "evict_t_saving_norm"]:
                rec[k] = avg([x[k] for x in fig2_rows])
            fig2_agg.append(rec)

    print("processed_fig1_rows =", len(fig1_agg))
    print("processed_fig2_rows =", len(fig2_agg))
    print("skipped =", skipped)

    if args.mode in ["fig1", "both"]:
        write_matrix(fig1_agg, "requests", "method", "phi_total",
                     "Fig.1 Process-normalized phi_total",
                     out_dir / "fig1_process_phi_total_matrix.md")
        write_matrix(fig1_agg, "requests", "method", "delay_term",
                     "Fig.1 Process-normalized delay_term",
                     out_dir / "fig1_process_delay_term_matrix.md")
        write_matrix(fig1_agg, "requests", "method", "frag_term",
                     "Fig.1 Process-normalized frag_term",
                     out_dir / "fig1_process_frag_term_matrix.md")
        write_matrix(fig1_agg, "requests", "method", "aff_reward_term",
                     "Fig.1 Process-normalized aff_reward_term",
                     out_dir / "fig1_process_aff_reward_term_matrix.md")
        write_matrix(fig1_agg, "requests", "method", "load_term",
                     "Fig.1 Process-normalized load_term",
                     out_dir / "fig1_process_load_term_matrix.md")

    if args.mode in ["fig2", "both"]:
        write_matrix(fig2_agg, "cache_mb", "method", "dynamic_gain",
                     "Fig.2 Process-normalized Dynamic Gain",
                     out_dir / "fig2_process_dynamic_gain_matrix.md")
        write_matrix(fig2_agg, "cache_mb", "method", "reuse_t_norm",
                     "Fig.2 Process-normalized Reuse_t(u)",
                     out_dir / "fig2_process_reuse_t_norm_matrix.md")
        write_matrix(fig2_agg, "cache_mb", "method", "future_t_norm",
                     "Fig.2 Process-normalized Future_t(u)",
                     out_dir / "fig2_process_future_t_norm_matrix.md")
        write_matrix(fig2_agg, "cache_mb", "method", "pull_t_saving_norm",
                     "Fig.2 Process-normalized Pull_t(u) saving",
                     out_dir / "fig2_process_pull_t_saving_norm_matrix.md")
        write_matrix(fig2_agg, "cache_mb", "method", "evict_t_saving_norm",
                     "Fig.2 Process-normalized Evict_t(u) saving",
                     out_dir / "fig2_process_evict_t_saving_norm_matrix.md")

if __name__ == "__main__":
    main()
