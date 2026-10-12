#!/usr/bin/env python3
"""Export FALCONS submission's existing all-request metrics (R2.2).

No simulation is run. Source table is the experiment behind final Fig. 8
(legacy generated directory called fig5). Uses article Eq. (8), not old Obj.
The source "CA" times were reconstructed by an assumed provisioning/quantile
replay, NOT physically measured Kubernetes startup times.
"""
import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path

COLUMNS = [
    "requests","method","n_total","edge_scheduled","CA","CA_rate",
    "ACT_edge_summary","AMS_edge_summary","ACT_all","P95_all",
    "CMS_all","Delay_all","Delay_all_norm","CA_norm","frag_score",
    "Frag_norm","Obj_Final","downloaded_mb","reuse_rate","missing_edge_time"
]
METHODS = ["FG-DSCR-GC","ILR-SA","LRScheduler","GAHRL","ORR","LASA"]

def table_rows(text):
    lines = text.splitlines()
    marker = "## Fig.5 All-request Detail"
    if marker not in text:
        raise ValueError("Expected final all-request detail section missing")
    start = text.index(marker)
    section = text[start:]
    rows = []
    cols = None
    for line in section.splitlines():
        if not line.startswith("|"):
            if cols and rows:
                break
            continue
        cells = [x.strip() for x in line.strip().strip("|").split("|")]
        if cells and cells[0] == "requests":
            cols = cells
            if cols != COLUMNS:
                raise ValueError("Source columns changed: "+repr(cols))
            continue
        if cols is None or cells[0].startswith("---"):
            continue
        if len(cells) != len(cols):
            raise ValueError("Truncated data row: "+line)
        row = {}
        for k,v in zip(cols,cells):
            row[k] = v if k=="method" else float(v)
        rows.append(row)
    return rows

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--source",default="results/drtp/k8s_same_scale/tables_allrequest_fig5_edgeca300_prefix/fig5_allrequest_detail.md")
    ap.add_argument("--out-dir",default="results/comnet_revision/raw_metrics")
    ap.add_argument("--d-ref",type=float,default=440.7696)
    ap.add_argument("--ca-ref",type=float,default=0.77924)
    ap.add_argument("--frag-ref",type=float,default=0.180069)
    args=ap.parse_args()
    for k in ("d_ref","ca_ref","frag_ref"):
        if getattr(args,k)<=0:
            ap.error("--"+k.replace("_","-")+" must be positive")
    source=Path(args.source)
    rows=table_rows(source.read_text(encoding="utf-8"))
    if not rows:
        raise ValueError("No source data")
    seen=set()
    for row in rows:
        key=(int(row["requests"]),row["method"])
        if key in seen:
            raise ValueError("Duplicate request/method: "+repr(key))
        seen.add(key)
        if row["method"] not in METHODS:
            raise ValueError("Unexpected method "+row["method"])
        if not math.isclose(row["CA"]+row["edge_scheduled"],row["n_total"],abs_tol=0.01):
            raise ValueError("Edge/CA count mismatch "+str(key))
        if not math.isclose(row["CA"]/row["n_total"],row["CA_rate"],abs_tol=0.00015):
            raise ValueError("CA rate mismatch "+str(key))
        if not math.isclose(.5*(row["ACT_all"]+row["P95_all"]),row["Delay_all"],abs_tol=0.002):
            raise ValueError("All-request delay mismatch "+str(key))
        d=row["Delay_all"]/args.d_ref
        ca=row["CA_rate"]/args.ca_ref
        frag=row["frag_score"]/args.frag_ref
        j=d+ca+frag
        if abs(j-row["Obj_Final"])>0.006:
            raise ValueError(f"Submission J reference mismatch {key}: {j} vs {row['Obj_Final']}")
        if row["missing_edge_time"]>0:
            raise ValueError(f"Missing edge completion time for {key}")
        row["D_norm_exact_from_rounded_input"]=d
        row["CA_norm_exact_from_rounded_input"]=ca
        row["Frag_norm_exact_from_rounded_input"]=frag
        row["J_recomputed_from_rounded_input"]=j
    scales=sorted({int(x["requests"]) for x in rows})
    for scale in scales:
        missing=set(METHODS)-{x["method"] for x in rows if x["requests"]==scale}
        if missing:
            raise ValueError(f"Scale {scale} missing methods {sorted(missing)}")

    out=Path(args.out_dir)
    out.mkdir(parents=True,exist_ok=True)
    with (out/"raw_allrequest_by_method_scale.csv").open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    averages=[]
    for method in METHODS:
        subset=[x for x in rows if x["method"]==method]
        row={"method":method,"scenarios":len(subset)}
        for col in ("ACT_all","P95_all","CA_rate","frag_score","downloaded_mb",
                    "Delay_all_norm","CA_norm","Frag_norm","Obj_Final"):
            row[col]=sum(x[col] for x in subset)/len(subset)
        averages.append(row)
    with (out/"raw_allrequest_summary.csv").open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(averages[0]))
        w.writeheader()
        w.writerows(averages)
    provenance={
        "reviewer_comments":["R2.2","R1.5"],
        "source_path":str(source),
        "source_type":"existing final manuscript source tables; not a newly sampled trial",
        "source_fig":"submission Fig. 8; original generated table named Fig. 5",
        "metric":"J = (0.5 * all_request_mean + 0.5 * all_request_P95) / D_ref + CA_rate / CA_ref + Frag / Frag_ref",
        "reference_values":{"D_ref":args.d_ref,"CA_ref":args.ca_ref,"Frag_ref":args.frag_ref},
        "limitations":[
            "Source results are single scenario point estimates; no independent-seed error bars.",
            "All-request cloud fallback completion times rely on model-based provisioning and deterministic quantile mapping of edge times; they are not physical Kubernetes samples.",
            "Downloaded MB follows source table accounting and may not include off-cluster/cloud pulls.",
            "No retuning or post-hoc normalization to make J look better."
        ],
        "scales":scales,"methods":METHODS,"rows":len(rows)
    }
    (out/"source_and_formula.json").write_text(json.dumps(provenance,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"Validated {len(rows)} submission-derived records, scales={scales}")
    print("method           mean_T_all   P95_all   CA_rate   Frag       DL_MB      J")
    for r in averages:
        print(f"{r['method']:<16} {r['ACT_all']:>10.3f} {r['P95_all']:>9.3f} "
              f"{r['CA_rate']:>8.4f} {r['frag_score']:>9.6f} "
              f"{r['downloaded_mb']:>10.1f} {r['Obj_Final']:>7.3f}")
    print("Saved:",out)

if __name__=="__main__":
    main()
