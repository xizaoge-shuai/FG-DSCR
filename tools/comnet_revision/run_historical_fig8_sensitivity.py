#!/usr/bin/env python3
"""R1.4: paper Fig.8 historical CLI-accurate one-at-a-time sensitivity.

Explicitly sets the ACTUAL archived launcher settings (move-only topK6, five
rounds, lambda_fail=1000, etc.) before each perturbation, rather than merely
loading the archived_candidate *weight profile* into move_swap defaults.
One Python process per invocation: set PYTHONHASHSEED before launching it.
Hash-dependent cached eviction ties mean exact historical Fig.8 reproducibility
is NOT guaranteed. This produces a new, separately labelled sensitivity run.
"""
from __future__ import annotations
import argparse,csv,json,os,sys,hashlib
from pathlib import Path
from statistics import mean
from run_formal_parameter_audit import modload,run_one,PARAMS,PROFILES

HISTORICAL={
    "phase1_neighbor_mode":"move",
    "move_topk_per_node":6,
    "max_best_response_rounds":5,
    "beam_width":1,
    "greedy_load_factor":0.8,
    "init_order":"resource_asc",
    "lambda_cong":1.0,
    "lambda_frag":10.0,
    "lambda_aff":0.0,
    "lambda_task_load":0.03,
    "lambda_fail":1000.0,
    "hard_resource_filter":True,
    "k_pin":6,
    "cache_policy":"pgdsf",
    "order_policy":"dynamic_state",
}
DCO_PGDSF={
    "alpha1_reuse":1.,"alpha2_future":0.15,"alpha3_pull":50.,
    "alpha4_evict":0.02,"beta_slot":2.,"rho_centrality":0.2,
    "kappa_overshoot":0.5,
}

def save(path,rows):
    with path.open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]))
        w.writeheader();w.writerows(rows)

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--algorithm",type=Path,default=Path("scripts/fg_dscr_incremental.py"))
    p.add_argument("--case-dir",type=Path,default=Path("cases/drtp_cache_only_sweep_88_prefix"))
    p.add_argument("--paper",type=Path,default=Path("results/comnet_revision/raw_metrics/raw_allrequest_by_method_scale.csv"))
    p.add_argument("--out-dir",type=Path,default=Path("results/comnet_revision/parameter_sensitivity_fig8_exact_config"))
    p.add_argument("--sizes",nargs="+",type=int,default=list(range(200,1001,100)))
    a=p.parse_args()
    if not str(a.case_dir).endswith("_prefix"):
        p.error("Fig.8 exact workload requires _prefix cases")
    seed=os.environ.get("PYTHONHASHSEED")
    if seed is None or seed=="random":
        p.error("Set fixed PYTHONHASHSEED=2 (or other chosen explicit integer) before launching.")
    with a.paper.open(newline="",encoding="utf-8") as f:
        source={int(float(r["requests"])):r for r in csv.DictReader(f) if r["method"]=="FG-DSCR-GC"}
    mod=modload(str(a.algorithm))
    configurations=[("default",None)]
    base={**HISTORICAL,**DCO_PGDSF}
    for name,factors in PARAMS:
        original=base[name]
        for factor in factors:
            if original==0:
                if factor==0.5:continue
                configurations.append((f"{name}_absolute_0.1",(name,0.1)))
            else:
                configurations.append((f"{name}_x{factor:g}",(name,original*factor)))
    rows=[]
    a.out_dir.mkdir(parents=True,exist_ok=True)
    for tag,change in configurations:
        for n in a.sizes:
            overrides=dict(base)
            if change is not None:overrides[change[0]]=change[1]
            case=a.case_dir/f"drtp_img88_cacheonly_1024mb_{n}.json"
            metrics,elapsed,parameters,result=run_one(mod,case,"archived_candidate",overrides,300.)
            row=dict(variant=tag,requests=n,J=metrics["J"],
                     all_request_mean_s=metrics["all_request_mean"],P95_all_s=metrics["P95_all"],
                     CA_rate=metrics["CA_rate"],Frag=metrics["frag_score"],
                     downloaded_mb=metrics["downloaded_mb"],
                     assignment_signature=metrics["assignment_signature"],
                     ordering_signature=metrics["ordering_signature"],
                     cache_trace_signature=metrics["cache_trace_signature"],
                     wall_ms=elapsed,paper_J=float(source[n]["Obj_Final"]),
                     J_minus_paper=metrics["J"]-float(source[n]["Obj_Final"]))
            rows.append(row)
            print(f"{tag:<34} n={n:4d} J={row['J']:.6f} "
                  f"paper_J_rounded={row['paper_J']:.3f} "
                  f"gap={row['J_minus_paper']:+.5f}",flush=True)
        save(a.out_dir/"sensitivity_by_scale.csv",rows)
    idx={(r["variant"],r["requests"]):r for r in rows}
    summaries=[]
    for tag,_ in configurations:
        sub=[idx[tag,n] for n in a.sizes]
        baseline=[idx["default",n] for n in a.sizes]
        summaries.append(dict(variant=tag,scales=len(sub),
                              mean_J=mean(x["J"] for x in sub),
                              mean_delta_J_vs_same_run_default=mean(
                                  x["J"]-b["J"] for x,b in zip(sub,baseline)),
                              changed_placements=sum(x["assignment_signature"]!=b["assignment_signature"] for x,b in zip(sub,baseline)),
                              changed_orders=sum(x["ordering_signature"]!=b["ordering_signature"] for x,b in zip(sub,baseline)),
                              changed_cache_traces=sum(x["cache_trace_signature"]!=b["cache_trace_signature"] for x,b in zip(sub,baseline))))
    save(a.out_dir/"sensitivity_summary.csv",summaries)
    provenance={
        "classification":"Fig.8 CLI-matched parameter sensitivity, not assured exact Fig.8 archival outputs",
        "pythonhashseed":seed,"algorithm":str(a.algorithm),
        "algorithm_sha256":hashlib.sha256(a.algorithm.read_bytes()).hexdigest(),
        "case_dir":str(a.case_dir),"sizes":a.sizes,"base_algorithm_settings":base,
        "scoring":"Paper normalized J; synthetic cloud quantile +300s",
        "baseline_disclaimer":"The archived paper dataset may use a different hash seed, so match the algorithm CLI "
                              "separately from exact output-level parity. Do not overwrite historical raw files.",
        "scale_disclaimer":"Nine workload prefix scales are NOT nine independent statistical replicates.",
    }
    (a.out_dir/"provenance.json").write_text(json.dumps(provenance,indent=2,ensure_ascii=False),encoding="utf-8")
    print("\n=== Summary: change relative to matching baseline under same seed ===")
    for r in summaries:print(f"{r['variant']:<34} meanJ={r['mean_J']:.6f} delta={r['mean_delta_J_vs_same_run_default']:+.6f} placements={r['changed_placements']} orders={r['changed_orders']}")
    print("Saved",a.out_dir)

if __name__=="__main__":main()
