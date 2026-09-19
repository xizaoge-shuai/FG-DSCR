#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import os
import re
from collections import defaultdict

def is_number(x):
    try:
        float(x)
        return True
    except Exception:
        return False

def parse_md_table(path):
    rows = []
    header = None

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("|"):
                continue
            if "---" in line:
                continue

            parts = [x.strip() for x in line.strip("|").split("|")]

            if header is None:
                header = parts
                continue

            if len(parts) != len(header):
                continue

            row = dict(zip(header, parts))

            # 兼容列名
            req_key = None
            for k in ["requests", "request", "num_containers", "size"]:
                if k in row:
                    req_key = k
                    break

            if req_key is None or "method" not in row:
                continue

            needed = ["phi_total", "delay_term", "frag_term", "aff_reward_term", "load_term"]
            if not all(k in row for k in needed):
                continue

            if not is_number(row[req_key]):
                continue

            try:
                rows.append({
                    "requests": int(float(row[req_key])),
                    "method": row["method"],
                    "phi_total_raw": float(row["phi_total"]),
                    "delay_term": float(row["delay_term"]),
                    "frag_term": float(row["frag_term"]),
                    "aff_reward_term": float(row["aff_reward_term"]),
                    "load_term": float(row["load_term"]),
                })
            except Exception:
                pass

    return rows

def safe_scale(vals):
    m = max(abs(x) for x in vals) if vals else 0.0
    return m if m > 1e-12 else 1.0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--lambda-delay", type=float, default=1.0)
    ap.add_argument("--lambda-frag", type=float, default=1.0)
    ap.add_argument("--lambda-aff", type=float, default=1.0)
    ap.add_argument("--lambda-load", type=float, default=1.0)
    args = ap.parse_args()

    rows = parse_md_table(args.input)
    if not rows:
        raise SystemExit(f"[FAIL] no valid rows parsed from {args.input}")

    groups = defaultdict(list)
    for r in rows:
        groups[r["requests"]].append(r)

    out_rows = []

    for req, rs in sorted(groups.items()):
        sd = safe_scale([r["delay_term"] for r in rs])
        sf = safe_scale([r["frag_term"] for r in rs])
        sa = safe_scale([r["aff_reward_term"] for r in rs])
        sl = safe_scale([r["load_term"] for r in rs])

        for r in rs:
            delay_norm = r["delay_term"] / sd
            frag_norm = r["frag_term"] / sf
            aff_norm = r["aff_reward_term"] / sa
            load_norm = r["load_term"] / sl

            phi_norm = (
                args.lambda_delay * delay_norm
                + args.lambda_frag * frag_norm
                - args.lambda_aff * aff_norm
                + args.lambda_load * load_norm
            )

            out_rows.append({
                **r,
                "delay_norm": delay_norm,
                "frag_norm": frag_norm,
                "aff_norm": aff_norm,
                "load_norm": load_norm,
                "phi_total_norm": phi_norm,
                "scale_delay": sd,
                "scale_frag": sf,
                "scale_aff": sa,
                "scale_load": sl,
            })

    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("| requests | method | phi_total_norm | delay_norm | frag_norm | aff_norm | load_norm | phi_total_raw | delay_term | frag_term | aff_reward_term | load_term |\n")
        f.write("|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n")

        for r in sorted(out_rows, key=lambda x: (x["requests"], x["method"])):
            f.write(
                "| {requests} | {method} | {phi_total_norm:.6f} | {delay_norm:.6f} | {frag_norm:.6f} | {aff_norm:.6f} | {load_norm:.6f} | {phi_total_raw:.3f} | {delay_term:.3f} | {frag_term:.6f} | {aff_reward_term:.3f} | {load_term:.3f} |\n".format(**r)
            )

    print("[OK]", args.out)
    print("[INFO] normalized phi = delay_norm + frag_norm - aff_norm + load_norm")
    print("[INFO] raw terms are preserved for interpretation")

if __name__ == "__main__":
    main()
