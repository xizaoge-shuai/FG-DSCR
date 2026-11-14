#!/usr/bin/env bash
set -Eeuo pipefail

cd ~/FG-DSCR

export PYTHONHASHSEED=0
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"

PY="$HOME/miniconda3/envs/fg/bin/python3.10"

CASE_ROOT=$(cat cases/LATEST_CIDER_RQ_SCALE_CASES)
RQ1_ROOT=$(cat results/LATEST_CIDER_RQ1_SCALE)

RUN_ROOT=$(cat results/LATEST_CIDER_RQ2_ABLATION)

BASELINES=(registry dragonfly_style peersync_style metapipe_reactive_style ilrsa_style)

NET=(
  --wan 250
  --core-capacity 1000
  --domain-capacity 300
  --access-capacity 200
  --peer-upload 12.5
  --registry-latency-ms 40
  --inter-domain-latency-ms 10
  --intra-domain-latency-ms 2
)

PULSE=(--pulse-budget-mb 2048 --pulse-max-transfers 0 --pulse-refill-ratio 0.50)

SCOUT=(--scout-alpha 1 --scout-beta 4 --scout-gamma 0.5 --scout-source-concurrency 0)

KEEP=(--keep-v 3e6 --keep-upper-budget-mb-s 150)

for SEED in 1 2 3; do

  for SPEC in "200 40 4000" "400 80 8000" "600 120 12000"; do

    read -r N D R <<< "$SPEC"

    NAME="cider_nodes${N}_domains${D}_req${R}_weak"

    CASE="$CASE_ROOT/seed${SEED}/weak_scale_arrival/${NAME}.json"

    BOUT="$RQ1_ROOT/seed${SEED}/N${N}/baselines"

    STATE="$BOUT/states/${NAME}__seed${SEED}.json"

    test -s "$CASE"
    test -s "$BOUT/raw.csv"
    test -s "$STATE"

    for ABL in full no_pulse no_scout no_keep; do

      OUT="$RUN_ROOT/seed${SEED}/N${N}/$ABL"

      mkdir -p "$OUT"

      if [ ! -s "$OUT/raw.csv" ]; then

        echo "RQ2 $ABL N=$N SEED=$SEED"

        "$PY" -u \
          -m secon_exp.experiments.run_cider_mainline \
          --state-root "$BOUT" \
          --cases "$CASE" \
          --seed "$SEED" \
          "${NET[@]}" \
          "${PULSE[@]}" \
          "${SCOUT[@]}" \
          "${KEEP[@]}" \
          --ablation "$ABL" \
          --out "$OUT" \
          > "$OUT/run.log" 2>&1

      fi

      test -s "$OUT/raw.csv"

    done

    # Warmup-Frequency retention
    OUT="$RUN_ROOT/seed${SEED}/N${N}/warmup_frequency"

    mkdir -p "$OUT"

    if [ ! -s "$OUT/raw.csv" ]; then

      echo "RQ2 WARMUP-FREQUENCY N=$N SEED=$SEED"

      "$PY" -u -m scripts.review_mechanisms \
        --scout-mode audit \
        --cache-policy warmup_lfu \
        --diagnostics "$OUT/review_diag.json" \
        -- \
        --state-root "$BOUT" \
        --cases "$CASE" \
        --seed "$SEED" \
        "${NET[@]}" \
        "${PULSE[@]}" \
        "${SCOUT[@]}" \
        "${KEEP[@]}" \
        --ablation full \
        --out "$OUT" \
        > "$OUT/run.log" 2>&1

    fi

    test -s "$OUT/raw.csv"
    test -s "$OUT/review_diag.json"

  done
done

date -Is > "$RUN_ROOT/FINISHED.txt"

echo "RQ2 COMPLETED"
