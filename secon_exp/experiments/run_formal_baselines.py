from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

from secon_exp.baselines import (
    REGISTRY,
    DRAGONFLY_STYLE,
    PEERSYNC_STYLE,
    METAPIPE_REACTIVE_STYLE,
    ILRSA_STYLE,
)
from secon_exp.simulator import simulate


BASELINES = {
    "registry": REGISTRY,
    "dragonfly_style": DRAGONFLY_STYLE,
    "peersync_style": PEERSYNC_STYLE,
    "metapipe_reactive_style": METAPIPE_REACTIVE_STYLE,
    "ilrsa_style": ILRSA_STYLE,
}


FIELDS = [
    "case",
    "base_requests",
    "target_requests",
    "seed",
    "cache_scale",
    "wan_mb_s",
    "baseline",
    "accepted",
    "excluded",
    "mean_ready_s",
    "p95_ready_s",
    "makespan_s",
    "registry_mb",
    "peer_mb",
    "total_transfer_mb",
    "p2p_offload_pct",
    "evicted_mb",
    "refused_cache_mb",
    "runtime_ms",
]


def load_state_files(roots):
    states = {}

    for root in roots:
        root = Path(root)

        if (root / "states").is_dir():
            root = root / "states"

        if not root.is_dir():
            raise FileNotFoundError(
                f"State directory not found: {root}"
            )

        for path in root.glob("*.json"):
            state = json.loads(
                path.read_text(encoding="utf-8")
            )

            key = (
                Path(state["case"]).stem,
                int(state["seed"]),
                float(state["cache_scale"]),
            )

            # Allows multiple result directories;
            # identical state identities are de-duplicated.
            states[key] = (path, state)

    return states


def load_done(raw_path):
    done = set()

    if not raw_path.exists():
        return done

    with raw_path.open(
        newline="",
        encoding="utf-8",
    ) as f:
        for r in csv.DictReader(f):
            done.add(
                (
                    r["case"],
                    int(r["seed"]),
                    float(r["cache_scale"]),
                    float(r["wan_mb_s"]),
                    r["baseline"],
                )
            )

    return done


def append_row(path, row):
    exists = path.exists()

    with path.open(
        "a",
        newline="",
        encoding="utf-8",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=FIELDS,
        )

        if not exists:
            writer.writeheader()

        writer.writerow(row)
        f.flush()


def prepare_case(state):
    case_path = Path(state["case"])

    if not case_path.is_file():
        raise FileNotFoundError(
            f"Case not found: {case_path}"
        )

    case = json.loads(
        case_path.read_text(
            encoding="utf-8"
        )
    )

    base_requests = len(
        case["containers"]
    )

    target_ids = set(
        state["target_request_ids"]
    )

    case["containers"] = [
        c
        for c in case["containers"]
        if c["cid"] in target_ids
    ]

    target_requests = len(
        case["containers"]
    )

    scale = float(
        state["cache_scale"]
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

    return (
        case,
        placement,
        base_requests,
        target_requests,
    )


def main():
    ap = argparse.ArgumentParser(
        description=(
            "Formal external-baseline sweep "
            "using saved FG warm-up states."
        )
    )

    ap.add_argument(
        "--state-roots",
        nargs="+",
        required=True,
    )

    ap.add_argument(
        "--wan",
        type=float,
        nargs="+",
        default=[10, 30, 100],
    )

    ap.add_argument(
        "--upload",
        type=float,
        default=100,
    )

    ap.add_argument(
        "--lan",
        type=float,
        default=1000,
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

    ap.add_argument(
        "--resume",
        action="store_true",
    )

    args = ap.parse_args()

    for name in args.baselines:
        if name not in BASELINES:
            ap.error(
                f"Unknown baseline: {name}"
            )

    states = load_state_files(
        args.state_roots
    )

    print(
        "Unique saved states:",
        len(states),
    )

    out = Path(args.out)
    out.mkdir(
        parents=True,
        exist_ok=True,
    )

    raw_path = out / "raw.csv"

    if (
        raw_path.exists()
        and not args.resume
    ):
        ap.error(
            "raw.csv already exists; "
            "use --resume or a new --out"
        )

    done = load_done(raw_path)

    total = (
        len(states)
        * len(args.wan)
        * len(args.baselines)
    )

    completed = 0

    for state_key in sorted(states):
        state_path, state = states[
            state_key
        ]

        (
            case,
            placement,
            base_requests,
            target_requests,
        ) = prepare_case(state)

        case_name = Path(
            state["case"]
        ).stem

        accepted_expected = sum(
            len(v)
            for v in placement.values()
        )

        for wan in args.wan:
            for baseline_name in args.baselines:

                completed += 1

                key = (
                    case_name,
                    int(state["seed"]),
                    float(state["cache_scale"]),
                    float(wan),
                    baseline_name,
                )

                if key in done:
                    print(
                        f"[{completed}/{total}] "
                        f"skip "
                        f"{case_name} "
                        f"seed={state['seed']} "
                        f"cache={state['cache_scale']} "
                        f"WAN={wan:g} "
                        f"{baseline_name}",
                        flush=True,
                    )
                    continue

                start = time.perf_counter()

                r = simulate(
                    case=case,
                    placement=placement,
                    policy=BASELINES[
                        baseline_name
                    ],
                    wan=wan,
                    upload=args.upload,
                    lan=args.lan,

                    # Capacity has already been
                    # scaled above.
                    cache_scale=1.0,
                )

                elapsed_ms = (
                    time.perf_counter()
                    - start
                ) * 1000.0

                if (
                    r["accepted"]
                    != accepted_expected
                ):
                    raise AssertionError(
                        "Accepted-request mismatch"
                    )

                if r[
                    "cache_overflow_nodes"
                ]:
                    raise AssertionError(
                        "Cache overflow: "
                        + str(
                            r[
                                "cache_overflow_nodes"
                            ]
                        )
                    )

                total_transfer = (
                    r["registry_mb"]
                    + r["peer_mb"]
                )

                offload = (
                    100.0
                    * r["peer_mb"]
                    / max(
                        total_transfer,
                        1e-9,
                    )
                )

                row = {
                    "case": case_name,
                    "base_requests": (
                        base_requests
                    ),
                    "target_requests": (
                        target_requests
                    ),
                    "seed": int(
                        state["seed"]
                    ),
                    "cache_scale": float(
                        state["cache_scale"]
                    ),
                    "wan_mb_s": float(
                        wan
                    ),
                    "baseline": (
                        baseline_name
                    ),
                    "accepted": (
                        r["accepted"]
                    ),
                    "excluded": (
                        r["excluded"]
                    ),
                    "mean_ready_s": (
                        r["mean_ready_s"]
                    ),
                    "p95_ready_s": (
                        r["p95_ready_s"]
                    ),
                    "makespan_s": (
                        r["makespan_s"]
                    ),
                    "registry_mb": (
                        r["registry_mb"]
                    ),
                    "peer_mb": (
                        r["peer_mb"]
                    ),
                    "total_transfer_mb": (
                        r["total_transfer_mb"]
                    ),
                    "p2p_offload_pct": (
                        offload
                    ),
                    "evicted_mb": (
                        r["evicted_mb"]
                    ),
                    "refused_cache_mb": (
                        r[
                            "refused_cache_mb"
                        ]
                    ),
                    "runtime_ms": (
                        elapsed_ms
                    ),
                }

                append_row(
                    raw_path,
                    row,
                )

                done.add(key)

                print(
                    f"[{completed}/{total}] "
                    f"req={base_requests:<4d} "
                    f"seed={state['seed']:<2} "
                    f"cache={float(state['cache_scale']):<3g} "
                    f"WAN={wan:<3g} "
                    f"{baseline_name:<27} "
                    f"mean={r['mean_ready_s']:.2f} "
                    f"p95={r['p95_ready_s']:.2f} "
                    f"reg={r['registry_mb']:.0f} "
                    f"peer={r['peer_mb']:.0f} "
                    f"offload={offload:.1f}%",
                    flush=True,
                )

    print()
    print(
        "Baseline sweep completed."
    )
    print(
        "Saved:",
        raw_path.resolve(),
    )


if __name__ == "__main__":
    main()
