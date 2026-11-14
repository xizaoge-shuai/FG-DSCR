#!/usr/bin/env python3
import argparse
import csv
import json
import os
import sys
from pathlib import Path
from statistics import mean

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_formal_parameter_audit as audit
import run_standard_cache_policy_fig8 as cache_runner

SCALES = list(range(200, 1001, 100))
POLICIES = ("pgdsf", "lru", "lfu", "gdsf")

# 固定 S1 调度结果，仅改变 PGDSF 缓存参数。
VARIANTS = [
    ("original50", {}),
    ("exact1", {"unit_mb": 1}),
    ("exact1_kpin3", {"unit_mb": 1, "k_pin": 3}),
    ("exact1_kpin9", {"unit_mb": 1, "k_pin": 9}),
    ("exact1_kpin12", {"unit_mb": 1, "k_pin": 12}),
    ("exact1_beta1", {"unit_mb": 1, "beta_slot": 1.0}),
    ("exact1_beta4", {"unit_mb": 1, "beta_slot": 4.0}),
    ("exact1_rho0", {"unit_mb": 1, "rho_centrality": 0.0}),
    ("exact1_rho05", {"unit_mb": 1, "rho_centrality": 0.5}),
    ("exact1_kappa025", {"unit_mb": 1, "kappa_overshoot": 0.25}),
    ("exact1_kappa1", {"unit_mb": 1, "kappa_overshoot": 1.0}),
]


def load_reference(path):
    if not path.is_file():
        raise FileNotFoundError(path)

    result = {}

    with path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            policy = r["cache_policy"].lower()
            n = int(r["requests"])
            result.setdefault(n, {})[policy] = r

    for n in SCALES:
        if n not in result:
            raise RuntimeError(f"Reference missing N={n}")
        for p in POLICIES:
            if p not in result[n]:
                raise RuntimeError(
                    f"Reference missing N={n}, policy={p}"
                )

    return result


def verify_reference(n, policy, metrics, ref):
    r = ref[n][policy]

    expected_j = float(r["J"])
    if abs(metrics["J"] - expected_j) > 1e-7:
        raise RuntimeError(
            f"Reference J mismatch: N={n}, {policy}, "
            f"new={metrics['J']:.10f}, old={expected_j:.10f}"
        )

    expected_dl = float(r["DownloadedMB"])
    if abs(metrics["downloaded_mb"] - expected_dl) > 1e-5:
        raise RuntimeError(
            f"Reference download mismatch: N={n}, {policy}"
        )


def replay_pgdsf(mod, containers, nodes, original,
                 settings, overrides):

    kw = dict(settings)
    kw.pop("effective_queue_order", None)
    kw.update(overrides)
    kw["cache_policy"] = "pgdsf"

    scheduler = mod.FGDscrScheduler(**kw)
    scheduler.set_data(containers, nodes)

    node_logs = []
    container_logs = []
    downloaded = 0.0
    reused = 0.0

    node_lookup = {
        str(k): v for k, v in scheduler.nodes.items()
    }

    for eid, sequence in original["ordered_queues"].items():
        qm = scheduler.simulate_queue(
            list(sequence),
            node_lookup[str(eid)]
        )

        node_logs.extend(qm.step_logs)
        container_logs.extend(qm.container_logs)

        downloaded += qm.downloaded_mb
        reused += qm.reused_mb

    summary = dict(original["summary"])
    summary["downloaded_mb"] = downloaded
    summary["reused_mb"] = reused
    summary["reuse_rate"] = reused / max(
        downloaded + reused, 1e-9
    )

    return {
        "assignment": original["assignment"],
        "ordered_queues": original["ordered_queues"],
        "container_metrics": container_logs,
        "node_step_logs": node_logs,
        "summary": summary,
    }


def make_row(variant, params, policy, n, result,
             metrics, events, evicted_mb, signatures):

    return {
        "variant": variant,
        "parameters": json.dumps(params, sort_keys=True),
        "requests": n,
        "policy": policy,
        "J": metrics["J"],
        "D": metrics["Delay_raw"],
        "ACT": metrics["all_request_mean"],
        "P95": metrics["P95_all"],
        "Fallback_pct": metrics["CA_rate"] * 100,
        "Frag": metrics["frag_score"],
        "DownloadedMB": metrics["downloaded_mb"],
        "ReuseRate": result["summary"].get("reuse_rate", 0.0),
        "EvictionEvents": events,
        "EvictedMB": evicted_mb,
        "AssignmentSignature": signatures[0],
        "OrderingSignature": signatures[1],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--case-dir",
        default="cases/drtp_cache_only_sweep_88_prefix"
    )
    ap.add_argument(
        "--algorithm",
        default="scripts/fg_dscr_incremental.py"
    )
    ap.add_argument(
        "--reference-csv",
        default=(
            "results/comnet_revision/"
            "cache_policy_fig8_matched_seed0/"
            "cache_policy_by_scale.csv"
        )
    )
    ap.add_argument("--provision-sec", type=float, default=300.0)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    historical = dict(cache_runner.HISTORICAL_FIG8)

    reference = load_reference(Path(args.reference_csv))
    mod = audit.modload(args.algorithm)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    rows = []
    base_settings = {}

    for n in SCALES:
        print(f"\n========== N={n} ==========", flush=True)

        cp = (
            Path(args.case_dir)
            / f"drtp_img88_cacheonly_1024mb_{n}.json"
        )

        if not cp.is_file():
            raise FileNotFoundError(cp)

        case = json.loads(cp.read_text(encoding="utf-8"))
        containers, nodes, _ = mod.load_case(str(cp))

        # 仅生成一次原始 S1 placement/order。
        full_metrics, wall_ms, settings, original = audit.run_one(
            mod, cp, "archived_candidate",
            historical, args.provision_sec
        )

        signatures = (
            full_metrics["assignment_signature"],
            full_metrics["ordering_signature"],
        )

        ref_pg = reference[n]["pgdsf"]

        if signatures[0] != ref_pg["AssignmentSignature"]:
            raise RuntimeError(
                f"N={n}: placement signature mismatch"
            )

        if signatures[1] != ref_pg["OrderingSignature"]:
            raise RuntimeError(
                f"N={n}: ordering signature mismatch"
            )

        base_settings[str(n)] = {
            "wall_ms": wall_ms,
            "assignment_signature": signatures[0],
            "ordering_signature": signatures[1],
        }

        # 先计算原始 PGDSF 的固定队列回放。
        base_pg = replay_pgdsf(
            mod, containers, nodes, original,
            settings, {}
        )

        base_pg_metrics = audit.raw_article_metrics(
            case, base_pg, args.provision_sec
        )

        if abs(base_pg_metrics["J"] - full_metrics["J"]) > 1e-8:
            raise RuntimeError(
                f"N={n}: PGDSF replay differs from original"
            )

        verify_reference(
            n, "pgdsf", base_pg_metrics, reference
        )

        # LRU / LFU / GDSF 只计算一次。
        baselines = {}

        for policy in ("lru", "lfu", "gdsf"):
            result, events, evicted_mb = (
                cache_runner.replay_standard(
                    case, mod, original, policy
                )
            )

            metrics = audit.raw_article_metrics(
                case, result, args.provision_sec
            )

            verify_reference(
                n, policy, metrics, reference
            )

            baselines[policy] = (
                result, metrics, events, evicted_mb
            )

        # 对每组参数执行固定 placement/order 的 PGDSF 回放。
        for variant, params in VARIANTS:
            if variant == "original50":
                result = base_pg
                metrics = base_pg_metrics
            else:
                result = replay_pgdsf(
                    mod, containers, nodes, original,
                    settings, params
                )

                metrics = audit.raw_article_metrics(
                    case, result, args.provision_sec
                )

            events = sum(
                int(x.get("num_evicted_layers", 0))
                for x in result["node_step_logs"]
            )

            evicted_mb = sum(
                float(x.get("evicted_mb", 0.0))
                for x in result["node_step_logs"]
            )

            rows.append(make_row(
                variant, params, "pgdsf", n,
                result, metrics, events, evicted_mb,
                signatures
            ))

            for policy, values in baselines.items():
                b_result, b_metrics, b_events, b_evicted = values

                rows.append(make_row(
                    variant, params, policy, n,
                    b_result, b_metrics,
                    b_events, b_evicted, signatures
                ))

            print(
                f"{variant:<20} "
                f"PGDSF J={metrics['J']:.6f}",
                flush=True
            )

    # 保存逐规模结果。
    by_scale = out / "pgdsf_parameter_by_scale.csv"

    with by_scale.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    # 汇总九个规模。
    summaries = []

    for variant, params in VARIANTS:
        means = {}

        for policy in POLICIES:
            subset = [
                r for r in rows
                if r["variant"] == variant
                and r["policy"] == policy
            ]

            means[policy] = {
                key: mean(float(x[key]) for x in subset)
                for key in (
                    "J", "D", "ACT", "P95",
                    "Fallback_pct", "Frag",
                    "DownloadedMB", "ReuseRate"
                )
            }

        margins = {
            p: means[p]["J"] - means["pgdsf"]["J"]
            for p in ("lru", "lfu", "gdsf")
        }

        summaries.append({
            "variant": variant,
            "parameters": json.dumps(params, sort_keys=True),
            "PGDSF_J": means["pgdsf"]["J"],
            "LRU_J": means["lru"]["J"],
            "LFU_J": means["lfu"]["J"],
            "GDSF_J": means["gdsf"]["J"],
            "margin_vs_LRU": margins["lru"],
            "margin_vs_LFU": margins["lfu"],
            "margin_vs_GDSF": margins["gdsf"],
            "minimum_margin": min(margins.values()),
            "mean_margin": mean(margins.values()),
            "PGDSF_DownloadMB": means["pgdsf"]["DownloadedMB"],
            "LRU_DownloadMB": means["lru"]["DownloadedMB"],
            "PGDSF_D": means["pgdsf"]["D"],
            "PGDSF_ReuseRate": means["pgdsf"]["ReuseRate"],
        })

    summaries.sort(
        key=lambda x: x["minimum_margin"],
        reverse=True
    )

    summary_file = out / "pgdsf_parameter_summary.csv"

    with summary_file.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(summaries[0]))
        w.writeheader()
        w.writerows(summaries)

    provenance = {
        "experiment": "S1 fixed-order PGDSF cache parameter sweep",
        "profile": "archived_candidate",
        "historical_overrides": historical,
        "algorithm": args.algorithm,
        "case_dir": args.case_dir,
        "reference_csv": args.reference_csv,
        "provision_sec": args.provision_sec,
        "PYTHONHASHSEED": os.environ.get("PYTHONHASHSEED"),
        "frozen_placement": True,
        "frozen_ordering": True,
        "variants": [
            {"name": name, "overrides": params}
            for name, params in VARIANTS
        ],
        "base_signatures": base_settings,
        "interpretation": (
            "Positive margin means PGDSF has lower J than the baseline. "
            "This is an exploratory parameter sensitivity sweep; "
            "the selected configuration requires independent-seed validation."
        )
    }

    (out / "provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    print("\n========== PARAMETER RANKING ==========")

    for r in summaries:
        print(
            f"{r['variant']:<20} "
            f"PGDSF={r['PGDSF_J']:.6f} "
            f"LRU={r['LRU_J']:.6f} "
            f"LFU={r['LFU_J']:.6f} "
            f"GDSF={r['GDSF_J']:.6f} "
            f"WORST_MARGIN={r['minimum_margin']:+.6f}"
        )

    print("\n[OK] Results:", out)


if __name__ == "__main__":
    main()
