#!/usr/bin/env bash
set -euo pipefail

CATS=(16 50 68 88)
EDGES=(4 6 8 10 12 14 16 18)
REQS=(200 400 600 800 1000 1200 1500 2000)

ROOT=results/drtp/k8s_ca_figs/fig3_scale_fixed
mkdir -p "$ROOT"

for CAT in "${CATS[@]}"
do
  for EDGE in "${EDGES[@]}"
  do
    for REQ in "${REQS[@]}"
    do
      CASE=cases/drtp_scale_nodes/drtp_img${CAT}_nodes${EDGE}_cache1024mb_${REQ}.json

      if [ ! -f "$CASE" ]; then
        echo "[MISSING] $CASE"
        continue
      fi

      OUT=$ROOT/fg_cat${CAT}_edge${EDGE}_req${REQ}.json
      LOG=results/drtp/k8s_ca_figs/logs/fig3_fixed_cat${CAT}_edge${EDGE}_req${REQ}.log

      echo "[FIG3-FIXED] cat=$CAT edge=$EDGE req=$REQ"
      /usr/bin/time -v scripts/k8s_ca_figs/run_one_k8s_ca.sh fg "$CASE" "$OUT" \
        > "$LOG" 2>&1
    done
  done
done

python3 scripts/k8s_ca_figs/summarize_results.py \
  --root "$ROOT" \
  --out results/drtp/k8s_ca_figs/summaries/fig3_scale_fixed.csv
