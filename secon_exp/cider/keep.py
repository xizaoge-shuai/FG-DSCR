from __future__ import annotations

import math
import os
import atexit


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
        upper_budget_rate_mb_s=3.0,
        quantum_mb=8.0,
    ):
        self.p_hat = dict(
            history_probability
        )

        self.V = float(V)

        # KEEP_UPPER_LYAPUNOV_V1
        #
        # Long-term budget applies to:
        #
        #   upper-network traffic
        #   = Registry traffic
        #   + cross-domain peer traffic
        #
        # Same-domain peer traffic is not charged.
        self.upper_budget_rate_mb_s = float(
            upper_budget_rate_mb_s
        )

        self.quantum_mb = float(
            quantum_mb
        )

        self.virtual_queue = 0.0

        # ----------------------------------------------------
        # Optional diagnostic instrumentation.
        # Does NOT alter KEEP decisions.
        # ----------------------------------------------------

        self.debug_enabled = (
            os.environ.get(
                "CIDER_KEEP_DEBUG",
                "0",
            )
            == "1"
        )

        self._dbg = {
            "items": 0,
            "g_zero": 0,
            "db_zero": 0,
            "vg_dominant": 0,

            "g_sum": 0.0,
            "g_min": float("inf"),
            "g_max": 0.0,

            "db_sum": 0.0,
            "db_min": float("inf"),
            "db_max": 0.0,

            "vg_sum": 0.0,
            "vg_min": float("inf"),
            "vg_max": 0.0,

            "qdb_sum": 0.0,
            "qdb_min": float("inf"),
            "qdb_max": 0.0,

            "ratio_sum": 0.0,

            "q_sum": 0.0,
            "q_max": 0.0,
        }

        if self.debug_enabled:
            atexit.register(
                self._print_debug_summary
            )

    def update_virtual_queue(
        self,
        upper_mb,
        duration_s,
    ):
        """
        KEEP_UPPER_LYAPUNOV_V1

        Q(t+1) =
            [Q(t)
             + B_upper(t)
             - B_upper_budget * dt]^+

        where

            B_upper
              = Registry bytes
              + cross-domain peer bytes.
        """
        self.virtual_queue = max(
            0.0,
            self.virtual_queue
            + float(upper_mb)
            - (
                self.upper_budget_rate_mb_s
                * float(duration_s)
            ),
        )

    def _print_debug_summary(self):
        if not self.debug_enabled:
            return

        d = self._dbg
        n = max(
            1,
            d["items"],
        )

        probs = [
            float(x)
            for x in self.p_hat.values()
            if float(x) > 0
        ]

        if probs:
            p_min = min(probs)
            p_max = max(probs)
            p_mean = (
                sum(probs)
                / len(probs)
            )
        else:
            p_min = 0.0
            p_max = 0.0
            p_mean = 0.0

        def safe_min(name):
            x = d[name]
            if x == float("inf"):
                return 0.0
            return x

        print()
        print(
            "KEEP_DEBUG "
            f"V={self.V:g} "
            f"items={d['items']} "
            f"p_count={len(probs)} "
            f"p_min={p_min:.8g} "
            f"p_mean={p_mean:.8g} "
            f"p_max={p_max:.8g}"
        )

        print(
            "KEEP_DEBUG "
            "G "
            f"min={safe_min('g_min'):.8g} "
            f"mean={d['g_sum']/n:.8g} "
            f"max={d['g_max']:.8g} "
            f"zero={d['g_zero']/n:.4f}"
        )

        print(
            "KEEP_DEBUG "
            "DeltaB "
            f"min={safe_min('db_min'):.8g} "
            f"mean={d['db_sum']/n:.8g} "
            f"max={d['db_max']:.8g} "
            f"zero={d['db_zero']/n:.4f}"
        )

        print(
            "KEEP_DEBUG "
            "V*G "
            f"min={safe_min('vg_min'):.8g} "
            f"mean={d['vg_sum']/n:.8g} "
            f"max={d['vg_max']:.8g}"
        )

        print(
            "KEEP_DEBUG "
            "Q*DeltaB "
            f"min={safe_min('qdb_min'):.8g} "
            f"mean={d['qdb_sum']/n:.8g} "
            f"max={d['qdb_max']:.8g}"
        )

        print(
            "KEEP_DEBUG "
            f"mean_VG_over_QDB="
            f"{d['ratio_sum']/n:.8g} "
            f"VG_dominant_fraction="
            f"{d['vg_dominant']/n:.4f} "
            f"Q_mean={d['q_sum']/n:.8g} "
            f"Q_max={d['q_max']:.8g}"
        )


    @staticmethod
    def path_cost(
        topology,
        path,
        size_mb,
        link_usage=None,
    ):
        """
        Congestion-aware transfer-time estimate.

        Keep this consistent with SCOUT:

            effective_bw(e)
              = capacity(e) / (1 + q_e)

        where q_e is the number of currently reserved
        active/pending transfers using link e.
        """

        if link_usage is None:
            link_usage = {}

        bw = min(
            topology.capacity[x]
            / (
                1.0
                + float(
                    link_usage.get(
                        x,
                        0,
                    )
                )
            )
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
        link_usage=None,
    ):
        """
        Compute:

            G_{n,l}
            =
            p_hat_l *
            sum_j
            [C_alt(j,l)-C_n(j,l)]^+

        and expected upper-network avoidance DeltaB.
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

        # Expected upper-network bytes avoided if this
        # replica creates a same-domain delivery option.
        upper_avoidance = 0.0

        for dst in nodes:

            if dst == node:
                continue

            # ------------------------------------------------
            # KEEP_UPPER_LYAPUNOV_V1
            #
            # Marginal same-domain coverage.
            # ------------------------------------------------

            if (
                topology.same_domain(
                    node,
                    dst,
                )
                and layer
                not in relay_cache[dst]
            ):

                other_same_domain_copy = False

                for src in nodes:

                    if (
                        src == node
                        or src == dst
                    ):
                        continue

                    if not topology.same_domain(
                        src,
                        dst,
                    ):
                        continue

                    if (
                        layer
                        in relay_cache[src]
                    ):
                        other_same_domain_copy = True
                        break

                if not other_same_domain_copy:

                    upper_avoidance += (
                        p * size
                    )

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
                    link_usage=link_usage,
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
                    link_usage=link_usage,
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
                    link_usage=link_usage,
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

        return (
            communication_gain,
            upper_avoidance,
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
        current_flows=None,
    ):
        # KEEP_CONGESTION_AWARE_G_V1
        #
        # Use the same occupancy proxy as SCOUT so that
        # KEEP does not evaluate future replicas under an
        # inconsistent uncongested network model.
        link_usage = {}

        if current_flows is not None:

            for flow in current_flows:

                for lid in flow["path"]:

                    link_usage[lid] = (
                        link_usage.get(
                            lid,
                            0,
                        )
                        + 1
                    )

        # Deterministic ordering is required because
        # DP tie-breaking follows item insertion order.
        candidate_layers = sorted(
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
                    link_usage=link_usage,
                )
            )

            vg = (
                self.V
                * g
            )

            qdb = (
                self.virtual_queue
                * delta_b
            )

            if self.debug_enabled:

                d = self._dbg

                d["items"] += 1

                d["g_sum"] += g
                d["g_min"] = min(
                    d["g_min"],
                    g,
                )
                d["g_max"] = max(
                    d["g_max"],
                    g,
                )

                d["db_sum"] += delta_b
                d["db_min"] = min(
                    d["db_min"],
                    delta_b,
                )
                d["db_max"] = max(
                    d["db_max"],
                    delta_b,
                )

                d["vg_sum"] += vg
                d["vg_min"] = min(
                    d["vg_min"],
                    vg,
                )
                d["vg_max"] = max(
                    d["vg_max"],
                    vg,
                )

                d["qdb_sum"] += qdb
                d["qdb_min"] = min(
                    d["qdb_min"],
                    qdb,
                )
                d["qdb_max"] = max(
                    d["qdb_max"],
                    qdb,
                )

                if g <= EPS:
                    d["g_zero"] += 1

                if delta_b <= EPS:
                    d["db_zero"] += 1

                if vg > qdb:
                    d[
                        "vg_dominant"
                    ] += 1

                d["ratio_sum"] += (
                    vg
                    / max(
                        qdb,
                        EPS,
                    )
                )

                d["q_sum"] += (
                    self.virtual_queue
                )

                d["q_max"] = max(
                    d["q_max"],
                    self.virtual_queue,
                )

            objective = (
                vg + qdb
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
