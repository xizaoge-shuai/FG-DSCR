from __future__ import annotations

import heapq
from collections import defaultdict


INF = 10**18


class MinCostFlow:
    def __init__(self):
        self.g = defaultdict(list)

    def add_edge(
        self,
        u,
        v,
        cap,
        cost,
    ):
        a = [
            v,
            cap,
            cost,
            None,
        ]

        b = [
            u,
            0,
            -cost,
            a,
        ]

        a[3] = b

        self.g[u].append(a)
        self.g[v].append(b)

        return a

    def solve(
        self,
        source,
        sink,
        required_flow,
    ):
        flow = 0
        cost = 0.0

        potential = defaultdict(float)

        while flow < required_flow:

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
                d, u = heapq.heappop(
                    pq
                )

                if d != dist[u]:
                    continue

                for edge in self.g[u]:

                    v, cap, c, rev = edge

                    if cap <= 0:
                        continue

                    nd = (
                        d
                        + c
                        + potential[u]
                        - potential[v]
                    )

                    if nd < dist[v]:
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

            for v in list(dist):
                if dist[v] < INF:
                    potential[v] += (
                        dist[v]
                    )

            cur = sink

            while cur != source:
                u, edge = parent[cur]

                edge[1] -= 1
                edge[3][1] += 1

                cost += edge[2]

                cur = u

            flow += 1

        return flow, cost


class ScoutOptimizer:
    """
    SCOUT:
    Source-aware Communication Optimization
    for Unified Transfer.
    """

    def __init__(
        self,
        registry_penalty=0.02,
        cross_domain_penalty=0.005,
        congestion_penalty=1.0,
        source_concurrency=0,
    ):
        self.registry_penalty = float(
            registry_penalty
        )

        self.cross_domain_penalty = float(
            cross_domain_penalty
        )

        self.congestion_penalty = float(
            congestion_penalty
        )

        self.source_concurrency = int(
            source_concurrency
        )

    def choose(
        self,
        demands,
        relay_cache,
        topology,
        active,
        sizes,
    ):
        """
        demands:
            list[PulseItem]

        return:
            dict[(dst, layer)] = source

        source=None 表示 Registry。
        """

        if not demands:
            return {}

        usage = defaultdict(int)

        for f in active:
            for lid in f["path"]:
                usage[lid] += 1

        def path_cost(
            path,
            layer_size,
        ):
            bottleneck = min(
                topology.capacity[x]
                / (
                    1
                    + usage[x]
                )
                for x in path
            )

            latency_s = (
                topology
                .path_latency_ms(path)
                / 1000.0
            )

            transfer_s = (
                layer_size
                / max(
                    bottleneck,
                    1e-9,
                )
            )

            congestion = sum(
                usage[x]
                / max(
                    topology.capacity[x],
                    1e-9,
                )
                for x in path
            )

            return (
                transfer_s
                + latency_s
                + self.congestion_penalty
                * congestion
            )

        mcf = MinCostFlow()

        SRC = "__SRC__"
        SNK = "__SNK__"
        REG = "__REGISTRY__"

        source_edges = {}

        # Registry 足够大
        mcf.add_edge(
            SRC,
            REG,
            len(demands),
            0.0,
        )

        peer_sources = set()

        for item in demands:
            for peer, cached in (
                relay_cache.items()
            ):
                if (
                    peer != item.node
                    and item.layer in cached
                ):
                    peer_sources.add(
                        peer
                    )

        for peer in peer_sources:
            # source_concurrency <= 0 表示不再使用人为的
            # cardinality cap。真实上传限制由 tx:peer 链路
            # 和后续 max-min fair network simulator 强制执行。
            source_cap = (
                len(demands)
                if self.source_concurrency <= 0
                else self.source_concurrency
            )

            mcf.add_edge(
                SRC,
                f"peer:{peer}",
                source_cap,
                0.0,
            )

        demand_nodes = []

        for i, item in enumerate(
            demands
        ):
            dn = f"demand:{i}"

            demand_nodes.append(
                (
                    dn,
                    item,
                )
            )

            mcf.add_edge(
                dn,
                SNK,
                1,
                0.0,
            )

            # Registry candidate
            rpath = (
                topology.registry_path(
                    item.node
                )
            )

            rcost = (
                path_cost(
                    rpath,
                    item.size_mb,
                )
                + self.registry_penalty
                * item.size_mb
            )

            edge = mcf.add_edge(
                REG,
                dn,
                1,
                rcost,
            )

            source_edges[
                (
                    i,
                    None,
                )
            ] = edge

            # Peer candidates
            for peer in peer_sources:

                if (
                    item.layer
                    not in relay_cache[
                        peer
                    ]
                ):
                    continue

                ppath = (
                    topology.peer_path(
                        peer,
                        item.node,
                    )
                )

                pcost = path_cost(
                    ppath,
                    item.size_mb,
                )

                if not topology.same_domain(
                    peer,
                    item.node,
                ):
                    pcost += (
                        self.cross_domain_penalty
                        * item.size_mb
                    )

                edge = mcf.add_edge(
                    f"peer:{peer}",
                    dn,
                    1,
                    pcost,
                )

                source_edges[
                    (
                        i,
                        peer,
                    )
                ] = edge

        mcf.solve(
            SRC,
            SNK,
            len(demands),
        )

        assignment = {}

        for i, item in enumerate(
            demands
        ):
            chosen = None

            for (
                j,
                peer,
            ), edge in (
                source_edges.items()
            ):
                if j != i:
                    continue

                # 初始 cap=1，
                # 使用后 cap=0
                if edge[1] == 0:
                    chosen = peer
                    break

            assignment[
                (
                    item.node,
                    item.layer,
                )
            ] = chosen

        return assignment
