from __future__ import annotations

import argparse
import csv
import math
import statistics
from collections import defaultdict
from pathlib import Path


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
    30: 2.042,
}


METRICS = [
    "mean_ready_s",
    "p95_ready_s",
    "makespan_s",
    "registry_mb",
    "peer_mb",
    "p2p_offload_pct",
    "runtime_ms",
]


def mean_ci95(values):
    xs = list(values)
    n = len(xs)

    if not xs:
        return 0.0, 0.0

    m = statistics.mean(xs)

    if n == 1:
        return m, 0.0

    sd = statistics.stdev(xs)

    df = n - 1

    if df in T975:
        t = T975[df]
    elif df > 30:
        t = 1.96
    else:
        # conservative fallback
        t = 2.0

    ci = (
        t
        * sd
        / math.sqrt(n)
    )

    return m, ci


def write_csv(path, rows):
    if not rows:
        return

    with path.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as f:
        w = csv.DictWriter(
            f,
            fieldnames=list(
                rows[0].keys()
            ),
        )

        w.writeheader()
        w.writerows(rows)


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

    rows = []

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
            ) + tuple(METRICS):
                r[k] = float(r[k])

            rows.append(r)

    grouped = defaultdict(list)

    for r in rows:
        key = (
            r["base_requests"],
            r["target_requests"],
            r["cache_scale"],
            r["wan_mb_s"],
            r["baseline"],
        )

        grouped[key].append(r)

    summary = []

    for key, xs in sorted(
        grouped.items()
    ):
        (
            base_req,
            target_req,
            cache,
            wan,
            baseline,
        ) = key

        rec = {
            "base_requests": base_req,
            "target_requests": (
                target_req
            ),
            "cache_scale": cache,
            "wan_mb_s": wan,
            "baseline": baseline,
            "num_seeds": len(xs),
        }

        for metric in METRICS:
            mean, ci = mean_ci95(
                x[metric]
                for x in xs
            )

            rec[
                metric + "_mean"
            ] = mean

            rec[
                metric + "_ci95"
            ] = ci

        summary.append(rec)

    out = Path(args.out)
    out.mkdir(
        parents=True,
        exist_ok=True,
    )

    write_csv(
        out / "summary.csv",
        summary,
    )

    # -------------------------------------------------
    # Relative improvement over Registry
    # -------------------------------------------------

    index = {
        (
            r["base_requests"],
            r["target_requests"],
            r["cache_scale"],
            r["wan_mb_s"],
            r["baseline"],
        ): r
        for r in summary
    }

    relative = []

    for r in summary:
        if r["baseline"] == "registry":
            continue

        reg_key = (
            r["base_requests"],
            r["target_requests"],
            r["cache_scale"],
            r["wan_mb_s"],
            "registry",
        )

        reg = index.get(
            reg_key
        )

        if not reg:
            continue

        mean_gain = (
            100.0
            * (
                reg[
                    "mean_ready_s_mean"
                ]
                - r[
                    "mean_ready_s_mean"
                ]
            )
            / max(
                reg[
                    "mean_ready_s_mean"
                ],
                1e-9,
            )
        )

        p95_gain = (
            100.0
            * (
                reg[
                    "p95_ready_s_mean"
                ]
                - r[
                    "p95_ready_s_mean"
                ]
            )
            / max(
                reg[
                    "p95_ready_s_mean"
                ],
                1e-9,
            )
        )

        registry_reduction = (
            100.0
            * (
                reg[
                    "registry_mb_mean"
                ]
                - r[
                    "registry_mb_mean"
                ]
            )
            / max(
                reg[
                    "registry_mb_mean"
                ],
                1e-9,
            )
        )

        relative.append(
            {
                "base_requests": (
                    r["base_requests"]
                ),
                "target_requests": (
                    r["target_requests"]
                ),
                "cache_scale": (
                    r["cache_scale"]
                ),
                "wan_mb_s": (
                    r["wan_mb_s"]
                ),
                "baseline": (
                    r["baseline"]
                ),
                "num_seeds": (
                    r["num_seeds"]
                ),
                "mean_ready_gain_pct": (
                    mean_gain
                ),
                "p95_gain_pct": (
                    p95_gain
                ),
                "registry_reduction_pct": (
                    registry_reduction
                ),
                "p2p_offload_pct": (
                    r[
                        "p2p_offload_pct_mean"
                    ]
                ),
            }
        )

    write_csv(
        out / "vs_registry.csv",
        relative,
    )

    print()
    print(
        "REQ CACHE WAN BASELINE "
        "N MEAN±CI P95±CI "
        "REGISTRY OFFLOAD"
    )

    for r in summary:
        print(
            f"{r['base_requests']:<4d} "
            f"{r['cache_scale']:<5g} "
            f"{r['wan_mb_s']:<3g} "
            f"{r['baseline']:<27} "
            f"{r['num_seeds']:<2d} "
            f"{r['mean_ready_s_mean']:8.2f}"
            f"±{r['mean_ready_s_ci95']:<6.2f} "
            f"{r['p95_ready_s_mean']:8.2f}"
            f"±{r['p95_ready_s_ci95']:<6.2f} "
            f"{r['registry_mb_mean']:10.0f} "
            f"{r['p2p_offload_pct_mean']:6.1f}%"
        )

    print()
    print(
        "Saved:",
        (out / "summary.csv").resolve(),
    )

    print(
        "Saved:",
        (out / "vs_registry.csv").resolve(),
    )


if __name__ == "__main__":
    main()
