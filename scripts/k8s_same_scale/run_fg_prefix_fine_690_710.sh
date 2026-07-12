#!/usr/bin/env bash
set -euo pipefail

cd ~/FG-DSCR

if [ -f results/drtp/k8s_same_scale/sweep/best_fg_edgeca_params.env ]; then
  source results/drtp/k8s_same_scale/sweep/best_fg_edgeca_params.env
elif [ -f results/drtp/k8s_same_scale/sweep/best_fg_params.env ]; then
  source results/drtp/k8s_same_scale/sweep/best_fg_params.env
fi

REQS=($(seq 690 1 710))

CASE_DIR=cases/drtp_cache_only_sweep_88_prefix
OUT_DIR=results/drtp/k8s_same_scale/fig5_overall_prefix_fine_690_710_step1
LOG_DIR=results/drtp/k8s_same_scale/logs/fig5_overall_prefix_fine_690_710_step1

mkdir -p "$OUT_DIR" "$LOG_DIR"

for REQ in "${REQS[@]}"
do
  CASE=$CASE_DIR/drtp_img88_cacheonly_1024mb_${REQ}.json
  OUT=$OUT_DIR/fg_prefix_req${REQ}.json
  LOG=$LOG_DIR/fg_prefix_req${REQ}.log

  if [ ! -f "$CASE" ]; then
    echo "[MISSING CASE] $CASE"
    continue
  fi

  if [ -f "$OUT" ]; then
    echo "[SKIP] $OUT"
    continue
  fi

  echo "[RUN] FG req=$REQ"
  bash scripts/k8s_same_scale/run_one.sh fg "$CASE" "$OUT" > "$LOG" 2>&1
done

echo "[DONE] FG fine 690-710 step=1"
