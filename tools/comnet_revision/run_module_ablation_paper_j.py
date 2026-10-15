#!/usr/bin/env python3
"""COMNET R2.5: formally evaluate FGP/DCO 2x2 module ablation using manuscript J.

Do not compare a different run profile to historical Fig. 8. All four variants
use precisely the same inputs and cache replacement (PGDSF). No legacy ACT/AMS
objective enters any comparison. All-request cloud completions follow the
original Fig. 8 deterministic edge-quantile + 300s provisioning model.

Variants:
    full             greedy + bounded FGP refinement + DCO ordering
    no_fgp_refine    greedy placement ONLY             + DCO ordering
    no_dco           greedy + bounded FGP refinement + arrival order
    no_both          greedy placement ONLY             + arrival order

Therefore FGP ablation isolates *best-response refinement*, not all placement.
This experiment does not implement true standard LRU/LFU/GDSF cache comparators.
"""
from __future__ import annotations
import argparse
import csv
import json
from pathlib import Path
from statistics import mean
from run_formal_parameter_audit import PROFILES, modload, run_one, fingerprint, REF_D, REF_CA, REF_FRAG

VARIANTS = {
    "full": {},
    "no_fgp_refine": {"max_best_response_rounds": 0},
    "no_dco": {"_input_order_queue": True},
    "no_both": {"max_best_response_rounds": 0, "_input_order_queue": True},
    "no_future_share": {"disable_future_share": True},
}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--profile", choices=list(PROFILES), default="paper_declared")
    ap.add_argument("--algorithm", default="scripts/fg_dscr_incremental.py")
    ap.add_argument("--case-dir", default="cases/drtp_cache_only_sweep_88")
    ap.add_argument("--out-dir", default="results/comnet_revision/ablation_paper_j")
    ap.add_argument("--provision-sec", type=float, default=300.)
    ap.add_argument("--historical-fig8", action="store_true",
                    help="Use Fig.8 original move-only, move-topK=6 configuration. "
                         "Requires --profile archived_candidate and prefix cases.")
    ap.add_argument("--mainline-results",default="results/drtp/k8s_same_scale/fig5_overall_prefix",
                    help="Existing Fig8 raw scheduler outputs for verifying Full parity.")
    args=ap.parse_args()
    out=Path(args.out_dir);out.mkdir(parents=True,exist_ok=True)
    if args.historical_fig8 and args.profile!="archived_candidate":
        raise ValueError("--historical-fig8 must use --profile archived_candidate")
    if args.historical_fig8 and not args.case_dir.endswith("_prefix"):
        raise ValueError("--historical-fig8 must use original _prefix workload")
    mod=modload(args.algorithm)
    rows=[];settings={};mainline_checks=[]
    historical={
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
    } if args.historical_fig8 else {}
    scales=list(range(200,1001,100))
    for n in scales:
        path=Path(args.case_dir)/f"drtp_img88_cacheonly_1024mb_{n}.json"
        if not path.is_file():
            raise FileNotFoundError(path)
        for variant,overrides in VARIANTS.items():
            effective_overrides={**historical,**overrides}
            metrics,elapsed,parameters,result=run_one(
                mod,path,args.profile,effective_overrides,args.provision_sec
            )
            if args.historical_fig8 and variant=="full":
                reference=Path(args.mainline_results)/f"fg_prefix_req{n}.json"
                if not reference.is_file():
                    raise FileNotFoundError(f"Missing original Fig8 result: {reference}")
                original=json.loads(reference.read_text(encoding="utf-8"))
                reference_assignment=fingerprint(original["assignment"])
                reference_order=fingerprint(original["ordered_queues"])
                check={
                    "requests":n,
                    "placement_parity":int(metrics["assignment_signature"]==reference_assignment),
                    "order_parity":int(metrics["ordering_signature"]==reference_order),
                    "recomputed_J":metrics["J"],
                    "paper_result_J":None,
                    "download_difference_MB":metrics["downloaded_mb"]-
                        float(original["summary"]["downloaded_mb"]),
                }
                mainline_checks.append(check)
                print(f"[MAINLINE] req={n} placement={check['placement_parity']} "
                      f"order={check['order_parity']} "
                      f"DLdiff={check['download_difference_MB']:+.3f}MB",flush=True)
            settings[variant]=parameters
            rows.append({
                "profile":args.profile,
                "variant":variant,
                "requests":n,
                **metrics,
                "total_algorithm_wall_ms":elapsed,
            })
            print(
                f"N={n:4d} {variant:<15} "
                f"J={metrics['J']:.6f} D={metrics['Delay_raw']:.3f} "
                f"CA={metrics['CA_rate']:.4f} Frag={metrics['frag_score']:.6f} "
                f"DL={metrics['downloaded_mb']:.0f}MB "
                f"FGP_rounds={overrides.get('max_best_response_rounds',5)}",
                flush=True,
            )
    index={(r["variant"],r["requests"]):r for r in rows}
    for n in scales:
        # Ordering cannot change the placement phase; these equalities are
        # essential controls, rather than tests that results must improve.
        if index["full",n]["assignment_signature"]!=index["no_dco",n]["assignment_signature"]:
            raise AssertionError(f"N={n}: no_dco unintentionally changed placement")
        if index["no_fgp_refine",n]["assignment_signature"]!=index["no_both",n]["assignment_signature"]:
            raise AssertionError(f"N={n}: no_both unintentionally changed greedy placement")
    dest=out/"module_ablation_by_scale.csv"
    with dest.open("w",newline="",encoding="utf-8") as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    if mainline_checks:
        with (out/"original_fig8_parity.csv").open("w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=list(mainline_checks[0]))
            w.writeheader();w.writerows(mainline_checks)
        failures=[r for r in mainline_checks if not (
            r["placement_parity"] and r["order_parity"]
            and abs(r["download_difference_MB"])<1e-6
        )]
        if failures:
            raise AssertionError(
                f"Full FALCONS does not reproduce original Fig8 at "
                f"{[r['requests'] for r in failures]}; "
                "see original_fig8_parity.csv; don't treat ablations as formal yet."
            )
    summaries=[]
    base={n:index["full",n] for n in scales}
    for variant in VARIANTS:
        subset=[index[variant,n] for n in scales]
        summaries.append({
            "variant":variant,
            "mean_J":mean(r["J"] for r in subset),
            "mean_D":mean(r["Delay_raw"] for r in subset),
            "mean_CA":mean(r["CA_rate"] for r in subset),
            "mean_Frag":mean(r["frag_score"] for r in subset),
            "mean_downloaded_MB":mean(r["downloaded_mb"] for r in subset),
            "mean_wall_ms":mean(r["total_algorithm_wall_ms"] for r in subset),
            "mean_J_minus_full":mean(index[variant,n]["J"]-base[n]["J"] for n in scales),
            "changed_placements":sum(index[variant,n]["assignment_signature"]!=base[n]["assignment_signature"] for n in scales),
            "changed_orders":sum(index[variant,n]["ordering_signature"]!=base[n]["ordering_signature"] for n in scales),
            "changed_execution_traces":sum(index[variant,n]["cache_trace_signature"]!=base[n]["cache_trace_signature"] for n in scales),
        })
    summ=out/"module_ablation_summary.csv"
    with summ.open("w",newline="",encoding="utf-8") as f:
        writer=csv.DictWriter(f,fieldnames=list(summaries[0]));writer.writeheader();writer.writerows(summaries)
    provenance={
        "manuscript_comment":"R2.5",
        "experiment":"2x2 FGP refinement x DCO ordering, plus DCO FutureShare ablation with true case-sequence no-DCO comparator",
        "profile":args.profile,
        "algorithm":args.algorithm,
        "historical_fig8":args.historical_fig8,
        "original_mainline_parity_file":"original_fig8_parity.csv" if mainline_checks else None,
        "cases":[str(Path(args.case_dir)/f"drtp_img88_cacheonly_1024mb_{n}.json") for n in scales],
        "J_formula":"J = (.5*all_mean+.5*all_p95)/D_ref + cloud_fraction/CA_ref + fragmentation/Frag_ref",
        "references":{"D_ref":REF_D,"CA_ref":REF_CA,"Frag_ref":REF_FRAG},
        "cloud_provision_s":args.provision_sec,
        "parameters_by_variant":settings,
        "limitations":[
            "If historical_fig8: Full has been checked for exact assignment, ordering, and downloads against archived Fig.8 output.",
            "If paper_declared: this profile produces a different J from archived Fig.8 and cannot be mixed with it.",
            "No FGP means no best-response refinement; greedy initialization stays in place for valid node assignment.",
            "No DCO means case input sequence (FCFS proxy), while PGDSF cache policy and queue simulation are unchanged.",
            "Cache replacement comparisons against standard LRU/LFU/GDSF are NOT provided by this experiment.",
            "Cloud times are simulator-model quantile replay, not real Kubernetes latency.",
            "Nine request-size scenarios are not nine independent random seeds."
        ]
    }
    (out/"provenance.json").write_text(json.dumps(provenance,indent=2,ensure_ascii=False),encoding="utf-8")
    print("\n=== R2.5 summary (comparable only WITHIN the selected profile) ===")
    for r in summaries:
        print(f"{r['variant']:<17} J={r['mean_J']:.6f} "
              f"delta_vs_full={r['mean_J_minus_full']:+.6f} "
              f"changed_placement={r['changed_placements']}/9 "
              f"changed_order={r['changed_orders']}/9")
    print("Saved:",dest,summ)

if __name__ == "__main__":
    main()
