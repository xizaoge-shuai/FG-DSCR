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
# 配置
# ============================================================

STATE_POINTER = Path(
    "results/LATEST_FORMAL_DENSE_CORRECTED"
)

CASE_NAME = (
    "cider_nodes40_domains8_req800_weak"
)

QUANTUM_MB = 8.0
BUDGET_MB = 2048.0

# N=40 默认全局传输槽位
MAX_TRANSFERS = 40

CANDIDATE_COUNTS = [
    10,
    20,
    30,
    40,
    50,
    60,
    70,
    80,
    90,
    100,
]

# 每个 problem size 使用多个确定性子实例
INSTANCE_REPEATS = 10

# runtime 重复测量次数
TIMING_REPEATS = 5

SEED = 1


# ============================================================
# 帮助函数
# ============================================================

def total_utility(items):
    return sum(
        float(x.utility)
        for x in items
    )


def total_size(items):
    return sum(
        float(x.size_mb)
        for x in items
    )


def quantize_item(item):
    """
    两个 solver 必须使用完全相同的量化实例。

    select_exact 内部使用：
        ceil(size / quantum)

    因此这里提前把 size 映射到相同 quantum，
    然后 Lagrangian 也使用这个 size。
    """
    qsize = (
        math.ceil(
            float(item.size_mb)
            / QUANTUM_MB
        )
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
            (
                time.perf_counter()
                - t0
            )
            * 1000.0
        )

    return (
        result,
        statistics.median(times),
    )


# ============================================================
# 读取真实 N=40 workload / placement
# ============================================================

formal_root = Path(
    STATE_POINTER
    .read_text()
    .strip()
)

state_path = (
    formal_root
    / "scale"
    / "baselines"
    / "states"
    / (
        CASE_NAME
        + "__seed1.json"
    )
)

if not state_path.exists():
    raise FileNotFoundError(
        state_path
    )

state = json.loads(
    state_path.read_text(
        encoding="utf-8"
    )
)

case = state["target_case"]
placement = state["placement"]


# ============================================================
# 构造真实 first-cycle PULSE candidate pool
# ============================================================

sizes = {
    k: float(v)
    for k, v
    in case[
        "layer_sizes_mb"
    ].items()
}

tasks = {
    c["cid"]: set(c["layers"])
    for c in case["containers"]
}

nodes = [
    n["eid"]
    for n in case["nodes"]
]

groups = {
    node: list(
        placement.get(
            node,
            []
        )
    )
    for node in nodes
}


# 初始 reusable cache 作为已获得 layer
acquired = {}

for n in case["nodes"]:
    eid = n["eid"]

    acquired[eid] = set(
        n.get(
            "initial_cache",
            []
        )
    )


need = {}

for node in nodes:
    union = set()

    for cid in groups[node]:
        union.update(
            tasks[cid]
        )

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


print(
    "REAL CANDIDATE POOL =",
    len(pool)
)

if len(pool) < min(
    CANDIDATE_COUNTS
):
    raise RuntimeError(
        "Candidate pool is too small"
    )


# ============================================================
# 为两个 solver 构造完全一致的量化问题
# ============================================================

pool = [
    quantize_item(x)
    for x in pool
]

budget_q = (
    math.floor(
        BUDGET_MB
        / QUANTUM_MB
    )
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
# 输出目录
# ============================================================

stamp = time.strftime(
    "%Y%m%d_%H%M%S"
)

out_dir = Path(
    "results"
) / (
    "pulse_optimality_gap_"
    + stamp
)

out_dir.mkdir(
    parents=True,
    exist_ok=True,
)

pointer = Path(
    "results/"
    "LATEST_PULSE_OPTIMALITY_GAP"
)

pointer.write_text(
    str(out_dir),
    encoding="utf-8",
)

raw_path = (
    out_dir
    / "raw.csv"
)


fields = [
    "candidate_count",
    "instance",
    "lag_utility",
    "exact_utility",
    "gap_pct",
    "lag_runtime_ms",
    "exact_runtime_ms",
    "speedup_exact_over_lag",
    "lag_selected",
    "exact_selected",
    "lag_size_mb",
    "exact_size_mb",
]


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

        if n > len(pool):
            print(
                f"SKIP n={n}: "
                f"pool={len(pool)}"
            )
            continue

        for rep in range(
            INSTANCE_REPEATS
        ):

            rng = random.Random(
                SEED
                + n * 1000
                + rep
            )

            indices = rng.sample(
                range(len(pool)),
                n,
            )

            items = [
                pool[i]
                for i in indices
            ]

            max_transfers = min(
                MAX_TRANSFERS,
                n,
            )

            lag, lag_ms = run_timed(
                lambda:
                lag_solver
                .select_lagrangian(
                    items,
                    budget_q,
                    max_transfers,
                )
            )

            exact, exact_ms = run_timed(
                lambda:
                exact_solver
                .select_exact(
                    items,
                    budget_q,
                    max_transfers,
                )
            )

            lag_u = total_utility(
                lag
            )

            exact_u = total_utility(
                exact
            )

            if exact_u <= 1e-12:
                gap = 0.0
            else:
                gap = (
                    exact_u
                    - lag_u
                ) / exact_u * 100.0

            # 数值误差保护
            if (
                gap < 0
                and abs(gap) < 1e-9
            ):
                gap = 0.0

            writer.writerow({
                "candidate_count":
                    n,

                "instance":
                    rep,

                "lag_utility":
                    lag_u,

                "exact_utility":
                    exact_u,

                "gap_pct":
                    gap,

                "lag_runtime_ms":
                    lag_ms,

                "exact_runtime_ms":
                    exact_ms,

                "speedup_exact_over_lag":
                    (
                        exact_ms
                        / max(
                            lag_ms,
                            1e-12,
                        )
                    ),

                "lag_selected":
                    len(lag),

                "exact_selected":
                    len(exact),

                "lag_size_mb":
                    total_size(lag),

                "exact_size_mb":
                    total_size(exact),
            })

            print(
                f"N={n:<3d} "
                f"rep={rep:<2d} "
                f"gap={gap:8.4f}% "
                f"lag={lag_ms:9.4f} ms "
                f"exact={exact_ms:9.4f} ms"
            )


print()
print("SAVED:", raw_path)
print("ROOT :", out_dir)
