#!/usr/bin/env bash
set -euo pipefail

cd ~/FG-DSCR

if [ -f results/drtp/k8s_same_scale/sweep/best_fg_edgeca_params.env ]; then
  source results/drtp/k8s_same_scale/sweep/best_fg_edgeca_params.env
elif [ -f results/drtp/k8s_same_scale/sweep/best_fg_params.env ]; then
  source results/drtp/k8s_same_scale/sweep/best_fg_params.env
fi

REQS=(200 300 400 500 600 700 800 900 1000)
METHODS=(ilrsa lrs gahrl orr lasa fg)

CASE_DIR=cases/drtp_cache_only_sweep_88_prefix
OUT_DIR=results/drtp/k8s_same_scale/fig5_overall_prefix
LOG_DIR=results/drtp/k8s_same_scale/logs/fig5_overall_prefix

mkdir -p "$OUT_DIR" "$LOG_DIR"

for REQ in "${REQS[@]}"
do
  CASE=$CASE_DIR/drtp_img88_cacheonly_1024mb_${REQ}.json

  for M in "${METHODS[@]}"
  do
    OUT=$OUT_DIR/${M}_prefix_req${REQ}.json
    LOG=$LOG_DIR/${M}_prefix_req${REQ}.log

    if [ ! -f "$CASE" ]; then
      echo "[MISSING CASE] $CASE"
      continue
    fi

    if [ -f "$OUT" ]; then
      echo "[SKIP] $OUT"
      continue
    fi

    echo "[FIG5-PREFIX] method=$M req=$REQ"
    bash scripts/k8s_same_scale/run_one.sh "$M" "$CASE" "$OUT" > "$LOG" 2>&1
  done
done

echo "[DONE] Fig.5 prefix"
