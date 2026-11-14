import argparse
import atexit
import json
import runpy
import statistics
import sys
import time
from pathlib import Path

from secon_exp.cider.scout import ScoutOptimizer
from secon_exp.cider.keep import KeepOptimizer
from secon_exp.cider.pulse import PulseScheduler

parser = argparse.ArgumentParser()
parser.add_argument("--diagnostics", required=True)
parser.add_argument("runner", nargs=argparse.REMAINDER)

args = parser.parse_args()
rest = args.runner

if rest and rest[0] == "--":
    rest = rest[1:]

if not rest or "--out" not in rest:
    parser.error("Missing underlying CIDER arguments or --out")

times = {
    "scout": [],
    "keep": [],
    "pulse": [],
}

def timed(name, original):
    def wrapper(self, *a, **kw):
        start = time.perf_counter()
        try:
            return original(self, *a, **kw)
        finally:
            times[name].append(
                (time.perf_counter() - start) * 1000.0
            )
    return wrapper

ScoutOptimizer.choose = timed(
    "scout", ScoutOptimizer.choose
)

KeepOptimizer.choose_retention = timed(
    "keep", KeepOptimizer.choose_retention
)

PulseScheduler.select = timed(
    "pulse", PulseScheduler.select
)

def describe(values):
    if not values:
        return {
            "calls": 0,
            "mean_ms": 0.0,
            "median_ms": 0.0,
            "p95_ms": 0.0,
            "max_ms": 0.0,
            "total_ms": 0.0,
        }

    ordered = sorted(values)
    pos = 0.95 * (len(ordered) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ordered))

    p95 = (
        ordered[lo]
        + (ordered[hi] - ordered[lo]) * (pos - lo)
    )

    return {
        "calls": len(values),
        "mean_ms": statistics.mean(values),
        "median_ms": statistics.median(values),
        "p95_ms": p95,
        "max_ms": max(values),
        "total_ms": sum(values),
    }

def save():
    path = Path(args.diagnostics)
    path.parent.mkdir(parents=True, exist_ok=True)

    result = {
        "timings_ms": {
            k: describe(v) for k, v in times.items()
        },
        "samples_ms": times,
    }

    path.write_text(
        json.dumps(result, indent=2),
        encoding="utf-8",
    )

    print("PROFILE SAVED:", path, flush=True)

atexit.register(save)

sys.argv = [
    "run_cider_mainline.py",
    *rest,
]

runpy.run_module(
    "secon_exp.experiments.run_cider_mainline",
    run_name="__main__",
)
