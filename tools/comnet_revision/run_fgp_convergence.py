#!/usr/bin/env python3
"""Reviewer R2.7: bounded FGP refinement / convergence behaviour.

Runs only Phase I with the manuscript default parameters. Reports the initial
potential, accepted move/swap operations, final potential, stopping label and
wall-clock. It does NOT claim Nash convergence: a capped/local neighborhood
search may stop without reaching a Nash equilibrium.
"""
import argparse, csv, importlib.util, json, sys, time
from pathlib import Path

def load(path):
    spec=importlib.util.spec_from_file_location("falcons_fg_convergence",path)
    mod=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=mod
    spec.loader.exec_module(mod)
    return mod

def run_case(mod, case, cap):
    containers,nodes,sizes=mod.load_case(str(case))
    s=mod.FGDscrScheduler(
        layer_sizes_mb=sizes,
        beam_width=1,
        hard_resource_filter=True,
        init_order="image_resource",
        lambda_cong=1.0,
        lambda_frag=0.1,
        lambda_aff=0.2,
        lambda_task_load=0.2,
        alpha1_reuse=1.0,
        alpha2_future=0.15,
        alpha3_pull=50.0,
        alpha4_evict=0.02,
        beta_slot=2.0,
        rho_centrality=0.2,
        kappa_overshoot=0.5,
        k_pin=6,
        unit_mb=50,
        max_best_response_rounds=cap,
        phase1_neighbor_mode="move_swap",
        move_topk_per_node=8,
        swap_topk_per_node=6,
        cache_policy="pgdsf",
        order_policy="dynamic_state",
    )
    s.set_data(containers,nodes)
    t0=time.perf_counter()
    assignment=s.best_response_assignment()
    wall=time.perf_counter()-t0
    hist=s.phase1_history
    accepted=[h for h in hist[1:] if str(h.get("label","")).startswith(("move_","swap_"))]
    initial=float(hist[0]["potential"]) if hist else float("nan")
    final=float(hist[-1]["potential"]) if hist else float("nan")
    stop=hist[-1]["label"] if hist else "missing"
    return {
        "case":str(case),
        "requests":len(containers),
        "nodes":len(nodes),
        "round_cap":cap,
        "edge_admitted":len(assignment),
        "cloud_fallback":len(getattr(s,"failed_container_ids",[])),
        "accepted_ops":len(accepted),
        "history_states":len(hist),
        "stop_label":stop,
        "initial_potential":initial,
        "final_potential":final,
        "relative_potential_drop":(initial-final)/initial if initial else 0.0,
        "phase1_wall_ms":wall*1000.0,
        "history":[{"cycle":h["cycle"],"label":h["label"],"potential":h["potential"]} for h in hist],
    }

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--cases",nargs="+",required=True)
    ap.add_argument("--round-caps",nargs="+",type=int,default=[1,3,5,10,20])
    ap.add_argument("--algorithm",default="scripts/fg_dscr_incremental.py")
    ap.add_argument("--out-dir",default="results/comnet_revision/convergence")
    args=ap.parse_args()
    mod=load(args.algorithm)
    out=Path(args.out_dir); out.mkdir(parents=True,exist_ok=True)
    rows=[]; details=[]
    for cp in args.cases:
        case=Path(cp)
        for cap in args.round_caps:
            r=run_case(mod,case,cap)
            details.append(r)
            row={k:v for k,v in r.items() if k!="history"}
            rows.append(row)
            print(f"req={r['requests']} cap={cap} accepted={r['accepted_ops']} "
                  f"stop={r['stop_label']} phi={r['final_potential']:.6g} "
                  f"time={r['phase1_wall_ms']:.2f}ms",flush=True)
    with (out/"convergence_summary.csv").open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    (out/"convergence_history.json").write_text(json.dumps(details,ensure_ascii=False,indent=2),encoding="utf-8")
    print("Saved",out)

if __name__=="__main__":
    main()
