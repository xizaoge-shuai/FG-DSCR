import csv
import json
import sys
from pathlib import Path
from statistics import mean

sys.path.insert(
    0, str(Path("tools/comnet_revision").resolve())
)

from run_formal_parameter_audit import modload, run_one

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=False)

CASE_DIR = Path("cases/drtp_cache_only_sweep_88")
MOD = modload("scripts/fg_dscr_incremental.py")

SCALES = list(range(200, 1001, 100))

# 三组候选配置
CONFIGS = {
    "fgp_enhanced": {
        "lambda_frag": 0.3,
        "phase1_neighbor_mode": "full",
        "max_best_response_rounds": 8,
        "beam_width": 1,
        "alpha2_future": 0.15,
    },

    "dco_enhanced": {
        "lambda_frag": 0.1,
        "phase1_neighbor_mode": "move_swap",
        "max_best_response_rounds": 5,
        "beam_width": 4,
        "alpha2_future": 0.30,
    },

    "combined_enhanced": {
        "lambda_frag": 0.3,
        "phase1_neighbor_mode": "full",
        "max_best_response_rounds": 8,
        "beam_width": 4,
        "alpha2_future": 0.30,
    },
}

# 每组消融均只关闭指定模块
VARIANTS = {
    "full": {},

    "no_fgp_refine": {
        "max_best_response_rounds": 0,
    },

    "no_dco": {
        "_input_order_queue": True,
    },

    "no_both": {
        "max_best_response_rounds": 0,
        "_input_order_queue": True,
    },

    # 只去除 DCO 显式未来共享奖励，
    # 不改变 PGDSF 的未来频率及 Pinning 机制。
    "no_future_share": {
        "alpha2_future": 0.0,
    },
}

# 严格冻结 PGDSF 参数
PGDSF_FIXED = {
    "cache_policy": "pgdsf",
    "k_pin": 6,
    "unit_mb": 50,
    "alpha1_reuse": 1.0,
    "alpha3_pull": 50.0,
    "alpha4_evict": 0.02,
    "beta_slot": 2.0,
    "rho_centrality": 0.2,
    "kappa_overshoot": 0.5,
}

def check_pgdsf(settings):
    for key, expected in PGDSF_FIXED.items():
        actual = settings.get(key)
        if actual is None:
            raise RuntimeError(
                f"Missing PGDSF parameter: {key}"
            )

        if isinstance(expected, float):
            if abs(float(actual) - expected) > 1e-12:
                raise RuntimeError(
                    f"PGDSF changed: {key}={actual}"
                )
        elif actual != expected:
            raise RuntimeError(
                f"PGDSF changed: {key}={actual}"
            )

def get_phi(result):
    history = result.get("phase1_history", [])

    if not history:
        return None, None, 0

    initial = float(history[0]["potential"])
    final = float(history[-1]["potential"])

    accepted = sum(
        1 for x in history
        if int(x.get("cycle", 0)) > 0
        and not str(x.get("label", "")).startswith("no_improve")
    )

    return initial, final, accepted

rows = []

csv_path = OUT / "ablation_sensitivity_by_case.csv"

def save_checkpoint():
    if not rows:
        return

    with csv_path.open(
        "w", newline="", encoding="utf-8"
    ) as f:
        writer = csv.DictWriter(
            f, fieldnames=list(rows[0].keys())
        )
        writer.writeheader()
        writer.writerows(rows)

for config_name, config in CONFIGS.items():

    print("\n" + "=" * 75, flush=True)
    print("CONFIG:", config_name, config, flush=True)
    print("=" * 75, flush=True)

    for n in SCALES:

        case = CASE_DIR / (
            f"drtp_img88_cacheonly_1024mb_{n}.json"
        )

        if not case.is_file():
            raise FileNotFoundError(case)

        full_metrics = None

        for variant, variant_override in VARIANTS.items():

            overrides = {
                **config,
                **variant_override,
            }

            metrics, elapsed, settings, result = run_one(
                MOD,
                case,
                "paper_declared",
                overrides,
                300.0,
            )

            check_pgdsf(settings)

            if variant == "full":
                full_metrics = metrics

            if full_metrics is None:
                raise RuntimeError(
                    "Full must be executed before ablations"
                )

            delta_j = (
                float(metrics["J"])
                - float(full_metrics["J"])
            )

            phi_initial, phi_final, accepted = get_phi(result)

            row = {
                "config": config_name,
                "variant": variant,
                "requests": n,

                "J": metrics["J"],
                "delta_J_vs_full": delta_j,

                "D": metrics["Delay_raw"],
                "CA": metrics["CA_rate"],
                "Frag": metrics["frag_score"],
                "Download_MB": metrics["downloaded_mb"],

                "placement_match": int(
                    metrics["assignment_signature"]
                    == full_metrics["assignment_signature"]
                ),

                "order_match": int(
                    metrics["ordering_signature"]
                    == full_metrics["ordering_signature"]
                ),

                "initial_phi": phi_initial,
                "final_phi": phi_final,
                "accepted_refinements": accepted,
                "wall_ms": elapsed,

                "beam_width": settings["beam_width"],
                "lambda_frag": settings["lambda_frag"],
                "alpha2_future": settings["alpha2_future"],
            }

            rows.append(row)
            save_checkpoint()

            print(
                f"{config_name:<19} "
                f"N={n:4d} "
                f"{variant:<17} "
                f"J={metrics['J']:.6f} "
                f"Delta={delta_j:+.6f} "
                f"PlacementMatch={row['placement_match']} "
                f"OrderMatch={row['order_match']} "
                f"FGP_moves={accepted}",
                flush=True,
            )

# 汇总
summary = []

for config_name in CONFIGS:

    full = {
        int(r["requests"]): r
        for r in rows
        if r["config"] == config_name
        and r["variant"] == "full"
    }

    for variant in VARIANTS:

        rr = [
            r for r in rows
            if r["config"] == config_name
            and r["variant"] == variant
        ]

        deltas = [
            float(r["J"]) - float(full[int(r["requests"])]["J"])
            for r in rr
        ]

        summary.append({
            "config": config_name,
            "variant": variant,
            "mean_J": mean(float(r["J"]) for r in rr),
            "mean_delta_J": mean(deltas),
            "wins_for_full": sum(x > 1e-9 for x in deltas),
            "losses_for_full": sum(x < -1e-9 for x in deltas),
            "changed_placements": sum(
                not r["placement_match"] for r in rr
            ),
            "changed_orders": sum(
                not r["order_match"] for r in rr
            ),
            "mean_D": mean(float(r["D"]) for r in rr),
            "mean_Frag": mean(float(r["Frag"]) for r in rr),
            "mean_download_MB": mean(
                float(r["Download_MB"]) for r in rr
            ),
        })

with (OUT / "ablation_sensitivity_summary.csv").open(
    "w", newline="", encoding="utf-8"
) as f:
    w = csv.DictWriter(f, fieldnames=list(summary[0]))
    w.writeheader()
    w.writerows(summary)

manifest = {
    "profile": "paper_declared",
    "configs": CONFIGS,
    "variants": VARIANTS,
    "PGDSF_fixed": PGDSF_FIXED,
    "scales": SCALES,
    "provision_sec": 300,
    "note": (
        "Parameter sensitivity experiment. "
        "Candidate configurations are not automatically "
        "substitutes for the original manuscript results."
    ),
}

(OUT / "manifest.json").write_text(
    json.dumps(manifest, indent=2, ensure_ascii=False),
    encoding="utf-8",
)

print("\n========== FINAL SUMMARY ==========")

for r in summary:
    print(
        f"{r['config']:<20} "
        f"{r['variant']:<18} "
        f"J={r['mean_J']:.6f} "
        f"Delta={r['mean_delta_J']:+.6f} "
        f"Wins={r['wins_for_full']}/9 "
        f"PlacementChanged={r['changed_placements']}/9 "
        f"OrderChanged={r['changed_orders']}/9"
    )

print("\nPGDSF PARAMETERS VERIFIED UNCHANGED")
print("Saved:", OUT)
print("DONE")
