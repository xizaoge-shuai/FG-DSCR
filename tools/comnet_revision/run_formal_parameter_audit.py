#!/usr/bin/env python3
"""COMNET R2.3/R1.4: audit actual vs declared config using the paper's J.

Audits two documented profiles against the already-verified final Fig. 8 data.
Optional one-at-a-time parameter sensitivity (no tuning on the test results).
Uses the original fig5_allrequest latency and frag score definitions, with
fixed paper normalization references; never reports the legacy scheduler Obj.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import importlib.util
import json
import math
import sys
import time
from pathlib import Path
from statistics import mean

REF_D,REF_CA,REF_FRAG=440.7696,.77924,.180069
PROFILES={
 "paper_declared":dict(lambda_cong=1.,lambda_frag=.1,lambda_aff=.2,
                       lambda_task_load=.2,init_order="image_resource",greedy_load_factor=0.),
 "archived_candidate":dict(lambda_cong=1.,lambda_frag=10.,lambda_aff=0.,
                           lambda_task_load=.03,init_order="resource_asc",greedy_load_factor=.8),
}
PARAMS=[
 ("lambda_cong", [0.5,2.]),
 ("lambda_frag", [0.5,2.]),
 ("lambda_aff", [0.5,2.]),
 ("lambda_task_load", [0.5,2.]),
 ("alpha1_reuse", [0.5,2.]),
 ("alpha2_future", [0.5,2.]),
 ("alpha3_pull", [0.5,2.]),
 ("alpha4_evict", [0.5,2.]),
 ("beta_slot", [0.5,2.]),
 ("rho_centrality", [0.5,2.]),
 ("kappa_overshoot", [0.5,2.]),
]

def modload(file):
    spec=importlib.util.spec_from_file_location("falcons_sensitivity",file)
    mod=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=mod
    spec.loader.exec_module(mod)
    return mod

def pct(values,p):
    v=sorted(values)
    if not v:return 0.
    pos=(len(v)-1)*p/100
    lo=math.floor(pos);hi=math.ceil(pos);w=pos-lo
    return v[lo]*(1-w)+v[hi]*w

def raw_article_metrics(case,result,provision_s):
    # Same semantics as tools/summarize_fig5_allrequest_latency.py
    containers=case["containers"];all_ids=[str(c["cid"]) for c in containers]
    by_id={str(c["cid"]):c for c in containers}
    nodes={str(n["eid"]):n for n in case["nodes"]}
    assignment=result["assignment"]
    edge=set(str(cid) for cids in assignment.values() for cid in cids)
    ca_ids=[cid for cid in all_ids if cid not in edge]
    # final simulator logs must contain the completion time of EVERY edge request
    log=result["container_metrics"]
    if not isinstance(log,list):raise ValueError("Expected container_metrics list")
    edge_t={str(x["cid"]):float(x["completion_time"]) for x in log if "completion_time" in x}
    missing=edge-set(edge_t)
    if missing:raise ValueError(f"Missing {len(missing)} edge completion logs")
    refs=sorted(edge_t[cid] for cid in edge)
    if not refs:refs=[float(result["summary"]["ACT"])]
    ca_seen=0;times=[]
    for cid in all_ids:
        if cid in edge:
            times.append(edge_t[cid])
        else:
            q=(ca_seen+.5)/max(1,len(ca_ids))
            pos=q*(len(refs)-1)
            lo=int(pos);hi=min(lo+1,len(refs)-1);w=pos-lo
            edge_like=refs[lo]*(1-w)+refs[hi]*w
            times.append(provision_s+edge_like)
            ca_seen+=1
    avg=mean(times);p95=pct(times,95)
    fragments=[]
    for eid,cids in assignment.items():
        if not cids:continue
        n=nodes[str(eid)];pressures=[]
        for resource,capacity in n["resources"].items():
            cap=float(capacity)
            if cap<=0:continue
            used=sum(float(by_id[str(cid)].get("resources",{}).get(resource,0.)) for cid in cids)
            pressures.append(used/cap)
        if pressures:
            x=mean(pressures)
            fragments.append(mean(abs(y-x) for y in pressures))
    frag=mean(fragments) if fragments else 0.
    ca=len(ca_ids)/max(len(all_ids),1)
    d=.5*(avg+p95)
    return dict(all_request_mean=avg,P95_all=p95,Delay_raw=d,
                CA_rate=ca,frag_score=frag,
                D_norm=d/REF_D,CA_norm=ca/REF_CA,Frag_norm=frag/REF_FRAG,
                J=d/REF_D+ca/REF_CA+frag/REF_FRAG,
                downloaded_mb=float(result["summary"].get("downloaded_mb",0)),
                edge_scheduled=len(edge),cloud_requests=len(ca_ids))

def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode('utf-8')).hexdigest()[:16]

def run_one(mod,path,profile,overrides,provision_s):
    raw=json.loads(path.read_text(encoding="utf-8"))
    cs,ns,layers=mod.load_case(str(path))
    kw=dict(
        layer_sizes_mb=layers,beam_width=1,k_pin=6,unit_mb=50,
        hard_resource_filter=True,max_best_response_rounds=5,
        phase1_neighbor_mode="move_swap",move_topk_per_node=8,swap_topk_per_node=6,
        alpha1_reuse=1.,alpha2_future=.15,alpha3_pull=50.,alpha4_evict=.02,
        beta_slot=2.,rho_centrality=.2,kappa_overshoot=.5,
        cache_policy="pgdsf",order_policy="dynamic_state",
    )
    kw.update(PROFILES[profile])
    # Underscore-prefixed overrides are protocol controls, not scheduler knobs.
    # In particular, a genuine input-order queue is NOT the same as the
    # legacy order_policy="arrival", which returns greedy assignment order.
    input_order_queue = bool(overrides.get("_input_order_queue", False))
    kw.update({k:v for k,v in overrides.items() if not k.startswith("_")})
    s=mod.FGDscrScheduler(**kw);s.set_data(cs,ns)
    if input_order_queue:
        # Case JSON's container sequence is used as the deterministic
        # FCFS proxy; do not claim physical arrival timestamps are measured.
        position={c.cid:i for i,c in enumerate(cs)}
        s.order_node=lambda cids,node: sorted(cids,key=lambda cid:position[cid])
    t0=time.perf_counter();out=s.run();wall_ms=(time.perf_counter()-t0)*1000
    metrics=raw_article_metrics(raw,out,provision_s)
    metrics['assignment_signature']=fingerprint(out['assignment'])
    metrics['ordering_signature']=fingerprint(out['ordered_queues'])
    metrics['cache_trace_signature']=fingerprint(out['node_step_logs'])
    effective=kw.copy()
    effective["effective_queue_order"] = "case_input_sequence" if input_order_queue else kw["order_policy"]
    return metrics,wall_ms,effective,out

def paper_results(path):
    with path.open(newline="",encoding="utf-8") as f:items=list(csv.DictReader(f))
    return {int(float(x["requests"])):x for x in items if x["method"]=="FG-DSCR-GC"}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--mode",choices=["audit","sensitivity"],required=True)
    ap.add_argument("--algorithm",default="scripts/fg_dscr_incremental.py")
    ap.add_argument("--case-dir",default="cases/drtp_cache_only_sweep_88")
    ap.add_argument("--paper-metrics",default="results/comnet_revision/raw_metrics/raw_allrequest_by_method_scale.csv")
    ap.add_argument("--out-dir",default="results/comnet_revision/config_audit")
    ap.add_argument("--profile",choices=list(PROFILES),default="paper_declared")
    ap.add_argument("--provision-sec",type=float,default=300.)
    args=ap.parse_args()
    mod=modload(args.algorithm)
    observed=paper_results(Path(args.paper_metrics))
    scales=list(range(200,1001,100))
    if any(n not in observed for n in scales):raise ValueError("Paper result missing one or more request scales")
    outdir=Path(args.out_dir);outdir.mkdir(parents=True,exist_ok=True)
    jobs=[]
    if args.mode=="audit":
        for profile in PROFILES:jobs.append((profile,"default",{}))
    else:
        jobs.append((args.profile,"default",{}))
        for name,factors in PARAMS:
            base=PROFILES[args.profile].get(name,{
                "alpha1_reuse":1.,"alpha2_future":.15,"alpha3_pull":50.,"alpha4_evict":.02,
                "beta_slot":2.,"rho_centrality":.2,"kappa_overshoot":.5,
            }.get(name))
            for factor in factors:
                if base is None:raise ValueError("Unknown param "+name)
                if base==0.:
                    # A multiplicative sweep around zero is undefined.
                    # The profile keeps the true zero baseline; an absolute
                    # 0.1 alternative tests the effect of activating this term.
                    if factor == .5: continue
                    actual=.1
                    label=f"{name}_absolute_{actual:g}"
                else:
                    actual=base*factor
                    label=f"{name}_x{factor:g}"
                jobs.append((args.profile,label,{name:actual}))
    rows=[];metadata=[]
    for profile,tag,changes in jobs:
        for n in scales:
            case=Path(args.case_dir)/f"drtp_img88_cacheonly_1024mb_{n}.json"
            metrics,wall_ms,kw,_=run_one(mod,case,profile,changes,args.provision_sec)
            prior=observed[n]
            row=dict(profile=profile,variant=tag,requests=n,**metrics,
                     total_algorithm_wall_ms=wall_ms,
                     paper_J=float(prior["Obj_Final"]),
                     paper_CA=float(prior["CA_rate"]),
                     paper_frag=float(prior["frag_score"]),
                     paper_ACT=float(prior["ACT_all"]),
                     paper_P95=float(prior["P95_all"]))
            row["J_gap"]=row["J"]-row["paper_J"]
            row["CA_gap"]=row["CA_rate"]-row["paper_CA"]
            row["ACT_gap"]=row["all_request_mean"]-row["paper_ACT"]
            rows.append(row)
            print(f"profile={profile} variant={tag} n={n} paperJ={row['paper_J']:.4f} "
                  f"reproducedJ={row['J']:.4f} deltaJ={row['J_gap']:+.4f} "
                  f"CA={row['CA_rate']:.3f} paperCA={row['paper_CA']:.3f}",flush=True)
        metadata.append(dict(profile=profile,variant=tag,params=kw))
    base_name="config_reproduction" if args.mode=="audit" else "parameter_sensitivity"
    with (outdir/(base_name+".csv")).open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    (outdir/(base_name+"_settings.json")).write_text(json.dumps({
        "source_paper":args.paper_metrics,"algorithm":args.algorithm,
        "mode":args.mode,"provision_s":args.provision_sec,"profiles":PROFILES,
        "references":{"D":REF_D,"CA":REF_CA,"Frag":REF_FRAG},
        "settings":metadata,
        "warnings":[
          "Manuscript declared and archived profiles are competing provenance hypotheses.",
          "Matching J alone is NOT proof of matching original code path. Check placements and provenance.",
          "Changing parameters re-runs decisions; this is not a post-hoc reweighting experiment.",
          "Cloud latency is the published synthetic edge-like quantile replay + 300 s provisioning.",
          "Controller parameters can have identical outputs when decisions and cache traces are unchanged; inspect fingerprint columns.",
          "Any profile whose recorded J differs materially from the submission is NOT a valid basis for manuscript sensitivity claims."
        ]
    },indent=2,ensure_ascii=False),encoding="utf-8")
    if args.mode=="audit":
        print("\n== Absolute mean J gap from paper, smaller is closer (not proof of provenance) ==")
        for profile in PROFILES:
            subset=[r for r in rows if r["profile"]==profile]
            print(profile,mean(abs(r["J_gap"]) for r in subset),
                  "mean_CArate",mean(r["CA_rate"] for r in subset))
    print("saved",outdir/(base_name+".csv"))
if __name__=="__main__":main()