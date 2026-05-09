import json
import os
import copy

REQS = [200, 400, 600, 800, 1000]
CAPS = [0, 32, 64, 128, 256, 512, 1024]

def load(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def save(obj, p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)

def set_cache_only(node, cap):
    node["repo_capacity_mb"] = cap
    node["cache_capacity_mb"] = cap
    node["cache_mb"] = cap
    if "storage_capacity_mb" in node:
        del node["storage_capacity_mb"]

for n in REQS:
    src = f"cases/drtp_scale_nodes/drtp_img88_nodes18_cache1024mb_{n}.json"
    if not os.path.exists(src):
        print("[MISS]", src)
        continue

    base = load(src)

    for cap in CAPS:
        obj = copy.deepcopy(base)
        for node in obj["nodes"]:
            set_cache_only(node, cap)

        obj.setdefault("meta", {})
        obj["meta"]["cache_only_sweep"] = True
        obj["meta"]["edge_nodes"] = 18
        obj["meta"]["cache_capacity_mb_per_node"] = cap
        obj["meta"]["total_extra_cache_mb"] = 18 * cap
        obj["meta"]["cache_definition"] = "extra reusable layer cache, decoupled from native runtime storage"
        obj["meta"]["generated_from"] = src

        dst = f"cases/drtp_cache_small_nodes18_88/drtp_img88_nodes18_cache{cap}mb_{n}.json"
        save(obj, dst)
        print("[OK]", dst)
