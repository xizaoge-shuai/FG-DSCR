import json
import os

ROOT = "results/drtp/final_exp/phase2_fixed88"

variants = [
    ("FIFO", "fifo"),
    ("Random", "random"),
    ("Static ILR-SA", "static"),
    ("Dynamic w/o FutureShare", "dynamic_no_future"),
    ("Dynamic + LRU", "dynamic_lru"),
    ("Dynamic + LFU", "dynamic_lfu"),
    ("Dynamic + PGDSF", "dynamic_pgdsf"),
]

def load(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

print("| requests | method | ACT | AMS | Obj | pull_time | downloaded_mb | reused_mb | cache_hit_mb | evicted_mb | reuse_rate | degradation_vs_full_% |")
print("|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")

for n in [200, 500, 1000]:
    full_p = os.path.join(ROOT, f"dynamic_pgdsf_{n}.json")
    if not os.path.exists(full_p):
        print(f"[MISSING FULL] {full_p}")
        continue
    full = load(full_p)["summary"]
    full_obj = float(full["objective"])

    for name, stem in variants:
        p = os.path.join(ROOT, f"{stem}_{n}.json")
        if not os.path.exists(p):
            continue
        s = load(p)["summary"]
        obj = float(s["objective"])
        deg = (obj - full_obj) / max(full_obj, 1e-9) * 100.0

        print("| {} | {} | {:.3f} | {:.3f} | {:.3f} | {:.3f} | {} | {} | {} | {} | {:.6f} | {:.2f} |".format(
            n,
            name,
            float(s["ACT"]),
            float(s["AMS"]),
            obj,
            float(s.get("pull_time", 0.0)),
            int(s.get("downloaded_mb", 0)),
            int(s.get("reused_mb", 0)),
            int(s.get("cache_hit_mb", s.get("reused_mb", 0))),
            int(s.get("evicted_mb", 0)),
            float(s.get("reuse_rate", 0.0)),
            deg,
        ))
