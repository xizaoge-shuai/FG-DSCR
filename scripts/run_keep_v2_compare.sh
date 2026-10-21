#!/usr/bin/env bash

set -euo pipefail

cd "$(dirname "$0")/.."

export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1

ROOT="${1:?Need output directory}"
ROOT="$(realpath -m "$ROOT")"

# 直接复用修正后的容量实验状态
BASE_ROOT="$(cat results/LATEST_KEEP_CAPACITY_CORRECTED)"
BASE_ROOT="$(realpath "$BASE_ROOT")"

NAME="cider_nodes40_domains8_req800_weak"

mkdir -p "$ROOT"

echo "SOURCE=$BASE_ROOT"
echo "OUTPUT=$ROOT"

# 检查已有 state、baseline 是否齐全
for SCALE in 0p5 1p0; do

    SCENARIO="${NAME}__cap_${SCALE}x"

    test -s "$BASE_ROOT/cases/$SCENARIO.json"
    test -s "$BASE_ROOT/states/${SCENARIO}__seed1.json"

    for METHOD in keep warmup_frequency; do
        test -s \
          "$BASE_ROOT/runs/$SCENARIO/$METHOD/raw.csv"
    done

done

{
    echo "DATE=$(date -Is)"
    echo "GIT=$(git rev-parse HEAD 2>/dev/null || echo unknown)"
    echo "BASE_ROOT=$BASE_ROOT"

    sha256sum \
      scripts/review_keep_v2.py \
      scripts/review_mechanisms.py

} > "$ROOT/manifest.txt"


# ==========================================================
# 只跑两个容量、两个 KEEP-v2 版本，共四次
# ==========================================================

for SCALE in 0p5 1p0; do

    SCENARIO="${NAME}__cap_${SCALE}x"

    CASE="$BASE_ROOT/cases/$SCENARIO.json"

    for METHOD in v2_no_comm v2_full; do

        OUT="$ROOT/runs/$SCENARIO/$METHOD"

        mkdir -p "$OUT"

        echo "[$(date -Is)] START $SCALE $METHOD" \
          | tee -a "$ROOT/progress.txt"

        if [[ -s "$OUT/raw.csv" && -s "$OUT/review_diag.json" ]]; then
            echo "[SKIP] Existing result"
            continue
        fi

        if ! python -u -m scripts.review_keep_v2 \
          --algorithm "$METHOD" \
          --diagnostics "$OUT/review_diag.json" \
          -- \
          --state-root "$BASE_ROOT/states" \
          --cases "$CASE" \
          --seed 1 \
          --wan 250 \
          --core-capacity 1000 \
          --domain-capacity 300 \
          --access-capacity 200 \
          --peer-upload 12.5 \
          --registry-latency-ms 40 \
          --inter-domain-latency-ms 10 \
          --intra-domain-latency-ms 2 \
          --pulse-budget-mb 2048 \
          --pulse-max-transfers 0 \
          --pulse-refill-ratio 0.5 \
          --scout-alpha 1 \
          --scout-beta 4 \
          --scout-gamma 0.5 \
          --scout-source-concurrency 0 \
          --keep-v 3e6 \
          --keep-upper-budget-mb-s 150 \
          --ablation full \
          --out "$OUT" \
          > "$OUT/run.log" 2>&1
        then

            echo "[FAILED] $SCALE $METHOD"

            tail -60 "$OUT/run.log"

            exit 1
        fi

        echo "[$(date -Is)] DONE $SCALE $METHOD" \
          | tee -a "$ROOT/progress.txt"

    done
done


# ==========================================================
# 合并已有的原版 KEEP / Frequency 与新实验
# ==========================================================

python - "$BASE_ROOT" "$ROOT" "$NAME" <<'PY'
import csv
import sys
from pathlib import Path

base = Path(sys.argv[1])
root = Path(sys.argv[2])
name = sys.argv[3]

rows = []

for scale in ("0p5", "1p0"):

    case = f"{name}__cap_{scale}x"

    for method in (
        "keep",
        "warmup_frequency",
        "v2_no_comm",
        "v2_full",
    ):

        source = (
            base
            if method in ("keep", "warmup_frequency")
            else root
        )

        path = (
            source
            / "runs"
            / case
            / method
            / "raw.csv"
        )

        with path.open(
            encoding="utf-8",
            newline="",
        ) as f:
            r = next(csv.DictReader(f))

        row = {
            "capacity": (
                "0.5x" if scale == "0p5" else "1x"
            ),
            "method": method,
            "mean_s": float(r["mean_ready_s"]),
            "p95_s": float(r["p95_ready_s"]),
            "upper_mb": (
                float(r["registry_mb"])
                + float(r["cross_domain_peer_mb"])
            ),
            "keep_control_ms": float(r["keep_ms"]),
            "accepted": int(r["accepted"]),
            "source": (
                "reused" if source == base else "new"
            ),
        }

        rows.append(row)

        print(
            f"{row['capacity']:<6}"
            f"{method:<20}"
            f"Mean={row['mean_s']:9.3f} "
            f"P95={row['p95_s']:9.3f} "
            f"Upper={row['upper_mb']:10.1f} "
            f"KEEPms={row['keep_control_ms']:10.2f}"
        )

out = root / "summary.csv"

with out.open(
    "w",
    encoding="utf-8",
    newline="",
) as f:

    writer = csv.DictWriter(
        f,
        fieldnames=list(rows[0]),
    )

    writer.writeheader()
    writer.writerows(rows)


print("\n====== RELATIVE TO ORIGINAL KEEP ======")

for scale in ("0.5x", "1x"):

    group = {
        r["method"]: r
        for r in rows
        if r["capacity"] == scale
    }

    print("\nCAPACITY:", scale)

    for method in (
        "warmup_frequency",
        "v2_no_comm",
        "v2_full",
    ):

        changes = []

        for metric in (
            "mean_s",
            "p95_s",
            "upper_mb",
        ):

            old = group["keep"][metric]
            new = group[method][metric]

            changes.append(
                100.0 * (new / old - 1.0)
            )

        print(
            f"{method:<20}"
            f"Mean={changes[0]:+8.2f}% "
            f"P95={changes[1]:+8.2f}% "
            f"Upper={changes[2]:+8.2f}%"
        )

print("\nSaved:", out)
PY

date -Is > "$ROOT/FINISHED.txt"

echo "[ALL FINISHED] $ROOT/summary.csv"
