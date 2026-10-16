#!/usr/bin/env python3
"""Read-only manuscript baseline fairness audit: saved edge completion times.

Compare the article's archived all-request J against ONE uniform re-evaluation
using saved node-level/individual completion timestamps for ALL SIX METHODS.
No placement/queue/cache result is recomputed and no submitted table is mutated.
"""
from __future__ import annotations
import argparse,csv,json,statistics
from pathlib import Path
from run_concurrent_replay import METHODS,score,serial_times

def save(path,rows):
    with path.open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]))
        w.writeheader();w.writerows(rows)

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--case-dir",type=Path,default=Path("cases/drtp_cache_only_sweep_88_prefix"))
    p.add_argument("--saved-result-dir",type=Path,default=Path("results/drtp/k8s_same_scale/fig5_overall_prefix"))
    p.add_argument("--paper",type=Path,default=Path("results/comnet_revision/raw_metrics/raw_allrequest_by_method_scale.csv"))
    p.add_argument("--out-dir",type=Path,default=Path("results/comnet_revision/baseline_uniform_latency_audit"))
    p.add_argument("--sizes",nargs="+",type=int,default=list(range(200,1001,100)))
    a=p.parse_args()
    with a.paper.open(newline="",encoding="utf-8") as f:
        paper={(int(float(r["requests"])),r["method"]):r for r in csv.DictReader(f)}
    rows=[]
    for n in a.sizes:
        case=json.loads((a.case_dir/f"drtp_img88_cacheonly_1024mb_{n}.json").read_text(encoding="utf-8"))
        for key,name in METHODS.items():
            src=a.saved_result_dir/f"{key}_prefix_req{n}.json"
            saved=json.loads(src.read_text(encoding="utf-8"))
            times=serial_times(saved)
            metrics=score(case,saved,times,300.)
            orig=paper[n,name]
            j0=float(orig["Obj_Final"]);j1=metrics["J"]
            row=dict(requests=n,method=name,edge_timestamp_count=len(times),
                     article_J=j0,uniform_saved_J=j1,delta_J=j1-j0,
                     article_ACT_all=float(orig["ACT_all"]),
                     uniform_ACT_all=metrics["all_request_mean"],
                     article_P95_all=float(orig["P95_all"]),
                     uniform_P95_all=metrics["P95_all"],
                     article_CA=float(orig["CA_rate"]),
                     uniform_CA=metrics["CA_rate"],
                     article_downloaded_MB=float(orig["downloaded_mb"]),
                     saved_downloaded_MB=metrics["downloaded_mb"])
            rows.append(row)
            if abs(row["article_CA"]-row["uniform_CA"])>0.001:
                raise AssertionError(f"Admission mismatch {n} {name}")
            print(f"N={n:4d} {name:<14} articleJ={j0:.5f} "
                  f"uniformSavedJ={j1:.5f} DeltaJ={j1-j0:+.5f}",flush=True)
    a.out_dir.mkdir(parents=True,exist_ok=True)
    save(a.out_dir/"uniform_saved_latency_by_scale.csv",rows)
    summary=[]
    for name in METHODS.values():
        subset=[r for r in rows if r["method"]==name]
        summary.append(dict(method=name,scales=len(subset),
                            mean_article_J=statistics.mean(r["article_J"] for r in subset),
                            mean_uniform_saved_J=statistics.mean(r["uniform_saved_J"] for r in subset),
                            mean_delta_J=statistics.mean(r["delta_J"] for r in subset),
                            max_abs_delta_J=max(abs(r["delta_J"]) for r in subset),
                            changed_scales=sum(abs(r["delta_J"])>.005 for r in subset)))
    save(a.out_dir/"uniform_saved_latency_summary.csv",summary)
    (a.out_dir/"provenance.json").write_text(json.dumps({
        "classification":"READ-ONLY audit; NOT a replacement for published results",
        "source_paper":str(a.paper),"source_results":str(a.saved_result_dir),
        "same_definition_for_all_methods":"Saved edge request completion timestamp, plus edge-quantile 300s cloud fallback",
        "warning":"Original submitted evaluations may have used a different fallback estimator for methods without container_metrics; "
                  "compare and disclose differences across ALL methods before updating manuscript claims. "
                  "Physical Kubernetes times were NOT measured.",
    },indent=2,ensure_ascii=False),encoding="utf-8")
    print("\n=== Uniform saved timestamp vs article J, nine scales ===")
    for r in summary:
        print(f"{r['method']:<15} articleJ={r['mean_article_J']:.5f} "
              f"uniformJ={r['mean_uniform_saved_J']:.5f} "
              f"delta={r['mean_delta_J']:+.5f} changed={r['changed_scales']}/{r['scales']}")
    print("Saved",a.out_dir)

if __name__=="__main__":main()
