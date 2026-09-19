#!/usr/bin/env bash
set -euo pipefail

cd ~/FG-DSCR

if [ -f results/drtp/k8s_same_scale/sweep/best_fg_edgeca_params.env ]; then
  source results/drtp/k8s_same_scale/sweep/best_fg_edgeca_params.env
elif [ -f results/drtp/k8s_same_scale/sweep/best_fg_params.env ]; then
  source results/drtp/k8s_same_scale/sweep/best_fg_params.env
fi

CACHES=(0 128 256 384 512 640 768 896 1024)
REQS=(200 300 400 500 600 700 800 900 1000)
METHODS=(ilrsa lrs gahrl orr lasa fg)

CASE_DIR=cases/drtp_cache_only_sweep_88_prefix
OUT_DIR=results/drtp/k8s_same_scale/fig2_cache_prefix
LOG_DIR=results/drtp/k8s_same_scale/logs/fig2_cache_prefix

mkdir -p "$OUT_DIR" "$LOG_DIR"

for C in "${CACHES[@]}"
do
  for REQ in "${REQS[@]}"
  do
    CASE=$CASE_DIR/drtp_img88_cacheonly_${C}mb_${REQ}.json

    for M in "${METHODS[@]}"
    do
      OUT=$OUT_DIR/${M}_cache${C}_req${REQ}.json
      LOG=$LOG_DIR/${M}_cache${C}_req${REQ}.log

      if [ ! -f "$CASE" ]; then
        echo "[MISSING CASE] $CASE"
        continue
      fi

      if [ -f "$OUT" ]; then
        echo "[SKIP] $OUT"
        continue
      fi

      echo "[FIG2-PREFIX] method=$M cache=$C req=$REQ"
      bash scripts/k8s_same_scale/run_one.sh "$M" "$CASE" "$OUT" > "$LOG" 2>&1
    done
  done
done

echo "[DONE] Fig.2 prefix"
