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

    pos = (len(xs) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))

    if lo == hi:
        return xs[lo]

    frac = pos - lo

    return (
        xs[lo] * (1.0 - frac)
        + xs[hi] * frac
    )


def weighted_percentile(samples, q):
    xs = [
        (float(v), float(w))
        for v, w in samples
        if w > EPS
    ]

    if not xs:
        return 0.0

    xs.sort(key=lambda x: x[0])

    total = sum(w for _, w in xs)
    target = q * total

    acc = 0.0

    for value, weight in xs:
        acc += weight

        if acc + EPS >= target:
            return value

    return xs[-1][0]


def simulate_cider(
    case,
    placement,
    topology,

    pulse_budget_mb=2048.0,
    pulse_max_transfers=0,
    pulse_quantum_mb=8.0,

    scout_source_concurrency=0,
    scout_registry_penalty=0.02,
    scout_cross_domain_penalty=0.005,
    scout_congestion_penalty=1.0,

    keep_v=1.0,
    keep_registry_budget_rate_mb_s=3.0,
):
    """
    CIDER workflow:

        missing layer demands
               |
             PULSE
        what to transmit
               |
             SCOUT
        where to retrieve
               |
        network transfer
               |
              KEEP
        future source preservation
               |
         next scheduling cycle
    """

    algorithm_time = {
        "pulse_ms": 0.0,
        "scout_ms": 0.0,
        "keep_ms": 0.0,
    }

    sizes = {
        str(k): float(v)
        for k, v in case[
            "layer_sizes_mb"
        ].items()
    }

    nodes = {
        n["eid"]: n
        for n in case["nodes"]
    }

    containers = {
        c["cid"]: c
        for c in case["containers"]
    }

    groups = {
        node: list(
            placement.get(node, [])
        )
        for node in nodes
    }

    assigned = set()

    for cids in groups.values():
        assigned.update(cids)

    accepted = len(assigned)

    excluded = (
        len(containers)
        - accepted
    )

    # ---------------------------------------------
    # Container -> unique layer set
    # ---------------------------------------------

    tasks = {}

    for cid in assigned:
        tasks[cid] = set(
            containers[cid]["layers"]
        )

    popularity = Counter()

    for layers in tasks.values():
        for layer in layers:
            popularity[layer] += 1

    # ---------------------------------------------
    # Per-node layer demand
    # ---------------------------------------------

    need = {}

    for node in nodes:
        required = set()

        for cid in groups[node]:
            required.update(
                tasks[cid]
            )

        need[node] = required

    # ---------------------------------------------
    # Layer states
    #
    # acquired:
    #   deployment can use the layer.
    #
    # relay_cache:
    #   layer is physically preserved and may
    #   serve as a future communication source.
    # ---------------------------------------------

    acquired = {}
    relay_cache = {}

    for node in nodes:

        initial = set(
            nodes[node].get(
                "initial_cache",
                [],
            )
        )

        initial &= set(sizes)

        acquired[node] = set(initial)
        relay_cache[node] = set(initial)

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

    # ---------------------------------------------
    # Container readiness
    # ---------------------------------------------

    ready = {}

    def update_ready(now):
        for node in nodes:
            for cid in groups[node]:

                if cid in ready:
                    continue

                if (
                    tasks[cid]
                    <= acquired[node]
                ):
                    ready[cid] = now

    update_ready(0.0)

    # ---------------------------------------------
    # CIDER modules
    # ---------------------------------------------

    pulse = PulseScheduler(
        quantum_mb=pulse_quantum_mb,
        max_candidates_per_node=8,
    )

    scout = ScoutOptimizer(
        registry_penalty=(
            scout_registry_penalty
        ),
        cross_domain_penalty=(
            scout_cross_domain_penalty
        ),
        congestion_penalty=(
            scout_congestion_penalty
        ),
        source_concurrency=(
            scout_source_concurrency
        ),
    )

    keep = KeepOptimizer(
        V=keep_v,
        registry_budget_rate_mb_s=(
            keep_registry_budget_rate_mb_s
        ),
        quantum_mb=8.0,
    )

    # ---------------------------------------------
    # Network flows
    # ---------------------------------------------

    active = []
    pending = []

    now = 0.0

    registry_mb = 0.0
    peer_mb = 0.0

    same_domain_peer_mb = 0.0
    cross_domain_peer_mb = 0.0

    not_preserved_mb = 0.0
    evicted_mb = 0.0

    transfer_times = []

    link_bytes = {
        lid: 0.0
        for lid in topology.capacity
    }

    link_peak_utilization = {
        lid: 0.0
        for lid in topology.capacity
    }

    link_busy_time = {
        lid: 0.0
        for lid in topology.capacity
    }

    utilization_samples = []

    wan_busy_time_s = 0.0

    # ---------------------------------------------
    # KEEP helpers
    # ---------------------------------------------

    def pinned_layers(node):
        result = set()

        for f in active + pending:
            if (
                f["src"] == node
            ):
                result.add(
                    f["layer"]
                )

        return result

    def preserve_layer(
        node,
        new_layer,
    ):
        """
        KEEP is triggered by storage pressure.

        If capacity is still available, keeping an
        additional communication source cannot hurt
        the feasible source set, so no unnecessary
        proactive deletion is performed.
        """

        nonlocal evicted_mb
        nonlocal not_preserved_mb

        if (
            new_layer
            in relay_cache[node]
        ):
            return

        old_cache = set(
            relay_cache[node]
        )

        current_bytes = sum(
            sizes[layer]
            for layer in old_cache
        )

        new_size = sizes[
            new_layer
        ]

        # No storage pressure: simply preserve it.
        if (
            current_bytes
            + new_size
            <= cache_capacity[node]
            + EPS
        ):
            relay_cache[node].add(
                new_layer
            )
            return

        t0 = time.perf_counter()

        pins = pinned_layers(
            node
        )

        pinned_bytes = sum(
            sizes[layer]
            for layer in pins
        )

        if (
            pinned_bytes
            > cache_capacity[node]
            + EPS
        ):
            raise RuntimeError(
                f"Pinned layers exceed cache "
                f"capacity at {node}"
            )

        remaining_capacity = (
            cache_capacity[node]
            - pinned_bytes
        )

        candidates = (
            old_cache
            | {new_layer}
        ) - pins

        remaining_need = {
            dst: (
                need[dst]
                - acquired[dst]
            )
            for dst in nodes
        }

        selected = (
            keep.choose_retention(
                node=node,
                candidate_layers=(
                    candidates
                ),
                capacity_mb=(
                    remaining_capacity
                ),
                nodes=nodes,
                relay_cache=(
                    relay_cache
                ),
                need=remaining_need,
                topology=topology,
                sizes=sizes,
                popularity=popularity,
            )
        )

        new_cache = (
            set(pins)
            | set(selected)
        )

        used = sum(
            sizes[layer]
            for layer in new_cache
        )

        if (
            used
            > cache_capacity[node]
            + 1e-7
        ):
            raise RuntimeError(
                f"KEEP cache overflow "
                f"node={node}: "
                f"{used} > "
                f"{cache_capacity[node]}"
            )

        removed = (
            old_cache
            - new_cache
        )

        evicted_mb += sum(
            sizes[layer]
            for layer in removed
        )

        if (
            new_layer
            not in new_cache
        ):
            not_preserved_mb += (
                new_size
            )

        relay_cache[node] = (
            new_cache
        )

        algorithm_time[
            "keep_ms"
        ] += (
            time.perf_counter()
            - t0
        ) * 1000.0

    # ---------------------------------------------
    # PULSE + SCOUT scheduling cycle
    # ---------------------------------------------

    scheduling_cycles = 0
    pulse_selected_total = 0

    def schedule_cycle():
        nonlocal scheduling_cycles
        nonlocal pulse_selected_total

        busy_dst = {
            f["dst"]
            for f in (
                active + pending
            )
        }

        idle_nodes = [
            node
            for node in nodes
            if (
                node not in busy_dst
                and (
                    need[node]
                    - acquired[node]
                )
            )
        ]

        if not idle_nodes:
            return 0

        # Ensure the communication budget can admit
        # at least one currently missing layer.
        min_missing_size = min(
            sizes[layer]
            for node in idle_nodes
            for layer in (
                need[node]
                - acquired[node]
            )
        )

        effective_budget = max(
            pulse_budget_mb,
            math.ceil(
                min_missing_size
                / pulse_quantum_mb
            )
            * pulse_quantum_mb,
        )

        max_transfers = (
            len(idle_nodes)
            if pulse_max_transfers <= 0
            else min(
                pulse_max_transfers,
                len(idle_nodes),
            )
        )

        # -------------------------
        # PULSE
        # -------------------------

        t0 = time.perf_counter()

        selected = pulse.select(
            idle_nodes=idle_nodes,
            groups=groups,
            tasks=tasks,
            acquired=acquired,
            need=need,
            sizes=sizes,
            budget_mb=effective_budget,
            max_transfers=max_transfers,
        )

        algorithm_time[
            "pulse_ms"
        ] += (
            time.perf_counter()
            - t0
        ) * 1000.0

        if not selected:
            return 0

        # -------------------------
        # SCOUT
        # -------------------------

        t0 = time.perf_counter()

        assignment = scout.choose(
            demands=selected,
            relay_cache=relay_cache,
            topology=topology,
            active=(
                active + pending
            ),
            sizes=sizes,
        )

        algorithm_time[
            "scout_ms"
        ] += (
            time.perf_counter()
            - t0
        ) * 1000.0

        started = 0

        for item in selected:

            dst = item.node
            layer = item.layer

            source = assignment.get(
                (
                    dst,
                    layer,
                )
            )

            if source is None:
                path = (
                    topology.registry_path(
                        dst
                    )
                )
            else:
                path = (
                    topology.peer_path(
                        source,
                        dst,
                    )
                )

            latency_s = (
                topology
                .path_latency_ms(path)
                / 1000.0
            )

            flow = {
                "dst": dst,
                "src": source,
                "layer": layer,
                "size": sizes[layer],
                "remaining": sizes[layer],
                "path": tuple(path),
                "created_at": now,
                "ready_at": (
                    now + latency_s
                ),
            }

            if latency_s <= EPS:
                active.append(flow)
            else:
                pending.append(flow)

            started += 1

        scheduling_cycles += 1
        pulse_selected_total += (
            len(selected)
        )

        return started

    # ---------------------------------------------
    # Event-driven simulation
    # ---------------------------------------------

    schedule_cycle()

    safety = 0

    while (
        len(ready)
        < accepted
    ):
        safety += 1

        if safety > 10_000_000:
            raise RuntimeError(
                "CIDER simulation exceeded "
                "safety limit"
            )

        # Activate propagation-complete flows.
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

        # Fill currently available transfer slots.
        schedule_cycle()

        if not active:

            if pending:
                now = min(
                    f["ready_at"]
                    for f in pending
                )
                continue

            # No active/pending flow but requests
            # remain -> this would be a scheduler bug.
            remaining = sum(
                len(
                    need[node]
                    - acquired[node]
                )
                for node in nodes
            )

            raise RuntimeError(
                "CIDER deadlock: "
                f"{remaining} layers remain"
            )

        paths = [
            f["path"]
            for f in active
        ]

        rates = fair_rates(
            paths,
            topology.capacity,
        )

        dt_completion = min(
            f["remaining"]
            / max(
                rates[i],
                EPS,
            )
            for i, f in enumerate(
                active
            )
        )

        dt = dt_completion

        if pending:
            next_activation = min(
                f["ready_at"]
                for f in pending
            )

            until_activation = (
                next_activation - now
            )

            if until_activation > EPS:
                dt = min(
                    dt,
                    until_activation,
                )

        if dt <= EPS:
            now += EPS
            continue

        # -----------------------------------------
        # Link utilization for this event interval
        # -----------------------------------------

        link_rate = Counter()

        for i, f in enumerate(active):

            rate = rates[i]

            for lid in f["path"]:
                link_rate[lid] += (
                    rate
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

            link_peak_utilization[
                lid
            ] = max(
                link_peak_utilization[
                    lid
                ],
                util,
            )

            utilization_samples.append(
                (
                    util,
                    dt,
                )
            )

            if util > EPS:
                link_busy_time[
                    lid
                ] += dt

        if (
            link_rate.get(
                "wan",
                0.0,
            )
            > EPS
        ):
            wan_busy_time_s += dt

        # -----------------------------------------
        # Transfer bytes
        # -----------------------------------------

        registry_interval_mb = 0.0

        for i, f in enumerate(active):

            amount = min(
                f["remaining"],
                rates[i] * dt,
            )

            f["remaining"] -= (
                amount
            )

            if f["src"] is None:
                registry_interval_mb += (
                    amount
                )

            for lid in f["path"]:
                link_bytes[lid] += (
                    amount
                )

        # Lyapunov queue is updated using actual
        # Registry bytes transferred during this
        # event interval.
        keep.update_virtual_queue(
            registry_mb=(
                registry_interval_mb
            ),
            duration_s=dt,
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

        if not completed:
            continue

        for f in completed:
            active.remove(f)

            dst = f["dst"]
            src = f["src"]
            layer = f["layer"]
            size = f["size"]

            acquired[dst].add(
                layer
            )

            transfer_times.append(
                now
                - f["created_at"]
            )

            if src is None:
                registry_mb += size
            else:
                peer_mb += size

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

            # KEEP:
            # decide whether this completed layer
            # remains a future communication source.
            preserve_layer(
                dst,
                layer,
            )

        update_ready(now)

    # ---------------------------------------------
    # Results
    # ---------------------------------------------

    ready_values = list(
        ready.values()
    )

    mean_ready_s = (
        statistics.mean(
            ready_values
        )
        if ready_values
        else 0.0
    )

    p95_ready_s = percentile(
        ready_values,
        0.95,
    )

    makespan_s = (
        max(ready_values)
        if ready_values
        else 0.0
    )

    total_transfer_mb = (
        registry_mb
        + peer_mb
    )

    p2p_offload_pct = (
        100.0
        * peer_mb
        / max(
            total_transfer_mb,
            EPS,
        )
    )

    peak_link_utilization = max(
        link_peak_utilization.values(),
        default=0.0,
    )

    p95_link_utilization = (
        weighted_percentile(
            utilization_samples,
            0.95,
        )
    )

    avg_layer_transfer_s = (
        statistics.mean(
            transfer_times
        )
        if transfer_times
        else 0.0
    )

    return {
        "policy": "cider",

        "accepted": accepted,
        "excluded": excluded,

        "mean_ready_s": (
            mean_ready_s
        ),
        "p95_ready_s": (
            p95_ready_s
        ),
        "makespan_s": (
            makespan_s
        ),

        "registry_mb": (
            registry_mb
        ),
        "peer_mb": (
            peer_mb
        ),
        "total_transfer_mb": (
            total_transfer_mb
        ),

        "same_domain_peer_mb": (
            same_domain_peer_mb
        ),
        "cross_domain_peer_mb": (
            cross_domain_peer_mb
        ),

        "p2p_offload_pct": (
            p2p_offload_pct
        ),

        "peak_link_utilization": (
            peak_link_utilization
        ),
        "p95_link_utilization": (
            p95_link_utilization
        ),

        "wan_busy_time_s": (
            wan_busy_time_s
        ),

        "avg_layer_transfer_s": (
            avg_layer_transfer_s
        ),

        "evicted_mb": (
            evicted_mb
        ),
        "not_preserved_mb": (
            not_preserved_mb
        ),

        "pulse_ms": (
            algorithm_time[
                "pulse_ms"
            ]
        ),
        "scout_ms": (
            algorithm_time[
                "scout_ms"
            ]
        ),
        "keep_ms": (
            algorithm_time[
                "keep_ms"
            ]
        ),

        "scheduler_runtime_ms": (
            algorithm_time[
                "pulse_ms"
            ]
            + algorithm_time[
                "scout_ms"
            ]
            + algorithm_time[
                "keep_ms"
            ]
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

        "link_bytes": (
            link_bytes
        ),
        "link_busy_time_s": (
            link_busy_time
        ),
        "link_peak_utilization": (
            link_peak_utilization
        ),

        "simulation_end_s": now,
    }
