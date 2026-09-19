import os
import re
import json
import glob
from collections import defaultdict, Counter

CACHE = [0,128,256,384,512,640,768,896,1024]
REQS = [200,300,400,500,600,700,800,900,1000]

# Dynamic Gain 系数；如果论文公式里不是 1，可以在这里改
A1_REUSE  = 1.0
A2_FUTURE = 1.0
A3_PULL   = 1.0
A4_EVICT  = 1.0

CASE_ROOT = "cases/drtp_cache_only_sweep_88"

METHOD_DIRS = {
    "LRScheduler": "results/drtp/final_exp/cacheonly_dg_0_1024_lrscheduler",
    "GAHRL":       "results/drtp/final_exp/cacheonly_dg_0_1024_gahrl",
    "ORR":         "results/drtp/final_exp/cacheonly_dg_0_1024_orr",
    "LASA":        "results/drtp/final_exp/cacheonly_dg_0_1024_lasa",
    "ILR-SA":      "results/drtp/final_exp/cacheonly_dg_0_1024_ilrsa",
    "FG-DSCR-GC":  "results/drtp/final_exp/cacheonly_dg_0_1024_fg",
}

OUTDIR = "results/drtp/final_exp/final_plot_data/fig_cacheonly_dynamic_gain_components"
os.makedirs(OUTDIR, exist_ok=True)

def read_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def parse_cache_req(path):
    b = os.path.basename(path)
    m = re.search(r"drtp_img88_cacheonly_(\d+)mb_(\d+)\.json$", b)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))

def get_case_path(cache, req):
    return f"{CASE_ROOT}/drtp_img88_cacheonly_{cache}mb_{req}.json"

def normalize_layer_sizes(case):
    for k in ["layer_sizes_mb", "layer_sizes", "layers_size_mb"]:
        if isinstance(case.get(k), dict):
            return case[k]
    if isinstance(case.get("meta"), dict):
        for k in ["layer_sizes_mb", "layer_sizes", "layers_size_mb"]:
            if isinstance(case["meta"].get(k), dict):
                return case["meta"][k]
    raise KeyError("cannot find layer_sizes_mb/layer_sizes")

def size_getter(layer_sizes):
    def f(l):
        if l in layer_sizes:
            return float(layer_sizes[l])
        sl = str(l)
        if sl in layer_sizes:
            return float(layer_sizes[sl])
        return 0.0
    return f

def normalize_nodes(case):
    mp = {}
    for i, n in enumerate(case.get("nodes", [])):
        eid = n.get("eid", n.get("id", f"edge-{i+1}"))
        mp[str(eid)] = n
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
        eid = str(eid)
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

# 聚合：每个 case 先除以 container 数，再对 request size 平均
agg = defaultdict(lambda: defaultdict(float))
case_count = defaultdict(int)

audit = defaultdict(int)
missing_case = []
bad_result = []
bad_replay = []

for method, root in METHOD_DIRS.items():
    for p in glob.glob(root + "/*.json"):
        parsed = parse_cache_req(p)
        if parsed is None:
            continue
        cache, req = parsed
        if cache not in CACHE or req not in REQS:
            continue

        case_path = get_case_path(cache, req)
        if not os.path.exists(case_path):
            missing_case.append(case_path)
            continue

        try:
            result = read_json(p)
            case = read_json(case_path)
        except Exception as e:
            bad_result.append((p, str(e)))
            continue

        try:
            totals = replay_components(case, result)
        except Exception as e:
            bad_replay.append((p, str(e)))
            continue

        n = max(totals["n_steps"], 1)
        for metric in ["dynamic_gain", "reuse_t", "future_t", "pull_t", "pull_time_t", "evict_t"]:
            agg[(method, cache)][metric] += totals[metric] / n

        case_count[(method, cache)] += 1
        audit[(method, cache)] += 1

final = defaultdict(dict)
for method in METHOD_DIRS:
    for cache in CACHE:
        c = case_count[(method, cache)]
        if c <= 0:
            continue
        for metric in ["dynamic_gain", "reuse_t", "future_t", "pull_t", "pull_time_t", "evict_t"]:
            final[metric][(method, cache)] = agg[(method, cache)][metric] / c

print("===== audit cases =====")
print("| method | " + " | ".join(map(str, CACHE)) + " |")
print("|---|" + "|".join(["---:"]*len(CACHE)) + "|")
for method in METHOD_DIRS:
    vals = [str(audit[(method, c)]) for c in CACHE]
    print("| " + method + " | " + " | ".join(vals) + " |")

print()
print("missing_case =", len(set(missing_case)))
print("bad_result =", len(bad_result))
print("bad_replay =", len(bad_replay))
if bad_replay[:5]:
    print("bad_replay examples:")
    for x in bad_replay[:5]:
        print(x)

def write_metric(metric, title, fmt="{:.6f}"):
    path = os.path.join(OUTDIR, f"cacheonly_{metric}_lines.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# cache-only {title}\n\n")
        f.write("| method | " + " | ".join(map(str, CACHE)) + " |\n")
        f.write("|---|" + "|".join(["---:"]*len(CACHE)) + "|\n")
        for method in METHOD_DIRS:
            vals = []
            for cache in CACHE:
                v = final[metric].get((method, cache))
                vals.append("MISSING" if v is None else fmt.format(v))
            f.write("| " + method + " | " + " | ".join(vals) + " |\n")
    print("[OK]", path)

write_metric("dynamic_gain", "Dynamic Gain Function")
write_metric("reuse_t", "Reuse_t(u)")
write_metric("future_t", "Future_t(u)")
write_metric("pull_t", "Pull_t(u) MB")
write_metric("pull_time_t", "PullTime_t(u) seconds")
write_metric("evict_t", "Evict_t(u) MB")

long_path = os.path.join(OUTDIR, "cacheonly_dynamic_gain_components_long.md")
with open(long_path, "w", encoding="utf-8") as f:
    f.write("| method | cache | cases | dynamic_gain | reuse_t | future_t | pull_t_mb | pull_time_t_s | evict_t_mb |\n")
    f.write("|---|---:|---:|---:|---:|---:|---:|---:|---:|\n")
    for method in METHOD_DIRS:
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

print("\n===== preview dynamic gain =====")
print(open(os.path.join(OUTDIR, "cacheonly_dynamic_gain_lines.md"), encoding="utf-8").read())

print("\n===== preview reuse =====")
print(open(os.path.join(OUTDIR, "cacheonly_reuse_t_lines.md"), encoding="utf-8").read())

print("\n===== preview pull time =====")
print(open(os.path.join(OUTDIR, "cacheonly_pull_time_t_lines.md"), encoding="utf-8").read())
