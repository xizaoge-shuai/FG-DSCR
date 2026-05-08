import json
import os
import copy

def load(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def save(obj, p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)

def find_src(cap, n):
    candidates = [
        f"cases/drtp_cache_sweep_88/drtp_img88_cache_{cap}mb_{n}.json",
        f"cases/drtp_large_v2/drtp_img88_cache_{cap}mb_{n}.json",
    ]
    if cap == 1024:
        candidates.append(f"cases/drtp_large_v2/drtp_img88_cache_1024mb_{n}.json")
    for p in candidates:
        if os.path.exists(p):
            return p
    return None

# 8 个节点的异构倍率，平均值为 1.0
multipliers = [0.50, 0.75, 1.00, 1.25, 1.50, 1.25, 1.00, 0.75]

for cap in [1024, 1536, 2048]:
    for n in [200, 500, 1000]:
        src = find_src(cap, n)
        if src is None:
            print("[MISS SRC]", cap, n)
            continue

        obj = load(src)
        nodes = obj["nodes"]

        for i, node in enumerate(nodes):
            m = multipliers[i % len(multipliers)]
            node["repo_capacity_mb"] = int(round(cap * m))

        obj.setdefault("meta", {})
        obj["meta"]["heterogeneous_cache"] = True
        obj["meta"]["base_cache_mb"] = cap
        obj["meta"]["cache_multipliers"] = multipliers
        obj["meta"]["generated_from"] = src

        dst = f"cases/drtp_cache_sweep_hetero88/drtp_img88_heterocache_{cap}mb_{n}.json"
        save(obj, dst)
        print("[OK]", dst)
