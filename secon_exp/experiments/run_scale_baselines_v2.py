from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

from secon_exp.adapters.fg_dscr_adapter import (
    FGDSCRAdapter,
)
from secon_exp.baselines import (
    REGISTRY,
    DRAGONFLY_STYLE,
    PEERSYNC_STYLE,
    METAPIPE_REACTIVE_STYLE,
    ILRSA_STYLE,
)
from secon_exp.network.topology import (
    EdgeTopology,
)
from secon_exp.simulator_v2 import (
    simulate_v2,
)


BASELINES = {
    "registry": REGISTRY,
    "dragonfly_style": (
        DRAGONFLY_STYLE
    ),
    "peersync_style": (
        PEERSYNC_STYLE
    ),
    "metapipe_reactive_style": (
        METAPIPE_REACTIVE_STYLE
    ),
    "ilrsa_style": (
        ILRSA_STYLE
    ),
}


FIELDS = [
    "case",
    "num_nodes",
    "num_domains",
    "seed",
    "wan_mb_s",
    "baseline",

    "warmup_requests",
    "target_requests",
    "accepted",
    "excluded",

    "mean_ready_s",
    "p95_ready_s",
    "makespan_s",

    "registry_mb",
    "peer_mb",

    "same_domain_peer_mb",
    "cross_domain_peer_mb",

    "p2p_offload_pct",

    "peak_link_utilization",
    "p95_link_utilization",
    "wan_busy_time_s",

    "evicted_mb",
    "refused_cache_mb",

    "algorithm_runtime_ms",
]


def append_row(
    path,
    row,
):
    exists = path.exists()

    with path.open(
        "a",
        newline="",
        encoding="utf-8",
    ) as f:
        w = csv.DictWriter(
            f,
            fieldnames=FIELDS,
        )

        if not exists:
            w.writeheader()

        w.writerow(row)
        f.flush()


def load_done(path):
    done = set()

    if not path.exists():
        return done

    with path.open(
        newline="",
        encoding="utf-8",
    ) as f:
        for r in csv.DictReader(f):
            done.add(
                (
                    r["case"],
                    int(r["seed"]),
                    float(
                        r["wan_mb_s"]
                    ),
                    r["baseline"],
                )
            )

    return done


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--cases",
        nargs="+",
        required=True,
    )

    ap.add_argument(
        "--seeds",
        nargs="+",
        type=int,
        default=[
            1, 2, 3, 4, 5
        ],
    )

    ap.add_argument(
        "--wan",
        nargs="+",
        type=float,
        default=[
            10, 30, 100
        ],
    )

    ap.add_argument(
        "--warmup-fraction",
        type=float,
        default=0.5,
    )

    ap.add_argument(
        "--core-capacity",
        type=float,
        default=1000.0,
    )

    ap.add_argument(
        "--domain-capacity",
        type=float,
        default=300.0,
    )

    ap.add_argument(
        "--access-capacity",
        type=float,
        default=200.0,
    )

    ap.add_argument(
        "--peer-upload",
        type=float,
        default=100.0,
    )

    ap.add_argument(
        "--registry-latency-ms",
        type=float,
        default=40.0,
    )

    ap.add_argument(
        "--inter-domain-latency-ms",
        type=float,
        default=10.0,
    )

    ap.add_argument(
        "--intra-domain-latency-ms",
        type=float,
        default=2.0,
    )

    ap.add_argument(
        "--baselines",
        nargs="+",
        default=list(
            BASELINES
        ),
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

    for x in args.baselines:
        if x not in BASELINES:
            ap.error(
                f"Unknown baseline: {x}"
            )

    out = Path(args.out)

    out.mkdir(
        parents=True,
        exist_ok=True,
    )

    state_dir = (
        out / "states"
    )

    state_dir.mkdir(
        exist_ok=True,
    )

    raw_path = (
        out / "raw.csv"
    )

    if (
        raw_path.exists()
        and not args.resume
    ):
        ap.error(
            "raw.csv already exists; "
            "use --resume"
        )

    done = load_done(
        raw_path
    )

    adapter = (
        FGDSCRAdapter(
            "scripts/fg_dscr.py"
        )
    )

    total = (
        len(args.cases)
        * len(args.seeds)
        * len(args.wan)
        * len(args.baselines)
    )

    step = 0

    for case_path in args.cases:
        base = json.loads(
            Path(case_path)
            .read_text(
                encoding="utf-8"
            )
        )

        num_nodes = len(
            base["nodes"]
        )

        num_domains = int(
            base.get(
                "metadata",
                {}
            ).get(
                "num_domains",
                max(
                    1,
                    round(
                        num_nodes / 5
                    ),
                ),
            )
        )

        case_name = (
            Path(case_path).stem
        )

        for seed in args.seeds:

            state_path = (
                state_dir
                / (
                    f"{case_name}"
                    f"__seed{seed}.json"
                )
            )

            if (
                args.resume
                and state_path.exists()
            ):
                state = json.loads(
                    state_path.read_text(
                        encoding="utf-8"
                    )
                )

                target_case = (
                    state[
                        "target_case"
                    ]
                )

                placement = (
                    state[
                        "placement"
                    ]
                )

                print(
                    f"reuse FG state: "
                    f"{case_name} "
                    f"seed={seed}",
                    flush=True,
                )

            else:
                warm, target = (
                    adapter.split_windows(
                        base,
                        warmup_fraction=(
                            args
                            .warmup_fraction
                        ),
                        seed=seed,
                    )
                )

                # 正式通信实验从空
                # reusable cache 开始预热。
                for node in warm[
                    "nodes"
                ]:
                    node[
                        "initial_cache"
                    ] = []

                print(
                    f"FG warmup: "
                    f"nodes={num_nodes} "
                    f"seed={seed}",
                    flush=True,
                )

                warm_result = (
                    adapter.run(
                        warm
                    )
                )

                target_case = (
                    adapter
                    .apply_cache_state(
                        target,
                        warm_result[
                            "final_cache"
                        ],
                    )
                )

                print(
                    f"FG target placement: "
                    f"nodes={num_nodes} "
                    f"seed={seed}",
                    flush=True,
                )

                target_result = (
                    adapter.run(
                        target_case
                    )
                )

                placement = (
                    target_result[
                        "assignment"
                    ]
                )

                state = {
                    "case": case_path,
                    "seed": seed,
                    "num_nodes": (
                        num_nodes
                    ),
                    "num_domains": (
                        num_domains
                    ),
                    "warmup_requests": (
                        len(
                            warm[
                                "containers"
                            ]
                        )
                    ),
                    "target_requests": (
                        len(
                            target[
                                "containers"
                            ]
                        )
                    ),
                    "warm_failed": (
                        warm_result[
                            "failed_containers"
                        ]
                    ),
                    "target_failed": (
                        target_result[
                            "failed_containers"
                        ]
                    ),
                    "placement": (
                        placement
                    ),
                    "target_case": (
                        target_case
                    ),
                }

                state_path.write_text(
                    json.dumps(
                        state,
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

            accepted_expected = sum(
                len(v)
                for v in (
                    placement.values()
                )
            )

            for wan in args.wan:

                topo = EdgeTopology(
                    nodes=(
                        target_case[
                            "nodes"
                        ]
                    ),
                    num_domains=(
                        num_domains
                    ),
                    wan_capacity=wan,
                    core_capacity=(
                        args
                        .core_capacity
                    ),
                    domain_capacity=(
                        args
                        .domain_capacity
                    ),
                    access_capacity=(
                        args
                        .access_capacity
                    ),
                    peer_upload_capacity=(
                        args
                        .peer_upload
                    ),
                    registry_latency_ms=(
                        args
                        .registry_latency_ms
                    ),
                    inter_domain_latency_ms=(
                        args
                        .inter_domain_latency_ms
                    ),
                    intra_domain_latency_ms=(
                        args
                        .intra_domain_latency_ms
                    ),
                )

                for baseline_name in (
                    args.baselines
                ):
                    step += 1

                    key = (
                        case_name,
                        seed,
                        float(wan),
                        baseline_name,
                    )

                    if key in done:
                        print(
                            f"[{step}/{total}] "
                            f"skip {key}",
                            flush=True,
                        )
                        continue

                    t0 = (
                        time.perf_counter()
                    )

                    result = simulate_v2(
                        case=target_case,
                        placement=placement,
                        policy=BASELINES[
                            baseline_name
                        ],
                        topology=topo,
                    )

                    runtime_ms = (
                        (
                            time.perf_counter()
                            - t0
                        )
                        * 1000.0
                    )

                    if (
                        result[
                            "accepted"
                        ]
                        != accepted_expected
                    ):
                        raise RuntimeError(
                            "accepted mismatch"
                        )

                    row = {
                        "case": (
                            case_name
                        ),
                        "num_nodes": (
                            num_nodes
                        ),
                        "num_domains": (
                            num_domains
                        ),
                        "seed": seed,
                        "wan_mb_s": (
                            wan
                        ),
                        "baseline": (
                            baseline_name
                        ),

                        "warmup_requests": (
                            state[
                                "warmup_requests"
                            ]
                        ),
                        "target_requests": (
                            state[
                                "target_requests"
                            ]
                        ),
                        "accepted": (
                            result[
                                "accepted"
                            ]
                        ),
                        "excluded": (
                            result[
                                "excluded"
                            ]
                        ),

                        "mean_ready_s": (
                            result[
                                "mean_ready_s"
                            ]
                        ),
                        "p95_ready_s": (
                            result[
                                "p95_ready_s"
                            ]
                        ),
                        "makespan_s": (
                            result[
                                "makespan_s"
                            ]
                        ),

                        "registry_mb": (
                            result[
                                "registry_mb"
                            ]
                        ),
                        "peer_mb": (
                            result[
                                "peer_mb"
                            ]
                        ),

                        "same_domain_peer_mb": (
                            result[
                                "same_domain_peer_mb"
                            ]
                        ),
                        "cross_domain_peer_mb": (
                            result[
                                "cross_domain_peer_mb"
                            ]
                        ),

                        "p2p_offload_pct": (
                            result[
                                "p2p_offload_pct"
                            ]
                        ),

                        "peak_link_utilization": (
                            result[
                                "peak_link_utilization"
                            ]
                        ),
                        "p95_link_utilization": (
                            result[
                                "p95_link_utilization"
                            ]
                        ),
                        "wan_busy_time_s": (
                            result[
                                "wan_busy_time_s"
                            ]
                        ),

                        "evicted_mb": (
                            result[
                                "evicted_mb"
                            ]
                        ),
                        "refused_cache_mb": (
                            result[
                                "refused_cache_mb"
                            ]
                        ),

                        "algorithm_runtime_ms": (
                            runtime_ms
                        ),
                    }

                    append_row(
                        raw_path,
                        row,
                    )

                    done.add(
                        key
                    )

                    print(
                        f"[{step}/{total}] "
                        f"N={num_nodes:<2d} "
                        f"seed={seed:<2d} "
                        f"WAN={wan:<4g} "
                        f"{baseline_name:<27} "
                        f"mean="
                        f"{result['mean_ready_s']:.2f} "
                        f"p95="
                        f"{result['p95_ready_s']:.2f} "
                        f"reg="
                        f"{result['registry_mb']:.0f} "
                        f"peer="
                        f"{result['peer_mb']:.0f} "
                        f"cross="
                        f"{result['cross_domain_peer_mb']:.0f} "
                        f"offload="
                        f"{result['p2p_offload_pct']:.1f}%",
                        flush=True,
                    )

    print()
    print(
        "Scale baseline sweep completed."
    )
    print(
        "Saved:",
        raw_path.resolve(),
    )


if __name__ == "__main__":
    main()
