#!/usr/bin/env bash
set -euo pipefail

cd ~/FG-DSCR
source results/drtp/k8s_same_scale/sweep/best_fg_params.env

MODES=(homo hetero)
CACHES=(0 128 256 384 512 640 768 896 1024)
REQS=(200 300 400 500 600 700 800 900 1000)
METHODS=(ilrsa lrs gahrl orr lasa fg_orig fg)

ROOT=results/drtp/k8s_same_scale/fig2_cache_homohetero
CASE_ROOT=results/drtp/k8s_same_scale/cases/fig2_cache_homohetero

mkdir -p "$ROOT" "$CASE_ROOT" results/drtp/k8s_same_scale/logs

for MODE in "${MODES[@]}"
do
  for CACHE in "${CACHES[@]}"
  do
    for REQ in "${REQS[@]}"
    do
      BASE=cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_${REQ}.json
      CASE=$CASE_ROOT/case_${MODE}_cache${CACHE}_req${REQ}.json

      if [ ! -f "$BASE" ]; then
        echo "[MISSING BASE] $BASE"
        continue
      fi

      if [ ! -f "$CASE" ]; then
        python3 scripts/k8s_same_scale/make_case_variant.py \
          --base "$BASE" \
          --out "$CASE" \
          --hetero-cache-mean "$CACHE" \
          --cache-mode "$MODE" \
          --bandwidth-env default
      fi

      for M in "${METHODS[@]}"
      do
        OUT=$ROOT/${M}_${MODE}_cache${CACHE}_req${REQ}.json
        if [ -f "$OUT" ]; then
          echo "[SKIP] $OUT"
          continue
        fi

        echo "[FIG2] mode=$MODE method=$M cache=$CACHE req=$REQ"
        bash scripts/k8s_same_scale/run_one.sh "$M" "$CASE" "$OUT"
      done
    done
  done
done
