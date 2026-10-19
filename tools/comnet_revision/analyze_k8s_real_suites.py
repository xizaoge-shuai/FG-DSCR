#!/usr/bin/env python3
"""Audit and aggregate actual FALCONS real-OCI paired K8s experiment suites.

Usage:
 python3 tools/comnet_revision/analyze_k8s_real_suites.py
 python3 tools/comnet_revision/analyze_k8s_real_suites.py --suite-dir .../suite-2026...

Reads EACH saved trials.csv and each matching run folder, checks workload
fingerprints (image digest/layers, requests, runtime) identical across methods,
then computes paired per-trial ACT/P95/D differences and t-based 95% CIs
on INDEPENDENT TRIAL differences. n=1 -> CI blank.
Does not mix warm/cold, mix work, or invent OCI download measurement.
"""
import argparse,csv,hashlib,json,math,pathlib,re,statistics,sys
from collections import defaultdict
from export_k8s_real_paper_metrics import collect,load,pctl,t95

METRICS=("ACT_all_real_s","P95_all_real_s","D_real_s","batch_wall_s")
BASELINES=("fgp_fcfs","ilrsa","default")

def csv_write(path,rows):
    if not rows:return
    with path.open("w",newline="",encoding="utf-8") as f:
        keys=list(rows[0])
        w=csv.DictWriter(f,fieldnames=keys,extrasaction="ignore")
        w.writeheader();w.writerows(rows)

def check_workload(p):
    obj=load(p)
    # Workload comparisons exclude algorithm-specific placement/queue decisions.
    canonical={"containers":sorted(obj["containers"],key=lambda x:x["cid"]),
               "layer_sizes_mb":obj["layer_sizes_mb"]}
    return hashlib.sha256(json.dumps(canonical,sort_keys=True,separators=(",",":")).encode()).hexdigest()

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--suite-root",default="results/comnet_revision/k8s_real_testbed/multiimage_suite")
    ap.add_argument("--suite-dir",action="append",default=[])
    ap.add_argument("--catalog",default="results/comnet_revision/k8s_real_testbed/oci_v2_12/catalog.json")
    ap.add_argument("--out-dir",default="results/comnet_revision/k8s_real_testbed/multiimage_analysis")
    a=ap.parse_args()
    catalog=load(pathlib.Path(a.catalog))
    if catalog.get("errors"):ap.error("catalog contains errors")
    suites=[pathlib.Path(x) for x in a.suite_dir] if a.suite_dir else sorted(
        pathlib.Path(a.suite_root).glob("suite-*"))
    out=pathlib.Path(a.out_dir);out.mkdir(parents=True,exist_ok=True)
    merged=[];comparisons=[];notes=[];summary=[]
    for suite in suites:
        trialsfile=suite/"trials.csv"
        if not trialsfile.exists():continue
        runs=suite/"runs"
        if not runs.exists():
            notes.append({"suite":str(suite),"problem":"missing runs folder"});continue
        metrics,_=collect(runs,catalog)
        byrun={x["run_id"]:x for x in metrics}
        trialmap=defaultdict(dict)
        with trialsfile.open(newline="",encoding="utf-8") as f:
            for tr in csv.DictReader(f):
                log=pathlib.Path(tr["log_path"])
                if not log.exists():
                    log=suite/log.name
                if not log.exists():
                    notes.append({"suite":str(suite),"problem":"missing log "+tr["log_path"]})
                    continue
                logbody=log.read_text(encoding="utf-8",errors="replace")
                matches=re.findall(r"^RUN (oci-[a-f0-9]+)\b",logbody,re.M)
                if len(matches)!=1:
                    notes.append({"suite":str(suite),"problem":f"log RUN markers count {len(matches)} {log}"});continue
                rid=matches[0]
                if rid not in byrun:
                    notes.append({"suite":str(suite),"problem":"missing metrics "+rid});continue
                r=byrun[rid];folder=pathlib.Path(r["folder"])
                fp=check_workload(folder/"workload.json")
                key=(int(tr["requests"]),int(tr["image_count"]),int(tr["trial"]),int(tr["seed"]))
                mode=tr["mode"]
                if mode in trialmap[key]:
                    notes.append({"suite":str(suite),"problem":f"duplicate mode {key} {mode}"})
                entry={
                    "suite":suite.name,"stage":tr["stage"],
                    "requests":int(tr["requests"]),"image_count":int(tr["image_count"]),
                    "trial":int(tr["trial"]),"seed":int(tr["seed"]),
                    "mode":mode,"run_id":rid,"exit_code":int(tr["exit_code"]),
                    "successes":r["successes"],"workload_sha256":fp,
                    "effective_images_used":r["effective_images_used"],
                    "ACT_all_real_s":r["ACT_all_real_s"],
                    "P95_all_real_s":r["P95_all_real_s"],
                    "D_real_s":r["D_real_s"],
                    "batch_wall_s":r["batch_wall_s"],
                    "Frag_static_projection":r["Frag_static_placement_projection"],
                    "FGP_ms":r["FGP_placement_ms"],
                    "DCO_ms":r["DCO_ordering_ms"],
                    "Controller_total_ms":r["FGP_DCO_combined_controller_ms"],
                    "already_present_events":r["already_present_events"],
                    "pulling_events":r["pulling_events"],
                    "warning_events":r["warning_events"],
                    "telemetry_dir":tr.get("telemetry_dir",""),
                    "network_download_measured":False,
                    "PGDSF_physical_eviction_measured":False,
                }
                merged.append(entry);trialmap[key][mode]=entry
        for (n,ic,trial,seed),bymode in trialmap.items():
            fps={z["workload_sha256"] for z in bymode.values()}
            if len(fps)>1:
                notes.append({"suite":str(suite),"problem":f"WORKLOAD MISMATCH n={n} imgs={ic} trial={trial} {list(fps)}"})
                continue
            if "falcons" not in bymode:
                notes.append({"suite":str(suite),"problem":f"missing falcons n={n} img={ic} trial={trial}"})
                continue
            fal=bymode["falcons"]
            if any(x["successes"]!=n or x["exit_code"]!=0 for x in bymode.values()):
                notes.append({"suite":str(suite),"problem":f"one or more failed policy executions n={n} img={ic} trial={trial}"})
                continue
            for base in BASELINES:
                if base not in bymode:continue
                row={
                    "suite":suite.name,"stage":fal["stage"],
                    "requests":n,"image_count":ic,"trial":trial,"seed":seed,
                    "baseline":base,"same_workload_verified":True,
                }
                for metric in METRICS:
                    fv=float(fal[metric]);bv=float(bymode[base][metric])
                    row[metric+"_falcons"]=fv
                    row[metric+"_baseline"]=bv
                    row[metric+"_delta_falcons_minus_base"]=fv-bv
                    row[metric+"_relative_improvement_pct"]=(bv-fv)/bv*100 if bv else None
                comparisons.append(row)
    # Per-suite pairing. Do NOT combine same-seed runs across separate suites
    # as independent observations and inflate sample size.
    groups=defaultdict(list)
    for x in comparisons:
        groups[(x["suite"],x["stage"],x["requests"],x["image_count"],x["baseline"])].append(x)
    for (suite,stage,n,ic,base),g in sorted(groups.items()):
        z={"suite":suite,"stage":stage,"requests":n,"image_count":ic,
           "baseline":base,"paired_trials":len(g),"seeds":json.dumps([x["seed"] for x in g])}
        for met in METRICS:
            diffs=[x[met+"_delta_falcons_minus_base"] for x in g]
            z[met+"_mean_delta_s"]=statistics.mean(diffs)
            z[met+"_mean_relative_improvement_pct"]=statistics.mean(
                x[met+"_relative_improvement_pct"] for x in g)
            z[met+"_95CI_halfwidth_s"]=(t95(len(diffs))*statistics.stdev(diffs)/math.sqrt(len(diffs))
                                          if len(diffs)>1 else None)
        summary.append(z)
    csv_write(out/"runs_all_suites.csv",merged)
    csv_write(out/"paired_trial_differences.csv",comparisons)
    csv_write(out/"paired_summaries.csv",summary)
    (out/"quality_warnings.json").write_text(json.dumps(notes,ensure_ascii=False,indent=2),encoding="utf-8")
    print("\n=== VERIFIED PAIRED COMPARISONS (D lower better) ===")
    for x in summary:
        print(f"{x['stage']:<9} {x['suite']} N={x['requests']:2d} "
              f"images={x['image_count']:2d} vs={x['baseline']:<9} "
              f"trials={x['paired_trials']} "
              f"mean_D_delta={x['D_real_s_mean_delta_s']:+.3f}s "
              f"improve={x['D_real_s_mean_relative_improvement_pct']:+.2f}% "
              f"CI_half={x['D_real_s_95CI_halfwidth_s']}")
    print(f"\nTOTAL RUNS={len(merged)} PAIRS={len(comparisons)} WARNINGS={len(notes)}")
    for x in notes[:20]:print("WARN",x,file=sys.stderr)
    print("Saved:",out)
    return 0 if not notes else 2
if __name__=="__main__":sys.exit(main())
