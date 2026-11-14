from __future__ import annotations

import csv
import hashlib
import json
import math
import random
import statistics
import time
from collections import defaultdict
from pathlib import Path

from secon_exp.cider.pulse import (
    PulseItem,
    PulseScheduler,
)

# ============================================================
# Configuration
# ============================================================

STATE_PATH = Path(
    "/home/zhjia/FG-DSCR/results/"
    "cider_review77_20261109/train/baselines/states/"
    "cider_nodes40_domains8_req800_weak__seed1.json"
)

QUANTUM_MB = 8.0
BUDGET_MB = 2048.0
MAX_TRANSFERS = 40

CANDIDATE_COUNTS = list(range(10, 101, 10))

INSTANCE_REPEATS = 10
TIMING_REPEATS = 3
SEED = 1

# ============================================================
# Helpers
# ============================================================

def total_utility(items):
    return sum(float(x.utility) for x in items)


def total_size(items):
    return sum(float(x.size_mb) for x in items)


def quantize_item(item):
    qsize = (
        math.ceil(float(item.size_mb) / QUANTUM_MB)
        * QUANTUM_MB
    )

    return PulseItem(
        node=item.node,
        layer=item.layer,
        size_mb=qsize,
        utility=float(item.utility),
    )


def run_timed(fn):
    times = []
    result = None

    for _ in range(TIMING_REPEATS):
        t0 = time.perf_counter()
        result = fn()
        times.append(
            (time.perf_counter() - t0) * 1000.0
        )

    return result, statistics.median(times)


def check_feasible(name, selected, budget, max_count):
    size = total_size(selected)

    if len(selected) > max_count:
        raise RuntimeError(
            f"{name}: transfer count violation: {len(selected)}"
        )

    if size > budget + 1e-6:
        raise RuntimeError(
            f"{name}: budget violation: {size:.6f} > {budget}"
        )

    return size


# ============================================================
# 1. Load and verify the real training state
# ============================================================

print("=" * 65)
print("PULSE OPTIMALITY GAP BENCHMARK")
print("=" * 65)

if not STATE_PATH.is_file():
    raise FileNotFoundError(STATE_PATH)

state_bytes = STATE_PATH.read_bytes()

state = json.loads(
    state_bytes.decode("utf-8")
)

case = state["target_case"]
placement = state["placement"]

nodes_data = case["nodes"]
containers = case["containers"]

nodes = [n["eid"] for n in nodes_data]

capacities = {
    int(n["repo_capacity_mb"])
    for n in nodes_data
}

print("State:", STATE_PATH)
print("State SHA256:", hashlib.sha256(state_bytes).hexdigest())
print("Seed:", state["seed"])
print("Nodes:", len(nodes))
print("Target requests:", len(containers))
print("Placement entries:", len(placement))
print("Placed requests:", sum(len(v) for v in placement.values()))
print("Cache capacities:", capacities)

if len(nodes) != 40:
    raise RuntimeError("Expected exactly 40 nodes")

if len(containers) != 400:
    raise RuntimeError("Expected exactly 400 target requests")

if capacities != {1024}:
    raise RuntimeError(
        f"Wrong cache capacity: {capacities}"
    )

if sum(len(v) for v in placement.values()) != 400:
    raise RuntimeError("Placement does not contain 400 requests")

# ============================================================
# 2. Construct the actual first-cycle candidate pool
# ============================================================

sizes = {
    k: float(v)
    for k, v in case["layer_sizes_mb"].items()
}

tasks = {
    c["cid"]: set(c["layers"])
    for c in containers
}

groups = {
    node: list(placement.get(node, []))
    for node in nodes
}

acquired = {
    n["eid"]: set(n.get("initial_cache", []))
    for n in nodes_data
}

need = {}

for node in nodes:
    union = set()

    for cid in groups[node]:
        if cid not in tasks:
            raise RuntimeError(
                f"Unknown container ID in placement: {cid}"
            )
        union.update(tasks[cid])

    need[node] = union

scheduler = PulseScheduler(
    quantum_mb=QUANTUM_MB,
    solver="lagrangian",
)

pool = scheduler.build_candidates(
    nodes=nodes,
    groups=groups,
    tasks=tasks,
    acquired=acquired,
    need=need,
    sizes=sizes,
    container_weights=None,
)

print("\nREAL CANDIDATE POOL =", len(pool))

if len(pool) < max(CANDIDATE_COUNTS):
    raise RuntimeError(
        f"Candidate pool too small: {len(pool)}"
    )

pool = [quantize_item(x) for x in pool]

budget_q = (
    math.floor(BUDGET_MB / QUANTUM_MB)
    * QUANTUM_MB
)

lag_solver = PulseScheduler(
    quantum_mb=QUANTUM_MB,
    solver="lagrangian",
)

exact_solver = PulseScheduler(
    quantum_mb=QUANTUM_MB,
    solver="exact",
)

# ============================================================
# 3. Smoke test: verify both solvers before full experiment
# ============================================================

print("\n========== SOLVER SMOKE TEST ==========")

smoke_items = pool[:10]
smoke_k = min(MAX_TRANSFERS, len(smoke_items))

lag_smoke = lag_solver.select_lagrangian(
    smoke_items,
    budget_q,
    smoke_k,
)

exact_smoke = exact_solver.select_exact(
    smoke_items,
    budget_q,
    smoke_k,
)

lag_smoke_size = check_feasible(
    "Lagrangian",
    lag_smoke,
    budget_q,
    smoke_k,
)

exact_smoke_size = check_feasible(
    "Exact",
    exact_smoke,
    budget_q,
    smoke_k,
)

lag_smoke_u = total_utility(lag_smoke)
exact_smoke_u = total_utility(exact_smoke)

print("Lagrangian utility:", lag_smoke_u)
print("Exact utility:", exact_smoke_u)
print("Lagrangian selected:", len(lag_smoke))
print("Exact selected:", len(exact_smoke))
print("Lagrangian size:", lag_smoke_size)
print("Exact size:", exact_smoke_size)

if lag_smoke_u > exact_smoke_u + 1e-7:
    raise RuntimeError(
        "Exact solver returned lower utility than "
        "a feasible Lagrangian solution. "
        "Stop: solver consistency must be investigated."
    )

print("[PASS] Both solvers use a consistent feasible problem.")

# ============================================================
# 4. Independent output directory
# ============================================================

stamp = time.strftime("%Y%m%d_%H%M%S")

out_dir = (
    Path("results")
    / f"pulse_gap_review77_{stamp}"
)

out_dir.mkdir(parents=True, exist_ok=False)

pointer = Path(
    "results/LATEST_PULSE_GAP_REVIEW77"
)

pointer.write_text(
    str(out_dir.resolve()),
    encoding="utf-8",
)

metadata = {
    "state_path": str(STATE_PATH),
    "state_sha256": hashlib.sha256(state_bytes).hexdigest(),
    "seed": SEED,
    "nodes": len(nodes),
    "target_requests": len(containers),
    "cache_capacity_mb": 1024,
    "candidate_pool_size": len(pool),
    "quantum_mb": QUANTUM_MB,
    "budget_mb": budget_q,
    "max_transfers": MAX_TRANSFERS,
    "candidate_counts": CANDIDATE_COUNTS,
    "instance_repeats": INSTANCE_REPEATS,
    "timing_repeats": TIMING_REPEATS,
}

(out_dir / "metadata.json").write_text(
    json.dumps(metadata, indent=2),
    encoding="utf-8",
)

raw_path = out_dir / "raw.csv"

fields = [
    "candidate_count",
    "instance",
    "lag_utility",
    "exact_utility",
    "gap_pct",
    "lag_runtime_ms",
    "exact_runtime_ms",
    "exact_over_lag_runtime_ratio",
    "lag_selected",
    "exact_selected",
    "lag_size_mb",
    "exact_size_mb",
]

# ============================================================
# 5. Full benchmark
# ============================================================

print("\n========== FULL BENCHMARK ==========")

summary_data = defaultdict(list)

with raw_path.open(
    "w",
    newline="",
    encoding="utf-8",
) as f:

    writer = csv.DictWriter(
        f,
        fieldnames=fields,
    )

    writer.writeheader()

    for n in CANDIDATE_COUNTS:

        for rep in range(INSTANCE_REPEATS):

            rng = random.Random(
                SEED + n * 1000 + rep
            )

            indices = rng.sample(
                range(len(pool)),
                n,
            )

            items = [pool[i] for i in indices]

            max_transfers = min(MAX_TRANSFERS, n)

            lag, lag_ms = run_timed(
                lambda: lag_solver.select_lagrangian(
                    items,
                    budget_q,
                    max_transfers,
                )
            )

            exact, exact_ms = run_timed(
                lambda: exact_solver.select_exact(
                    items,
                    budget_q,
                    max_transfers,
                )
            )

            lag_size = check_feasible(
                "Lagrangian",
                lag,
                budget_q,
                max_transfers,
            )

            exact_size = check_feasible(
                "Exact",
                exact,
                budget_q,
                max_transfers,
            )

            lag_u = total_utility(lag)
            exact_u = total_utility(exact)

            if exact_u > 1e-12:
                gap = (
                    (exact_u - lag_u)
                    / exact_u
                    * 100.0
                )
            elif abs(lag_u) <= 1e-12:
                gap = 0.0
            else:
                raise RuntimeError(
                    "Invalid zero-utility exact solution"
                )

            if gap < -1e-7:
                raise RuntimeError(
                    f"Negative optimality gap: "
                    f"N={n}, rep={rep}, gap={gap}"
                )

            ratio = exact_ms / max(lag_ms, 1e-12)

            row = {
                "candidate_count": n,
                "instance": rep,
                "lag_utility": lag_u,
                "exact_utility": exact_u,
                "gap_pct": gap,
                "lag_runtime_ms": lag_ms,
                "exact_runtime_ms": exact_ms,
                "exact_over_lag_runtime_ratio": ratio,
                "lag_selected": len(lag),
                "exact_selected": len(exact),
                "lag_size_mb": lag_size,
                "exact_size_mb": exact_size,
            }

            writer.writerow(row)
            f.flush()

            summary_data[n].append(row)

            print(
                f"N={n:3d} "
                f"rep={rep + 1:02d}/{INSTANCE_REPEATS} "
                f"gap={gap:8.4f}% "
                f"lag={lag_ms:9.3f}ms "
                f"exact={exact_ms:9.3f}ms "
                f"ratio={ratio:7.3f}",
                flush=True,
            )

# ============================================================
# 6. Summary
# ============================================================

summary_path = out_dir / "summary.csv"

summary_fields = [
    "candidate_count",
    "instances",
    "mean_gap_pct",
    "sd_gap_pct",
    "min_gap_pct",
    "max_gap_pct",
    "median_lag_runtime_ms",
    "median_exact_runtime_ms",
    "median_exact_over_lag_ratio",
    "mean_lag_selected",
    "mean_exact_selected",
]

with summary_path.open(
    "w",
    newline="",
    encoding="utf-8",
) as f:

    writer = csv.DictWriter(
        f,
        fieldnames=summary_fields,
    )

    writer.writeheader()

    for n in CANDIDATE_COUNTS:

        rows = summary_data[n]

        gaps = [float(x["gap_pct"]) for x in rows]
        lag_times = [
            float(x["lag_runtime_ms"]) for x in rows
        ]
        exact_times = [
            float(x["exact_runtime_ms"]) for x in rows
        ]
        ratios = [
            float(x["exact_over_lag_runtime_ratio"])
            for x in rows
        ]

        writer.writerow({
            "candidate_count": n,
            "instances": len(rows),
            "mean_gap_pct": statistics.mean(gaps),
            "sd_gap_pct": statistics.stdev(gaps),
            "min_gap_pct": min(gaps),
            "max_gap_pct": max(gaps),
            "median_lag_runtime_ms": statistics.median(lag_times),
            "median_exact_runtime_ms": statistics.median(exact_times),
            "median_exact_over_lag_ratio": statistics.median(ratios),
            "mean_lag_selected": statistics.mean(
                x["lag_selected"] for x in rows
            ),
            "mean_exact_selected": statistics.mean(
                x["exact_selected"] for x in rows
            ),
        })

print("\n" + "=" * 65)
print("[DONE] PULSE OPTIMALITY GAP BENCHMARK")
print("RAW:", raw_path.resolve())
print("SUMMARY:", summary_path.resolve())
print("METADATA:", (out_dir / "metadata.json").resolve())
print("ROOT:", out_dir.resolve())
print("=" * 65)
