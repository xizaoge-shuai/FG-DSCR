#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Paper-style GAHRL training adapter for FG-DSCR case JSON files.

Implements the paper's hybrid structure:
  * GCN + Factorization Machine (FM) state extraction for placement;
  * Dueling DQN for discrete node placement;
  * DDPG-style actor for continuous CPU allocation;
  * replay buffer, Bellman target, policy-gradient update, soft target update.

IMPORTANT REPRODUCTION NOTE
--------------------------
The GAHRL paper does NOT report several implementation hyperparameters (e.g.
learning rate, mini-batch size, replay-buffer size/threshold, gamma, tau, hidden
width, FM embedding size, or epsilon schedule). Their values below are explicit
engineering defaults, NOT values claimed to be reported by the paper. The paper
reports 1500 episodes in the reward-curve experiment and states that GAHRL
stabilizes at about 400 episodes. The script defaults to 1500 episodes.

The input is the repository's case schema (layer_sizes_mb/containers/nodes).
Fields absent from that schema are mapped to paper-range defaults and recorded
in output metadata. This is a GAHRL-paper-style adaptation to the existing
trace, not an exact reproduction of the authors' unpublished code.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import os
import random
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import torch
from torch import nn

# These are the fixed normalization references used by the current FG-DSCR
# formal audit. Keep them visible in output so J can be interpreted correctly.
REF_D = 440.7696
REF_CA = 0.77924
REF_FRAG = 0.180069
EPS = 1e-9


def node_id(n: dict) -> str:
    for k in ("eid", "id", "nid", "name", "node_id"):
        if n.get(k) is not None:
            return str(n[k])
    raise ValueError(f"Node has no recognized ID field: {n}")


def layer_mb(layer_sizes: dict, layer: str) -> float:
    try:
        v = float(layer_sizes.get(str(layer), 1.0))
    except (TypeError, ValueError):
        v = 1.0
    return v if v > 0 else 1.0


def load_case(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        case = json.load(f)
    for key in ("containers", "nodes", "layer_sizes_mb"):
        if key not in case:
            raise ValueError(f"Case file must contain top-level key {key!r}: {path}")
    if not case["nodes"]:
        raise ValueError("Case contains no nodes")
    if not case["containers"]:
        raise ValueError("Case contains no containers")
    return case


def _edge_pair(edge: Any) -> Optional[Tuple[str, str]]:
    if isinstance(edge, (list, tuple)) and len(edge) >= 2:
        return str(edge[0]), str(edge[1])
    if isinstance(edge, dict):
        a = next((edge[k] for k in ("source", "src", "from", "u", "node1", "n1") if k in edge), None)
        b = next((edge[k] for k in ("target", "dst", "to", "v", "node2", "n2") if k in edge), None)
        if a is not None and b is not None:
            return str(a), str(b)
    return None


def build_adjacency(case: dict, nodes: List[dict], seed: int) -> Tuple[np.ndarray, str]:
    ids = [node_id(n) for n in nodes]
    idx = {x: i for i, x in enumerate(ids)}
    n = len(nodes)
    matrix = None
    source = ""
    for key in ("adjacency_matrix", "adjacency", "A"):
        raw = case.get(key)
        if isinstance(raw, list) and len(raw) == n and all(isinstance(row, list) and len(row) == n for row in raw):
            try:
                matrix = np.asarray(raw, dtype=np.float32)
                source = f"case.{key}"
                break
            except Exception:
                matrix = None
    if matrix is None:
        edges = None
        for key in ("edges", "links"):
            if isinstance(case.get(key), list):
                edges = case[key]
                source = f"case.{key}"
                break
        if edges is None and isinstance(case.get("topology"), dict):
            for key in ("edges", "links"):
                if isinstance(case["topology"].get(key), list):
                    edges = case["topology"][key]
                    source = f"case.topology.{key}"
                    break
        matrix = np.zeros((n, n), dtype=np.float32)
        if edges is not None:
            for edge in edges:
                pair = _edge_pair(edge)
                if not pair:
                    continue
                a, b = pair
                if a in idx and b in idx and a != b:
                    matrix[idx[a], idx[b]] = 1.0
                    matrix[idx[b], idx[a]] = 1.0
        else:
            # The case format used by FG-DSCR often has no explicit graph.
            # Use a deterministic connected random graph, matching the paper's
            # use of randomly generated NetworkX topologies as closely as possible.
            rng = random.Random(seed)
            p = min(0.45, max(0.20, 2.5 / max(n, 1)))
            for i in range(n):
                for j in range(i + 1, n):
                    if rng.random() < p:
                        matrix[i, j] = matrix[j, i] = 1.0
            # Ensure connectedness by adding a chain if random sampling leaves gaps.
            for i in range(n - 1):
                matrix[i, i + 1] = matrix[i + 1, i] = 1.0
            source = f"synthetic_connected_random_graph(seed={seed})"
    matrix = ((matrix + matrix.T) > 0).astype(np.float32)
    np.fill_diagonal(matrix, 0.0)
    # Symmetric GCN normalization: D^-1/2 (A + I) D^-1/2.
    a_hat = matrix + np.eye(n, dtype=np.float32)
    degree = a_hat.sum(axis=1)
    inv_sqrt = np.diag(1.0 / np.sqrt(np.maximum(degree, EPS)).astype(np.float32))
    normalized = inv_sqrt @ a_hat @ inv_sqrt
    return normalized.astype(np.float32), source


@dataclass
class Snapshot:
    node_x: np.ndarray
    layer_x: np.ndarray
    svc_x: np.ndarray
    ra_x: np.ndarray
    f_min: np.ndarray
    f_max: np.ndarray
    valid: np.ndarray


class GAHRLEnvironment:
    def __init__(self, case: dict, args: argparse.Namespace):
        self.case = case
        self.args = args
        self.raw_containers = case["containers"]
        self.raw_nodes = case["nodes"]
        self.layer_sizes = {str(k): float(v) for k, v in case["layer_sizes_mb"].items()}
        self.containers = []
        for c in self.raw_containers:
            item = dict(c)
            item["cid"] = str(c.get("cid", c.get("id", c.get("name", ""))))
            item["layers"] = [str(x) for x in (c.get("layers", []) or [])]
            item["resources"] = {str(k): float(v) for k, v in (c.get("resources", {}) or {}).items()}
            item["run_time"] = float(c.get("run_time", 0.0) or 0.0)
            if not item["cid"]:
                raise ValueError(f"Container has no cid/id/name: {c}")
            self.containers.append(item)
        self.nodes = []
        for n in self.raw_nodes:
            item = dict(n)
            item["_id"] = node_id(n)
            item["resources"] = {str(k): float(v) for k, v in (n.get("resources", {}) or {}).items()}
            # The project schema stores bandwidth in MB/s. If absent, use the
            # paper's default 60 Mbps = 7.5 MB/s.
            if n.get("bandwidth_mb_s") is not None:
                item["bandwidth_mb_s"] = max(float(n["bandwidth_mb_s"]), EPS)
                item["_bandwidth_source"] = "case.bandwidth_mb_s"
            elif n.get("bandwidth") is not None:
                item["bandwidth_mb_s"] = max(float(n["bandwidth"]), EPS)
                item["_bandwidth_source"] = "case.bandwidth (assumed MB/s)"
            else:
                item["bandwidth_mb_s"] = 7.5
                item["_bandwidth_source"] = "paper default 60 Mbps converted to 7.5 MB/s"
            cap = n.get("repo_capacity_mb", n.get("cache_capacity_mb", n.get("cache_mb", 1024.0)))
            item["repo_capacity_mb"] = max(float(cap), 0.0)
            item["initial_cache"] = [str(x) for x in (n.get("initial_cache", []) or [])]
            self.nodes.append(item)
        self.node_ids = [n["_id"] for n in self.nodes]
        self.node_index = {nid: i for i, nid in enumerate(self.node_ids)}
        all_layers = set(self.layer_sizes)
        for c in self.containers:
            all_layers.update(c["layers"])
        for n in self.nodes:
            all_layers.update(n["initial_cache"])
        self.layer_ids = sorted(all_layers)
        if not self.layer_ids:
            self.layer_ids = ["__empty_layer_catalog__"]
        for layer in self.layer_ids:
            self.layer_sizes.setdefault(layer, 1.0)
        self.layer_index = {layer: i for i, layer in enumerate(self.layer_ids)}
        self.resource_keys = sorted({k for n in self.nodes for k in n["resources"]} | {k for c in self.containers for k in c["resources"]})
        if not self.resource_keys:
            self.resource_keys = ["cpu", "mem", "disk"]
        self.cpu_key = "cpu" if "cpu" in self.resource_keys else self.resource_keys[0]
        self.max_resource = {k: max([n["resources"].get(k, 0.0) for n in self.nodes] + [EPS]) for k in self.resource_keys}
        self.max_bw = max([n["bandwidth_mb_s"] for n in self.nodes] + [EPS])
        self.max_cache = max([n["repo_capacity_mb"] for n in self.nodes] + [1.0])
        self.max_image_mb = max([self.image_size(c) for c in self.containers] + [1.0])
        self.max_runtime = max([c["run_time"] for c in self.containers] + [1.0])
        self.max_request_count = max([self.service_values(c)[0] for c in self.containers] + [1.0])
        self.layer_pop = {}
        for c in self.containers:
            for layer in set(c["layers"]):
                self.layer_pop[layer] = self.layer_pop.get(layer, 0) + 1
        self.adj_norm, self.graph_source = build_adjacency(case, self.nodes, args.seed)
        self.raw_adj = (self.adj_norm.copy() > 0).astype(np.float32)
        np.fill_diagonal(self.raw_adj, 0.0)
        self.reset()

    def image_size(self, c: dict) -> float:
        return sum(layer_mb(self.layer_sizes, x) for x in c.get("layers", []))

    def service_values(self, c: dict) -> Tuple[float, float, float, float]:
        # Fields absent in FG-DSCR cases are set to midpoints of the ranges in
        # GAHRL Table 2 / Section 5.1; values are emitted in output metadata.
        d = float(c.get("request_count", c.get("d_q", 1.0)) or 1.0)
        rs = float(c.get("request_size_mbits", c.get("rs_q_mbits", self.args.request_size_mbits)))
        rc = float(c.get("compute_complexity_ghz", c.get("rc_q_ghz", self.args.compute_complexity_ghz)))
        p = float(c.get("unit_reward", c.get("p_q", self.args.unit_reward)))
        return max(d, 0.0), max(rs, 0.0), max(rc, 0.0), max(p, 0.0)

    def reset(self) -> None:
        self.caches = {n["_id"]: set(n["initial_cache"]) for n in self.nodes}
        self.used = {n["_id"]: {k: 0.0 for k in self.resource_keys} for n in self.nodes}
        self.node_clock = {n["_id"]: 0.0 for n in self.nodes}
        self.assignment = {n["_id"]: [] for n in self.nodes}
        self.cloud_assignments = []
        self.ordered_queues = {n["_id"]: [] for n in self.nodes}
        self.container_metrics = {}
        self.allocations = {}
        self.total_downloaded = 0.0  # edge-side layer pulls; aligns with current audit metric
        self.cloud_downloaded_mb = 0.0
        self.total_reused = 0.0
        self.total_reward = 0.0
        self.edge_finish_times = []
        self.all_finish_times = []
        self.cloud_count = 0

    def _service_vector(self, c: dict) -> np.ndarray:
        d, rs, rc, p = self.service_values(c)
        r = c.get("resources", {})
        values = [
            d / max(self.max_request_count, 1.0),
            rs / 1.0,                         # paper request size range: 0.5--1 Mbit
            rc / 0.005,                       # paper text: 0.002--0.005 GHz
            p / 0.6,                           # paper unit reward range: 0.5--0.6
            self.image_size(c) / max(self.max_image_mb, 1.0),
            float(c.get("run_time", 0.0)) / max(self.max_runtime, 1.0),
        ]
        for key in self.resource_keys:
            values.append(float(r.get(key, 0.0)) / max(self.max_resource.get(key, 1.0), EPS))
        return np.asarray(values, dtype=np.float32)

    def _layer_vector(self, c: dict) -> np.ndarray:
        # Paper state: required-layer vector plus one local-layer vector per node.
        l = len(self.layer_ids)
        out = np.zeros((len(self.nodes) + 1, l), dtype=np.float32)
        for layer in c["layers"]:
            if layer in self.layer_index:
                out[0, self.layer_index[layer]] = 1.0
        for i, n in enumerate(self.nodes):
            for layer in self.caches[n["_id"]]:
                if layer in self.layer_index:
                    out[i + 1, self.layer_index[layer]] = 1.0
        return out.reshape(-1)

    def snapshot(self, index: int) -> Snapshot:
        c = self.containers[index]
        node_x = []
        ra_x = []
        f_min_base = []
        f_max = []
        base_valid = []
        f_min_delay = []
        delay_valid = []
        cpu_demand = float(c["resources"].get(self.cpu_key, 0.0))
        run_time = float(c.get("run_time", 0.0))
        for ni, n in enumerate(self.nodes):
            nid = n["_id"]
            cap = n["resources"]
            used = self.used[nid]
            remain = {k: float(cap.get(k, 0.0)) - float(used.get(k, 0.0)) for k in self.resource_keys}
            features = []
            # Capacity, remaining and used ratios for each resource dimension.
            for key in self.resource_keys:
                features.append(float(cap.get(key, 0.0)) / max(self.max_resource.get(key, 1.0), EPS))
            for key in self.resource_keys:
                features.append(max(0.0, remain[key]) / max(float(cap.get(key, 0.0)), EPS))
            for key in self.resource_keys:
                features.append(max(0.0, float(used.get(key, 0.0))) / max(float(cap.get(key, 0.0)), EPS))
            cache_cap = float(n["repo_capacity_mb"])
            cache_bytes = sum(layer_mb(self.layer_sizes, x) for x in self.caches[nid])
            features.extend([
                n["bandwidth_mb_s"] / max(self.max_bw, EPS),
                cache_cap / max(self.max_cache, EPS),
                min(cache_bytes / max(cache_cap, EPS), 2.0) if cache_cap > 0 else 0.0,
                len(self.caches[nid]) / max(len(self.layer_ids), 1),
            ])
            node_x.append(features)
            cpu_cap = float(cap.get(self.cpu_key, 0.0))
            cpu_rem = float(remain.get(self.cpu_key, 0.0))
            ra_x.append([cpu_cap / max(self.max_resource.get(self.cpu_key, 1.0), EPS), max(cpu_rem, 0.0) / max(cpu_cap, EPS)])
            feasible = cpu_rem + 1e-9 >= cpu_demand
            for key in self.resource_keys:
                if key == self.cpu_key:
                    continue
                demand = float(c["resources"].get(key, 0.0))
                if remain.get(key, 0.0) + 1e-9 < demand:
                    feasible = False
                    break
            if cpu_cap <= 0 and cpu_demand > 0:
                feasible = False
            lo_base = cpu_demand / max(cpu_cap, EPS)
            hi = max(0.0, cpu_rem) / max(cpu_cap, EPS)
            if lo_base > hi + 1e-9:
                feasible = False
            base_valid.append(bool(feasible))
            f_min_base.append(float(min(max(lo_base, 0.0), 1.0)) if feasible else 0.0)
            f_max.append(float(min(max(hi, 0.0), 1.0)) if feasible else 0.0)

            # Eq. (5)/(9) in the paper constrains response latency (compute +
            # requester-to-host transmission), not image-layer pull time. Pull
            # time remains part of this repository's deployment completion metric.
            tx_time = self._transmission_delay(c, ni)
            budget = self.args.tmax - tx_time
            min_cpu_possible = min(max(cpu_rem, 0.0), cpu_cap)
            min_compute_time = run_time * cpu_demand / max(min_cpu_possible, EPS) if cpu_demand > EPS else run_time
            can_meet_delay = feasible and (tx_time + min_compute_time <= self.args.tmax + 1e-9)
            if can_meet_delay:
                if run_time > EPS and cpu_demand > EPS and budget > EPS:
                    cpu_needed = run_time * cpu_demand / budget
                else:
                    cpu_needed = cpu_demand
                lo_delay = max(cpu_demand, cpu_needed) / max(cpu_cap, EPS)
                delay_valid.append(True)
                f_min_delay.append(float(min(max(lo_delay, 0.0), 1.0)))
            else:
                delay_valid.append(False)
                f_min_delay.append(0.0)

        # Strictly enforce the paper's response-delay constraint: if no edge
        # node can meet t_max, the request is forwarded to the cloud (the paper
        # models cloud service latency as t_max=3 s). Resource constraints are
        # never relaxed.
        valid = np.asarray(delay_valid, dtype=np.bool_)
        f_min = np.asarray(f_min_delay, dtype=np.float32)
        f_max_arr = np.asarray(f_max, dtype=np.float32)
        f_min[~valid] = 0.0
        f_max_arr[~valid] = 0.0
        return Snapshot(
            node_x=np.asarray(node_x, dtype=np.float32),
            layer_x=self._layer_vector(c),
            svc_x=self._service_vector(c),
            ra_x=np.asarray(ra_x, dtype=np.float32),
            f_min=f_min,
            f_max=f_max_arr,
            valid=valid,
        )

    def _transmission_delay(self, c: dict, host_idx: int) -> float:
        # Eq. (3)'s requester-to-host transmission term can only be calculated
        # when the trace identifies a request-origin node. Do not fabricate one.
        source = None
        for key in ("source_node", "requester_node", "origin_node", "request_node"):
            if c.get(key) is not None:
                source = str(c[key])
                break
        if source is None or source not in self.node_index:
            return 0.0
        src_idx = self.node_index[source]
        if src_idx == host_idx:
            return 0.0
        n = len(self.nodes)
        dist = [-1] * n
        parent = [-1] * n
        dist[src_idx] = 0
        queue = [src_idx]
        for u in queue:
            if u == host_idx:
                break
            for v in range(n):
                if self.raw_adj[u, v] > 0 and dist[v] < 0:
                    dist[v] = dist[u] + 1
                    parent[v] = u
                    queue.append(v)
        if dist[host_idx] < 0:
            return self.args.mu * float(n)
        path = [host_idx]
        cur = host_idx
        while cur != src_idx and cur >= 0:
            cur = parent[cur]
            if cur >= 0:
                path.append(cur)
        path_bw_mbps = min(self.nodes[i]["bandwidth_mb_s"] * 8.0 for i in path)
        d, rs, _, _ = self.service_values(c)
        return (d * rs / max(path_bw_mbps, EPS)) + self.args.mu * float(dist[host_idx])

    def _evict_to_capacity(self, nid: str) -> None:
        cache = self.caches[nid]
        cap = next(n["repo_capacity_mb"] for n in self.nodes if n["_id"] == nid)
        if cap <= 0:
            cache.clear()
            return
        def size() -> float:
            return sum(layer_mb(self.layer_sizes, x) for x in cache)
        # Deterministic layer-retention rule: preserve popular layers; when tied,
        # evict larger layers first to reduce the amount over capacity.
        while cache and size() > cap + 1e-9:
            victim = min(cache, key=lambda x: (self.layer_pop.get(x, 0), -layer_mb(self.layer_sizes, x), x))
            cache.remove(victim)

    def step(self, index: int, action: int, alloc_fraction: float) -> float:
        c = self.containers[index]
        d, rs, rc, p = self.service_values(c)
        cid = c["cid"]
        image_mb = self.image_size(c)
        if action < 0:
            # No feasible edge node: use the paper's cloud-delay assumption and
            # failure penalty. Full image size is charged as cloud-side transfer.
            completion = float(self.args.tmax)
            reward = -self.args.failure_penalty * d
            self.cloud_assignments.append(cid)
            self.cloud_count += 1
            self.cloud_downloaded_mb += image_mb
            self.all_finish_times.append(completion)
            self.container_metrics[cid] = {
                "cid": cid, "node": "cloud", "completion_time": completion,
                "downloaded_mb": image_mb, "reused_mb": 0.0, "download_time": image_mb / max(7.5, EPS),
                "compute_time": 0.0, "transmission_time": float(self.args.tmax),
                "allocated_cpu": 0.0, "f_cpu": 0.0, "reward": reward,
                "delay_violation": False,
            }
            self.total_reward += reward
            return reward

        n = self.nodes[action]
        nid = n["_id"]
        cache = self.caches[nid]
        layers = c["layers"]
        reused = sum(layer_mb(self.layer_sizes, x) for x in layers if x in cache)
        missing = sum(layer_mb(self.layer_sizes, x) for x in layers if x not in cache)
        bw = max(float(n["bandwidth_mb_s"]), EPS)
        pull_time = missing / bw
        req_cpu = float(c["resources"].get(self.cpu_key, 0.0))
        cpu_cap = max(float(n["resources"].get(self.cpu_key, 0.0)), EPS)
        allocated_cpu = max(req_cpu, float(alloc_fraction) * cpu_cap)
        allocated_cpu = min(allocated_cpu, max(req_cpu, cpu_cap - self.used[nid].get(self.cpu_key, 0.0)))
        # run_time is the trace's execution-time proxy. More allocated CPU reduces
        # its compute component proportionally; fixed resource requests remain hard minima.
        compute_time = float(c.get("run_time", 0.0))
        if req_cpu > EPS and allocated_cpu > EPS:
            compute_time *= req_cpu / allocated_cpu
        tx_time = self._transmission_delay(c, action)
        # Paper reward/constraint latency is response latency (compute + request
        # transmission). The project deployment-completion metric also includes
        # image pull time, as in the existing FG-DSCR baselines.
        service_latency = compute_time + tx_time
        completion = self.node_clock[nid] + pull_time + compute_time
        self.node_clock[nid] = completion
        self.assignment[nid].append(cid)
        self.ordered_queues[nid].append(cid)
        self.edge_finish_times.append(completion)
        self.all_finish_times.append(completion)
        self.total_downloaded += missing
        self.total_reused += reused
        self.used[nid][self.cpu_key] = self.used[nid].get(self.cpu_key, 0.0) + allocated_cpu
        for key in self.resource_keys:
            if key == self.cpu_key:
                continue
            self.used[nid][key] = self.used[nid].get(key, 0.0) + float(c["resources"].get(key, 0.0))
        # Storage cost is converted from MB to GB, consistent with the paper's
        # node storage capacity M being specified in GB.
        storage_gb = missing / 1024.0
        reward = p * d - self.args.alpha_res * self.args.lambda_latency * service_latency - self.args.alpha_sto * (1.0 - self.args.lambda_latency) * storage_gb
        delay_violation = service_latency > self.args.tmax
        self.allocations[cid] = {
            "node": nid, "f_cpu": float(allocated_cpu / cpu_cap),
            "allocated_cpu": float(allocated_cpu), "requested_cpu": req_cpu,
        }
        self.container_metrics[cid] = {
            "cid": cid, "node": nid, "completion_time": completion,
            "downloaded_mb": missing, "reused_mb": reused, "download_time": pull_time,
            "compute_time": compute_time, "transmission_time": tx_time,
            "service_latency": service_latency, "allocated_cpu": allocated_cpu,
            "f_cpu": float(allocated_cpu / cpu_cap), "reward": reward,
            "delay_violation": bool(delay_violation),
        }
        # Update local layer state after reward computation.
        for layer in layers:
            cache.add(layer)
        self._evict_to_capacity(nid)
        self.total_reward += reward
        return reward

    def result(self, alpha_obj: float, episodes: int, train_rewards: List[float], training_seconds: float, update_count: int, args: argparse.Namespace) -> dict:
        # The GAHRL paper uses 3 s as its synthetic cloud service latency. For
        # cross-method comparison in this repository, use the existing unified
        # audit convention: cloud requests receive provision_sec plus an
        # interpolated edge-like completion time. Keep both metrics in output.
        paper_times = [float(self.container_metrics[c["cid"]]["completion_time"]) for c in self.containers]
        paper_act = float(np.mean(paper_times)) if paper_times else 0.0
        paper_p95 = float(np.percentile(paper_times, 95)) if paper_times else 0.0
        edge_ids = {cid for cids in self.assignment.values() for cid in cids}
        edge_times = sorted(float(self.container_metrics[cid]["completion_time"]) for cid in edge_ids)
        cloud_ids = [c["cid"] for c in self.containers if c["cid"] not in edge_ids]
        unified_times = []
        cloud_seen = 0
        for c in self.containers:
            cid = c["cid"]
            if cid in edge_ids:
                t = float(self.container_metrics[cid]["completion_time"])
            else:
                if edge_times:
                    q = (cloud_seen + 0.5) / max(len(cloud_ids), 1)
                    pos = q * (len(edge_times) - 1)
                    lo = int(math.floor(pos)); hi = min(lo + 1, len(edge_times) - 1)
                    w = pos - lo
                    edge_like = edge_times[lo] * (1.0 - w) + edge_times[hi] * w
                else:
                    edge_like = float(args.tmax)
                t = float(args.provision_sec) + edge_like
                cloud_seen += 1
            self.container_metrics[cid]["unified_completion_time"] = float(t)
            unified_times.append(float(t))
        act = float(np.mean(unified_times)) if unified_times else 0.0
        p95 = float(np.percentile(unified_times, 95)) if unified_times else 0.0
        ams = float(max(unified_times)) if unified_times else 0.0
        mean_node_makespan = float(np.mean(list(self.node_clock.values()))) if self.node_clock else 0.0
        objective = alpha_obj * act + (1.0 - alpha_obj) * ams
        denom = self.total_downloaded + self.total_reused
        reuse_rate = self.total_reused / denom if denom > 0 else 0.0
        # Unified fragmentation metric follows run_formal_parameter_audit.py:
        # it uses the original container resource demands, not GAHRL's extra
        # CPU allocation, so it remains comparable to non-RL schedulers.
        by_id = {c["cid"]: c for c in self.containers}
        frag_values = []
        frag_actual_values = []
        for n in self.nodes:
            nid = n["_id"]
            cids = self.assignment[nid]
            if not cids:
                continue
            requested_ratios = []
            actual_ratios = []
            for key in self.resource_keys:
                cap = float(n["resources"].get(key, 0.0))
                if cap <= 0:
                    continue
                requested_used = sum(float(by_id[cid]["resources"].get(key, 0.0)) for cid in cids)
                requested_ratios.append(requested_used / cap)
                actual_ratios.append(float(self.used[nid].get(key, 0.0)) / cap)
            if requested_ratios:
                m = float(np.mean(requested_ratios))
                frag_values.append(float(np.mean([abs(x - m) for x in requested_ratios])))
            if actual_ratios:
                m_actual = float(np.mean(actual_ratios))
                frag_actual_values.append(float(np.mean([abs(x - m_actual) for x in actual_ratios])))
        frag = float(np.mean(frag_values)) if frag_values else 0.0
        frag_actual = float(np.mean(frag_actual_values)) if frag_actual_values else 0.0
        ca = self.cloud_count / max(len(self.containers), 1)
        delay_raw = 0.5 * (act + p95)
        d_norm = delay_raw / REF_D
        ca_norm = ca / REF_CA
        frag_norm = frag / REF_FRAG
        j_value = d_norm + ca_norm + frag_norm
        train_first = float(np.mean(train_rewards[:min(100, len(train_rewards))])) if train_rewards else None
        train_last = float(np.mean(train_rewards[-min(100, len(train_rewards)):])) if train_rewards else None
        node_details = {}
        for n in self.nodes:
            nid = n["_id"]
            node_details[nid] = {
                "num_containers": len(self.assignment[nid]),
                "finish_time": float(self.node_clock[nid]),
                "downloaded_mb": float(sum(self.container_metrics[cid]["downloaded_mb"] for cid in self.assignment[nid])),
                "reused_mb": float(sum(self.container_metrics[cid]["reused_mb"] for cid in self.assignment[nid])),
                "final_cache_mb": float(sum(layer_mb(self.layer_sizes, x) for x in self.caches[nid])),
                "final_cache_layers": len(self.caches[nid]),
                "used_resources": {k: float(v) for k, v in self.used[nid].items()},
            }
        return {
            "algorithm": "GAHRL-paper-style-adaptation",
            "paper_reference": {
                "paper": "Zeng et al., Computer Networks 267 (2025) 111343",
                "episodes_used_in_paper_reward_curve": 1500,
                "paper_reported_convergence": "approximately 400 episodes (paper text, Section 5.2)",
                "reported_parameters": {
                    "lambda": 0.5, "alpha_res": 20, "alpha_sto": 16, "mu_seconds": 0.08,
                    "t_max_seconds": 3, "nodes_default": 10, "services_default": 30,
                    "layer_size_mb_range": [30, 600], "download_speed_mbps_range": [30, 90],
                    "request_size_mbits_range": [0.5, 1.0], "compute_complexity_ghz_range": [0.002, 0.005],
                    "unit_reward_range": [0.5, 0.6],
                },
            },
            "adaptation_notes": {
                "case_schema": "FG-DSCR case JSON: layer_sizes_mb, containers, nodes",
                "case_node_count_used": len(self.nodes),
                "case_container_count_used": len(self.containers),
                "graph_source": self.graph_source,
                "bandwidth_units": "case bandwidth_mb_s interpreted as MB/s; paper Mbps converted only for defaults",
                "missing_service_fields": "request_count=1, request_size_mbits=0.75, compute_complexity_ghz=0.0035, unit_reward=0.55 unless case overrides them",
                "request_origin": "requester-to-host transmission delay is included only if a container has source_node/requester_node/origin_node/request_node; otherwise it is omitted rather than fabricated",
                "cpu_allocation": "continuous actor allocates CPU between requested CPU and current remaining capacity; memory/disk remain hard constraints",
                "storage_units": "missing layer MB converted to GB in reward because paper node storage capacity is specified in GB",
                "delay_constraint": "paper response-delay constraint is compute plus request-transmission latency (not image-pull time); if no edge node can meet t_max, request is forwarded to cloud",
                "cloud_metric": "GAHRL reward environment uses paper's 3 s cloud assumption; unified J uses provision_sec plus interpolated edge-like completion time, matching this repository's formal audit convention",
                "cache_semantics": "repo_capacity_mb follows FG-DSCR extra reusable-cache semantics, not the paper's total node-storage M_n; pulled layers may be evicted after the current deployment",
                "not_exact_reproduction": "paper does not disclose several neural-network/training hyperparameters; defaults below are implementation choices",
            },
            "training": {
                "episodes": episodes,
                "environment_steps": len(self.containers) * episodes,
                "gradient_updates": update_count,
                "training_seconds": training_seconds,
                "mean_reward_first_100_episodes": train_first,
                "mean_reward_last_100_episodes": train_last,
                "reward_curve_csv": args.out_prefix + "_episodes.csv",
            },
            "config": vars(args),
            "summary": {
                "algo": "GAHRL-paper-style-training",
                "num_containers": len(self.containers), "num_nodes": len(self.nodes),
                "episodes": episodes, "mean_training_reward_last_100": train_last,
                "ACT": act, "AMS": ams, "mean_node_makespan": mean_node_makespan,
                "objective": objective, "all_request_mean": act, "P95_all": p95,
                "Delay_raw": delay_raw, "CA_rate": ca, "frag_score": frag,
                "frag_score_actual_allocation": frag_actual,
                "gahrl_environment_completion_mean": paper_act, "gahrl_environment_completion_P95": paper_p95,
                "D_norm": d_norm, "CA_norm": ca_norm, "Frag_norm": frag_norm, "J": j_value,
                "downloaded_mb": float(self.total_downloaded), "reused_mb": float(self.total_reused),
                "reuse_rate": float(reuse_rate), "cloud_requests": self.cloud_count,
                "cloud_full_image_mb": float(self.cloud_downloaded_mb),
                "delay_violations": sum(1 for x in self.container_metrics.values() if x.get("delay_violation", False)),
                "alpha_obj": alpha_obj,
            },
            "assignment": self.assignment,
            "ordered_queues": self.ordered_queues,
            "cloud_assignments": self.cloud_assignments,
            "allocations": self.allocations,
            "node_details": node_details,
            "container_metrics": [self.container_metrics[c["cid"]] for c in self.containers],
            "container_metrics_by_id": self.container_metrics,
        }


class GCN(nn.Module):
    def __init__(self, node_dim: int, hidden: int):
        super().__init__()
        self.fc1 = nn.Linear(node_dim, hidden)
        self.fc2 = nn.Linear(hidden, hidden)

    def forward(self, x: torch.Tensor, adj: torch.Tensor) -> torch.Tensor:
        # x [B,N,F], adj [N,N]
        h = torch.relu(torch.matmul(adj, self.fc1(x)))
        h = torch.relu(torch.matmul(adj, self.fc2(h)))
        return h


class FactorizationMachine(nn.Module):
    def __init__(self, dim: int, embed_dim: int):
        super().__init__()
        self.linear = nn.Linear(dim, 1)
        self.v = nn.Parameter(torch.randn(dim, embed_dim) * 0.01)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # y = w.x + 1/2 sum_f[(sum_i v_if x_i)^2 - sum_i(v_if x_i)^2]
        linear = self.linear(x)
        xv = x.unsqueeze(-1) * self.v.unsqueeze(0)
        summed = xv.sum(dim=1)
        interaction = 0.5 * (summed.pow(2) - xv.pow(2).sum(dim=1)).sum(dim=1, keepdim=True)
        return linear + interaction


class DuelingPlacementQ(nn.Module):
    def __init__(self, node_dim: int, service_dim: int, layer_dim: int, num_nodes: int, hidden: int, fm_dim: int, adj_norm: np.ndarray):
        super().__init__()
        self.num_nodes = num_nodes
        self.gcn = GCN(node_dim, hidden)
        self.fm = FactorizationMachine(layer_dim, fm_dim)
        global_dim = hidden * 2 + service_dim + 1 + num_nodes
        self.global_fc = nn.Sequential(nn.Linear(global_dim, hidden), nn.ReLU(), nn.Linear(hidden, hidden), nn.ReLU())
        self.value_head = nn.Sequential(nn.Linear(hidden, hidden // 2), nn.ReLU(), nn.Linear(hidden // 2, 1))
        self.adv_head = nn.Sequential(nn.Linear(hidden * 2, hidden), nn.ReLU(), nn.Linear(hidden, hidden // 2), nn.ReLU(), nn.Linear(hidden // 2, 1))
        self.register_buffer("adj_norm", torch.tensor(adj_norm, dtype=torch.float32))

    def forward(self, node_x: torch.Tensor, layer_x: torch.Tensor, svc_x: torch.Tensor, alloc_f: torch.Tensor) -> torch.Tensor:
        if node_x.dim() == 2:
            node_x = node_x.unsqueeze(0)
            layer_x = layer_x.unsqueeze(0)
            svc_x = svc_x.unsqueeze(0)
            alloc_f = alloc_f.unsqueeze(0)
        b = node_x.shape[0]
        node_emb = self.gcn(node_x, self.adj_norm)
        mean_emb = node_emb.mean(dim=1)
        max_emb = node_emb.max(dim=1).values
        fm_value = self.fm(layer_x)
        global_raw = torch.cat([mean_emb, max_emb, svc_x, fm_value, alloc_f], dim=1)
        global_emb = self.global_fc(global_raw)
        global_rep = global_emb.unsqueeze(1).expand(-1, self.num_nodes, -1)
        adv_input = torch.cat([node_emb, global_rep], dim=-1)
        advantage = self.adv_head(adv_input).squeeze(-1)
        value = self.value_head(global_emb)
        q = value + advantage - advantage.mean(dim=1, keepdim=True)
        return q


class ResourceActor(nn.Module):
    def __init__(self, num_nodes: int, ra_dim: int, service_dim: int, hidden: int):
        super().__init__()
        self.num_nodes = num_nodes
        self.net = nn.Sequential(
            nn.Linear(num_nodes * ra_dim + service_dim, hidden), nn.ReLU(),
            nn.Linear(hidden, hidden), nn.ReLU(), nn.Linear(hidden, num_nodes), nn.Sigmoid(),
        )

    def forward(self, ra_x: torch.Tensor, svc_x: torch.Tensor) -> torch.Tensor:
        if ra_x.dim() == 2:
            ra_x = ra_x.unsqueeze(0)
            svc_x = svc_x.unsqueeze(0)
        return self.net(torch.cat([ra_x.flatten(start_dim=1), svc_x], dim=1))


class ReplayBuffer:
    def __init__(self, capacity: int):
        self.data = deque(maxlen=capacity)

    def __len__(self) -> int:
        return len(self.data)

    def add(self, s: Snapshot, action: int, alloc_behavior: np.ndarray, reward: float, ns: Snapshot, done: bool) -> None:
        self.data.append((
            s.node_x.copy(), s.layer_x.copy(), s.svc_x.copy(), s.ra_x.copy(), s.f_min.copy(), s.f_max.copy(), s.valid.copy(),
            int(action), alloc_behavior.copy(), float(reward),
            ns.node_x.copy(), ns.layer_x.copy(), ns.svc_x.copy(), ns.ra_x.copy(), ns.f_min.copy(), ns.f_max.copy(), ns.valid.copy(), bool(done),
        ))

    def sample(self, batch_size: int) -> tuple:
        batch = random.sample(self.data, batch_size)
        cols = list(zip(*batch))
        arrays = []
        for i, col in enumerate(cols):
            if i == 7:
                arrays.append(np.asarray(col, dtype=np.int64))
            elif i in (6, 16):
                arrays.append(np.asarray(col, dtype=np.float32))
            else:
                arrays.append(np.asarray(col, dtype=np.float32))
        return arrays


def tensor(x: np.ndarray, device: torch.device, dtype=torch.float32) -> torch.Tensor:
    return torch.as_tensor(x, dtype=dtype, device=device)


def allocation_from_actor(actor: ResourceActor, s: Snapshot, device: torch.device, noise_std: float = 0.0) -> Tuple[np.ndarray, np.ndarray]:
    with torch.no_grad():
        ra = tensor(s.ra_x, device).unsqueeze(0)
        svc = tensor(s.svc_x, device).unsqueeze(0)
        z = actor(ra, svc).squeeze(0).cpu().numpy()
    if noise_std > 0:
        z = np.clip(z + np.random.normal(0.0, noise_std, size=z.shape).astype(np.float32), 0.0, 1.0)
    f = s.f_min + z * (s.f_max - s.f_min)
    f[~s.valid] = 0.0
    return z.astype(np.float32), f.astype(np.float32)


def update_models(actor: ResourceActor, qnet: DuelingPlacementQ, target_q: DuelingPlacementQ,
                  actor_opt: torch.optim.Optimizer, q_opt: torch.optim.Optimizer,
                  replay: ReplayBuffer, args: argparse.Namespace, device: torch.device) -> Tuple[float, float]:
    b = replay.sample(args.batch_size)
    (node_x, layer_x, svc_x, ra_x, f_min, f_max, valid, actions, alloc_behavior, rewards,
     next_node_x, next_layer_x, next_svc_x, next_ra_x, next_f_min, next_f_max, next_valid, dones) = b
    node_x_t = tensor(node_x, device); layer_x_t = tensor(layer_x, device); svc_x_t = tensor(svc_x, device)
    ra_x_t = tensor(ra_x, device); fmin_t = tensor(f_min, device); fmax_t = tensor(f_max, device)
    valid_t = tensor(valid, device); actions_t = tensor(actions, device, torch.long)
    alloc_behavior_t = tensor(alloc_behavior, device)
    rewards_t = tensor(rewards, device).unsqueeze(1); dones_t = tensor(dones, device).unsqueeze(1)
    next_node_t = tensor(next_node_x, device); next_layer_t = tensor(next_layer_x, device); next_svc_t = tensor(next_svc_x, device)
    next_ra_t = tensor(next_ra_x, device); next_fmin_t = tensor(next_f_min, device); next_fmax_t = tensor(next_f_max, device)
    next_valid_t = tensor(next_valid, device)

    # Dueling DQN Bellman update. Actor provides the continuous action vector.
    q_all = qnet(node_x_t, layer_x_t, svc_x_t, alloc_behavior_t)
    q_sa = q_all.gather(1, actions_t.unsqueeze(1))
    with torch.no_grad():
        next_z = actor(next_ra_t, next_svc_t)
        next_alloc = (next_fmin_t + next_z * (next_fmax_t - next_fmin_t)) * next_valid_t
        next_q = target_q(next_node_t, next_layer_t, next_svc_t, next_alloc)
        next_q = next_q.masked_fill(next_valid_t <= 0.0, -1e9)
        no_next_action = (next_valid_t.sum(dim=1, keepdim=True) <= 0.0)
        next_max = next_q.max(dim=1, keepdim=True).values
        next_max = torch.where(no_next_action, torch.zeros_like(next_max), next_max)
        target = rewards_t + args.gamma * (1.0 - dones_t) * next_max
    critic_loss = torch.mean((q_sa - target) ** 2)
    q_opt.zero_grad(set_to_none=True)
    critic_loss.backward()
    torch.nn.utils.clip_grad_norm_(qnet.parameters(), 5.0)
    q_opt.step()

    # DDPG-style policy gradient: maximize the mean valid placement Q value.
    old_flags = [p.requires_grad for p in qnet.parameters()]
    for p in qnet.parameters():
        p.requires_grad_(False)
    z_actor = actor(ra_x_t, svc_x_t)
    alloc_actor = (fmin_t + z_actor * (fmax_t - fmin_t)) * valid_t
    q_actor = qnet(node_x_t, layer_x_t, svc_x_t, alloc_actor)
    denom = valid_t.sum().clamp(min=1.0)
    actor_loss = -((q_actor * valid_t).sum() / denom)
    actor_opt.zero_grad(set_to_none=True)
    actor_loss.backward()
    torch.nn.utils.clip_grad_norm_(actor.parameters(), 5.0)
    actor_opt.step()
    for p, flag in zip(qnet.parameters(), old_flags):
        p.requires_grad_(flag)

    with torch.no_grad():
        for target_p, source_p in zip(target_q.parameters(), qnet.parameters()):
            target_p.mul_(1.0 - args.tau).add_(source_p, alpha=args.tau)
    return float(critic_loss.item()), float(actor_loss.item())


def choose_action(s: Snapshot, actor: ResourceActor, qnet: DuelingPlacementQ, args: argparse.Namespace,
                  device: torch.device, epsilon: float, training: bool) -> Tuple[int, float, np.ndarray]:
    valid_ids = np.flatnonzero(s.valid)
    if len(valid_ids) == 0:
        return -1, 0.0, np.zeros_like(s.f_min, dtype=np.float32)
    _, alloc_f = allocation_from_actor(actor, s, device, args.actor_noise if training else 0.0)
    with torch.no_grad():
        q = qnet(tensor(s.node_x, device), tensor(s.layer_x, device), tensor(s.svc_x, device), tensor(alloc_f, device)).squeeze(0).cpu().numpy()
    q[~s.valid] = -1e9
    if training and random.random() < epsilon:
        action = int(random.choice(valid_ids.tolist()))
    else:
        action = int(np.argmax(q))
    return action, float(alloc_f[action]), alloc_f


def write_csv(path: str, rows: List[dict], fieldnames: Sequence[str]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(fieldnames))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="Train a paper-style GAHRL baseline on an FG-DSCR case.")
    ap.add_argument("--case", required=True, help="FG-DSCR case JSON")
    ap.add_argument("--out", required=True, help="Output evaluation JSON")
    ap.add_argument("--episodes", type=int, default=1500, help="Paper Fig. 3 uses 1500 episodes")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    ap.add_argument("--torch-threads", type=int, default=1)
    # Paper-reported objective/reward parameters.
    ap.add_argument("--lambda-latency", type=float, default=0.5)
    ap.add_argument("--alpha-res", type=float, default=20.0)
    ap.add_argument("--alpha-sto", type=float, default=16.0)
    ap.add_argument("--mu", type=float, default=0.08)
    ap.add_argument("--tmax", type=float, default=3.0)
    ap.add_argument("--alpha-obj", type=float, default=0.5)
    ap.add_argument("--provision-sec", type=float, default=300.0, help="Current repository's unified cloud provisioning proxy for J; GAHRL paper's own cloud delay remains tmax=3 s")
    # Fields missing from our case schema: paper-range midpoint defaults.
    ap.add_argument("--request-size-mbits", type=float, default=0.75)
    ap.add_argument("--compute-complexity-ghz", type=float, default=0.0035)
    ap.add_argument("--unit-reward", type=float, default=0.55)
    ap.add_argument("--failure-penalty", type=float, default=1.0, help="Paper does not give p_c; explicit implementation default")
    # Not reported in the paper: explicit implementation defaults.
    ap.add_argument("--gamma", type=float, default=0.99)
    ap.add_argument("--tau", type=float, default=0.005)
    ap.add_argument("--lr-actor", type=float, default=1e-3)
    ap.add_argument("--lr-critic", type=float, default=1e-3)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--replay-capacity", type=int, default=50000)
    ap.add_argument("--replay-start-size", type=int, default=512)
    ap.add_argument("--update-every", type=int, default=4, help="Practical adaptation; paper updates once buffer threshold is met")
    ap.add_argument("--hidden-dim", type=int, default=128)
    ap.add_argument("--fm-dim", type=int, default=16)
    ap.add_argument("--epsilon-start", type=float, default=1.0)
    ap.add_argument("--epsilon-end", type=float, default=0.05)
    ap.add_argument("--epsilon-decay-episodes", type=int, default=1000)
    ap.add_argument("--actor-noise", type=float, default=0.10)
    args = ap.parse_args()
    args.out_prefix = os.path.splitext(args.out)[0]

    if args.episodes < 1 or args.batch_size < 1 or args.replay_capacity < 1 or args.update_every < 1:
        raise ValueError("episodes, batch-size, replay-capacity, and update-every must be positive")
    if args.device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("--device cuda was requested, but CUDA is unavailable; use --device cpu")
    random.seed(args.seed); np.random.seed(args.seed); torch.manual_seed(args.seed)
    torch.set_num_threads(max(1, args.torch_threads))
    device = torch.device(args.device)
    case = load_case(args.case)
    env = GAHRLEnvironment(case, args)
    n_nodes = len(env.nodes)
    node_dim = env.snapshot(0).node_x.shape[1]
    service_dim = env.snapshot(0).svc_x.shape[0]
    layer_dim = env.snapshot(0).layer_x.shape[0]

    actor = ResourceActor(n_nodes, ra_dim=2, service_dim=service_dim, hidden=args.hidden_dim).to(device)
    qnet = DuelingPlacementQ(node_dim, service_dim, layer_dim, n_nodes, args.hidden_dim, args.fm_dim, env.adj_norm).to(device)
    target_q = copy.deepcopy(qnet).to(device)
    for p in target_q.parameters():
        p.requires_grad_(False)
    actor_opt = torch.optim.Adam(actor.parameters(), lr=args.lr_actor)
    q_opt = torch.optim.Adam(qnet.parameters(), lr=args.lr_critic)
    replay = ReplayBuffer(args.replay_capacity)

    episode_rows = []
    train_rewards = []
    update_count = 0
    total_env_steps = 0
    losses_q = []
    losses_actor = []
    t_train = time.perf_counter()
    for ep in range(1, args.episodes + 1):
        env.reset()
        epsilon_progress = min(1.0, (ep - 1) / max(args.epsilon_decay_episodes, 1))
        epsilon = args.epsilon_start + epsilon_progress * (args.epsilon_end - args.epsilon_start)
        ep_reward = 0.0
        for i, c in enumerate(env.containers):
            s = env.snapshot(i)
            action, alloc, alloc_vector = choose_action(s, actor, qnet, args, device, epsilon, training=True)
            reward = env.step(i, action, alloc)
            ep_reward += reward
            total_env_steps += 1
            done = i == len(env.containers) - 1
            if not done:
                ns = env.snapshot(i + 1)
            else:
                ns = env.snapshot(i)
            if action >= 0:
                replay.add(s, action, alloc_vector, reward, ns, done)
            if len(replay) >= max(args.replay_start_size, args.batch_size) and total_env_steps % args.update_every == 0:
                qloss, aloss = update_models(actor, qnet, target_q, actor_opt, q_opt, replay, args, device)
                update_count += 1
                losses_q.append(qloss); losses_actor.append(aloss)
        train_rewards.append(float(ep_reward))
        episode_rows.append({
            "episode": ep, "episode_reward": float(ep_reward), "epsilon": float(epsilon),
            "replay_size": len(replay), "gradient_updates": update_count,
            "critic_loss_last": losses_q[-1] if losses_q else "",
            "actor_loss_last": losses_actor[-1] if losses_actor else "",
        })
        if ep == 1 or ep % 50 == 0 or ep == args.episodes:
            print(f"[GAHRL] episode={ep}/{args.episodes} reward={ep_reward:.4f} epsilon={epsilon:.3f} replay={len(replay)} updates={update_count}", flush=True)
    training_seconds = time.perf_counter() - t_train

    # Deterministic evaluation episode with the learned policy; no exploration noise.
    env.reset()
    eval_reward = 0.0
    for i, c in enumerate(env.containers):
        s = env.snapshot(i)
        action, alloc, _ = choose_action(s, actor, qnet, args, device, epsilon=0.0, training=False)
        eval_reward += env.step(i, action, alloc)
    result = env.result(args.alpha_obj, args.episodes, train_rewards, training_seconds, update_count, args)
    result["evaluation"] = {"policy_reward": float(eval_reward), "exploration": False, "case": os.path.abspath(args.case)}
    result["model_artifacts"] = {
        "checkpoint": args.out_prefix + ".pt",
        "episode_csv": args.out_prefix + "_episodes.csv",
    }
    # Save training artifacts alongside the requested JSON.
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    write_csv(args.out_prefix + "_episodes.csv", episode_rows,
              ["episode", "episode_reward", "epsilon", "replay_size", "gradient_updates", "critic_loss_last", "actor_loss_last"])
    torch.save({
        "actor_state_dict": actor.state_dict(),
        "q_state_dict": qnet.state_dict(),
        "target_q_state_dict": target_q.state_dict(),
        "config": vars(args),
        "layer_ids": env.layer_ids,
        "node_ids": env.node_ids,
        "adjacency_normalized": env.adj_norm.tolist(),
    }, args.out_prefix + ".pt")
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(json.dumps(result["summary"], indent=2, ensure_ascii=False))
    print(f"[SAVED] {args.out}")
    print(f"[SAVED] {args.out_prefix}_episodes.csv")
    print(f"[SAVED] {args.out_prefix}.pt")


if __name__ == "__main__":
    main()
