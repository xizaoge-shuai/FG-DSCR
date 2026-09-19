import os, re, json, glob
from pathlib import Path
from collections import defaultdict, Counter

CACHE=[0,128,256,384,512,640,768,896,1024]
REQS=[200,300,400,500,600,700,800,900,1000]

CASE_ROOT="cases/drtp_cache_only_sweep_88"
METHOD_DIRS={
    "LRScheduler": "results/drtp/final_exp/cacheonly_dg_0_1024_lrscheduler",
    "GAHRL": "results/drtp/final_exp/cacheonly_dg_0_1024_gahrl",
    "ORR": "results/drtp/final_exp/cacheonly_dg_0_1024_orr",
    "LASA": "results/drtp/final_exp/cacheonly_dg_0_1024_lasa",
    "ILR-SA": "results/drtp/final_exp/cacheonly_dg_0_1024_ilrsa",
    "FG-DSCR-GC": "results/drtp/final_exp/cacheonly_dg_0_1024_fg",
}
OUTDIR=Path("results/drtp/final_exp/final_plot_data/fig_cacheonly_dynamic_gain_components_capaware")
OUTDIR.mkdir(parents=True,exist_ok=True)

def read(p):
    return json.load(open(p,encoding="utf-8"))

def parse(p):
    m=re.search(r"cacheonly_(\d+)mb_(\d+)\.json$", os.path.basename(p))
    return (int(m.group(1)), int(m.group(2))) if m else None

def layer_sizes(case):
    for k in ["layer_sizes_mb","layer_sizes","layers_size_mb"]:
        if isinstance(case.get(k),dict):
            return case[k]
    if isinstance(case.get("meta"),dict):
        for k in ["layer_sizes_mb","layer_sizes","layers_size_mb"]:
            if isinstance(case["meta"].get(k),dict):
                return case["meta"][k]
    raise KeyError("no layer sizes")

def size_func(ls):
    def f(l):
        return float(ls.get(l, ls.get(str(l), 0.0)))
    return f

def nodes(case):
    out={}
    for i,n in enumerate(case.get("nodes",[])):
        eid=str(n.get("eid",n.get("id",f"edge-{i+1}")))
        out[eid]=n
    return out

def containers(case):
    out={}
    for i,c in enumerate(case.get("containers",[])):
        cid=str(c.get("cid",c.get("id",c.get("container_id",f"c{i}"))))
        out[cid]=set(map(str,c.get("layers",c.get("layer_ids",[]))))
    return out

def cid(x):
    if isinstance(x,dict):
        return str(x.get("cid",x.get("id",x.get("container_id",""))))
    return str(x)

def cache_size(cache, size_of):
    return sum(size_of(l) for l in cache)

def evict_lru(cache,last_used,cap,size_of):
    ev=0.0
    if cap <= 0:
        ev=cache_size(cache,size_of)
        cache.clear()
        last_used.clear()
        return ev
    while cache_size(cache,size_of) > cap + 1e-9:
        if not cache:
            break
        victim=min(cache,key=lambda l:(last_used.get(l,-1),-size_of(l),l))
        ev += size_of(victim)
        cache.remove(victim)
        last_used.pop(victim,None)
    return ev

def replay(case,result):
    ls=layer_sizes(case)
    size_of=size_func(ls)
    ns=nodes(case)
    cs=containers(case)
    ordered=result.get("ordered_queues") or result.get("assignment") or {}

    total=defaultdict(float)
    total["n_steps"]=0

    for eid,q in ordered.items():
        eid=str(eid)
        if eid not in ns:
            continue
        node=ns[eid]
        cap=float(node.get("repo_capacity_mb",node.get("cache_capacity_mb",0)))
        bw=max(float(node.get("bandwidth_mb_s",1.0)),1e-9)

        queue=[cid(x) for x in q]
        queue=[x for x in queue if x in cs]

        future_cnt=Counter()
        for c in queue:
            for l in cs[c]:
                future_cnt[l]+=1

        cache=set(map(str,node.get("initial_cache",[])))
        last_used={}
        t=0

        for c in queue:
            layers=cs[c]

            for l in layers:
                future_cnt[l]-=1
                if future_cnt[l] <= 0:
                    future_cnt.pop(l,None)

            hit=layers & cache
            miss=layers - cache

            reuse=sum(size_of(l) for l in hit)
            pull=sum(size_of(l) for l in miss)
            pull_time=pull/bw

            # 先加入当前容器 layer
            for l in layers:
                cache.add(l)
                last_used[l]=t

            # 再执行容量约束淘汰
            evict=evict_lru(cache,last_used,cap,size_of)

            # capacity-aware Future:
            # 只计算当前容器 layer 中，经过淘汰后仍实际保留在 cache 里的部分
            retained_current_layers = layers & cache
            future_cap = sum(size_of(l)*float(future_cnt.get(l,0)) for l in retained_current_layers)

            gain_cap = reuse + future_cap - pull - evict

            total["reuse_t"] += reuse
            total["future_cap_t"] += future_cap
            total["pull_t"] += pull
            total["pull_time_t"] += pull_time
            total["evict_t"] += evict
            total["dynamic_gain_cap"] += gain_cap
            total["n_steps"] += 1
            t += 1

    return total

agg=defaultdict(lambda: defaultdict(float))
cnt=defaultdict(int)

for method,root in METHOD_DIRS.items():
    for p in glob.glob(root+"/*.json"):
        x=parse(p)
        if not x:
            continue
        cache,req=x
        if cache not in CACHE or req not in REQS:
            continue
        cp=f"{CASE_ROOT}/drtp_img88_cacheonly_{cache}mb_{req}.json"
        if not os.path.exists(cp):
            continue
        case=read(cp)
        result=read(p)
        m=replay(case,result)
        n=max(m["n_steps"],1)
        for k in ["dynamic_gain_cap","reuse_t","future_cap_t","pull_t","pull_time_t","evict_t"]:
            agg[(method,cache)][k] += m[k]/n
        cnt[(method,cache)] += 1

final=defaultdict(dict)
for method in METHOD_DIRS:
    for cache in CACHE:
        c=cnt[(method,cache)]
        if c <= 0:
            continue
        for k in ["dynamic_gain_cap","reuse_t","future_cap_t","pull_t","pull_time_t","evict_t"]:
            final[k][(method,cache)] = agg[(method,cache)][k]/c

def write(metric,title):
    path=OUTDIR/f"cacheonly_{metric}_lines.md"
    with open(path,"w",encoding="utf-8") as f:
        f.write(f"# cache-only {title}\n\n")
        f.write("| cache_mb | " + " | ".join(METHOD_DIRS.keys()) + " |\n")
        f.write("|---:|" + "|".join(["---:"]*len(METHOD_DIRS)) + "|\n")
        for cache in CACHE:
            vals=[]
            for method in METHOD_DIRS:
                v=final[metric].get((method,cache))
                vals.append("MISSING" if v is None else f"{v:.6f}")
            f.write("| " + str(cache) + " | " + " | ".join(vals) + " |\n")
    print("[OK]",path)

write("dynamic_gain_cap","Capacity-aware Dynamic Gain")
write("reuse_t","Reuse_t(u)")
write("future_cap_t","Capacity-aware Future_t(u)")
write("pull_t","Pull_t(u) MB")
write("evict_t","Evict_t(u) MB")
write("pull_time_t","PullTime_t(u) seconds")

long=OUTDIR/"cacheonly_capacity_aware_components_long.md"
with open(long,"w",encoding="utf-8") as f:
    f.write("| cache_mb | method | cases | dynamic_gain_cap | reuse_t | future_cap_t | pull_t | evict_t | pull_time_t |\n")
    f.write("|---:|---|---:|---:|---:|---:|---:|---:|---:|\n")
    for cache in CACHE:
        for method in METHOD_DIRS:
            c=cnt[(method,cache)]
            if c <= 0:
                continue
            f.write("| {} | {} | {} | {:.6f} | {:.6f} | {:.6f} | {:.6f} | {:.6f} | {:.6f} |\n".format(
                cache,method,c,
                final["dynamic_gain_cap"][(method,cache)],
                final["reuse_t"][(method,cache)],
                final["future_cap_t"][(method,cache)],
                final["pull_t"][(method,cache)],
                final["evict_t"][(method,cache)],
                final["pull_time_t"][(method,cache)],
            ))
print("[OK]",long)

print("\n===== Capacity-aware Future =====")
print((OUTDIR/"cacheonly_future_cap_t_lines.md").read_text(encoding="utf-8"))
