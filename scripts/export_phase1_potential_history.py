import argparse
import csv
import json
import os
from collections import defaultdict

def load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

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

def get_bw(node):
    for k in ["bandwidth_mb_s", "bandwidth", "download_bandwidth", "pull_bandwidth", "bw"]:
        if k in node:
            return float(node[k])
    return 100.0

def get_layers(c):
    return list(c.get("layers", c.get("image_layers", [])))

def layer_mb(layers, layer_sizes):
    return sum(float(layer_sizes.get(str(x), layer_sizes.get(x, 0.0))) for x in layers)

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

def frag_node(demand, cap):
    pcpu = demand["cpu"] / max(cap["cpu"], 1e-9)
    pmem = demand["mem"] / max(cap["mem"], 1e-9)
    pdisk = demand["disk"] / max(cap["disk"], 1e-9)
    mean = (pcpu + pmem + pdisk) / 3.0
    return (abs(pcpu - mean) + abs(pmem - mean) + abs(pdisk - mean)) / 3.0

def extract_assignment(result):
    # common formats
    for key in ["assignment", "ordered_queues", "queues", "node_queues"]:
        if key in result and isinstance(result[key], dict):
            return {str(k): [str(x) for x in v] for k, v in result[key].items()}

    # nested result
    for root_key in ["result", "details", "schedule"]:
        if root_key in result and isinstance(result[root_key], dict):
            nested = result[root_key]
            for key in ["assignment", "ordered_queues", "queues", "node_queues"]:
                if key in nested and isinstance(nested[key], dict):
                    return {str(k): [str(x) for x in v] for k, v in nested[key].items()}

    # list of placements
    for key in ["placements", "placement"]:
        if key in result and isinstance(result[key], list):
            assign = defaultdict(list)
            for item in result[key]:
                cid = item.get("cid", item.get("container_id", item.get("container")))
                nid = item.get("node", item.get("node_id", item.get("eid")))
                if cid is not None and nid is not None:
                    assign[str(nid)].append(str(cid))
            if assign:
                return dict(assign)

    raise RuntimeError(
        "No assignment found. Please inspect result keys: "
        + ", ".join(result.keys())
    )

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--result", required=True)
    ap.add_argument("--out-csv", required=True)
    ap.add_argument("--lambda-delay", type=float, default=1.0)
    ap.add_argument("--lambda-frag", type=float, default=1.0)
    ap.add_argument("--lambda-aff", type=float, default=0.2)
    ap.add_argument("--lambda-load", type=float, default=1.0)
    args = ap.parse_args()

    case = load_json(args.case)
    result = load_json(args.result)

    layer_sizes = case.get("layer_sizes_mb", case.get("layer_sizes", {}))
    containers = case["containers"]
    nodes_list = case["nodes"]

    nodes = {}
    for i, node in enumerate(nodes_list):
        nid = get_node_id(node, i)
        nodes[nid] = node

    assignment = extract_assignment(result)

    cid_to_node = {}
    for nid, cids in assignment.items():
        for cid in cids:
            cid_to_node[str(cid)] = str(nid)

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
            continue

        nid = cid_to_node[cid]
        if nid not in nodes:
            # tolerate edge id mismatch by mapping edge-idx
            continue

        layers = set(str(x) for x in get_layers(c))
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

        delay_term = args.lambda_delay * raw_delay
        frag_term = args.lambda_frag * raw_frag
        aff_reward_term = args.lambda_aff * raw_aff
        load_term = args.lambda_load * raw_load
        phi_total = delay_term + frag_term - aff_reward_term + load_term

        rows.append({
            "iter": t,
            "progress": t / len(containers),
            "container_id": cid,
            "chosen_node": nid,
            "raw_delay": raw_delay,
            "raw_frag": raw_frag,
            "raw_aff": raw_aff,
            "raw_load": raw_load,
            "delay_term": delay_term,
            "frag_term": frag_term,
            "aff_reward_term": aff_reward_term,
            "aff_contrib_to_phi": -aff_reward_term,
            "load_term": load_term,
            "phi_total": phi_total,
        })

    os.makedirs(os.path.dirname(args.out_csv), exist_ok=True)

    if not rows:
        raise RuntimeError("No rows exported. Check assignment/container ids.")

    with open(args.out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print("[OK]", args.out_csv, "rows=", len(rows))

if __name__ == "__main__":
    main()
