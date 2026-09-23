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

    Formal problem:

        max sum x_{n,l} U_{n,l}

    s.t.

        sum s_l x_{n,l} <= B(t)
        sum x_{n,l} <= K(t)
        x_{n,l} in {0,1}

    Large-scale:
        Lagrangian-relaxation-based approximation.

    Small-scale analysis:
        quantized exact dynamic programming.
    """

    def __init__(
        self,
        quantum_mb=8.0,
        solver="lagrangian",
    ):
        self.quantum_mb = float(
            quantum_mb
        )

        self.solver = solver


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
        U_{n,l}(t) =
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
                not missing
                or layer
                not in missing
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

                if u <= EPS:
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


    @staticmethod
    def total_utility(items):
        return sum(
            x.utility
            for x in items
        )


    @staticmethod
    def total_size(items):
        return sum(
            x.size_mb
            for x in items
        )


    def _repair(
        self,
        items,
        budget_mb,
        max_transfers,
        lam,
    ):
        """
        Construct a feasible primal solution for a
        fixed Lagrange multiplier lambda.
        """

        ranked = sorted(
            items,
            key=lambda x: (
                -(
                    x.utility
                    - lam
                    * x.size_mb
                ),
                -(
                    x.utility
                    / max(
                        x.size_mb,
                        EPS,
                    )
                ),
                -x.utility,
                x.size_mb,
            ),
        )

        chosen = []
        used = 0.0
        chosen_set = set()

        # First take positive reduced-value items.
        for item in ranked:

            if (
                len(chosen)
                >= max_transfers
            ):
                break

            reduced = (
                item.utility
                - lam
                * item.size_mb
            )

            if reduced <= 0:
                continue

            if (
                used
                + item.size_mb
                <= budget_mb
                + EPS
            ):
                chosen.append(item)
                chosen_set.add(
                    (
                        item.node,
                        item.layer,
                    )
                )
                used += (
                    item.size_mb
                )

        # Feasibility repair / residual filling:
        # use utility density for remaining capacity.
        remaining = sorted(
            items,
            key=lambda x: (
                -(
                    x.utility
                    / max(
                        x.size_mb,
                        EPS,
                    )
                ),
                -x.utility,
                x.size_mb,
            ),
        )

        for item in remaining:

            if (
                len(chosen)
                >= max_transfers
            ):
                break

            key = (
                item.node,
                item.layer,
            )

            if key in chosen_set:
                continue

            if (
                used
                + item.size_mb
                <= budget_mb
                + EPS
            ):
                chosen.append(item)
                chosen_set.add(key)
                used += (
                    item.size_mb
                )

        return chosen


    def select_lagrangian(
        self,
        items,
        budget_mb,
        max_transfers,
    ):
        if not items:
            return []

        max_transfers = min(
            max_transfers,
            len(items),
        )

        max_density = max(
            x.utility
            / max(
                x.size_mb,
                EPS,
            )
            for x in items
        )

        lo = 0.0
        hi = max(
            max_density * 2.0,
            EPS,
        )

        best = []
        best_value = -1.0

        # Candidate at lambda=0.
        candidate = self._repair(
            items,
            budget_mb,
            max_transfers,
            0.0,
        )

        if candidate:
            best = candidate
            best_value = (
                self.total_utility(
                    candidate
                )
            )

        # Lagrangian multiplier search.
        for _ in range(28):

            lam = (
                lo + hi
            ) / 2.0

            candidate = self._repair(
                items,
                budget_mb,
                max_transfers,
                lam,
            )

            size = self.total_size(
                candidate
            )

            value = (
                self.total_utility(
                    candidate
                )
            )

            if (
                size
                <= budget_mb + EPS
                and value
                > best_value + EPS
            ):
                best = candidate
                best_value = value

            # Larger lambda penalizes traffic more.
            if (
                size
                > 0.95 * budget_mb
            ):
                lo = lam
            else:
                hi = lam

        # Safety fallback.
        if not best:

            feasible = [
                x
                for x in items
                if (
                    x.size_mb
                    <= budget_mb + EPS
                )
            ]

            if feasible:
                best = [
                    max(
                        feasible,
                        key=lambda x:
                            x.utility
                            / max(
                                x.size_mb,
                                EPS,
                            )
                    )
                ]

        return best


    def select_exact(
        self,
        items,
        budget_mb,
        max_transfers,
    ):
        """
        Quantized exact DP.

        Reserved for small-instance optimality-gap
        experiments rather than the main 20--50 node runs.
        """

        capacity = int(
            math.floor(
                budget_mb
                / self.quantum_mb
            )
        )

        if capacity <= 0:
            return []

        dp = {
            (0, 0): (
                0.0,
                tuple(),
            )
        }

        for idx, item in enumerate(
            items
        ):

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

                if (
                    count
                    >= max_transfers
                ):
                    continue

                new_used = (
                    used + weight
                )

                if (
                    new_used
                    > capacity
                ):
                    continue

                key = (
                    new_used,
                    count + 1,
                )

                new_value = (
                    value
                    + item.utility
                )

                old = dp.get(key)

                if (
                    old is None
                    or new_value
                    > old[0] + EPS
                ):
                    dp[key] = (
                        new_value,
                        chosen + (idx,),
                    )

        _, best_indices = max(
            dp.values(),
            key=lambda x: x[0],
        )

        return [
            items[i]
            for i
            in best_indices
        ]


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
            max_transfers = len(
                items
            )

        if self.solver == "exact":
            return self.select_exact(
                items,
                budget_mb,
                max_transfers,
            )

        return self.select_lagrangian(
            items,
            budget_mb,
            max_transfers,
        )
