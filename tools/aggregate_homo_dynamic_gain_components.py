import os
import re
import json
import glob
from collections import defaultdict, Counter

# ============================================================
# X 轴：你说 8 种缓存大小，所以默认不含 0
# 如果想把 0 也画进去，改成 [0,128,256,384,512,640,768,896,1024]
# ============================================================
CACHE = [128,256,384,512,640,768,896,1024]
REQS = [200,300,400,500,600,700,800,900,1000]

# Dynamic Gain 系数
# 如果论文里 alpha 不是 1，就在这里改
A1_REUSE  = 1.0
A2_FUTURE = 1.0
A3_PULL   = 1.0
A4_EVICT  = 1.0

RESULT_ROOT = "results/drtp/final_exp"
CASE_ROOT = "cases"

OUTDIR = "results/drtp/final_exp/final_plot_data/fig_homo_dynamic_gain_components"
os.makedirs(OUTDIR, exist_ok=True)

METHOD_ORDER = [
    "LRScheduler",
    "GAHRL",
    "ORR",
    "LASA",
    "ILR-SA",
    "FG-DSCR-GC",
]

def read_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def get_summary(obj):
    return obj.get("summary", obj)

def norm_method_from_algo(algo, path):
    algo = str(algo or "")
    low = (algo + " " + path).lower()

    if "lrscheduler" in low:
        return "LRScheduler"
    if "gahrl" in low:
        return "GAHRL"
    if "orr" in low:
        return "ORR"
    if "lasa" in low:
        return "LASA"
    if "ilr" in low:
        return "ILR-SA"
    if "fg-dscr" in low or "fg_" in low or "fg/" in low:
        return "FG-DSCR-GC"

    return None

def parse_homo_key_from_name(name):
    """
    兼容几类命名：
    1) drtp_img88_homo_bad_cache128mb_200.json
    2) fg_homo_bad_cache128_200.json
    3) drtp_img88_cache_128mb_200.json  # 普通同构 cache sweep，无 bad/good
    """
    b = os.path.basename(name)

    # 去掉常见前缀
    if b.startswith("ilrsa_"):
        b = b[len("ilrsa_"):]

    # drtp_img88_homo_bad_cache128mb_200.json
    m = re.search(r"(?:drtp_img88_)?homo_(bad|good)_cache(\d+)mb?_(\d+)\.json$", b)
    if m:
        return (m.group(1), int(m.group(2)), int(m.group(3)))

    # fg_homo_bad_cache128_200.json
    m = re.search(r"fg_homo_(bad|good)_cache(\d+)_(\d+)\.json$", b)
    if m:
        return (m.group(1), int(m.group(2)), int(m.group(3)))

    # drtp_img88_cache_128mb_200.json
    # 注意排除 cachehetero / hetero
    if "hetero" not in b and "cachehetero" not in b:
        m = re.search(r"drtp_img88_cache_(\d+)mb_(\d+)\.json$", b)
        if m:
            return ("all", int(m.group(1)), int(m.group(2)))

    return None

def normalize_layer_sizes(case):
    for k in ["layer_sizes_mb", "layer_sizes", "layers_size_mb"]:
        if k in case and isinstance(case[k], dict):
            return case[k]
    if "meta" in case and isinstance(case["meta"], dict):
        for k in ["layer_sizes_mb", "layer_sizes", "layers_size_mb"]:
            if k in case["meta"] and isinstance(case["meta"][k], dict):
                return case["meta"][k]
    raise KeyError("cannot find layer_sizes_mb/layer_sizes in case json")

def size_getter(layer_sizes):
    def size_of(l):
        if l in layer_sizes:
            return float(layer_sizes[l])
        sl = str(l)
        if sl in layer_sizes:
            return float(layer_sizes[sl])
        return 0.0
    return size_of

def normalize_nodes(case):
    mp = {}
    for i, n in enumerate(case.get("nodes", [])):
        eid = n.get("eid", n.get("id", f"edge-{i+1}"))
        mp[eid] = n
    return mp

def normalize_containers(case):
    mp = {}
    for i, c in enumerate(case.get("containers", [])):
        cid = str(c.get("cid", c.get("id", c.get("container_id", f"c{i}"))))
        layers = c.get("layers", c.get("layer_ids", []))
        mp[cid] = set(map(str, layers))
    return mp

def cid_of(x):
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        return str(x.get("cid", x.get("id", x.get("container_id", ""))))
    return str(x)

def cache_size(cache, size_of):
    return sum(size_of(l) for l in cache)

def evict_lru(cache, last_used, cap, size_of):
    """
    统一 replay 口径：用 LRU 计算 Evict_t。
    这里不是重新运行算法，只是用各算法已有 ordered_queues 做统一指标统计。
    """
    evicted = 0.0

    if cap <= 0:
        evicted = cache_size(cache, size_of)
        cache.clear()
        last_used.clear()
        return evicted

    while cache_size(cache, size_of) > cap + 1e-9:
        if not cache:
            break
        victim = min(cache, key=lambda l: (last_used.get(l, -1), -size_of(l), l))
        evicted += size_of(victim)
        cache.remove(victim)
        last_used.pop(victim, None)

    return evicted

def build_case_index():
    """
    建立 case 索引：
    key = (quality, cache, requests)
    quality 可以是 bad/good/all
    """
    idx = {}

    for p in glob.glob(os.path.join(CASE_ROOT, "**", "*.json"), recursive=True):
        key = parse_homo_key_from_name(p)
        if key is None:
            continue
        qual, cache, req = key
        if cache not in CACHE or req not in REQS:
            continue

        # 优先保留更短路径/更明确的 homo case
        old = idx.get(key)
        if old is None:
            idx[key] = p
        else:
            if "homo_" in os.path.basename(p) and "homo_" not in os.path.basename(old):
                idx[key] = p

    return idx

def find_case(case_idx, result_key):
    qual, cache, req = result_key

    # exact
    if result_key in case_idx:
        return case_idx[result_key]

    # result 是 bad/good，但 case 是普通 all
    if (qual, cache, req) not in case_idx:
        k = ("all", cache, req)
        if k in case_idx:
            return case_idx[k]

    # result 是 all，但只有 bad/good case：这种情况不够严谨，优先 bad
    if qual == "all":
        for q in ["bad", "good"]:
            k = (q, cache, req)
            if k in case_idx:
                return case_idx[k]

    return None

def replay_components(case, result):
    layer_sizes = normalize_layer_sizes(case)
    size_of = size_getter(layer_sizes)
    nodes = normalize_nodes(case)
    containers = normalize_containers(case)

    ordered = result.get("ordered_queues") or result.get("assignment") or {}

    totals = {
        "dynamic_gain": 0.0,
        "reuse_t": 0.0,
        "future_t": 0.0,
        "pull_t": 0.0,
        "pull_time_t": 0.0,
        "evict_t": 0.0,
        "n_steps": 0,
    }

    for eid, queue in ordered.items():
        if eid not in nodes:
            continue

        node = nodes[eid]
        cap = float(node.get("repo_capacity_mb", node.get("cache_capacity_mb", 0)))
        bw = max(float(node.get("bandwidth_mb_s", 1.0)), 1e-9)

        q = [cid_of(x) for x in queue]
        q = [x for x in q if x in containers]

        future_cnt = Counter()
        for cid in q:
            for l in containers[cid]:
                future_cnt[l] += 1

        cache = set(map(str, node.get("initial_cache", [])))
        last_used = {}
        t = 0

        for cid in q:
            layers = containers[cid]

            # 当前容器之后的 future
            for l in layers:
                future_cnt[l] -= 1
                if future_cnt[l] <= 0:
                    future_cnt.pop(l, None)

            hit_layers = layers & cache
            miss_layers = layers - cache

            reuse_mb = sum(size_of(l) for l in hit_layers)
            pull_mb = sum(size_of(l) for l in miss_layers)
            pull_time = pull_mb / bw

            future_val = 0.0
            for l in layers:
                future_val += size_of(l) * float(future_cnt.get(l, 0))

            for l in layers:
                cache.add(l)
                last_used[l] = t

            evict_mb = evict_lru(cache, last_used, cap, size_of)

            gain = (
                A1_REUSE  * reuse_mb
                + A2_FUTURE * future_val
                - A3_PULL   * pull_mb
                - A4_EVICT  * evict_mb
            )

            totals["dynamic_gain"] += gain
            totals["reuse_t"] += reuse_mb
            totals["future_t"] += future_val
            totals["pull_t"] += pull_mb
            totals["pull_time_t"] += pull_time
            totals["evict_t"] += evict_mb
            totals["n_steps"] += 1

            t += 1

    return totals

# ============================================================
# 1. 建立 case index
# ============================================================

case_idx = build_case_index()

print("===== case index audit =====")
ccnt = defaultdict(int)
for (qual, cache, req), p in case_idx.items():
    ccnt[(qual, cache)] += 1

print("| quality | cache | cases |")
print("|---|---:|---:|")
for qual in ["all", "bad", "good"]:
    for cache in CACHE:
        print(f"| {qual} | {cache} | {ccnt[(qual, cache)]} |")

# ============================================================
# 2. 收集 result json
# ============================================================

picked = {}
skipped_no_key = 0
skipped_no_method = 0

for p in glob.glob(os.path.join(RESULT_ROOT, "**", "*.json"), recursive=True):
    key = parse_homo_key_from_name(p)
    if key is None:
        skipped_no_key += 1
        continue

    qual, cache, req = key
    if cache not in CACHE or req not in REQS:
        continue

    try:
        obj = read_json(p)
        summary = get_summary(obj)
        method = norm_method_from_algo(summary.get("algo", ""), p)
    except Exception:
        continue

    if method not in METHOD_ORDER:
        skipped_no_method += 1
        continue

    pick_key = (method, qual, cache, req)

    old = picked.get(pick_key)
    if old is None or os.path.getmtime(p) > os.path.getmtime(old):
        picked[pick_key] = p

print("\n===== picked result audit =====")
cnt = defaultdict(int)
for (method, qual, cache, req), p in picked.items():
    cnt[(method, cache)] += 1

print("| method | cache | cases |")
print("|---|---:|---:|")
for method in METHOD_ORDER:
    for cache in CACHE:
        print(f"| {method} | {cache} | {cnt[(method, cache)]} |")

print("\nskipped_no_key =", skipped_no_key)
print("skipped_no_method =", skipped_no_method)

# ============================================================
# 3. replay
# ============================================================

agg = defaultdict(lambda: defaultdict(float))
case_count = defaultdict(int)

warn_missing_case = 0
warn_replay_failed = 0

for (method, qual, cache, req), result_path in sorted(picked.items()):
    result_key = (qual, cache, req)
    case_path = find_case(case_idx, result_key)

    if not case_path:
        warn_missing_case += 1
        if warn_missing_case <= 20:
            print("[WARN] missing case for", result_path, "key=", result_key)
        continue

    try:
        case = read_json(case_path)
        result = read_json(result_path)
        totals = replay_components(case, result)
    except Exception as e:
        warn_replay_failed += 1
        if warn_replay_failed <= 20:
            print("[WARN] replay failed:", result_path, "case=", case_path, "ERR=", e)
        continue

    n = max(totals["n_steps"], 1)

    for metric in ["dynamic_gain", "reuse_t", "future_t", "pull_t", "pull_time_t", "evict_t"]:
        # 每个 case 先取 per-container 平均，避免 1000 请求 case 权重过大
        agg[(method, cache)][metric] += totals[metric] / n

    case_count[(method, cache)] += 1

print("\nwarn_missing_case =", warn_missing_case)
print("warn_replay_failed =", warn_replay_failed)

final = defaultdict(dict)
for method in METHOD_ORDER:
    for cache in CACHE:
        c = case_count[(method, cache)]
        if c <= 0:
            continue
        for metric in ["dynamic_gain", "reuse_t", "future_t", "pull_t", "pull_time_t", "evict_t"]:
            final[metric][(method, cache)] = agg[(method, cache)][metric] / c

# ============================================================
# 4. 写表
# ============================================================

def write_metric(metric, title, fmt="{:.6f}"):
    path = os.path.join(OUTDIR, f"homo_{metric}_lines.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# homo {title}\n\n")
        f.write("| method | " + " | ".join(map(str, CACHE)) + " |\n")
        f.write("|---|" + "|".join(["---:"] * len(CACHE)) + "|\n")

        for method in METHOD_ORDER:
            vals = []
            for cache in CACHE:
                v = final[metric].get((method, cache), None)
                vals.append("MISSING" if v is None else fmt.format(v))
            f.write("| " + method + " | " + " | ".join(vals) + " |\n")

    print("[OK]", path)

write_metric("dynamic_gain", "Dynamic Gain Function", "{:.6f}")
write_metric("reuse_t", "Reuse_t(u)", "{:.6f}")
write_metric("future_t", "Future_t(u)", "{:.6f}")
write_metric("pull_t", "Pull_t(u) MB", "{:.6f}")
write_metric("pull_time_t", "PullTime_t(u) seconds", "{:.6f}")
write_metric("evict_t", "Evict_t(u) MB", "{:.6f}")

long_path = os.path.join(OUTDIR, "homo_dynamic_gain_components_long.md")
with open(long_path, "w", encoding="utf-8") as f:
    f.write("| method | cache | cases | dynamic_gain | reuse_t | future_t | pull_t_mb | pull_time_t_s | evict_t_mb |\n")
    f.write("|---|---:|---:|---:|---:|---:|---:|---:|---:|\n")
    for method in METHOD_ORDER:
        for cache in CACHE:
            c = case_count[(method, cache)]
            if c <= 0:
                continue
            f.write("| {} | {} | {} | {:.6f} | {:.6f} | {:.6f} | {:.6f} | {:.6f} | {:.6f} |\n".format(
                method,
                cache,
                c,
                final["dynamic_gain"][(method, cache)],
                final["reuse_t"][(method, cache)],
                final["future_t"][(method, cache)],
                final["pull_t"][(method, cache)],
                final["pull_time_t"][(method, cache)],
                final["evict_t"][(method, cache)],
            ))

print("[OK]", long_path)

print("\n===== preview: Dynamic Gain =====")
print(open(os.path.join(OUTDIR, "homo_dynamic_gain_lines.md"), encoding="utf-8").read())

print("\n===== preview: Reuse_t =====")
print(open(os.path.join(OUTDIR, "homo_reuse_t_lines.md"), encoding="utf-8").read())

print("\n===== preview: PullTime_t =====")
print(open(os.path.join(OUTDIR, "homo_pull_time_t_lines.md"), encoding="utf-8").read())
