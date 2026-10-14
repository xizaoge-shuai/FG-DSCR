#!/usr/bin/env python3
"""COMNET R2.1: paired serial vs pipelined execution counterfactual.

All six ORIGINAL saved placement/queue outputs are held fixed. Serial download
startups share one pull channel per edge node, but container compute can overlap
with subsequent image pulls and other container compute. CPU/mem/disk are
acquired at execution start and released on completion using a deterministic
event heap. The pull sizes/cache hits are inferred EXACTLY from the original
serial simulator per-container completion times and unchanged run times.
No new download policy, admission decisions or layer-level bandwidth sharing
are introduced.

This is a limited (but fair across methods) pipelined replay; it is NOT a
complete asynchronous Kubernetes admission or multi-registry emulator.
"""
from __future__ import annotations
import argparse, csv, heapq, json, math, statistics
from pathlib import Path
from collections import defaultdict
from run_formal_parameter_audit import raw_article_metrics,REF_D,REF_CA,REF_FRAG

METHODS={"fg":"FG-DSCR-GC","ilrsa":"ILR-SA","lrs":"LRScheduler",
         "gahrl":"GAHRL","orr":"ORR","lasa":"LASA"}

def metric_map(res):
    raw=res.get("container_metrics",{})
    items=[]
    if isinstance(raw,list):
        for x in raw:
            if isinstance(x,dict):items.append(x)
    elif isinstance(raw,dict):
        for k,v in raw.items():
            if isinstance(v,dict):
                item=dict(v)
                item.setdefault("cid",str(k))
                items.append(item)
            elif isinstance(v,list):
                for item in v:
                    if isinstance(item,dict):items.append(item)
    result={}
    for x in items:
        cid=x.get("cid") or x.get("container_id") or x.get("id")
        if cid is None:continue
        for k in ("completion_time","finish_time","end_time"):
            if k in x:
                try:
                    val=float(x[k]);break
                except (TypeError,ValueError):pass
        else:continue
        if str(cid) in result and abs(result[str(cid)]-val)>1e-7:
            raise ValueError(f"Conflicting completion time for {cid}")
        result[str(cid)]=val
    return result

def serial_times(res):
    # Main Fig.8 simulators save node_details q and per-node completion arrays;
    # learned/greedy baselines also commonly save container_metrics by CID.
    out=metric_map(res)
    details=res.get("node_details",{})
    queues=res["ordered_queues"]
    for eid,cids in queues.items():
        node=details.get(eid,{})
        ends=node.get("completion_times")
        if not isinstance(ends,list):continue
        if len(ends)!=len(cids):
            raise ValueError(f"Misaligned completion times for node={eid}")
        for cid,t in zip(cids,ends):
            cid=str(cid);t=float(t)
            if cid in out and abs(out[cid]-t)>0.01:
                raise ValueError(f"Reported completion times disagree node={eid} cid={cid}: {out[cid]} vs {t}")
            out[cid]=t
    expected={str(cid) for q in queues.values() for cid in q}
    missing=expected-set(out)
    if missing:
        raise ValueError(f"Cannot reconstruct {len(missing)} serial edge times, e.g. {list(sorted(missing))[:3]}")
    return out

def safe_resources(case):
    return {str(c["cid"]):{k:float(c.get("resources",{}).get(k,0.)) for k in ("cpu","mem","disk")}
            for c in case["containers"]}

def pipelined_replay(case,res,src_times):
    c_by_id={str(c["cid"]):c for c in case["containers"]}
    n_by_id={str(n["eid"]):n for n in case["nodes"]}
    rs=safe_resources(case)
    pipeline={}
    waits=0
    overlapping=0
    max_active=0
    total_nominal_pull=0.
    for eid,queue in res["ordered_queues"].items():
        eid=str(eid);n=n_by_id[eid]
        cap={k:float(n["resources"][k]) for k in ("cpu","mem","disk")}
        used=dict.fromkeys(cap,0.)
        running=[]
        pull_clock=0.
        serial_clock=0.
        for cid in queue:
            cid=str(cid)
            c=c_by_id[cid]
            run=float(c["run_time"])
            serial_end=src_times[cid]
            pull_time=serial_end-serial_clock-run
            # Log rounding can yield a near-zero negative transfer delta.
            if pull_time < -0.05:
                raise ValueError(
                    f"Cannot infer serial transfer time: node={eid} cid={cid} "
                    f"prev={serial_clock} next={serial_end} run={run} pull={pull_time}"
                )
            pull_time=max(0.,pull_time)
            serial_clock=serial_end
            total_nominal_pull+=pull_time
            pull_clock+=pull_time  # single serialized pull channel
            ready=pull_clock
            # Advance completions to current execution eligibility time
            while running and running[0][0]<=ready+1e-9:
                _,old_cid=heapq.heappop(running)
                for k in cap:used[k]-=rs[old_cid][k]
            req=rs[cid]
            for k in cap:
                if req[k]>cap[k]+1e-8:
                    raise ValueError(f"Task {cid} alone exceeds node {eid} capacity {k}")
            def fits():
                return all(used[k]+req[k]<=cap[k]+1e-8 for k in cap)
            delayed=False
            while not fits():
                if not running:
                    raise RuntimeError(f"No resources available and no running task: {cid}")
                end,old_cid=heapq.heappop(running)
                ready=max(ready,end)
                for k in cap:used[k]-=rs[old_cid][k]
                delayed=True
                while running and running[0][0]<=ready+1e-9:
                    _,old_cid=heapq.heappop(running)
                    for k in cap:used[k]-=rs[old_cid][k]
            if delayed:waits+=1
            if running:overlapping+=1
            for k in cap:used[k]+=req[k]
            finish=ready+run
            heapq.heappush(running,(finish,cid))
            max_active=max(max_active,len(running))
            pipeline[cid]=finish
    return pipeline,dict(resource_wait_events=waits,overlap_starts=overlapping,
                         peak_concurrent_runs=max_active,
                         reconstructed_total_pull_s=total_nominal_pull)

def score(case,orig,completion,provision_s):
    data={
        "assignment":orig["assignment"],
        "container_metrics":[{"cid":cid,"completion_time":t} for cid,t in completion.items()],
        "summary":{"ACT":float(orig.get("summary",{}).get("ACT",0.)),
                   "downloaded_mb":float(orig.get("summary",{}).get("downloaded_mb",0.))}
    }
    return raw_article_metrics(case,data,provision_s)

def save(path,rows):
    with path.open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--case-dir",type=Path,default=Path("cases/drtp_cache_only_sweep_88_prefix"))
    ap.add_argument("--saved-result-dir",type=Path,
                    default=Path("results/drtp/k8s_same_scale/fig5_overall_prefix"))
    ap.add_argument("--out-dir",type=Path,default=Path("results/comnet_revision/concurrency_replay"))
    ap.add_argument("--provision-sec",type=float,default=300.)
    ap.add_argument("--sizes",nargs="+",type=int,default=list(range(200,1001,100)))
    a=ap.parse_args()
    out=a.out_dir;out.mkdir(parents=True,exist_ok=True)
    rows=[]
    for n in a.sizes:
        path=a.case_dir/f"drtp_img88_cacheonly_1024mb_{n}.json"
        case=json.loads(path.read_text(encoding="utf-8"))
        for key,name in METHODS.items():
            src=a.saved_result_dir/f"{key}_prefix_req{n}.json"
            if not src.is_file():raise FileNotFoundError(src)
            res=json.loads(src.read_text(encoding="utf-8"))
            ser=serial_times(res)
            con,info=pipelined_replay(case,res,ser)
            if set(con)!=set(ser):raise AssertionError("Edge scheduling set changed")
            if any(con[cid]>ser[cid]+0.05 for cid in ser):
                raise AssertionError(f"Pipelined model slower than serial; inspect {key} {n}")
            serial_metrics=score(case,res,ser,a.provision_sec)
            pipe_metrics=score(case,res,con,a.provision_sec)
            if abs(serial_metrics["CA_rate"]-pipe_metrics["CA_rate"])>1e-9:
                raise AssertionError("CA changed under frozen admission")
            if abs(serial_metrics["frag_score"]-pipe_metrics["frag_score"])>1e-9:
                raise AssertionError("Static fragmentation changed under frozen admission")
            for mode,m in (("serial_archived",serial_metrics),("pipelined_compute",pipe_metrics)):
                rows.append({
                    "requests":n,"method":name,"execution_model":mode,
                    "all_request_mean_s":m["all_request_mean"],
                    "P95_all_s":m["P95_all"],"D_s":m["Delay_raw"],
                    "CA_rate":m["CA_rate"],"Frag":m["frag_score"],
                    "DownloadedMB":m["downloaded_mb"],"J":m["J"],
                    **info
                })
            print(f"N={n:4d} {name:<14} SerialJ={serial_metrics['J']:.4f} "
                  f"PipeJ={pipe_metrics['J']:.4f} "
                  f"Delta={pipe_metrics['J']-serial_metrics['J']:+.4f} "
                  f"overlap_starts={info['overlap_starts']} "
                  f"resource_waits={info['resource_wait_events']}",flush=True)
    save(out/"serial_vs_pipeline_by_scale.csv",rows)
    means=[]
    for name in METHODS.values():
        for mode in ("serial_archived","pipelined_compute"):
            subset=[r for r in rows if r["method"]==name and r["execution_model"]==mode]
            means.append({
                "method":name,"model":mode,"scales":len(subset),
                **{k:statistics.mean(r[k] for r in subset) for k in (
                    "all_request_mean_s","P95_all_s","D_s","CA_rate","Frag",
                    "DownloadedMB","J","resource_wait_events","overlap_starts","peak_concurrent_runs"
                )}
            })
    save(out/"serial_vs_pipeline_summary.csv",means)
    (out/"provenance.json").write_text(json.dumps({
        "reviewer":"R2.1",
        "source_case_dir":str(a.case_dir),
        "saved_result_dir":str(a.saved_result_dir),
        "input":"same fixed node assignments, startup order, and realized layer-pull delays from archived original simulations",
        "serial_model":"original per-node serialized download + compute",
        "pipelined_model":"single serialized pull stream per node, overlapping compute after pull, capacity-constrained runtime, CPU/MEM/DISK released on completion",
        "not_modelled":[
            "online admission reallocation following resource release",
            "cross-node/shared registry contention","parallel layer pulls",
            "kubelet and container runtime overhead","network-bandwidth sharing",
            "cache pinning/races during concurrent pulls"
        ],
        "normalization":{"D_ref":REF_D,"CA_ref":REF_CA,"Frag_ref":REF_FRAG},
        "cloud_provision_seconds":a.provision_sec,
        "note":"Compare algorithm RANKING under the model change; no claims of validated live Kubernetes deployment."
    },indent=2,ensure_ascii=False),encoding="utf-8")
    print("\n=== Mean J across nine request scales ===")
    for r in means:
        print(f"{r['method']:<14} {r['model']:<18} J={r['J']:.5f} "
              f"D={r['D_s']:.3f}s CA={r['CA_rate']:.4f}")
    print("Saved:",out)

if __name__=="__main__":
    main()
