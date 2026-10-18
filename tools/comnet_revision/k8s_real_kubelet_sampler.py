#!/usr/bin/env python3
"""Read-only real-K8s node/pod telemetry sampler, no metrics-server dependency.

Uses kube-apiserver -> kubelet summary proxy. Call alongside the test runner
to capture CPU usage, working-set RAM and host-network byte counters. NODE
network counters are not a measure of OCI layer traffic, especially where
Docker Hub is accessed by SSH-tunneled proxy. Never call them download_mb.
Stops cleanly on SIGINT/SIGTERM and saves each successful sample to CSV/JSONL.
"""
import argparse,csv,datetime as dt,json,pathlib,signal,subprocess,sys,time
STOP=False
def stopping(sig,frame):
    global STOP
    STOP=True
def read(node,timeout):
    p=subprocess.run(["kubectl","--request-timeout="+str(timeout)+"s",
        "get","--raw",f"/api/v1/nodes/{node}/proxy/stats/summary"],
        capture_output=True,text=True,timeout=timeout+5)
    if p.returncode:raise RuntimeError(p.stderr.strip()[:500])
    return json.loads(p.stdout)
def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--workers",nargs="+",default=[
        "falcons-worker1","falcons-worker2","falcons-worker3"])
    ap.add_argument("--interval",type=float,default=3.)
    ap.add_argument("--max-duration",type=float,default=1800.)
    ap.add_argument("--timeout",type=int,default=12)
    ap.add_argument("--namespace-prefix",default="falcons-oci-")
    ap.add_argument("--out-dir",default="results/comnet_revision/k8s_real_testbed/live_telemetry")
    a=ap.parse_args()
    if a.interval<1 or a.max_duration<2:ap.error("interval >=1 and duration >=2 are required")
    signal.signal(signal.SIGINT,stopping);signal.signal(signal.SIGTERM,stopping)
    out=pathlib.Path(a.out_dir);out.mkdir(parents=True,exist_ok=True)
    fields=["utc","elapsed_s","node","node_cpu_nanocores","node_mem_working_set_bytes",
            "node_network_rx_bytes","node_network_tx_bytes",
            "matching_pod_count","matching_container_cpu_nanocores",
            "matching_container_mem_working_set_bytes"]
    csvfile=(out/"kubelet_summary_samples.csv").open("w",encoding="utf-8",newline="")
    rawfile=(out/"kubelet_summary_raw.jsonl").open("w",encoding="utf-8")
    errfile=(out/"kubelet_summary_errors.jsonl").open("w",encoding="utf-8")
    writer=csv.DictWriter(csvfile,fieldnames=fields);writer.writeheader()
    start=time.monotonic();count=0
    try:
        while not STOP and time.monotonic()-start<a.max_duration:
            tic=time.monotonic()
            for node in a.workers:
                if STOP:break
                now=dt.datetime.now(dt.timezone.utc).isoformat()
                try:
                    obj=read(node,a.timeout)
                    observed=[p for p in obj.get("pods",[])
                        if p.get("podRef",{}).get("namespace","").startswith(a.namespace_prefix)]
                    containers=[c for p in observed for c in p.get("containers",[])]
                    network=obj.get("node",{}).get("network",{})
                    nc=obj.get("node",{}).get("cpu",{})
                    nm=obj.get("node",{}).get("memory",{})
                    row={"utc":now,"elapsed_s":round(time.monotonic()-start,3),"node":node,
                         "node_cpu_nanocores":nc.get("usageNanoCores"),
                         "node_mem_working_set_bytes":nm.get("workingSetBytes"),
                         "node_network_rx_bytes":network.get("rxBytes"),
                         "node_network_tx_bytes":network.get("txBytes"),
                         "matching_pod_count":len(observed),
                         "matching_container_cpu_nanocores":sum(
                             c.get("cpu",{}).get("usageNanoCores") or 0 for c in containers),
                         "matching_container_mem_working_set_bytes":sum(
                             c.get("memory",{}).get("workingSetBytes") or 0 for c in containers)}
                    writer.writerow(row)
                    rawfile.write(json.dumps({"utc":now,"node":node,"stats":obj},ensure_ascii=False)+"\n")
                    count+=1
                except Exception as e:
                    errfile.write(json.dumps({"utc":now,"node":node,"error":str(e)})+"\n")
                    print("[KUBELET ERROR]",node,str(e),flush=True)
            csvfile.flush();rawfile.flush();errfile.flush()
            print("[SAMPLE]",count,"elapsed",round(time.monotonic()-start,1),flush=True)
            until=tic+a.interval
            while not STOP and time.monotonic()<until:
                time.sleep(min(.2,until-time.monotonic()))
    finally:
        csvfile.close();rawfile.close();errfile.close()
        print("Saved",out,"successful samples",count,flush=True)
    return 0 if count>0 else 2
if __name__=="__main__":sys.exit(main())
