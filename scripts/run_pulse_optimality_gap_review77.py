from __future__ import annotations

import csv
import json
import math
import random
import statistics
import time

from pathlib import Path

from secon_exp.cider.pulse import (
    PulseItem,
    PulseScheduler,
)


# ============================================================
# Fixed experiment configuration
# ============================================================

STATE_PATH = Path(
    "results/cider_review77_20261109/train/"
    "baselines/states/"
    "cider_nodes40_domains8_req800_weak__seed1.json"
)

QUANTUM_MB = 8.0
BUDGET_MB = 2048.0
MAX_TRANSFERS = 40

CANDIDATE_COUNTS = [
    10, 20, 30, 40, 50,
    60, 70, 80, 90, 100
]

INSTANCE_REPEATS = 10
TIMING_REPEATS = 5
SEED = 1


# ============================================================
# Helpers
# ============================================================

def total_utility(items):
    return sum(float(x.utility) for x in items)


def total_size(items):
    return sum(float(x.size_mb) for x in items)


def quantize_item(item):
    size = (
        math.ceil(
            float(item.size_mb) / QUANTUM_MB
        ) * QUANTUM_MB
    )

    return PulseItem(
        node=item.node,
        layer=item.layer,
        size_mb=size,
        utility=float(item.utility),
    )


def timed(fn):
    samples = []
    result = None

    for _ in range(TIMING_REPEATS):
        start = time.perf_counter()
        result = fn()

        samples.append(
            (time.perf_counter() - start) * 1000
        )

    return result, statistics.median(samples)


# ============================================================
# Load actual N=40 placement
# ============================================================

assert STATE_PATH.is_file(), STATE_PATH

state = json.loads(
    STATE_PATH.read_text(encoding="utf-8")
)

case = state["target_case"]
placement = state["placement"]

nodes = [
    n["eid"] for n in case["nodes"]
]

sizes = {
    k: float(v)
    for k, v in case["layer_sizes_mb"].items()
}

tasks = {
    c["cid"]: set(c["layers"])
    for c in case["containers"]
}

groups = {
    node: list(placement.get(node, []))
    for node in nodes
}

acquired = {
    n["eid"]: set(n.get("initial_cache", []))
    for n in case["nodes"]
}

need = {}

for node in nodes:
    union = set()

    for cid in groups[node]:
        union.update(tasks[cid])

    need[node] = union


# ============================================================
# Build real PULSE candidate pool
# ============================================================

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

print("NODES =", len(nodes))
print("TARGET REQUESTS =", len(case["containers"]))
print("REAL CANDIDATE POOL =", len(pool))

if len(pool) < 10:
    raise RuntimeError("Insufficient PULSE candidates")


pool = [
    quantize_item(x)
    for x in pool
]

lag_solver = PulseScheduler(
    quantum_mb=QUANTUM_MB,
    solver="lagrangian",
)

exact_solver = PulseScheduler(
    quantum_mb=QUANTUM_MB,
    solver="exact",
)


# ============================================================
# Output
# ============================================================

stamp = time.strftime("%Y%m%d_%H%M%S")

out = Path("results") / (
    "pulse_optimality_gap_review77_" + stamp
)

out.mkdir(parents=True, exist_ok=True)

Path(
    "results/LATEST_PULSE_OPTIMALITY_GAP"
).write_text(str(out), encoding="utf-8")

raw = out / "raw.csv"

fields = [
    "candidate_count",
    "instance",
    "lag_utility",
    "exact_utility",
    "gap_pct",
    "lag_runtime_ms",
    "exact_runtime_ms",
    "lag_selected",
    "exact_selected",
    "lag_size_mb",
    "exact_size_mb",
]

with raw.open(
    "w", newline="", encoding="utf-8"
) as f:

    writer = csv.DictWriter(
        f, fieldnames=fields
    )
    writer.writeheader()

    for n in CANDIDATE_COUNTS:

        if n > len(pool):
            print("[SKIP] candidate count:", n)
            continue

        for rep in range(INSTANCE_REPEATS):

            rng = random.Random(
                SEED + n * 1000 + rep
            )

            indices = rng.sample(
                range(len(pool)), n
            )

            items = [
                pool[i] for i in indices
            ]

            max_transfers = min(
                MAX_TRANSFERS, n
            )

            budget = BUDGET_MB

            lag, lag_ms = timed(
                lambda: lag_solver.select_lagrangian(
                    items, budget, max_transfers
                )
            )

            exact, exact_ms = timed(
                lambda: exact_solver.select_exact(
                    items, budget, max_transfers
                )
            )

            lag_u = total_utility(lag)
            exact_u = total_utility(exact)

            if lag_u > exact_u + 1e-8:
                raise RuntimeError(
                    "Lagrangian utility exceeds "
                    "the exact-DP optimum. "
                    "Check solver consistency."
                )

            gap = (
                0.0 if exact_u <= 1e-12
                else (exact_u - lag_u)
                / exact_u * 100
            )

            writer.writerow({
                "candidate_count": n,
                "instance": rep,
                "lag_utility": lag_u,
                "exact_utility": exact_u,
                "gap_pct": gap,
                "lag_runtime_ms": lag_ms,
                "exact_runtime_ms": exact_ms,
                "lag_selected": len(lag),
                "exact_selected": len(exact),
                "lag_size_mb": total_size(lag),
                "exact_size_mb": total_size(exact),
            })

            f.flush()

            print(
                f"N={n:<3d} rep={rep:<2d} "
                f"gap={gap:8.4f}% "
                f"LAG={lag_ms:9.3f}ms "
                f"EXACT={exact_ms:9.3f}ms",
                flush=True,
            )

print("PULSE EXPERIMENT COMPLETED")
print("RESULT =", raw)
print("ROOT =", out)
