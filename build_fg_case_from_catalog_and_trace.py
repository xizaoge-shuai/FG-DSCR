#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
import argparse
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Tuple


def load_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_weights(items: List[Tuple[str, float]]) -> List[Tuple[str, float]]:
    s = sum(w for _, w in items)
    if s <= 0:
        n = len(items)
        return [(k, 1.0 / n) for k, _ in items]
    return [(k, w / s) for k, w in items]


def weighted_choice(items: List[Tuple[str, float]], rng: random.Random) -> str:
    r = rng.random()
    acc = 0.0
    for k, w in items:
        acc += w
        if r <= acc:
            return k
    return items[-1][0]


def image_category(name: str) -> str:
    base = name.split("/")[-1].split(":")[0]
    if base in {"mysql", "mariadb", "redis", "postgres", "cassandra", "memcached", "rabbitmq"}:
        return "data"
    if base in {"python", "golang", "openjdk", "node"}:
        return "runtime"
    if base in {"ubuntu"}:
        return "os"
    if base in {"wordpress", "ghost", "httpd", "registry"}:
        return "service"
    if base in {"flink"}:
        return "analytics"
    return "default"


def sample_resources(cat: str, rng: random.Random) -> Tuple[Dict[str, float], float]:
    """
    返回 (resources, run_time)
    """
    if cat == "data":
        cpu = rng.choice([2, 3, 4])
        mem = rng.choice([4, 6, 8, 10])
        disk = rng.choice([8, 10, 12, 16])
        run_time = rng.choice([20, 25, 30, 35, 40])
    elif cat == "runtime":
        cpu = rng.choice([1, 2])
        mem = rng.choice([2, 3, 4])
        disk = rng.choice([2, 3, 4, 5])
        run_time = rng.choice([10, 12, 15, 18])
    elif cat == "os":
        cpu = rng.choice([1, 2])
        mem = rng.choice([1, 2, 3])
        disk = rng.choice([2, 3, 4])
        run_time = rng.choice([8, 10, 12])
    elif cat == "service":
        cpu = rng.choice([1, 2, 3])
        mem = rng.choice([2, 3, 4, 5])
        disk = rng.choice([3, 4, 5, 6])
        run_time = rng.choice([12, 15, 18, 22])
    elif cat == "analytics":
        cpu = rng.choice([3, 4, 5])
        mem = rng.choice([6, 8, 10])
        disk = rng.choice([6, 8, 10, 12])
        run_time = rng.choice([25, 30, 35, 40])
    else:
        cpu = rng.choice([1, 2, 3])
        mem = rng.choice([2, 3, 4, 5])
        disk = rng.choice([3, 4, 5, 6])
        run_time = rng.choice([12, 16, 20])
    return {"cpu": cpu, "mem": mem, "disk": disk}, float(run_time)


def greedy_hot_cache(layer_freq: Dict[str, float], layer_sizes_mb: Dict[str, int], cap_mb: int) -> List[str]:
    items = []
    for lid, f in layer_freq.items():
        sz = layer_sizes_mb.get(lid, 0)
        # 按 热度/大小 排序
        score = f / max(sz, 1)
        items.append((score, lid, sz))
    items.sort(reverse=True)

    picked = []
    used = 0
    for _, lid, sz in items:
        if used + sz <= cap_mb:
            picked.append(lid)
            used += sz
    return picked


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", required=True, help="脚本1输出的 image_catalog.json")
    parser.add_argument("--trace-profile", required=True, help="脚本2输出的 drtp_profile.json")
    parser.add_argument("--sizes", nargs="+", type=int, default=[200, 500, 1000])
    parser.add_argument("--num-nodes", type=int, default=8)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out-prefix", default="paper_case")
    args = parser.parse_args()

    rng = random.Random(args.seed)

    catalog = load_json(args.catalog)
    profile = load_json(args.trace_profile)

    if not catalog:
        raise SystemExit("catalog 为空")

    # 组织 image -> layer list / size
    image_items = []
    layer_sizes_mb = {}
    image_to_layers = {}
    image_to_size = {}

    for item in catalog:
        image_ref = item["image_ref"]
        layers = []
        total_mb = 0
        for x in item["layers"]:
            lid = x["digest"]
            sz = int(round(float(x["size_mb"])))
            if sz <= 0:
                sz = max(1, int(math.ceil(x["size_bytes"] / 1024 / 1024)))
            layer_sizes_mb[lid] = sz
            layers.append(lid)
            total_mb += sz
        image_to_layers[image_ref] = layers
        image_to_size[image_ref] = total_mb
        image_items.append(image_ref)

    # 用 DRTP 的 repo 热度向量，映射到真实镜像集合
    repo_weights = profile.get("repo_weights", [])
    raw_weights = [x.get("weight", 0.0) for x in repo_weights]
    if len(raw_weights) == 0:
        raw_weights = [1.0 for _ in image_items]

    # 把 trace 热度按 rank 映射到 image rank
    # 为了让热门镜像更可能被采样，这里按镜像总大小降序排序，也可以自己改成固定顺序
    sorted_images = sorted(image_items, key=lambda x: image_to_size[x], reverse=True)
    weights = []
    for i, img in enumerate(sorted_images):
        w = raw_weights[i] if i < len(raw_weights) else raw_weights[-1]
        weights.append((img, float(w)))
    weights = normalize_weights(weights)

    for n_containers in args.sizes:
        containers = []
        layer_freq = Counter()

        for i in range(1, n_containers + 1):
            img = weighted_choice(weights, rng)
            cat = image_category(img)
            resources, run_time = sample_resources(cat, rng)

            layers = image_to_layers[img]
            for lid in layers:
                layer_freq[lid] += 1

            containers.append({
                "cid": f"c{i:03d}",
                "image_ref": img,
                "service_type": img.split("/")[-1].split(":")[0],
                "layers": layers,
                "resources": resources,
                "run_time": run_time,
            })

        # 节点配置
        nodes = []
        # 每个节点缓存容量给大一些，保证单镜像可放
        repo_capacity_mb = max(max(image_to_size.values()) * 2, 12000)

        hot_cache = greedy_hot_cache(dict(layer_freq), layer_sizes_mb, int(repo_capacity_mb * 0.15))

        for j in range(1, args.num_nodes + 1):
            nodes.append({
                "eid": f"edge-{j}",
                "resources": {
                    "cpu": 24,
                    "mem": 64,
                    "disk": 128,
                },
                "repo_capacity_mb": int(repo_capacity_mb),
                "bandwidth_mb_s": float(80 if j % 2 == 0 else 60),
                "initial_cache": hot_cache[:],
            })

        out = {
            "layer_sizes_mb": layer_sizes_mb,
            "containers": [
                {
                    "cid": x["cid"],
                    "layers": x["layers"],
                    "resources": x["resources"],
                    "run_time": x["run_time"],
                    "service_type": x["service_type"],
                }
                for x in containers
            ],
            "nodes": nodes,
            "meta": {
                "num_containers": n_containers,
                "num_nodes": args.num_nodes,
                "seed": args.seed,
                "catalog_size": len(catalog),
                "note": "Generated from Docker Hub image layers + DRTP popularity profile",
            },
        }

        out_path = f"{args.out_prefix}_{n_containers}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)

        print(f"[OK] wrote {out_path}")


if __name__ == "__main__":
    main()