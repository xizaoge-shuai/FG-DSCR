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

        self.g[u].append(forward)
        self.g[v].append(backward)

        return forward

    def solve(
        self,
        source,
        sink,
        required_flow,
    ):
        total_flow = 0
        total_cost = 0.0

        potential = defaultdict(float)

        while total_flow < required_flow:

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

                u, edge = parent[cur]

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

    对 PULSE 当前选中的一批 layer demands
    联合进行 source assignment。

    Normalized communication cost:

        c =
            alpha * T_norm
          + beta  * B_norm
          + gamma * P_norm

    T_norm:
        relative transfer time compared with
        Registry delivery for the SAME demand.

    B_norm:
        0 -> same-domain peer
        1 -> Registry or cross-domain peer

    P_norm:
        normalized current path congestion proxy.
    """

    def __init__(
        self,
        alpha=1.0,
        beta=1.0,
        gamma=0.5,
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

        # ------------------------------------------------
        # Current path occupancy.
        #
        # active/pending flows are treated as already
        # reserving network resources for SCOUT's
        # communication-aware decision.
        # ------------------------------------------------

        usage = Counter()

        for flow in active:
            for lid in flow["path"]:
                usage[lid] += 1

        def estimated_bandwidth(path):

            return min(
                topology.capacity[lid]
                / (
                    1.0
                    + usage[lid]
                )
                for lid in path
            )

        def transfer_time(
            path,
            size_mb,
        ):
            bw = estimated_bandwidth(
                path
            )

            propagation = (
                topology
                .path_latency_ms(path)
                / 1000.0
            )

            return (
                propagation
                + size_mb
                / max(
                    bw,
                    EPS,
                )
            )

        def normalized_congestion(
            path
        ):
            """
            Convert path occupancy into a
            dimensionless [0,1) quantity.

            A link with q currently reserved
            transfers contributes q/(1+q).
            """

            if not path:
                return 0.0

            values = [
                usage[lid]
                / (
                    1.0
                    + usage[lid]
                )
                for lid in path
            ]

            return (
                sum(values)
                / len(values)
            )

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

            candidate_time = (
                transfer_time(
                    path,
                    size_mb,
                )
            )

            # ------------------------------------------------
            # Time normalization.
            #
            # Registry delivery for this exact
            # (dst, layer size) is the reference.
            # This prevents raw time magnitudes from
            # changing by one order of magnitude when
            # WAN capacity changes from 10 -> 100 MB/s.
            # ------------------------------------------------

            registry_path = (
                topology.registry_path(
                    dst
                )
            )

            reference_time = (
                transfer_time(
                    registry_path,
                    size_mb,
                )
            )

            time_norm = (
                candidate_time
                / max(
                    reference_time,
                    EPS,
                )
            )

            # ------------------------------------------------
            # Communication-scope cost.
            #
            # Same-domain P2P does not consume
            # higher-level network resources.
            #
            # Registry and cross-domain transfer
            # both receive unit communication cost.
            # ------------------------------------------------

            if source is None:

                # External Registry / cloud-backhaul
                # communication has the highest
                # communication-scope cost.
                traffic_norm = 1.0

            elif topology.same_domain(
                source,
                dst,
            ):

                # Same-domain P2P stays inside the
                # local edge domain.
                traffic_norm = 0.0

            else:

                # Cross-domain P2P and Registry both
                # consume upper-level/shared network
                # resources in the normalized cost.
                traffic_norm = 1.0

            congestion_norm = (
                normalized_congestion(
                    path
                )
            )

            cost = (
                self.alpha
                * time_norm
                + self.beta
                * traffic_norm
                + self.gamma
                * congestion_norm
            )

            return (
                cost,
                path,
            )

        # ------------------------------------------------
        # Min-Cost Max-Flow graph.
        # ------------------------------------------------

        SRC = "__SRC__"
        SNK = "__SNK__"
        REG = "__REGISTRY__"

        mcf = MinCostFlow()

        k = len(demands)

        # Registry may serve all demands.
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

            source_cap = (
                k
                if self.source_concurrency
                <= 0
                else self.source_concurrency
            )

            mcf.add_edge(
                SRC,
                f"peer:{src}",
                source_cap,
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

            # -----------------------------
            # Registry candidate
            # -----------------------------

            registry_cost, _ = (
                communication_cost(
                    source=None,
                    dst=item.node,
                    size_mb=item.size_mb,
                )
            )

            edge = mcf.add_edge(
                REG,
                demand_node,
                1,
                registry_cost,
            )

            candidate_edges[
                (
                    i,
                    None,
                )
            ] = edge

            # -----------------------------
            # Peer candidates
            # -----------------------------

            for src in peer_sources:

                if (
                    item.layer
                    not in relay_cache[
                        src
                    ]
                ):
                    continue

                peer_cost, _ = (
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
                    peer_cost,
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
                f"SCOUT assigned only "
                f"{flow}/{k} demands"
            )

        assignment = {}

        for i, item in enumerate(
            demands
        ):
            chosen = None
            found = False

            for (
                idx,
                source,
            ), edge in (
                candidate_edges.items()
            ):
                if idx != i:
                    continue

                # Initial candidate capacity is 1.
                # cap == 0 means this edge carries
                # one unit of MCMF flow.
                if edge[1] == 0:

                    chosen = source
                    found = True
                    break

            if not found:
                raise RuntimeError(
                    "SCOUT selected no "
                    "source for demand "
                    f"({item.node}, "
                    f"{item.layer})"
                )

            assignment[
                (
                    item.node,
                    item.layer,
                )
            ] = chosen

        return assignment
