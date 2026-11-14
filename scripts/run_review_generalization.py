from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


# 原 SCOUT、独立选源、LRU 和历史频率
POLICIES = (
    ("original_keep", "audit", "keep", "full"),
    ("independent_keep", "independent", "keep", "full"),
    ("independent_lru", "independent", "keep", "no_keep"),
    ("independent_frequency", "independent", "warmup_lfu", "full"),
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def find_states(repo, wanted_nodes):
    """
    查找已有 state，不执行 FG-DSCR。
    仅选择时间戳齐全、预热和测试划分可验证的状态。
    """
    cases_root = repo / "cases"
    found = []
    rejected = defaultdict(int)
    seen = set()

    paths = sorted(
        (repo / "results").glob(
            "**/states/*__seed1.json"
        )
    )

    for state_path in paths:
        try:
            state = json.loads(
                state_path.read_text(encoding="utf-8")
            )

            required = {
                "case",
                "target_case",
                "placement",
                "num_nodes",
                "num_domains",
            }

            if not required <= state.keys():
                rejected["missing_fields"] += 1
                continue

            if int(state["num_nodes"]) != wanted_nodes:
                rejected["other_nodes"] += 1
                continue

            case_path = Path(state["case"])

            if not case_path.is_absolute():
                case_path = repo / case_path

            case_path = case_path.resolve()

            # 必须来源于正式 cases，而不是以前实验
            # 临时复制或人为切片的结果。
            case_path.relative_to(cases_root)

            if not case_path.is_file():
                rejected["missing_case"] += 1
                continue

            case = json.loads(
                case_path.read_text(encoding="utf-8")
            )

            requests = case["containers"]

            if (
                len(requests) < 100
                or not all(
                    "arrival_time_s" in c
                    for c in requests
                )
            ):
                rejected["no_arrivals"] += 1
                continue

            ordered = sorted(
                requests,
                key=lambda c: (
                    float(c["arrival_time_s"]),
                    str(c["cid"]),
                ),
            )

            n = len(ordered)
            cut = max(
                1, min(n - 1, round(n * 0.5))
            )

            warm_ids = {
                str(c["cid"])
                for c in ordered[:cut]
            }

            expected_target = {
                str(c["cid"])
                for c in ordered[cut:]
            }

            actual_target = {
                str(c["cid"])
                for c in state[
                    "target_case"
                ]["containers"]
            }

            # 保证测试请求与预热请求不重叠。
            if (
                not actual_target
                or expected_target != actual_target
                or warm_ids & actual_target
            ):
                rejected["split_mismatch"] += 1
                continue

            placement_ids = [
                str(cid)
                for ids in state["placement"].values()
                for cid in ids
            ]

            if (
                len(set(placement_ids)) != len(placement_ids)
                or not set(placement_ids) <= actual_target
            ):
                rejected["placement_mismatch"] += 1
                continue

            target = state["target_case"]
            sizes = target["layer_sizes_mb"]

            if not all(
                "initial_cache" in node
                and "repo_capacity_mb" in node
                for node in target["nodes"]
            ):
                rejected["missing_cache"] += 1
                continue

            if any(
                sum(
                    float(sizes[layer])
                    for layer in node["initial_cache"]
                )
                > float(node["repo_capacity_mb"]) + 1e-6
                for node in target["nodes"]
            ):
                rejected["cache_overflow"] += 1
                continue

            case_hash = digest(case_path)

            # 同一个 case 的多个 state 副本不重复跑。
            if case_hash in seen:
                rejected["duplicate_case"] += 1
                continue

            seen.add(case_hash)

            family = str(
                case_path.parent.relative_to(cases_root)
            )

            found.append({
                "family": family,
                "case": str(case_path),
                "case_name": case_path.stem,
                "case_hash": case_hash,
                "state": str(state_path.resolve()),
                "state_hash": digest(state_path),
                "target_requests": len(actual_target),
                "accepted": len(placement_ids),
            })

        except (
            ValueError,
            TypeError,
            KeyError,
            OSError,
            json.JSONDecodeError,
        ):
            rejected["invalid_state"] += 1

    # 优先覆盖不同 workload 家族，
    # 每个家族先选一个典型规模。
    groups = defaultdict(list)

    for item in found:
        groups[item["family"]].append(item)

    for group in groups.values():
        group.sort(
            key=lambda x: (
                abs(x["target_requests"] - 400),
                x["case_name"],
            )
        )

    ordered = []

    for family in sorted(groups):
        ordered.append(groups[family].pop(0))

    for family in sorted(groups):
        ordered.extend(groups[family])

    return ordered, dict(rejected)


def save_csv(path, rows):
    if not rows:
        return

    with path.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=list(rows[0]),
        )
        writer.writeheader()
        writer.writerows(rows)


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument("--out", required=True)
    ap.add_argument("--nodes", type=int, default=40)
    ap.add_argument("--max-cases", type=int, default=6)

    # 不添加 --run 时只审计输入，不跑实验。
    ap.add_argument("--run", action="store_true")

    args = ap.parse_args()

    if args.nodes < 1 or args.max_cases < 1:
        ap.error("nodes and max-cases must be positive")

    repo = Path.cwd().resolve()
    root = Path(args.out).resolve()

    root.mkdir(parents=True, exist_ok=True)

    candidates, rejected = find_states(
        repo, args.nodes
    )

    selected = candidates[:args.max_cases]

    manifest = {
        "experiment": "review_generalization",
        "scope": (
            "Chronological cross-workload evaluation "
            "with shared offline placement. "
            "Not evidence of independent source datasets."
        ),
        "candidate_count": len(candidates),
        "selected": selected,
        "rejected": rejected,
        "configuration": {
            "nodes": args.nodes,
            "max_cases": args.max_cases,
            "WAN_MBps": 250,
            "peer_upload_MBps": 12.5,
            "alpha": 1,
            "beta": 4,
            "gamma": 0.5,
            "keep_v": 3e6,
        },
    }

    (root / "manifest.json").write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        f"[INVENTORY] candidates={len(candidates)} "
        f"selected={len(selected)} "
        f"rejected={rejected}",
        flush=True,
    )

    for i, item in enumerate(selected, 1):
        print(
            f"[{i}] {item['family']}/"
            f"{item['case_name']} "
            f"target={item['target_requests']} "
            f"accepted={item['accepted']}",
            flush=True,
        )

    if not args.run:
        print("[DRY RUN] No simulations started")
        return

    if not selected:
        raise SystemExit(
            "No valid saved states; FG rerun is disabled."
        )

    if not Path(
        "scripts/review_mechanisms.py"
    ).is_file():
        raise FileNotFoundError(
            "scripts/review_mechanisms.py"
        )

    rows = []

    for i, item in enumerate(selected, 1):

        case_path = Path(item["case"])
        state_path = Path(item["state"])

        # 防止后台运行时输入被修改。
        if (
            digest(case_path) != item["case_hash"]
            or digest(state_path) != item["state_hash"]
        ):
            raise RuntimeError(
                "Case or state changed during experiment"
            )

        label = (
            f"{i:02d}_"
            f"{item['family'].replace('/', '_')}_"
            f"{item['case_name']}"
        )

        for method, scout, cache, ablation in POLICIES:

            out = root / "runs" / label / method
            out.mkdir(parents=True, exist_ok=True)

            raw = out / "raw.csv"
            diagnostic = out / "diagnostics.json"

            if not (
                raw.is_file()
                and diagnostic.is_file()
            ):

                cmd = [
                    sys.executable,
                    "-u",
                    "-m",
                    "scripts.review_mechanisms",

                    "--scout-mode", scout,
                    "--cache-policy", cache,
                    "--diagnostics", str(diagnostic),
                    "--",

                    "--state-root", str(state_path.parent),
                    "--cases", str(case_path),
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
                    f"[RUN {i}/{len(selected)}] "
                    f"{item['family']} {method}",
                    flush=True,
                )

                with (
                    out / "run.log"
                ).open("w", encoding="utf-8") as log:

                    result = subprocess.run(
                        cmd,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        check=False,
                    )

                if result.returncode != 0:
                    raise RuntimeError(
                        f"FAILED: {out / 'run.log'}"
                    )

            with raw.open(
                encoding="utf-8",
                newline="",
            ) as f:
                r = next(csv.DictReader(f))

            d = json.loads(
                diagnostic.read_text(encoding="utf-8")
            )

            if int(r["accepted"]) != item["accepted"]:
                raise RuntimeError(
                    "Accepted request count differs "
                    "from saved placement"
                )

            row = {
                "family": item["family"],
                "case": item["case_name"],
                "case_hash": item["case_hash"],
                "policy": method,
                "target_requests": item["target_requests"],
                "accepted": int(r["accepted"]),
                "mean_s": float(r["mean_ready_s"]),
                "p95_s": float(r["p95_ready_s"]),
                "upper_mb": (
                    float(r["registry_mb"])
                    + float(r["cross_domain_peer_mb"])
                ),
                "keep_ms": float(r["keep_ms"]),
                "scout_ms": float(r["scout_ms"]),
                "audit_differences": int(
                    d.get("audit_differing_demands", 0)
                ),
            }

            rows.append(row)

            # 每完成一组立即保存。
            save_csv(root / "summary.csv", rows)

            print(
                f"[DONE] {method} "
                f"Mean={row['mean_s']:.3f} "
                f"P95={row['p95_s']:.3f} "
                f"Upper={row['upper_mb']:.0f}",
                flush=True,
            )

    (root / "FINISHED.txt").write_text(
        "COMPLETE\n",
        encoding="utf-8",
    )

    print(
        f"[ALL FINISHED] {root / 'summary.csv'}",
        flush=True,
    )


if __name__ == "__main__":
    main()
