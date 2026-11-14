from __future__ import annotations

import argparse
import csv
import math
import statistics
from collections import defaultdict
from pathlib import Path


# 双侧百分之九十五置信区间，
# 自由度 1~30 的 t 分布临界值。
T975 = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
    11: 2.201,
    12: 2.179,
    13: 2.160,
    14: 2.145,
    15: 2.131,
    16: 2.120,
    17: 2.110,
    18: 2.101,
    19: 2.093,
    20: 2.086,
    21: 2.080,
    22: 2.074,
    23: 2.069,
    24: 2.064,
    25: 2.060,
    26: 2.056,
    27: 2.052,
    28: 2.048,
    29: 2.045,
    30: 2.042,
}


def mean_ci95(values):
    values = list(values)
    n = len(values)

    if n == 0:
        return 0.0, 0.0

    mean = statistics.mean(values)

    if n == 1:
        return mean, 0.0

    sd = statistics.stdev(values)

    t = T975.get(
        n - 1,
        1.96,
    )

    ci = (
        t
        * sd
        / math.sqrt(n)
    )

    return mean, ci


def write_csv(path, rows):
    if not rows:
        return

    with path.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=list(
                rows[0].keys()
            ),
        )

        writer.writeheader()
        writer.writerows(rows)


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--input",
        required=True,
    )

    ap.add_argument(
        "--out",
        required=True,
    )

    args = ap.parse_args()

    raw = []

    with open(
        args.input,
        newline="",
        encoding="utf-8",
    ) as f:
        for r in csv.DictReader(f):
            for k in (
                "base_requests",
                "target_requests",
                "seed",
                "accepted",
                "excluded",
            ):
                r[k] = int(r[k])

            for k in (
                "cache_scale",
                "wan_mb_s",
                "mean_ready_s",
                "p95_ready_s",
                "makespan_s",
                "registry_mb",
                "peer_mb",
                "total_transfer_mb",
                "evicted_mb",
                "refused_cache_mb",
                "max_lease_mb",
            ):
                r[k] = float(r[k])

            raw.append(r)

    out = Path(args.out)
    out.mkdir(
        parents=True,
        exist_ok=True,
    )

    metrics = [
        "mean_ready_s",
        "p95_ready_s",
        "makespan_s",
        "registry_mb",
        "peer_mb",
        "evicted_mb",
        "refused_cache_mb",
    ]

    grouped = defaultdict(list)

    for r in raw:
        key = (
            r["case"],
            r["base_requests"],
            r["target_requests"],
            r["cache_scale"],
            r["wan_mb_s"],
            r["policy"],
        )

        grouped[key].append(r)

    summary_rows = []

    for key, rows in sorted(
        grouped.items()
    ):
        (
            case,
            base_requests,
            target_requests,
            scale,
            wan,
            policy,
        ) = key

        rec = {
            "case": case,
            "base_requests": (
                base_requests
            ),
            "target_requests": (
                target_requests
            ),
            "cache_scale": scale,
            "wan_mb_s": wan,
            "policy": policy,
            "num_seeds": len(rows),
        }

        for metric in metrics:
            mean, ci = mean_ci95(
                r[metric]
                for r in rows
            )

            rec[
                metric + "_mean"
            ] = mean

            rec[
                metric + "_ci95"
            ] = ci

        summary_rows.append(rec)

    write_csv(
        out / "summary.csv",
        summary_rows,
    )

    by_seed = {
        (
            r["case"],
            r["seed"],
            r["cache_scale"],
            r["wan_mb_s"],
            r["policy"],
        ): r
        for r in raw
    }

    gain_groups = defaultdict(list)

    for r in raw:
        if r["policy"] != "d_lease":
            continue

        baseline_key = (
            r["case"],
            r["seed"],
            r["cache_scale"],
            r["wan_mb_s"],
            "c_lru",
        )

        if baseline_key not in by_seed:
            continue

        b = by_seed[
            baseline_key
        ]

        mean_gain = (
            100.0
            * (
                b["mean_ready_s"]
                - r["mean_ready_s"]
            )
            / b["mean_ready_s"]
        )

        p95_gain = (
            100.0
            * (
                b["p95_ready_s"]
                - r["p95_ready_s"]
            )
            / b["p95_ready_s"]
        )

        registry_delta = (
            100.0
            * (
                r["registry_mb"]
                - b["registry_mb"]
            )
            / max(
                b["registry_mb"],
                1e-9,
            )
        )

        key = (
            r["case"],
            r["base_requests"],
            r["target_requests"],
            r["cache_scale"],
            r["wan_mb_s"],
        )

        gain_groups[key].append(
            (
                mean_gain,
                p95_gain,
                registry_delta,
            )
        )

    gain_rows = []

    print()
    print(
        "D-LEASE 相对 C-LRU 的正式多随机种子结果"
    )
    print()

    print(
        "CASE     CACHE WAN "
        "MEAN_GAIN            "
        "P95_GAIN             "
        "REGISTRY_DELTA"
    )

    for key, vals in sorted(
        gain_groups.items()
    ):
        (
            case,
            base_requests,
            target_requests,
            scale,
            wan,
        ) = key

        mg, mg_ci = mean_ci95(
            x[0] for x in vals
        )

        pg, pg_ci = mean_ci95(
            x[1] for x in vals
        )

        rd, rd_ci = mean_ci95(
            x[2] for x in vals
        )

        gain_rows.append(
            {
                "case": case,
                "base_requests": (
                    base_requests
                ),
                "target_requests": (
                    target_requests
                ),
                "cache_scale": scale,
                "wan_mb_s": wan,
                "num_seeds": len(vals),
                "mean_gain_pct": mg,
                "mean_gain_ci95": (
                    mg_ci
                ),
                "p95_gain_pct": pg,
                "p95_gain_ci95": (
                    pg_ci
                ),
                "registry_delta_pct": (
                    rd
                ),
                "registry_delta_ci95": (
                    rd_ci
                ),
            }
        )

        print(
            f"{base_requests:<8d} "
            f"{scale:<5g} "
            f"{wan:<3g} "
            f"{mg:+6.2f}%"
            f" ± {mg_ci:5.2f}  "
            f"{pg:+6.2f}%"
            f" ± {pg_ci:5.2f}  "
            f"{rd:+6.2f}%"
            f" ± {rd_ci:5.2f}"
        )

    write_csv(
        out / "d_lease_vs_c_lru.csv",
        gain_rows,
    )

    print()
    print(
        "Saved:",
        (out / "summary.csv").resolve(),
    )

    print(
        "Saved:",
        (
            out
            / "d_lease_vs_c_lru.csv"
        ).resolve(),
    )


if __name__ == "__main__":
    main()
