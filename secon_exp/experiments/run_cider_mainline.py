from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

from secon_exp.network.topology import EdgeTopology
from secon_exp.cider_simulator import simulate_cider


FIELDS = [
    "case",
    "num_nodes",
    "num_domains",
    "seed",
    "wan_mb_s",

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
    "avg_layer_transfer_s",

    "evicted_mb",
    "not_preserved_mb",

    "pulse_ms",
    "scout_ms",
    "keep_ms",
    "scheduler_runtime_ms",

    "scheduling_cycles",
    "pulse_selected_total",
    "keep_virtual_queue",

    "wall_runtime_s",
]


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--state-root",
        required=True,
    )

    ap.add_argument(
        "--cases",
        nargs="+",
        required=True,
    )

    # 固定数据划分，不做 seed sweep
    ap.add_argument(
        "--seed",
        type=int,
        default=1,
    )

    ap.add_argument(
        "--wan",
        type=float,
        default=10.0,
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

    # PULSE
    ap.add_argument(
        "--pulse-budget-mb",
        type=float,
        default=2048.0,
    )

    ap.add_argument(
        "--pulse-max-transfers",
        type=int,
        default=0,
    )

    # SCOUT
    ap.add_argument(
        "--scout-source-concurrency",
        type=int,
        default=0,
    )

    # KEEP
    ap.add_argument(
        "--keep-v",
        type=float,
        default=1.0,
    )

    ap.add_argument(
        "--keep-registry-budget-ratio",
        type=float,
        default=0.30,
        help=(
            "Long-term Registry traffic budget "
            "as a fraction of WAN capacity."
        ),
    )

    ap.add_argument(
        "--out",
        required=True,
    )

    args = ap.parse_args()

    state_root = Path(
        args.state_root
    )

    if (
        state_root / "states"
    ).is_dir():
        state_root = (
            state_root / "states"
        )

    out = Path(args.out)

    out.mkdir(
        parents=True,
        exist_ok=True,
    )

    raw_path = (
        out / "raw.csv"
    )

    rows = []

    for case_path in args.cases:

        case_name = (
            Path(case_path).stem
        )

        state_path = (
            state_root
            / (
                f"{case_name}"
                f"__seed{args.seed}.json"
            )
        )

        if not state_path.exists():
            raise FileNotFoundError(
                f"Missing state: "
                f"{state_path}"
            )

        state = json.loads(
            state_path.read_text(
                encoding="utf-8"
            )
        )

        case = state[
            "target_case"
        ]

        placement = state[
            "placement"
        ]

        num_nodes = int(
            state["num_nodes"]
        )

        num_domains = int(
            state["num_domains"]
        )

        topology = EdgeTopology(
            nodes=case["nodes"],
            num_domains=(
                num_domains
            ),

            wan_capacity=args.wan,

            core_capacity=(
                args.core_capacity
            ),

            domain_capacity=(
                args.domain_capacity
            ),

            access_capacity=(
                args.access_capacity
            ),

            peer_upload_capacity=(
                args.peer_upload
            ),

            registry_latency_ms=40.0,
            inter_domain_latency_ms=10.0,
            intra_domain_latency_ms=2.0,
        )

        print()
        print(
            "=" * 80
        )

        print(
            f"CIDER N={num_nodes} "
            f"domains={num_domains} "
            f"WAN={args.wan:g}"
        )

        print(
            "=" * 80,
            flush=True,
        )

        t0 = time.perf_counter()

        result = simulate_cider(
            case=case,
            placement=placement,
            topology=topology,

            pulse_budget_mb=(
                args.pulse_budget_mb
            ),

            pulse_max_transfers=(
                args.pulse_max_transfers
            ),

            scout_source_concurrency=(
                args
                .scout_source_concurrency
            ),

            keep_v=(
                args.keep_v
            ),

            keep_registry_budget_rate_mb_s=(
                args.wan
                * args.keep_registry_budget_ratio
            ),
        )

        wall_runtime = (
            time.perf_counter()
            - t0
        )

        row = {
            "case": case_name,
            "num_nodes": num_nodes,
            "num_domains": (
                num_domains
            ),
            "seed": args.seed,
            "wan_mb_s": args.wan,

            "accepted": (
                result["accepted"]
            ),
            "excluded": (
                result["excluded"]
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
            "avg_layer_transfer_s": (
                result[
                    "avg_layer_transfer_s"
                ]
            ),

            "evicted_mb": (
                result[
                    "evicted_mb"
                ]
            ),
            "not_preserved_mb": (
                result[
                    "not_preserved_mb"
                ]
            ),

            "pulse_ms": (
                result["pulse_ms"]
            ),
            "scout_ms": (
                result["scout_ms"]
            ),
            "keep_ms": (
                result["keep_ms"]
            ),
            "scheduler_runtime_ms": (
                result[
                    "scheduler_runtime_ms"
                ]
            ),

            "scheduling_cycles": (
                result[
                    "scheduling_cycles"
                ]
            ),
            "pulse_selected_total": (
                result[
                    "pulse_selected_total"
                ]
            ),
            "keep_virtual_queue": (
                result[
                    "keep_virtual_queue"
                ]
            ),

            "wall_runtime_s": (
                wall_runtime
            ),
        }

        rows.append(row)

        print(
            f"mean={row['mean_ready_s']:.2f}s "
            f"p95={row['p95_ready_s']:.2f}s "
            f"registry={row['registry_mb']:.0f}MB "
            f"peer={row['peer_mb']:.0f}MB "
            f"cross={row['cross_domain_peer_mb']:.0f}MB "
            f"offload={row['p2p_offload_pct']:.1f}% "
            f"scheduler={row['scheduler_runtime_ms']:.2f}ms",
            flush=True,
        )

    with raw_path.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=FIELDS,
        )

        writer.writeheader()
        writer.writerows(rows)

    print()
    print(
        "Saved:",
        raw_path.resolve(),
    )


if __name__ == "__main__":
    main()
