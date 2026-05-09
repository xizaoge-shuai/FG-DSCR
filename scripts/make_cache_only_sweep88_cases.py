import json
import os
import copy

REQS = [200,300,400,500,600,700,800,900,1000]
CAPS = [0,128,256,384,512,640,768,896,1024]

def load(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def save(obj, p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)

def set_cache_only(node, cap):
    # 只改缓存容量，不改 resources.disk，不改节点物理存储资源
    node["repo_capacity_mb"] = cap
    node["cache_capacity_mb"] = cap
    node["cache_mb"] = cap

    # 删除可能被误用为硬 storage 的字段，避免小 cache 导致 infeasible
    # 注意：不动 node["resources"]["disk"]
    for k in ["storage_capacity_mb"]:
        if k in node:
            del node[k]

for n in REQS:
    src = f"cases/drtp_cache_sweep_88/drtp_img88_cache_1024mb_{n}.json"
    if not os.path.exists(src):
        src = f"cases/drtp_large_v2/drtp_img88_cache_1024mb_{n}.json"

    if not os.path.exists(src):
        print("[MISS BASE]", n)
        continue

    base = load(src)

    for cap in CAPS:
        obj = copy.deepcopy(base)
        for node in obj["nodes"]:
            set_cache_only(node, cap)

        obj.setdefault("meta", {})
        obj["meta"]["cache_only_sweep"] = True
        obj["meta"]["cache_capacity_mb"] = cap
        obj["meta"]["generated_from"] = src

        dst = f"cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_{cap}mb_{n}.json"
        save(obj, dst)
        print("[OK]", dst)
