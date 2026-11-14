from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from pathlib import Path

from secon_exp.policies import (
    CLOUD_FIFO,
    CLOUD_SJF,
    P2P_SJF,
    C_LRU,
)
from secon_exp.simulator import simulate


def load_module(path: str):
    p = Path(path)
    name = "_secon_pilot_reference"

    spec = importlib.util.spec_from_file_location(
        name,
        str(p),
    )

    if spec is None or spec.loader is None:
        raise ImportError(p)

    mod = importlib.util.module_from_spec(
        spec
    )

    sys.modules[name] = mod
    spec.loader.exec_module(mod)

    return mod


def assert_close(a, b, label):
    if not math.isclose(
        float(a),
        float(b),
        rel_tol=1e-10,
        abs_tol=1e-8,
    ):
        raise AssertionError(
            f"{label}: {a} != {b}"
        )


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--pilot",
        default="scripts/secon_comm_pilot_v1.py",
    )
    ap.add_argument("--case", required=True)
    ap.add_argument(
        "--placement",
        required=True,
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

    args = ap.parse_args()

    pilot = load_module(args.pilot)

    case = json.loads(
        Path(args.case).read_text(
            encoding="utf-8"
        )
    )

    result = json.loads(
        Path(args.placement).read_text(
            encoding="utf-8"
        )
    )

    placement = result["assignment"]

    checks = [
        (
            "cloud_fifo",
            CLOUD_FIFO,
        ),
        (
            "cloud_sjf",
            CLOUD_SJF,
        ),
        (
            "p2p_sjf",
            P2P_SJF,
        ),
        (
            "p2p_coalesce",
            C_LRU,
        ),
    ]

    print(
        "policy             mean_diff"
        "        p95_diff"
        "       registry_diff"
        " peer_diff"
    )

    for old_name, spec in checks:
        old = pilot.simulate(
            case,
            placement,
            old_name,
            args.wan,
            args.upload,
            args.lan,
        )

        new = simulate(
            case,
            placement,
            spec,
            args.wan,
            args.upload,
            args.lan,
            cache_scale=None,
        )

        for key in (
            "mean_ready_s",
            "p95_ready_s",
            "makespan_s",
            "registry_mb",
            "peer_mb",
        ):
            assert_close(
                old[key],
                new[key],
                f"{old_name}.{key}",
            )

        if old["ready_s"] != new["ready_s"]:
            raise AssertionError(
                f"{old_name}.ready_s differs"
            )

        if old["events"] != new["events"]:
            raise AssertionError(
                f"{old_name}.events differs"
            )

        print(
            f"{old_name:<18}"
            f" {new['mean_ready_s']-old['mean_ready_s']:>12.3g}"
            f" {new['p95_ready_s']-old['p95_ready_s']:>15.3g}"
            f" {new['registry_mb']-old['registry_mb']:>19.3g}"
            f" {new['peer_mb']-old['peer_mb']:>9.3g}"
        )

    print(
        "\nPASS: formal simulator exactly matches "
        "the validated no-eviction pilot for "
        "Cloud-FIFO, Cloud-SJF, P2P-SJF and "
        "P2P-Coalesce."
    )


if __name__ == "__main__":
    main()
