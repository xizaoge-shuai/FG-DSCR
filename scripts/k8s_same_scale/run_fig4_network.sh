#!/usr/bin/env bash
set -euo pipefail

cd ~/FG-DSCR
source results/drtp/k8s_same_scale/sweep/best_fg_params.env

ENVS=(homo_good homo_bad hetero_good hetero_bad)
CACHES=(0 128 256 384 512 640 768 896 1024)
REQS=(200 300 400 500 600 700 800 900 1000)

ROOT=results/drtp/k8s_same_scale/fig4_network
CASE_ROOT=results/drtp/k8s_same_scale/cases/fig4_network
mkdir -p "$ROOT" "$CASE_ROOT" results/drtp/k8s_same_scale/logs

for ENV in "${ENVS[@]}"
do
  for CACHE in "${CACHES[@]}"
  do
    for REQ in "${REQS[@]}"
    do
      BASE=cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_${REQ}.json
      CASE=$CASE_ROOT/case_${ENV}_cache${CACHE}_req${REQ}.json

      if [ ! -f "$BASE" ]; then
        echo "[MISSING] $BASE"
        continue
      fi

      python3 scripts/k8s_same_scale/make_case_variant.py \
        --base "$BASE" \
        --out "$CASE" \
        --hetero-cache-mean "$CACHE" \
        --bandwidth-env "$ENV"

      OUT=$ROOT/fg_${ENV}_cache${CACHE}_req${REQ}.json
      echo "[FIG4] env=$ENV cache=$CACHE req=$REQ"
      bash scripts/k8s_same_scale/run_one.sh fg "$CASE" "$OUT"
    done
  done
done
