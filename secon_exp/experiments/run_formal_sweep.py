from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from pathlib import Path

from secon_exp.adapters.fg_dscr_adapter import FGDSCRAdapter
from secon_exp.policies import POLICIES
from secon_exp.simulator import simulate


FIELDS = [
    "case",
    "base_requests",
    "target_requests",
    "seed",
    "cache_scale",
    "wan_mb_s",
    "policy",
    "accepted",
    "excluded",
    "mean_ready_s",
    "p95_ready_s",
    "makespan_s",
    "registry_mb",
    "peer_mb",
    "total_transfer_mb",
    "evicted_mb",
    "refused_cache_mb",
    "max_lease_mb",
]


def file_sha256(path):
    return hashlib.sha256(
        Path(path).read_bytes()
    ).hexdigest()


def git_commit():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True,
        ).strip()
    except Exception:
        return None


def clear_initial_cache(case):
    for node in case["nodes"]:
        node["initial_cache"] = []
    return case


def write_row(path, row):
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


def load_done(path):
    done = set()

    if not path.exists():
        return done

    with path.open(
        newline="",
        encoding="utf-8",
    ) as f:
        for row in csv.DictReader(f):
            done.add(
                (
                    row["case"],
                    int(row["seed"]),
                    float(row["cache_scale"]),
                    float(row["wan_mb_s"]),
                    row["policy"],
                )
            )

    return done


def scenario_name(case_path, seed, scale):
    return (
        f"{Path(case_path).stem}"
        f"__seed{seed}"
        f"__cache{scale:g}"
    )


def main():
    ap = argparse.ArgumentParser(
        description="Formal SECON experiment sweep"
    )

    ap.add_argument(
        "--cases",
        nargs="+",
        required=True,
    )

    ap.add_argument(
        "--fg",
        default="scripts/fg_dscr.py",
    )

    ap.add_argument(
        "--seeds",
        nargs="+",
        type=int,
        default=[1, 2, 3, 4, 5],
    )

    ap.add_argument(
        "--cache-scales",
        nargs="+",
        type=float,
        default=[0.5, 1.0, 2.0],
    )

    ap.add_argument(
        "--wan",
        nargs="+",
        type=float,
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
        "--warmup-fraction",
        type=float,
        default=0.5,
    )

    ap.add_argument(
        "--policies",
        nargs="+",
        default=[
            "cloud_fifo",
            "cloud_sjf",
            "p2p_sjf",
            "c_lru",
            "l_lru",
            "l_lease",
            "d_lease",
        ],
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

    for p in args.policies:
        if p not in POLICIES:
            ap.error(
                f"Unknown policy: {p}"
            )

    out = Path(args.out)

    if (
        out.exists()
        and not args.resume
        and (out / "raw.csv").exists()
    ):
        ap.error(
            "Output already exists; "
            "use --resume or choose a new directory"
        )

    out.mkdir(
        parents=True,
        exist_ok=True,
    )

    state_dir = out / "states"
    state_dir.mkdir(
        exist_ok=True,
    )

    raw_path = out / "raw.csv"
    done = load_done(raw_path)

    adapter = FGDSCRAdapter(
        args.fg
    )

    meta = {
        "git_commit": git_commit(),
        "arguments": vars(args),
        "case_hashes": {
            p: file_sha256(p)
            for p in args.cases
        },
        "fg_hash": file_sha256(
            args.fg
        ),
        "method_parameters": {
            "relay_lease_rho": 0.5,
            "communication_debt_lambda": 1.0,
        },
        "pipeline": [
            "start warm-up from empty reusable cache",
            "split historical and target requests by seed",
            "run FG-DSCR on historical window",
            "use FG final cache as target initial cache",
            "run FG-DSCR to obtain target placement",
            "freeze placement and cache state",
            "compare communication policies",
        ],
    }

    (out / "meta.json").write_text(
        json.dumps(
            meta,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    total_scenarios = (
        len(args.cases)
        * len(args.seeds)
        * len(args.cache_scales)
    )

    scenario_index = 0

    for case_path in args.cases:
        base_case = json.loads(
            Path(case_path).read_text(
                encoding="utf-8"
            )
        )

        base_count = len(
            base_case["containers"]
        )

        case_name = Path(
            case_path
        ).stem

        for seed in args.seeds:
            for scale in args.cache_scales:

                scenario_index += 1

                sid = scenario_name(
                    case_path,
                    seed,
                    scale,
                )

                print()
                print(
                    f"[{scenario_index}/"
                    f"{total_scenarios}] "
                    f"{sid}",
                    flush=True,
                )

                warm, target = (
                    adapter.split_windows(
                        base_case,
                        warmup_fraction=(
                            args.warmup_fraction
                        ),
                        seed=seed,
                    )
                )

                warm = (
                    adapter.scale_cache_capacity(
                        warm,
                        scale,
                    )
                )

                target = (
                    adapter.scale_cache_capacity(
                        target,
                        scale,
                    )
                )

                # Formal experiments do not inherit
                # synthetic case-generator caches.
                clear_initial_cache(
                    warm
                )

                state_path = (
                    state_dir
                    / f"{sid}.json"
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

                    target_with_cache = (
                        adapter.apply_cache_state(
                            target,
                            state[
                                "warm_final_cache"
                            ],
                        )
                    )

                    placement = state[
                        "target_assignment"
                    ]

                    print(
                        "  reuse saved "
                        "warm-up/placement state",
                        flush=True,
                    )

                else:
                    print(
                        "  running historical "
                        "FG-DSCR warm-up...",
                        flush=True,
                    )

                    warm_result = (
                        adapter.run(warm)
                    )

                    target_with_cache = (
                        adapter.apply_cache_state(
                            target,
                            warm_result[
                                "final_cache"
                            ],
                        )
                    )

                    print(
                        "  running target "
                        "FG-DSCR placement...",
                        flush=True,
                    )

                    target_result = (
                        adapter.run(
                            target_with_cache
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
                        "cache_scale": scale,

                        "warmup_request_ids": [
                            c["cid"]
                            for c in warm[
                                "containers"
                            ]
                        ],

                        "target_request_ids": [
                            c["cid"]
                            for c in target[
                                "containers"
                            ]
                        ],

                        "warm_final_cache": (
                            warm_result[
                                "final_cache"
                            ]
                        ),

                        "warm_failed": (
                            warm_result[
                                "failed_containers"
                            ]
                        ),

                        "target_assignment": (
                            placement
                        ),

                        "target_failed": (
                            target_result[
                                "failed_containers"
                            ]
                        ),
                    }

                    state_path.write_text(
                        json.dumps(
                            state,
                            indent=2,
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )

                target_count = len(
                    target[
                        "containers"
                    ]
                )

                accepted = sum(
                    len(x)
                    for x in placement.values()
                )

                print(
                    f"  target requests="
                    f"{target_count}, "
                    f"accepted={accepted}",
                    flush=True,
                )

                if accepted <= 0:
                    raise RuntimeError(
                        f"No accepted target "
                        f"requests in {sid}"
                    )

                for wan in args.wan:
                    for policy_name in (
                        args.policies
                    ):

                        key = (
                            case_name,
                            seed,
                            float(scale),
                            float(wan),
                            policy_name,
                        )

                        if key in done:
                            print(
                                f"  skip "
                                f"WAN={wan:g} "
                                f"{policy_name}",
                                flush=True,
                            )
                            continue

                        result = simulate(
                            case=(
                                target_with_cache
                            ),
                            placement=placement,
                            policy=POLICIES[
                                policy_name
                            ],
                            wan=wan,
                            upload=args.upload,
                            lan=args.lan,

                            # repo_capacity has
                            # already been scaled.
                            cache_scale=1.0,
                        )

                        if (
                            result["accepted"]
                            != accepted
                        ):
                            raise AssertionError(
                                "Policy changed "
                                "accepted requests"
                            )

                        if result[
                            "cache_overflow_nodes"
                        ]:
                            raise AssertionError(
                                "Cache overflow: "
                                + str(
                                    result[
                                        "cache_overflow_nodes"
                                    ]
                                )
                            )

                        row = {
                            "case": case_name,
                            "base_requests": (
                                base_count
                            ),
                            "target_requests": (
                                target_count
                            ),
                            "seed": seed,
                            "cache_scale": (
                                scale
                            ),
                            "wan_mb_s": wan,
                            "policy": (
                                policy_name
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
                            "total_transfer_mb": (
                                result[
                                    "total_transfer_mb"
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
                            "max_lease_mb": (
                                result[
                                    "max_lease_mb"
                                ]
                            ),
                        }

                        write_row(
                            raw_path,
                            row,
                        )

                        done.add(key)

                        print(
                            f"  WAN={wan:<3g} "
                            f"{policy_name:<11} "
                            f"mean="
                            f"{result['mean_ready_s']:.2f} "
                            f"p95="
                            f"{result['p95_ready_s']:.2f} "
                            f"registry="
                            f"{result['registry_mb']:.0f}",
                            flush=True,
                        )

    print()
    print(
        "Formal sweep completed."
    )
    print(
        "Raw results:",
        raw_path.resolve(),
    )


if __name__ == "__main__":
    main()
