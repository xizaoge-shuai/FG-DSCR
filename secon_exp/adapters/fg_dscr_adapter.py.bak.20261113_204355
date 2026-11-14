from __future__ import annotations

import copy
import importlib.util
import random
import sys
from pathlib import Path
from typing import Any, Dict, Tuple


DEFAULT_FG_PARAMS = dict(
    beam_width=1,
    phase1_neighbor_mode="move",
    max_best_response_rounds=5,
    move_topk_per_node=6,
    hard_resource_filter=True,
    lambda_fail=1000,
    lambda_cong=1,
    lambda_frag=10,
    lambda_aff=0,
    k_pin=6,
    cache_policy="pgdsf",
    order_policy="dynamic_state",
    greedy_load_factor=0.8,
    init_order="resource_asc",
)


class FGDSCRAdapter:
    """
    Adapter between the previous FG-DSCR implementation and the new SECON
    communication experiments.

    The original scripts/fg_dscr.py is deliberately left untouched.

    Responsibilities:
      1. run FG-DSCR placement;
      2. run Phase-II ordering/cache evolution;
      3. expose naturally evolved per-node final caches;
      4. construct historical/target workload windows.
    """

    def __init__(
        self,
        fg_path: str = "scripts/fg_dscr.py",
        scheduler_params: Dict[str, Any] | None = None,
    ):
        self.fg_path = Path(fg_path)
        if not self.fg_path.is_file():
            raise FileNotFoundError(self.fg_path)

        self.mod = self._load_module(self.fg_path)
        self.params = dict(DEFAULT_FG_PARAMS)
        if scheduler_params:
            self.params.update(scheduler_params)

    @staticmethod
    def _load_module(path: Path):
        name = "_secon_fg_dscr_adapter"
        spec = importlib.util.spec_from_file_location(name, str(path))
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot import {path}")

        mod = importlib.util.module_from_spec(spec)

        # Required by Python 3.10 dataclasses during dynamic import.
        sys.modules[name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            sys.modules.pop(name, None)
            raise

        return mod

    def build_data(self, case: Dict[str, Any]):
        m = self.mod

        containers = []
        for x in case["containers"]:
            containers.append(
                m.Container(
                    cid=x["cid"],
                    layers=set(x["layers"]),
                    resources=dict(x["resources"]),
                    run_time=float(x["run_time"]),
                    service_type=x.get(
                        "service_type",
                        x.get("image_type", "default"),
                    ),
                )
            )

        nodes = []
        for x in case["nodes"]:
            nodes.append(
                m.EdgeNode(
                    eid=x["eid"],
                    resources=dict(x["resources"]),
                    repo_capacity_mb=int(x["repo_capacity_mb"]),
                    bandwidth_mb_s=float(x["bandwidth_mb_s"]),
                    initial_cache=set(x.get("initial_cache", [])),
                )
            )

        sizes = dict(case["layer_sizes_mb"])
        return containers, nodes, sizes

    def make_scheduler(self, case: Dict[str, Any]):
        containers, nodes, sizes = self.build_data(case)

        scheduler = self.mod.FGDscrScheduler(
            layer_sizes_mb=sizes,
            **self.params,
        )
        scheduler.set_data(containers, nodes)
        return scheduler

    def run(
        self,
        case: Dict[str, Any],
        capture_final_cache: bool = True,
    ) -> Dict[str, Any]:
        """
        Execute FG Phase-I placement followed by the original Phase-II
        ordering/cache evolution.

        Unlike scheduler.run(), this method preserves QueueMetrics.final_cache.
        """
        scheduler = self.make_scheduler(case)

        assignment_map = scheduler.best_response_assignment()

        node_to_cids = {
            eid: [] for eid in scheduler.nodes
        }
        for cid, eid in assignment_map.items():
            node_to_cids[eid].append(cid)

        ordered = {}
        final_cache = {}
        node_metrics = {}

        for eid, cids in node_to_cids.items():
            node = scheduler.nodes[eid]
            seq = scheduler.order_node(cids, node)
            qm = scheduler.simulate_queue(seq, node)

            ordered[eid] = list(seq)
            final_cache[eid] = sorted(qm.final_cache)

            node_metrics[eid] = {
                "act": qm.act,
                "ams": qm.ams,
                "downloaded_mb": qm.downloaded_mb,
                "reused_mb": qm.reused_mb,
                "final_cache_mb": sum(
                    scheduler.layer_sizes_mb[l]
                    for l in qm.final_cache
                ),
            }

        return {
            "assignment": node_to_cids,
            "ordered_queues": ordered,
            "final_cache": final_cache if capture_final_cache else {},
            "failed_containers": list(
                getattr(scheduler, "failed_container_ids", [])
            ),
            "node_metrics": node_metrics,
        }

    def run_path(self, case_path: str) -> Dict[str, Any]:
        import json

        case = json.loads(
            Path(case_path).read_text(encoding="utf-8")
        )
        return self.run(case)

    @staticmethod
    def scale_cache_capacity(
        case: Dict[str, Any],
        scale: float,
    ) -> Dict[str, Any]:
        if scale < 0:
            raise ValueError("cache scale must be >= 0")

        out = copy.deepcopy(case)

        for node in out["nodes"]:
            node["repo_capacity_mb"] = int(
                round(float(node["repo_capacity_mb"]) * scale)
            )

        return out

    @staticmethod
    def apply_cache_state(
        case: Dict[str, Any],
        cache_state: Dict[str, list[str]],
    ) -> Dict[str, Any]:
        out = copy.deepcopy(case)

        for node in out["nodes"]:
            eid = node["eid"]
            node["initial_cache"] = list(cache_state.get(eid, []))

        return out

    @staticmethod
    def split_windows(
        case: Dict[str, Any],
        warmup_fraction: float = 0.5,
        seed: int = 1,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Split historical warm-up and target requests.

        Arrival-aware cases:
            use a chronological split so the warm-up
            window strictly precedes the target window.

        Legacy cases without arrival_time_s:
            preserve the previous seeded random split.
        """

        if not 0 < warmup_fraction < 1:
            raise ValueError(
                "warmup_fraction must be in (0, 1)"
            )

        containers = list(
            case["containers"]
        )

        if len(containers) < 2:
            raise ValueError(
                "at least two containers are required"
            )

        n_warm = max(
            1,
            min(
                len(containers) - 1,
                int(
                    round(
                        len(containers)
                        * warmup_fraction
                    )
                ),
            ),
        )

        has_arrivals = all(
            "arrival_time_s" in c
            for c in containers
        )

        warm = copy.deepcopy(case)
        target = copy.deepcopy(case)

        if has_arrivals:

            ordered = sorted(
                containers,
                key=lambda c: (
                    float(
                        c["arrival_time_s"]
                    ),
                    str(c["cid"]),
                ),
            )

            warm[
                "containers"
            ] = [
                copy.deepcopy(c)
                for c in ordered[:n_warm]
            ]

            target[
                "containers"
            ] = [
                copy.deepcopy(c)
                for c in ordered[n_warm:]
            ]

            # Rebase each independent simulation window
            # to t=0 while preserving all relative gaps.
            for window_name, window in (
                ("warmup", warm),
                ("target", target),
            ):

                cs = window[
                    "containers"
                ]

                t0 = min(
                    float(
                        c["arrival_time_s"]
                    )
                    for c in cs
                )

                for c in cs:
                    c[
                        "arrival_time_s"
                    ] = (
                        float(
                            c[
                                "arrival_time_s"
                            ]
                        )
                        - t0
                    )

                meta = window.setdefault(
                    "meta",
                    {},
                )

                meta[
                    "window_split"
                ] = "chronological"

                meta[
                    "window_role"
                ] = window_name

                meta[
                    "window_original_t0_s"
                ] = t0

            return warm, target

        # ----------------------------------------------------
        # Legacy behavior for old cases without arrival times.
        # ----------------------------------------------------

        cids = [
            c["cid"]
            for c in containers
        ]

        rng = random.Random(seed)

        shuffled = list(cids)
        rng.shuffle(shuffled)

        warm_ids = set(
            shuffled[:n_warm]
        )

        warm[
            "containers"
        ] = [
            copy.deepcopy(c)
            for c in containers
            if c["cid"] in warm_ids
        ]

        target[
            "containers"
        ] = [
            copy.deepcopy(c)
            for c in containers
            if c["cid"] not in warm_ids
        ]

        return warm, target
