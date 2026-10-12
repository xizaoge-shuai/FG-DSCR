#!/usr/bin/env python3
"""Formal original-vs-incremental algorithm parity + scheduling-overhead evaluation.

Runs both methods using the SAME published workload and unchanged decisions.
Produces one paired CSV/JSON per workload, not fabricated time or quality claims.
Fails loudly if any assignment, queue or objective differs.
"""
import argparse
import csv
import importlib.util
import json
import math
import random
import statistics
import sys
import time
from pathlib import Path

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

def run_one(module, case_path, cfg):
    containers, nodes, sizes = module.load_case(str(case_path))
    s = module.FGDscrScheduler(
        layer_sizes_mb=sizes,
        beam_width=1,
        max_best_response_rounds=5,
        phase1_neighbor_mode="move_swap",
        move_topk_per_node=8,
        swap_topk_per_node=6,
        hard_resource_filter=True,
        init_order="image_resource",
        lambda_cong=1.0,lambda_frag=0.1,lambda_aff=0.2,
        lambda_task_load=0.2,
        alpha1_reuse=1.0,alpha2_future=0.15,
        alpha3_pull=50.0,alpha4_evict=0.02,
        cache_policy="pgdsf",order_policy="dynamic_state",
        k_pin=6,unit_mb=50,
    )
    s.set_data(containers, nodes)
    # Distinguish scheduler time from final performance evaluation.
    phase1 = s.best_response_assignment
    phase2 = s.order_node
    timers={"phase1_ns":0,"phase2_ns":0}
    def wrap(fn, name):
        def measured(*args, **kwargs):
            started=time.perf_counter_ns()
            try:
                return fn(*args,**kwargs)
            finally:
                timers[name] += time.perf_counter_ns()-started
        return measured
    s.best_response_assignment=wrap(phase1,"phase1_ns")
    s.order_node=wrap(phase2,"phase2_ns")
    res=s.run()
    return res, {k.replace("_ns","_ms"):v/1e6 for k,v in timers.items()}

def close(a,b):
    return math.isclose(a,b,rel_tol=1e-9,abs_tol=1e-7)

def check_pair(ref, faster, name):
    if ref["assignment"]!=faster["assignment"]:
        raise AssertionError(f"{name}: edge placement CHANGED")
    if ref["ordered_queues"]!=faster["ordered_queues"]:
        raise AssertionError(f"{name}: node-local startup ordering CHANGED")
    if ref.get("failed_containers")!=faster.get("failed_containers"):
        raise AssertionError(f"{name}: fallback requests CHANGED")
    for metric in ("ACT","AMS","objective","num_failed","downloaded_mb","reused_mb"):
        x=ref["summary"].get(metric)
        y=faster["summary"].get(metric)
        if x is None or y is None or not close(float(x),float(y)):
            raise AssertionError(f"{name}: {metric} changed: {x} vs {y}")
    a=ref.get("phase1_history",[])
    b=faster.get("phase1_history",[])
    if len(a)!=len(b):
        raise AssertionError(f"{name}: accepted refinement rounds changed")
    for i,(x,y) in enumerate(zip(a,b)):
        if not close(float(x["potential"]),float(y["potential"])):
            raise AssertionError(f"{name}: potential diverged at state {i}")

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--cases",nargs="+",required=True)
    p.add_argument("--repeats",type=int,default=10)
    p.add_argument("--out-dir",default="results/comnet_revision/optimized_runtime")
    cfg=p.parse_args()
    if cfg.repeats<2:
        p.error("--repeats must be >= 2")
    original=load("scripts/fg_dscr.py","fg_original_revision")
    optimized=load("scripts/fg_dscr_incremental.py","fg_incremental_revision")
    out=Path(cfg.out_dir)
    out.mkdir(parents=True,exist_ok=True)
    rows=[]
    for name in cfg.cases:
        case=Path(name)
        for repeat in range(cfg.repeats):
            # Alternate order to reduce systematic caching/thermal bias.
            sequence = [("original",original),("incremental",optimized)]
            if repeat%2:
                sequence.reverse()
            results={}
            for key,module in sequence:
                random.seed(42)
                r,t=run_one(module,case,cfg)
                results[key]=(r,t)
            check_pair(results["original"][0],results["incremental"][0],str(case))
            for key in ("original","incremental"):
                r,t=results[key]
                row={"case":str(case),"requests":r["summary"]["num_containers"],
                    "nodes":r["summary"]["num_nodes"],"repeat":repeat,"implementation":key,
                    "phase1_ms":t["phase1_ms"],"phase2_ms":t["phase2_ms"],
                    "controller_ms":t["phase1_ms"]+t["phase2_ms"],
                    "num_failed":r["summary"]["num_failed"],
                    "objective":r["summary"]["objective"]}
                rows.append(row)
            a=results["original"][1]
            b=results["incremental"][1]
            print(f"{case.name} run={repeat} original={a['phase1_ms']:.2f}ms "
                  f"incremental={b['phase1_ms']:.2f}ms parity=PASS",flush=True)
    csv_path=out/"paired_runtime.csv"
    with csv_path.open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    agg=[]
    for case in sorted({r["case"] for r in rows}):
        methods={}
        for method in ("original","incremental"):
            xs=[r["controller_ms"] for r in rows if r["case"]==case and r["implementation"]==method]
            methods[method]={"mean_ms":statistics.mean(xs),
                             "std_ms":statistics.stdev(xs),
                             "median_ms":statistics.median(xs)}
        agg.append({"case":case,"original":methods["original"],
                    "incremental":methods["incremental"],
                    "speedup":methods["original"]["mean_ms"]/max(methods["incremental"]["mean_ms"],1e-12),
                    "parity":"PASS"})
    (out/"paired_runtime_summary.json").write_text(json.dumps(agg,indent=2),encoding="utf-8")
    print("Completed",len(cfg.cases),"formal workloads; all algorithm parity assertions passed.")
    print("Wrote",csv_path)

if __name__=="__main__":
    main()
