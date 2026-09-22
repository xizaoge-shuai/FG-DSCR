from __future__ import annotations

import math
from dataclasses import dataclass


EPS = 1e-9


@dataclass(frozen=True)
class PulseItem:
    node: str
    layer: str
    size_mb: float
    utility: float


class PulseScheduler:
    """
    PULSE:
    Priority-aware Utility-driven Layer Selection Engine.

    Candidate:
        (destination node, layer)

    Objective:
        max sum x_{n,l} U_{n,l}

    Constraints:
        sum s_l x_{n,l} <= B(t)
        sum x_{n,l} <= K(t)

    不设置“每个 node 最多选一个 layer”的额外约束。
    """

    def __init__(
        self,
        quantum_mb=8.0,
    ):
        self.quantum_mb = float(
            quantum_mb
        )

    def layer_utility(
        self,
        node,
        layer,
        groups,
        tasks,
        acquired,
        container_weights=None,
    ):
        """
        U_{n,l}(t)
        =
        sum_{i in I_n, l in M_i(t)}
            w_i(t) / |M_i(t)|
        """

        utility = 0.0

        for cid in groups[node]:

            missing = (
                tasks[cid]
                - acquired[node]
            )

            if (
                layer not in missing
                or not missing
            ):
                continue

            weight = 1.0

            if container_weights:
                weight = float(
                    container_weights.get(
                        cid,
                        1.0,
                    )
                )

            utility += (
                weight
                / len(missing)
            )

        return utility

    def build_candidates(
        self,
        nodes,
        groups,
        tasks,
        acquired,
        need,
        sizes,
        container_weights=None,
    ):
        items = []

        for node in nodes:

            missing = (
                need[node]
                - acquired[node]
            )

            for layer in missing:

                u = self.layer_utility(
                    node=node,
                    layer=layer,
                    groups=groups,
                    tasks=tasks,
                    acquired=acquired,
                    container_weights=(
                        container_weights
                    ),
                )

                if u <= 0:
                    continue

                items.append(
                    PulseItem(
                        node=node,
                        layer=layer,
                        size_mb=float(
                            sizes[layer]
                        ),
                        utility=float(u),
                    )
                )

        return items

    def select(
        self,
        nodes,
        groups,
        tasks,
        acquired,
        need,
        sizes,
        budget_mb,
        max_transfers,
        container_weights=None,
    ):
        """
        Quantized 0/1 two-constraint knapsack.

        State:
            (used_budget_units, selected_count)

        Value:
            maximum sum U_{n,l}.
        """

        items = self.build_candidates(
            nodes=nodes,
            groups=groups,
            tasks=tasks,
            acquired=acquired,
            need=need,
            sizes=sizes,
            container_weights=(
                container_weights
            ),
        )

        if not items:
            return []

        if max_transfers <= 0:
            max_transfers = len(items)

        capacity = int(
            math.floor(
                float(budget_mb)
                / self.quantum_mb
            )
        )

        if capacity <= 0:
            return []

        # dp[(budget, count)]
        # =
        # (utility, tuple(item indices))
        dp = {
            (0, 0): (
                0.0,
                tuple(),
            )
        }

        for idx, item in enumerate(items):

            weight = max(
                1,
                int(
                    math.ceil(
                        item.size_mb
                        / self.quantum_mb
                    )
                ),
            )

            if weight > capacity:
                continue

            old_states = list(
                dp.items()
            )

            for (
                used,
                count,
            ), (
                value,
                chosen,
            ) in old_states:

                if count >= max_transfers:
                    continue

                new_used = (
                    used + weight
                )

                if new_used > capacity:
                    continue

                new_count = (
                    count + 1
                )

                new_value = (
                    value
                    + item.utility
                )

                key = (
                    new_used,
                    new_count,
                )

                previous = dp.get(
                    key
                )

                if (
                    previous is None
                    or new_value
                    > previous[0] + EPS
                ):
                    dp[key] = (
                        new_value,
                        chosen + (idx,),
                    )

        best_value = -1.0
        best_indices = tuple()

        for (
            _,
            _,
        ), (
            value,
            chosen,
        ) in dp.items():

            if (
                value
                > best_value + EPS
            ):
                best_value = value
                best_indices = chosen

        return [
            items[i]
            for i in best_indices
        ]
