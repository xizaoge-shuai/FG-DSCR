#!/usr/bin/env python3
"""FALCONS real-K8s functional placement/order adapter (NOT final layer benchmark).

Runs SAME small Pod workload under:
- default: no spec.nodeName; kube-scheduler chooses nodes, concurrent submits
- fgp_fcfs: FGP placement, per-node input-order sequential submit
- falcons: FGP placement, per-node DCO-ordered sequential submit

The scheduler's layer IDs/sizes are explicit PLACEHOLDER workload metadata,
not verified OCI layer digests. The observed containerd caching cannot be
attributed to PGDSF. Intended only for integration and timing smoke checks.
"""
import argparse,csv,datetime as dt,importlib.util,json,pathlib,statistics,subprocess,sys,time,uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

WORKERS=("falcons-worker1","falcons-worker2","falcons-worker3")
IMAGES=("nginx:1.27-alpine","busybox:1.36.1","alpine:3.20")
LABEL="comnet-falcons-integration"

def kc(*args,stdin=None,timeout=60):
    p=subprocess.run(["kubectl","--request-timeout=30s",*args],input=stdin,
        text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
    if p.returncode:raise RuntimeError("kubectl "+" ".join(args)+" : "+p.stderr.strip())
    return p.stdout

def quantity(v,kind):
    s=str(v)
    if kind=="cpu":return float(s[:-1])/1000 if s.endswith("m") else float(s)
    for suf,scale in (("Ki",2**10),("Mi",2**20),("Gi",2**30),("K",10**3),("M",10**6),("G",10**9)):
        if s.endswith(suf):return float(s[:-len(suf)])*scale/(2**30)
    return float(s)/(2**30)

def timeval(x):
    return dt.datetime.fromisoformat(x.replace("Z","+00:00")) if x else None

def delta(a,b):
    if a and b:return round((timeval(b)-timeval(a)).total_seconds(),4)
    return None

def percentile(xs,p):
    xs=sorted(xs)
    if not xs:return None
    i=(len(xs)-1)*p; lo=int(i);hi=min(lo+1,len(xs)-1)
    return round(xs[lo]*(hi-i)+xs[hi]*(i-lo),4) if lo!=hi else round(xs[lo],4)

def build_case(nodes,count):
    # Proxy logical layers are strictly ALGORITHM-INPUT DEMONSTRATORS:
    # these names/MB sizes do NOT claim to be OCI digests or measured downloads.
    shared={"common":3,"nginx":12,"busy":2,"alpine":3}
    containers=[]
    for i in range(count):
        image=IMAGES[i%len(IMAGES)]
        parts={0:["common","nginx"],1:["busy"],2:["common","alpine"]}[i%3]
        containers.append({"cid":f"r{i:03d}","layers":parts,
            "resources":{"cpu":.15,"mem":.12,"disk":.12},
            "run_time":8.0,"service_type":image})
    nodes_in=[]
    for n in nodes:
        alloc=n["status"]["allocatable"]
        nodes_in.append({"eid":n["metadata"]["name"],
            "resources":{"cpu":quantity(alloc["cpu"],"cpu")*.8,
                         "mem":quantity(alloc["memory"],"mem")*.8,
                         "disk":max(1.,quantity(alloc.get("ephemeral-storage","10Gi"),"mem")*.8)},
            "repo_capacity_mb":1024,"bandwidth_mb_s":10.,
            "initial_cache":[]})
    return {"containers":containers,"nodes":nodes_in,"layer_sizes_mb":shared}

def scheduler_result(root,case):
    path=root/"scripts/fg_dscr_incremental.py"
    spec=importlib.util.spec_from_file_location("k8s_falcons_original",str(path))
    mod=importlib.util.module_from_spec(spec);sys.modules[spec.name]=mod;spec.loader.exec_module(mod)
    cs,ns,ls=mod.load_case(str(root/"case_for_algorithm.json"))
    s=mod.FGDscrScheduler(layer_sizes_mb=ls,beam_width=1,k_pin=6,
      hard_resource_filter=True,max_best_response_rounds=5,
      phase1_neighbor_mode="move",move_topk_per_node=6,
      init_order="resource_asc",greedy_load_factor=.8,
      lambda_cong=1.,lambda_frag=10.,lambda_aff=0.,lambda_task_load=.03,
      lambda_fail=1000.,cache_policy="pgdsf",order_policy="dynamic_state",
      alpha1_reuse=1.,alpha2_future=.15,alpha3_pull=50.,alpha4_evict=.02,
      beta_slot=2.,rho_centrality=.2,kappa_overshoot=.5)
    s.set_data(cs,ns);start=time.perf_counter();result=s.run()
    return result,round((time.perf_counter()-start)*1000,3)

def pod_obj(ns,name,node,image,timeout):
    spec={"restartPolicy":"Never","activeDeadlineSeconds":timeout,
        "terminationGracePeriodSeconds":1,
        "containers":[{"name":"app","image":image,"imagePullPolicy":"IfNotPresent",
          "command":["/bin/sh","-c"],
          "args":["sleep 8; echo DONE"],
          "resources":{"requests":{"cpu":"150m","memory":"120Mi","ephemeral-storage":"120Mi"},
             "limits":{"cpu":"500m","memory":"240Mi","ephemeral-storage":"350Mi"}}}]}
    if node:spec["nodeName"]=node
    return {"apiVersion":"v1","kind":"Pod",
      "metadata":{"name":name,"namespace":ns,"labels":{"app.kubernetes.io/part-of":LABEL}},
      "spec":spec}

def create_pod(obj):
    t=time.perf_counter()
    kc("create","-f","-",stdin=json.dumps(obj))
    return round((time.perf_counter()-t)*1000,3)

def wait_pod(ns,name,seconds):
    until=time.monotonic()+seconds
    while time.monotonic()<until:
        p=json.loads(kc("-n",ns,"get","pod",name,"-o","json"))
        if p.get("status",{}).get("phase") in ("Succeeded","Failed"):return p
        time.sleep(1)
    return json.loads(kc("-n",ns,"get","pod",name,"-o","json"))

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--mode",choices=["default","fgp_fcfs","falcons"],required=True)
    ap.add_argument("--count",type=int,default=9)
    ap.add_argument("--timeout",type=int,default=240)
    ap.add_argument("--out-dir",default="results/comnet_revision/k8s_real_testbed/integration")
    ap.add_argument("--repo",default=None)
    a=ap.parse_args()
    if a.count not in (3,6,9,12,20,40):ap.error("choose count 3, 6, 9, 12, 20 or 40")
    if a.timeout<90:ap.error("timeout must be >=90")
    repo=pathlib.Path(a.repo).resolve() if a.repo else pathlib.Path(__file__).resolve().parents[2]
    out=pathlib.Path(a.out_dir).resolve()/f"{a.mode}_n{a.count}_{uuid.uuid4().hex[:8]}"
    out.mkdir(parents=True,exist_ok=False)
    all_nodes=json.loads(kc("get","nodes","-o","json"))["items"]
    byname={n["metadata"]["name"]:n for n in all_nodes}
    for n in WORKERS:
        if n not in byname:raise SystemExit("Missing worker "+n)
        if not any(c["type"]=="Ready" and c["status"]=="True" for c in byname[n]["status"]["conditions"]):
            raise SystemExit("NotReady worker "+n)
    case=build_case([byname[n] for n in WORKERS],a.count)
    (out/"case_for_algorithm.json").write_text(json.dumps(case,indent=2))
    # scheduler_result expects case alongside workdir; run with own isolated case path via copy
    result=None; algorithm_ms=None
    if a.mode!="default":
        spec=importlib.util.spec_from_file_location("k8s_falcons_incremental",str(repo/"scripts/fg_dscr_incremental.py"))
        mod=importlib.util.module_from_spec(spec);sys.modules[spec.name]=mod;spec.loader.exec_module(mod)
        cs,ns,ls=mod.load_case(str(out/"case_for_algorithm.json"))
        s=mod.FGDscrScheduler(layer_sizes_mb=ls,beam_width=1,k_pin=6,
          hard_resource_filter=True,max_best_response_rounds=5,
          phase1_neighbor_mode="move",move_topk_per_node=6,
          init_order="resource_asc",greedy_load_factor=.8,
          lambda_cong=1.,lambda_frag=10.,lambda_aff=0.,lambda_task_load=.03,
          lambda_fail=1000.,cache_policy="pgdsf",order_policy="dynamic_state",
          alpha1_reuse=1.,alpha2_future=.15,alpha3_pull=50.,alpha4_evict=.02,
          beta_slot=2.,rho_centrality=.2,kappa_overshoot=.5)
        s.set_data(cs,ns);t0=time.perf_counter();result=s.run()
        algorithm_ms=round((time.perf_counter()-t0)*1000,3)
        (out/"scheduler_output.json").write_text(json.dumps(result,indent=2))
    ns="falcons-integ-"+uuid.uuid4().hex[:8]
    kc("create","namespace",ns)
    kc("label","namespace",ns,f"app.kubernetes.io/part-of={LABEL}")
    print("NAMESPACE:",ns,"MODE:",a.mode,flush=True)
    images={c["cid"]:c["service_type"] for c in case["containers"]}
    cids=[c["cid"] for c in case["containers"]]
    if a.mode=="default":
        node_queues={"native_scheduler":cids}
    elif a.mode=="falcons":
        node_queues={k:list(v) for k,v in result["ordered_queues"].items()}
    else:
        orders={x:i for i,x in enumerate(cids)}
        node_queues={k:sorted(v,key=lambda cid:orders[cid])
                     for k,v in result["assignment"].items()}
    node_queues={k:v for k,v in node_queues.items() if v}
    unknown=set(cids)-{cid for queue in node_queues.values() for cid in queue}
    if unknown:raise RuntimeError(f"Algorithm rejected requests: {unknown}. Not a comparable experiment.")
    (out/"planned_queues.json").write_text(json.dumps(node_queues,indent=2))
    api_ack={}
    entries={}
    def run_queue(node,queue):
        for cid in queue:
            name="falc-"+cid
            doc=pod_obj(ns,name,None if a.mode=="default" else node,images[cid],a.timeout)
            api_ack[cid]=create_pod(doc)
            if a.mode!="default":
                entries[cid]=wait_pod(ns,name,a.timeout)
    start=time.perf_counter()
    if a.mode=="default":
        # Native scheduler; launch together, then wait globally.
        for cid in cids:
            doc=pod_obj(ns,"falc-"+cid,None,images[cid],a.timeout)
            api_ack[cid]=create_pod(doc)
    else:
        with ThreadPoolExecutor(max_workers=len(node_queues)) as pool:
            futures=[pool.submit(run_queue,node,queue) for node,queue in node_queues.items()]
            for future in futures:future.result()
    until=time.monotonic()+a.timeout
    while True:
        raw=json.loads(kc("-n",ns,"get","pods","-o","json"))
        phases=[p["status"].get("phase","Pending") for p in raw["items"]]
        if len(phases)==a.count and all(x in ("Succeeded","Failed") for x in phases):break
        if time.monotonic()>until:break
        time.sleep(2)
    elapsed=round(time.perf_counter()-start,3)
    raw=json.loads(kc("-n",ns,"get","pods","-o","json"))
    events=json.loads(kc("-n",ns,"get","events","-o","json"))
    (out/"pods_raw.json").write_text(json.dumps(raw,indent=2))
    (out/"events_raw.json").write_text(json.dumps(events,indent=2))
    rows=[]
    for p in raw["items"]:
        m=p["metadata"];st=p["status"];cid=m["name"].replace("falc-","")
        cst=(st.get("containerStatuses") or [{}])[0]
        state=cst.get("state",{})
        term=state.get("terminated",{})
        when=term.get("startedAt") or state.get("running",{}).get("startedAt")
        fin=term.get("finishedAt")
        rows.append({
            "mode":a.mode,"cid":cid,"node":p["spec"].get("nodeName",""),
            "phase":st.get("phase",""),"image":images[cid],
            "created_utc":m.get("creationTimestamp",""),
            "started_utc":when or "","finished_utc":fin or "",
            "create_to_start_s":delta(m.get("creationTimestamp"),when),
            "create_to_finish_s":delta(m.get("creationTimestamp"),fin),
            "image_id":cst.get("imageID",""),
            "exit_code":term.get("exitCode",""),
            "api_ack_ms":api_ack.get(cid),
        })
    if rows:
        with (out/"measurements.csv").open("w",newline="") as f:
            writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    starts=[r["create_to_start_s"] for r in rows if r["create_to_start_s"] is not None]
    finishes=[r["create_to_finish_s"] for r in rows if r["create_to_finish_s"] is not None]
    summary={
        "mode":a.mode,"namespace":ns,"count":a.count,"completed":len(rows),
        "succeeded":sum(r["phase"]=="Succeeded" and r["exit_code"]==0 for r in rows),
        "algorithm_wall_ms":algorithm_ms,
        "api_ack_mean_ms":round(statistics.mean(api_ack.values()),3) if api_ack else None,
        "submission_to_all_terminal_wall_s":elapsed,
        "mean_create_to_start_s":round(statistics.mean(starts),4) if starts else None,
        "p95_create_to_start_s":percentile(starts,.95),
        "mean_create_to_finish_s":round(statistics.mean(finishes),4) if finishes else None,
        "p95_create_to_finish_s":percentile(finishes,.95),
        "limits":[
          "INTEGRATION ONLY: imaginary layer-size metadata is NOT OCI layer digest/bytes",
          "containerd cache not controlled, PGDSF not deployed at node level",
          "FALCONS / FGP-only use nodeName; native default uses kube-scheduler",
          "native baseline uses concurrent submission, FGP/FALCONS serialize each worker queue",
          "This comparison cannot establish performance superiority until admission and concurrency are matched",
          "Algorithm placement weights follow historical Fig.8 archival run, not paper-declared weights.",
          "CPU/memory resources derived from node allocatable with conservative mapping.",
          "No cloud spillover simulation; all requests must be placeable."
        ]
    }
    (out/"summary.json").write_text(json.dumps(summary,indent=2,ensure_ascii=False))
    print(json.dumps(summary,indent=2,ensure_ascii=False))
    print("Results:",out,"\nNamespace kept for inspection:",ns,flush=True)
    if summary["succeeded"]!=a.count:
        print("WARNING: one or more Pods failed or did not finish.",file=sys.stderr)
        return 3
    return 0

if __name__=="__main__":sys.exit(main())
