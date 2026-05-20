#!/usr/bin/env bash
set -euo pipefail

cd ~/FG-DSCR
source results/drtp/k8s_same_scale/sweep/best_fg_params.env

CACHES=(0 128 256 384 512 640 768 896 1024)
REQS=(200 300 400 500 600 700 800 900 1000)
METHODS=(ilrsa lrs gahrl orr lasa fg_orig fg)

ROOT=results/drtp/k8s_same_scale/fig2_cache
CASE_ROOT=results/drtp/k8s_same_scale/cases/fig2_cache
mkdir -p "$ROOT" "$CASE_ROOT" results/drtp/k8s_same_scale/logs

for CACHE in "${CACHES[@]}"
do
  for REQ in "${REQS[@]}"
  do
    BASE=cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_${REQ}.json
    CASE=$CASE_ROOT/case_cache${CACHE}_req${REQ}.json

    if [ ! -f "$BASE" ]; then
      echo "[MISSING] $BASE"
      continue
    fi

    python3 scripts/k8s_same_scale/make_case_variant.py \
      --base "$BASE" \
      --out "$CASE" \
      --hetero-cache-mean "$CACHE" \
      --bandwidth-env default

    for M in "${METHODS[@]}"
    do
      OUT=$ROOT/${M}_cache${CACHE}_req${REQ}.json
      echo "[FIG2] method=$M cache=$CACHE req=$REQ"
      bash scripts/k8s_same_scale/run_one.sh "$M" "$CASE" "$OUT"
    done
  done
done
