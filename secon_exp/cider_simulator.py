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
    pulse_refill_ratio=0.5,

    scout_alpha=1.0,
    scout_beta=1.0,
    scout_gamma=0.5,
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

    # =========================================================
    # ARRIVAL_AWARE_CIDER_V1
    #
    # Only released requests may contribute demand.
    # Requests without arrival_time_s retain legacy t=0 behavior.
    # =========================================================

    arrival_time = {
        cid: float(
            containers[cid].get(
                "arrival_time_s",
                0.0,
            )
        )
        for cid in assigned
    }

    released = set()

    need = {
        node: set()
        for node in nodes
    }

    def release_due(now):
        newly_released = []

        for node in nodes:

            for cid in groups[node]:

                if cid in released:
                    continue

                if (
                    arrival_time[cid]
                    <= now + EPS
                ):
                    released.add(cid)

                    need[node].update(
                        tasks[cid]
                    )

                    newly_released.append(
                        cid
                    )

        return newly_released

    def next_arrival_after(now):
        future = [
            arrival_time[cid]
            for cid in assigned
            if (
                cid not in released
                and arrival_time[cid]
                > now + EPS
            )
        ]

        if not future:
            return None

        return min(future)


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

            for cid in groups[node]:

                if (
                    cid not in released
                    or cid in ready
                ):
                    continue

                if (
                    tasks[cid]
                    <= acquired[node]
                ):
                    ready[cid] = now

    # Target windows are rebased so their first
    # arrival occurs at t=0.
    release_due(0.0)
    update_ready(0.0)

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

    # KEEP realized-value diagnostics.
    #
    # retained_new_pairs:
    #   (node, layer) that was newly delivered and preserved.
    #
    # reused_retained_pairs:
    #   preserved pairs later actually selected by SCOUT
    #   as a peer source.
    #
    # rejected_pairs:
    #   newly delivered but not preserved.
    #
    # rejected_then_needed:
    #   a rejected (node, layer) whose layer is later required
    #   somewhere again.
    retained_new_pairs = set()
    reused_retained_pairs = set()
    rejected_pairs = set()
    rejected_then_needed = set()

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

    # =========================================================
    # Event-driven CIDER
    #
    # A scheduling cycle is triggered whenever transmission
    # slots become available.
    #
    # We DO NOT wait for all flows selected in the previous
    # PULSE decision to finish.
    #
    # PULSE remains a global layer-selection optimization;
    # SCOUT remains a joint source-assignment optimization;
    # KEEP is executed on layers completed at the current
    # event boundary.
    # =========================================================

    active = []
    pending = []

    inflight_pairs = set()

    # Fair global concurrency constraint shared with baselines.
    max_total_transfers = (
        int(pulse_max_transfers)
        if pulse_max_transfers > 0
        else len(nodes)
    )

    safety = 0

    while (
        len(ready)
        < accepted
    ):
        safety += 1

        if safety > 10_000_000:
            raise RuntimeError(
                "CIDER event-loop safety "
                "limit exceeded"
            )

        # -----------------------------------------------------
        # Release requests whose arrival event has fired.
        # -----------------------------------------------------

        release_due(now)
        update_ready(now)

        # -----------------------------------------------------
        # Activate flows whose propagation delay elapsed.
        # -----------------------------------------------------

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

        # -----------------------------------------------------
        # PULSE + SCOUT refill.
        # -----------------------------------------------------

        free_slots = max(
            0,
            max_total_transfers
            - len(active)
            - len(pending),
        )

        refill_threshold = max(
            1,
            int(
                math.ceil(
                    max_total_transfers
                    * pulse_refill_ratio
                )
            ),
        )

        should_refill = (
            free_slots
            >= refill_threshold
            or (
                not active
                and not pending
            )
        )

        if (
            free_slots > 0
            and should_refill
        ):

            remaining_pairs = sum(
                len(
                    need[node]
                    - acquired[node]
                )
                for node in nodes
            )

            if remaining_pairs > 0:

                # Layers already in flight are excluded from
                # the current PULSE candidate set by treating
                # them as provisionally acquired.
                acquired_for_pulse = {
                    node: set(
                        acquired[node]
                    )
                    for node in nodes
                }

                for (
                    dst,
                    layer,
                ) in inflight_pairs:
                    acquired_for_pulse[
                        dst
                    ].add(
                        layer
                    )

                t0 = time.perf_counter()

                selected = pulse.select(
                    nodes=list(nodes),
                    groups={
                        node: [
                            cid
                            for cid
                            in groups[node]
                            if cid in released
                        ]
                        for node in nodes
                    },
                    tasks=tasks,
                    acquired=(
                        acquired_for_pulse
                    ),
                    need=need,
                    sizes=sizes,
                    budget_mb=(
                        pulse_budget_mb
                    ),
                    max_transfers=(
                        free_slots
                    ),
                )

                pulse_ms += (
                    time.perf_counter()
                    - t0
                ) * 1000.0

                if selected:

                    for item in selected:

                        for (
                            rejected_node,
                            rejected_layer,
                        ) in rejected_pairs:

                            if (
                                rejected_layer
                                == item.layer
                            ):
                                rejected_then_needed.add(
                                    (
                                        rejected_node,
                                        rejected_layer,
                                    )
                                )

                    t0 = (
                        time.perf_counter()
                    )

                    assignment = (
                        scout.choose(
                            demands=selected,
                            relay_cache=(
                                relay_cache
                            ),
                            topology=topology,
                            active=(
                                active
                                + pending
                            ),
                        )
                    )

                    scout_ms += (
                        time.perf_counter()
                        - t0
                    ) * 1000.0

                    # Common transport-level coalescing.
                    #
                    # If a layer is already being fetched from
                    # Registry, another demand whose SCOUT
                    # decision is also Registry waits until the
                    # first copy becomes available.
                    #
                    # Dragonfly/PeerSync baseline already use
                    # the same behavior, so this is a simulator
                    # fairness rule rather than a CIDER novelty.
                    cloud_layers_inflight = {
                        f["layer"]
                        for f in (
                            active
                            + pending
                        )
                        if f["src"] is None
                    }

                    started_this_epoch = 0

                    for item in selected:

                        pair = (
                            item.node,
                            item.layer,
                        )

                        if (
                            pair
                            in inflight_pairs
                        ):
                            raise RuntimeError(
                                "duplicate in-flight "
                                f"demand: {pair}"
                            )

                        src = assignment[
                            pair
                        ]

                        if (
                            src is None
                            and item.layer
                            in cloud_layers_inflight
                        ):
                            # Defer this demand.
                            # It remains missing and can be
                            # reconsidered by the next PULSE
                            # scheduling epoch.
                            continue

                        if src is None:
                            path = (
                                topology
                                .registry_path(
                                    item.node
                                )
                            )

                            cloud_layers_inflight.add(
                                item.layer
                            )
                        else:

                            source_pair = (
                                src,
                                item.layer,
                            )

                            if (
                                source_pair
                                in retained_new_pairs
                            ):
                                reused_retained_pairs.add(
                                    source_pair
                                )

                            path = (
                                topology
                                .peer_path(
                                    src,
                                    item.node,
                                )
                            )

                        latency_s = (
                            topology
                            .path_latency_ms(
                                path
                            )
                            / 1000.0
                        )

                        flow = {
                            "dst": item.node,
                            "src": src,
                            "layer": item.layer,
                            "size": (
                                item.size_mb
                            ),
                            "remaining": (
                                item.size_mb
                            ),
                            "path": tuple(path),
                            "created_at": now,
                            "ready_at": (
                                now
                                + latency_s
                            ),
                        }

                        inflight_pairs.add(
                            pair
                        )

                        started_this_epoch += 1

                        if (
                            latency_s
                            <= EPS
                        ):
                            active.append(
                                flow
                            )
                        else:
                            pending.append(
                                flow
                            )

                    if started_this_epoch > 0:
                        scheduling_cycles += 1
                        pulse_selected_total += (
                            started_this_epoch
                        )

        # -----------------------------------------------------
        # If nothing is transferring, move to next activation.
        # -----------------------------------------------------

        if not active:

            event_times = []

            if pending:
                event_times.append(
                    min(
                        f["ready_at"]
                        for f in pending
                    )
                )

            next_arrival = (
                next_arrival_after(
                    now
                )
            )

            if next_arrival is not None:
                event_times.append(
                    next_arrival
                )

            if event_times:

                next_time = min(
                    event_times
                )

                dt_idle = max(
                    0.0,
                    next_time - now,
                )

                keep.update_virtual_queue(
                    registry_mb=0.0,
                    duration_s=dt_idle,
                )

                now = max(
                    now,
                    next_time,
                )

                continue

            remaining_pairs = sum(
                len(
                    need[node]
                    - acquired[node]
                )
                for node in nodes
            )

            if remaining_pairs > 0:
                raise RuntimeError(
                    "CIDER deadlock: "
                    f"{remaining_pairs} "
                    "released layer demands remain"
                )

            if (
                len(released)
                < accepted
            ):
                raise RuntimeError(
                    "CIDER deadlock: "
                    "unreleased requests remain "
                    "without a future arrival event"
                )

            break

        # -----------------------------------------------------
        # Network event.
        # -----------------------------------------------------

        rates = fair_rates(
            [
                f["path"]
                for f in active
            ],
            topology.capacity,
        )

        dt_completion = min(
            f["remaining"]
            / max(
                rates[i],
                EPS,
            )
            for i, f
            in enumerate(active)
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

            if (
                until_activation
                > EPS
            ):
                dt = min(
                    dt,
                    until_activation,
                )

        next_arrival = (
            next_arrival_after(
                now
            )
        )

        if next_arrival is not None:

            until_arrival = (
                next_arrival
                - now
            )

            if until_arrival > EPS:
                dt = min(
                    dt,
                    until_arrival,
                )

        if dt <= EPS:
            now += EPS
            continue

        # -----------------------------------------------------
        # Link utilization.
        # -----------------------------------------------------

        link_rate = Counter()

        for i, f in enumerate(
            active
        ):
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

        # -----------------------------------------------------
        # Transfer data during [now, now+dt].
        # -----------------------------------------------------

        registry_interval_mb = 0.0

        for i, f in enumerate(
            active
        ):
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

        # Lyapunov queue uses actual WAN bytes
        # during this event interval.
        keep.update_virtual_queue(
            registry_mb=(
                registry_interval_mb
            ),
            duration_s=dt,
        )

        now += dt

        # -----------------------------------------------------
        # Process completed transfers.
        # -----------------------------------------------------

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

        newly_arrived = {
            node: set()
            for node in nodes
        }

        for f in completed:

            active.remove(f)

            dst = f["dst"]
            src = f["src"]
            layer = f["layer"]
            size = f["size"]

            inflight_pairs.discard(
                (
                    dst,
                    layer,
                )
            )

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

            else:

                peer_mb += (
                    size
                )

                if (
                    topology
                    .same_domain(
                        src,
                        dst,
                    )
                ):
                    same_domain_peer_mb += (
                        size
                    )
                else:
                    cross_domain_peer_mb += (
                        size
                    )

        update_ready(
            now
        )

        # -----------------------------------------------------
        # KEEP:
        # optimize preservation for layers completed at this
        # event boundary.
        #
        # Layers currently serving as active/pending sources
        # are pinned until their transmissions finish.
        # -----------------------------------------------------

        t0 = time.perf_counter()

        for node in nodes:

            if not newly_arrived[
                node
            ]:
                continue

            old_cache = set(
                relay_cache[node]
            )

            pins = {
                f["layer"]
                for f in (
                    active + pending
                )
                if (
                    f["src"]
                    == node
                )
            }

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
                    "active source pins exceed "
                    f"cache capacity at {node}"
                )

            candidate_nonpins = (
                (
                    old_cache
                    | newly_arrived[node]
                )
                - pins
            )

            remaining_capacity = (
                cache_capacity[node]
                - pinned_bytes
            )

            selected_nonpins = (
                keep.choose_retention(
                    node=node,
                    candidate_layers=(
                        candidate_nonpins
                    ),
                    capacity_mb=(
                        remaining_capacity
                    ),
                    nodes=list(nodes),
                    relay_cache=(
                        relay_cache
                    ),
                    topology=topology,
                    sizes=sizes,
                )
            )

            selected_cache = (
                set(pins)
                | set(
                    selected_nonpins
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

            rejected_new = (
                newly_arrived[node]
                - selected_cache
            )

            retained_new = (
                newly_arrived[node]
                & selected_cache
            )

            not_preserved_mb += sum(
                sizes[x]
                for x
                in rejected_new
            )

            for layer in retained_new:
                retained_new_pairs.add(
                    (
                        node,
                        layer,
                    )
                )

            for layer in rejected_new:
                rejected_pairs.add(
                    (
                        node,
                        layer,
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

    # Startup latency is measured from each
    # request's own release time.
    ready_values = [
        max(
            0.0,
            ready[cid]
            - arrival_time[cid],
        )
        for cid in ready
    ]

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

    # Makespan is the absolute completion time of
    # the rebased target window, not max startup delay.
    makespan = (
        max(
            ready.values()
        )
        if ready
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

        "keep_retained_new_pairs": (
            len(
                retained_new_pairs
            )
        ),

        "keep_reused_retained_pairs": (
            len(
                reused_retained_pairs
            )
        ),

        "keep_retained_reuse_pct": (
            100.0
            * len(
                reused_retained_pairs
            )
            / max(
                1,
                len(
                    retained_new_pairs
                ),
            )
        ),

        "keep_rejected_pairs": (
            len(
                rejected_pairs
            )
        ),

        "keep_rejected_then_needed": (
            len(
                rejected_then_needed
            )
        ),

        "keep_rejected_needed_pct": (
            100.0
            * len(
                rejected_then_needed
            )
            / max(
                1,
                len(
                    rejected_pairs
                ),
            )
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
