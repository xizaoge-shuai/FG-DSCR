import json
import re
from pathlib import Path
from collections import defaultdict, OrderedDict

ROOT = Path("results/drtp/k8s_same_scale")
RES_DIR = ROOT / "fig2_dynamic_gain"
CASE_DIR = ROOT / "cases/fig2_dynamic_gain"
OUT = ROOT / "tables_delay_frag_no_fgor_recomputed"
OUT.mkdir(parents=True, exist_ok=True)

METHODS = ["ILR-SA", "LRScheduler", "GAHRL", "ORR", "LASA", "FG-DSCR-GC"]
CACHES = [128, 256, 384, 512, 640, 768, 896, 1024]

def load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def method_name(fn, s):
    algo = str(s.get("algo", ""))

    if "selected" in algo:
        return "FG-DSCR-GC"
    if "orig" in algo:
        return "FG-orig"
    if "ILR" in algo:
        return "ILR-SA"
    if "LRScheduler" in algo:
        return "LRScheduler"
    if "GAHRL" in algo:
        return "GAHRL"
    if "ORR" in algo:
        return "ORR"
    if "LASA" in algo:
        return "LASA"

    if fn.startswith("ilrsa"):
        return "ILR-SA"
    if fn.startswith("lrs"):
        return "LRScheduler"
    if fn.startswith("gahrl"):
        return "GAHRL"
    if fn.startswith("orr"):
        return "ORR"
    if fn.startswith("lasa"):
        return "LASA"
    if fn.startswith("fg_orig"):
        return "FG-orig"
    if fn.startswith("fg_"):
        return "FG-DSCR-GC"

    return fn

def get_assignment(result):
    if isinstance(result.get("assignment"), dict):
        return result["assignment"]
    if isinstance(result.get("assignments"), dict):
        return result["assignments"]
    return {}

def build_layer_sizes(case):
    sizes = {}

    for key in ["layer_sizes_mb", "layer_sizes", "layers_size", "layer_size"]:
        obj = case.get(key)
        if isinstance(obj, dict):
            for k, v in obj.items():
                try:
                    sizes[str(k)] = float(v)
                except Exception:
                    pass

    # 兼容 layers 列表
    layers = case.get("layers")
    if isinstance(layers, list):
        for x in layers:
            if isinstance(x, dict):
                lid = x.get("id") or x.get("layer_id") or x.get("lid") or x.get("name")
                sz = x.get("size_mb") or x.get("size") or x.get("mb")
                if lid is not None and sz is not None:
                    try:
                        sizes[str(lid)] = float(sz)
                    except Exception:
                        pass

    return sizes

def get_container_layers(case):
    """
    返回 cid -> [layer_id]
    尽量兼容不同 case schema。
    """
    cid_layers = {}

    images = {}
    for key in ["images", "image_catalog", "catalog"]:
        obj = case.get(key)
        if isinstance(obj, dict):
            images.update(obj)

    for c in case.get("containers", []):
        cid = c.get("cid") or c.get("id") or c.get("name")
        if cid is None:
            continue
        cid = str(cid)

        layers = None
        for key in ["layers", "layer_ids", "image_layers", "required_layers"]:
            if isinstance(c.get(key), list):
                layers = c.get(key)
                break

        if layers is None:
            img = c.get("image") or c.get("image_id") or c.get("repo") or c.get("image_name")
            if img is not None and str(img) in images:
                im = images[str(img)]
                if isinstance(im, dict):
                    for key in ["layers", "layer_ids", "image_layers"]:
                        if isinstance(im.get(key), list):
                            layers = im.get(key)
                            break
                elif isinstance(im, list):
                    layers = im

        if layers is None:
            layers = []

        norm_layers = []
        for x in layers:
            if isinstance(x, dict):
                lid = x.get("id") or x.get("layer_id") or x.get("lid") or x.get("name")
                if lid is not None:
                    norm_layers.append(str(lid))
            else:
                norm_layers.append(str(x))

        cid_layers[cid] = norm_layers

    return cid_layers

def layer_size(lid, layer_sizes):
    return float(layer_sizes.get(str(lid), 0.0))

def replay_dynamic_terms(case, result):
    """
    对最终 assignment 做统一 post-hoc replay。
    不是说 baseline 用了这个 gain，而是用相同公式评价它们的二阶段缓存状态。
    """
    assignment = get_assignment(result)
    nodes = {str(n.get("eid")): n for n in case.get("nodes", [])}

    layer_sizes = build_layer_sizes(case)
    cid_layers = get_container_layers(case)

    total_reuse = 0.0
    total_future = 0.0
    total_pull = 0.0
    total_evict = 0.0
    n_steps = 0

    for eid, cids in assignment.items():
        eid = str(eid)
        if eid not in nodes:
            continue

        cap = float(nodes[eid].get("repo_capacity_mb", 0.0))
        if cap < 0:
            cap = 0.0

        # cache: OrderedDict[layer_id] = size_mb
        # 用 LRU 风格 post-hoc replay，保证所有算法评价口径一致
        cache = OrderedDict()
        cache_size = 0.0

        seq = [str(x) for x in cids]

        # future layer count for each position
        future_counts = []
        counter = defaultdict(int)
        for cid in seq:
            for lid in cid_layers.get(cid, []):
                counter[lid] += 1

        for idx, cid in enumerate(seq):
            layers = cid_layers.get(cid, [])

            # 当前容器从 future counter 中移除
            for lid in layers:
                counter[lid] -= 1
                if counter[lid] <= 0:
                    counter.pop(lid, None)

            reuse_mb = sum(layer_size(lid, layer_sizes) for lid in layers if lid in cache)
            future_mb = sum(layer_size(lid, layer_sizes) for lid in layers if lid in counter)

            missing = [lid for lid in layers if lid not in cache]
            pull_mb = sum(layer_size(lid, layer_sizes) for lid in missing)

            evict_mb = 0.0

            # 加入缺失层，容量不够则按 LRU 淘汰
            for lid in missing:
                sz = layer_size(lid, layer_sizes)
                if sz <= 0:
                    continue

                # 单层比 cache 还大，则无法长期缓存，但仍然算 pull，不加入 cache
                if cap <= 0 or sz > cap:
                    continue

                while cache_size + sz > cap and cache:
                    old_lid, old_sz = cache.popitem(last=False)
                    cache_size -= old_sz
                    evict_mb += old_sz

                if cache_size + sz <= cap:
                    cache[lid] = sz
                    cache_size += sz

            # 访问过的已有层刷新 LRU
            for lid in layers:
                if lid in cache:
                    sz = cache.pop(lid)
                    cache[lid] = sz

            total_reuse += reuse_mb
            total_future += future_mb
            total_pull += pull_mb
            total_evict += evict_mb
            n_steps += 1

    if n_steps == 0:
        return {
            "Reuse_raw": 0.0,
            "Future_raw": 0.0,
            "Pull_raw": 0.0,
            "Evict_raw": 0.0,
        }

    return {
        "Reuse_raw": total_reuse / n_steps,
        "Future_raw": total_future / n_steps,
        "Pull_raw": total_pull / n_steps,
        "Evict_raw": total_evict / n_steps,
    }

rows = []

for p in sorted(RES_DIR.glob("*.json")):
    m = re.search(r"cache(\d+)_req(\d+)", p.name)
    if not m:
        continue

    cache = int(m.group(1))
    req = int(m.group(2))

    if cache not in CACHES:
        continue

    case_path = CASE_DIR / f"case_cache{cache}_req{req}.json"
    if not case_path.exists():
        print("[MISSING CASE]", case_path)
        continue

    result = load_json(p)
    case = load_json(case_path)
    s = result.get("summary", {})

    terms = replay_dynamic_terms(case, result)

    rows.append({
        "cache": cache,
        "req": req,
        "method": method_name(p.name, s),
        **terms,
    })

def avg(xs):
    return sum(xs) / len(xs) if xs else 0.0

rows = [r for r in rows if r.get("method") != "FG-orig"]

# 先按 cache 和 method 对 9 个 requests 求平均
agg = []
for cache in CACHES:
    for method in METHODS:
        sub = [r for r in rows if r["cache"] == cache and r["method"] == method]
        if not sub:
            continue
        agg.append({
            "cache": cache,
            "method": method,
            "Reuse_raw": avg([x["Reuse_raw"] for x in sub]),
            "Future_raw": avg([x["Future_raw"] for x in sub]),
            "Pull_raw": avg([x["Pull_raw"] for x in sub]),
            "Evict_raw": avg([x["Evict_raw"] for x in sub]),
            "n": len(sub),
        })

agg = [x for x in agg if x.get("method") != "FG-orig"]

# per-cache min-max normalization
for cache in CACHES:
    sub = [x for x in agg if x["cache"] == cache]
    if not sub:
        continue

    def norm_good(key, out_key):
        vals = [x[key] for x in sub]
        lo, hi = min(vals), max(vals)
        for x in sub:
            x[out_key] = 0.0 if abs(hi - lo) < 1e-12 else (x[key] - lo) / (hi - lo)

    def norm_cost_saving(key, out_key):
        vals = [x[key] for x in sub]
        lo, hi = min(vals), max(vals)
        for x in sub:
            x[out_key] = 0.0 if abs(hi - lo) < 1e-12 else (hi - x[key]) / (hi - lo)

    norm_good("Reuse_raw", "Reuse_norm")
    norm_good("Future_raw", "Future_norm")
    norm_cost_saving("Pull_raw", "Pull_saving_norm")
    norm_cost_saving("Evict_raw", "Evict_saving_norm")

    for x in sub:
        x["Dynamic_Gain"] = (
            x["Reuse_norm"]
            + x["Future_norm"]
            + x["Pull_saving_norm"]
            + x["Evict_saving_norm"]
        ) / 4.0

def write(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("written", path)

def write_matrix(key, title, filename, fmt="{:.6f}"):
    lines = [f"## {title}", ""]
    lines.append("| cache_mb | " + " | ".join(METHODS) + " |")
    lines.append("|---:|" + "|".join(["---:"] * len(METHODS)) + "|")

    for cache in CACHES:
        vals = []
        for method in METHODS:
            hit = [x for x in agg if x["cache"] == cache and x["method"] == method]
            vals.append("" if not hit else fmt.format(hit[0][key]))
        lines.append(f"| {cache} | " + " | ".join(vals) + " |")

    write(OUT / filename, lines)

# 论文画图建议用这些归一化矩阵
write_matrix("Dynamic_Gain", "Fig.2 Dynamic Gain Function", "fig2_dynamic_gain_matrix.md")
write_matrix("Reuse_norm", "Fig.2 Reuse_t(u), normalized", "fig2_reuse_t_norm_matrix.md")
write_matrix("Future_norm", "Fig.2 Future_t(u), normalized", "fig2_future_t_norm_matrix.md")
write_matrix("Pull_saving_norm", "Fig.2 Pull_t(u) saving, normalized", "fig2_pull_t_saving_norm_matrix.md")
write_matrix("Evict_saving_norm", "Fig.2 Evict_t(u) saving, normalized", "fig2_evict_t_saving_norm_matrix.md")

# 原始值也保留，用于解释
write_matrix("Reuse_raw", "Fig.2 Reuse_t(u), raw MB/step", "fig2_reuse_t_raw_matrix.md", "{:.3f}")
write_matrix("Future_raw", "Fig.2 Future_t(u), raw MB/step", "fig2_future_t_raw_matrix.md", "{:.3f}")
write_matrix("Pull_raw", "Fig.2 Pull_t(u), raw MB/step", "fig2_pull_t_raw_matrix.md", "{:.3f}")
write_matrix("Evict_raw", "Fig.2 Evict_t(u), raw MB/step", "fig2_evict_t_raw_matrix.md", "{:.3f}")

# CSV 明细，后续画图方便
csv = OUT / "fig2_dynamic_gain_summary.csv"
with open(csv, "w", encoding="utf-8") as f:
    keys = [
        "cache", "method", "Dynamic_Gain",
        "Reuse_norm", "Future_norm", "Pull_saving_norm", "Evict_saving_norm",
        "Reuse_raw", "Future_raw", "Pull_raw", "Evict_raw", "n"
    ]
    f.write(",".join(keys) + "\n")
    for x in sorted(agg, key=lambda z: (z["cache"], METHODS.index(z["method"]))):
        f.write(",".join(str(x.get(k, "")) for k in keys) + "\n")
print("written", csv)

# 总表
all_files = [
    "fig2_dynamic_gain_matrix.md",
    "fig2_reuse_t_norm_matrix.md",
    "fig2_future_t_norm_matrix.md",
    "fig2_pull_t_saving_norm_matrix.md",
    "fig2_evict_t_saving_norm_matrix.md",
    "fig2_reuse_t_raw_matrix.md",
    "fig2_future_t_raw_matrix.md",
    "fig2_pull_t_raw_matrix.md",
    "fig2_evict_t_raw_matrix.md",
]
combined = []
for fn in all_files:
    p = OUT / fn
    if p.exists():
        combined.append(p.read_text(encoding="utf-8"))

write(OUT / "fig2_dynamic_gain_all_tables.md", ["\n\n".join(combined)])

print("[DONE] Fig.2 Dynamic Gain summary")
