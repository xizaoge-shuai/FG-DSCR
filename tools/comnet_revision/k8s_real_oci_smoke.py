#!/usr/bin/env python3
"""Small real OCI FALCONS executor: use VERIFIED layers, not image placeholders.

Functional smoke trial with immutable platform image digest, genuine compressed
layer sizes, common global inflight cap. Does not claim PGDSF is deployed in
containerd or represent controlled cold cache. Records Pod/event evidence and
total request-to-finish time from common batch start (controller wall time).
"""
import argparse,csv,datetime as dt,importlib.util,json,pathlib,random,statistics,subprocess,sys,time,uuid
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
    ap.add_argument("--image-count",type=int,default=0,
       help="Number of first images from catalog to use (0=all); 4/8/12 for v2 corpus")
    ap.add_argument("--image-selection",choices=["round_robin","zipf_like"],default="round_robin",
       help="Fixed seeded Zipf-like request popularity or reproducible cyclic workload")
    ap.add_argument("--zipf-alpha",type=float,default=1.1)
    ap.add_argument("--seed",type=int,default=42,
       help="Controls workload choice and variable application demands; same seed preserves N prefixes")
    ap.add_argument("--task-profile",choices=["uniform","mixed","pressure"],default="uniform",
       help="mixed: sleep-only light workloads; pressure: ~0.4-0.5 CPU, ~0.5-0.75 GiB requests plus actual CPU and RAM stress")
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
    if a.image_count:
        if not 1<=a.image_count<=len(images):ap.error("image-count outside catalog size")
        images=images[:a.image_count]
    if len(images)<3:raise ValueError("Expected at least three real OCI images")
    if not 0<=a.zipf_alpha<=2.5:ap.error("zipf-alpha must be 0..2.5")
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
    rng=random.Random(a.seed)
    ranks=list(range(len(images)))
    weights=[(i+1)**(-a.zipf_alpha) for i in ranks]
    # Per-request stochastic generation: identical workload prefixes for
    # counts 10/20/40 with same catalog, seed and profile.
    for i in range(a.count):
        idx=(i % len(images)) if a.image_selection=="round_robin" else rng.choices(ranks,weights=weights,k=1)[0]
        im=images[idx];cid=f"r{i:03d}"
        real_images[cid]=im["image_ref"]
        if a.task_profile=="mixed":
            cpu=rng.choice((.06,.08,.10,.12,.16))
            mem=rng.choice((.125,.1875,.25,.3125))
            runtime=rng.choice((8,12,20,32,40))
            disk=rng.choice((.10,.15,.20))
        elif a.task_profile=="pressure":
            # High reservation AND active work, unlike the legacy sleep-only case.
            cpu=rng.choice((.40,.45,.50))
            mem=rng.choice((.50,.625,.75))
            runtime=rng.choice((16,20,24))
            disk=rng.choice((.20,.30,.40))
        else:
            cpu,mem,runtime,disk=.15,.12,8,.12
        containers.append({"cid":cid,"layers":[l["digest"] for l in im["layers"]],
             "resources":{"cpu":cpu,"mem":mem,"disk":disk},
             "run_time":float(runtime),"service_type":im["source"]})
    case={"nodes":nodes,"containers":containers,
          "layer_sizes_mb":{d:v/1000000.0 for d,v in layers.items()}}
    if a.task_profile=="pressure":
        requested={k:sum(c["resources"][k] for c in containers)
                   for k in ("cpu","mem","disk")}
        modeled={k:sum(n["resources"][k] for n in nodes)
                 for k in ("cpu","mem","disk")}
        print("[RESOURCE PRESSURE] total_requested",requested,
              "modeled_node_budgets",modeled,flush=True)
        for k in requested:
            if requested[k]>modeled[k]+1e-9:
                raise RuntimeError(
                    f"Impossible workload aggregate {k}: requested={requested[k]:.3f} "
                    f"modeled_available={modeled[k]:.3f}; lower count or Pod requests.")
    rid="oci-"+uuid.uuid4().hex[:8]
    dest=pathlib.Path(a.out_root).resolve()/rid;dest.mkdir(parents=True)
    store(dest/"workload.json",case)
    store(dest/"layer_sizes_bytes.json",layers)
    bycid={x["cid"]:x for x in containers}
    image_hist={}
    for cid,ref in real_images.items():image_hist[ref]=image_hist.get(ref,0)+1
    print("[WORKLOAD] images",len(images),"selection",a.image_selection,
          "task_profile",a.task_profile,"seed",a.seed,
          "histogram",image_hist,flush=True)
    repo=pathlib.Path(__file__).resolve().parents[2]
    result=None;wall=None;config=None
    phase_timing={"fgp_ms":None,"dco_ms":None}
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
        # Non-invasive timer wrappers: the original planner remains unchanged.
        # FGP includes best-response search. DCO aggregates ALL per-node order_node calls.
        fgp_orig=alg.best_response_assignment
        dco_orig=alg.order_node
        phase_timing={"fgp_ms":0.0,"dco_ms":0.0}
        def fgp_timed(*args,**kwargs):
            t=time.perf_counter()
            try:return fgp_orig(*args,**kwargs)
            finally:phase_timing["fgp_ms"]+=(time.perf_counter()-t)*1000
        def dco_timed(*args,**kwargs):
            t=time.perf_counter()
            try:return dco_orig(*args,**kwargs)
            finally:phase_timing["dco_ms"]+=(time.perf_counter()-t)*1000
        alg.best_response_assignment=fgp_timed
        alg.order_node=dco_timed
        z=time.perf_counter()
        if a.mode=="fgp_fcfs":
            # FCFS ablation must not execute DCO or simulate PGDSF merely to
            # discard its output; placement is the only planner component.
            assign=alg.best_response_assignment()
            bynode={node["eid"]:[] for node in nodes}
            for cid,eid in assign.items():
                bynode[eid].append(cid)
            result={"assignment":bynode,
                    "ordered_queues":{},
                    "failed_containers":list(getattr(alg,"failed_container_ids",[])),
                    "phase1_history":getattr(alg,"phase1_history",[]),
                    "summary":{"algo":"FGP+FCFS","stage2":"not executed",
                        "runtime_scope":"FGP placement only"}}
        else:
            result=alg.run()
        wall=round((time.perf_counter()-z)*1000,3)
        phase_timing={k:round(v,3) for k,v in phase_timing.items()}
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
      "image_count":len(images),"image_selection":a.image_selection,
      "zipf_alpha":a.zipf_alpha,"seed":a.seed,
      "task_profile":a.task_profile,
      "execution_semantics":("actual CPU busy loop + 192Mi tmpfs payload"
                              if a.task_profile=="pressure" else "sleep-only"),
      "workload_scope":"controlled small-scale OCI microservice diversity; NOT the original 88-image trace",
      "layer_count":len(layers),"exact_compressed_sizes_file":"layer_sizes_bytes.json",
      "simulator_layer_size_mapping":"exact compressed bytes divided by 1e6 (decimal MB), floats; disk and bandwidth modeled",
      "initial_cache_model":a.cache_model+"; not automatically validated by this adapter",
      "algorithm_parameters":config,"algorithm_wall_ms":wall,
      "phase1_placement_ms":phase_timing["fgp_ms"],
      "phase2_ordering_ms":phase_timing["dco_ms"],
      "cache_mode":"uncontrolled, warm/cold NOT established",
      "admission":"precomputed static resource constraints, not dynamic Kubernetes reservation",
      "limitations":"real OCI digests, real Pods, external FGP/DCO plan; no physical PGDSF eviction; external nodeName binding vs native kube-scheduler; global inflight cap is external; pressure CPU and tmpfs memory must be verified with kubelet samples; mixed remains sleep-only"})
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
                req=bycid[cid]["resources"]
                job_seconds=int(bycid[cid]["run_time"])
                cpu_milli=max(1,round(req["cpu"]*1000))
                memory_mib=max(1,round(req["mem"]*1024))
                disk_mib=max(1,round(req["disk"]*1024))
                if a.task_profile=="pressure":
                    # No privileged mounts or host mutation. GNU/BusyBox dd,
                    # date and POSIX sh are supplied by official images.
                    work_command=(
                        "set -eu; "
                        "dd if=/dev/zero of=/work/load.bin bs=1048576 count=192 "
                        ">/dev/null 2>&1; "
                        f"stop=$(($(date +%s)+{job_seconds})); "
                        'while [ "$(date +%s)" -lt "$stop" ]; do '
                        'i=0; while [ "$i" -lt 15000 ]; do i=$((i+1)); done; '
                        'done; echo DONE'
                    )
                else:
                    work_command=f"sleep {job_seconds}; echo DONE"
                p={"apiVersion":"v1","kind":"Pod",
                   "metadata":{"name":"job-"+cid,"namespace":nsname,
                       "labels":{"app.kubernetes.io/part-of":"falcons-oci-experiment"}},
                   "spec":{"restartPolicy":"Never","activeDeadlineSeconds":a.timeout,
                     "terminationGracePeriodSeconds":1,
                     "containers":[{"name":"app","image":real_images[cid],
                       "imagePullPolicy":"IfNotPresent",
                       "command":["/bin/sh","-c"],"args":[work_command],
                       "resources":{"requests":{"cpu":f"{cpu_milli}m","memory":f"{memory_mib}Mi",
                                                "ephemeral-storage":f"{disk_mib}Mi"},
                                    "limits":{"cpu":f"{max(250,cpu_milli*2)}m",
                                              "memory":f"{memory_mib*2}Mi",
                                              "ephemeral-storage":f"{disk_mib*2}Mi"}}}]}}
                if a.task_profile=="pressure":
                    p["spec"]["volumes"]=[{"name":"work-mem",
                        "emptyDir":{"medium":"Memory","sizeLimit":"256Mi"}}]
                    p["spec"]["containers"][0]["volumeMounts"]=[
                        {"name":"work-mem","mountPath":"/work"}]
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
             "planner_ms":wall,
             "phase1_placement_ms":phase_timing["fgp_ms"],
             "phase2_ordering_ms":phase_timing["dco_ms"],
             "global_inflight":a.max_inflight,
             "batch_controller_wall_s":round(time.perf_counter()-t0,3),
             "cache_model":a.cache_model,
             "image_count":len(images),"image_selection":a.image_selection,
             "task_profile":a.task_profile,"workload_seed":a.seed,
             "execution_semantics":("cpu_loop_tmpfs_192Mi"
                                    if a.task_profile=="pressure" else "sleep"),
             "mean_request_to_finish_s":round(statistics.mean(finished.values()),3) if finished else None,
             "p95_request_to_finish_s":(
                 round((lambda s,t:s[int(t)]+(s[min(int(t)+1,len(s)-1)]-s[int(t)])*
                      (t-int(t)))(sorted(finished.values()),.95*(len(finished)-1)),3)
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
