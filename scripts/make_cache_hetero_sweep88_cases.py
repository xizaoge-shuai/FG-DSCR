import json
import os
import copy

REQS = [200,300,400,500,600,700,800,900,1000]
CAPS = [0,128,256,384,512,640,768,896,1024]

# 8 个节点异构比例，均值为 1，总 cache 与同构场景保持一致
RATIOS = [0.5, 0.75, 1.0, 1.25, 1.5, 0.5, 1.0, 1.5]

def load(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def save(obj, p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)

for n in REQS:
    src = f"cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_{n}.json"
    if not os.path.exists(src):
        src = f"cases/drtp_cache_sweep_88/drtp_img88_cache_1024mb_{n}.json"

    if not os.path.exists(src):
        print("[MISS BASE]", src)
        continue

    base = load(src)

    for cap in CAPS:
        obj = copy.deepcopy(base)

        for i, node in enumerate(obj["nodes"]):
            ratio = RATIOS[i % len(RATIOS)]
            c = int(round(cap * ratio))

            node["repo_capacity_mb"] = c
            node["cache_capacity_mb"] = c
            node["cache_mb"] = c

            if "storage_capacity_mb" in node:
                del node["storage_capacity_mb"]

        obj.setdefault("meta", {})
        obj["meta"]["cache_heterogeneous"] = True
        obj["meta"]["avg_cache_mb"] = cap
        obj["meta"]["cache_ratios"] = RATIOS
        obj["meta"]["total_extra_cache_mb"] = int(round(cap * len(obj["nodes"])))
        obj["meta"]["cache_definition"] = "extra reusable layer cache, decoupled from native runtime storage"

        dst = f"cases/drtp_cache_hetero_sweep_88/drtp_img88_cachehetero_avg{cap}mb_{n}.json"
        save(obj, dst)
        print("[OK]", dst)
