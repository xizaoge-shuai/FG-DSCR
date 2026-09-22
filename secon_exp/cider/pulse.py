from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Set


@dataclass
class PulseItem:
    node: str
    layer: str
    size_mb: float
    utility: float


class PulseScheduler:
    """
    PULSE:
    Priority-aware Utility-driven Layer Selection Engine

    将每个目标节点看作一个 group。
    每个 group 中可以选 0 或 1 个待传输 layer，
    在统一 transmission budget 下求 multiple-choice knapsack。
    """

    def __init__(
        self,
        quantum_mb: float = 8.0,
        max_candidates_per_node: int = 8,
    ):
        self.quantum_mb = float(
            quantum_mb
        )

        self.max_candidates_per_node = int(
            max_candidates_per_node
        )

    def utility(
        self,
        node: str,
        layer: str,
        groups: Dict[str, List[str]],
        tasks: Dict[str, Set[str]],
        acquired: Dict[str, Set[str]],
        waiting_time=None,
    ) -> float:

        score = 0.0

        for cid in groups[node]:

            remaining = (
                tasks[cid]
                - acquired[node]
            )

            if (
                not remaining
                or layer not in remaining
            ):
                continue

            # 越接近 container completion，
            # 当前 layer utility 越高。
            score += (
                1.0
                / len(remaining)
            )

        return score

    def build_candidates(
        self,
        idle_nodes,
        groups,
        tasks,
        acquired,
        need,
        sizes,
    ):

        result = {}

        for node in idle_nodes:

            missing = (
                need[node]
                - acquired[node]
            )

            items = []

            for layer in missing:

                u = self.utility(
                    node,
                    layer,
                    groups,
                    tasks,
                    acquired,
                )

                # PULSE 的正式目标是最大化原始启动收益 U_{n,l}。
                # layer size 只作为 knapsack resource constraint，
                # 不能再次把 utility 除以 size，否则会过度偏向小层。
                items.append(
                    PulseItem(
                        node=node,
                        layer=layer,
                        size_mb=sizes[layer],
                        utility=u,
                    )
                )

            items.sort(
                key=lambda x: (
                    -x.utility,
                    x.size_mb,
                    x.layer,
                )
            )

            result[node] = items[
                :self.max_candidates_per_node
            ]

        return result

    def select(
        self,
        idle_nodes,
        groups,
        tasks,
        acquired,
        need,
        sizes,
        budget_mb,
        max_transfers,
    ):
        """
        Multiple-choice knapsack.

        每个 node 最多选一个 layer。
        """

        if (
            budget_mb <= 0
            or max_transfers <= 0
        ):
            return []

        candidates = (
            self.build_candidates(
                idle_nodes,
                groups,
                tasks,
                acquired,
                need,
                sizes,
            )
        )

        capacity = max(
            1,
            int(
                math.floor(
                    budget_mb
                    / self.quantum_mb
                )
            ),
        )

        # dp[(used_capacity, count)]
        # = (value, selected_items)
        dp = {
            (0, 0): (
                0.0,
                [],
            )
        }

        for node in idle_nodes:

            items = candidates.get(
                node,
                [],
            )

            next_dp = dict(dp)

            for (
                used,
                count,
            ), (
                value,
                chosen,
            ) in dp.items():

                if count >= max_transfers:
                    continue

                for item in items:

                    weight = max(
                        1,
                        int(
                            math.ceil(
                                item.size_mb
                                / self.quantum_mb
                            )
                        ),
                    )

                    new_used = (
                        used + weight
                    )

                    new_count = (
                        count + 1
                    )

                    if (
                        new_used
                        > capacity
                    ):
                        continue

                    key = (
                        new_used,
                        new_count,
                    )

                    new_value = (
                        value
                        + item.utility
                    )

                    old = next_dp.get(
                        key
                    )

                    if (
                        old is None
                        or new_value
                        > old[0]
                    ):
                        next_dp[key] = (
                            new_value,
                            chosen
                            + [item],
                        )

            dp = next_dp

        best = max(
            dp.values(),
            key=lambda x: (
                x[0],
                len(x[1]),
            ),
        )

        return best[1]
