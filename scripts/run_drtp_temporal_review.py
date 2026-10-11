from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import random
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from scripts.add_drtp_arrivals import sample_arrivals


REPO = Path(__file__).resolve().parent.parent

TRACE = REPO / "data/stats/drtp_stats/minute_arrivals.csv"

BASE_POINTER = (
    REPO / "results/LATEST_KEEP_CAPACITY_CORRECTED"
)

POLICIES = [
    ("original_keep", "audit", "keep", "full"),
    ("lru", "independent", "keep", "no_keep"),
    ("warmup_frequency", "independent", "warmup_lfu", "full"),
]


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_files():
    base = Path(
        BASE_POINTER.read_text().strip()
    ).resolve()

    manifest = load(base / "manifest.json")
    state_path = Path(
        manifest["source_state"]
    ).resolve()

    state = load(state_path)

    case_path = Path(state["case"])

    if not case_path.is_absolute():
        case_path = REPO / case_path

    case_path = case_path.resolve()

    if not state_path.is_file() or not case_path.is_file():
        raise RuntimeError("Source state/case missing")

    return state, load(case_path), state_path, case_path


def select_days():
    daily = defaultdict(lambda: [0] * 1440)

    with TRACE.open(
        encoding="utf-8-sig",
        newline="",
    ) as f:
        for row in csv.DictReader(f):
            dt = datetime.strptime(
                row["minute"],
                "%Y-%m-%d %H:%M",
            )
            n = int(row["requests"])

            if n < 0:
                raise ValueError("Negative request count")

            daily[str(dt.date())][
                dt.hour * 60 + dt.minute
            ] += n

    days = sorted(
        d for d, weights in daily.items()
        if sum(weights) > 0
    )

    if len(days) < 4:
        raise RuntimeError(
            "Need at least four nonempty DRTP days"
        )

    selected = [
        days[round(i * (len(days) - 1) / 3)]
        for i in range(4)
    ]

    if len(set(selected)) != 4:
        raise RuntimeError("Selected days overlap")

    return daily, selected


def build_inputs(root):
    state, case, state_path, case_path = source_files()
    daily, days = select_days()

    containers = case["containers"]

    if not all(
        "arrival_time_s" in c for c in containers
    ):
        raise RuntimeError(
            "Source case must have chronological arrivals"
        )

    ordered = sorted(
        containers,
        key=lambda c: (
            float(c["arrival_time_s"]),
            str(c["cid"]),
        ),
    )

    cut = round(len(ordered) * 0.5)

    warm = ordered[:cut]
    target = ordered[cut:]

    warm_ids = {str(c["cid"]) for c in warm}
    target_ids = {str(c["cid"]) for c in target}

    state_target_ids = {
        str(c["cid"])
        for c in state["target_case"]["containers"]
    }

    if target_ids != state_target_ids:
        raise RuntimeError(
            "Saved state does not match chronological target"
        )

    if warm_ids & target_ids:
        raise RuntimeError("Warmup/test overlap")

    if (
        int(state["target_requests"]) != len(target)
        or len(target) != 400
    ):
        raise RuntimeError(
            "Expected original 400-request target"
        )

    # 保持原有请求顺序，只改变到达间隔。
    target_order = sorted(
        target,
        key=lambda c: (
            float(c["arrival_time_s"]),
            str(c["cid"]),
        ),
    )

    warm_end = max(
        float(c["arrival_time_s"])
        for c in warm
    )

    # 使用正式实验真实的 target 时间跨度，
    # 而不是错误地将测试 400 请求铺满 240 秒。
    source_times = [
        float(c["arrival_time_s"])
        for c in state["target_case"]["containers"]
    ]

    original_span = max(source_times) - min(source_times)

    if original_span <= 0:
        raise RuntimeError("Invalid original arrival span")

    print(
        f"[ORIGINAL TARGET] "
        f"requests={len(source_times)} "
        f"span={original_span:.6f}s "
        f"rate={len(source_times)/original_span:.3f}req/s",
        flush=True,
    )

    # 原始时序控制组：验证复制 state 和 case 后，
    # 完全不改到达时间能否恢复正式实验指标。
    control_name = "original_target_control"
    control_case_path = (
        root / "cases" / f"{control_name}.json"
    ).resolve()
    control_state_path = (
        root / "states" / f"{control_name}__seed1.json"
    ).resolve()

    control_state = copy.deepcopy(state)
    control_state["case"] = str(control_case_path)

    save(control_case_path, copy.deepcopy(case))
    save(control_state_path, control_state)

    items = [{
        "day": "original_control",
        "name": control_name,
        "requests_in_source_day": -1,
        "target_requests": len(target),
        "arrival_seed": -1,
        "case": str(control_case_path),
        "case_sha256": sha(control_case_path),
        "state_sha256": sha(control_state_path),
    }]

    for idx, day in enumerate(days):
        weights = daily[day]
        arrival_seed = 2027 + idx

        sampled = sample_arrivals(
            n=len(target_order),
            weights=weights,
            horizon_s=original_span,
            seed=arrival_seed,
        )

        sampled_span = sampled[-1] - sampled[0]

        if sampled_span <= 0:
            raise RuntimeError("Degenerate sampled arrivals")

        # 首次到达为 0，末次到达与正式实验一致。
        # 固定总请求数和观察时长，只比较时间形状。
        arrivals = [
            (t - sampled[0]) * original_span / sampled_span
            for t in sampled
        ]

        arrival_map = {
            str(c["cid"]): float(a)
            for c, a in zip(target_order, arrivals)
        }

        new_case = copy.deepcopy(case)

        # 保证原来 400 个 warmup 请求完全不变。
        # 测试请求全部位于 warmup 之后。
        for c in new_case["containers"]:
            cid = str(c["cid"])

            if cid in arrival_map:
                c["arrival_time_s"] = (
                    warm_end + 1.0 + arrival_map[cid]
                )

        new_state = copy.deepcopy(state)

        # 仿真器中的 target 窗口从 t=0 开始。
        for c in new_state["target_case"]["containers"]:
            c["arrival_time_s"] = arrival_map[str(c["cid"])]

        name = f"drtp_day_{idx+1:02d}"

        case_out = (
            root / "cases" / f"{name}.json"
        ).resolve()

        state_out = (
            root / "states" / f"{name}__seed1.json"
        ).resolve()

        new_case.setdefault("meta", {}).update({
            "experiment": "DRTP temporal robustness",
            "arrival_source": str(TRACE),
            "arrival_day": day,
            "arrival_seed": arrival_seed,
            "arrival_horizon_s": original_span,
            "arrival_semantics": (
                "400 synthetic deployment arrivals sampled "
                "from this DRTP day's empirical minute intensity"
            ),
            "warmup_unchanged": True,
            "fixed_offline_placement": True,
        })

        new_state["case"] = str(case_out)

        save(case_out, new_case)
        save(state_out, new_state)

        # 验证输入时间排序和层、节点、缓存不变。
        check = sorted(
            new_case["containers"],
            key=lambda c: (
                float(c["arrival_time_s"]),
                str(c["cid"]),
            ),
        )

        if {
            str(c["cid"]) for c in check[:cut]
        } != warm_ids:
            raise RuntimeError(
                "Warmup identity changed"
            )

        if new_state["placement"] != state["placement"]:
            raise RuntimeError("Placement changed")

        if (
            new_state["target_case"]["nodes"]
            != state["target_case"]["nodes"]
        ):
            raise RuntimeError("Cache/node state changed")

        items.append({
            "day": day,
            "name": name,
            "requests_in_source_day": sum(weights),
            "target_requests": len(target),
            "arrival_seed": arrival_seed,
            "case": str(case_out),
            "case_sha256": sha(case_out),
            "state_sha256": sha(state_out),
        })

    manifest = {
        "experiment": "DRTP temporal arrival robustness",
        "source_state": str(state_path),
        "source_state_sha256": sha(state_path),
        "source_case": str(case_path),
        "source_case_sha256": sha(case_path),
        "trace": str(TRACE),
        "trace_sha256": sha(TRACE),
        "original_target_duration_s": original_span,
        "limitations": (
            "Same request identities, layers, warmup cache and "
            "offline placement. Only target arrival intensity "
            "changes. Not an independent source dataset and "
            "not an exact trace replay."
        ),
        "windows": items,
    }

    save(root / "manifest.json", manifest)

    return items


def run_one(root, item, method, scout, cache, ablation):
    out = root / "runs" / item["name"] / method
    out.mkdir(parents=True, exist_ok=True)

    raw = out / "raw.csv"
    diag = out / "diagnostics.json"

    # 每组可断点续跑。
    if not (raw.exists() and diag.exists()):
        cmd = [
            sys.executable, "-u", "-m",
            "scripts.review_mechanisms",

            "--scout-mode", scout,
            "--cache-policy", cache,
            "--diagnostics", str(diag),
            "--",

            "--state-root", str(root / "states"),
            "--cases", item["case"],
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

        with (
            out / "run.log"
        ).open("w", encoding="utf-8") as log:
            p = subprocess.run(
                cmd,
                cwd=REPO,
                stdout=log,
                stderr=subprocess.STDOUT,
            )

        if p.returncode != 0:
            print(
                (out / "run.log")
                .read_text(errors="replace")[-5000:],
                flush=True,
            )
            raise RuntimeError(
                f"Failed: {item['name']} {method}"
            )

    with raw.open(encoding="utf-8") as f:
        r = next(csv.DictReader(f))

    d = load(diag)

    if int(r["accepted"]) != 400:
        raise RuntimeError("Accepted count mismatch")

    return {
        "day": item["day"],
        "window": item["name"],
        "policy": method,
        "mean_s": float(r["mean_ready_s"]),
        "p95_s": float(r["p95_ready_s"]),
        "upper_mb": (
            float(r["registry_mb"])
            + float(r["cross_domain_peer_mb"])
        ),
        "keep_ms": float(r["keep_ms"]),
        "scout_ms": float(r["scout_ms"]),
        "accepted": int(r["accepted"]),
        "mcf_differences": int(
            d.get("audit_differing_demands", 0)
        ),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--run", action="store_true")
    args = ap.parse_args()

    root = Path(args.out).resolve()
    root.mkdir(parents=True, exist_ok=True)

    items = build_inputs(root)

    print("[SOURCE]", TRACE, flush=True)

    for item in items:
        print(
            "[WINDOW]",
            item["name"],
            item["day"],
            "source_requests=",
            item["requests_in_source_day"],
            flush=True,
        )

    if not args.run:
        print("[DRY RUN PASSED] No simulation executed")
        return

    rows = []

    for item in items:
        for method, scout, cache, ablation in POLICIES:
            print(
                "[RUN]", item["name"], method,
                flush=True,
            )

            row = run_one(
                root, item,
                method, scout, cache, ablation,
            )

            # 原始对照必须先通过，才能相信后面的窗口实验。
            if item["name"] == "original_target_control":
                expected = {
                    "original_keep": (
                        10.880469819638849,
                        52.68245398173562,
                        29541.0,
                    ),
                    "lru": (
                        10.812954563970255,
                        58.55824517017785,
                        32045.0,
                    ),
                    "warmup_frequency": (
                        7.155637478214478,
                        44.324499999999944,
                        29086.0,
                    ),
                }

                e = expected[method]

                if (
                    abs(row["mean_s"] - e[0]) > 0.01
                    or abs(row["p95_s"] - e[1]) > 0.01
                    or abs(row["upper_mb"] - e[2]) > 1.0
                ):
                    raise RuntimeError(
                        f"CONTROL FAILED: {method} "
                        f"actual={row} expected={e}"
                    )

                print(
                    "[CONTROL PASSED]", method,
                    flush=True,
                )

            rows.append(row)

            summary = root / "summary.csv"

            with summary.open(
                "w", newline="", encoding="utf-8"
            ) as f:
                w = csv.DictWriter(
                    f, fieldnames=list(rows[0])
                )
                w.writeheader()
                w.writerows(rows)

            print(
                f"[DONE] {item['name']} {method} "
                f"Mean={row['mean_s']:.3f} "
                f"P95={row['p95_s']:.3f} "
                f"Upper={row['upper_mb']:.0f}",
                flush=True,
            )

    (root / "FINISHED.txt").write_text(
        "COMPLETE\n", encoding="utf-8"
    )

    print("[ALL FINISHED]", root / "summary.csv")


if __name__ == "__main__":
    main()
