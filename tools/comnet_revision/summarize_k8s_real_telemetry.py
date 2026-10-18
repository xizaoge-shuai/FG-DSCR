#!/usr/bin/env python3
"""Read-only aggregate per-policy real K8s kubelet CPU/Memory telemetry.

This reads individual suite trials.csv and sampled kubelet Summary API CSV.
It measures actual node/pod CPU usage and working set; DOES NOT measure
OCI-registry download bytes. Host-network RX/TX deltas are reported as
unattributed host-traffic observations ONLY.
"""
import argparse,csv,pathlib,re,statistics
from collections import defaultdict
def parse(v):
    if v in ("",None):return None
    try:return float(v)
    except (ValueError,TypeError):return None
def m(x):return statistics.mean(x) if x else None
def wcsv(path,rows):
    if not rows:return
    with path.open("w",newline="",encoding="utf-8") as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]))
        writer.writeheader();writer.writerows(rows)
def summarize(file,base):
    pernode=defaultdict(list)
    with file.open(newline="",encoding="utf-8") as f:
        for z in csv.DictReader(f):
            pernode[z["node"]].append(z)
    out=[]
    for node,arr in sorted(pernode.items()):
        arr.sort(key=lambda z:float(z["elapsed_s"]))
        def series(field,div=1):
            return [v/div for z in arr if (v:=parse(z.get(field))) is not None]
        cpu=series("node_cpu_nanocores",1e9)
        mem=series("node_mem_working_set_bytes",1048576)
        pcpu=series("matching_container_cpu_nanocores",1e9)
        pmem=series("matching_container_mem_working_set_bytes",1048576)
        rx=series("node_network_rx_bytes",1048576)
        tx=series("node_network_tx_bytes",1048576)
        out.append(dict(base,node=node,samples=len(arr),
                        node_cpu_mean_cores=m(cpu),node_cpu_peak_cores=max(cpu,default=None),
                        node_mem_mean_mib=m(mem),node_mem_peak_mib=max(mem,default=None),
                        experiment_pods_cpu_mean_cores=m(pcpu),
                        experiment_pods_cpu_peak_cores=max(pcpu,default=None),
                        experiment_pods_mem_mean_mib=m(pmem),
                        experiment_pods_mem_peak_mib=max(pmem,default=None),
                        host_rx_delta_mib_NOT_OCI=(rx[-1]-rx[0]) if len(rx)>1 else None,
                        host_tx_delta_mib_NOT_OCI=(tx[-1]-tx[0]) if len(tx)>1 else None))
    return out

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--suite-root",default="results/comnet_revision/k8s_real_testbed/multiimage_suite")
    p.add_argument("--out-dir",default="results/comnet_revision/k8s_real_testbed/telemetry_analysis")
    a=p.parse_args()
    rows=[];miss=[]
    for suite in sorted(pathlib.Path(a.suite_root).glob("suite-*")):
        trialfile=suite/"trials.csv"
        if not trialfile.exists():continue
        with trialfile.open(newline="",encoding="utf-8") as f:
            for tr in csv.DictReader(f):
                td=tr.get("telemetry_dir") or ""
                if not td:continue
                sample=pathlib.Path(td)/"kubelet_summary_samples.csv"
                if not sample.exists():
                    miss.append(str(sample));continue
                base={k:tr[k] for k in (
                    "stage","requests","image_count","trial","seed","mode")}
                base["suite"]=suite.name
                rows.extend(summarize(sample,base))
    out=pathlib.Path(a.out_dir);out.mkdir(parents=True,exist_ok=True)
    wcsv(out/"node_telemetry_by_run.csv",rows)
    print("Summarized",len(rows),"node-by-run telemetry records",
          "missing sources",len(miss))
    for r in rows:
        print(r["suite"],"N",r["requests"],"imgs",r["image_count"],
              r["mode"],r["node"],"CPU peak cores",r["node_cpu_peak_cores"],
              "Mem peak MiB",r["node_mem_peak_mib"])
    for path in miss[:20]:print("MISSING",path)
    print("WARNING host RX/TX is NOT OCI download traffic; no physical cache eviction or cloud fallback observed")
    print("Saved:",out)
if __name__=="__main__":main()
