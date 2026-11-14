from __future__ import annotations

import math
from collections import Counter
from typing import Any, Dict, Iterable, List, Tuple

from secon_exp.policies.base import PolicySpec


def fair_rates(
    paths: List[Tuple[str, ...]],
    caps: Dict[str, float],
) -> List[float]:
    """
    Max-min fair progressive filling.

    This intentionally matches the validated pilot implementation.
    """
    rates = [0.0] * len(paths)
    free = set(range(len(paths)))
    left = dict(caps)

    while free:
        counts = Counter(
            resource
            for i in free
            for resource in paths[i]
        )

        step = min(
            left[r] / k
            for r, k in counts.items()
        )

        for i in free:
            rates[i] += step

        for r, k in counts.items():
            left[r] = max(
                0.0,
                left[r] - step * k,
            )

        saturated = {
            r
            for r in counts
            if left[r] <= 1e-8
        }

        blocked = {
            i
            for i in free
            if saturated.intersection(paths[i])
        }

        if not blocked:
            raise RuntimeError(
                "Bandwidth allocation failed to advance"
            )

        free -= blocked

    return rates


def percentile95(values: Iterable[float]) -> float:
    vals = sorted(values)
    if not vals:
        return 0.0

    return vals[
        max(
            0,
            math.ceil(0.95 * len(vals)) - 1,
        )
    ]


def simulate(
    case: Dict[str, Any],
    placement: Dict[str, List[str]],
    policy: PolicySpec,
    wan: float,
    upload: float = 100.0,
    lan: float = 1000.0,
    cache_scale: float | None = None,
) -> Dict[str, Any]:
    """
    Event-driven layer-delivery simulator.

    cache_scale:
      None -> unlimited reusable relay cache.
              This is used for exact pilot-equivalence checks.
      float -> node.repo_capacity_mb * cache_scale.

    Important state separation:
      acquired[n]:
          layer has already reached node n for this deployment batch.
          It remains locally usable by this batch.

      relay_cache[n]:
          compressed layer blob currently retained and therefore available
          as a future P2P source.

    The separation models service installation space and redistributable
    compressed-layer cache as different resources.
    """
    sizes = {
        l: float(s)
        for l, s in case["layer_sizes_mb"].items()
    }

    nodes = {
        n["eid"]: n
        for n in case["nodes"]
    }

    tasks = {
        c["cid"]: set(c["layers"])
        for c in case["containers"]
    }

    layer_popularity = Counter()
    for _layers in tasks.values():
        for _layer in _layers:
            layer_popularity[_layer] += 1

    if len(tasks) != len(case["containers"]):
        raise ValueError("Duplicate request IDs")

    if len(nodes) != len(case["nodes"]):
        raise ValueError("Duplicate node IDs")

    if any(
        not math.isfinite(s) or s < 0
        for s in sizes.values()
    ):
        raise ValueError("Invalid layer sizes")

    assigned = []
    for n, ids in placement.items():
        if n not in nodes or not isinstance(ids, list):
            raise ValueError(
                "Expected assignment: node_id -> list[cid]"
            )
        assigned.extend(ids)

    if (
        len(assigned) != len(set(assigned))
        or not set(assigned) <= tasks.keys()
    ):
        raise ValueError(
            "Duplicate or unknown requests in assignment"
        )

    groups = {
        n: list(placement.get(n, []))
        for n in sorted(nodes)
    }

    relay_cache = {
        n: set(nodes[n].get("initial_cache", []))
        for n in groups
    }

    acquired = {
        n: set(relay_cache[n])
        for n in groups
    }

    need = {
        n: set().union(
            *(tasks[c] for c in groups[n])
        )
        if groups[n]
        else set()
        for n in groups
    }

    if any(
        not (need[n] | relay_cache[n]) <= sizes.keys()
        for n in groups
    ):
        raise ValueError("Missing layer size")

    caps = {
        "wan": float(wan),
        "lan": float(lan),
    }

    for n in groups:
        caps["rx:" + n] = float(
            nodes[n]["bandwidth_mb_s"]
        )
        caps["tx:" + n] = float(upload)

    if any(
        not math.isfinite(x) or x <= 0
        for x in caps.values()
    ):
        raise ValueError(
            "All bandwidths must be finite and positive"
        )

    for n in groups:
        zero = {
            l
            for l in need[n]
            if sizes[l] == 0
        }
        acquired[n].update(zero)
        relay_cache[n].update(zero)

    limits = {}
    for n in groups:
        if cache_scale is None:
            limits[n] = float("inf")
        else:
            if cache_scale < 0:
                raise ValueError(
                    "cache_scale must be >= 0"
                )

            limits[n] = (
                float(nodes[n]["repo_capacity_mb"])
                * cache_scale
            )

    cache_bytes = {
        n: sum(
            sizes[l]
            for l in relay_cache[n]
        )
        for n in groups
    }

    for n in groups:
        if (
            math.isfinite(limits[n])
            and cache_bytes[n] > limits[n] + 1e-8
        ):
            raise ValueError(
                f"Initial relay cache exceeds tested "
                f"capacity on {n}: "
                f"{cache_bytes[n]} > {limits[n]}"
            )

    touched = {
        n: {
            l: 0.0
            for l in relay_cache[n]
        }
        for n in groups
    }

    order = {
        c: i
        for i, c in enumerate(tasks)
    }

    fifo = {
        n: {
            l: min(
                order[c]
                for c in groups[n]
                if l in tasks[c]
            )
            for l in need[n]
        }
        for n in groups
    }

    ready: Dict[str, float] = {}
    active: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []

    traffic = {
        "registry_mb": 0.0,
        "peer_mb": 0.0,
    }

    evicted_mb = 0.0
    refused_cache_mb = 0.0
    max_lease_mb = 0.0

    t = 0.0

    def relay_value(
        node: str,
        layer: str,
    ):
        waiters = sum(
            1
            for other in groups
            if (
                other != node
                and layer in need[other]
                and layer not in acquired[other]
            )
        )

        holders = sum(
            1
            for other in groups
            if layer in relay_cache[other]
        )

        scarce = (
            waiters > 0
            and holders <= 1
        )

        value = (
            waiters
            / max(1, holders)
        )

        return (
            scarce,
            value,
            waiters,
            holders,
        )

    def active_source_pins(
        node: str,
    ):
        return {
            f["layer"]
            for f in active
            if (
                f["src"] == node
                and f["remaining"] > 1e-7
            )
        }

    def choose_relay_pins(
        node: str,
    ):
        nonlocal max_lease_mb

        if (
            policy.eviction != "lease"
            or policy.lease_rho <= 0
            or not math.isfinite(limits[node])
        ):
            return set()

        budget = (
            policy.lease_rho
            * limits[node]
        )

        candidates = []

        for layer in relay_cache[node]:
            (
                scarce,
                value,
                waiters,
                holders,
            ) = relay_value(
                node,
                layer,
            )

            if scarce and waiters > 0:
                candidates.append(
                    (
                        layer,
                        value,
                        waiters,
                        sizes[layer],
                    )
                )

        candidates.sort(
            key=lambda x: (
                x[1],
                x[2],
                x[3],
            ),
            reverse=True,
        )

        pins = set()
        used = 0.0

        for (
            layer,
            value,
            waiters,
            size,
        ) in candidates:
            if (
                used + size
                <= budget + 1e-8
            ):
                pins.add(layer)
                used += size

        max_lease_mb = max(
            max_lease_mb,
            used,
        )

        return pins

    def victim_key(
        node: str,
        layer: str,
    ):
        if policy.eviction == "replica_popularity":
            holders = sum(
                1
                for other in groups
                if layer in relay_cache[other]
            )

            pending = sum(
                1
                for other in groups
                if (
                    layer in need[other]
                    and layer not in acquired[other]
                )
            )

            # PeerSync-style:
            # 非唯一副本、低需求、低流行度、久未访问的内容优先删除。
            #
            # holders <= 1 为 True，min() 时会排在 False 后面，
            # 因而唯一/稀缺副本得到保护。
            return (
                holders <= 1,
                pending,
                layer_popularity.get(layer, 0),
                touched[node].get(
                    layer,
                    0.0,
                ),
                -sizes[layer],
                layer,
            )

        if policy.eviction == "future_reuse":
            future_local_use = sum(
                1
                for cid in groups[node]
                if (
                    cid not in ready
                    and layer in tasks[cid]
                )
            )

            # ILR-SA-style:
            # 局部未来复用价值越低，越先淘汰。
            return (
                future_local_use,
                touched[node].get(
                    layer,
                    0.0,
                ),
                -sizes[layer],
                layer,
            )

        if policy.eviction == "lease":
            (
                scarce,
                value,
                waiters,
                holders,
            ) = relay_value(
                node,
                layer,
            )

            return (
                scarce,
                value,
                waiters,
                touched[node].get(
                    layer,
                    0.0,
                ),
                layer,
            )

        return (
            touched[node].get(
                layer,
                0.0,
            ),
            layer,
        )

    def predict_eviction_debt(
        node: str,
        incoming: str,
    ):
        if not math.isfinite(limits[node]):
            return 0.0, 0

        pins = (
            active_source_pins(node)
            | choose_relay_pins(node)
        )

        incoming_size = sizes[incoming]

        pinned_size = sum(
            sizes[l]
            for l in pins
        )

        # The layer will be usable locally but cannot enter the relay cache.
        # No existing relay is displaced in that case.
        if (
            incoming_size + pinned_size
            > limits[node] + 1e-8
        ):
            return 0.0, 0

        need_free = max(
            0.0,
            cache_bytes[node]
            + incoming_size
            - limits[node],
        )

        if need_free <= 1e-8:
            return 0.0, 0

        candidates = list(
            relay_cache[node] - pins
        )
        candidates.sort(
            key=lambda l: victim_key(
                node,
                l,
            )
        )

        freed = 0.0
        debt_mb = 0.0
        victims = 0

        for victim in candidates:
            if freed >= need_free - 1e-8:
                break

            (
                scarce,
                value,
                waiters,
                holders,
            ) = relay_value(
                node,
                victim,
            )

            debt_mb += (
                sizes[victim]
                * value
            )

            freed += sizes[victim]
            victims += 1

        if freed < need_free - 1e-8:
            return 0.0, 0

        return debt_mb, victims

    def admit(
        node: str,
        layer: str,
        now: float,
    ):
        nonlocal evicted_mb
        nonlocal refused_cache_mb

        if not math.isfinite(limits[node]):
            if layer not in relay_cache[node]:
                relay_cache[node].add(layer)
                cache_bytes[node] += sizes[layer]
            touched[node][layer] = now
            return

        pins = active_source_pins(node)

        if policy.eviction == "lease":
            pins |= choose_relay_pins(node)

        pinned_size = sum(
            sizes[l]
            for l in pins
        )

        if (
            sizes[layer] + pinned_size
            > limits[node] + 1e-8
        ):
            refused_cache_mb += sizes[layer]
            return

        while (
            cache_bytes[node]
            + sizes[layer]
            > limits[node] + 1e-8
        ):
            candidates = (
                relay_cache[node]
                - pins
            )

            if not candidates:
                refused_cache_mb += sizes[layer]
                return

            victim = min(
                candidates,
                key=lambda l: victim_key(
                    node,
                    l,
                ),
            )

            relay_cache[node].remove(
                victim
            )
            cache_bytes[node] -= sizes[
                victim
            ]
            evicted_mb += sizes[victim]

        if layer not in relay_cache[node]:
            relay_cache[node].add(layer)
            cache_bytes[node] += sizes[layer]

        touched[node][layer] = now

    while True:
        for n in groups:
            for c in groups[n]:
                if (
                    c not in ready
                    and tasks[c] <= acquired[n]
                ):
                    ready[c] = t

        if len(ready) == len(assigned):
            break

        missing = {
            n: need[n] - acquired[n]
            for n in groups
        }

        busy = {
            f["dst"]
            for f in active
        }

        for n in groups:
            if n in busy:
                continue

            local = Counter()

            if policy.ordering in (
                "local",
                "debt",
            ):
                for c in groups[n]:
                    remaining = (
                        tasks[c]
                        - acquired[n]
                    )

                    if not remaining:
                        continue

                    for l in remaining:
                        local[l] += (
                            1.0
                            / len(remaining)
                        )

            def priority(layer: str):
                if policy.ordering == "fifo":
                    return (
                        fifo[n][layer],
                        layer,
                    )

                if policy.ordering == "sjf":
                    return (
                        sizes[layer],
                        layer,
                    )

                if policy.ordering == "popularity":
                    global_waiters = sum(
                        1
                        for other in groups
                        if layer in missing[other]
                    )

                    score = (
                        global_waiters
                        + 0.25 * layer_popularity.get(layer, 0)
                    ) / max(
                        sizes[layer],
                        1e-9,
                    )

                    return (
                        -score,
                        layer,
                    )

                if policy.ordering == "reuse":
                    local_users = sum(
                        1
                        for cid in groups[n]
                        if layer in (
                            tasks[cid]
                            - acquired[n]
                        )
                    )

                    # ILR-SA 思想：
                    # 优先处理能产生更多 layer reuse bytes 的 layer。
                    reuse_value = (
                        local_users
                        * sizes[layer]
                    )

                    return (
                        -reuse_value,
                        layer,
                    )

                local_score = (
                    local[layer]
                    / max(
                        sizes[layer],
                        1e-9,
                    )
                )

                if policy.ordering == "local":
                    return (
                        -local_score,
                        layer,
                    )

                debt_mb, victims = (
                    predict_eviction_debt(
                        n,
                        layer,
                    )
                )

                debt_units = (
                    debt_mb
                    / max(
                        sizes[layer],
                        1e-9,
                    )
                )

                score = (
                    local[layer]
                    - policy.debt_lambda
                    * debt_units
                ) / max(
                    sizes[layer],
                    1e-9,
                )

                return (
                    -score,
                    layer,
                )

            usage = Counter(
                r
                for f in active
                for r in f["path"]
            )

            for layer in sorted(
                missing[n],
                key=priority,
            ):
                sources = [None]

                if policy.source_mode in (
                    "p2p",
                    "dragonfly",
                    "peersync",
                ):
                    sources += [
                        s
                        for s in groups
                        if (
                            s != n
                            and layer
                            in relay_cache[s]
                        )
                    ]

                options = []

                for source in sources:
                    path = (
                        "wan"
                        if source is None
                        else "tx:" + source,
                        "rx:" + n,
                        "lan",
                    )

                    estimate = min(
                        caps[r]
                        / (usage[r] + 1)
                        for r in path
                    )

                    options.append(
                        (
                            source,
                            path,
                            estimate,
                        )
                    )

                if policy.source_mode == "dragonfly":
                    # Dragonfly-style:
                    # 优先 P2P parent；根据实时可用速率和当前上传负载
                    # 选择 parent；没有 peer 时回源。
                    peer_options = [
                        x
                        for x in options
                        if x[0] is not None
                    ]

                    if peer_options:
                        def dragonfly_score(opt):
                            src, pth, estimate = opt

                            uploads = sum(
                                1
                                for f in active
                                if f["src"] == src
                            )

                            return (
                                estimate
                                / (1.0 + uploads),
                                -uploads,
                                src,
                            )

                        source, path, estimate = max(
                            peer_options,
                            key=dragonfly_score,
                        )
                    else:
                        source, path, estimate = options[0]

                elif policy.source_mode == "peersync":
                    # PeerSync-style:
                    # 1) 同一网络域优先；
                    # 2) 高估计传输速率优先；
                    # 3) 内容价值较高的 peer 优先；
                    # 无可用 peer 时直接回源。
                    peer_options = [
                        x
                        for x in options
                        if x[0] is not None
                    ]

                    if peer_options:
                        dst_domain = nodes[n].get(
                            "domain",
                            nodes[n].get("lan_id"),
                        )

                        def peersync_score(opt):
                            src, pth, estimate = opt

                            src_domain = nodes[src].get(
                                "domain",
                                nodes[src].get("lan_id"),
                            )

                            same_domain = (
                                dst_domain is not None
                                and src_domain is not None
                                and dst_domain == src_domain
                            )

                            inventory_popularity = sum(
                                layer_popularity.get(x, 0)
                                for x in relay_cache[src]
                            )

                            uploads = sum(
                                1
                                for f in active
                                if f["src"] == src
                            )

                            return (
                                int(same_domain),
                                estimate,
                                inventory_popularity,
                                -uploads,
                                src,
                            )

                        source, path, estimate = max(
                            peer_options,
                            key=peersync_score,
                        )
                    else:
                        source, path, estimate = options[0]

                else:
                    # 原正式 simulator 的 fastest-source 行为。
                    source, path, estimate = min(
                        options,
                        key=lambda opt: (
                            -opt[2],
                            opt[0] is None,
                            opt[0] or "",
                        ),
                    )

                cloud = source is None

                if (
                    policy.coalesce
                    and cloud
                    and any(
                        f["layer"] == layer
                        for f in active
                    )
                ):
                    continue

                if not cloud:
                    touched[source][layer] = t

                active.append(
                    {
                        "src": (
                            None
                            if cloud
                            else source
                        ),
                        "dst": n,
                        "layer": layer,
                        "path": path,
                        "remaining": sizes[layer],
                        "start_s": t,
                    }
                )

                break

        if not active:
            raise RuntimeError(
                "Unfinished requests but "
                "no possible transfer"
            )

        rates = fair_rates(
            [f["path"] for f in active],
            caps,
        )

        if min(rates) <= 0:
            raise RuntimeError(
                "Non-positive rate"
            )

        for resource, capacity in caps.items():
            used = sum(
                r
                for f, r in zip(
                    active,
                    rates,
                )
                if resource in f["path"]
            )

            if (
                used
                > capacity
                + 1e-7
                * max(
                    1.0,
                    capacity,
                )
            ):
                raise RuntimeError(
                    "Link capacity violated"
                )

        dt = min(
            f["remaining"] / rate
            for f, rate in zip(
                active,
                rates,
            )
        )

        t += dt

        for f, rate in zip(
            active,
            rates,
        ):
            f["remaining"] -= (
                rate * dt
            )

        pending = []

        for f in active:
            if f["remaining"] > 1e-7:
                pending.append(f)
                continue

            layer = f["layer"]
            dst = f["dst"]

            acquired[dst].add(layer)
            admit(
                dst,
                layer,
                t,
            )

            amount = sizes[layer]

            if f["src"] is None:
                traffic["registry_mb"] += amount
            else:
                traffic["peer_mb"] += amount

            events.append(
                {
                    "src": f["src"],
                    "dst": dst,
                    "layer": layer,
                    "size_mb": amount,
                    "start_s": f["start_s"],
                    "end_s": t,
                }
            )

        active = pending

        # A peer flow may only continue while its sender still retains the blob.
        assert all(
            (
                f["src"] is None
                or f["layer"]
                in relay_cache[f["src"]]
            )
            for f in active
        )

    values = sorted(
        ready.values()
    )

    retained_mb = {
        n: sum(
            sizes[l]
            for l in relay_cache[n]
        )
        for n in groups
    }

    overflow = [
        n
        for n in groups
        if (
            math.isfinite(limits[n])
            and retained_mb[n]
            > limits[n] + 1e-8
        )
    ]

    return {
        "policy": policy.name,
        "wan_mb_s": wan,
        "upload_mb_s": upload,
        "lan_mb_s": lan,
        "accepted": len(assigned),
        "excluded": (
            len(tasks)
            - len(assigned)
        ),
        "mean_ready_s": (
            sum(values)
            / max(
                1,
                len(values),
            )
        ),
        "p95_ready_s": percentile95(
            values
        ),
        "makespan_s": max(
            values,
            default=0.0,
        ),
        "registry_mb": traffic[
            "registry_mb"
        ],
        "peer_mb": traffic[
            "peer_mb"
        ],
        "total_transfer_mb": (
            traffic["registry_mb"]
            + traffic["peer_mb"]
        ),
        "evicted_mb": evicted_mb,
        "refused_cache_mb": (
            refused_cache_mb
        ),
        "max_lease_mb": max_lease_mb,
        "cache_overflow_nodes": overflow,
        "retained_mb": retained_mb,
        "ready_s": ready,
        "events": events,
    }
