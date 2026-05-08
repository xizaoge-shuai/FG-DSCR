import json
import os
import math
import statistics

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

def node_id(n, i):
    return n.get("eid") or n.get("id") or n.get("name") or n.get("node_id") or f"edge-{i+1}"

def get_assignment(res):
    if "assignment" in res:
        return res["assignment"]
    if "ordered_queues" in res:
        return res["ordered_queues"]
    raise RuntimeError("No assignment or ordered_queues found")

def var(xs):
    if not xs:
        return 0.0
    m = sum(xs) / len(xs)
    return sum((x - m) ** 2 for x in xs) / len(xs)

def summarize_resources(case, res):
    nodes = {node_id(n, i): n for i, n in enumerate(case["nodes"])}
    containers = {c["cid"]: c for c in case["containers"]}
    assignment = get_assignment(res)

    loads = []
    cpu_pressures = []
    mem_pressures = []
    disk_pressures = []
    total_pressures = []

    frag_cpu_list = []
    frag_mem_list = []
    frag_disk_list = []
    frag_score_list = []

    for eid, cids in assignment.items():
        n = nodes[eid]
        cap = n.get("resources", {})

        cpu_cap = float(cap.get("cpu", 1.0))
        mem_cap = float(cap.get("mem", 1.0))
        disk_cap = float(cap.get("disk", 1.0))

        cpu_d = mem_d = disk_d = 0.0

        for cid in cids:
            r = containers[cid].get("resources", {})
            cpu_d += float(r.get("cpu", 0.0))
            mem_d += float(r.get("mem", 0.0))
            disk_d += float(r.get("disk", 0.0))

        p_cpu = cpu_d / max(cpu_cap, 1e-9)
        p_mem = mem_d / max(mem_cap, 1e-9)
        p_disk = disk_d / max(disk_cap, 1e-9)

        pressures = [p_cpu, p_mem, p_disk]
        mean_p = sum(pressures) / 3.0

        f_cpu = abs(p_cpu - mean_p)
        f_mem = abs(p_mem - mean_p)
        f_disk = abs(p_disk - mean_p)
        f_score = (f_cpu + f_mem + f_disk) / 3.0

        loads.append(len(cids))
        cpu_pressures.append(p_cpu)
        mem_pressures.append(p_mem)
        disk_pressures.append(p_disk)
        total_pressures.append(sum(pressures))

        frag_cpu_list.append(f_cpu)
        frag_mem_list.append(f_mem)
        frag_disk_list.append(f_disk)
        frag_score_list.append(f_score)

    return {
        "max_load": max(loads) if loads else 0,
        "load_var": var(loads),

        "avg_cpu_pressure": sum(cpu_pressures) / max(len(cpu_pressures), 1),
        "avg_mem_pressure": sum(mem_pressures) / max(len(mem_pressures), 1),
        "avg_disk_pressure": sum(disk_pressures) / max(len(disk_pressures), 1),

        "var_cpu_pressure": var(cpu_pressures),
        "var_mem_pressure": var(mem_pressures),
        "var_disk_pressure": var(disk_pressures),
        "var_total_pressure": var(total_pressures),

        "frag_cpu": sum(frag_cpu_list) / max(len(frag_cpu_list), 1),
        "frag_mem": sum(frag_mem_list) / max(len(frag_mem_list), 1),
        "frag_disk": sum(frag_disk_list) / max(len(frag_disk_list), 1),
        "fragmentation_score": sum(frag_score_list) / max(len(frag_score_list), 1),
    }

print("| requests | method | Obj | downloaded_mb | reuse_rate | max_load | load_var | avg_cpu_pressure | avg_mem_pressure | avg_disk_pressure | var_total_pressure | frag_cpu | frag_mem | frag_disk | fragmentation_score |")
print("|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")

for n in [200, 500, 1000]:
    case_path = f"cases/drtp_large_v2/drtp_img88_cache_1024mb_{n}.json"
    case = load(case_path)

    for name, stem in variants:
        p = os.path.join(ROOT, f"{stem}_{n}.json")
        if not os.path.exists(p):
            print(f"[MISSING] {p}")
            continue

        res = load(p)
        s = res["summary"]
        m = summarize_resources(case, res)

        print("| {} | {} | {:.3f} | {} | {:.6f} | {} | {:.3f} | {:.6f} | {:.6f} | {:.6f} | {:.6f} | {:.6f} | {:.6f} | {:.6f} | {:.6f} |".format(
            n,
            name,
            float(s["objective"]),
            int(s["downloaded_mb"]),
            float(s["reuse_rate"]),
            int(m["max_load"]),
            float(m["load_var"]),
            float(m["avg_cpu_pressure"]),
            float(m["avg_mem_pressure"]),
            float(m["avg_disk_pressure"]),
            float(m["var_total_pressure"]),
            float(m["frag_cpu"]),
            float(m["frag_mem"]),
            float(m["frag_disk"]),
            float(m["fragmentation_score"]),
        ))
