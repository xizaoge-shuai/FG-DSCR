#!/usr/bin/env bash
set -euo pipefail

cd ~/FG-DSCR
source results/drtp/k8s_same_scale/sweep/best_fg_params.env

REQS=(200 300 400 500 600 700 800 900 1000)
METHODS=(fg_orig fg wo_frag)

ROOT=results/drtp/k8s_same_scale/fig1_phase1
mkdir -p "$ROOT" results/drtp/k8s_same_scale/logs

for REQ in "${REQS[@]}"
do
  CASE=cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_${REQ}.json
  if [ ! -f "$CASE" ]; then
    echo "[MISSING] $CASE"
    continue
  fi

  for M in "${METHODS[@]}"
  do
    OUT=$ROOT/${M}_cache1024_req${REQ}.json
    echo "[FIG1] method=$M req=$REQ"
    bash scripts/k8s_same_scale/run_one.sh "$M" "$CASE" "$OUT"
  done
done
