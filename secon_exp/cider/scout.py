from __future__ import annotations

import heapq
from collections import Counter, defaultdict


INF = 1e100
EPS = 1e-9


class MinCostFlow:
    def __init__(self):
        self.g = defaultdict(list)

    def add_edge(
        self,
        u,
        v,
        capacity,
        cost,
    ):
        forward = [
            v,
            int(capacity),
            float(cost),
            None,
        ]

        backward = [
            u,
            0,
            -float(cost),
            forward,
        ]

        forward[3] = backward

        self.g[u].append(
            forward
        )

        self.g[v].append(
            backward
        )

        return forward

    def solve(
        self,
        source,
        sink,
        required_flow,
    ):
        total_flow = 0
        total_cost = 0.0

        potential = defaultdict(
            float
        )

        while (
            total_flow
            < required_flow
        ):
            dist = defaultdict(
                lambda: INF
            )

            parent = {}

            dist[source] = 0.0

            pq = [
                (
                    0.0,
                    source,
                )
            ]

            while pq:

                d, u = (
                    heapq.heappop(pq)
                )

                if (
                    d
                    > dist[u] + EPS
                ):
                    continue

                for edge in self.g[u]:

                    v = edge[0]
                    cap = edge[1]
                    cost = edge[2]

                    if cap <= 0:
                        continue

                    nd = (
                        d
                        + cost
                        + potential[u]
                        - potential[v]
                    )

                    if (
                        nd
                        < dist[v] - EPS
                    ):
                        dist[v] = nd

                        parent[v] = (
                            u,
                            edge,
                        )

                        heapq.heappush(
                            pq,
                            (
                                nd,
                                v,
                            )
                        )

            if sink not in parent:
                break

            for v, d in dist.items():
                if d < INF:
                    potential[v] += d

            cur = sink

            while cur != source:

                u, edge = (
                    parent[cur]
                )

                edge[1] -= 1
                edge[3][1] += 1

                total_cost += (
                    edge[2]
                )

                cur = u

            total_flow += 1

        return (
            total_flow,
            total_cost,
        )


class ScoutOptimizer:
    """
    SCOUT:
    Source-aware Communication Optimization
    for Unified Transfer.

    对 PULSE 选出的整批需求联合做 source assignment。
    """

    def __init__(
        self,
        alpha=1.0,
        beta=0.02,
        gamma=1.0,
        source_concurrency=0,
    ):
        self.alpha = float(alpha)
        self.beta = float(beta)
        self.gamma = float(gamma)

        self.source_concurrency = int(
            source_concurrency
        )

    def choose(
        self,
        demands,
        relay_cache,
        topology,
        active=None,
    ):
        if not demands:
            return {}

        if active is None:
            active = []

        current_usage = Counter()

        for flow in active:
            for lid in flow["path"]:
                current_usage[lid] += 1

        def communication_cost(
            source,
            dst,
            size_mb,
        ):
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

            estimated_bw = min(
                topology.capacity[lid]
                / (
                    1.0
                    + current_usage[lid]
                )
                for lid in path
            )

            transfer_time = (
                topology.path_latency_ms(
                    path
                )
                / 1000.0
                + size_mb
                / max(
                    estimated_bw,
                    EPS,
                )
            )

            # 当前路径拥塞代价
            path_congestion = sum(
                current_usage[lid]
                / max(
                    topology.capacity[
                        lid
                    ],
                    EPS,
                )
                for lid in path
            )

            wan_bytes = 0.0

            if source is None:
                wan_bytes = size_mb

            elif not topology.same_domain(
                source,
                dst,
            ):
                # 跨域通信也属于需要控制的
                # higher-level network traffic。
                wan_bytes = size_mb

            return (
                self.alpha
                * transfer_time
                + self.beta
                * wan_bytes
                + self.gamma
                * path_congestion
            )

        SRC = "__SRC__"
        SNK = "__SNK__"
        REG = "__REGISTRY__"

        mcf = MinCostFlow()

        k = len(demands)

        # Registry.
        mcf.add_edge(
            SRC,
            REG,
            k,
            0.0,
        )

        peer_sources = set()

        for item in demands:
            for src, cache in (
                relay_cache.items()
            ):
                if (
                    src != item.node
                    and item.layer
                    in cache
                ):
                    peer_sources.add(
                        src
                    )

        for src in peer_sources:

            cap = (
                k
                if self.source_concurrency
                <= 0
                else self.source_concurrency
            )

            mcf.add_edge(
                SRC,
                f"peer:{src}",
                cap,
                0.0,
            )

        candidate_edges = {}

        for i, item in enumerate(
            demands
        ):
            demand_node = (
                f"demand:{i}"
            )

            mcf.add_edge(
                demand_node,
                SNK,
                1,
                0.0,
            )

            # Registry candidate
            cost = communication_cost(
                source=None,
                dst=item.node,
                size_mb=item.size_mb,
            )

            edge = mcf.add_edge(
                REG,
                demand_node,
                1,
                cost,
            )

            candidate_edges[
                (
                    i,
                    None,
                )
            ] = edge

            # Edge peer candidates
            for src in peer_sources:

                if (
                    item.layer
                    not in relay_cache[
                        src
                    ]
                ):
                    continue

                cost = (
                    communication_cost(
                        source=src,
                        dst=item.node,
                        size_mb=item.size_mb,
                    )
                )

                edge = mcf.add_edge(
                    f"peer:{src}",
                    demand_node,
                    1,
                    cost,
                )

                candidate_edges[
                    (
                        i,
                        src,
                    )
                ] = edge

        flow, _ = mcf.solve(
            SRC,
            SNK,
            k,
        )

        if flow != k:
            raise RuntimeError(
                f"SCOUT only assigned "
                f"{flow}/{k} demands"
            )

        assignment = {}

        for i, item in enumerate(
            demands
        ):
            selected_source = None
            found = False

            for (
                idx,
                source,
            ), edge in (
                candidate_edges.items()
            ):
                if idx != i:
                    continue

                # capacity 1 -> 0 means selected.
                if edge[1] == 0:
                    selected_source = (
                        source
                    )
                    found = True
                    break

            if not found:
                raise RuntimeError(
                    "SCOUT demand has "
                    "no selected source"
                )

            assignment[
                (
                    item.node,
                    item.layer,
                )
            ] = selected_source

        return assignment
