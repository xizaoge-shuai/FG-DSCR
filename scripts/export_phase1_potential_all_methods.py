import os
import csv
import json
import math
from collections import defaultdict

REQS = [200, 300, 400, 500, 600, 700, 800, 900, 1000]

METHODS = [
    {
        "name": "ILR-SA",
        "paths": [
            "results/drtp/final_exp/overall_normal88/ilrsa/ilrsa_img88_{N}.json",
            "results/drtp/final_exp/cache_sweep88_feasible/ilrsa/ilrsa_cache1024_{N}.json",
            "results/drtp/cache_sweep_88/ilrsa/ilrsa_img88_cache_1024mb_{N}.json",
        ],
    },
    {
        "name": "LRScheduler-inspired",
        "paths": [
            "results/drtp/final_exp/overall_normal88/lrscheduler_source/lrs_img88_{N}.json",
            "results/drtp/final_exp/cache_sweep88_feasible/lrscheduler_source/lrs_cache1024_{N}.json",
            "results/drtp/recent_baselines/lrscheduler_source/lrscheduler_source_img88_{N}.json",
        ],
    },
    {
        "name": "GAHRL-inspired",
        "paths": [
            "results/drtp/final_exp/overall_normal88/gahrl/gahrl_img88_{N}.json",
            "results/drtp/final_exp/cache_sweep88_feasible/gahrl/gahrl_cache1024_{N}.json",
            "results/drtp/recent_baselines/gahrl_objective_greedy/gahrl_objective_greedy_img88_{N}.json",
            "results/drtp/recent_baselines/gahrl/gahrl_img88_{N}.json",
        ],
    },
    {
        "name": "ORR-inspired",
        "paths": [
            "results/drtp/final_exp/overall_normal88/orr/orr_img88_{N}.json",
            "results/drtp/final_exp/cache_sweep88_feasible/orr/orr_cache1024_{N}.json",
            "results/drtp/recent_baselines/orr/orr_img88_{N}.json",
        ],
    },
    {
        "name": "LASA-reimpl",
        "paths": [
            "results/drtp/final_exp/overall_normal88/lasa/lasa_img88_{N}.json",
            "results/drtp/final_exp/cache_sweep88_feasible/lasa/lasa_cache1024_{N}.json",
            "results/drtp/recent_baselines/lasa/lasa_img88_{N}.json",
        ],
    },
    {
        "name": "FG-DSCR-GC",
        "paths": [
            "results/drtp/final_exp/phase1_placement88_req200_1000/full_{N}.json",
            "results/drtp/final_exp/overall_normal88/fg_gc/fg_img88_{N}.json",
            "results/drtp/final_exp/cache_sweep88_feasible/fg_gc/fg_cache1024_{N}.json",
        ],
    },
]

CASE_TEMPLATE = "cases/drtp_cache_sweep_88/drtp_img88_cache_1024mb_{N}.json"
OUTDIR = "results/drtp/final_exp/phase1_potential_all_methods88_req200_1000"

LAMBDA_DELAY = 1.0
LAMBDA_FRAG = 1.0
LAMBDA_AFF = 0.2
LAMBDA_LOAD = 1.0


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def find_existing(paths, N):
    for tmpl in paths:
        p = tmpl.format(N=N)
        if os.path.exists(p):
            return p
    return None


def get_node_id(node, idx):
    for k in ["eid", "id", "name", "node_id"]:
        if k in node:
            return str(node[k])
    return f"edge-{idx+1}"


def get_cid(c, idx):
    for k in ["cid", "id", "container_id", "name"]:
        if k in c:
            return str(c[k])
    return f"c{idx:03d}"


def get_layers(c):
    return [str(x) for x in c.get("layers", c.get("image_layers", []))]


def get_bw(node):
    for k in ["bandwidth_mb_s", "bandwidth", "download_bandwidth", "pull_bandwidth", "bw"]:
        if k in node:
            try:
                return float(node[k])
            except Exception:
                pass
    return 100.0


def get_resources(obj):
    r = obj.get("resources", {})
    return {
        "cpu": float(r.get("cpu", obj.get("cpu", 0.0))),
        "mem": float(r.get("mem", obj.get("mem", obj.get("memory", 0.0)))),
        "disk": float(r.get("disk", obj.get("disk", obj.get("storage", 0.0)))),
    }


def get_cap(node):
    r = node.get("resources", {})
    return {
        "cpu": float(r.get("cpu", node.get("cpu", 1.0))),
        "mem": float(r.get("mem", node.get("mem", node.get("memory", 1.0)))),
        "disk": float(r.get("disk", node.get("disk", node.get("storage", 1.0)))),
    }


def layer_mb(layers, layer_sizes):
    total = 0.0
    for l in layers:
        total += float(layer_sizes.get(l, layer_sizes.get(str(l), 0.0)))
    return total


def frag_node(demand, cap):
    pcpu = demand["cpu"] / max(cap["cpu"], 1e-9)
    pmem = demand["mem"] / max(cap["mem"], 1e-9)
    pdisk = demand["disk"] / max(cap["disk"], 1e-9)
    mean = (pcpu + pmem + pdisk) / 3.0
    return (abs(pcpu - mean) + abs(pmem - mean) + abs(pdisk - mean)) / 3.0


def normalize_cid(x):
    x = str(x)
    return x


def looks_like_node_assignment(obj):
    if not isinstance(obj, dict) or not obj:
        return False
    good = 0
    total = 0
    for k, v in obj.items():
        if isinstance(v, list):
            total += 1
            if len(v) == 0:
                good += 1
            elif all(isinstance(x, (str, int)) or isinstance(x, dict) for x in v):
                good += 1
    return total > 0 and good == total


def list_to_cids(v):
    out = []
    for x in v:
        if isinstance(x, (str, int)):
            out.append(str(x))
        elif isinstance(x, dict):
            for key in ["cid", "id", "container_id", "container", "name"]:
                if key in x:
                    out.append(str(x[key]))
                    break
    return out


def extract_assignment_from_dict(d):
    assignment_keys = [
        "assignment",
        "assignments",
        "ordered_queues",
        "queues",
        "node_queues",
        "node_to_containers",
        "placement_by_node",
        "placements_by_node",
    ]

    for key in assignment_keys:
        if key in d and looks_like_node_assignment(d[key]):
            return {str(k): list_to_cids(v) for k, v in d[key].items()}

    # container -> node
    for key in ["container_to_node", "cid_to_node", "placement_map"]:
        if key in d and isinstance(d[key], dict):
            mp = d[key]
            assign = defaultdict(list)
            for cid, nid in mp.items():
                assign[str(nid)].append(str(cid))
            if assign:
                return dict(assign)

    # list placement
    for key in ["placements", "placement", "schedule", "deployments"]:
        if key in d and isinstance(d[key], list):
            assign = defaultdict(list)
            for item in d[key]:
                if not isinstance(item, dict):
                    continue
                cid = None
                nid = None
                for ck in ["cid", "id", "container_id", "container", "name"]:
                    if ck in item:
                        cid = item[ck]
                        break
                for nk in ["node", "node_id", "eid", "edge", "host"]:
                    if nk in item:
                        nid = item[nk]
                        break
                if cid is not None and nid is not None:
                    assign[str(nid)].append(str(cid))
            if assign:
                return dict(assign)

    return None


def recursive_find_assignment(obj):
    if isinstance(obj, dict):
        direct = extract_assignment_from_dict(obj)
        if direct:
            return direct

        for v in obj.values():
            got = recursive_find_assignment(v)
            if got:
                return got

    elif isinstance(obj, list):
        # list of placement dicts
        assign = defaultdict(list)
        for item in obj:
            if isinstance(item, dict):
                cid = None
                nid = None
                for ck in ["cid", "id", "container_id", "container", "name"]:
                    if ck in item:
                        cid = item[ck]
                        break
                for nk in ["node", "node_id", "eid", "edge", "host"]:
                    if nk in item:
                        nid = item[nk]
                        break
                if cid is not None and nid is not None:
                    assign[str(nid)].append(str(cid))
        if assign:
            return dict(assign)

        for v in obj:
            got = recursive_find_assignment(v)
            if got:
                return got

    return None


def extract_assignment(result):
    assignment = recursive_find_assignment(result)
    if not assignment:
        raise RuntimeError("No assignment-like field found in result JSON.")
    return assignment


def align_assignment_node_ids(assignment, nodes):
    node_ids = set(nodes.keys())

    if set(assignment.keys()).issubset(node_ids):
        return assignment

    # Try edge index mapping if keys are numbers or node names not matching.
    node_list = list(nodes.keys())
    aligned = defaultdict(list)

    for k, cids in assignment.items():
        kk = str(k)
        if kk in node_ids:
            aligned[kk].extend(cids)
            continue

        # numeric 0-based or 1-based
        try:
            idx = int(kk)
            if 0 <= idx < len(node_list):
                aligned[node_list[idx]].extend(cids)
                continue
            if 1 <= idx <= len(node_list):
                aligned[node_list[idx - 1]].extend(cids)
                continue
        except Exception:
            pass

        # edge-X fallback
        if kk.startswith("edge-") and kk in node_ids:
            aligned[kk].extend(cids)

    return dict(aligned)


def compute_potential_history(case, result):
    layer_sizes = case.get("layer_sizes_mb", case.get("layer_sizes", {}))
    containers = case["containers"]

    nodes = {}
    for i, node in enumerate(case["nodes"]):
        nid = get_node_id(node, i)
        nodes[nid] = node

    assignment = extract_assignment(result)
    assignment = align_assignment_node_ids(assignment, nodes)

    cid_to_node = {}
    for nid, cids in assignment.items():
        if nid not in nodes:
            continue
        for cid in cids:
            cid_to_node[str(cid)] = nid

    node_cache = {}
    for nid, node in nodes.items():
        init_cache = node.get("initial_cache", node.get("cached_layers", node.get("layers", [])))
        node_cache[nid] = set(str(x) for x in init_cache)

    node_download = {nid: 0.0 for nid in nodes}
    node_reuse = {nid: 0.0 for nid in nodes}
    node_load = {nid: 0 for nid in nodes}
    node_demand = {nid: {"cpu": 0.0, "mem": 0.0, "disk": 0.0} for nid in nodes}
    node_cap = {nid: get_cap(node) for nid, node in nodes.items()}

    rows = []

    for t, c in enumerate(containers, start=1):
        cid = get_cid(c, t)
        if cid not in cid_to_node:
            # tolerate c003 vs 3 is hard; skip absent
            continue

        nid = cid_to_node[cid]
        if nid not in nodes:
            continue

        layers = set(get_layers(c))
        hit = layers & node_cache[nid]
        miss = layers - node_cache[nid]

        reused_mb = layer_mb(hit, layer_sizes)
        downloaded_mb = layer_mb(miss, layer_sizes)

        node_reuse[nid] += reused_mb
        node_download[nid] += downloaded_mb
        node_cache[nid] |= layers
        node_load[nid] += 1

        res = get_resources(c)
        node_demand[nid]["cpu"] += res["cpu"]
        node_demand[nid]["mem"] += res["mem"]
        node_demand[nid]["disk"] += res["disk"]

        raw_delay = 0.0
        raw_frag = 0.0
        raw_aff = 0.0
        raw_load = 0.0

        for u, node in nodes.items():
            bw = get_bw(node)
            raw_delay += (node_download[u] ** 2) / max(bw, 1e-9)
            raw_frag += frag_node(node_demand[u], node_cap[u])
            raw_aff += node_reuse[u]
            raw_load += node_load[u] ** 2

        delay_term = LAMBDA_DELAY * raw_delay
        frag_term = LAMBDA_FRAG * raw_frag
        aff_reward_term = LAMBDA_AFF * raw_aff
        load_term = LAMBDA_LOAD * raw_load
        phi_total = delay_term + frag_term - aff_reward_term + load_term

        rows.append({
            "iter": t,
            "progress": t / len(containers),
            "phi_total": phi_total,
            "delay_term": delay_term,
            "frag_term": frag_term,
            "aff_reward_term": aff_reward_term,
            "load_term": load_term,
        })

    if not rows:
        raise RuntimeError("No matched container ids between case and assignment.")

    return rows


def write_csv(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def safe_method_name(name):
    return (
        name.replace(" ", "_")
        .replace("/", "_")
        .replace("+", "plus")
        .replace("-", "_")
    )


def main():
    os.makedirs(OUTDIR, exist_ok=True)

    final_rows = []
    failures = []

    for N in REQS:
        case_path = CASE_TEMPLATE.format(N=N)
        if not os.path.exists(case_path):
            failures.append((N, "ALL", f"missing case {case_path}"))
            continue

        case = load_json(case_path)

        for m in METHODS:
            method = m["name"]
            result_path = find_existing(m["paths"], N)

            if result_path is None:
                failures.append((N, method, "missing result json"))
                continue

            try:
                result = load_json(result_path)
                hist = compute_potential_history(case, result)

                method_dir = os.path.join(OUTDIR, safe_method_name(method))
                csv_path = os.path.join(method_dir, f"potential_{safe_method_name(method)}_{N}.csv")
                write_csv(csv_path, hist)

                last = hist[-1]
                final_rows.append({
                    "requests": N,
                    "method": method,
                    "result_path": result_path,
                    "phi_total": last["phi_total"],
                    "delay_term": last["delay_term"],
                    "frag_term": last["frag_term"],
                    "aff_reward_term": last["aff_reward_term"],
                    "load_term": last["load_term"],
                })
                print("[OK]", N, method, result_path)

            except Exception as e:
                failures.append((N, method, str(e)))
                print("[FAIL]", N, method, str(e))

    # full summary
    summary_path = os.path.join(OUTDIR, "summary_potential_final_all_methods.md")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("| requests | method | phi_total | delay_term | frag_term | aff_reward_term | load_term |\n")
        f.write("|---:|---|---:|---:|---:|---:|---:|\n")
        for r in sorted(final_rows, key=lambda x: (x["requests"], x["method"])):
            f.write("| {} | {} | {:.3f} | {:.3f} | {:.3f} | {:.3f} | {:.3f} |\n".format(
                r["requests"],
                r["method"],
                r["phi_total"],
                r["delay_term"],
                r["frag_term"],
                r["aff_reward_term"],
                r["load_term"],
            ))

        f.write(f"\nFailures: {len(failures)}\n")
        for N, method, reason in failures:
            f.write(f"\n[FAIL] N={N}, method={method}, reason={reason}")

    # matrices for plotting
    components = ["phi_total", "delay_term", "frag_term", "aff_reward_term", "load_term"]
    method_names = [m["name"] for m in METHODS]
    mp = {(r["requests"], r["method"]): r for r in final_rows}

    for comp in components:
        out = os.path.join(OUTDIR, f"plot_{comp}_matrix.md")
        with open(out, "w", encoding="utf-8") as f:
            f.write(f"# {comp}\n\n")
            f.write("| requests | " + " | ".join(method_names) + " |\n")
            f.write("|---:|" + "|".join(["---:"] * len(method_names)) + "|\n")
            for N in REQS:
                vals = []
                for method in method_names:
                    r = mp.get((N, method))
                    vals.append("MISSING" if r is None else f"{r[comp]:.3f}")
                f.write("| {} | {} |\n".format(N, " | ".join(vals)))

    # failure table
    fail_path = os.path.join(OUTDIR, "potential_failures.md")
    with open(fail_path, "w", encoding="utf-8") as f:
        f.write("| requests | method | reason |\n")
        f.write("|---:|---|---|\n")
        for N, method, reason in failures:
            f.write(f"| {N} | {method} | {reason} |\n")

    print("\n[SUMMARY]", summary_path)
    print("[FAILURES]", fail_path)
    print("[N_FAILURES]", len(failures))


if __name__ == "__main__":
    main()
