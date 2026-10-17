#!/usr/bin/env python3
"""Read-only cross-region Kubernetes provenance for FALCONS COMNET revision.

Run on the real cloud Kubernetes control plane. Does NOT create Pods, edit
node labels, drain nodes, prune images, or require SSH to the workers.
"""
import argparse, csv, datetime as dt, json, pathlib, subprocess, sys

EXPECTED=("falcons-master","falcons-worker1","falcons-worker2","falcons-worker3")

def kubectl(*args):
    p=subprocess.run(["kubectl","--request-timeout=20s",*args],text=True,
                     stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if p.returncode:
        raise RuntimeError("kubectl "+' '.join(args)+" failed: "+p.stderr.strip())
    return p.stdout

def qty(value,what):
    v=str(value)
    if what=="cpu":
        return float(v[:-1])/1000. if v.endswith("m") else float(v)
    # Kubernetes memory quantities include binary and decimal suffixes.
    for suf,mult in (("Ki",2**10),("Mi",2**20),("Gi",2**30),("Ti",2**40),
                     ("K",10**3),("M",10**6),("G",10**9),("T",10**12)):
        if v.endswith(suf):
            return float(v[:-len(suf)])*mult/2**30
    return float(v)/2**30

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--out-dir",default="results/comnet_revision/k8s_real_testbed/preflight")
    ap.add_argument("--workers",nargs="+",default=list(EXPECTED[1:]))
    a=ap.parse_args()
    out=pathlib.Path(a.out_dir);out.mkdir(parents=True,exist_ok=True)
    nodes=json.loads(kubectl("get","nodes","-o","json"))
    pods=json.loads(kubectl("get","pods","-A","-o","json"))
    (out/"nodes_raw.json").write_text(json.dumps(nodes,indent=2,ensure_ascii=False))
    (out/"pods_all_namespaces_raw.json").write_text(json.dumps(pods,indent=2,ensure_ascii=False))
    versions={}
    try:
        versions=json.loads(kubectl("get","--raw","/version"))
    except Exception as exc:
        versions={"warning":str(exc)}
    names={n["metadata"]["name"] for n in nodes["items"]}
    missing=[n for n in a.workers if n not in names]
    rows=[]
    for n in nodes["items"]:
        m=n["metadata"];s=n["status"];name=m["name"]
        ready=next((x["status"] for x in s["conditions"] if x["type"]=="Ready"),"Unknown")
        alloc=s.get("allocatable",{})
        addresses={x["type"]:x["address"] for x in s.get("addresses",[])}
        rows.append({
            "name":name,"ready":ready,
            "role":"worker" if name in a.workers else "other/control-plane",
            "cpu_allocatable_cores":round(qty(alloc.get("cpu","0"),"cpu"),3),
            "memory_allocatable_GiB":round(qty(alloc.get("memory","0"),"mem"),3),
            "ephemeral_storage_allocatable_GiB":round(qty(alloc.get("ephemeral-storage","0"),"mem"),3),
            "internal_ip":addresses.get("InternalIP",""),
            "os_image":s["nodeInfo"].get("osImage",""),
            "kernel_version":s["nodeInfo"].get("kernelVersion",""),
            "kubelet_version":s["nodeInfo"].get("kubeletVersion",""),
            "container_runtime":s["nodeInfo"].get("containerRuntimeVersion",""),
            "arch":s["nodeInfo"].get("architecture",""),
            "taints":json.dumps(n["spec"].get("taints",[]),ensure_ascii=False),
        })
    with (out/"nodes.csv").open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    meta={"timestamp_utc":dt.datetime.now(dt.timezone.utc).isoformat(),
          "current_context":kubectl("config","current-context").strip(),
          "version":versions,"requested_workers":a.workers,
          "cloud_regions":"Record each worker's purchased region separately; K8s node status does not prove cloud region.",
          "warnings":["A Ready node and pod connectivity do NOT verify FGP/DCO integration.",
                      "Observed PVC/cache/registry behavior is not implied by Kubernetes readiness."]}
    (out/"provenance.json").write_text(json.dumps(meta,indent=2,ensure_ascii=False))
    print(f"Context: {meta['current_context']}")
    for r in rows:
        print(f"{r['name']:<24} Ready={r['ready']:<6} "
              f"CPU={r['cpu_allocatable_cores']:>6} cores "
              f"Mem={r['memory_allocatable_GiB']:>7.2f} GiB "
              f"IP={r['internal_ip']}")
    print("Pods across namespaces:",len(pods["items"]))
    if missing:
        print("MISSING WORKERS:",missing,file=sys.stderr)
        return 2
    if any(r["ready"]!="True" for r in rows if r["name"] in a.workers):
        print("BLOCKER: one or more requested workers are not Ready",file=sys.stderr)
        return 2
    print("PASS: expected workers present and Ready; artifacts in",out)
    return 0

if __name__=="__main__":
    sys.exit(main())
