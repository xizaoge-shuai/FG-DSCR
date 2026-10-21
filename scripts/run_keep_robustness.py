from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from secon_exp.adapters.fg_dscr_adapter import FGDSCRAdapter


CASE_DEFAULT = (
    "cases/cider_scale_dense_arrival_corrected/"
    "cider_nodes40_domains8_req800_weak.json"
)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_csv_one(path):
    with path.open(encoding="utf-8", newline="") as f:
        return next(csv.DictReader(f))


def prepare_state(case, name, root, adapter):
    """独立生成 warmup cache + target placement。"""

    case_path = root / "cases" / (name + ".json")
    save_json(case_path, case)

    warm, target = adapter.split_windows(
        case,
        warmup_fraction=0.5,
        seed=1,
    )

    for node in warm["nodes"]:
        node["initial_cache"] = []

    print(
        f"[STATE] {name} "
        f"warmup={len(warm['containers'])} "
        f"target={len(target['containers'])}",
        flush=True,
    )

    warm_result = adapter.run(warm)

    target_case = adapter.apply_cache_state(
        target,
        warm_result["final_cache"],
    )

    target_result = adapter.run(target_case)
    placement = target_result["assignment"]

    accepted = sum(len(v) for v in placement.values())

    num_nodes = len(case["nodes"])
    num_domains = int(
        case.get("metadata", {}).get(
            "num_domains",
            max(1, round(num_nodes / 5)),
        )
    )

    state = {
        "case": str(case_path.resolve()),
        "seed": 1,
        "num_nodes": num_nodes,
        "num_domains": num_domains,
        "warmup_requests": len(warm["containers"]),
        "target_requests": len(target["containers"]),
        "warm_failed": warm_result["failed_containers"],
        "target_failed": target_result["failed_containers"],
        "placement": placement,
        "target_case": target_case,
    }

    state_path = (
        root / "states" / (name + "__seed1.json")
    )

    save_json(state_path, state)

    print(
        f"[STATE READY] {name} "
        f"accepted={accepted} "
        f"excluded={len(target['containers']) - accepted}",
        flush=True,
    )

    return {
        "name": name,
        "case": str(case_path.resolve()),
        "state": str(state_path.resolve()),
        "target_requests": len(target["containers"]),
        "accepted": accepted,
    }


def make_scenarios(base, args):
    scenarios = []

    if args.phase in ("capacity", "both"):
        for scale in args.scales:
            case = copy.deepcopy(base)

            for node in case["nodes"]:
                old = float(node["repo_capacity_mb"])
                node["repo_capacity_mb"] = max(
                    1, int(round(old * scale))
                )

            label = ("cap_%gx" % scale).replace(".", "p")

            scenarios.append((
                "capacity",
                label,
                case,
            ))

    if args.phase in ("windows", "both"):
        containers = list(base["containers"])

        if not all(
            "arrival_time_s" in c
            for c in containers
        ):
            raise RuntimeError(
                "Window study requires arrival_time_s "
                "on every request."
            )

        containers.sort(
            key=lambda c: (
                float(c["arrival_time_s"]),
                str(c["cid"]),
            )
        )

        count = args.windows

        if len(containers) < count * 20:
            raise RuntimeError(
                "Too few requests for the requested windows."
            )

        print(
            f"[WINDOWS] requests={len(containers)} "
            f"windows={count}",
            flush=True,
        )

        for i in range(count):
            lo = i * len(containers) // count
            hi = (i + 1) * len(containers) // count

            case = copy.deepcopy(base)
            case["containers"] = copy.deepcopy(
                containers[lo:hi]
            )

            label = f"window_{i + 1:02d}"

            print(
                f"[WINDOW] {label} "
                f"requests={hi-lo} "
                f"warmup≈{(hi-lo)//2} "
                f"target≈{(hi-lo)-(hi-lo)//2}",
                flush=True,
            )

            scenarios.append((
                "windows",
                label,
                case,
            ))

    return scenarios


def run_policy(root, scenario, policy):
    """执行一个完全独立的 Python 仿真进程。"""

    name = scenario["name"]
    out = root / "runs" / name / policy

    out.mkdir(parents=True, exist_ok=True)

    raw = out / "raw.csv"
    diag = out / "review_diag.json"

    if raw.is_file() and diag.is_file():
        print(
            f"[RESUME] {name} / {policy}",
            flush=True,
        )
    else:
        ablation = (
            "no_keep" if policy == "lru"
            else "full"
        )

        cache_policy = (
            "warmup_lfu"
            if policy == "warmup_frequency"
            else "keep"
        )

        cmd = [
            sys.executable,
            "-u",
            "-m",
            "scripts.review_mechanisms",

            "--scout-mode", "independent",
            "--cache-policy", cache_policy,
            "--diagnostics", str(diag),

            "--",

            "--state-root", str(root / "states"),
            "--cases", scenario["case"],
            "--seed", "1",

            "--wan", "250",
            "--core-capacity", "1000",
            "--domain-capacity", "300",
            "--access-capacity", "200",
            "--peer-upload", "12.5",

            "--registry-latency-ms", "40",
            "--inter-domain-latency-ms", "10",
            "--intra-domain-latency-ms", "2",

            "--pulse-budget-mb", "2048",
            "--pulse-max-transfers", "0",
            "--pulse-refill-ratio", "0.5",

            "--scout-alpha", "1",
            "--scout-beta", "4",
            "--scout-gamma", "0.5",
            "--scout-source-concurrency", "0",

            "--keep-v", "3e6",
            "--keep-upper-budget-mb-s", "150",

            "--ablation", ablation,
            "--out", str(out),
        ]

        print(
            f"[RUN] {name} / {policy}",
            flush=True,
        )

        with (out / "run.log").open("w") as log:
            completed = subprocess.run(
                cmd,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )

        if completed.returncode != 0:
            print(
                f"[FAILED] {name} / {policy}",
                flush=True,
            )
            print(
                (out / "run.log")
                .read_text(errors="replace")[-5000:],
                flush=True,
            )
            raise RuntimeError(
                "Experiment failed; see run.log"
            )

    r = read_csv_one(raw)
    d = json.loads(diag.read_text())

    upper = (
        float(r["registry_mb"])
        + float(r["cross_domain_peer_mb"])
    )

    timing = d["timings_ms"]["keep"]

    row = {
        "scenario": name,
        "policy": policy,
        "mean_s": float(r["mean_ready_s"]),
        "p95_s": float(r["p95_ready_s"]),
        "upper_mb": upper,
        "registry_mb": float(r["registry_mb"]),
        "same_mb": float(r["same_domain_peer_mb"]),
        "cross_mb": float(r["cross_domain_peer_mb"]),
        "accepted": int(r["accepted"]),
        "excluded": int(r["excluded"]),
        "keep_total_ms": float(r["keep_ms"]),
        "keep_p95_ms": float(timing["p95_ms"]),
        "keep_reuse_pct": float(
            r.get("keep_retained_reuse_pct", 0)
        ),
    }

    print(
        f"[DONE] {name:<15} {policy:<18}"
        f"Mean={row['mean_s']:.3f} "
        f"P95={row['p95_s']:.3f} "
        f"Upper={upper:.0f}",
        flush=True,
    )

    return row


def save_summary(root, rows):
    if not rows:
        return

    dest = root / "summary.csv"

    with dest.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=list(rows[0])
        )
        writer.writeheader()
        writer.writerows(rows)


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--source-case",
        default=CASE_DEFAULT,
    )
    ap.add_argument(
        "--out",
        required=True,
    )
    ap.add_argument(
        "--phase",
        choices=["capacity", "windows", "both"],
        default="both",
    )
    ap.add_argument(
        "--windows",
        type=int,
        default=4,
    )
    ap.add_argument(
        "--scales",
        type=float,
        nargs="+",
        default=[0.5, 1.0, 2.0],
    )

    args = ap.parse_args()

    if args.windows < 1:
        ap.error("--windows must be positive")

    if any(x <= 0 for x in args.scales):
        ap.error("--scales must be positive")

    source = Path(args.source_case).resolve()
    root = Path(args.out).resolve()

    root.mkdir(parents=True, exist_ok=True)

    if not source.is_file():
        raise FileNotFoundError(source)

    if not Path("scripts/review_mechanisms.py").is_file():
        raise FileNotFoundError(
            "Run the previous review_mechanisms.py "
            "creation commands first."
        )

    base = json.loads(
        source.read_text(encoding="utf-8")
    )

    print(
        "[SOURCE]",
        source,
        "SHA256=",
        sha256(source),
        flush=True,
    )

    adapter = FGDSCRAdapter("scripts/fg_dscr.py")

    scenarios = make_scenarios(base, args)

    index_path = root / "scenario_index.json"
    if index_path.exists():
        index = json.loads(index_path.read_text())
    else:
        index = []

    existing_names = {s["name"] for s in index}

    for kind, label, case in scenarios:
        name = f"{source.stem}__{label}"

        if name in existing_names:
            print("[STATE RESUME]", name, flush=True)
            continue

        info = prepare_state(
            case,
            name,
            root,
            adapter,
        )
        info["group"] = kind
        info["label"] = label
        index.append(info)

        save_json(index_path, index)

    rows = []

    for scenario in index:
        if (
            args.phase != "both"
            and scenario["group"] != args.phase
        ):
            continue

        for policy in [
            "keep",
            "lru",
            "warmup_frequency",
        ]:
            row = run_policy(
                root,
                scenario,
                policy,
            )

            row["group"] = scenario["group"]
            row["label"] = scenario["label"]

            rows.append(row)
            save_summary(root, rows)

    (root / "FINISHED.txt").write_text(
        "COMPLETE\n",
        encoding="utf-8",
    )

    print(
        "\nALL FINISHED:",
        root,
        flush=True,
    )


if __name__ == "__main__":
    main()
