#!/usr/bin/env python3
"""Read-only provenance diagnosis for ILR-SA Fig.8 and concurrent-replay mismatch.

Compares (a) the submitted table's old fallback estimator (full image size /
node bandwidth, cumulative in assignment order) with (b) the explicit completion
timestamps already saved in node_details. No experiment is rerun or rewritten.
DO NOT interpret fallback-only values as observed container completion times.
"""
from __future__ import annotations
import argparse
import csv
import json
from pathlib import Path
from statistics import mean

from run_concurrent_replay import serial_times, score


def fallback_from_paper(case, result):
    nodes = {str(n["eid"]): n for n in case["nodes"]}
    containers = {str(c["cid"]): c for c in case["containers"]}
    sizes = case["layer_sizes_mb"]
    all_sizes = [sum(float(sizes.get(str(l), 0)) for l in c.get("layers", []))
                 for c in case["containers"]]
    positives = [x for x in all_sizes if x > 0]
    fallback_mean = mean(positives) if positives else 0.0
    ans = {}
    for eid, cids in result["assignment"].items():
        eid = str(eid)
        bw = float(nodes[eid].get("bandwidth_mb_s", 100.0))
        if bw <= 0:
            bw = 100.0
        clock = 0.0
        for cid in cids:
            cid = str(cid)
            c = containers[cid]
            image_size = sum(float(sizes.get(str(l), 0.0)) for l in c.get("layers", []))
            if image_size <= 0:
                image_size = next((float(c[k]) for k in (
                    "image_size_mb","size_mb","total_size_mb","image_mb"
                ) if float(c.get(k, 0) or 0) > 0), fallback_mean)
            clock += image_size / bw
            ans[cid] = clock
    return ans


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--case-dir",type=Path,default=Path("cases/drtp_cache_only_sweep_88_prefix"))
    p.add_argument("--result-dir",type=Path,default=Path("results/drtp/k8s_same_scale/fig5_overall_prefix"))
    p.add_argument("--paper",type=Path,default=Path("results/comnet_revision/raw_metrics/raw_allrequest_by_method_scale.csv"))
    p.add_argument("--out-dir",type=Path,default=Path("results/comnet_revision/ilrsa_semantics_audit"))
    p.add_argument("--sizes",nargs="+",type=int,default=list(range(200,1001,100)))
    args=p.parse_args()
    with args.paper.open(newline="",encoding="utf-8") as f:
        paper={int(float(r["requests"])):r for r in csv.DictReader(f) if r["method"]=="ILR-SA"}
    rows=[]
    for n in args.sizes:
        case=json.loads((args.case_dir/f"drtp_img88_cacheonly_1024mb_{n}.json").read_text())
        result=json.loads((args.result_dir/f"ilrsa_prefix_req{n}.json").read_text())
        old=fallback_from_paper(case,result)
        saved=serial_times(result)
        if set(old)!=set(saved):
            raise ValueError(f"N={n} fallback set differs from saved completion time set: {len(old)} vs {len(saved)}")
        a=score(case,result,old,300.)
        b=score(case,result,saved,300.)
        pr=paper[n]
        row=dict(requests=n,edge_count=len(saved),
                 differing_completion_times=sum(abs(old[k]-saved[k])>0.01 for k in saved),
                 paper_J=float(pr["Obj_Final"]),paper_ACT_all=float(pr["ACT_all"]),
                 paper_P95_all=float(pr["P95_all"]),
                 fallback_J=a["J"],saved_timestamp_J=b["J"],
                 fallback_ACT_all=a["all_request_mean"],saved_timestamp_ACT_all=b["all_request_mean"],
                 fallback_P95_all=a["P95_all"],saved_timestamp_P95_all=b["P95_all"],
                 fallback_minus_paper_J=a["J"]-float(pr["Obj_Final"]),
                 saved_minus_paper_J=b["J"]-float(pr["Obj_Final"]),
                 saved_minus_fallback_J=b["J"]-a["J"])
        rows.append(row)
        print(f"N={n:4d} article={row['paper_J']:.5f} "
              f"historical_fallback={row['fallback_J']:.5f} "
              f"saved_node_times={row['saved_timestamp_J']:.5f} "
              f"changed_edge_times={row['differing_completion_times']}/{row['edge_count']}",flush=True)
    args.out_dir.mkdir(parents=True,exist_ok=True)
    out=args.out_dir/"ilrsa_completion_semantics.csv"
    with out.open("w",newline="",encoding="utf-8") as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    print("MEAN J:",{k:round(mean(r[k] for r in rows),5)
          for k in ("paper_J","fallback_J","saved_timestamp_J")})
    print("READ-ONLY diagnosis saved:",out)
    print("NOTE: paper fallback is an IMAGE-SIZE/BANDWIDTH ESTIMATOR and is not an observed latency.")
    print("DO NOT mix a saved-timestamp ILR-SA concurrency result with a fallback-based historical J.")

if __name__=="__main__":
    main()
