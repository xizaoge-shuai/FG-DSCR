#!/usr/bin/env python3
"""Small real OCI FALCONS executor: use VERIFIED layers, not image placeholders.

Functional smoke trial with immutable platform image digest, genuine compressed
layer sizes, common global inflight cap. Does not claim PGDSF is deployed in
containerd or represent controlled cold cache. Records Pod/event evidence and
total request-to-finish time from common batch start (controller wall time).
"""
import argparse,csv,datetime as dt,importlib.util,json,pathlib,statistics,subprocess,sys,time,uuid
from collections import Counter

WORKERS=("falcons-worker1","falcons-worker2","falcons-worker3")
def kc(*args,stdin=None):
    p=subprocess.run(["kubectl","--request-timeout=30s",*args],input=stdin,
        text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=65)
    if p.returncode:raise RuntimeError("kubectl "+" ".join(args)+": "+p.stderr.strip())
    return p.stdout
def store(p,obj):p.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding="utf-8")
def stamp(s):
    return dt.datetime.fromisoformat(s.replace("Z","+00:00")) if s else None
def interval(x,y):
    return round((stamp(y)-stamp(x)).total_seconds(),4) if x and y else None
def qnum(s,k):
    s=str(s)
    if k=="cpu":return float(s[:-1])/1000 if s.endswith("m") else float(s)
    for suf,val in (("Ki",1024),("Mi",1024**2),("Gi",1024**3),("K",1000),("M",1000**2),("G",1000**3)):
        if s.endswith(suf):return float(s[:-len(suf)])*val/1024**3
    return float(s)/1024**3
def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--catalog",required=True)
    ap.add_argument("--mode",choices=["falcons","fgp_fcfs","default"],required=True)
    ap.add_argument("--count",type=int,default=9)
    ap.add_argument("--max-inflight",type=int,default=3)
    ap.add_argument("--profile",choices=["paper_declared","archived_fig8"],default="paper_declared")
    ap.add_argument("--cache-model",choices=["empty_assumed","warm_all_verified"],default="empty_assumed",
       help="After independent full image prewarm verification, warm_all_verified initializes all manifest layers on each node")
    ap.add_argument("--timeout",type=int,default=900)
    ap.add_argument("--out-root",default="results/comnet_revision/k8s_real_testbed/real_oci_smoke")
    a=ap.parse_args()
    if a.count not in (3,6,9,10,12,20,40):ap.error("count must be 3/6/9/10/12/20/40")
    if not 1<=a.max_inflight<=a.count:ap.error("bad inflight cap")
    if not 120<=a.timeout<=3600:ap.error("timeout must be 120..3600")
    catalog=json.loads(pathlib.Path(a.catalog).read_text(encoding="utf-8"))
    images=catalog["images"]
    if len(images)<3:raise ValueError("Expected three real OCI images")
    if catalog.get("errors"):raise ValueError("Catalog has errors")
    layers={}
    for img in images:
        ref=img["image_ref"]
        if "@sha256:" not in ref or "None" in ref:
            raise ValueError("UNVERIFIED or missing image digest: "+ref)
        for layer in img["layers"]:
            digest=layer["digest"];sz=int(layer["size_bytes"])
            if digest in layers and layers[digest]!=sz:raise ValueError("Layer digest size mismatch")
            layers[digest]=sz
    raw_nodes=json.loads(kc("get","nodes","-o","json"))["items"]
    nd={node["metadata"]["name"]:node for node in raw_nodes}
    nodes=[]
    for name in WORKERS:
        if name not in nd:raise RuntimeError("Worker missing "+name)
        x=nd[name];alloc=x["status"]["allocatable"]
        if not any(c["type"]=="Ready" and c["status"]=="True" for c in x["status"]["conditions"]):
            raise RuntimeError("Node not ready "+name)
        nodes.append({"eid":name,"resources":{
             "cpu":qnum(alloc["cpu"],"cpu")*.8,
             "mem":qnum(alloc["memory"],"mem")*.8,
             "disk":qnum(alloc["ephemeral-storage"],"mem")*.8},
             "repo_capacity_mb":1024,"bandwidth_mb_s":10.0,"initial_cache":[]})
    if a.cache_model=="warm_all_verified":
        # This is an ASSERTED test precondition, not an automatic containerd check.
        # The user must first prewarm all three immutable digests on ALL workers,
        # then check successful Pods/content store on each worker.
        for n in nodes:
            n["initial_cache"]=list(layers)
        print("[CACHE MODEL] all verified manifest layers assumed present on each Worker; "
              "run per-worker prewarm probe before using this option",flush=True)
    else:
        print("[CACHE MODEL] EMPTY ASSUMED; physical cache not verified",flush=True)
    containers=[]
    real_images={}
    for i in range(a.count):
        im=images[i%3];cid=f"r{i:03d}"
        real_images[cid]=im["image_ref"]
        containers.append({"cid":cid,"layers":[l["digest"] for l in im["layers"]],
             "resources":{"cpu":.15,"mem":.12,"disk":.12},
             "run_time":8.0,"service_type":im["source"]})
    case={"nodes":nodes,"containers":containers,
          "layer_sizes_mb":{d:v/1000000.0 for d,v in layers.items()}}
    rid="oci-"+uuid.uuid4().hex[:8]
    dest=pathlib.Path(a.out_root).resolve()/rid;dest.mkdir(parents=True)
    store(dest/"workload.json",case)
    store(dest/"layer_sizes_bytes.json",layers)
    repo=pathlib.Path(__file__).resolve().parents[2]
    result=None;wall=None;config=None
    if a.mode!="default":
        path=repo/"scripts/fg_dscr_incremental.py"
        spec=importlib.util.spec_from_file_location("falcons_real_oci",str(path))
        mo=importlib.util.module_from_spec(spec);sys.modules[spec.name]=mo;spec.loader.exec_module(mo)
        cs,ns,ls=mo.load_case(str(dest/"workload.json"))
        config=dict(layer_sizes_mb=ls,beam_width=1,k_pin=6,
            hard_resource_filter=True,lambda_fail=1000.0,
            max_best_response_rounds=5,phase1_neighbor_mode="move",
            move_topk_per_node=6,cache_policy="pgdsf",order_policy="dynamic_state",
            alpha1_reuse=1.,alpha2_future=.15,alpha3_pull=50.,alpha4_evict=.02,
            beta_slot=2.,rho_centrality=.2,kappa_overshoot=.5)
        if a.profile=="paper_declared":
            config.update(lambda_cong=1.,lambda_frag=.1,lambda_aff=.2,
                lambda_task_load=.2,init_order="image_resource",greedy_load_factor=0.)
        else:
            config.update(lambda_cong=1.,lambda_frag=10.,lambda_aff=0.,
                lambda_task_load=.03,init_order="resource_asc",greedy_load_factor=.8)
        alg=mo.FGDscrScheduler(**config);alg.set_data(cs,ns)
        z=time.perf_counter();result=alg.run();wall=round((time.perf_counter()-z)*1000,3)
        store(dest/"scheduler_output.json",result)
        node_queues={}
        for node,cids in result["assignment"].items():
            node_queues[node]=list(result["ordered_queues"][node]) if a.mode=="falcons" else sorted(cids)
    else:
        node_queues={"native":[x["cid"] for x in containers]}
    cover=[x for q in node_queues.values() for x in q]
    if len(cover)!=a.count or len(set(cover))!=a.count:
        raise RuntimeError("Planner rejected or duplicated requests")
    store(dest/"queues.json",node_queues)
    store(dest/"provenance.json",{
      "policy":a.mode,"profile":a.profile,"real_oci_manifest_refs":[x["image_ref"] for x in images],
      "layer_count":len(layers),"exact_compressed_sizes_file":"layer_sizes_bytes.json",
      "simulator_layer_size_mapping":"exact compressed bytes divided by 1e6 (decimal MB), floats; disk and bandwidth modeled",
      "initial_cache_model":a.cache_model+"; not automatically validated by this adapter",
      "algorithm_parameters":config,"algorithm_wall_ms":wall,
      "cache_mode":"uncontrolled, warm/cold NOT established",
      "admission":"precomputed static resource constraints, not dynamic Kubernetes reservation",
      "limitations":"real OCI digests, real Pods, external FGP/DCO plan; no physical PGDSF eviction; external nodeName binding vs native kube-scheduler; fixed global inflight cap does not imply full baseline fairness"})
    nsname="falcons-oci-"+uuid.uuid4().hex[:8]
    print("RUN",rid,"namespace",nsname,"mode",a.mode,"algorithm_ms",wall,flush=True)
    print("Queues",{n:len(q) for n,q in node_queues.items()},flush=True)
    kc("create","namespace",nsname)
    kc("label","namespace",nsname,"app.kubernetes.io/part-of=falcons-oci-experiment")
    cursor=0;node_names=list(node_queues);active={};ack={};submitted={};finished={};fail=""
    t0=time.perf_counter()
    try:
        while time.perf_counter()-t0<a.timeout:
            if active:
                pods=json.loads(kc("-n",nsname,"get","pods","-o","json"))["items"]
                known={p["metadata"]["name"]:p for p in pods}
                for name,cid in list(active.items()):
                    if name not in known:continue
                    p=known[name];st=p["status"];phase=st.get("phase")
                    for c in st.get("containerStatuses",[]):
                        w=c.get("state",{}).get("waiting") or {}
                        if w.get("reason") in {"ErrImagePull","ImagePullBackOff","InvalidImageName","CreateContainerConfigError","CreateContainerError"}:
                            raise RuntimeError(f"Image startup error {cid}: {w.get('reason')} {w.get('message')}")
                    if phase=="Failed":raise RuntimeError(f"Failed {cid}: {st.get('reason')} {st.get('message')}")
                    if phase=="Succeeded":
                        del active[name]
                        finished[cid]=round(time.perf_counter()-t0,3)
                        print("[DONE]",cid,"node",p["spec"].get("nodeName"),"batch_elapsed",finished[cid],flush=True)
            if len(finished)==a.count:break
            while len(active)<a.max_inflight and len(submitted)<a.count:
                selected=None
                for j in range(len(node_names)):
                    idx=(cursor+j)%len(node_names);n=node_names[idx]
                    if node_queues[n]:
                        selected=(n,node_queues[n].pop(0));cursor=(idx+1)%len(node_names);break
                if not selected:break
                n,cid=selected
                p={"apiVersion":"v1","kind":"Pod",
                   "metadata":{"name":"job-"+cid,"namespace":nsname,
                       "labels":{"app.kubernetes.io/part-of":"falcons-oci-experiment"}},
                   "spec":{"restartPolicy":"Never","activeDeadlineSeconds":a.timeout,
                     "terminationGracePeriodSeconds":1,
                     "containers":[{"name":"app","image":real_images[cid],
                       "imagePullPolicy":"IfNotPresent",
                       "command":["/bin/sh","-c"],"args":["sleep 8; echo DONE"],
                       "resources":{"requests":{"cpu":"150m","memory":"120Mi","ephemeral-storage":"120Mi"},
                                    "limits":{"cpu":"500m","memory":"240Mi","ephemeral-storage":"350Mi"}}}]}}
                if a.mode!="default":p["spec"]["nodeName"]=n
                z=time.perf_counter()
                kc("create","-f","-",stdin=json.dumps(p))
                ack[cid]=round((time.perf_counter()-z)*1000,3)
                submitted[cid]=round(time.perf_counter()-t0,3)
                active["job-"+cid]=cid
                print("[SUBMIT]",cid,"->",n if a.mode!="default" else "native",
                      "active",len(active),flush=True)
            time.sleep(2)
        else:
            fail=f"Global timeout after {a.timeout}s"
    except Exception as e:
        fail=str(e)
        print("[ERROR]",fail,file=sys.stderr,flush=True)
    raw=json.loads(kc("-n",nsname,"get","pods","-o","json"))
    store(dest/"pods_raw.json",raw)
    store(dest/"events_raw.json",json.loads(kc("-n",nsname,"get","events","-o","json")))
    rows=[]
    for p in raw["items"]:
        cid=p["metadata"]["name"].removeprefix("job-")
        status=p["status"];st=(status.get("containerStatuses") or [{}])[0]
        t=st.get("state",{}).get("terminated") or {}
        began=t.get("startedAt") or st.get("state",{}).get("running",{}).get("startedAt")
        end=t.get("finishedAt")
        rows.append({"cid":cid,"image":real_images[cid],
            "node":p["spec"].get("nodeName",""),"phase":status.get("phase"),
            "exit_code":t.get("exitCode"),
            "api_ack_ms":ack.get(cid),"controller_submit_relative_s":submitted.get(cid),
            "controller_finished_relative_s":finished.get(cid),
            "create_to_start_s":interval(p["metadata"].get("creationTimestamp"),began),
            "create_to_finish_s":interval(p["metadata"].get("creationTimestamp"),end)})
    if rows:
        with (dest/"measurements.csv").open("w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    success=sum(x["phase"]=="Succeeded" and x["exit_code"]==0 for x in rows)
    summary={"run":rid,"namespace":nsname,"mode":a.mode,"count":a.count,
             "submitted":len(submitted),"succeeded":success,
             "planner_ms":wall,"global_inflight":a.max_inflight,
             "batch_controller_wall_s":round(time.perf_counter()-t0,3),
             "cache_model":a.cache_model,
             "mean_request_to_finish_s":round(statistics.mean(finished.values()),3) if finished else None,
             "p95_request_to_finish_s":(
                 round(sorted(finished.values())[int(round(.95*(len(finished)-1)))],3)
                 if finished else None
             ),
             "worker_distribution":dict(__import__("collections").Counter(
                 x["node"] for x in rows)),
             "failure":fail,"scientific_scope":"real immutable OCI images & real Pods; physical cache uncontrolled; exploratory"}
    store(dest/"summary.json",summary)
    print(json.dumps(summary,indent=2),flush=True)
    print("ARTIFACTS",dest,"NAMESPACE",nsname,flush=True)
    return 0 if not fail and success==a.count else 3
if __name__=="__main__":sys.exit(main())
