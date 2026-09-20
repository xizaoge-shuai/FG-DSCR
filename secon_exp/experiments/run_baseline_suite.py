from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from secon_exp.baselines import BASELINES
from secon_exp.simulator import simulate


def load_case(path):
    return json.loads(
        Path(path).read_text(
            encoding="utf-8"
        )
    )


def prepare_from_state(case, state):
    target_ids = set(
        state["target_request_ids"]
    )

    case["containers"] = [
        c
        for c in case["containers"]
        if c["cid"] in target_ids
    ]

    scale = float(
        state.get(
            "cache_scale",
            1.0,
        )
    )

    warm_cache = state[
        "warm_final_cache"
    ]

    for node in case["nodes"]:
        node["repo_capacity_mb"] = int(
            round(
                float(
                    node["repo_capacity_mb"]
                )
                * scale
            )
        )

        node["initial_cache"] = list(
            warm_cache.get(
                node["eid"],
                [],
            )
        )

    placement = state[
        "target_assignment"
    ]

    return case, placement


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--case",
        required=True,
    )

    ap.add_argument(
        "--state",
        help=(
            "Formal FG warm-up state JSON. "
            "If supplied, target requests, "
            "placement and warm cache are "
            "reconstructed automatically."
        ),
    )

    ap.add_argument(
        "--placement",
        help="Existing placement JSON",
    )

    ap.add_argument(
        "--wan",
        type=float,
        default=10.0,
    )

    ap.add_argument(
        "--upload",
        type=float,
        default=100.0,
    )

    ap.add_argument(
        "--lan",
        type=float,
        default=1000.0,
    )

    ap.add_argument(
        "--cache-scale",
        type=float,
        default=1.0,
    )

    ap.add_argument(
        "--baselines",
        nargs="+",
        default=list(BASELINES),
    )

    ap.add_argument(
        "--out",
        required=True,
    )

    args = ap.parse_args()

    case = load_case(
        args.case
    )

    if args.state:
        state = load_case(
            args.state
        )

        case, placement = (
            prepare_from_state(
                case,
                state,
            )
        )

        effective_scale = 1.0

    elif args.placement:
        pobj = load_case(
            args.placement
        )

        placement = pobj[
            "assignment"
        ]

        effective_scale = (
            args.cache_scale
        )

    else:
        ap.error(
            "Pass either --state or --placement"
        )

    for name in args.baselines:
        if name not in BASELINES:
            ap.error(
                f"Unknown baseline: {name}"
            )

    out = Path(
        args.out
    )

    out.mkdir(
        parents=True,
        exist_ok=False,
    )

    rows = []

    print()
    print(
        "BASELINE                     "
        "MEAN_s      P95_s   "
        "REGISTRY   PEER   "
        "EVICT   REFUSED"
    )

    for name in args.baselines:
        result = simulate(
            case=case,
            placement=placement,
            policy=BASELINES[name],
            wan=args.wan,
            upload=args.upload,
            lan=args.lan,
            cache_scale=effective_scale,
        )

        (out / f"{name}.json").write_text(
            json.dumps(
                result,
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        row = {
            "baseline": name,
            "accepted": result[
                "accepted"
            ],
            "excluded": result[
                "excluded"
            ],
            "mean_ready_s": result[
                "mean_ready_s"
            ],
            "p95_ready_s": result[
                "p95_ready_s"
            ],
            "makespan_s": result[
                "makespan_s"
            ],
            "registry_mb": result[
                "registry_mb"
            ],
            "peer_mb": result[
                "peer_mb"
            ],
            "total_transfer_mb": result[
                "total_transfer_mb"
            ],
            "evicted_mb": result[
                "evicted_mb"
            ],
            "refused_cache_mb": result[
                "refused_cache_mb"
            ],
        }

        rows.append(
            row
        )

        print(
            f"{name:<28}"
            f"{result['mean_ready_s']:9.2f} "
            f"{result['p95_ready_s']:10.2f} "
            f"{result['registry_mb']:10.0f} "
            f"{result['peer_mb']:7.0f} "
            f"{result['evicted_mb']:7.0f} "
            f"{result['refused_cache_mb']:9.0f}"
        )

    with (
        out / "summary.csv"
    ).open(
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
        writer.writerows(
            rows
        )

    print()
    print(
        "Saved:",
        out.resolve(),
    )


if __name__ == "__main__":
    main()
