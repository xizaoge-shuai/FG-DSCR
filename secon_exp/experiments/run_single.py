from __future__ import annotations

import argparse
import json
from pathlib import Path

from secon_exp.adapters.fg_dscr_adapter import FGDSCRAdapter
from secon_exp.policies import POLICIES
from secon_exp.simulator import simulate


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument("--case", required=True)
    ap.add_argument(
        "--fg",
        default="scripts/fg_dscr.py",
    )
    ap.add_argument(
        "--placement",
        help="Existing result JSON containing assignment",
    )
    ap.add_argument(
        "--policy",
        choices=sorted(POLICIES),
        default="d_lease",
    )
    ap.add_argument(
        "--wan",
        type=float,
        default=30.0,
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
    ap.add_argument("--out")

    args = ap.parse_args()

    case = json.loads(
        Path(args.case).read_text(
            encoding="utf-8"
        )
    )

    if args.placement:
        existing = json.loads(
            Path(args.placement).read_text(
                encoding="utf-8"
            )
        )
        placement = existing["assignment"]
    else:
        adapter = FGDSCRAdapter(args.fg)
        placement = adapter.run(case)[
            "assignment"
        ]

    result = simulate(
        case=case,
        placement=placement,
        policy=POLICIES[args.policy],
        wan=args.wan,
        upload=args.upload,
        lan=args.lan,
        cache_scale=args.cache_scale,
    )

    text = json.dumps(
        result,
        indent=2,
        ensure_ascii=False,
    )

    if args.out:
        Path(args.out).write_text(
            text,
            encoding="utf-8",
        )

    print(
        json.dumps(
            {
                "policy": result["policy"],
                "accepted": result["accepted"],
                "excluded": result["excluded"],
                "mean_ready_s": round(
                    result["mean_ready_s"],
                    6,
                ),
                "p95_ready_s": round(
                    result["p95_ready_s"],
                    6,
                ),
                "makespan_s": round(
                    result["makespan_s"],
                    6,
                ),
                "registry_mb": round(
                    result["registry_mb"],
                    6,
                ),
                "peer_mb": round(
                    result["peer_mb"],
                    6,
                ),
                "evicted_mb": round(
                    result["evicted_mb"],
                    6,
                ),
                "refused_cache_mb": round(
                    result[
                        "refused_cache_mb"
                    ],
                    6,
                ),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
