"""
CIDER 审稿意见补充实验。

SCOUT 模式：
  audit       原始 SCOUT 与同成本独立选源等价性审计
  independent 每个需求独立选择最低成本来源
  batch       考虑同批次路径竞争的局部搜索

KEEP 模式：
  keep        原始 KEEP
  warmup_lfu  仅使用预热历史频率的缓存策略

LRU 使用原始模拟器已有的 no_keep 模式。

不修改正式算法源码。
"""

import argparse
import atexit
import json
import runpy
import statistics
import sys
import time

from collections import Counter
from pathlib import Path

from secon_exp.cider.scout import ScoutOptimizer
from secon_exp.cider.keep import KeepOptimizer
from secon_exp.cider.pulse import PulseScheduler


EPS = 1e-9


# ============================================================
# 来源、路径与拥塞
# ============================================================

def path(src, dst, topo):
    if src is None:
        return topo.registry_path(dst)

    return topo.peer_path(src, dst)


def options(item, cache):
    result = [None]

    for src in sorted(cache):
        if (
            src != item.node
            and item.layer in cache[src]
        ):
            result.append(src)

    return result


def occupancy(flows):
    counts = Counter()

    for flow in flows or ():
        counts.update(flow["path"])

    return counts


# ============================================================
# 与原 SCOUT 完全相同的成本函数
# ============================================================

def cost(src, item, topo, usage, scout):

    current_path = path(
        src,
        item.node,
        topo,
    )

    registry_path = path(
        None,
        item.node,
        topo,
    )

    def estimated_time(p):

        bw = min(
            topo.capacity[e]
            / (1.0 + usage[e])
            for e in p
        )

        return (
            topo.path_latency_ms(p) / 1000.0
            + item.size_mb / max(bw, EPS)
        )

    time_norm = (
        estimated_time(current_path)
        / max(
            estimated_time(registry_path),
            EPS,
        )
    )

    # 同域传输不计上层流量
    if (
        src is not None
        and topo.same_domain(src, item.node)
    ):
        traffic_norm = 0.0
    else:
        traffic_norm = 1.0

    congestion_norm = sum(
        usage[e] / (1.0 + usage[e])
        for e in current_path
    ) / len(current_path)

    return (
        scout.alpha * time_norm
        + scout.beta * traffic_norm
        + scout.gamma * congestion_norm
    )


# ============================================================
# 独立选源
# ============================================================

def independent(
    items,
    cache,
    topo,
    base,
    scout,
):

    selected = []

    for item in items:

        src = min(
            options(item, cache),
            key=lambda s: (
                cost(
                    s,
                    item,
                    topo,
                    base,
                    scout,
                ),
                "" if s is None else str(s),
            ),
        )

        selected.append(src)

    return selected


# ============================================================
# 同批次竞争成本
# ============================================================

def batch_objective(
    items,
    chosen,
    topo,
    base,
    scout,
):

    # 已有 active/pending 流
    usage = base.copy()

    # 添加本轮所有待启动传输
    for item, src in zip(items, chosen):
        usage.update(
            path(src, item.node, topo)
        )

    total = 0.0

    for item, src in zip(items, chosen):

        own_path = path(
            src,
            item.node,
            topo,
        )

        # 计算本需求成本时排除自身，
        # 但保留本轮其他需求的链路占用。
        for e in own_path:
            usage[e] -= 1

        total += cost(
            src,
            item,
            topo,
            usage,
            scout,
        )

        usage.update(own_path)

    return total


# ============================================================
# Batch-SCOUT：坐标下降局部搜索
# ============================================================

def batch_optimize(
    items,
    cache,
    topo,
    base,
    scout,
    passes,
):

    # 独立选源作为初始解
    chosen = independent(
        items,
        cache,
        topo,
        base,
        scout,
    )

    before = batch_objective(
        items,
        chosen,
        topo,
        base,
        scout,
    )

    current = before
    moves = 0

    for _ in range(passes):

        improved = False

        for i, item in enumerate(items):

            old_src = chosen[i]
            best_src = old_src
            best_value = current

            for src in options(item, cache):

                if src == old_src:
                    continue

                chosen[i] = src

                value = batch_objective(
                    items,
                    chosen,
                    topo,
                    base,
                    scout,
                )

                if value < best_value - 1e-10:
                    best_value = value
                    best_src = src

            chosen[i] = best_src

            if best_src != old_src:

                current = best_value
                moves += 1
                improved = True

        if not improved:
            break

    if current > before + 1e-7:
        raise AssertionError(
            "Batch objective increased unexpectedly"
        )

    return (
        chosen,
        before,
        current,
        moves,
    )


# ============================================================
# 每轮运行耗时统计
# ============================================================

def describe_times(values):

    if not values:
        return {
            "calls": 0,
            "median_ms": 0.0,
            "p95_ms": 0.0,
            "max_ms": 0.0,
        }

    values = sorted(values)

    pos = 0.95 * (len(values) - 1)
    i = int(pos)

    p95 = (
        values[i]
        + (
            values[min(i + 1, len(values) - 1)]
            - values[i]
        ) * (pos - i)
    )

    return {
        "calls": len(values),
        "median_ms": statistics.median(values),
        "p95_ms": p95,
        "max_ms": values[-1],
    }


# ============================================================
# 主函数：运行期策略注入
# ============================================================

def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--scout-mode",
        required=True,
        choices=[
            "audit",
            "independent",
            "batch",
        ],
    )

    parser.add_argument(
        "--cache-policy",
        default="keep",
        choices=[
            "keep",
            "warmup_lfu",
        ],
    )

    parser.add_argument(
        "--batch-passes",
        type=int,
        default=3,
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

    rest = args.runner

    if rest and rest[0] == "--":
        rest = rest[1:]

    if "--out" not in rest:
        parser.error(
            "Underlying CIDER runner needs --out"
        )

    if not 1 <= args.batch_passes <= 8:
        parser.error(
            "batch-passes must be within [1,8]"
        )

    if (
        args.cache_policy == "warmup_lfu"
        and "--ablation" in rest
        and rest[rest.index("--ablation") + 1]
            == "no_keep"
    ):
        parser.error(
            "Cannot combine warmup_lfu and no_keep"
        )

    original_scout = ScoutOptimizer.choose
    original_keep = KeepOptimizer.choose_retention
    original_pulse = PulseScheduler.select

    times = {
        "scout": [],
        "keep": [],
        "pulse": [],
    }

    diag = {
        "mode": args.scout_mode,
        "cache_policy": args.cache_policy,
        "cycles": 0,
        "demands": 0,
        "audit_differing_demands": 0,
        "audit_differing_cycles": 0,
        "batch_moves": 0,
        "batch_improved_cycles": 0,
        "batch_surrogate_before": 0.0,
        "batch_surrogate_after": 0.0,
    }

    # --------------------------------------------------------
    # SCOUT 替换
    # --------------------------------------------------------

    def reviewed_scout(
        self,
        demands,
        relay_cache,
        topology,
        active=None,
    ):

        start = time.perf_counter()

        items = list(demands)
        base = occupancy(active)

        diag["cycles"] += 1
        diag["demands"] += len(items)

        if self.source_concurrency != 0:
            raise RuntimeError(
                "This pilot requires source_concurrency=0"
            )

        if args.scout_mode == "audit":

            same_cost = independent(
                items,
                relay_cache,
                topology,
                base,
                self,
            )

            assigned = original_scout(
                self,
                demands,
                relay_cache,
                topology,
                active=active,
            )

            diff = sum(
                assigned[(item.node, item.layer)] != src
                for item, src
                in zip(items, same_cost)
            )

            diag["audit_differing_demands"] += diff

            if diff > 0:
                diag["audit_differing_cycles"] += 1

        else:

            if args.scout_mode == "independent":

                chosen = independent(
                    items,
                    relay_cache,
                    topology,
                    base,
                    self,
                )

            else:

                (
                    chosen,
                    before,
                    after,
                    moves,
                ) = batch_optimize(
                    items,
                    relay_cache,
                    topology,
                    base,
                    self,
                    args.batch_passes,
                )

                diag["batch_moves"] += moves

                if moves > 0:
                    diag["batch_improved_cycles"] += 1

                diag["batch_surrogate_before"] += before
                diag["batch_surrogate_after"] += after

            # 保留原模拟器使用的 SCOUT 来源统计
            for item, src in zip(items, chosen):

                size = float(item.size_mb)

                peers = options(
                    item,
                    relay_cache,
                )[1:]

                has_same = any(
                    topology.same_domain(p, item.node)
                    for p in peers
                )

                has_cross = any(
                    not topology.same_domain(p, item.node)
                    for p in peers
                )

                flags = [
                    ("has_same", has_same),
                    ("has_cross", has_cross),
                    ("has_any_peer", bool(peers)),
                    ("registry_only", not peers),
                    ("no_same_candidate", not has_same),
                ]

                for key, enabled in flags:

                    if enabled:
                        self.diag[key] += 1
                        self.diag_mb[key + "_mb"] += size

                self.diag["demands"] += 1
                self.diag_mb["demand_mb"] += size

                if src is None:
                    label = "chosen_registry"

                elif topology.same_domain(src, item.node):
                    label = "chosen_same"

                else:
                    label = "chosen_cross"

                self.diag[label] += 1
                self.diag_mb[label + "_mb"] += size

                diag[label] = (
                    diag.get(label, 0) + 1
                )

                diag[label + "_mb"] = (
                    diag.get(label + "_mb", 0.0)
                    + size
                )

            assigned = {
                (item.node, item.layer): src
                for item, src in zip(items, chosen)
            }

        times["scout"].append(
            1000.0 * (time.perf_counter() - start)
        )

        return assigned

    # --------------------------------------------------------
    # KEEP 替换
    # --------------------------------------------------------

    def reviewed_keep(self, *a, **kw):

        start = time.perf_counter()

        try:

            if args.cache_policy == "keep":
                return original_keep(
                    self,
                    *a,
                    **kw,
                )

            # Warmup-frequency retention
            # 不是使用未来请求的 LFU。
            layers = set(
                kw.get(
                    "candidate_layers",
                    a[1] if len(a) > 1 else (),
                )
            )

            cap = float(
                kw.get(
                    "capacity_mb",
                    a[2] if len(a) > 2 else 0.0,
                )
            )

            sizes = kw.get(
                "sizes",
                a[6] if len(a) > 6 else {},
            )

            result = set()
            remaining = cap

            for layer in sorted(
                layers,
                key=lambda x: (
                    -float(self.p_hat.get(x, 0.0)),
                    str(x),
                ),
            ):

                size = float(sizes[layer])

                if size <= remaining + EPS:
                    result.add(layer)
                    remaining -= size

            return result

        finally:

            times["keep"].append(
                1000.0 * (
                    time.perf_counter() - start
                )
            )

    # --------------------------------------------------------
    # PULSE 逐次耗时
    # --------------------------------------------------------

    def reviewed_pulse(self, *a, **kw):

        start = time.perf_counter()

        try:
            return original_pulse(
                self,
                *a,
                **kw,
            )

        finally:
            times["pulse"].append(
                1000.0 * (
                    time.perf_counter() - start
                )
            )

    # --------------------------------------------------------
    # 输出诊断
    # --------------------------------------------------------

    def write_diagnostics():

        dest = Path(
            args.diagnostics
        )

        dest.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        diag["timings_ms"] = {
            key: describe_times(value)
            for key, value in times.items()
        }

        dest.write_text(
            json.dumps(
                diag,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        print(
            "REVIEW DIAGNOSTICS:",
            dest,
            flush=True,
        )

    atexit.register(
        write_diagnostics
    )

    # 只替换当前实验进程的方法
    ScoutOptimizer.choose = reviewed_scout

    KeepOptimizer.choose_retention = reviewed_keep

    PulseScheduler.select = reviewed_pulse

    sys.argv = [
        "run_cider_mainline.py",
        *rest,
    ]

    runpy.run_module(
        "secon_exp.experiments.run_cider_mainline",
        run_name="__main__",
    )


if __name__ == "__main__":
    main()
