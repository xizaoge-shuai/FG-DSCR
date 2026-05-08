import json
import os
import argparse

def load(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def save(obj, p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)

def set_profile(c, idx, mode):
    # 节点资源通常是 cpu=24, mem=64, disk=128
    # 这里保证单个容器仍然可行，但资源结构明显不同。
    if mode == "mixed":
        profiles = [
            {"cpu": 8, "mem": 4,  "disk": 6},    # CPU-heavy
            {"cpu": 2, "mem": 16, "disk": 6},    # MEM-heavy
            {"cpu": 2, "mem": 4,  "disk": 24},   # DISK-heavy
            {"cpu": 4, "mem": 8,  "disk": 12},   # balanced-ish
        ]
        c["resources"] = dict(profiles[idx % len(profiles)])

    elif mode == "cpu_skew":
        if idx % 2 == 0:
            c["resources"] = {"cpu": 10, "mem": 4, "disk": 6}
        else:
            c["resources"] = {"cpu": 3, "mem": 8, "disk": 8}

    elif mode == "mem_skew":
        if idx % 2 == 0:
            c["resources"] = {"cpu": 3, "mem": 20, "disk": 6}
        else:
            c["resources"] = {"cpu": 4, "mem": 6, "disk": 8}

    elif mode == "disk_skew":
        if idx % 2 == 0:
            c["resources"] = {"cpu": 3, "mem": 4, "disk": 32}
        else:
            c["resources"] = {"cpu": 4, "mem": 8, "disk": 8}
    else:
        raise ValueError(mode)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=["mixed", "cpu_skew", "mem_skew", "disk_skew"])
    args = ap.parse_args()

    for n in [200, 500, 1000]:
        src = f"cases/drtp_large_v2/drtp_img88_cache_1024mb_{n}.json"
        dst = f"cases/drtp_resource_stress_88/drtp_img88_resource_{args.mode}_cache_1024mb_{n}.json"

        obj = load(src)

        for idx, c in enumerate(obj["containers"]):
            set_profile(c, idx, args.mode)

        obj.setdefault("meta", {})
        obj["meta"]["resource_stress_mode"] = args.mode
        obj["meta"]["generated_from"] = src

        save(obj, dst)
        print("[OK]", dst)

if __name__ == "__main__":
    main()
