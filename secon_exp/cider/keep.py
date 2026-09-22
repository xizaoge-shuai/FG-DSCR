from __future__ import annotations

import math


EPS = 1e-9


class KeepOptimizer:
    """
    KEEP:
    Knowledge-guided Efficient Edge-layer Preservation.

    Event-driven Lyapunov formulation:

        Q(t+dt) =
            [Q(t)
             + B_WAN(t,t+dt)
             - B_bar * dt]^+

    B_bar is therefore expressed in MB/s.

    When storage pressure occurs, KEEP solves a
    quantized 0/1 capacity-constrained preservation
    subproblem instead of using plain LRU/LFU.
    """

    def __init__(
        self,
        V=1.0,
        registry_budget_rate_mb_s=3.0,
        quantum_mb=8.0,
    ):
        self.V = float(V)

        self.registry_budget_rate_mb_s = float(
            registry_budget_rate_mb_s
        )

        self.quantum_mb = float(
            quantum_mb
        )

        self.virtual_queue = 0.0


    def update_virtual_queue(
        self,
        registry_mb,
        duration_s,
    ):
        """
        Continuous/event-driven equivalent of

            Q(t+1) =
            [Q(t)+B_WAN(t)-B_bar]^+.
        """

        if duration_s <= 0:
            return

        self.virtual_queue = max(
            0.0,
            self.virtual_queue
            + float(registry_mb)
            - (
                self.registry_budget_rate_mb_s
                * float(duration_s)
            ),
        )


    @staticmethod
    def path_cost(
        topology,
        path,
        size_mb,
    ):
        """
        Long-term communication cost.

        Uses propagation latency plus bottleneck
        transmission time. Dynamic instantaneous
        congestion belongs to SCOUT.
        """

        rate = min(
            topology.capacity[x]
            for x in path
        )

        latency_s = (
            topology.path_latency_ms(
                path
            )
            / 1000.0
        )

        return (
            latency_s
            + size_mb
            / max(
                rate,
                EPS,
            )
        )


    def communication_value(
        self,
        node,
        layer,
        nodes,
        relay_cache,
        topology,
        sizes,
        popularity,
    ):
        """
        G_{n,l} =
            p_hat_l *
            sum_j [
                C_alt(j,l) - C_n(j,l)
            ]^+

        A candidate copy on `node` is evaluated as a
        possible future communication source.
        """

        demand_count = float(
            popularity.get(
                layer,
                0,
            )
        )

        if demand_count <= 0:
            return 0.0

        total_popularity = max(
            1.0,
            float(
                sum(
                    popularity.values()
                )
            ),
        )

        p_hat = (
            demand_count
            / total_popularity
        )

        size = sizes[layer]

        holders = [
            x
            for x in nodes
            if layer in relay_cache[x]
        ]

        saving = 0.0

        for dst in nodes:

            if dst == node:
                continue

            candidate_path = (
                topology.peer_path(
                    node,
                    dst,
                )
            )

            candidate_cost = (
                self.path_cost(
                    topology,
                    candidate_path,
                    size,
                )
            )

            # Alternative 1: Registry.
            registry_path = (
                topology.registry_path(
                    dst
                )
            )

            alt_cost = (
                self.path_cost(
                    topology,
                    registry_path,
                    size,
                )
            )

            # Alternative 2: any other current
            # edge replica.
            for src in holders:

                if (
                    src == node
                    or src == dst
                ):
                    continue

                path = (
                    topology.peer_path(
                        src,
                        dst,
                    )
                )

                cost = (
                    self.path_cost(
                        topology,
                        path,
                        size,
                    )
                )

                alt_cost = min(
                    alt_cost,
                    cost,
                )

            saving += max(
                0.0,
                alt_cost
                - candidate_cost,
            )

        return (
            p_hat
            * saving
        )


    def registry_avoidance_value(
        self,
        node,
        layer,
        nodes,
        relay_cache,
        sizes,
        popularity,
    ):
        """
        Approximate Delta B_{n,l}.

        Scarce and popular replicas receive larger
        expected WAN-avoidance value.
        """

        total_popularity = max(
            1.0,
            float(
                sum(
                    popularity.values()
                )
            ),
        )

        p_hat = (
            float(
                popularity.get(
                    layer,
                    0,
                )
            )
            / total_popularity
        )

        other_holders = sum(
            1
            for src in nodes
            if (
                src != node
                and layer
                in relay_cache[src]
            )
        )

        scarcity = (
            1.0
            / (
                1.0
                + other_holders
            )
        )

        return (
            p_hat
            * sizes[layer]
            * scarcity
        )


    def choose_retention(
        self,
        node,
        candidate_layers,
        capacity_mb,
        nodes,
        relay_cache,
        need,
        topology,
        sizes,
        popularity,
    ):
        """
        Solve

            max sum z_{n,l}
                [V G_{n,l} + Q DeltaB_{n,l}]

        subject to

            sum s_l z_{n,l} <= M_n.

        Storage is quantized only for computational
        efficiency. The returned set is checked
        against the exact MB capacity afterwards.
        """

        candidate_layers = list(
            set(candidate_layers)
        )

        if (
            capacity_mb <= EPS
            or not candidate_layers
        ):
            return set()

        scored = []

        for layer in candidate_layers:

            g = (
                self.communication_value(
                    node=node,
                    layer=layer,
                    nodes=nodes,
                    relay_cache=relay_cache,
                    topology=topology,
                    sizes=sizes,
                    popularity=popularity,
                )
            )

            delta_b = (
                self.registry_avoidance_value(
                    node=node,
                    layer=layer,
                    nodes=nodes,
                    relay_cache=relay_cache,
                    sizes=sizes,
                    popularity=popularity,
                )
            )

            objective = (
                self.V * g
                + self.virtual_queue
                * delta_b
            )

            scored.append(
                (
                    layer,
                    objective,
                )
            )

        capacity_units = max(
            0,
            int(
                math.floor(
                    capacity_mb
                    / self.quantum_mb
                )
            ),
        )

        if capacity_units <= 0:
            return set()

        # dp[used_units] =
        #     (objective, tuple(selected layers))
        dp = {
            0: (
                0.0,
                tuple(),
            )
        }

        for layer, value in scored:

            weight = max(
                1,
                int(
                    math.ceil(
                        sizes[layer]
                        / self.quantum_mb
                    )
                ),
            )

            if weight > capacity_units:
                continue

            next_dp = dict(dp)

            for used, (
                old_value,
                chosen,
            ) in dp.items():

                new_used = (
                    used + weight
                )

                if (
                    new_used
                    > capacity_units
                ):
                    continue

                new_value = (
                    old_value
                    + value
                )

                old = next_dp.get(
                    new_used
                )

                if (
                    old is None
                    or new_value
                    > old[0]
                ):
                    next_dp[
                        new_used
                    ] = (
                        new_value,
                        chosen
                        + (layer,),
                    )

            dp = next_dp

        _, chosen = max(
            dp.values(),
            key=lambda x: x[0],
        )

        # Quantization uses ceil(size/q), therefore
        # this should already fit in exact MB.
        selected = set(chosen)

        used_mb = sum(
            sizes[x]
            for x in selected
        )

        if (
            used_mb
            > capacity_mb + EPS
        ):
            raise RuntimeError(
                "KEEP internal capacity error: "
                f"{used_mb} > {capacity_mb}"
            )

        return selected
