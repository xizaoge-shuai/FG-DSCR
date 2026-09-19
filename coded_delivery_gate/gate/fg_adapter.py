#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FG-DSCR 适配层。

目标：不修改上一篇任何源码，直接调用 scripts/fg_dscr.py 中已有的
placement / ordering / PGDSF cache evolution，导出历史窗口结束后的 final cache。
"""

from __future__ import annotations

import copy
import importlib.util
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple


@dataclass
class WarmupResult:
    assignment: Dict[str, str]
    node_to_cids: Dict[str, List[str]]
    ordered_queues: Dict[str, List[str]]
    final_cache: Dict[str, Set[str]]
    failed_container_ids: List[str]


class FGDSCRAdapter:
    """对 master/dev 两个分支尽量保持兼容的轻量适配器。"""

    def __init__(
        self,
        repo_root: str | Path,
        scheduler_config: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.scheduler_path = self.repo_root / "scripts" / "fg_dscr.py"
        if not self.scheduler_path.exists():
            raise FileNotFoundError(
                f"找不到 {self.scheduler_path}。请把压缩包解压到 FG-DSCR 仓库根目录，"
                "或通过 --repo-root 指定仓库路径。"
            )

        self.mod = self._load_module(self.scheduler_path)
        self.scheduler_config = dict(scheduler_config or {})

        required = ["load_case", "FGDscrScheduler"]
        for name in required:
            if not hasattr(self.mod, name):
                raise RuntimeError(f"{self.scheduler_path} 缺少预期符号：{name}")

    @staticmethod
    def _load_module(path: Path):
        spec = importlib.util.spec_from_file_location("fg_dscr_gate_adapter", str(path))
        if spec is None or spec.loader is None:
            raise ImportError(f"无法导入 {path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def load_case(self, case_path: str | Path):
        """直接复用上一篇 load_case，保持 Container/EdgeNode 类型一致。"""
        return self.mod.load_case(str(Path(case_path).resolve()))

    def _new_scheduler(self, layer_sizes: Dict[str, int], algo_name: str):
        """
        只把当前分支支持的配置项传进去，避免 master/dev 参数不一致导致报错。
        未显式配置的参数沿用该分支自己的默认值。
        """
        cls = self.mod.FGDscrScheduler
        sig = inspect.signature(cls.__init__)
        accepted = set(sig.parameters) - {"self"}

        kwargs: Dict[str, Any] = {"layer_sizes_mb": layer_sizes}
        for k, v in self.scheduler_config.items():
            if k in accepted:
                kwargs[k] = v
        if "algo_name" in accepted:
            kwargs["algo_name"] = algo_name
        return cls(**kwargs)

    @staticmethod
    def _copy_nodes_with_cache(nodes: Sequence[Any], cache_by_node: Mapping[str, Set[str]]):
        out = []
        for node in nodes:
            cp = copy.deepcopy(node)
            cp.initial_cache = set(cache_by_node.get(cp.eid, set()))
            out.append(cp)
        return out

    def run_warmup(
        self,
        containers: Sequence[Any],
        nodes: Sequence[Any],
        layer_sizes: Dict[str, int],
    ) -> WarmupResult:
        """
        跑完整历史窗口：Phase-I placement + Phase-II ordering/cache evolution。

        关键点：上一篇 run() 没把 qm.final_cache 写进 JSON，
        这里直接调用其公开/已有方法，在内存中读取 QueueMetrics.final_cache，
        因而无需修改原项目文件。
        """
        scheduler = self._new_scheduler(layer_sizes, algo_name="FG-DSCR-Warmup-Gate")
        scheduler.set_data(list(containers), list(nodes))

        if not containers:
            return WarmupResult(
                assignment={},
                node_to_cids={n.eid: [] for n in nodes},
                ordered_queues={n.eid: [] for n in nodes},
                final_cache={n.eid: set(n.initial_cache) for n in nodes},
                failed_container_ids=[],
            )

        assignment = dict(scheduler.best_response_assignment())
        node_to_cids: Dict[str, List[str]] = {n.eid: [] for n in nodes}
        for cid, eid in assignment.items():
            if eid in node_to_cids:
                node_to_cids[eid].append(cid)

        ordered: Dict[str, List[str]] = {}
        final_cache: Dict[str, Set[str]] = {}

        node_map = {n.eid: n for n in nodes}
        for eid, cids in node_to_cids.items():
            node = node_map[eid]
            seq = scheduler.order_node(list(cids), node)
            ordered[eid] = list(seq)
            qm = scheduler.simulate_queue(list(seq), node)
            final_cache[eid] = set(qm.final_cache)

        failed = list(getattr(scheduler, "failed_container_ids", []))
        return WarmupResult(
            assignment=assignment,
            node_to_cids=node_to_cids,
            ordered_queues=ordered,
            final_cache=final_cache,
            failed_container_ids=failed,
        )

    def place_target_fg_dscr(
        self,
        containers: Sequence[Any],
        nodes: Sequence[Any],
        layer_sizes: Dict[str, int],
        warm_cache: Mapping[str, Set[str]],
    ) -> Tuple[Dict[str, str], List[Any], List[str]]:
        """在历史 final cache 上仅执行目标窗口 placement，不推进目标窗口 cache。"""
        target_nodes = self._copy_nodes_with_cache(nodes, warm_cache)
        scheduler = self._new_scheduler(layer_sizes, algo_name="FG-DSCR-Target-Placement")
        scheduler.set_data(list(containers), target_nodes)
        if not containers:
            return {}, target_nodes, []
        assignment = dict(scheduler.best_response_assignment())
        failed = list(getattr(scheduler, "failed_container_ids", []))
        return assignment, target_nodes, failed

    @staticmethod
    def place_target_round_robin(
        containers: Sequence[Any],
        nodes: Sequence[Any],
        warm_cache: Mapping[str, Set[str]],
    ) -> Tuple[Dict[str, str], List[Any], List[str]]:
        """
        通信门禁用的无缓存感知控制组。
        这里故意不利用 layer/cache 信息；它不是完整 K8s scheduler baseline。
        """
        target_nodes = FGDSCRAdapter._copy_nodes_with_cache(nodes, warm_cache)
        eids = [n.eid for n in target_nodes]
        if not eids:
            return {}, target_nodes, [c.cid for c in containers]
        assignment = {c.cid: eids[i % len(eids)] for i, c in enumerate(containers)}
        return assignment, target_nodes, []

    @staticmethod
    def place_target_random(
        containers: Sequence[Any],
        nodes: Sequence[Any],
        warm_cache: Mapping[str, Set[str]],
        seed: int,
    ) -> Tuple[Dict[str, str], List[Any], List[str]]:
        import random

        target_nodes = FGDSCRAdapter._copy_nodes_with_cache(nodes, warm_cache)
        eids = [n.eid for n in target_nodes]
        if not eids:
            return {}, target_nodes, [c.cid for c in containers]
        rng = random.Random(seed)
        assignment = {c.cid: rng.choice(eids) for c in containers}
        return assignment, target_nodes, []
