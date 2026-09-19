#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""门禁结果快速画图。仅用于第一阶段判断方向，不是最终论文绘图脚本。"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv", required=True)
    p.add_argument("--out-dir", default="coded_delivery_gate_figs")
    args = p.parse_args()

    df = pd.read_csv(args.csv)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # 图 1：平均通信量（Unicast / MCAST / strict-XOR2）
    means = df[["unicast_mb", "mcast_mb", "xor2_mb"]].mean()
    fig, ax = plt.subplots(figsize=(6.5, 4.2))
    ax.bar(["Unicast", "MCAST", "Strict-XOR2"], means.values)
    ax.set_ylabel("Backhaul Traffic (MB)")
    ax.set_title("Stage-1 Communication Gate")
    fig.tight_layout()
    fig.savefig(out / "gate_traffic_bar.pdf")
    fig.savefig(out / "gate_traffic_bar.png", dpi=200)
    plt.close(fig)

    # 图 2：缓存异构性 vs 编码增益
    fig, ax = plt.subplots(figsize=(6.5, 4.2))
    ax.scatter(df["cache_heterogeneity"], df["xor2_gain_over_mcast"])
    ax.set_xlabel("Cache Heterogeneity (1 - Avg. Jaccard)")
    ax.set_ylabel("Strict-XOR2 Gain over MCAST")
    ax.set_title("Coding Gain vs. Cache Heterogeneity")
    fig.tight_layout()
    fig.savefig(out / "gain_vs_cache_heterogeneity.pdf")
    fig.savefig(out / "gain_vs_cache_heterogeneity.png", dpi=200)
    plt.close(fig)

    # 图 3：coding density vs 编码增益
    fig, ax = plt.subplots(figsize=(6.5, 4.2))
    ax.scatter(df["coding_density"], df["xor2_gain_over_mcast"])
    ax.set_xlabel("Reciprocal Side-Information Density")
    ax.set_ylabel("Strict-XOR2 Gain over MCAST")
    ax.set_title("Coding Opportunity vs. Realized Gain")
    fig.tight_layout()
    fig.savefig(out / "gain_vs_coding_density.pdf")
    fig.savefig(out / "gain_vs_coding_density.png", dpi=200)
    plt.close(fig)

    print(f"[OK] figures -> {out}")


if __name__ == "__main__":
    main()
