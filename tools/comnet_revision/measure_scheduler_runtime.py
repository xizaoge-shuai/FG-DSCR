#!/usr/bin/env python3
"""FALCONS CN revision: measured scheduler runtime, no synthetic timing data.

Run from repository root. This deliberately leaves the original algorithm unchanged.
Phase I includes greedy initialization and bounded refinement. Phase II measures
DCO ordering. "evaluation" is the simulator, not online controller time.
"""
import argparse
import csv
import importlib.util
import json
import platform
import statistics
import time
from pathlib import Path

def load_module(path):
    spec = importlib.util.spec_from_file_location("falcons_runtime_subject", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot import " + path)
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

def measure_once(mod, case_path, args):
    containers, nodes, layer_sizes = mod.load_case(str(case_path))
    scheduler = mod.FGDscrScheduler(
        layer_sizes_mb=layer_sizes,
        beam_width=args.beam,
        max_best_response_rounds=args.rounds,
        phase1_neighbor_mode=args.neighbor_mode,
        move_topk_per_node=args.move_topk,
        swap_topk_per_node=args.swap_topk,
        block_seed_topk_per_node=args.block_topk,
        hard_resource_filter=True,
        init_order=args.init_order,
        lambda_cong=1.0, lambda_frag=0.1, lambda_aff=0.2,
        lambda_task_load=0.2,
        alpha1_reuse=1.0, alpha2_future=0.15,
        alpha3_pull=50.0, alpha4_evict=0.02,
        cache_policy="pgdsf", order_policy="dynamic_state",
        k_pin=6, unit_mb=50,
    )
    scheduler.set_data(containers, nodes)
    segments = {"greedy_ns": 0, "placement_ns": 0, "ordering_ns": 0, "evaluation_ns": 0}
    original_greedy = scheduler.greedy_init_assignment
    original_phase1 = scheduler.best_response_assignment
    original_order = scheduler.order_node
    original_eval = scheduler.simulate_queue

    def timed(key, fn):
        def wrapper(*a, **kw):
            t = time.perf_counter_ns()
            try:
                return fn(*a, **kw)
            finally:
                segments[key] += time.perf_counter_ns() - t
        return wrapper

    scheduler.greedy_init_assignment = timed("greedy_ns", original_greedy)
    scheduler.best_response_assignment = timed("placement_ns", original_phase1)
    scheduler.order_node = timed("ordering_ns", original_order)
    scheduler.simulate_queue = timed("evaluation_ns", original_eval)
    wall_t0 = time.perf_counter_ns()
    result = scheduler.run()
    total_ns = time.perf_counter_ns() - wall_t0
    control_ns = segments["placement_ns"] + segments["ordering_ns"]
    return {
        "requests": len(containers), "nodes": len(nodes), "beam": args.beam,
        "round_limit": args.rounds, "neighbor_mode": args.neighbor_mode,
        "init_order": args.init_order,
        "greedy_ms": segments["greedy_ns"]/1e6,
        "refinement_ms": (segments["placement_ns"]-segments["greedy_ns"])/1e6,
        "placement_ms": segments["placement_ns"]/1e6,
        "ordering_ms": segments["ordering_ns"]/1e6,
        "simulation_evaluation_ms": segments["evaluation_ns"]/1e6,
        "controller_ms": control_ns/1e6,
        "total_run_ms": total_ns/1e6,
        "cloud_fallback_count": len(result.get("failed_containers", [])),
        "phase1_recorded_states": len(result.get("phase1_history", [])),
    }

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--case", required=True, help="Existing unmodified case JSON")
    p.add_argument("--algorithm", default="scripts/fg_dscr.py")
    p.add_argument("--out-dir", default="results/comnet_revision/runtime")
    p.add_argument("--repeats", type=int, default=10)
    p.add_argument("--warmup", type=int, default=1)
    p.add_argument("--beam", type=int, default=1)
    p.add_argument("--rounds", type=int, default=5)
    p.add_argument("--neighbor-mode", choices=["move","move_swap","full"], default="move_swap")
    p.add_argument("--move-topk", type=int, default=8)
    p.add_argument("--swap-topk", type=int, default=6)
    p.add_argument("--block-topk", type=int, default=5)
    p.add_argument("--init-order", choices=["image_resource","arrival","resource_desc","resource_asc"], default="image_resource")
    args = p.parse_args()
    if args.repeats < 2 or args.warmup < 0:
        p.error("Use --repeats >= 2 and --warmup >= 0")
    import random
    mod = load_module(args.algorithm)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    case_path = Path(args.case)
    rows = []
    for i in range(args.warmup + args.repeats):
        random.seed(42)
        observation = measure_once(mod, case_path, args)
        if i >= args.warmup:
            observation["repeat"] = i - args.warmup
            rows.append(observation)
            print(f"repeat={observation['repeat']} controller_ms={observation['controller_ms']:.3f} "
                  f"phase1_ms={observation['placement_ms']:.3f} phase2_ms={observation['ordering_ms']:.3f}", flush=True)
    csv_path = out / (case_path.stem + "_runtime.csv")
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    metrics = ["greedy_ms","refinement_ms","placement_ms","ordering_ms",
               "simulation_evaluation_ms","controller_ms","total_run_ms"]
    summary = {}
    for metric in metrics:
        values = sorted(x[metric] for x in rows)
        p95_index = min(len(values)-1, max(0, __import__("math").ceil(.95*len(values))-1))
        summary[metric] = {"mean": statistics.mean(values), "stddev": statistics.stdev(values),
                           "median": statistics.median(values), "p95": values[p95_index]}
    metadata = {"algorithm":args.algorithm, "case":str(case_path),
                "repeats":args.repeats, "warmup":args.warmup,
                "python":platform.python_version(), "machine":platform.platform(),
                "config":vars(args), "summary_ms":summary,
                "NOTE":"controller_ms = FGP placement + DCO ordering. The simulation evaluator and JSON/reporting are excluded. Physical K8s startup/registry contention is not measured."}
    (out/(case_path.stem+"_runtime_summary.json")).write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Saved", csv_path)

if __name__ == "__main__":
    main()
