import json
import os
import copy

REQS = [200,300,400,500,600,700,800,900,1000]
CAPS = [1024,1152,1280,1408,1536,1664,1792,2048]

def load(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def save(obj, p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)

def set_cache_cap(node, cap):
    node["repo_capacity_mb"] = cap
    node["cache_capacity_mb"] = cap
    node["storage_capacity_mb"] = cap
    return node

for n in REQS:
    base_candidates = [
        f"cases/drtp_cache_sweep_88/drtp_img88_cache_1024mb_{n}.json",
        f"cases/drtp_large_v2/drtp_img88_cache_1024mb_{n}.json",
    ]

    src = None
    for p in base_candidates:
        if os.path.exists(p):
            src = p
            break

    if src is None:
        print("[MISS BASE]", n)
        continue

    base = load(src)

    for cap in CAPS:
        dst = f"cases/drtp_cache_sweep_88/drtp_img88_cache_{cap}mb_{n}.json"

        obj = copy.deepcopy(base)
        for node in obj["nodes"]:
            set_cache_cap(node, cap)

        obj.setdefault("meta", {})
        obj["meta"]["generated_cache_mb"] = cap
        obj["meta"]["generated_request_size"] = n
        obj["meta"]["generated_from"] = src

        save(obj, dst)
        print("[OK]", dst)
