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

def find_base_case(k):
    candidates = [
        f"cases/drtp_large_v2/drtp_img{k}_cache_1024mb_1000.json",
        f"cases/drtp_diversity/drtp_img{k}_cache_1024mb_1000.json",
        f"cases/drtp_cache_sweep/drtp_cache_1024mb_1000.json" if k == 16 else "",
        f"cases/drtp_cache_sweep_88/drtp_img88_cache_1024mb_1000.json" if k == 88 else "",
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return None

def node_id_key(node):
    for key in ["eid", "id", "name", "node_id"]:
        if key in node:
            return key
    return "eid"

def expand_nodes(nodes, e):
    out = []
    for i in range(e):
        base = copy.deepcopy(nodes[i % len(nodes)])
        key = node_id_key(base)
        base[key] = f"edge-{i+1}"
        out.append(base)
    return out

def expand_containers(containers, n):
    out = []
    for i in range(n):
        base = copy.deepcopy(containers[i % len(containers)])
        base["cid"] = f"{base.get('cid', 'c')}_scale_{i}"
        out.append(base)
    return out

for k in [16, 50, 68, 88]:
    src = find_base_case(k)
    if src is None:
        print("[MISS BASE]", k)
        continue

    base = load(src)

    for e in [4, 6, 8, 10, 12, 14, 16, 18]:
        for n in [200, 400, 600, 800, 1000, 1200, 1500, 2000]:
            obj = copy.deepcopy(base)
            obj["nodes"] = expand_nodes(base["nodes"], e)
            obj["containers"] = expand_containers(base["containers"], n)

            obj.setdefault("meta", {})
            obj["meta"]["scale_nodes_case"] = True
            obj["meta"]["catalog_size"] = k
            obj["meta"]["num_nodes"] = e
            obj["meta"]["num_containers"] = n
            obj["meta"]["generated_from"] = src

            dst = f"cases/drtp_scale_nodes/drtp_img{k}_nodes{e}_cache1024mb_{n}.json"
            save(obj, dst)
            print("[OK]", dst)
