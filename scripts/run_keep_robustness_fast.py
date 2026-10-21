from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path

CASE = Path(
    "cases/cider_scale_dense_arrival_corrected/"
    "cider_nodes40_domains8_req800_weak.json"
)

POLICIES = ("keep", "lru", "warmup_frequency")


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_source_state():
    review_root = Path(
        Path("results/LATEST_REVIEWER_MECHANISM")
        .read_text(encoding="utf-8")
        .strip()
    )

    manifest = {}

    for line in (
        review_root / "manifest.txt"
    ).read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            manifest[k] = v

    state_path = Path(manifest["STATE"])

    return (
        json.loads(state_path.read_text(encoding="utf-8")),
        state_path,
    )


def prepare(root, source, source_path):
    original_case = json.loads(
        CASE.read_text(encoding="utf-8")
    )
    original_target = source["target_case"]

    sizes = {
        str(k): float(v)
        for k, v in original_target["layer_sizes_mb"].items()
    }

    scales = (0.5, 1.0, 2.0)

    # 构造三档容量都能容纳的相同初始缓存。
    # 用确定性哈希排序裁剪，不读取 target 未来请求。
    common_cache = {}
    removed = 0

    for node in original_target["nodes"]:
        eid = node["eid"]

        cap_min = max(
            1,
            round(float(node["repo_capacity_mb"]) * min(scales)),
        )

        layers = sorted(
            set(node.get("initial_cache", [])),
            key=lambda layer: hashlib.sha256(
                f"{eid}|{layer}".encode()
            ).hexdigest(),
        )

        kept = []
        used = 0.0

        for layer in layers:
            size = sizes.get(str(layer))

            if size is None:
                continue

            if used + size <= cap_min + 1e-9:
                kept.append(layer)
                used += size
            else:
                removed += 1

        common_cache[eid] = kept

    scenarios = []

    # --------------------------------------------------------
    # A. 容量敏感性：固定同一份 placement 和初始缓存
    # --------------------------------------------------------

    for scale in scales:
        tag = str(scale).replace(".", "p")
        name = f"{CASE.stem}__cap_{tag}x"

        state = copy.deepcopy(source)
        inp = copy.deepcopy(original_case)
        target = state["target_case"]

        for node, inp_node in zip(
            target["nodes"], inp["nodes"]
        ):
            if node["eid"] != inp_node["eid"]:
                raise ValueError("Node-order mismatch")

            cap = max(
                1,
                round(float(node["repo_capacity_mb"]) * scale),
            )

            node["repo_capacity_mb"] = cap
            inp_node["repo_capacity_mb"] = cap

            # 只在 0.5x 容量不足时裁剪初始缓存。
            # 1x / 2x 保留正式实验的原始 warmup cache。
            node["initial_cache"] = (
                list(common_cache[node["eid"]])
                if scale == 0.5
                else list(node.get("initial_cache", []))
            )

        scenarios.append((
            name, "capacity", f"{scale:g}x", state, inp
        ))

    # --------------------------------------------------------
    # B. 将原 target 400 请求分成四个不重叠子窗口
    # 历史 warmup 仍然是原先的 400 请求
    # 不调用 FG-DSCR
    # --------------------------------------------------------

    ordered = sorted(
        original_target["containers"],
        key=lambda c: (
            float(c.get("arrival_time_s", 0)),
            str(c["cid"]),
        ),
    )

    if not all(
        "arrival_time_s" in c for c in ordered
    ):
        raise ValueError(
            "Window experiment requires arrival_time_s"
        )

    for i in range(4):
        lo = i * len(ordered) // 4
        hi = (i + 1) * len(ordered) // 4

        sample = copy.deepcopy(ordered[lo:hi])

        if not sample:
            raise ValueError("Empty target window")

        t0 = min(
            float(c["arrival_time_s"])
            for c in sample
        )

        for c in sample:
            c["arrival_time_s"] -= t0

        ids = {c["cid"] for c in sample}

        state = copy.deepcopy(source)

        state["target_case"]["containers"] = sample

        state["placement"] = {
            eid: [
                cid for cid in cids
                if cid in ids
            ]
            for eid, cids
            in source["placement"].items()
        }

        state["target_requests"] = len(sample)

        name = f"{CASE.stem}__window_{i+1:02d}"

        scenarios.append((
            name,
            "window",
            f"window_{i+1:02d}",
            state,
            copy.deepcopy(original_case),
        ))

    index = []

    for name, kind, label, state, inp in scenarios:
        case_path = (
            root / "cases" / f"{name}.json"
        ).resolve()

        state_path = (
            root / "states" / f"{name}__seed1.json"
        )

        state["case"] = str(case_path)

        write_json(case_path, inp)
        write_json(state_path, state)

        index.append({
            "name": name,
            "kind": kind,
            "label": label,
            "case": str(case_path),
            "target_requests": len(
                state["target_case"]["containers"]
            ),
        })

    write_json(
        root / "manifest.json",
        {
            "source_state": str(source_path),
            "source_case_sha256": hashlib.sha256(
                CASE.read_bytes()
            ).hexdigest(),
            "common_cache_removed_layers": removed,
            "description": (
                "Capacity: common initial cache and placement. "
                "Window: non-overlapping target slices with "
                "shared historical warmup. No FG rerun."
            ),
            "scenarios": index,
        },
    )

    return index


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument("--out", required=True)

    ap.add_argument(
        "--phase",
        choices=("capacity", "window", "both"),
        default="capacity",
    )

    args = ap.parse_args()

    root = Path(args.out).resolve()
    root.mkdir(parents=True, exist_ok=True)

    source, source_path = get_source_state()
    index = prepare(root, source, source_path)

    print(
        "[READY] Reused FG state; "
        "adapter.run() was NOT called",
        flush=True,
    )

    summary = root / "summary.csv"

    if summary.exists():
        with summary.open(
            encoding="utf-8", newline=""
        ) as f:
            rows = list(csv.DictReader(f))
    else:
        rows = []

    existing = {
        (r["scenario"], r["policy"])
        for r in rows
    }

    selected = [
        item for item in index
        if args.phase == "both"
        or item["kind"] == args.phase
    ]

    total = len(selected) * len(POLICIES)
    completed = 0

    for item in selected:
        for policy in POLICIES:
            key = (item["label"], policy)

            if key in existing:
                completed += 1
                print("[SKIP]", *key, flush=True)
                continue

            out = (
                root / "runs" / item["name"] / policy
            )
            out.mkdir(parents=True, exist_ok=True)

            raw = out / "raw.csv"
            diag = out / "review_diag.json"

            if not (raw.exists() and diag.exists()):
                cmd = [
                    sys.executable, "-u", "-m",
                    "scripts.review_mechanisms",

                    "--scout-mode", "independent",
                    "--cache-policy",
                    (
                        "warmup_lfu"
                        if policy == "warmup_frequency"
                        else "keep"
                    ),
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

                    "--ablation",
                    (
                        "no_keep"
                        if policy == "lru"
                        else "full"
                    ),

                    "--out", str(out),
                ]

                print(
                    "[RUN]",
                    item["label"],
                    policy,
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
                    print(
                        (out / "run.log")
                        .read_text(errors="replace")[-6000:],
                        flush=True,
                    )
                    raise SystemExit(
                        f"[FAILED] {item['label']}/{policy}"
                    )

            with raw.open(
                encoding="utf-8", newline=""
            ) as f:
                r = next(csv.DictReader(f))

            d = json.loads(
                diag.read_text(encoding="utf-8")
            )

            row = {
                "group": item["kind"],
                "scenario": item["label"],
                "policy": policy,
                "target_requests": item["target_requests"],
                "accepted": int(r["accepted"]),
                "mean_s": float(r["mean_ready_s"]),
                "p95_s": float(r["p95_ready_s"]),
                "upper_mb": (
                    float(r["registry_mb"])
                    + float(r["cross_domain_peer_mb"])
                ),
                "keep_total_ms": float(r["keep_ms"]),
                "keep_p95_ms": float(
                    d["timings_ms"]["keep"]["p95_ms"]
                ),
            }

            rows.append(row)
            completed += 1

            with summary.open(
                "w", newline="", encoding="utf-8"
            ) as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=list(rows[0]),
                )
                writer.writeheader()
                writer.writerows(rows)

            print(
                f"[DONE {completed}/{total}] "
                f"{item['label']} {policy} "
                f"Mean={row['mean_s']:.3f} "
                f"P95={row['p95_s']:.3f} "
                f"Upper={row['upper_mb']:.0f}",
                flush=True,
            )

    (
        root / f"FINISHED_{args.phase}.txt"
    ).write_text("COMPLETE\n")

    print(
        "[FINISHED]",
        args.phase,
        root,
        flush=True,
    )


if __name__ == "__main__":
    main()
