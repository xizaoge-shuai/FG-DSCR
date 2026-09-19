import argparse
import copy
import csv
import hashlib
import importlib.util
import json
import math
import random
import statistics
import sys
from datetime import datetime
from pathlib import Path


def relabel(case, placement, mapping):
    names = [n["eid"] for n in case["nodes"]]
    if set(mapping) != set(names) or sorted(mapping.values()) != sorted(names):
        raise ValueError("Node mapping must be a bijection")
    changed = copy.deepcopy(case)
    for node in changed["nodes"]:
        node["eid"] = mapping[node["eid"]]
    return changed, {mapping[n]: list(ids) for n, ids in placement.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="results/secon_comm_pilot_20260919_144134")
    ap.add_argument("--module", default="scripts/secon_comm_pilot_v1.py")
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--out")
    args = ap.parse_args()
    base = Path(args.base)
    meta = json.loads((base / "meta.json").read_text())
    case_path = Path(meta["arguments"]["case"])
    module_path = Path(args.module)

    for path in (case_path, module_path):
        matches = [v for k, v in meta["hashes"].items()
                   if Path(k).resolve() == path.resolve()]
        if not matches or hashlib.sha256(path.read_bytes()).hexdigest() != matches[0]:
            raise ValueError("Input changed or hash missing: " + str(path))

    case = json.loads(case_path.read_text())
    placement = json.loads((base / "placement.json").read_text())["assignment"]
    names = sorted(n["eid"] for n in case["nodes"])
    if len(names) < 2 or args.trials < 1:
        raise ValueError("Need at least two nodes and one trial")

    count = min(args.trials, math.factorial(len(names)) - 1)
    rng = random.Random(2027)
    permutations = [dict(zip(names, names))]
    seen = {tuple(names)}
    while len(permutations) <= count:
        shuffled = list(names)
        rng.shuffle(shuffled)
        if tuple(shuffled) not in seen:
            seen.add(tuple(shuffled))
            permutations.append(dict(zip(names, shuffled)))

    spec = importlib.util.spec_from_file_location("_comm_order_check", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    out = Path(args.out or ("results/secon_node_order_" +
                           datetime.now().strftime("%Y%m%d_%H%M%S_%f")))
    out.mkdir(parents=True, exist_ok=False)
    network = meta["arguments"]
    policies = ("cloud_fifo", "p2p_coalesce", "p2p_priority")
    rows, results = [], {}

    for trial, mapping in enumerate(permutations):
        changed, moved = relabel(case, placement, mapping)
        restored, back = relabel(changed, moved, {v: k for k, v in mapping.items()})
        assert restored == case and back == placement

        for wan in network["wan"]:
            for policy in policies:
                r = module.simulate(changed, moved, policy, wan,
                                    network["upload"], network["lan"])
                row = dict(trial=trial, wan=wan, policy=policy,
                           mean=r["mean_ready_s"], p95=r["p95_ready_s"],
                           registry=r["registry_mb"], peer=r["peer_mb"],
                           accepted=r["accepted"], excluded=r["excluded"])
                rows.append(row)
                results[trial, wan, policy] = row

        print(f"Completed {trial}/{count}", flush=True)

    with (out / "raw.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    provenance = dict(
        base=str(base), trials=count, shuffle_seed=2027,
        permutations=permutations, assumptions=meta["assumptions"],
        note="Same physical case and placement; node labels only. Not independent workloads.",
        hashes={str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (case_path, module_path, Path(__file__),
                          base / "placement.json", base / "meta.json")}
    )
    (out / "meta.json").write_text(json.dumps(provenance, indent=2))

    summary = []
    for wan in network["wan"]:
        reference = results[0, wan, "cloud_fifo"]
        cloud_error = max(
            abs(results[t, wan, "cloud_fifo"]["mean"] - reference["mean"])
            for t in range(1, count + 1)
        )
        assert cloud_error <= 1e-7 * max(1.0, reference["mean"]), "Cloud control changed"
        print(f"\nWAN={wan:g}; cloud control max difference={cloud_error:.3g}s")

        for metric in ("mean", "p95"):
            gains = []
            for t in range(count + 1):
                baseline = results[t, wan, "p2p_coalesce"]
                candidate = results[t, wan, "p2p_priority"]
                assert (baseline["accepted"], baseline["excluded"]) == (
                    candidate["accepted"], candidate["excluded"])
                assert math.isclose(
                    baseline["registry"] + baseline["peer"],
                    candidate["registry"] + candidate["peer"], abs_tol=1e-7
                )
                if baseline[metric] <= 0:
                    raise ValueError("Cannot compute relative reduction from zero")
                gains.append(
                    100 * (baseline[metric] - candidate[metric]) / baseline[metric]
                )

            values = gains[1:]
            record = dict(
                wan=wan, metric=metric, original_gain_pct=gains[0],
                min_gain_pct=min(values),
                median_gain_pct=statistics.median(values),
                max_gain_pct=max(values),
                wins=sum(v > 1e-8 for v in values), trials=count
            )
            summary.append(record)
            print(
                f"  {metric}: original={gains[0]:.2f}%; shuffled min/median/max="
                f"{min(values):.2f}%/{statistics.median(values):.2f}%/{max(values):.2f}%; "
                f"improved={record['wins']}/{count}"
            )

    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print("\nSaved:", out.resolve())
    print("Positive gain = priority faster. Same workload; no significance claim.")


if __name__ == "__main__":
    main()
