from __future__ import annotations

import argparse
import atexit
import json
import runpy
import sys
from pathlib import Path

from secon_exp.cider.keep import KeepOptimizer


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--local-time",
        type=float,
        default=0.0,
    )

    ap.add_argument(
        "--local-upper",
        type=float,
        default=0.0,
    )

    ap.add_argument(
        "--diagnostics",
        required=True,
    )

    ap.add_argument(
        "runner",
        nargs=argparse.REMAINDER,
    )

    args = ap.parse_args()

    if args.local_time < 0 or args.local_upper < 0:
        ap.error("Local weights must be nonnegative")

    rest = args.runner

    if rest and rest[0] == "--":
        rest = rest[1:]

    original_value = KeepOptimizer.value

    stats = {
        "calls": 0,
        "positive_local_time": 0,
        "positive_local_upper": 0,
        "local_time_sum": 0.0,
        "local_upper_sum": 0.0,
    }

    def local_aware_value(
        self,
        node,
        layer,
        nodes,
        relay_cache,
        topology,
        sizes,
        link_usage=None,
    ):
        # 保留原版的所有远程通信收益计算。
        g, b = original_value(
            self,
            node=node,
            layer=layer,
            nodes=nodes,
            relay_cache=relay_cache,
            topology=topology,
            sizes=sizes,
            link_usage=link_usage,
        )

        stats["calls"] += 1

        # 严格控制组：原样返回，确保结果一致。
        if (
            args.local_time == 0
            and args.local_upper == 0
        ):
            return g, b

        p = float(self.p_hat.get(layer, 0.0))

        if p <= 0:
            return g, b

        size = float(sizes[layer])

        # 假设不在本节点保留此层，
        # 本节点未来请求它时的最优替代传输来源。
        registry_path = topology.registry_path(node)

        best_time = self.path_cost(
            topology,
            registry_path,
            size,
            link_usage=link_usage,
        )

        # Registry 下载消耗 Upper-tier traffic。
        best_uses_upper = True

        for src in sorted(nodes):

            if src == node:
                continue

            if layer not in relay_cache.get(src, ()):
                continue

            peer_path = topology.peer_path(src, node)

            peer_time = self.path_cost(
                topology,
                peer_path,
                size,
                link_usage=link_usage,
            )

            if peer_time < best_time:
                best_time = peer_time

                best_uses_upper = (
                    not topology.same_domain(src, node)
                )

        # 本地命中避免一次远程取层，
        # 这里采用历史全局概率作为未来本地需求代理。
        local_g = p * best_time

        local_b = (
            p * size
            if best_uses_upper
            else 0.0
        )

        stats["positive_local_time"] += int(local_g > 0)
        stats["positive_local_upper"] += int(local_b > 0)

        stats["local_time_sum"] += local_g
        stats["local_upper_sum"] += local_b

        return (
            g + args.local_time * local_g,
            b + args.local_upper * local_b,
        )

    KeepOptimizer.value = local_aware_value

    def write_stats():
        dest = Path(args.diagnostics)
        dest.parent.mkdir(parents=True, exist_ok=True)

        dest.write_text(
            json.dumps({
                "local_time_weight": args.local_time,
                "local_upper_weight": args.local_upper,
                "stats": stats,
            }, indent=2),
            encoding="utf-8",
        )

        print(
            "LOCAL_VALUE_DIAGNOSTICS",
            json.dumps(stats),
            flush=True,
        )

    atexit.register(write_stats)

    # 复用现有 SCOUT 审计、CIDER runner。
    sys.argv = [
        "review_mechanisms.py",
        "--scout-mode", "audit",
        "--cache-policy", "keep",
        "--diagnostics",
        str(Path(args.diagnostics).with_name(
            "review_diagnostics.json"
        )),
        "--",
        *rest,
    ]

    runpy.run_module(
        "scripts.review_mechanisms",
        run_name="__main__",
    )


if __name__ == "__main__":
    main()
