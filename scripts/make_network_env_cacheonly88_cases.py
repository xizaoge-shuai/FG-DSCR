import json
import os
import copy

SCENARIOS = {
    "homo_good":  [150,150,150,150,150,150,150,150],
    "homo_bad":   [50,50,50,50,50,50,50,50],
    "hetero_good":[150,150,120,120,150,120,150,120],
    "hetero_bad": [80,80,50,50,80,50,80,50],
}

CAPS = [0,128,256,384,512,640,768,896,1024]
REQS = [200,300,400,500,600,700,800,900,1000]

def load(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def save(obj, p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)

for scen, bws in SCENARIOS.items():
    for cap in CAPS:
        for n in REQS:
            src = f"cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_{cap}mb_{n}.json"
            if not os.path.exists(src):
                print("[MISS SRC]", src)
                continue

            obj = load(src)

            for i, node in enumerate(obj["nodes"]):
                bw = bws[i % len(bws)]
                node["bandwidth_mb_s"] = bw
                node["bandwidth"] = bw
                node["download_bandwidth"] = bw
                node["pull_bandwidth"] = bw

            obj.setdefault("meta", {})
            obj["meta"]["network_scenario"] = scen
            obj["meta"]["bandwidth_pattern"] = bws
            obj["meta"]["cache_only_sweep"] = True
            obj["meta"]["generated_from"] = src

            dst = f"cases/drtp_network_env_cacheonly_88/drtp_img88_{scen}_cacheonly{cap}mb_{n}.json"
            save(obj, dst)
            print("[OK]", dst)
