#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import argparse

def parse_md(path):
    rows = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line.startswith("|"):
            continue
        if "---" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        rows.append(cells)
    header = rows[0]
    data = rows[1:]
    return header, data

def to_float(x):
    return float(str(x).replace(",", "").strip())

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in-md", required=True)
    ap.add_argument("--outdir", required=True)

    # 和你现在最终采用的势函数权重保持一致
    ap.add_argument("--lambda-delay", type=float, default=1.0)
    ap.add_argument("--lambda-frag", type=float, default=0.1)
    ap.add_argument("--lambda-aff", type=float, default=0.2)
    ap.add_argument("--lambda-load", type=float, default=0.03)

    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    header, data = parse_md(args.in_md)
    idx = {h: i for i, h in enumerate(header)}

    raw_rows = []
    for r in data:
        raw_rows.append({
            "requests": int(float(r[idx["requests"]])),
            "method": r[idx["method"]],
            "phi_total_raw": to_float(r[idx["phi_total"]]),
            "delay_term_raw": to_float(r[idx["delay_term"]]),
            "frag_term_raw": to_float(r[idx["frag_term"]]),
            "aff_reward_term_raw": to_float(r[idx["aff_reward_term"]]),
            "load_term_raw": to_float(r[idx["load_term"]]),
        })

    # 全局尺度归一化：保留 requests 增大带来的趋势，同时避免 delay_term 数值支配
    scales = {}
    for k in ["delay_term_raw", "frag_term_raw", "aff_reward_term_raw", "load_term_raw"]:
        vals = [abs(x[k]) for x in raw_rows]
        scales[k] = sum(vals) / max(len(vals), 1)
        if scales[k] == 0:
            scales[k] = 1.0

    rows = []
    for r in raw_rows:
        delay_norm = r["delay_term_raw"] / scales["delay_term_raw"]
        frag_norm = r["frag_term_raw"] / scales["frag_term_raw"]
        aff_norm = r["aff_reward_term_raw"] / scales["aff_reward_term_raw"]
        load_norm = r["load_term_raw"] / scales["load_term_raw"]

        delay_weighted = args.lambda_delay * delay_norm
        frag_weighted = args.lambda_frag * frag_norm
        aff_weighted = args.lambda_aff * aff_norm
        load_weighted = args.lambda_load * load_norm

        # aff_reward 是收益项，所以在势函数里减去
        phi_norm = delay_weighted + frag_weighted - aff_weighted + load_weighted

        rows.append({
            **r,
            "delay_norm": delay_norm,
            "frag_norm": frag_norm,
            "aff_reward_norm": aff_norm,
            "load_norm": load_norm,
            "delay_weighted": delay_weighted,
            "frag_weighted": frag_weighted,
            "aff_reward_weighted": aff_weighted,
            "load_weighted": load_weighted,
            "phi_norm": phi_norm,
        })

    reqs = sorted(set(r["requests"] for r in rows))
    methods = []
    for r in rows:
        if r["method"] not in methods:
            methods.append(r["method"])

    # 详细长表
    detailed = os.path.join(args.outdir, "summary_phase1_potential_normalized_global.md")
    with open(detailed, "w", encoding="utf-8") as f:
        f.write("| requests | method | phi_norm | delay_norm | frag_norm | aff_reward_norm | load_norm | delay_weighted | frag_weighted | aff_reward_weighted | load_weighted | phi_total_raw | delay_term_raw | frag_term_raw | aff_reward_term_raw | load_term_raw |\n")
        f.write("|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n")
        for r in sorted(rows, key=lambda x: (x["requests"], x["method"])):
            f.write(
                "| {requests} | {method} | {phi_norm:.6f} | {delay_norm:.6f} | {frag_norm:.6f} | {aff_reward_norm:.6f} | {load_norm:.6f} | {delay_weighted:.6f} | {frag_weighted:.6f} | {aff_reward_weighted:.6f} | {load_weighted:.6f} | {phi_total_raw:.3f} | {delay_term_raw:.3f} | {frag_term_raw:.3f} | {aff_reward_term_raw:.3f} | {load_term_raw:.3f} |\n".format(**r)
            )

    # 生成宽表，方便画折线图
    metrics = [
        "phi_norm",
        "delay_norm",
        "frag_norm",
        "aff_reward_norm",
        "load_norm",
        "delay_weighted",
        "frag_weighted",
        "aff_reward_weighted",
        "load_weighted",
    ]

    for metric in metrics:
        out = os.path.join(args.outdir, f"plot_{metric}_matrix.md")
        mp = {(r["method"], r["requests"]): r[metric] for r in rows}
        with open(out, "w", encoding="utf-8") as f:
            f.write(f"# {metric}\n\n")
            f.write("| method | " + " | ".join(map(str, reqs)) + " |\n")
            f.write("|---|" + "|".join(["---:"] * len(reqs)) + "|\n")
            for m in methods:
                vals = [mp.get((m, q), None) for q in reqs]
                vals = [f"{v:.6f}" if v is not None else "MISSING" for v in vals]
                f.write("| {} | {} |\n".format(m, " | ".join(vals)))

    scale_file = os.path.join(args.outdir, "normalization_scales.md")
    with open(scale_file, "w", encoding="utf-8") as f:
        f.write("| component | global_mean_abs_scale |\n")
        f.write("|---|---:|\n")
        for k, v in scales.items():
            f.write(f"| {k} | {v:.6f} |\n")

    print("[OK]", detailed)
    print("[OK]", scale_file)
    print("\n===== normalized phi table =====")
    print(open(os.path.join(args.outdir, "plot_phi_norm_matrix.md"), encoding="utf-8").read())

if __name__ == "__main__":
    main()
