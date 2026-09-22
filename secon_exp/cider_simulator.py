from __future__ import annotations

import math
import statistics
import time
from collections import Counter

from secon_exp.simulator import fair_rates
from secon_exp.cider.pulse import PulseScheduler
from secon_exp.cider.scout import ScoutOptimizer
from secon_exp.cider.keep import KeepOptimizer


EPS = 1e-9


def percentile(values, q):
    if not values:
        return 0.0

    xs = sorted(values)

    if len(xs) == 1:
        return xs[0]

    pos = (
        (len(xs) - 1) * q
    )

    lo = int(
        math.floor(pos)
    )

    hi = int(
        math.ceil(pos)
    )

    if lo == hi:
        return xs[lo]

    f = pos - lo

    return (
        xs[lo] * (1 - f)
        + xs[hi] * f
    )


def weighted_percentile(
    samples,
    q,
):
    xs = [
        (float(v), float(w))
        for v, w in samples
        if w > EPS
    ]

    if not xs:
        return 0.0

    xs.sort(
        key=lambda x: x[0]
    )

    total = sum(
        w for _, w in xs
    )

    target = (
        q * total
    )

    acc = 0.0

    for value, weight in xs:

        acc += weight

        if (
            acc + EPS
            >= target
        ):
            return value

    return xs[-1][0]


def simulate_cider(
    case,
    placement,
    topology,
    history_probability,

    pulse_budget_mb=2048.0,
    pulse_max_transfers=0,
    pulse_quantum_mb=8.0,

    scout_alpha=1.0,
    scout_beta=0.02,
    scout_gamma=1.0,
    scout_source_concurrency=0,

    keep_v=1.0,
    keep_registry_budget_rate_mb_s=3.0,
):
    """
    Strict CIDER scheduling semantics:

      Cycle t
        1. Construct ALL missing (node, layer)
        2. PULSE selects D(t)
        3. SCOUT jointly assigns sources
        4. Execute the WHOLE batch
        5. KEEP performs batch retention
        6. Update Lyapunov queue
        7. Enter next cycle
    """

    sizes = {
        str(k): float(v)
        for k, v in case[
            "layer_sizes_mb"
        ].items()
    }

    nodes = {
        n["eid"]: n
        for n in case[
            "nodes"
        ]
    }

    containers = {
        c["cid"]: c
        for c in case[
            "containers"
        ]
    }

    groups = {
        node: list(
            placement.get(
                node,
                [],
            )
        )
        for node in nodes
    }

    assigned = set()

    for cids in (
        groups.values()
    ):
        assigned.update(
            cids
        )

    accepted = len(
        assigned
    )

    excluded = (
        len(containers)
        - accepted
    )

    tasks = {
        cid: set(
            containers[cid][
                "layers"
            ]
        )
        for cid in assigned
    }

    need = {}

    for node in nodes:

        required = set()

        for cid in groups[node]:
            required.update(
                tasks[cid]
            )

        need[node] = required

    acquired = {}
    relay_cache = {}

    for node in nodes:

        initial = set(
            nodes[node].get(
                "initial_cache",
                [],
            )
        )

        initial &= set(
            sizes
        )

        acquired[node] = set(
            initial
        )

        relay_cache[node] = set(
            initial
        )

    cache_capacity = {
        node: float(
            nodes[node].get(
                "repo_capacity_mb",
                nodes[node].get(
                    "cache_capacity_mb",
                    float("inf"),
                ),
            )
        )
        for node in nodes
    }

    ready = {}

    def update_ready(now):

        for node in nodes:

            for cid in (
                groups[node]
            ):

                if cid in ready:
                    continue

                if (
                    tasks[cid]
                    <= acquired[node]
                ):
                    ready[cid] = (
                        now
                    )

    update_ready(
        0.0
    )

    pulse = PulseScheduler(
        quantum_mb=(
            pulse_quantum_mb
        )
    )

    scout = ScoutOptimizer(
        alpha=scout_alpha,
        beta=scout_beta,
        gamma=scout_gamma,
        source_concurrency=(
            scout_source_concurrency
        ),
    )

    keep = KeepOptimizer(
        history_probability=(
            history_probability
        ),
        V=keep_v,
        registry_budget_rate_mb_s=(
            keep_registry_budget_rate_mb_s
        ),
        quantum_mb=8.0,
    )

    now = 0.0

    registry_mb = 0.0
    peer_mb = 0.0

    same_domain_peer_mb = 0.0
    cross_domain_peer_mb = 0.0

    evicted_mb = 0.0
    not_preserved_mb = 0.0

    transfer_times = []

    scheduling_cycles = 0
    pulse_selected_total = 0

    pulse_ms = 0.0
    scout_ms = 0.0
    keep_ms = 0.0

    link_bytes = {
        lid: 0.0
        for lid
        in topology.capacity
    }

    link_peak = {
        lid: 0.0
        for lid
        in topology.capacity
    }

    link_busy = {
        lid: 0.0
        for lid
        in topology.capacity
    }

    util_samples = []

    wan_busy_time_s = 0.0

    while (
        len(ready)
        < accepted
    ):
        scheduling_cycles += 1

        remaining_pairs = sum(
            len(
                need[node]
                - acquired[node]
            )
            for node in nodes
        )

        if remaining_pairs <= 0:
            break

        # -------------------------------------
        # 1. PULSE
        # -------------------------------------

        k = (
            int(
                pulse_max_transfers
            )
        )

        if k <= 0:
            # K(t): current scheduling window
            # transmission concurrency budget.
            #
            # Use N slots by default, but unlike
            # previous implementation multiple
            # items may belong to the same node.
            k = len(nodes)

        t0 = (
            time.perf_counter()
        )

        selected = pulse.select(
            nodes=list(nodes),
            groups=groups,
            tasks=tasks,
            acquired=acquired,
            need=need,
            sizes=sizes,
            budget_mb=(
                pulse_budget_mb
            ),
            max_transfers=k,
        )

        pulse_ms += (
            time.perf_counter()
            - t0
        ) * 1000.0

        if not selected:
            raise RuntimeError(
                "PULSE selected no layer "
                f"with {remaining_pairs} "
                "missing demands remaining. "
                "Increase pulse budget."
            )

        pulse_selected_total += (
            len(selected)
        )

        # -------------------------------------
        # 2. SCOUT
        # -------------------------------------

        t0 = (
            time.perf_counter()
        )

        assignment = scout.choose(
            demands=selected,
            relay_cache=relay_cache,
            topology=topology,
            active=[],
        )

        scout_ms += (
            time.perf_counter()
            - t0
        ) * 1000.0

        # -------------------------------------
        # 3. Build complete batch.
        # -------------------------------------

        active = []
        pending = []

        batch_registry_mb = 0.0

        newly_arrived = {
            node: set()
            for node in nodes
        }

        batch_start = now

        for item in selected:

            src = assignment[
                (
                    item.node,
                    item.layer,
                )
            ]

            if src is None:
                path = (
                    topology.registry_path(
                        item.node
                    )
                )
            else:
                path = (
                    topology.peer_path(
                        src,
                        item.node,
                    )
                )

            latency_s = (
                topology.path_latency_ms(
                    path
                )
                / 1000.0
            )

            flow = {
                "dst": item.node,
                "src": src,
                "layer": item.layer,
                "size": item.size_mb,
                "remaining": item.size_mb,
                "path": tuple(path),
                "created_at": now,
                "ready_at": (
                    now + latency_s
                ),
            }

            if latency_s <= EPS:
                active.append(
                    flow
                )
            else:
                pending.append(
                    flow
                )

        # -------------------------------------
        # 4. Execute WHOLE batch.
        #
        # No new PULSE scheduling is allowed
        # before this batch becomes empty.
        # -------------------------------------

        while (
            active or pending
        ):
            newly_active = [
                f
                for f in pending
                if (
                    f["ready_at"]
                    <= now + EPS
                )
            ]

            for f in newly_active:
                pending.remove(f)
                active.append(f)

            if not active:

                now = min(
                    f["ready_at"]
                    for f in pending
                )

                continue

            rates = fair_rates(
                [
                    f["path"]
                    for f in active
                ],
                topology.capacity,
            )

            dt = min(
                f["remaining"]
                / max(
                    rates[i],
                    EPS,
                )
                for i, f
                in enumerate(active)
            )

            if pending:

                next_activation = min(
                    f["ready_at"]
                    for f
                    in pending
                )

                delta = (
                    next_activation
                    - now
                )

                if delta > EPS:
                    dt = min(
                        dt,
                        delta,
                    )

            if dt <= EPS:
                now += EPS
                continue

            link_rate = Counter()

            for i, f in (
                enumerate(active)
            ):
                for lid in f["path"]:

                    link_rate[lid] += (
                        rates[i]
                    )

            for lid, cap in (
                topology.capacity.items()
            ):
                util = min(
                    1.0,
                    link_rate.get(
                        lid,
                        0.0,
                    )
                    / cap,
                )

                link_peak[lid] = max(
                    link_peak[lid],
                    util,
                )

                util_samples.append(
                    (
                        util,
                        dt,
                    )
                )

                if util > EPS:
                    link_busy[lid] += (
                        dt
                    )

            if (
                link_rate.get(
                    "wan",
                    0.0,
                )
                > EPS
            ):
                wan_busy_time_s += (
                    dt
                )

            for i, f in (
                enumerate(active)
            ):
                amount = min(
                    f["remaining"],
                    rates[i] * dt,
                )

                f["remaining"] -= (
                    amount
                )

                for lid in f["path"]:
                    link_bytes[lid] += (
                        amount
                    )

            now += dt

            completed = [
                f
                for f in active
                if (
                    f["remaining"]
                    <= EPS
                )
            ]

            for f in completed:

                active.remove(f)

                dst = f["dst"]
                src = f["src"]
                layer = f["layer"]
                size = f["size"]

                acquired[dst].add(
                    layer
                )

                newly_arrived[
                    dst
                ].add(
                    layer
                )

                transfer_times.append(
                    now
                    - f["created_at"]
                )

                if src is None:

                    registry_mb += (
                        size
                    )

                    batch_registry_mb += (
                        size
                    )

                else:
                    peer_mb += (
                        size
                    )

                    if topology.same_domain(
                        src,
                        dst,
                    ):
                        same_domain_peer_mb += (
                            size
                        )
                    else:
                        cross_domain_peer_mb += (
                            size
                        )

                # Container readiness can become
                # true immediately when a layer
                # completes, even though the next
                # scheduling cycle waits for the
                # full batch.
                update_ready(
                    now
                )

        batch_duration = (
            now - batch_start
        )

        # -------------------------------------
        # 5. KEEP
        #
        # One unified retention decision per
        # node AFTER the whole batch.
        # -------------------------------------

        t0 = (
            time.perf_counter()
        )

        for node in nodes:

            if not newly_arrived[node]:
                continue

            old_cache = set(
                relay_cache[node]
            )

            candidates = (
                old_cache
                | newly_arrived[node]
            )

            selected_cache = (
                keep.choose_retention(
                    node=node,
                    candidate_layers=(
                        candidates
                    ),
                    capacity_mb=(
                        cache_capacity[
                            node
                        ]
                    ),
                    nodes=list(nodes),
                    relay_cache=(
                        relay_cache
                    ),
                    topology=topology,
                    sizes=sizes,
                )
            )

            removed = (
                old_cache
                - selected_cache
            )

            evicted_mb += sum(
                sizes[x]
                for x in removed
            )

            not_preserved_mb += sum(
                sizes[x]
                for x in (
                    newly_arrived[node]
                    - selected_cache
                )
            )

            relay_cache[node] = (
                selected_cache
            )

            used = sum(
                sizes[x]
                for x
                in relay_cache[node]
            )

            if (
                used
                > cache_capacity[node]
                + 1e-7
            ):
                raise RuntimeError(
                    f"KEEP overflow "
                    f"{node}: "
                    f"{used} > "
                    f"{cache_capacity[node]}"
                )

        keep_ms += (
            time.perf_counter()
            - t0
        ) * 1000.0

        # -------------------------------------
        # 6. Lyapunov Q(t+1)
        # -------------------------------------

        keep.update_virtual_queue(
            registry_mb=(
                batch_registry_mb
            ),
            duration_s=max(
                batch_duration,
                EPS,
            ),
        )

    ready_values = list(
        ready.values()
    )

    mean_ready = (
        statistics.mean(
            ready_values
        )
        if ready_values
        else 0.0
    )

    p95_ready = percentile(
        ready_values,
        0.95,
    )

    makespan = (
        max(ready_values)
        if ready_values
        else 0.0
    )

    total_transfer = (
        registry_mb
        + peer_mb
    )

    offload = (
        100.0
        * peer_mb
        / max(
            total_transfer,
            EPS,
        )
    )

    return {
        "policy": "cider",

        "accepted": accepted,
        "excluded": excluded,

        "mean_ready_s": (
            mean_ready
        ),
        "p95_ready_s": (
            p95_ready
        ),
        "makespan_s": (
            makespan
        ),

        "registry_mb": (
            registry_mb
        ),
        "peer_mb": (
            peer_mb
        ),
        "total_transfer_mb": (
            total_transfer
        ),

        "same_domain_peer_mb": (
            same_domain_peer_mb
        ),
        "cross_domain_peer_mb": (
            cross_domain_peer_mb
        ),

        "p2p_offload_pct": (
            offload
        ),

        "peak_link_utilization": max(
            link_peak.values(),
            default=0.0,
        ),

        "p95_link_utilization": (
            weighted_percentile(
                util_samples,
                0.95,
            )
        ),

        "wan_busy_time_s": (
            wan_busy_time_s
        ),

        "avg_layer_transfer_s": (
            statistics.mean(
                transfer_times
            )
            if transfer_times
            else 0.0
        ),

        "evicted_mb": (
            evicted_mb
        ),

        "not_preserved_mb": (
            not_preserved_mb
        ),

        "pulse_ms": (
            pulse_ms
        ),
        "scout_ms": (
            scout_ms
        ),
        "keep_ms": (
            keep_ms
        ),

        "scheduler_runtime_ms": (
            pulse_ms
            + scout_ms
            + keep_ms
        ),

        "scheduling_cycles": (
            scheduling_cycles
        ),

        "pulse_selected_total": (
            pulse_selected_total
        ),

        "keep_virtual_queue": (
            keep.virtual_queue
        ),

        "simulation_end_s": (
            now
        ),

        "link_bytes": (
            link_bytes
        ),

        "link_busy_time_s": (
            link_busy
        ),

        "link_peak_utilization": (
            link_peak
        ),
    }
