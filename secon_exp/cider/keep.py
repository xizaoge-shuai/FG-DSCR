from __future__ import annotations

import math


EPS = 1e-9


class KeepOptimizer:
    """
    KEEP:
    Knowledge-guided Efficient Edge-layer Preservation.

    Objective:

        max sum z_{n,l} [
            V G_{n,l}
            + Q(t) DeltaB_{n,l}
        ]

    subject to:

        sum s_l z_{n,l} <= M_n
    """

    def __init__(
        self,
        history_probability,
        V=1.0,
        registry_budget_rate_mb_s=3.0,
        quantum_mb=8.0,
    ):
        self.p_hat = dict(
            history_probability
        )

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
        bw = min(
            topology.capacity[x]
            for x in path
        )

        return (
            topology.path_latency_ms(
                path
            )
            / 1000.0
            + float(size_mb)
            / max(
                bw,
                EPS,
            )
        )

    def value(
        self,
        node,
        layer,
        nodes,
        relay_cache,
        topology,
        sizes,
    ):
        """
        Compute:

            G_{n,l}
            =
            p_hat_l *
            sum_j
            [C_alt(j,l)-C_n(j,l)]^+

        and expected WAN avoidance DeltaB.
        """

        p = float(
            self.p_hat.get(
                layer,
                0.0,
            )
        )

        if p <= 0:
            return (
                0.0,
                0.0,
            )

        size = float(
            sizes[layer]
        )

        communication_gain = 0.0
        wan_avoidance = 0.0

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

            # Alternative if replica on `node`
            # does not exist.
            registry_path = (
                topology.registry_path(
                    dst
                )
            )

            registry_cost = (
                self.path_cost(
                    topology,
                    registry_path,
                    size,
                )
            )

            alt_cost = registry_cost
            alt_is_registry = True

            for src in nodes:

                if (
                    src == node
                    or src == dst
                ):
                    continue

                if (
                    layer
                    not in relay_cache[
                        src
                    ]
                ):
                    continue

                path = (
                    topology.peer_path(
                        src,
                        dst,
                    )
                )

                cost = self.path_cost(
                    topology,
                    path,
                    size,
                )

                if (
                    cost
                    < alt_cost
                ):
                    alt_cost = cost
                    alt_is_registry = False

            communication_gain += (
                p
                * max(
                    0.0,
                    alt_cost
                    - candidate_cost,
                )
            )

            if (
                alt_is_registry
                and candidate_cost
                < registry_cost
            ):
                wan_avoidance += (
                    p * size
                )

        return (
            communication_gain,
            wan_avoidance,
        )

    def choose_retention(
        self,
        node,
        candidate_layers,
        capacity_mb,
        nodes,
        relay_cache,
        topology,
        sizes,
    ):
        candidate_layers = list(
            set(candidate_layers)
        )

        total_size = sum(
            sizes[layer]
            for layer
            in candidate_layers
        )

        # No capacity pressure -> keep all.
        if (
            total_size
            <= capacity_mb + EPS
        ):
            return set(
                candidate_layers
            )

        capacity_units = int(
            math.floor(
                capacity_mb
                / self.quantum_mb
            )
        )

        if capacity_units <= 0:
            return set()

        items = []

        for layer in candidate_layers:

            g, delta_b = (
                self.value(
                    node=node,
                    layer=layer,
                    nodes=nodes,
                    relay_cache=(
                        relay_cache
                    ),
                    topology=topology,
                    sizes=sizes,
                )
            )

            objective = (
                self.V * g
                + self.virtual_queue
                * delta_b
            )

            weight = max(
                1,
                int(
                    math.ceil(
                        sizes[layer]
                        / self.quantum_mb
                    )
                ),
            )

            items.append(
                (
                    layer,
                    weight,
                    objective,
                )
            )

        # 0/1 knapsack.
        dp = {
            0: (
                0.0,
                tuple(),
            )
        }

        for (
            layer,
            weight,
            value,
        ) in items:

            previous_states = list(
                dp.items()
            )

            for used, (
                old_value,
                chosen,
            ) in previous_states:

                new_used = (
                    used + weight
                )

                if (
                    new_used
                    > capacity_units
                ):
                    continue

                new_value = (
                    old_value + value
                )

                old = dp.get(
                    new_used
                )

                if (
                    old is None
                    or new_value
                    > old[0] + EPS
                ):
                    dp[new_used] = (
                        new_value,
                        chosen + (layer,),
                    )

        _, chosen = max(
            dp.values(),
            key=lambda x: x[0],
        )

        return set(chosen)
