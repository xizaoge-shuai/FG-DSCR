import json
import os
import math

ROOT = "results/drtp/final_exp/phase1_placement88"

variants = [
    ("Resource-Greedy", "resource_greedy"),
    ("LayerLocality-Greedy", "layer_locality_greedy"),
    ("LRScheduler-inspired", "lrscheduler"),
    ("w/o soft load cap", "no_softcap"),
    ("w/o frag", "no_frag"),
    ("FG-DSCR-GC", "full"),
]

def load(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def get_assignment(res):
    if "assignment" in res:
        return res["assignment"]
    if "ordered_queues" in res:
        return res["ordered_queues"]
    raise RuntimeError("No assignment or ordered_queues found")

def node_id(n, i):
    return n.get("eid") or n.get("id") or n.get("name") or n.get("node_id") or f"edge-{i+1}"

def summarize_resources(case, res):
    nodes = {node_id(n, i): n for i, n in enumerate(case["nodes"])}
    containers = {c["cid"]: c for c in case["containers"]}
    assignment = get_assignment(res)

    loads = []
    frag_cpu = []
    frag_mem = []
    frag_disk = []
    frag_scores = []

    for eid, cids in assignment.items():
        n = nodes[eid]
        cap = n.get("resources", {})
        cpu_cap = float(cap.get("cpu", 1.0))
        mem_cap = float(cap.get("mem", 1.0))
        disk_cap = float(cap.get("disk", 1.0))

        used_cpu = used_mem = used_disk = 0.0
        for cid in cids:
            r = containers[cid].get("resources", {})
            used_cpu += float(r.get("cpu", 0.0))
            used_mem += float(r.get("mem", 0.0))
            used_disk += float(r.get("disk", 0.0))

        loads.append(len(cids))

        rem_cpu = max(cpu_cap - used_cpu, 0.0) / max(cpu_cap, 1e-9)
        rem_mem = max(mem_cap - used_mem, 0.0) / max(mem_cap, 1e-9)
        rem_disk = max(disk_cap - used_disk, 0.0) / max(disk_cap, 1e-9)

        mean_rem = (rem_cpu + rem_mem + rem_disk) / 3.0
        frag_cpu.append(abs(rem_cpu - mean_rem))
        frag_mem.append(abs(rem_mem - mean_rem))
        frag_disk.append(abs(rem_disk - mean_rem))
        frag_scores.append((abs(rem_cpu - mean_rem) + abs(rem_mem - mean_rem) + abs(rem_disk - mean_rem)) / 3.0)

    mean_load = sum(loads) / max(len(loads), 1)
    load_var = sum((x - mean_load) ** 2 for x in loads) / max(len(loads), 1)
    max_load = max(loads) if loads else 0

    return {
        "max_load": max_load,
        "load_var": load_var,
        "fragmented_cpu": sum(frag_cpu) / max(len(frag_cpu), 1),
        "fragmented_mem": sum(frag_mem) / max(len(frag_mem), 1),
        "fragmented_disk": sum(frag_disk) / max(len(frag_disk), 1),
        "fragmentation_score": sum(frag_scores) / max(len(frag_scores), 1),
    }

print("| requests | method | ACT | AMS | Obj | downloaded_mb | reuse_rate | max_load | load_var | fragmented_cpu | fragmented_mem | fragmented_disk | fragmentation_score |")
print("|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")

for n in [200, 500, 1000]:
    case = load(f"cases/drtp_large_v2/drtp_img88_cache_1024mb_{n}.json")
    for name, stem in variants:
        p = os.path.join(ROOT, f"{stem}_{n}.json")
        if not os.path.exists(p):
            print(f"[MISSING] {p}")
            continue
        res = load(p)
        s = res["summary"]
        m = summarize_resources(case, res)
        print("| {} | {} | {:.3f} | {:.3f} | {:.3f} | {} | {:.6f} | {} | {:.3f} | {:.6f} | {:.6f} | {:.6f} | {:.6f} |".format(
            n, name,
            float(s["ACT"]),
            float(s["AMS"]),
            float(s["objective"]),
            int(s["downloaded_mb"]),
            float(s["reuse_rate"]),
            int(m["max_load"]),
            float(m["load_var"]),
            float(m["fragmented_cpu"]),
            float(m["fragmented_mem"]),
            float(m["fragmented_disk"]),
            float(m["fragmentation_score"]),
        ))
