#!/usr/bin/env bash
set -euo pipefail

cd ~/FG-DSCR

STEP=${1:-10}
REQS=($(seq 680 "$STEP" 720))
METHODS=(fg)

CASE_DIR=cases/drtp_cache_only_sweep_88_prefix
OUT_DIR=results/drtp/k8s_same_scale/fig5_overall_prefix_fine_680_720_step${STEP}
LOG_DIR=results/drtp/k8s_same_scale/logs/fig5_overall_prefix_fine_680_720_step${STEP}

mkdir -p "$OUT_DIR" "$LOG_DIR"

if [ -f results/drtp/k8s_same_scale/sweep/best_fg_edgeca_params.env ]; then
  source results/drtp/k8s_same_scale/sweep/best_fg_edgeca_params.env
elif [ -f results/drtp/k8s_same_scale/sweep/best_fg_params.env ]; then
  source results/drtp/k8s_same_scale/sweep/best_fg_params.env
fi

for REQ in "${REQS[@]}"
do
  CASE=$CASE_DIR/drtp_img88_cacheonly_1024mb_${REQ}.json
  for M in "${METHODS[@]}"
  do
    OUT=$OUT_DIR/${M}_prefix_req${REQ}.json
    LOG=$LOG_DIR/${M}_prefix_req${REQ}.log

    if [ -f "$OUT" ]; then
      echo "[SKIP] $OUT"
      continue
    fi

    echo "[RUN] method=$M req=$REQ"
    bash scripts/k8s_same_scale/run_one.sh "$M" "$CASE" "$OUT" > "$LOG" 2>&1
  done
done

echo "[DONE] fine prefix 680-720 step=$STEP"
