from __future__ import annotations

import argparse
import bisect
import csv
import json
import random
import re
from datetime import datetime
from pathlib import Path


def load_arrival_profile(path: Path):
    rows = []

    with path.open(
        encoding="utf-8-sig"
    ) as f:

        reader = csv.DictReader(f)

        for r in reader:

            t = datetime.strptime(
                r["minute"],
                "%Y-%m-%d %H:%M",
            )

            count = int(
                r["requests"]
            )

            rows.append(
                (
                    t,
                    count,
                )
            )

    rows.sort(
        key=lambda x: x[0]
    )

    return rows


def build_cdf(weights):
    cdf = []
    total = 0.0

    for x in weights:
        total += float(x)
        cdf.append(total)

    if total <= 0:
        raise ValueError(
            "arrival window has zero requests"
        )

    return cdf, total


def sample_arrivals(
    n: int,
    weights,
    horizon_s: float,
    seed: int,
):
    """
    Sample n deployment arrivals from the empirical
    minute-level DRTP intensity profile.

    The chosen trace window is normalized onto a fixed
    experiment horizon.  This preserves relative burst
    shape while keeping offered load controllable.
    """

    rng = random.Random(seed)

    cdf, total = build_cdf(
        weights
    )

    m = len(weights)

    xs = []

    for _ in range(n):

        u = rng.random() * total

        minute_idx = bisect.bisect_left(
            cdf,
            u,
        )

        minute_idx = min(
            minute_idx,
            m - 1,
        )

        # Uniform placement inside the selected minute.
        frac = rng.random()

        trace_position = (
            minute_idx
            + frac
        ) / m

        arrival_s = (
            trace_position
            * horizon_s
        )

        xs.append(arrival_s)

    xs.sort()

    return xs


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--input-dir",
        default=(
            "cases/"
            "cider_weak_scale_nodes"
        ),
    )

    ap.add_argument(
        "--output-dir",
        default=(
            "cases/"
            "cider_weak_scale_arrival"
        ),
    )

    ap.add_argument(
        "--arrival-csv",
        default=(
            "data/stats/drtp_stats/"
            "minute_arrivals.csv"
        ),
    )

    ap.add_argument(
        "--window-minutes",
        type=int,
        default=1440,
    )

    ap.add_argument(
        "--window-start",
        type=int,
        default=-1,
        help=(
            "-1 = deterministically choose "
            "one trace window using seed"
        ),
    )

    ap.add_argument(
        "--horizon-s",
        type=float,
        default=240.0,
    )

    ap.add_argument(
        "--seed",
        type=int,
        default=1,
    )

    args = ap.parse_args()

    input_dir = Path(
        args.input_dir
    )

    output_dir = Path(
        args.output_dir
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    trace = load_arrival_profile(
        Path(
            args.arrival_csv
        )
    )

    w = int(
        args.window_minutes
    )

    if (
        w <= 0
        or w > len(trace)
    ):
        raise ValueError(
            "invalid window-minutes"
        )

    max_start = (
        len(trace)
        - w
    )

    if args.window_start >= 0:
        start = int(
            args.window_start
        )
    else:
        selector = random.Random(
            args.seed
        )

        start = selector.randint(
            0,
            max_start,
        )

    if (
        start < 0
        or start > max_start
    ):
        raise ValueError(
            "invalid window-start"
        )

    window = trace[
        start:
        start + w
    ]

    weights = [
        count
        for _, count
        in window
    ]

    print(
        "DRTP window:",
        window[0][0],
        "->",
        window[-1][0],
    )

    print(
        "window requests:",
        sum(weights),
    )

    print(
        "full-case horizon:",
        args.horizon_s,
        "s",
    )

    files = sorted(
        input_dir.glob(
            "cider_nodes*_weak.json"
        )
    )

    if not files:
        raise FileNotFoundError(
            f"no weak-scale cases in "
            f"{input_dir}"
        )

    for src in files:

        m = re.search(
            r"nodes(\d+)",
            src.name,
        )

        if not m:
            continue

        num_nodes = int(
            m.group(1)
        )

        case = json.loads(
            src.read_text(
                encoding="utf-8"
            )
        )

        containers = case[
            "containers"
        ]

        n = len(containers)

        # Deterministic, but different sample realization
        # for each scale.
        case_seed = (
            args.seed
            + 1009 * num_nodes
        )

        arrivals = sample_arrivals(
            n=n,
            weights=weights,
            horizon_s=float(
                args.horizon_s
            ),
            seed=case_seed,
        )

        for c, a in zip(
            containers,
            arrivals,
        ):
            c[
                "arrival_time_s"
            ] = float(a)

        meta = case.setdefault(
            "meta",
            {},
        )

        meta[
            "arrival_source"
        ] = (
            "DRTP minute_arrivals.csv"
        )

        meta[
            "arrival_window_start"
        ] = window[0][0].isoformat()

        meta[
            "arrival_window_end"
        ] = window[-1][0].isoformat()

        meta[
            "arrival_window_minutes"
        ] = w

        meta[
            "arrival_full_horizon_s"
        ] = float(
            args.horizon_s
        )

        meta[
            "arrival_seed"
        ] = int(
            case_seed
        )

        meta[
            "arrival_semantics"
        ] = (
            "Empirical DRTP minute-level "
            "intensity shape mapped to "
            "container deployment arrivals"
        )

        dst = (
            output_dir
            / src.name
        )

        dst.write_text(
            json.dumps(
                case,
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        print(
            f"[OK] N={num_nodes:<2d} "
            f"containers={n:<4d} "
            f"arrival="
            f"{arrivals[0]:.3f}"
            f".."
            f"{arrivals[-1]:.3f}s "
            f"-> {dst}"
        )


if __name__ == "__main__":
    main()
