#!/usr/bin/env python3
"""Isolate FGP affinity coefficient in the actual archived Fig.8 execution profile.

The original archived profile used lambda_aff=0, not proof that affinity is
absent in the method. Compare +/-affinity and, separately, the article's
declared four weights with EVERY other Fig.8 launcher setting held constant.
This is a matched sensitivity check; it does NOT overwrite the original Fig.8.
"""
from __future__ import annotations
import argparse,csv,json,sys
from pathlib import Path
from statistics import mean
from run_formal_parameter_audit import modload,run_one

ORIGINAL={
    "lambda_cong":1.0,"lambda_frag":10.0,"lambda_aff":0.0,
    "lambda_task_load":0.03,"init_order":"resource_asc",
    "greedy_load_factor":0.8,"lambda_fail":1000.0,"hard_resource_filter":True,
    "phase1_neighbor_mode":"move","move_topk_per_node":6,
    "max_best_response_rounds":5,"beam_width":1,"k_pin":6,
    "cache_policy":"pgdsf","order_policy":"dynamic_state"
}
DCO_PGDSF={
    "alpha1_reuse":1.0,"alpha2_future":0.15,"alpha3_pull":50.0,
    "alpha4_evict":0.02,"beta_slot":2.0,
    "rho_centrality":0.2,"kappa_overshoot":0.5
}
WEIGHTS={
    "fig8_archived":{},
    "affinity_0p2_only":{"lambda_aff":0.2},
    "paper_four_weights":{"lambda_frag":0.1,"lambda_aff":0.2,"lambda_task_load":0.2}
}
def write(path,rows):
    with path.open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
def main():
    p=argparse.ArgumentParser()
    p.add_argument("--algorithm",default="scripts/fg_dscr_incremental.py")
    p.add_argument("--case-dir",type=Path,default=Path("cases/drtp_cache_only_sweep_88_prefix"))
    p.add_argument("--out-dir",type=Path,default=Path("results/comnet_revision/affinity_parameter_check"))
    p.add_argument("--sizes",type=int,nargs="+",default=[200,500,1000])
    a=p.parse_args()
    mod=modload(a.algorithm)
    rows=[]
    for n in a.sizes:
        case=a.case_dir/f"drtp_img88_cacheonly_1024mb_{n}.json"
        for variant,changes in WEIGHTS.items():
            config={**ORIGINAL,**DCO_PGDSF,**changes}
            metrics,wall,kw,res=run_one(mod,case,"archived_candidate",config,300.)
            row=dict(requests=n,variant=variant,J=metrics["J"],
                     D=metrics["Delay_raw"],CA=metrics["CA_rate"],
                     Frag=metrics["frag_score"],downloaded_MB=metrics["downloaded_mb"],
                     placement=metrics["assignment_signature"],
                     ordering=metrics["ordering_signature"],
                     cache=metrics["cache_trace_signature"],wall_ms=wall)
            rows.append(row)
            print(f"N={n:4d} {variant:<22} J={row['J']:.6f} "
                  f"CA={row['CA']:.5f} frag={row['Frag']:.6f} "
                  f"downloaded={row['downloaded_MB']:.1f}MB",flush=True)
    a.out_dir.mkdir(parents=True,exist_ok=True)
    write(a.out_dir/"affinity_by_scale.csv",rows)
    lookup={(r["requests"],r["variant"]):r for r in rows}
    for variant in list(WEIGHTS)[1:]:
        x=[lookup[n,"fig8_archived"] for n in a.sizes]
        y=[lookup[n,variant] for n in a.sizes]
        print(f"COMPARE {variant}: mean_delta_J={mean(a['J']-b['J'] for a,b in zip(y,x)):+.6f} "
              f"placements_changed={sum(a['placement']!=b['placement'] for a,b in zip(y,x))}/{len(x)} "
              f"orders_changed={sum(a['ordering']!=b['ordering'] for a,b in zip(y,x))}/{len(x)}")
    (a.out_dir/"provenance.json").write_text(json.dumps({
        "experiment":"same Fig.8 workload and non-weight CLI controls across paired weight sets",
        "original":ORIGINAL,"dco_pgdsf":DCO_PGDSF,
        "variants":WEIGHTS,"algorithm":a.algorithm,
        "warning":"The four paper-declared weights were never established as the actual archived Fig.8 execution coefficients. "
                  "Current-state normalization in FGP is NOT the global-mean posthoc diagnostic normalization. "
                  "PYTHONHASHSEED can change tie breaking, keep it fixed for the matched comparison. "
                  "Even equal mean J does NOT prove equal placements or policy semantics.",
    },ensure_ascii=False,indent=2),encoding="utf-8")
    print("Saved",a.out_dir)
if __name__=="__main__":main()
