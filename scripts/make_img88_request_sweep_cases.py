import json
import os
import copy

REQS = [200, 300, 400, 500, 600, 700, 800, 900, 1000]
CAPS = [100, 128, 256, 384, 512, 640, 768, 1024, 1536, 2048]

def load(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def save(obj, p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)

def find_base(cap):
    candidates = [
        f"cases/drtp_cache_sweep_88/drtp_img88_cache_{cap}mb_1000.json",
        f"cases/drtp_large_v2_cache_sweep/drtp_img88_cache_{cap}mb_1000.json",
        f"cases/drtp_large_v2/drtp_img88_cache_1024mb_1000.json" if cap == 1024 else "",
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return None

for cap in CAPS:
    src = find_base(cap)
    if src is None:
        print("[MISS BASE]", cap)
        continue

    base = load(src)
    containers = base["containers"]

    for n in REQS:
        dst = f"cases/drtp_cache_sweep_88/drtp_img88_cache_{cap}mb_{n}.json"
        if os.path.exists(dst):
            print("[EXIST]", dst)
            continue

        obj = copy.deepcopy(base)
        obj["containers"] = copy.deepcopy(containers[:n])
        obj.setdefault("meta", {})
        obj["meta"]["generated_request_size"] = n
        obj["meta"]["generated_from"] = src
        obj["meta"]["repo_capacity_mb"] = cap

        save(obj, dst)
        print("[OK]", dst)
