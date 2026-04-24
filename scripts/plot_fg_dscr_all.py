#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
import argparse
import json
import math
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns


def parse_result_args(items: List[str]) -> List[Tuple[str, str]]:
    """
    输入格式:
      baseline=result_200.json
      light=result_200_light.json
      mid=result_200_mid.json
    """
    pairs = []
    for item in items:
        if "=" not in item:
            raise ValueError(f"--results 参数必须写成 label=path，收到: {item}")
        label, path = item.split("=", 1)
        label = label.strip()
        path = path.strip()
        if not label or not path:
            raise ValueError(f"--results 参数格式错误: {item}")
        pairs.append((label, path))
    return pairs


def load_json(path: str) -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_tables(result_pairs: List[Tuple[str, str]]):
    summary_rows = []
    assign_rows = []
    phase1_rows = []
    phase2_rows = []
    delay_rows = []

    for label, path in result_pairs:
        obj = load_json(path)

        summary = obj.get("summary", {})
        summary_rows.append({
            "label": label,
            "file": path,
            "algo": summary.get("algo", label),
            "num_containers": summary.get("num_containers"),
            "num_nodes": summary.get("num_nodes"),
            "ACT": summary.get("ACT"),
            "AMS": summary.get("AMS"),
            "downloaded_mb": summary.get("downloaded_mb"),
            "reused_mb": summary.get("reused_mb"),
            "reuse_rate": summary.get("reuse_rate"),
            "objective": summary.get("objective"),
        })

        for eid, cids in obj.get("assignment", {}).items():
            assign_rows.append({
                "label": label,
                "file": path,
                "node_id": eid,
                "num_containers_on_node": len(cids),
            })

        for rec in obj.get("phase1_history", []):
            phase1_rows.append({
                "label": label,
                "file": path,
                "cycle": rec.get("cycle"),
                "phase1_label": rec.get("label"),
                "potential": rec.get("potential"),
                "fragmented_cpu": rec.get("fragmented_cpu"),
                "fragmented_mem": rec.get("fragmented_mem"),
                "fragmented_disk": rec.get("fragmented_disk"),
                "active_nodes": rec.get("active_nodes"),
                "idle_nodes": rec.get("idle_nodes"),
            })

        for rec in obj.get("phase2_reuse_history", []):
            phase2_rows.append({
                "label": label,
                "file": path,
                "round": rec.get("round"),
                "step_reuse_mb_global": rec.get("step_reuse_mb_global"),
                "step_downloaded_mb_global": rec.get("step_downloaded_mb_global"),
                "cumulative_reuse_mb_global": rec.get("cumulative_reuse_mb_global"),
                "cumulative_downloaded_mb_global": rec.get("cumulative_downloaded_mb_global"),
                "active_nodes": rec.get("active_nodes"),
            })

        for rec in obj.get("container_metrics", []):
            delay_rows.append({
                "label": label,
                "file": path,
                "algo": rec.get("algo", label),
                "cid": rec.get("cid"),
                "service_type": rec.get("service_type", "default"),
                "node_id": rec.get("node_id"),
                "local_step": rec.get("local_step"),
                "wait_time": rec.get("wait_time"),
                "pull_time": rec.get("pull_time"),
                "deploy_delay": rec.get("deploy_delay"),
                "completion_time": rec.get("completion_time"),
                "run_time": rec.get("run_time"),
                "reuse_mb": rec.get("reuse_mb"),
                "downloaded_mb": rec.get("downloaded_mb"),
            })

    return (
        pd.DataFrame(summary_rows),
        pd.DataFrame(assign_rows),
        pd.DataFrame(phase1_rows),
        pd.DataFrame(phase2_rows),
        pd.DataFrame(delay_rows),
    )


def save_tables(
    outdir: Path,
    df_summary: pd.DataFrame,
    df_assign: pd.DataFrame,
    df_phase1: pd.DataFrame,
    df_phase2: pd.DataFrame,
    df_delay: pd.DataFrame,
):
    df_summary.to_csv(outdir / "summary_table.csv", index=False, encoding="utf-8-sig")
    df_assign.to_csv(outdir / "assignment_table.csv", index=False, encoding="utf-8-sig")
    df_phase1.to_csv(outdir / "phase1_table.csv", index=False, encoding="utf-8-sig")
    df_phase2.to_csv(outdir / "phase2_reuse_table.csv", index=False, encoding="utf-8-sig")
    df_delay.to_csv(outdir / "delay_table.csv", index=False, encoding="utf-8-sig")


def plot_fragmentation(df_phase1: pd.DataFrame, outdir: Path):
    """
    图1：
    x轴：时间周期
    y轴：碎片化 CPU / MEM / DISK 资源量
    一排三个子图
    """
    if df_phase1.empty:
        return

    sns.set_theme(style="whitegrid")
    fig, axes = plt.subplots(1, 3, figsize=(18, 5))

    items = [
        ("fragmented_cpu", "碎片化 CPU"),
        ("fragmented_mem", "碎片化 内存"),
        ("fragmented_disk", "碎片化 磁盘"),
    ]

    for ax, (col, title) in zip(axes, items):
        sns.lineplot(
            data=df_phase1,
            x="cycle",
            y=col,
            hue="label",
            marker="o",
            ax=ax,
        )
        ax.set_title(title)
        ax.set_xlabel("时间周期 / cycle")
        ax.set_ylabel("资源量")
        ax.legend(title="结果版本", fontsize=8, title_fontsize=9)

    plt.tight_layout()
    fig.savefig(outdir / "fig1_fragmentation.png", dpi=220, bbox_inches="tight")
    plt.close(fig)


def plot_potential(df_phase1: pd.DataFrame, outdir: Path):
    """
    图2：
    x轴：时间周期
    y轴：全局势函数
    """
    if df_phase1.empty:
        return

    sns.set_theme(style="whitegrid")
    fig, ax = plt.subplots(figsize=(8, 5))
    sns.lineplot(
        data=df_phase1,
        x="cycle",
        y="potential",
        hue="label",
        marker="o",
        ax=ax,
    )
    ax.set_title("全局势函数变化")
    ax.set_xlabel("时间周期 / cycle")
    ax.set_ylabel("Potential")
    ax.legend(title="结果版本", fontsize=8, title_fontsize=9)
    plt.tight_layout()
    fig.savefig(outdir / "fig2_potential.png", dpi=220, bbox_inches="tight")
    plt.close(fig)


def plot_reuse_total(df_phase2: pd.DataFrame, outdir: Path):
    """
    图3：
    x轴：时间周期
    y轴：通过层复用命中的总数据量
    这里画累计复用量，更符合“总数据量”
    """
    if df_phase2.empty:
        return

    sns.set_theme(style="whitegrid")
    fig, ax = plt.subplots(figsize=(8, 5))
    sns.lineplot(
        data=df_phase2,
        x="round",
        y="cumulative_reuse_mb_global",
        hue="label",
        marker="o",
        ax=ax,
    )
    ax.set_title("层复用命中的累计总数据量")
    ax.set_xlabel("时间周期 / round")
    ax.set_ylabel("累计复用命中量 (MB)")
    ax.legend(title="结果版本", fontsize=8, title_fontsize=9)
    plt.tight_layout()
    fig.savefig(outdir / "fig3_reuse_total.png", dpi=220, bbox_inches="tight")
    plt.close(fig)


def plot_delay_distribution(df_delay: pd.DataFrame, outdir: Path, delay_metric: str = "deploy_delay"):
    """
    图4：
    x轴：服务类型
    y轴：部署延迟分布
    每个结果版本一个子图
    """
    if df_delay.empty:
        return

    labels = list(df_delay["label"].dropna().unique())
    if len(labels) == 0:
        return

    cols = min(2, len(labels))
    rows = math.ceil(len(labels) / cols)

    sns.set_theme(style="whitegrid")
    fig, axes = plt.subplots(rows, cols, figsize=(8 * cols, 5 * rows), squeeze=False)
    axes = axes.flatten()

    for ax, label in zip(axes, labels):
        sub = df_delay[df_delay["label"] == label].copy()
        if sub.empty:
            ax.set_visible(False)
            continue

        med = sub.groupby("service_type")[delay_metric].median().sort_values()
        order = med.index.tolist()

        sns.violinplot(
            data=sub,
            x="service_type",
            y=delay_metric,
            order=order,
            inner="box",
            cut=0,
            ax=ax,
        )
        ax.set_title(f"{label} - 各服务类型延迟分布")
        ax.set_xlabel("服务类型")
        ax.set_ylabel(delay_metric)
        ax.tick_params(axis="x", rotation=30)

    for i in range(len(labels), len(axes)):
        axes[i].set_visible(False)

    plt.tight_layout()
    fig.savefig(outdir / "fig4_delay_distribution.png", dpi=220, bbox_inches="tight")
    plt.close(fig)


def print_summary(df_summary: pd.DataFrame):
    if df_summary.empty:
        return
    cols = ["label", "ACT", "AMS", "downloaded_mb", "reused_mb", "reuse_rate", "objective"]
    show = df_summary[cols].copy()
    print("\n=== Summary ===")
    print(show.to_string(index=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--results",
        nargs="+",
        required=True,
        help="格式: label=path，例如 baseline=result_200.json mid=result_200_mid.json",
    )
    parser.add_argument("--outdir", type=str, default="plot_outputs")
    parser.add_argument(
        "--delay-metric",
        type=str,
        default="deploy_delay",
        choices=["deploy_delay", "completion_time", "wait_time", "pull_time"],
        help="图4使用哪种延迟指标",
    )
    args = parser.parse_args()

    result_pairs = parse_result_args(args.results)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    df_summary, df_assign, df_phase1, df_phase2, df_delay = build_tables(result_pairs)
    save_tables(outdir, df_summary, df_assign, df_phase1, df_phase2, df_delay)

    plot_fragmentation(df_phase1, outdir)
    plot_potential(df_phase1, outdir)
    plot_reuse_total(df_phase2, outdir)
    plot_delay_distribution(df_delay, outdir, delay_metric=args.delay_metric)

    print_summary(df_summary)
    print(f"\\n图和表已经输出到: {outdir.resolve()}")


if __name__ == "__main__":
    main()