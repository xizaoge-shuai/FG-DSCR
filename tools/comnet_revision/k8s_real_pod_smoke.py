#!/usr/bin/env python3
"""Real Kubernetes three-Worker Pod execution & timing smoke test.

A narrow, conservative validation, NOT an FALCONS-vs-baselines comparison.
Explicit spec.nodeName bypasses kube-scheduler; images may already be cached.
Creates only namespace falcons-real-smoke and labeled test pods; NEVER prunes
shared containerd images or resets cluster resources.
"""
import argparse,csv,datetime as dt,json,pathlib,statistics,subprocess,sys,time,uuid

LABEL="comnet-falcons-real-smoke"
WORKERS=("falcons-worker1","falcons-worker2","falcons-worker3")

def kubectl(*args,stdin=None,timeout=40):
    p=subprocess.run(["kubectl",*args],input=stdin,text=True,
                     stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
    if p.returncode:
        raise RuntimeError("kubectl "+' '.join(args)+" failed: "+p.stderr.strip())
    return p.stdout

def ts(s):
    if not s:return None
    return dt.datetime.fromisoformat(s.replace("Z","+00:00"))

def secs(a,b):
    x,y=ts(a),ts(b)
    return round((y-x).total_seconds(),4) if x and y else None

def quantile(x,p):
    xs=sorted(x)
    if not xs:return None
    t=(len(xs)-1)*p
    lo=int(t);hi=min(lo+1,len(xs)-1)
    return xs[lo]*(hi-t)+xs[hi]*(t-lo) if hi!=lo else xs[lo]

def namespace_exists(ns):
    p=subprocess.run(["kubectl","get","namespace",ns,"-o","json"],
                     text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if p.returncode:return False
    o=json.loads(p.stdout)
    if o["metadata"].get("labels",{}).get("app.kubernetes.io/part-of")!=LABEL:
        raise RuntimeError(f"Namespace {ns} already exists but is not owned by this smoke tool")
    return True

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--out-dir",default="results/comnet_revision/k8s_real_testbed/smoke")
    p.add_argument("--namespace",default="falcons-real-smoke")
    p.add_argument("--image",default="nginx:1.27-alpine")
    p.add_argument("--sleep-seconds",type=int,default=8)
    p.add_argument("--timeout-seconds",type=int,default=300)
    p.add_argument("--nodes",nargs="+",default=list(WORKERS))
    args=p.parse_args()
    if not (2<=args.sleep_seconds<=60):p.error("--sleep-seconds must be 2..60")
    if args.timeout_seconds<90:p.error("--timeout-seconds must be at least 90")
    out=pathlib.Path(args.out_dir);out.mkdir(parents=True,exist_ok=True)
    token=uuid.uuid4().hex[:8]
    runname="frs-"+token
    nodes=json.loads(kubectl("get","nodes","-o","json"))
    byname={n["metadata"]["name"]:n for n in nodes["items"]}
    for name in args.nodes:
        if name not in byname:raise RuntimeError("Worker missing: "+name)
        if not any(c["type"]=="Ready" and c["status"]=="True" for c in byname[name]["status"]["conditions"]):
            raise RuntimeError("Worker not Ready: "+name)
    ns=args.namespace
    if not namespace_exists(ns):
        obj={"apiVersion":"v1","kind":"Namespace","metadata":{
             "name":ns,"labels":{"app.kubernetes.io/part-of":LABEL}}}
        kubectl("apply","-f","-",stdin=json.dumps(obj))
    pod_names=[]
    ack=[]
    for i,node in enumerate(args.nodes):
        name=f"{runname}-{i+1}"
        obj={
            "apiVersion":"v1","kind":"Pod","metadata":{
                "name":name,"namespace":ns,
                "labels":{"app.kubernetes.io/part-of":LABEL,"falcons-run":runname}},
            "spec":{
                "nodeName":node,"restartPolicy":"Never",
                "terminationGracePeriodSeconds":1,"activeDeadlineSeconds":args.timeout_seconds,
                "containers":[{"name":"workload","image":args.image,
                    "imagePullPolicy":"IfNotPresent",
                    "command":["/bin/sh","-c"],
                    "args":[f"sleep {args.sleep_seconds}; echo FALCONS-REAL-SMOKE-DONE"],
                    "resources":{"requests":{"cpu":"100m","memory":"64Mi","ephemeral-storage":"64Mi"},
                                 "limits":{"cpu":"500m","memory":"128Mi","ephemeral-storage":"256Mi"}}
                }]
            }
        }
        t0=time.perf_counter()
        kubectl("create","-f","-",stdin=json.dumps(obj),timeout=60)
        ack.append({"pod":name,"node":node,"api_ack_ms":round((time.perf_counter()-t0)*1000,2)})
        pod_names.append(name)
        print(f"CREATE {name} -> {node} API ack {ack[-1]['api_ack_ms']}ms",flush=True)
    deadline=time.monotonic()+args.timeout_seconds
    phases={}
    while time.monotonic()<deadline:
        pod_list=json.loads(kubectl("-n",ns,"get","pods","-l",f"falcons-run={runname}","-o","json"))
        phases={x["metadata"]["name"]:x["status"].get("phase","Unknown") for x in pod_list["items"]}
        print("Phases:",phases,flush=True)
        if len(phases)==len(pod_names) and all(v in ("Succeeded","Failed") for v in phases.values()):
            break
        time.sleep(3)
    raw=json.loads(kubectl("-n",ns,"get","pods","-l",f"falcons-run={runname}","-o","json"))
    events=json.loads(kubectl("-n",ns,"get","events","-o","json"))
    (out/f"{runname}_pods.json").write_text(json.dumps(raw,indent=2,ensure_ascii=False))
    names=set(pod_names)
    evs=[e for e in events["items"] if e.get("involvedObject",{}).get("name") in names]
    (out/f"{runname}_events.json").write_text(json.dumps(evs,indent=2,ensure_ascii=False))
    ack_by_name={r["pod"]:r for r in ack}
    rows=[]
    for pod in raw["items"]:
        m=pod["metadata"];state=pod["status"]
        name=m["name"]
        statuses=state.get("containerStatuses",[])
        cs=statuses[0] if statuses else {}
        status=cs.get("state",{})
        term=status.get("terminated",{})
        run=status.get("running",{})
        started=term.get("startedAt") or run.get("startedAt")
        finished=term.get("finishedAt")
        rows.append({
            "run":runname,"pod":name,"node":pod["spec"].get("nodeName",""),
            "phase":state.get("phase","Unknown"),
            "image":pod["spec"]["containers"][0]["image"],
            "creation_utc":m.get("creationTimestamp",""),
            "started_utc":started or "","finished_utc":finished or "",
            "create_to_start_s":secs(m.get("creationTimestamp"),started),
            "create_to_finish_s":secs(m.get("creationTimestamp"),finished),
            "image_id":cs.get("imageID",""),"exit_code":term.get("exitCode",""),
            "api_ack_ms":ack_by_name[name]["api_ack_ms"],
            "restart_count":cs.get("restartCount",0),
            "node_binding_mode":"spec.nodeName (bypasses kube-scheduler)",
            "cache_state":"uncontrolled/pre-existing; NOT cold-cache measurement"
        })
    path=out/f"{runname}_measurements.csv"
    with path.open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]))
        w.writeheader();w.writerows(rows)
    ok=all(r["phase"]=="Succeeded" and r["exit_code"]==0 for r in rows) and len(rows)==len(args.nodes)
    ready=[r["create_to_start_s"] for r in rows if r["create_to_start_s"] is not None]
    done=[r["create_to_finish_s"] for r in rows if r["create_to_finish_s"] is not None]
    summary={
        "run":runname,"success":ok,"nodes":args.nodes,"namespace":ns,
        "image":args.image,"n_pods":len(rows),
        "create_to_start_mean_s":statistics.mean(ready) if ready else None,
        "create_to_start_p95_s":quantile(ready,.95),
        "create_to_finish_mean_s":statistics.mean(done) if done else None,
        "create_to_finish_p95_s":quantile(done,.95),
        "api_ack_mean_ms":statistics.mean(x["api_ack_ms"] for x in ack),
        "disclaimer":"real K8s container execution with external nodeName binding; "
                     "this is an infrastructure smoke test, NOT FALCONS/controller/PGDSF validation; "
                     "cache state uncontrolled; no actual registry bytes measured"
    }
    (out/f"{runname}_summary.json").write_text(json.dumps(summary,indent=2,ensure_ascii=False))
    print(json.dumps(summary,indent=2,ensure_ascii=False))
    print("Measurements:",path)
    print(f"Namespace {ns} and pods preserved for inspection. Delete only this test namespace when finished.")
    return 0 if ok else 3

if __name__=="__main__":
    sys.exit(main())
