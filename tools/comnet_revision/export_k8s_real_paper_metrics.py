#!/usr/bin/env python3
"""Read-only paper-aligned REAL Kubernetes metric export for COMNET revision.

Reads saved real_oci_smoke run artifacts. All outputs distinguish:
  OBSERVED: real Pod timestamps, controller end-to-end timing, pull events, success.
  PROJECTED: static resource-fragmentation from actual node placements and
             requested resources, using original FGP residual-ratio formula.
  UNAVAILABLE: measured network bytes, real cloud fallback, physical PGDSF
               eviction, manuscript-normalized composite J.
Never relabel theoretical OCI unique blob sizes as actual downloaded bytes.
"""
import argparse,csv,json,math,pathlib,re,statistics
from collections import Counter,defaultdict

def load(path):
    return json.loads(path.read_text(encoding="utf-8"))

def pctl(vals,p):
    if not vals:return None
    s=sorted(vals);i=(len(s)-1)*p;lo=int(i);hi=min(lo+1,len(s)-1)
    return s[lo]+(s[hi]-s[lo])*(i-lo)

def mn(vals):return statistics.mean(vals) if vals else None
def finite_numbers(vals):
    ret=[]
    for v in vals:
        if v is None or v=="":continue
        try:
            n=float(v)
            if math.isfinite(n):ret.append(n)
        except (ValueError,TypeError):pass
    return ret

def event_pull_seconds(s):
    # kubelet event often: Successfully pulled ... in 3m37.304s
    m=re.search(r"Successfully pulled image .*? in (?:(\d+)h)?(?:(\d+)m)?(\d+(?:\.\d+)?)s",s)
    return (int(m.group(1) or 0)*3600+int(m.group(2) or 0)*60+
            float(m.group(3))) if m else None

def frag_projected(case,pods):
    # Exact function form in scripts/fg_dscr.py::fragmentation_penalty.
    # This is NOT measured instantaneous fragmentation: workloads run only in
    # waves, and this applies *all* submitted request reservations at once.
    nodes={x["eid"]:x["resources"] for x in case["nodes"]}
    requests={x["cid"]:x["resources"] for x in case["containers"]}
    usage={n:{q:0.0 for q in cap} for n,cap in nodes.items()}
    counts=Counter()
    for p in pods:
        name=p.get("metadata",{}).get("name","")
        cid=name.removeprefix("job-")
        eid=p.get("spec",{}).get("nodeName")
        if cid not in requests or eid not in nodes:continue
        counts[eid]+=1
        for q in usage[eid]:
            usage[eid][q]+=float(requests[cid].get(q,0.0))
    individual={}
    for eid,cap in nodes.items():
        if not counts[eid]:
            individual[eid]=0.0
            continue
        res=[];press=[];tiny=0.0
        for q,v in cap.items():
            capacity=max(float(v),1e-8);u=usage[eid][q]
            if u>capacity+1e-9:
                individual[eid]=None;break
            rem=max(0.0,capacity-u)/capacity
            res.append(rem);press.append(u/capacity)
            if 0.0<rem<0.15:tiny+=0.5
        else:
            mr=statistics.mean(res);mp=statistics.mean(press)
            vr=statistics.mean((x-mr)**2 for x in res)
            vp=statistics.mean((x-mp)**2 for x in press)
            individual[eid]=vr+0.5*vp+tiny
    vals=[v for v in individual.values() if v is not None]
    return (mn(vals) if len(vals)==len(nodes) else None),individual,dict(counts)

def csv_rows(path):
    if not path.exists():return []
    with path.open(encoding="utf-8",newline="") as f:return list(csv.DictReader(f))

def collect(root,catalog):
    images={im["image_ref"]:im for im in catalog["images"]}
    output=[];request_rows=[]
    for summaryfile in sorted(root.glob("*/summary.json")):
        folder=summaryfile.parent
        if not all((folder/name).exists() for name in
                   ("pods_raw.json","events_raw.json","workload.json")):
            print("SKIP incomplete",folder)
            continue
        summary=load(summaryfile)
        pods=load(folder/"pods_raw.json").get("items",[])
        events=load(folder/"events_raw.json").get("items",[])
        case=load(folder/"workload.json")
        rawrows=csv_rows(folder/"measurements.csv")
        fct=finite_numbers(x.get("controller_finished_relative_s") for x in rawrows)
        starts=finite_numbers(x.get("create_to_start_s") for x in rawrows)
        finish_from_create=finite_numbers(x.get("create_to_finish_s") for x in rawrows)
        ack=finite_numbers(x.get("api_ack_ms") for x in rawrows)
        success=sum(x.get("status",{}).get("phase")=="Succeeded" and
                ((x.get("status",{}).get("containerStatuses") or [{}])[0]
                 .get("state",{}).get("terminated") or {}).get("exitCode")==0
                for x in pods)
        n=int(summary["count"]);mode=summary["mode"]
        phase_counts=Counter(x.get("status",{}).get("phase","Unknown") for x in pods)
        pull=[e for e in events if e.get("reason")=="Pulling"]
        pulled=[e for e in events if e.get("reason")=="Pulled"]
        cached=[e for e in pulled if "already present" in
                e.get("message","").lower()]
        actual_pull=[e for e in pulled if "successfully pulled" in
                     e.get("message","").lower()]
        seconds=[event_pull_seconds(e.get("message","")) for e in actual_pull]
        seconds=[x for x in seconds if x is not None]
        frag,nodefrags,nodecounts=frag_projected(case,pods)
        imtotal=0;missing_img=[]
        for p in pods:
            ref=(p.get("spec",{}).get("containers") or [{}])[0].get("image","")
            if ref in images:
                imtotal+=int(images[ref]["compressed_layer_total_bytes"])
            else:missing_img.append(ref)
        overlap={}
        for node in case["nodes"]:
            eid=node["eid"];content={}
            for p in pods:
                if p.get("spec",{}).get("nodeName")!=eid:continue
                ref=(p.get("spec",{}).get("containers") or [{}])[0].get("image","")
                if ref not in images:continue
                for layer in images[ref]["layers"]:
                    content[layer["digest"]]=int(layer["size_bytes"])
            overlap[eid]=sum(content.values())
        # Unique bytes on each node is a MANIFEST bound assuming initially
        # zero layer cache, not measured actual network transfer.
        logical_unique=sum(overlap.values())
        observed_D=(.5*(mn(fct)+pctl(fct,.95))) if len(fct)==n else None
        row={
            "run_id":summary.get("run",folder.name),"mode":mode,
            "requests":n,"cache_model":summary.get("cache_model","unknown"),
            "n_pods_observed":len(pods),"successes":success,
            "success_rate":success/n,"failure_count":n-success,
            "FCT_samples_count":len(fct),
            "ACT_all_real_s":mn(fct) if len(fct)==n else None,
            "P95_all_real_s":pctl(fct,.95) if len(fct)==n else None,
            "AMS_global_real_s":max(fct) if len(fct)==n else None,
            "D_real_s":observed_D,
            "batch_wall_s":summary.get("batch_controller_wall_s"),
            "create_to_start_mean_s":mn(starts),
            "create_to_start_P95_s":pctl(starts,.95),
            "create_to_finish_mean_s":mn(finish_from_create),
            "create_to_finish_P95_s":pctl(finish_from_create,.95),
            "FGP_DCO_combined_controller_ms":summary.get("planner_ms"),
            "FGP_placement_ms":summary.get("phase1_placement_ms"),
            "DCO_ordering_ms":summary.get("phase2_ordering_ms"),
            "api_ack_mean_ms":mn(ack),
            "pulling_events":len(pull),"pulled_events":len(pulled),
            "already_present_events":len(cached),
            "successfully_pulled_events":len(actual_pull),
            "parsed_pull_duration_samples":len(seconds),
            "observed_pull_duration_mean_s":mn(seconds),
            "observed_pull_duration_P95_s":pctl(seconds,.95),
            "warning_events":sum(x.get("type")=="Warning" for x in events),
            "observed_cloud_fallback_count":None,
            "CA_rate_manuscript":None,
            "Frag_static_placement_projection":frag,
            "Frag_live_measurement":None,
            "image_logical_request_sum_MiB":imtotal/1048576 if not missing_img else None,
            "OCI_unique_per_node_ideal_cold_floor_MiB":logical_unique/1048576 if not missing_img else None,
            "OCI_possible_duplicate_elimination_MiB":(imtotal-logical_unique)/1048576 if not missing_img else None,
            "actual_network_download_MB":None,
            "physical_PGDSF_evicted_MB":None,
            "manuscript_J":None,
            "node_counts":json.dumps(nodecounts,sort_keys=True),
            "node_frag_projected":json.dumps(nodefrags,sort_keys=True),
            "folder":str(folder),
        }
        output.append(row)
        for r in rawrows:
            request_rows.append({"run_id":row["run_id"],"mode":mode,"requests":n,
                                 "cache_model":row["cache_model"],**r})
        print(f"{row['run_id']:<16} {mode:<10} N={n:<3} "
              f"success={success}/{n} ACT={row['ACT_all_real_s']} "
              f"P95={row['P95_all_real_s']} D={observed_D} "
              f"pulling={len(pull)} warm_events={len(cached)}",flush=True)
    return output,request_rows

def writecsv(path,rows):
    if not rows:return
    with path.open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]),extrasaction="ignore")
        w.writeheader();w.writerows(rows)

def t95(n):
    # two-sided Student-t 95%, df=n-1, for small independent trial counts
    v=[None,12.706,4.303,3.182,2.776,2.571,2.447,2.365,
       2.306,2.262,2.228,2.201,2.179,2.160,2.145,2.131,
       2.120,2.110,2.101,2.093,2.086]
    return v[n-1] if n<=len(v) else (2.042 if n<=31 else 1.96)

def group_rows(rows):
    grouped=defaultdict(list)
    for row in rows:
        grouped[(row["requests"],row["cache_model"],row["mode"])].append(row)
    metrics=["ACT_all_real_s","P95_all_real_s","AMS_global_real_s",
             "D_real_s","batch_wall_s","create_to_start_mean_s",
             "FGP_DCO_combined_controller_ms","FGP_placement_ms",
             "DCO_ordering_ms","api_ack_mean_ms",
             "Frag_static_placement_projection"]
    out=[]
    for (n,cache,mode),group in sorted(grouped.items()):
        entry={"requests":n,"cache_model":cache,"mode":mode,
               "independent_runs":len(group),
               "all_successful_runs":sum(x["successes"]==n for x in group)}
        for col in metrics:
            values=finite_numbers(x.get(col) for x in group)
            entry[col+"_mean"]=mn(values)
            if len(values)>=2:
                sd=statistics.stdev(values)
                entry[col+"_sd"]=sd
                entry[col+"_95CI_halfwidth"]=t95(len(values))*sd/math.sqrt(len(values))
            else:
                entry[col+"_sd"]=None;entry[col+"_95CI_halfwidth"]=None
        out.append(entry)
    return out

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--root",default="results/comnet_revision/k8s_real_testbed/real_oci_smoke")
    p.add_argument("--catalog",default="results/comnet_revision/k8s_real_testbed/oci_shared_family/catalog.json")
    p.add_argument("--out-dir",default="results/comnet_revision/k8s_real_testbed/paper_metrics")
    a=p.parse_args()
    root=pathlib.Path(a.root);out=pathlib.Path(a.out_dir)
    images=load(pathlib.Path(a.catalog))
    if images.get("errors"):p.error("OCI catalog includes errors")
    data,requests=collect(root,images)
    if not data:p.error("No complete saved real Kubernetes runs found")
    out.mkdir(parents=True,exist_ok=True)
    writecsv(out/"real_metrics_by_run.csv",data)
    writecsv(out/"real_requests.csv",requests)
    grouped=group_rows(data)
    writecsv(out/"real_metrics_by_method_scale.csv",grouped)
    provenance={
      "source":str(root),"catalog":a.catalog,
      "raw_data_files":["summary.json","pods_raw.json","events_raw.json",
                        "workload.json","measurements.csv"],
      "measured_metrics":[
        "batch-to-finish controller observed ACT,P95,AMS,D (same form as manuscript D but empirical seconds)",
        "success fraction, Pod creation-to-start/finish, mean API acknowledgment, algorithm combined wall time",
        "Kubelet pulling/pulled/warning events and durations parsed from kubelet messages"
      ],
      "projected_metrics":[
        "Frag_static_placement_projection matches per-node FGP fragmentation_penalty mathematical form, "
        "using observed actual Pod node assignment but summing all Pod declared requests at once: "
        "NOT live residual resource fragmentation during concurrent execution",
        "Manifest unique layer bytes per node under hypothetical initially empty cache and ideal one-download-per-blob reuse: model lower bound on content payload, NOT measured transfer"
      ],
      "unavailable_metrics":[
        "Manuscript CA cloud fallback rate: K8s trial has no modeled/real cloud fallback path; Pod success is not CA",
        "Actual download bytes require dedicated registry/worker network metering",
        "True PGDSF eviction requires physical cache control and eviction logging",
        "Manuscript-normalized J cannot be compared directly with current physical runs"
      ],
      "statistical_warning":"A 95% CI is reported only when at least two runs exist. Five or more independent repeated trials are recommended; equal workload requests within a run are NOT independent experimental replicates.",
      "timestamp_warning":"Controller observed completion times have polling granularity; Pod create/start/finish timestamps are node-reported and may have clock offset.",
      "image_warning":"The current container command only sleeps eight seconds; no throughput or realistic application responsiveness is measured."
    }
    (out/"metrics_provenance.json").write_text(json.dumps(provenance,indent=2,ensure_ascii=False),encoding="utf-8")
    print("Saved",out,"runs",len(data),"requests",len(requests),"groups",len(grouped),flush=True)

if __name__=="__main__":main()
