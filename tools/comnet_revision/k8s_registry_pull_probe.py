#!/usr/bin/env python3
"""Non-destructive real Kubernetes registry accessibility probe.

Creates a NEW uniquely named test namespace, and a tiny test Pod for each
requested (worker,image) pair. Does not restart containerd, touch existing
Pods, or prune any cached layers. Default imagePullPolicy=Always means this is
a registry-reachability test, NOT a cold download benchmark.
"""
import argparse,csv,datetime as dt,json,pathlib,subprocess,sys,time,uuid

DEFAULT_NODES=["falcons-worker1","falcons-worker2","falcons-worker3"]
DEFAULT_IMAGES=["busybox:1.36.1","alpine:3.20"]
WAITING_ERRORS={"ErrImagePull","ImagePullBackOff","InvalidImageName",
                "CreateContainerConfigError","CreateContainerError",
                "RunContainerError","ErrImageNeverPull"}

def kubectl(*args,stdin=None,timeout=50):
    cmd=["kubectl","--request-timeout=30s",*args]
    p=subprocess.run(cmd,input=stdin,text=True,stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE,timeout=timeout)
    if p.returncode:
        raise RuntimeError(" ".join(cmd)+" : "+p.stderr.strip())
    return p.stdout

def writejson(path,obj):
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding="utf-8")

def state(pod):
    status=pod.get("status",{})
    phase=status.get("phase","Unknown")
    if phase in ("Succeeded","Failed"):
        return phase,status.get("reason","")
    for c in status.get("containerStatuses",[]):
        waiting=(c.get("state",{}).get("waiting") or {})
        reason=waiting.get("reason","")
        if reason in WAITING_ERRORS:
            return "PullFailure",reason+": "+waiting.get("message","")
    return phase,""

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--workers",nargs="+",default=DEFAULT_NODES)
    ap.add_argument("--images",nargs="+",default=DEFAULT_IMAGES)
    ap.add_argument("--timeout",type=int,default=120)
    ap.add_argument("--image-pull-policy",choices=["Always","IfNotPresent"],
                    default="Always")
    ap.add_argument("--out-root",default="results/comnet_revision/k8s_real_testbed/registry_probe")
    a=ap.parse_args()
    if not (30<=a.timeout<=1800):ap.error("--timeout must be 30..1800")
    current=json.loads(kubectl("get","nodes","-o","json"))
    nodes={x["metadata"]["name"]:x for x in current["items"]}
    for worker in a.workers:
        if worker not in nodes:
            raise SystemExit("Missing requested worker: "+worker)
        if not any(x["type"]=="Ready" and x["status"]=="True"
                   for x in nodes[worker]["status"]["conditions"]):
            raise SystemExit("Worker is not Ready: "+worker)
    rid=uuid.uuid4().hex[:8]
    ns="falcons-regprobe-"+rid
    dest=pathlib.Path(a.out_root).resolve()/rid
    dest.mkdir(parents=True,exist_ok=False)
    pods=[]
    for i,worker in enumerate(a.workers):
        for j,image in enumerate(a.images):
            name=f"probe-{i}-{j}"
            pods.append({
                "apiVersion":"v1","kind":"Pod",
                "metadata":{"name":name,"namespace":ns,
                    "labels":{"app.kubernetes.io/part-of":"comnet-falcons-registry-probe"}},
                "spec":{"nodeName":worker,"restartPolicy":"Never",
                    "activeDeadlineSeconds":a.timeout,
                    "containers":[{
                        "name":"image-test","image":image,
                        "imagePullPolicy":a.image_pull_policy,
                        "command":["/bin/sh","-c"],
                        "args":["echo registry-probe-ok"],
                        "resources":{"requests":{"cpu":"30m","memory":"32Mi"},
                                     "limits":{"cpu":"100m","memory":"64Mi"}}
                    }]}
            })
    writejson(dest/"manifest.json",pods)
    nsmanifest={"apiVersion":"v1","kind":"Namespace","metadata":{
        "name":ns,"labels":{"app.kubernetes.io/part-of":"comnet-falcons-registry-probe"}}}
    kubectl("create","-f","-",stdin=json.dumps(nsmanifest))
    print(f"NAMESPACE {ns} OUT {dest}",flush=True)
    print(f"Probing {len(pods)} worker/image pairs (policy={a.image_pull_policy})",flush=True)
    for p in pods:
        kubectl("create","-f","-",stdin=json.dumps(p))
        print(f"SUBMIT {p['metadata']['name']} -> {p['spec']['nodeName']}"
              f" {p['spec']['containers'][0]['image']}",flush=True)
    start=time.monotonic()
    last={}
    while time.monotonic()-start<a.timeout:
        raw=json.loads(kubectl("-n",ns,"get","pods","-o","json"))
        states={p["metadata"]["name"]:state(p) for p in raw["items"]}
        for name,st in states.items():
            if last.get(name)!=st:
                print("STATUS",name,st[0],(st[1] or "")[:240],flush=True)
                last[name]=st
        if len(states)==len(pods) and all(v[0] in ("Succeeded","Failed","PullFailure")
                                         for v in states.values()):
            break
        time.sleep(3)
    raw=json.loads(kubectl("-n",ns,"get","pods","-o","json"))
    events=json.loads(kubectl("-n",ns,"get","events","-o","json"))
    writejson(dest/"pods_raw.json",raw)
    writejson(dest/"events_raw.json",events)
    rows=[]
    for p in raw["items"]:
        phase,reason=state(p)
        statuses=p.get("status",{}).get("containerStatuses",[])
        cs=statuses[0] if statuses else {}
        rows.append({
            "worker":p["spec"].get("nodeName",""),
            "image":p["spec"]["containers"][0]["image"],
            "pod":p["metadata"]["name"],"state":phase,
            "detail":reason,"image_id":cs.get("imageID",""),
            "pull_policy":a.image_pull_policy
        })
    if rows:
        with (dest/"summary.csv").open("w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    ok=len(rows)==len(pods) and all(r["state"]=="Succeeded" for r in rows)
    print("FINAL:",sum(r["state"]=="Succeeded" for r in rows),"/",len(pods),
          "succeeded",flush=True)
    for r in rows:print(f"  {r['worker']:<19} {r['image']:<20} {r['state']:<12} {r['detail'][:180]}",flush=True)
    print("Artifacts:",dest,flush=True)
    print("Clean only this namespace AFTER collecting evidence:",
          "kubectl delete namespace "+ns,flush=True)
    print("NOTE: pull policy =",a.image_pull_policy,
          "; IfNotPresent allows local reuse; Always resolves registry but may reuse cached content blobs. "
          "This is NOT a controlled cold-cache experiment.",flush=True)
    return 0 if ok else 2

if __name__=="__main__":sys.exit(main())
