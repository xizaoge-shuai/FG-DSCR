from __future__ import annotations

import copy
import random


def expand_case_nodes(
    case,
    num_nodes,
    num_domains,
    seed=1,
):
    rng = random.Random(seed)

    out = copy.deepcopy(case)

    templates = list(
        case["nodes"]
    )

    nodes = []

    for i in range(num_nodes):
        src = templates[
            i % len(templates)
        ]

        n = copy.deepcopy(src)

        n["eid"] = f"edge-{i+1}"

        n["domain"] = (
            i % num_domains
        )

        bw = float(
            n.get(
                "bandwidth_mb_s",
                100.0,
            )
        )

        # 保持原资源分布，只加入小幅异构性
        n["bandwidth_mb_s"] = round(
            bw * rng.uniform(
                0.8,
                1.2,
            ),
            3,
        )

        n["initial_cache"] = []

        nodes.append(n)

    out["nodes"] = nodes

    out.setdefault(
        "metadata",
        {}
    )

    out["metadata"].update(
        {
            "num_edge_nodes": num_nodes,
            "num_domains": num_domains,
            "node_expansion_seed": seed,
        }
    )

    return out
