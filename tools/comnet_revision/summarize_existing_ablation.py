#!/usr/bin/env python3
"""Summarize pre-existing 45-run FALCONS R2.5 ablation without rerunning jobs.

Original runner intentionally rejects mismatch against Fig.8 at hash-unstable
scales. This script preserves that failure, writes a separate WITHIN-RUN_ONLY
summary and never calls the results 'historically exact'.
"""
from __future__ import annotations
import argparse,csv,json
from pathlib import Path
from statistics import mean

VARIANTS=("full","no_fgp_refine","no_dco","no_both","no_future_share")
SCALES=tuple(range(200,1001,100))

def read(path):
    with path.open(newline="",encoding="utf-8") as f:
        return list(csv.DictReader(f))

def write(path,rows):
    with path.open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--out-dir",type=Path,default=Path("results/comnet_revision/ablation_fig8_exact"))
    args=p.parse_args()
    root=args.out_dir
    src=root/"module_ablation_by_scale.csv"
    if not src.exists():
        raise SystemExit("Missing "+str(src)+" -- the original runner must have completed the 45 jobs first.")
    rows=read(src)
    idx={(r["variant"],int(float(r["requests"]))):r for r in rows}
    wanted={(v,n) for v in VARIANTS for n in SCALES}
    if len(rows)!=45 or set(idx)!=wanted:
        raise SystemExit(f"Expected exactly 45 records; actual={len(rows)} distinct={len(idx)}, missing={sorted(wanted-set(idx))}")
    for n in SCALES:
        if idx["full",n]["assignment_signature"]!=idx["no_dco",n]["assignment_signature"]:
            raise RuntimeError(f"N={n}: no_dco changed placement; invalid ablation")
        if idx["no_fgp_refine",n]["assignment_signature"]!=idx["no_both",n]["assignment_signature"]:
            raise RuntimeError(f"N={n}: no_both changed greedy placement; invalid ablation")
    parity_file=root/"original_fig8_parity.csv"
    if not parity_file.exists():
        raise SystemExit("Missing historical parity CSV, cannot document archival mismatches")
    parity=read(parity_file)
    failures=[r for r in parity if (
        int(float(r["placement_parity"]))!=1
        or int(float(r["order_parity"]))!=1
        or abs(float(r["download_difference_MB"]))>1e-6)]
    summ=[]
    for v in VARIANTS:
        subset=[idx[v,n] for n in SCALES]
        summ.append({
            "variant":v,
            "mean_J":mean(float(r["J"]) for r in subset),
            "mean_D_s":mean(float(r["Delay_raw"]) for r in subset),
            "mean_CA":mean(float(r["CA_rate"]) for r in subset),
            "mean_Frag":mean(float(r["frag_score"]) for r in subset),
            "mean_downloaded_MB":mean(float(r["downloaded_mb"]) for r in subset),
            "mean_wall_ms":mean(float(r["total_algorithm_wall_ms"]) for r in subset),
            "mean_J_minus_full":mean(float(idx[v,n]["J"])-float(idx["full",n]["J"]) for n in SCALES),
            "changed_placements":sum(idx[v,n]["assignment_signature"]!=idx["full",n]["assignment_signature"] for n in SCALES),
            "changed_orders":sum(idx[v,n]["ordering_signature"]!=idx["full",n]["ordering_signature"] for n in SCALES),
            "historical_fig8_exact_parity":int(not failures),
            "scope":"controlled_within_run_not_archived_exact",
        })
    target=root/"module_ablation_summary_WITHIN_RUN_ONLY.csv"
    write(target,summ)
    meta={
        "classification":"WITHIN-RUN controlled ablation; NOT historically exact",
        "raw_rows":str(src),"archived_parity":str(parity_file),
        "hashseed":"Original ablation jobs' PYTHONHASHSEED from shell environment; unknown if not recorded",
        "historical_mismatch_sizes":[int(float(r["requests"])) for r in failures],
        "warning":"These mean values are paired across ablation variants WITHIN each request-scale experiment. "
                  "The archived Fig.8 values must not be replaced by this new run without explaining the hash-dependent cache/ordering behavior. "
                  "Nine scales are NOT independent replicate seeds.",
    }
    (root/"module_ablation_summary_WITHIN_RUN_ONLY.json").write_text(
        json.dumps(meta,ensure_ascii=False,indent=2),encoding="utf-8")
    print("Historical mismatch request sizes:",meta["historical_mismatch_sizes"])
    for r in summ:
        print(f"{r['variant']:<16} mean J={r['mean_J']:.6f} ΔJ={r['mean_J_minus_full']:+.6f} "
              f"downloaded={r['mean_downloaded_MB']:.1f}MB")
    print("Saved:",target)
    print("IMPORTANT: controlled within-run comparison only, not exact archival parity.")

if __name__=="__main__":
    main()
