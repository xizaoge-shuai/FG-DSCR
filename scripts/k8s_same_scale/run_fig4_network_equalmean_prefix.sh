#!/usr/bin/env bash
set -euo pipefail

cd ~/FG-DSCR

if [ -f results/drtp/k8s_same_scale/sweep/best_fg_edgeca_params.env ]; then
  source results/drtp/k8s_same_scale/sweep/best_fg_edgeca_params.env
elif [ -f results/drtp/k8s_same_scale/sweep/best_fg_params.env ]; then
  source results/drtp/k8s_same_scale/sweep/best_fg_params.env
fi

ENVS=(homo_good homo_bad hetero_good hetero_bad)
CACHES=(0 128 256 384 512 640 768 896 1024)
REQS=(200 300 400 500 600 700 800 900 1000)

BASE_DIR=cases/drtp_cache_only_sweep_88_prefix
CASE_ROOT=results/drtp/k8s_same_scale/cases/fig4_network_equalmean_prefix
OUT_DIR=results/drtp/k8s_same_scale/fig4_network_equalmean_prefix
LOG_DIR=results/drtp/k8s_same_scale/logs/fig4_network_equalmean_prefix

mkdir -p "$CASE_ROOT" "$OUT_DIR" "$LOG_DIR"

for ENV in "${ENVS[@]}"
do
  for CACHE in "${CACHES[@]}"
  do
    for REQ in "${REQS[@]}"
    do
      BASE=$BASE_DIR/drtp_img88_cacheonly_${CACHE}mb_${REQ}.json
      CASE=$CASE_ROOT/case_${ENV}_cache${CACHE}_req${REQ}.json
      OUT=$OUT_DIR/fg_${ENV}_cache${CACHE}_req${REQ}.json
      LOG=$LOG_DIR/fg_${ENV}_cache${CACHE}_req${REQ}.log

      if [ ! -f "$BASE" ]; then
        echo "[MISSING BASE] $BASE"
        continue
      fi

      if [ ! -f "$CASE" ]; then
        python3 scripts/k8s_same_scale/make_fig4_network_equalmean_case.py \
          --base "$BASE" \
          --out "$CASE" \
          --cache-mb "$CACHE" \
          --env "$ENV"
      fi

      if [ -f "$OUT" ]; then
        echo "[SKIP] $OUT"
        continue
      fi

      echo "[FIG4-PREFIX] env=$ENV cache=$CACHE req=$REQ"
      bash scripts/k8s_same_scale/run_one.sh fg "$CASE" "$OUT" > "$LOG" 2>&1
    done
  done
done

echo "[DONE] Fig.4 equalmean prefix"
