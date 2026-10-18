#!/usr/bin/env python3
"""Exploratory, bounded-concurrency FALCONS real-Kubernetes execution check.

All policies share a GLOBAL maximum count of non-terminal Pods. External
FALCONS/FGP policies preserve FGP node assignment and in-node DCO/FCFS
submission order; the native Kubernetes baseline uses normal kube-scheduler.
The concurrency cap does NOT remove every policy difference: native K8s still
chooses its own node and can run multiple jobs per node, and submission order
is not guaranteed image pull order. Results are functional/early feasibility
only, NOT the paper's full real image-layer/cache benchmark.

Does not overwrite/delete Pods, namespaces or cached images. New namespace per
run; explicitly logs images and raw Pod/Event responses, including failures.
"""
import argparse,csv,datetime as dt,importlib.util,json,pathlib,statistics,subprocess,sys,time,uuid
from collections import Counter
from k8s_integration_runner import (
    WORKERS,IMAGES,kc,build_case,pod_obj,delta,percentile
)

ERRORS={"ErrImagePull","ImagePullBackOff","InvalidImageName",
        "CreateContainerConfigError","CreateContainerError",
        "RunContainerError","CrashLoopBackOff","ErrImageNeverPull"}

def save(path,obj):
    path.write_text(json.dumps(obj,indent=2,ensure_ascii=False),encoding="utf-8")

def load_algo(repo,case,profile):
    path=repo/"scripts/fg_dscr_incremental.py"
    spec=importlib.util.spec_from_file_location("falcons_small_real",str(path))
    mod=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=mod;spec.loader.exec_module(mod)
    cs,ns,ls=mod.load_case(str(case))
    if profile=="paper_declared":
        weights=dict(init_order="image_resource",greedy_load_factor=0.0,
                     lambda_cong=1.0,lambda_frag=.1,lambda_aff=.2,
                     lambda_task_load=.2)
    else:
        weights=dict(init_order="resource_asc",greedy_load_factor=.8,
                     lambda_cong=1.0,lambda_frag=10.0,lambda_aff=0.0,
                     lambda_task_load=.03)
    parameters=dict(layer_sizes_mb=ls,beam_width=1,k_pin=6,
        hard_resource_filter=True,lambda_fail=1000.0,
        max_best_response_rounds=5,phase1_neighbor_mode="move",
        move_topk_per_node=6,cache_policy="pgdsf",order_policy="dynamic_state",
        alpha1_reuse=1.0,alpha2_future=.15,alpha3_pull=50.0,
        alpha4_evict=.02,beta_slot=2.0,rho_centrality=.2,
        kappa_overshoot=.5,**weights)
    scheduler=mod.FGDscrScheduler(**parameters);scheduler.set_data(cs,ns)
    start=time.perf_counter();result=scheduler.run()
    return result,round((time.perf_counter()-start)*1000,3),parameters

def state_error(p):
    s=p.get("status",{})
    if s.get("phase")=="Failed":
        return "Pod Failed: "+str(s.get("reason",""))+" "+str(s.get("message",""))
    for c in s.get("containerStatuses",[]):
        wait=c.get("state",{}).get("waiting") or {}
        if wait.get("reason") in ERRORS:
            return str(wait.get("reason"))+": "+str(wait.get("message",""))
    return ""

def result_rows(pods,ack,observed):
    rows=[]
    for pod in pods:
        m=pod["metadata"];s=pod["status"];name=m["name"]
        cid=name.removeprefix("falc-")
        cs=(s.get("containerStatuses") or [{}])[0]
        st=cs.get("state",{})
        term=st.get("terminated") or {}
        start=term.get("startedAt") or (st.get("running") or {}).get("startedAt")
        finish=term.get("finishedAt")
        rows.append({
            "cid":cid,"pod":name,"node":pod["spec"].get("nodeName",""),
            "phase":s.get("phase",""),"image":pod["spec"]["containers"][0]["image"],
            "created_utc":m.get("creationTimestamp",""),
            "started_utc":start or "","finished_utc":finish or "",
            "create_to_start_s":delta(m.get("creationTimestamp"),start),
            "create_to_finish_s":delta(m.get("creationTimestamp"),finish),
            "image_id":cs.get("imageID",""),"exit_code":term.get("exitCode",""),
            "ack_ms":ack.get(cid),"controller_observed_terminal_s":observed.get(cid),
            "error":state_error(pod)
        })
    return rows

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--mode",choices=["falcons","fgp_fcfs","default"],required=True)
    ap.add_argument("--count",type=int,default=9)
    ap.add_argument("--max-inflight",type=int,default=3,
                    help="Common cluster-wide maximum nonterminal Pod count")
    ap.add_argument("--only-nginx",action="store_true")
    ap.add_argument("--profile",choices=["archived_fig8","paper_declared"],
                    default="archived_fig8")
    ap.add_argument("--timeout",type=int,default=180,
                    help="Total wall-clock deadline across ALL requested Pods")
    ap.add_argument("--poll",type=float,default=1.5)
    ap.add_argument("--out-dir",default="results/comnet_revision/k8s_real_testbed/controlled")
    a=ap.parse_args()
    if a.count not in (3,6,9,12,20,40):ap.error("count must be 3/6/9/12/20/40")
    if not (1<=a.max_inflight<=a.count):ap.error("max-inflight must be in [1,count]")
    if not (90<=a.timeout<=1800):ap.error("timeout must be 90..1800 seconds")
    repo=pathlib.Path(__file__).resolve().parents[2]
    all_nodes=json.loads(kc("get","nodes","-o","json"))["items"]
    nodes={x["metadata"]["name"]:x for x in all_nodes}
    for n in WORKERS:
        if n not in nodes:raise RuntimeError("Worker missing "+n)
        ready=any(c["type"]=="Ready" and c["status"]=="True"
                  for c in nodes[n]["status"]["conditions"])
        if not ready:raise RuntimeError("Worker NotReady "+n)
    run="fc-"+uuid.uuid4().hex[:8]
    namespace="falcons-ctrl-"+uuid.uuid4().hex[:8]
    out=pathlib.Path(a.out_dir).resolve()/run
    out.mkdir(parents=True,exist_ok=False)
    case=build_case([nodes[n] for n in WORKERS],a.count,only_nginx=a.only_nginx)
    casefile=out/"algorithm_case.json";save(casefile,case)
    requests={x["cid"]:x for x in case["containers"]}
    cids=list(requests)
    result=None;wall_ms=None;parameters=None
    if a.mode!="default":
        result,wall_ms,parameters=load_algo(repo,casefile,a.profile)
        save(out/"scheduler_output.json",result)
        queues={}
        for node,cids_node in result["assignment"].items():
            if a.mode=="falcons":
                sequence=result["ordered_queues"][node]
            else:
                sequence=sorted(cids_node)
            queues[node]=list(sequence)
        covered=[x for q in queues.values() for x in q]
        if len(covered)!=len(cids) or set(covered)!=set(cids):
            raise RuntimeError(f"Planner admitted {len(covered)}/{len(cids)} requests; "
                               "cannot compare; reduce workload")
        print("Placement", {n:len(q) for n,q in queues.items()},flush=True)
    else:
        queues={"native":list(cids)}
    save(out/"planned_queues.json",queues)
    save(out/"provenance.json",{
        "mode":a.mode,"profile":a.profile,"count":a.count,
        "global_concurrency_cap":a.max_inflight,
        "only_nginx":a.only_nginx,"timeout_s":a.timeout,
        "algorithm_wall_ms":wall_ms,"parameters":parameters,
        "layer_metadata":"MODELLED PLACEHOLDER only; NOT real OCI layer digest/bytes",
        "limitations":[
            "A global inflight cap is shared, but per-node concurrency may differ due to placement.",
            "External placement uses spec.nodeName; native baseline uses kube-scheduler.",
            "DCO controls submitted order only; no guarantee of containerd pull order.",
            "All jobs synthetically run sleep 8s, rather than a production workload.",
            "Containerd caches are NOT normalized; imagePullPolicy IfNotPresent.",
            "No PGDSF cache eviction installed or verified in containerd.",
            "End-to-end wall clock is observed at controller polling granularity; event-based timings separate.",
            "Results are integration tests, not final paper-quality cold OCI layer comparisons."
        ]})
    print(f"RUN {run} mode={a.mode} count={a.count} max_inflight={a.max_inflight} "
          f"algorithm_wall_ms={wall_ms} images={list(sorted(set(x['service_type'] for x in case['containers'])))}",
          flush=True)
    kc("create","namespace",namespace)
    kc("label","namespace",namespace,"app.kubernetes.io/part-of=comnet-falcons-integration")
    print("NAMESPACE",namespace,"OUT",out,flush=True)
    ack={};seen_terminal={};active={};submitted=0;terminal=0;failure=""
    node_order=list(queues)
    cursor=0
    t0=time.perf_counter()
    deadline=t0+a.timeout
    try:
        while time.perf_counter()<deadline:
            if active:
                podlist=json.loads(kc("-n",namespace,"get","pods","-o","json"))["items"]
                known={p["metadata"]["name"]:p for p in podlist}
                for name in list(active):
                    pod=known.get(name)
                    if not pod:continue
                    err=state_error(pod)
                    if err:
                        failure=f"{name}: {err}"
                        raise RuntimeError(failure)
                    if pod["status"].get("phase")=="Succeeded":
                        cid=active.pop(name)
                        terminal+=1
                        seen_terminal[cid]=round(time.perf_counter()-t0,3)
                        print(f"[DONE] {cid} node={pod['spec'].get('nodeName')} "
                              f"elapsed={seen_terminal[cid]}s",flush=True)
            if terminal==a.count:
                break
            # Round-robin one Pod at a time across policy-specific queues.
            while len(active)<a.max_inflight and submitted<a.count:
                candidate=None
                for j in range(len(node_order)):
                    idx=(cursor+j)%len(node_order)
                    node=node_order[idx]
                    if queues[node]:
                        candidate=(node,queues[node].pop(0))
                        cursor=(idx+1)%len(node_order)
                        break
                if candidate is None:break
                node,cid=candidate
                assigned=None if a.mode=="default" else node
                pod=pod_obj(namespace,"falc-"+cid,assigned,
                            requests[cid]["service_type"],a.timeout)
                began=time.perf_counter()
                kc("create","-f","-",stdin=json.dumps(pod))
                ack[cid]=round((time.perf_counter()-began)*1000,3)
                active["falc-"+cid]=cid
                submitted+=1
                print(f"[SUBMIT] {cid} bound_to={assigned or 'native'} "
                      f"inflight={len(active)}/{a.max_inflight} "
                      f"submitted={submitted}/{a.count}",flush=True)
            if not active and submitted==a.count:
                break
            time.sleep(a.poll)
        else:
            failure=f"Global timeout after {a.timeout}s, submitted={submitted}/{a.count}, terminal={terminal}"
    except Exception as e:
        failure=str(e)
        print("[ERROR]",failure,file=sys.stderr,flush=True)
    elapsed=round(time.perf_counter()-t0,3)
    pods=[]
    try:
        pods=json.loads(kc("-n",namespace,"get","pods","-o","json"))["items"]
        save(out/"pods_raw.json",pods)
    except Exception as e:
        print("Could not collect Pod details:",e,file=sys.stderr)
    try:
        ev=json.loads(kc("-n",namespace,"get","events","-o","json"))
        save(out/"events_raw.json",ev)
    except Exception as e:
        print("Could not collect events:",e,file=sys.stderr)
    rows=result_rows(pods,ack,seen_terminal)
    if rows:
        with (out/"measurements.csv").open("w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    starts=[x["create_to_start_s"] for x in rows if x["create_to_start_s"] is not None]
    finishes=[x["create_to_finish_s"] for x in rows if x["create_to_finish_s"] is not None]
    successful=sum(x["phase"]=="Succeeded" and x["exit_code"]==0 for x in rows)
    summary={
        "run":run,"namespace":namespace,"mode":a.mode,"count":a.count,
        "submitted":submitted,"succeeded":successful,
        "max_inflight":a.max_inflight,
        "algorithm_wall_ms":wall_ms,
        "api_ack_mean_ms":round(statistics.mean(ack.values()),3) if ack else None,
        "controller_observed_batch_wall_s":elapsed,
        "mean_create_to_start_s":round(statistics.mean(starts),4) if starts else None,
        "p95_create_to_start_s":percentile(starts,.95),
        "mean_create_to_finish_s":round(statistics.mean(finishes),4) if finishes else None,
        "p95_create_to_finish_s":percentile(finishes,.95),
        "node_distribution":dict(Counter(x["node"] for x in rows)),
        "failure":failure,
        "scientific_scope":"Exploratory global-cap comparison, not fair OCI layer-cache study"
    }
    save(out/"summary.json",summary)
    print(json.dumps(summary,indent=2,ensure_ascii=False),flush=True)
    print(f"Resources retained. Clean this namespace after evidence saved: "
          f"kubectl delete namespace {namespace}",flush=True)
    return 0 if (not failure and successful==a.count) else 3

if __name__=="__main__":
    sys.exit(main())
