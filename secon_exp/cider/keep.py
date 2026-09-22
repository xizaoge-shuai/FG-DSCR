from __future__ import annotations

from collections import Counter


class KeepOptimizer:
    """
    KEEP:
    Knowledge-guided Efficient Edge-layer Preservation.

    基于长期 Registry traffic virtual queue
    的 Lyapunov-style preservation。
    """

    def __init__(
        self,
        V=1.0,
        registry_budget_mb=5000.0,
    ):
        self.V = float(V)

        self.registry_budget_mb = float(
            registry_budget_mb
        )

        self.virtual_queue = 0.0

    def update_virtual_queue(
        self,
        registry_mb,
    ):
        self.virtual_queue = max(
            0.0,
            self.virtual_queue
            + registry_mb
            - self.registry_budget_mb,
        )

    def layer_value(
        self,
        node,
        layer,
        nodes,
        relay_cache,
        need,
        topology,
        sizes,
        popularity,
    ):
        future_demand = (
            popularity.get(
                layer,
                0,
            )
        )

        if future_demand <= 0:
            return 0.0

        value = 0.0

        for dst in nodes:
            if dst == node:
                continue

            if layer not in need[dst]:
                continue

            # 保留 node 上副本时的通信成本
            peer_path = (
                topology.peer_path(
                    node,
                    dst,
                )
            )

            peer_rate = min(
                topology.capacity[x]
                for x in peer_path
            )

            peer_cost = (
                sizes[layer]
                / max(
                    peer_rate,
                    1e-9,
                )
            )

            # 删除后，寻找其他候选源
            alternatives = []

            for other in nodes:
                if (
                    other == node
                    or other == dst
                ):
                    continue

                if (
                    layer
                    not in relay_cache[
                        other
                    ]
                ):
                    continue

                p = topology.peer_path(
                    other,
                    dst,
                )

                rate = min(
                    topology.capacity[x]
                    for x in p
                )

                alternatives.append(
                    sizes[layer]
                    / max(
                        rate,
                        1e-9,
                    )
                )

            rp = (
                topology.registry_path(
                    dst
                )
            )

            rr = min(
                topology.capacity[x]
                for x in rp
            )

            alternatives.append(
                sizes[layer]
                / max(
                    rr,
                    1e-9,
                )
            )

            alt_cost = min(
                alternatives
            )

            value += max(
                0.0,
                alt_cost - peer_cost,
            )

        return (
            future_demand
            * value
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
        当前实现将 Lyapunov per-slot
        subproblem 转为 value/size 排序的
        capacity-constrained selection。
        """

        scored = []

        for layer in candidate_layers:

            communication_value = (
                self.layer_value(
                    node,
                    layer,
                    nodes,
                    relay_cache,
                    need,
                    topology,
                    sizes,
                    popularity,
                )
            )

            registry_pressure = (
                self.virtual_queue
                * popularity.get(
                    layer,
                    0,
                )
            )

            objective = (
                self.V
                * communication_value
                + registry_pressure
            )

            scored.append(
                (
                    objective
                    / max(
                        sizes[layer],
                        1e-9,
                    ),
                    layer,
                )
            )

        scored.sort(
            reverse=True
        )

        selected = set()
        used = 0.0

        for _, layer in scored:

            size = sizes[layer]

            if (
                used + size
                <= capacity_mb
            ):
                selected.add(
                    layer
                )

                used += size

        return selected
