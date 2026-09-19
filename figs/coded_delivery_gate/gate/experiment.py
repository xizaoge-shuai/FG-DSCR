#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import copy
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .fg_adapter import FGDSCRAdapter
from .metrics import build_demand_state, evaluate_gate_metrics


@dataclass
class SplitResult:
    warmup: List[Any]
    target: List[Any]


def split_containers(
    containers: Sequence[Any],
    warmup_fraction: float,
    target_fraction: float,
    split_mode: str,
    seed: int,
) -> SplitResult:
    if not (0.0 <= warmup_fraction <= 1.0):
        raise ValueError("warmup_fraction 必须在 [0,1]")
    if not (0.0 <= target_fraction <= 1.0):
        raise ValueError("target_fraction 必须在 [0,1]")
    if warmup_fraction + target_fraction > 1.0 + 1e-12:
        raise ValueError("warmup_fraction + target_fraction 不能超过 1")

    arr = list(containers)
    if split_mode == "random":
        rng = random.Random(seed)
        rng.shuffle(arr)
    elif split_mode != "sequential":
        raise ValueError("split_mode 仅支持 sequential/random")

    n = len(arr)
    nw = int(n * warmup_fraction)
    nt = int(n * target_fraction)
    if n > 0 and warmup_fraction > 0 and nw == 0:
        nw = 1
    if n - nw > 0 and target_fraction > 0 and nt == 0:
        nt = 1
    nt = min(nt, n - nw)

    return SplitResult(warmup=arr[:nw], target=arr[nw:nw + nt])


class GateExperiment:
    """两窗口门禁实验。"""

    def __init__(
        self,
        repo_root: str | Path,
        scheduler_config: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.adapter = FGDSCRAdapter(repo_root, scheduler_config=scheduler_config)

    def run(
        self,
        case_path: str | Path,
        placement: str = "fg_dscr",
        warmup_fraction: float = 0.5,
        target_fraction: float = 0.5,
        split_mode: str = "sequential",
        seed: int = 42,
        dump_state: bool = False,
    ) -> Dict[str, Any]:
        containers, nodes, layer_sizes = self.adapter.load_case(case_path)
        split = split_containers(
            containers,
            warmup_fraction=warmup_fraction,
            target_fraction=target_fraction,
            split_mode=split_mode,
            seed=seed,
        )

        # Window 0：让历史 placement/order/cache replacement 自然形成异构 H_n。
        warm = self.adapter.run_warmup(split.warmup, nodes, layer_sizes)
        warm_cache = warm.final_cache

        # Window 1：固定 H_n 后，只做 placement，不推进目标窗口缓存。
        if placement == "fg_dscr":
            assignment, target_nodes, failed = self.adapter.place_target_fg_dscr(
                split.target, nodes, layer_sizes, warm_cache
            )
        elif placement == "round_robin":
            assignment, target_nodes, failed = self.adapter.place_target_round_robin(
                split.target, nodes, warm_cache
            )
        elif placement == "random":
            assignment, target_nodes, failed = self.adapter.place_target_random(
                split.target, nodes, warm_cache, seed=seed
            )
        else:
            raise ValueError("placement 仅支持 fg_dscr/round_robin/random")

        target_by_id = {c.cid: c for c in split.target}
        node_ids = [n.eid for n in target_nodes]
        demand, wants = build_demand_state(
            containers_by_id=target_by_id,
            assignment=assignment,
            node_ids=node_ids,
            cache_by_node=warm_cache,
        )

        metrics = evaluate_gate_metrics(wants, warm_cache, layer_sizes)

        result: Dict[str, Any] = {
            "case": str(Path(case_path)),
            "seed": seed,
            "split_mode": split_mode,
            "warmup_fraction": warmup_fraction,
            "target_fraction": target_fraction,
            "placement": placement,
            "num_nodes": len(nodes),
            "num_case_containers": len(containers),
            "num_warmup_containers": len(split.warmup),
            "num_target_containers": len(split.target),
            "num_target_assigned": len(assignment),
            "num_target_failed_or_unassigned": max(len(split.target) - len(assignment), len(failed)),
            "num_warmup_failed": len(warm.failed_container_ids),
            "metrics": metrics,
        }

        # 便于后续复现实验，保留 assignment；H/W 只有显式要求才输出。
        result["target_assignment"] = assignment
        if dump_state:
            result["warm_final_cache"] = {k: sorted(v) for k, v in warm_cache.items()}
            result["target_demand"] = {k: sorted(v) for k, v in demand.items()}
            result["target_wants"] = {k: sorted(v) for k, v in wants.items()}
            result["warmup_assignment"] = warm.assignment
            result["warmup_ordered_queues"] = warm.ordered_queues

        return result


def load_scheduler_config(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)
    if not isinstance(obj, dict):
        raise ValueError("scheduler-config 必须是 JSON object")
    return obj
