#!/usr/bin/env python3
"""COMNET major revision R1.4/R2.4: paired independent empirical workloads.

Generate 20+ independent empirical-resample workload draws from the archived
prefix 1000-request population; every method is evaluated on the IDENTICAL
generated instance per seed. Reuse the ORIGINAL K8s same-scale run_one.sh,
original saved selected-parameter env, and original Fig5 all-request evaluator.

The original article's fixed reference values are always applied to raw metrics.
No seed-specific reference re-fitting. This is empirical resampling from a
1000-request sample, NOT evidence of 20 independent DRTP trace collections or
real Kubernetes runs.

Usage: python3 tools/comnet_revision/run_paired_multiseed.py --mode generate|run|analyze|all
"""
from __future__ import annotations
import argparse, csv, hashlib, json, math, os, random, re, statistics, subprocess, sys, time
from pathlib import Path

METHODS=("fg","ilrsa","lrs","gahrl","orr","lasa")
METHOD_NAMES=("FG-DSCR-GC","ILR-SA","LRScheduler","GAHRL","ORR","LASA")
NAME=dict(zip(METHODS,METHOD_NAMES))
REF_D,REF_CA,REF_FRAG=440.7696,.77924,.180069

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def write_csv(p,rows):
    if not rows:
        raise RuntimeError(f"No data to write to {p}")
    p.parent.mkdir(parents=True,exist_ok=True)
    with p.open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)

def load_selected_env(p):
    env={}
    for line in p.read_text(encoding="utf-8").splitlines():
        m=re.match(r'^\s*export\s+([A-Z][A-Z_0-9]*)=(\S+)\s*$',line)
        if m:env[m.group(1)]=m.group(2).strip('"').strip("'")
    for name in ("FG_INIT_ORDER","FG_LAMBDA_FRAG","FG_LAMBDA_AFF","FG_GREEDY_LOAD_FACTOR"):
        if name not in env:raise ValueError(f"Historical config missing {name}")
    return env

def seeds(args):
    return range(args.seed_start,args.seed_start+args.num_seeds)

def casepath(args,seed,n):
    return args.out_dir/"cases"/f"seed{seed}"/f"drtp_img88_cacheonly_1024mb_{n}.json"

def generate(args):
    base=json.loads(args.source_case.read_text(encoding="utf-8"))
    population=base["containers"]
    if not population:raise ValueError("Base has no requests")
    for seed in seeds(args):
        rng=random.Random(seed)
        sampled=rng.choices(population,k=max(args.sizes))
        for n in args.sizes:
            dest=casepath(args,seed,n)
            obj={k:v for k,v in base.items() if k not in ("containers","meta","prefix_meta")}
            obj["containers"]=[]
            for i,src in enumerate(sampled[:n],start=1):
                c=dict(src);c["cid"]=f"c{i:06d}"
                obj["containers"].append(c)
            obj["meta"]={
                "experiment":"COMNET reviewer R2.4 empirical resample",
                "draw_seed":seed,
                "draw_population_size":len(population),
                "source_case":str(args.source_case),
                "source_sha256":sha(args.source_case),
                "method":"sample with replacement preserving per-request joint layers/resources/runtime",
                "sampling_note":"Independent PRNG stream per seed; nested 200..1000 prefixes within seed",
                "num_containers":n,
                "num_nodes":len(obj["nodes"]),
            }
            raw=json.dumps(obj,sort_keys=True,ensure_ascii=False,indent=2)+"\n"
            if dest.exists():
                if dest.read_text(encoding="utf-8")!=raw:
                    raise RuntimeError(f"Existing generated input differs: {dest}. Never overwrite.")
                continue
            dest.parent.mkdir(parents=True,exist_ok=True)
            dest.write_text(raw,encoding="utf-8")
        print(f"[CASE] seed={seed} sizes={','.join(map(str,args.sizes))}",flush=True)
    print("[GENERATED]",args.out_dir/"cases",flush=True)

def run_job(argv,log,env):
    log.parent.mkdir(parents=True,exist_ok=True)
    with log.open("w",encoding="utf-8") as f:
        start=time.perf_counter()
        ret=subprocess.run(argv,stdout=f,stderr=subprocess.STDOUT,env=env,check=False).returncode
        elapsed=time.perf_counter()-start
        f.write(f"\nCOMNET_EXEC_RETURN={ret} COMNET_EXEC_WALL_S={elapsed:.6f}\n")
    if ret:
        raise RuntimeError(f"Failed exit={ret}: {' '.join(map(str,argv))}; inspect {log}")
    return elapsed

def result_exists(p):
    if not p.exists():return False
    try:
        data=json.loads(p.read_text(encoding="utf-8"))
        return isinstance(data.get("assignment"),dict) and isinstance(data.get("summary"),dict)
    except Exception:return False

def run(args):
    selected=load_selected_env(args.env_file)
    env=os.environ.copy();env.update(selected);env["PYTHONHASHSEED"]="0"
    rawroot=args.out_dir/"raw"
    scripts=Path("scripts/k8s_same_scale/run_one.sh")
    for seed in seeds(args):
        for n in args.sizes:
            cp=casepath(args,seed,n)
            if not cp.is_file():raise FileNotFoundError(f"Run --mode generate first: {cp}")
            for method in args.methods:
                dest=rawroot/f"seed{seed}"/f"{method}_prefix_req{n}.json"
                logfile=args.out_dir/"logs"/f"seed{seed}"/f"{method}_req{n}.log"
                if result_exists(dest):
                    print(f"[SKIP] seed={seed} req={n} method={method}",flush=True)
                    continue
                dest.parent.mkdir(parents=True,exist_ok=True)
                elapsed=run_job(["bash",str(scripts),method,str(cp),str(dest)],logfile,env)
                if not result_exists(dest):
                    raise RuntimeError(f"Program exit 0 without valid result: {dest}")
                print(f"[RUN] seed={seed} req={n} method={method} wall={elapsed:.2f}s",flush=True)
        # Use the official all-request evaluator; its separately calculated
        # per-group reference normalizations are IGNORED by our analysis.
        all_present=all(
            result_exists(rawroot/f"seed{seed}"/f"{method}_prefix_req{n}.json")
            for n in args.sizes for method in METHODS
        )
        if not all_present:
            print(f"[NOT EVALUATED] seed={seed}: not all 6 methods present",flush=True)
            continue
        out=args.out_dir/"evaluated"/f"seed{seed}"
        detail=out/"fig5_allrequest_detail.md"
        if detail.exists():
            print(f"[SKIP EVALUATOR] seed={seed}",flush=True)
            continue
        evaluation_env=env.copy()
        evaluation_env.update({
            "CASE_DIR":str((args.out_dir/"cases"/f"seed{seed}").resolve()),
            "FIG5_RES_DIR":str((rawroot/f"seed{seed}").resolve()),
            "OUT_DIR":str(out.resolve()),
            "CA_PROVISION_SEC":str(args.provision_sec),
            "CA_START_SEC":"0",
        })
        logfile=args.out_dir/"logs"/f"seed{seed}"/"evaluate.log"
        wall=run_job([sys.executable,"tools/summarize_fig5_allrequest_latency.py"],logfile,evaluation_env)
        if not detail.exists():raise RuntimeError(f"Evaluator did not create {detail}")
        print(f"[EVAL] seed={seed} 6 methods x {len(args.sizes)} scales in {wall:.2f}s",flush=True)

def parse_table(p):
    lines=p.read_text(encoding="utf-8").splitlines()
    head=None; rows=[]
    for line in lines:
        if not line.strip().startswith("|"):continue
        cells=[x.strip() for x in line.strip().strip("|").split("|")]
        if cells and cells[0]=="requests":
            head=cells;continue
        if head and len(cells)==len(head) and re.fullmatch(r"\d+",cells[0]):
            rows.append(dict(zip(head,cells)))
    return rows

def percentile(xs,p):
    xs=sorted(xs);q=(len(xs)-1)*p;lo=math.floor(q);hi=math.ceil(q)
    return xs[lo]+(xs[hi]-xs[lo])*(q-lo)

def bootstrap_ci(vals,label,rep=5000):
    vals=list(map(float,vals))
    if len(vals)<2: return (float("nan"),float("nan"))
    h=hashlib.sha256(label.encode()).digest()
    rnd=random.Random(int.from_bytes(h[:8],"big"))
    n=len(vals);boot=[]
    for _ in range(rep):
        boot.append(sum(vals[rnd.randrange(n)] for _ in range(n))/n)
    return (percentile(boot,.025),percentile(boot,.975))

def analyze(args):
    records=[]
    for seed in seeds(args):
        path=args.out_dir/"evaluated"/f"seed{seed}"/"fig5_allrequest_detail.md"
        if not path.is_file():raise FileNotFoundError(f"Missing formal evaluator output: {path}")
        rr=parse_table(path)
        observed={(int(x["requests"]),x["method"]) for x in rr}
        expected={(n,m) for n in args.sizes for m in METHOD_NAMES}
        if observed!=expected:
            raise RuntimeError(f"seed={seed} expected 6x{len(args.sizes)}, missing={expected-observed}, unexpected={observed-expected}")
        for row in rr:
            if float(row.get("missing_edge_time",0.))>0:
                raise ValueError(f"seed={seed} missing edge completion times for {row['method']}")
            n=int(row["requests"])
            D=float(row["Delay_all"])
            CA=float(row["CA_rate"])
            frag=float(row["frag_score"])
            rrw={
                "seed":seed,"requests":n,"method":row["method"],
                "mean_all_s":float(row["ACT_all"]),"p95_all_s":float(row["P95_all"]),
                "D_s":D,"CA_rate":CA,"frag_score":frag,
                "downloaded_mb":float(row["downloaded_mb"]),
                "J":D/REF_D+CA/REF_CA+frag/REF_FRAG,
            }
            records.append(rrw)
    write_csv(args.out_dir/"per_seed_allrequest_metrics.csv",records)
    summary=[]
    for n in args.sizes:
        for m in METHOD_NAMES:
            subset=[x for x in records if x["requests"]==n and x["method"]==m]
            if len(subset)!=args.num_seeds:raise RuntimeError("Unpaired seed records")
            values=[x["J"] for x in subset]
            low,high=bootstrap_ci(values,f"absolute_{n}_{m}")
            summary.append({
                "requests":n,"method":m,"n_independent_resampled_workloads":len(values),
                "mean_J":statistics.mean(values),"sample_SD_J":statistics.stdev(values),
                "CI95_mean_J_low":low,"CI95_mean_J_high":high,
                **{f"mean_{k}":statistics.mean(x[k] for x in subset) for k in (
                    "mean_all_s","p95_all_s","D_s","CA_rate","frag_score","downloaded_mb"
                )}
            })
    write_csv(args.out_dir/"per_scale_method_mean_ci95.csv",summary)
    paired=[]
    methods=[m for m in METHOD_NAMES if m!="FG-DSCR-GC"]
    for n in args.sizes:
        group=[n]
        for m in methods:
            a={x["seed"]:x for x in records if x["method"]=="FG-DSCR-GC" and x["requests"] in group}
            b={x["seed"]:x for x in records if x["method"]==m and x["requests"] in group}
            common=sorted(set(a)&set(b))
            if len(common)!=args.num_seeds:raise RuntimeError(f"Incomplete pairing n={n} baseline={m}")
            delta=[b[seed]["J"]-a[seed]["J"] for seed in common]
            improvement_pct=[100*d/b[seed]["J"] for d,seed in zip(delta,common)]
            low,high=bootstrap_ci(delta,f"paired_{n}_{m}")
            plow,phigh=bootstrap_ci(improvement_pct,f"pairedpct_{n}_{m}")
            paired.append({
                "requests":n,"baseline":m,"paired_seeds":len(common),
                "mean_deltaJ_baseline_minus_FALCONS":statistics.mean(delta),
                "deltaJ_CI95_low":low,"deltaJ_CI95_high":high,
                "mean_paired_improvement_pct":statistics.mean(improvement_pct),
                "improvement_pct_CI95_low":plow,
                "improvement_pct_CI95_high":phigh,
                "FALCONS_better_seeds":sum(x>0 for x in delta),
                "CI_excludes_zero":int(low>0 or high<0),
            })
    # Per-seed average over scales avoids pseudoreplication.
    if len(args.sizes)>1:
        for m in methods:
            seed_delta=[];seed_pct=[]
            for seed in seeds(args):
                a=statistics.mean(x["J"] for x in records if x["seed"]==seed and x["method"]=="FG-DSCR-GC")
                b=statistics.mean(x["J"] for x in records if x["seed"]==seed and x["method"]==m)
                seed_delta.append(b-a);seed_pct.append(100*(b-a)/b)
            low,high=bootstrap_ci(seed_delta,f"paired_mean_{m}")
            plow,phigh=bootstrap_ci(seed_pct,f"pairedpct_mean_{m}")
            paired.append({
                "requests":"ALL_SCALES_AVG_PER_SEED","baseline":m,"paired_seeds":len(seed_delta),
                "mean_deltaJ_baseline_minus_FALCONS":statistics.mean(seed_delta),
                "deltaJ_CI95_low":low,"deltaJ_CI95_high":high,
                "mean_paired_improvement_pct":statistics.mean(seed_pct),
                "improvement_pct_CI95_low":plow,"improvement_pct_CI95_high":phigh,
                "FALCONS_better_seeds":sum(x>0 for x in seed_delta),
                "CI_excludes_zero":int(low>0 or high<0),
            })
    write_csv(args.out_dir/"paired_differences_bootstrap_ci95.csv",paired)
    provenance={
        "experiment":"R1.4 R2.4 20+ paired empirical resampled workloads",
        "sampling":"with replacement from fixed prefix 1000-container archived population; all 6 methods share identical workload",
        "source_case":str(args.source_case),"sha256_source":sha(args.source_case),
        "selected_parameters":load_selected_env(args.env_file),"selected_parameters_source":str(args.env_file),
        "selected_parameters_sha256":sha(args.env_file),
        "seed_start":args.seed_start,"num_seeds":args.num_seeds,"scales":args.sizes,
        "allrequest_model":"tools/summarize_fig5_allrequest_latency.py; 300s cloud provision and empirical edge-quantile replay",
        "paper_refs":{"D":REF_D,"CA":REF_CA,"Frag":REF_FRAG},
        "bootstraps":"5000 percentile bootstrap draws of the paired seed-wise mean; deterministic SHA-derived bootstrap seeds",
        "statistical_unit":"workload draw (seed); sizes in the overall aggregate paired within seed",
        "git_sha":subprocess.run(["git","rev-parse","HEAD"],capture_output=True,text=True,check=False).stdout.strip(),
        "limitations":[
            "An empirical resample is not a newly captured trace.",
            "Fixed article reference scales; no per-seed recomputation of final J scales.",
            "Per-seed original summarizer does compute its own diagnostic normalized columns; they are discarded.",
            "This is a serial trace-driven simulation, not K8s wall-clock experiment.",
            "Baseline code adaptations and GAHRL training fairness must be audited separately.",
            "Post-hoc selection of parameter profiles based on held-out seeds invalidates confirmatory claims."
        ],
    }
    (args.out_dir/"provenance.json").write_text(json.dumps(provenance,indent=2,ensure_ascii=False),encoding="utf-8")
    print("\n=== Paired J improvements: positive means FALCONS improves ===")
    for x in paired:
        print(f"{str(x['requests']):>23} vs {x['baseline']:<13} "
              f"DeltaJ={x['mean_deltaJ_baseline_minus_FALCONS']:+.4f} "
              f"95%CI=[{x['deltaJ_CI95_low']:+.4f},{x['deltaJ_CI95_high']:+.4f}] "
              f"improvement={x['mean_paired_improvement_pct']:+.2f}% "
              f"wins={x['FALCONS_better_seeds']}/{x['paired_seeds']}",flush=True)
    print("\nSaved",args.out_dir.resolve())

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--mode",choices=("generate","run","analyze","all"),required=True)
    p.add_argument("--source-case",type=Path,default=Path("cases/drtp_cache_only_sweep_88_prefix/drtp_img88_cacheonly_1024mb_1000.json"))
    p.add_argument("--env-file",type=Path,default=Path("results/drtp/k8s_same_scale/sweep/best_fg_edgeca_params.env"))
    p.add_argument("--out-dir",type=Path,default=Path("results/comnet_revision/multiseed_archived"))
    p.add_argument("--seed-start",type=int,default=30101)
    p.add_argument("--num-seeds",type=int,default=20)
    p.add_argument("--sizes",type=int,nargs="+",default=[200,500,1000])
    p.add_argument("--methods",choices=METHODS,nargs="+",default=list(METHODS))
    p.add_argument("--provision-sec",type=float,default=300.)
    args=p.parse_args()
    if len(set(args.sizes))!=len(args.sizes) or min(args.sizes)<1:
        raise ValueError("Distinct positive --sizes required")
    if args.mode in ("generate","all"):generate(args)
    if args.mode in ("run","all"):run(args)
    if args.mode in ("analyze","all"):analyze(args)

if __name__=="__main__":
    main()
