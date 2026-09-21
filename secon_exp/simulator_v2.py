from __future__ import annotations

import math
import statistics
from collections import Counter
from typing import Dict, List, Tuple

from secon_exp.network.topology import EdgeTopology
from secon_exp.simulator import fair_rates


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
    """
    samples:
        [(value, duration), ...]

    用事件区间持续时间作为权重计算百分位。
    """
    samples = [
        (float(v), float(w))
        for v, w in samples
        if w > EPS
    ]

    if not samples:
        return 0.0

    samples.sort(
        key=lambda x: x[0]
    )

    total = sum(
        w for _, w in samples
    )

    target = total * q
    acc = 0.0

    for value, weight in samples:
        acc += weight

        if acc + EPS >= target:
            return value

    return samples[-1][0]


def simulate_v2(
    case,
    placement,
    policy,
    topology: EdgeTopology,
):
    """
    Topology-aware event-driven image-layer delivery simulator.

    主要区别：
    1. Registry 和 P2P 流使用真实多跳路径；
    2. 所有 flow 在路径上的共享链路竞争容量；
    3. 跟踪跨域流量、WAN 流量、链路利用率；
    4. 保持和旧 simulator 一样的：
       - delivered state
       - reusable relay cache
       - one-download-per-destination
       - max-min fair bandwidth sharing
    """

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
        for c in case[
            "containers"
        ]
    }

    groups = {
        eid: list(
            placement.get(
                eid,
                []
            )
        )
        for eid in nodes
    }

    assigned = set()

    for cids in groups.values():
        assigned.update(cids)

    accepted = len(assigned)

    excluded = (
        len(containers)
        - accepted
    )

    # ------------------------------------------------
    # container -> unique layer set
    # ------------------------------------------------

    tasks = {}

    for cid in assigned:
        tasks[cid] = set(
            containers[cid][
                "layers"
            ]
        )

    # ------------------------------------------------
    # Global popularity.
    # ------------------------------------------------

    layer_popularity = Counter()

    for layers in tasks.values():
        for layer in layers:
            layer_popularity[
                layer
            ] += 1

    # ------------------------------------------------
    # Per-node required layers.
    # ------------------------------------------------

    need = {}

    for n in nodes:
        x = set()

        for cid in groups[n]:
            x.update(
                tasks[cid]
            )

        need[n] = x

    # acquired:
    # 当前 deployment 可直接使用的 layer。
    #
    # relay_cache:
    # 可作为未来 P2P source 的可复用压缩 layer。
    # ------------------------------------------------

    acquired = {}
    relay_cache = {}

    for n in nodes:
        initial = set(
            nodes[n].get(
                "initial_cache",
                []
            )
        )

        initial &= set(sizes)

        acquired[n] = set(
            initial
        )

        relay_cache[n] = set(
            initial
        )

    capacity = {
        n: float(
            nodes[n].get(
                "repo_capacity_mb",
                nodes[n].get(
                    "cache_capacity_mb",
                    float("inf"),
                ),
            )
        )
        for n in nodes
    }

    cache_bytes = {
        n: sum(
            sizes[x]
            for x in relay_cache[n]
        )
        for n in nodes
    }

    touched = {
        n: {
            x: 0.0
            for x in relay_cache[n]
        }
        for n in nodes
    }

    # ------------------------------------------------
    # Ready states.
    # ------------------------------------------------

    ready = {}

    def update_ready(now):
        changed = True

        while changed:
            changed = False

            for n in nodes:
                for cid in groups[n]:

                    if cid in ready:
                        continue

                    if (
                        tasks[cid]
                        <= acquired[n]
                    ):
                        ready[cid] = now
                        changed = True

    update_ready(0.0)

    # ------------------------------------------------
    # Selection helpers.
    # ------------------------------------------------

    def missing_layers(node):
        return (
            need[node]
            - acquired[node]
        )

    def layer_order_key(
        node,
        layer,
    ):
        if policy.ordering == "fifo":
            first_index = 10**9

            for i, cid in enumerate(
                groups[node]
            ):
                if (
                    cid not in ready
                    and layer
                    in (
                        tasks[cid]
                        - acquired[node]
                    )
                ):
                    first_index = min(
                        first_index,
                        i,
                    )

            return (
                first_index,
                layer,
            )

        if policy.ordering == "sjf":
            return (
                sizes[layer],
                layer,
            )

        if policy.ordering == "reuse":
            local_users = sum(
                1
                for cid in groups[node]
                if (
                    cid not in ready
                    and layer
                    in (
                        tasks[cid]
                        - acquired[node]
                    )
                )
            )

            reuse_bytes = (
                local_users
                * sizes[layer]
            )

            return (
                -reuse_bytes,
                layer,
            )

        if policy.ordering == "popularity":
            waiters = sum(
                1
                for other in nodes
                if (
                    layer
                    in missing_layers(
                        other
                    )
                )
            )

            score = (
                waiters
                + 0.25
                * layer_popularity.get(
                    layer,
                    0,
                )
            ) / max(
                sizes[layer],
                EPS,
            )

            return (
                -score,
                layer,
            )

        # local readiness ordering
        score = 0.0

        for cid in groups[node]:
            if cid in ready:
                continue

            remaining = (
                tasks[cid]
                - acquired[node]
            )

            if (
                layer in remaining
                and remaining
            ):
                score += (
                    1.0
                    / len(remaining)
                )

        return (
            -score
            / max(
                sizes[layer],
                EPS,
            ),
            layer,
        )

    # ------------------------------------------------
    # Cache eviction.
    # ------------------------------------------------

    def active_source_pins(
        active,
        pending,
    ):
        pins = {
            n: set()
            for n in nodes
        }

        for f in (
            active + pending
        ):
            src = f["src"]

            if src is not None:
                pins[src].add(
                    f["layer"]
                )

        return pins

    def victim_key(
        node,
        layer,
    ):
        if (
            policy.eviction
            == "replica_popularity"
        ):
            holders = sum(
                1
                for other in nodes
                if layer
                in relay_cache[other]
            )

            pending_demand = sum(
                1
                for other in nodes
                if layer
                in missing_layers(
                    other
                )
            )

            return (
                holders <= 1,
                pending_demand,
                layer_popularity.get(
                    layer,
                    0,
                ),
                touched[node].get(
                    layer,
                    0.0,
                ),
                -sizes[layer],
                layer,
            )

        if (
            policy.eviction
            == "future_reuse"
        ):
            future_use = sum(
                1
                for cid in groups[node]
                if (
                    cid not in ready
                    and layer
                    in tasks[cid]
                )
            )

            return (
                future_use,
                touched[node].get(
                    layer,
                    0.0,
                ),
                -sizes[layer],
                layer,
            )

        # lease 在 v2 baseline 中按 LRU fallback。
        # CIDER KEEP 后面单独实现。
        return (
            touched[node].get(
                layer,
                0.0,
            ),
            layer,
        )

    evicted_mb = 0.0
    refused_cache_mb = 0.0

    def admit_cache(
        node,
        layer,
        now,
        active,
        pending,
    ):
        nonlocal evicted_mb
        nonlocal refused_cache_mb

        if layer in relay_cache[node]:
            touched[node][layer] = now
            return

        layer_size = sizes[layer]

        if (
            layer_size
            > capacity[node] + EPS
        ):
            refused_cache_mb += (
                layer_size
            )
            return

        pins = active_source_pins(
            active,
            pending,
        )[node]

        while (
            cache_bytes[node]
            + layer_size
            > capacity[node] + EPS
        ):
            candidates = [
                x
                for x in relay_cache[
                    node
                ]
                if x not in pins
            ]

            if not candidates:
                refused_cache_mb += (
                    layer_size
                )
                return

            victim = min(
                candidates,
                key=lambda x:
                    victim_key(
                        node,
                        x,
                    ),
            )

            relay_cache[node].remove(
                victim
            )

            cache_bytes[node] -= (
                sizes[victim]
            )

            evicted_mb += (
                sizes[victim]
            )

        relay_cache[node].add(
            layer
        )

        cache_bytes[node] += (
            layer_size
        )

        touched[node][layer] = now

    # ------------------------------------------------
    # Flow state.
    # ------------------------------------------------

    active = []
    pending = []

    registry_mb = 0.0
    peer_mb = 0.0

    cross_domain_peer_mb = 0.0
    same_domain_peer_mb = 0.0

    link_bytes = {
        lid: 0.0
        for lid in topology.capacity
    }

    link_busy_time = {
        lid: 0.0
        for lid in topology.capacity
    }

    link_peak_util = {
        lid: 0.0
        for lid in topology.capacity
    }

    util_samples = []

    wan_busy_time_s = 0.0

    now = 0.0

    # ------------------------------------------------
    # Source selection.
    # ------------------------------------------------

    def resource_usage():
        usage = Counter()

        for f in active:
            for r in f["path"]:
                usage[r] += 1

        return usage

    def estimate_rate(path):
        usage = resource_usage()

        return min(
            topology.capacity[r]
            / (
                usage[r]
                + 1
            )
            for r in path
        )

    def candidate_sources(
        node,
        layer,
    ):
        options = []

        registry_path = (
            topology.registry_path(
                node
            )
        )

        options.append(
            (
                None,
                registry_path,
                estimate_rate(
                    registry_path
                ),
            )
        )

        if policy.source_mode in (
            "p2p",
            "dragonfly",
            "peersync",
        ):
            for src in nodes:
                if src == node:
                    continue

                if (
                    layer
                    not in relay_cache[
                        src
                    ]
                ):
                    continue

                pth = (
                    topology.peer_path(
                        src,
                        node,
                    )
                )

                options.append(
                    (
                        src,
                        pth,
                        estimate_rate(
                            pth
                        ),
                    )
                )

        return options

    def choose_source(
        node,
        layer,
    ):
        options = candidate_sources(
            node,
            layer,
        )

        if (
            policy.source_mode
            == "cloud"
        ):
            return options[0]

        peers = [
            x
            for x in options
            if x[0] is not None
        ]

        if (
            policy.source_mode
            == "dragonfly"
        ):
            if not peers:
                return options[0]

            def key(opt):
                src, path, rate = opt

                uploads = sum(
                    1
                    for f in active
                    if f["src"]
                    == src
                )

                latency = (
                    topology
                    .path_latency_ms(
                        path
                    )
                )

                return (
                    rate
                    / (
                        1.0
                        + uploads
                    ),
                    -latency,
                    src,
                )

            return max(
                peers,
                key=key,
            )

        if (
            policy.source_mode
            == "peersync"
        ):
            if not peers:
                return options[0]

            def key(opt):
                src, path, rate = opt

                local = int(
                    topology.same_domain(
                        src,
                        node,
                    )
                )

                latency = (
                    topology
                    .path_latency_ms(
                        path
                    )
                )

                uploads = sum(
                    1
                    for f in active
                    if f["src"]
                    == src
                )

                return (
                    local,
                    rate,
                    -latency,
                    -uploads,
                    src,
                )

            return max(
                peers,
                key=key,
            )

        # generic P2P:
        # choose fastest current path.
        return max(
            options,
            key=lambda x: (
                x[2],
                x[0] is not None,
                -topology.path_latency_ms(
                    x[1]
                ),
            ),
        )

    # ------------------------------------------------
    # Start new downloads for idle destinations.
    # ------------------------------------------------

    def schedule_new():
        started = 0

        busy = {
            f["dst"]
            for f in (
                active + pending
            )
        }

        layers_inflight = {
            f["layer"]
            for f in (
                active + pending
            )
        }

        for node in nodes:
            if node in busy:
                continue

            missing = list(
                missing_layers(node)
            )

            if not missing:
                continue

            missing.sort(
                key=lambda l:
                    layer_order_key(
                        node,
                        l,
                    )
            )

            chosen = None

            for layer in missing:

                src, path, rate = (
                    choose_source(
                        node,
                        layer,
                    )
                )

                # Coalescing:
                # 如果某 layer 正在网络中传播，而当前只能回源，
                # 则等待第一个副本完成后再通过 P2P 获取。
                if (
                    policy.coalesce
                    and src is None
                    and layer
                    in layers_inflight
                ):
                    continue

                chosen = (
                    layer,
                    src,
                    path,
                    rate,
                )
                break

            if chosen is None:
                continue

            (
                layer,
                src,
                path,
                _
            ) = chosen

            latency_s = (
                topology
                .path_latency_ms(path)
                / 1000.0
            )

            flow = {
                "dst": node,
                "src": src,
                "layer": layer,
                "path": tuple(path),
                "remaining": (
                    sizes[layer]
                ),
                "size": (
                    sizes[layer]
                ),
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

            started += 1

        return started

    # ------------------------------------------------
    # Simulation loop.
    # ------------------------------------------------

    schedule_new()

    safety = 0

    while (
        len(ready)
        < accepted
    ):
        safety += 1

        if safety > 10_000_000:
            raise RuntimeError(
                "simulation safety limit "
                "exceeded"
            )

        # Activate flows whose propagation
        # delay has elapsed.
        newly_active = [
            f
            for f in pending
            if f["ready_at"]
            <= now + EPS
        ]

        if newly_active:
            for f in newly_active:
                pending.remove(f)
                active.append(f)

        schedule_new()

        if not active:
            if pending:
                next_time = min(
                    f["ready_at"]
                    for f in pending
                )

                now = max(
                    now,
                    next_time,
                )

                continue

            # No flow but unfinished requests.
            # Usually caused by coalescing:
            # disable one wait cycle by
            # starting the first missing layer.
            started = 0

            busy = {
                f["dst"]
                for f in pending
            }

            for node in nodes:
                if node in busy:
                    continue

                missing = list(
                    missing_layers(
                        node
                    )
                )

                if not missing:
                    continue

                missing.sort(
                    key=lambda l:
                        layer_order_key(
                            node,
                            l,
                        )
                )

                layer = missing[0]

                src, path, _ = (
                    choose_source(
                        node,
                        layer,
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
                    "dst": node,
                    "src": src,
                    "layer": layer,
                    "path": tuple(path),
                    "remaining": (
                        sizes[layer]
                    ),
                    "size": (
                        sizes[layer]
                    ),
                    "ready_at": (
                        now
                        + latency_s
                    ),
                }

                pending.append(
                    flow
                )

                started += 1

            if started:
                continue

            break

        paths = [
            f["path"]
            for f in active
        ]

        rates = fair_rates(
            paths,
            topology.capacity,
        )

        # Completion time under current
        # bandwidth allocation.
        dt_complete = min(
            f["remaining"]
            / max(
                rates[i],
                EPS,
            )
            for i, f
            in enumerate(active)
        )

        # A pending flow may become active
        # before an existing flow completes.
        if pending:
            next_activation = min(
                f["ready_at"]
                for f in pending
            )

            dt_activation = max(
                0.0,
                next_activation
                - now
            )

            if (
                dt_activation
                > EPS
            ):
                dt = min(
                    dt_complete,
                    dt_activation,
                )
            else:
                dt = 0.0
        else:
            dt = dt_complete

        if dt <= EPS:
            # activate pending flows
            now += EPS
            continue

        # --------------------------------------------
        # Link utilization in this event interval.
        # --------------------------------------------

        link_rate = Counter()

        for i, f in enumerate(
            active
        ):
            r = rates[i]

            for lid in f["path"]:
                link_rate[lid] += r

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

            link_peak_util[lid] = max(
                link_peak_util[lid],
                util,
            )

            util_samples.append(
                (
                    util,
                    dt,
                )
            )

            if util > EPS:
                link_busy_time[lid] += (
                    dt
                )

        if (
            link_rate.get(
                "wan",
                0.0,
            )
            > EPS
        ):
            wan_busy_time_s += dt

        # --------------------------------------------
        # Transfer bytes.
        # --------------------------------------------

        for i, f in enumerate(
            active
        ):
            amount = min(
                f["remaining"],
                rates[i] * dt,
            )

            f["remaining"] -= amount

            for lid in f["path"]:
                link_bytes[lid] += (
                    amount
                )

        now += dt

        completed = [
            f
            for f in active
            if f["remaining"]
            <= EPS
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

            touched[dst][
                layer
            ] = now

            if src is None:
                registry_mb += size
            else:
                peer_mb += size

                if (
                    topology.same_domain(
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

                touched[src][
                    layer
                ] = now

            admit_cache(
                dst,
                layer,
                now,
                active,
                pending,
            )

        update_ready(now)

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

    p2p_offload = (
        100.0
        * peer_mb
        / max(
            total_transfer,
            EPS,
        )
    )

    peak_link_util = max(
        link_peak_util.values(),
        default=0.0,
    )

    p95_link_util = (
        weighted_percentile(
            util_samples,
            0.95,
        )
    )

    return {
        "policy": policy.name,

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
            p2p_offload
        ),

        "evicted_mb": (
            evicted_mb
        ),
        "refused_cache_mb": (
            refused_cache_mb
        ),

        "peak_link_utilization": (
            peak_link_util
        ),
        "p95_link_utilization": (
            p95_link_util
        ),
        "wan_busy_time_s": (
            wan_busy_time_s
        ),

        "link_bytes": (
            link_bytes
        ),
        "link_busy_time_s": (
            link_busy_time
        ),
        "link_peak_utilization": (
            link_peak_util
        ),

        "simulation_end_s": now,
    }
