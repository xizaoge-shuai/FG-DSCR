from __future__ import annotations

import argparse
import math
import runpy
import sys
from collections import Counter

from secon_exp.cider.keep import KeepOptimizer


EPS = 1e-9


def transfer_time(topo, path, size, usage):
    """当前链路占用下的传输时间估计。"""

    bandwidth = min(
        float(topo.capacity[edge]) / (1.0 + usage[edge])
        for edge in path
    )

    return (
        float(topo.path_latency_ms(path)) / 1000.0
        + float(size) / max(bandwidth, EPS)
    )


def make_v2(mode):

    def choose(
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
        candidates = sorted(set(candidate_layers))
        capacity_mb = float(capacity_mb)

        if capacity_mb <= EPS:
            return set()

        # 缓存没有压力时，直接保留全部候选层
        if (
            sum(float(sizes[x]) for x in candidates)
            <= capacity_mb + EPS
        ):
            return set(candidates)

        # 当前正在使用链路的占用量
        usage = Counter()

        if mode == "v2_full":
            for flow in (current_flows or ()):
                usage.update(flow["path"])

        # 只考虑与候选节点同域的其他节点
        neighbors = (
            sorted(
                dst for dst in nodes
                if (
                    dst != node
                    and topology.same_domain(node, dst)
                )
            )
            if mode == "v2_full"
            else []
        )

        scored = []

        for layer in candidates:
            size = float(sizes[layer])

            if size <= 0:
                raise ValueError(
                    f"Invalid layer size: {layer}={size}"
                )

            # 只使用 warmup 历史频率
            frequency = float(
                self.p_hat.get(layer, 0.0)
            )

            gain = 0.0

            if neighbors and frequency > 0:

                improvements = []

                for dst in neighbors:

                    # 目标节点已有该层，则不需要远程传输
                    if layer in relay_cache.get(dst, ()):
                        continue

                    # 增加 node 作为源后的传输成本
                    candidate_path = topology.peer_path(
                        node, dst
                    )

                    candidate_t = transfer_time(
                        topology,
                        candidate_path,
                        size,
                        usage,
                    )

                    # 默认替代来源：Registry
                    registry_t = transfer_time(
                        topology,
                        topology.registry_path(dst),
                        size,
                        usage,
                    )

                    alternative_t = registry_t

                    # 考虑其他已有同域副本
                    for src in neighbors:

                        if src == dst:
                            continue

                        if (
                            layer
                            not in relay_cache.get(src, ())
                        ):
                            continue

                        other_t = transfer_time(
                            topology,
                            topology.peer_path(src, dst),
                            size,
                            usage,
                        )

                        alternative_t = min(
                            alternative_t,
                            other_t,
                        )

                    # 保留该层所能增加的通信收益
                    improvement = (
                        max(
                            0.0,
                            alternative_t - candidate_t,
                        )
                        / max(registry_t, EPS)
                    )

                    improvements.append(improvement)

                # 无量纲边际收益
                # 未被计入的同域节点相当于零收益
                gain = (
                    sum(improvements) / len(neighbors)
                )

            # v2_no_comm:
            #     score = p(layer) / sqrt(size)
            #
            # v2_full:
            #     score = p(layer) * (1 + gain) / sqrt(size)
            #
            # 两者只相差通信增益项。
            score = (
                frequency
                * (1.0 + gain)
                / math.sqrt(size)
            )

            scored.append(
                (
                    -score,
                    -frequency,
                    size,
                    str(layer),
                    layer,
                )
            )

        scored.sort()

        selected = set()
        remaining = capacity_mb

        # 按效用密度贪心选择
        for _, _, size, _, layer in scored:

            if size <= remaining + EPS:
                selected.add(layer)
                remaining -= size

        actual_used = sum(
            float(sizes[x])
            for x in selected
        )

        if actual_used > capacity_mb + 1e-7:
            raise AssertionError(
                "KEEP-v2 cache capacity overflow"
            )

        return selected

    return choose


def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--algorithm",
        required=True,
        choices=[
            "v2_no_comm",
            "v2_full",
        ],
    )

    parser.add_argument(
        "--diagnostics",
        required=True,
    )

    parser.add_argument(
        "runner",
        nargs=argparse.REMAINDER,
    )

    args = parser.parse_args()

    tail = args.runner

    if tail and tail[0] == "--":
        tail = tail[1:]

    # 运行期替换 KEEP，保留仓库原始文件不变。
    KeepOptimizer.choose_retention = make_v2(
        args.algorithm
    )

    # 复用此前已经测试过的实验封装：
    # Independent-SameCost + 新 KEEP
    sys.argv = [
        "review_mechanisms.py",

        "--scout-mode",
        "independent",

        "--cache-policy",
        "keep",

        "--diagnostics",
        args.diagnostics,

        "--",

        *tail,
    ]

    runpy.run_module(
        "scripts.review_mechanisms",
        run_name="__main__",
    )


if __name__ == "__main__":
    main()
