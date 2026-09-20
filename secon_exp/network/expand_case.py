from __future__ import annotations

import copy
import random
from typing import Dict


def expand_nodes(
    case: Dict,
    num_nodes: int,
    num_domains: int,
    seed: int,
) -> Dict:

    if num_nodes < 2:
        raise ValueError("num_nodes must be >= 2")

    if num_domains < 1:
        raise ValueError("num_domains must be >= 1")

    rng = random.Random(seed)

    out = copy.deepcopy(case)

    templates = list(
        case["nodes"]
    )

    nodes = []

    for i in range(num_nodes):

        base = copy.deepcopy(
            templates[
                i % len(templates)
            ]
        )

        eid = f"edge-{i+1}"

        base["eid"] = eid

        base["domain"] = (
            i % num_domains
        )

        # 保留原节点资源异构性，
        # 再加入小幅确定性扰动。
        bw = float(
            base["bandwidth_mb_s"]
        )

        bw *= rng.uniform(
            0.8,
            1.2,
        )

        base["bandwidth_mb_s"] = round(
            bw,
            3,
        )

        base["initial_cache"] = []

        nodes.append(base)

    out["nodes"] = nodes

    return out
