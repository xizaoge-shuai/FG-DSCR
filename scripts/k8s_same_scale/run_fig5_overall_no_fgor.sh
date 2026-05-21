#!/usr/bin/env bash
set -euo pipefail

cd ~/FG-DSCR

# FG-DSCR-GC 使用 K8s-aware selected 参数
if [ -f results/drtp/k8s_same_scale/sweep/best_fg_params.env ]; then
  source results/drtp/k8s_same_scale/sweep/best_fg_params.env
fi

REQS=(200 300 400 500 600 700 800 900 1000)
METHODS=(ilrsa lrs gahrl orr lasa fg)

ROOT=results/drtp/k8s_same_scale/fig5_overall_no_fgor
LOG_ROOT=results/drtp/k8s_same_scale/logs/fig5_overall_no_fgor

mkdir -p "$ROOT" "$LOG_ROOT"

for REQ in "${REQS[@]}"
do
  CASE=cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_${REQ}.json

  if [ ! -f "$CASE" ]; then
    echo "[MISSING CASE] $CASE"
    continue
  fi

  for M in "${METHODS[@]}"
  do
    OUT=$ROOT/${M}_cache1024_req${REQ}.json
    LOG=$LOG_ROOT/${M}_req${REQ}.log

    if [ -f "$OUT" ]; then
      echo "[SKIP] $OUT"
      continue
    fi

    echo "[FIG5] method=$M req=$REQ"
    bash scripts/k8s_same_scale/run_one.sh "$M" "$CASE" "$OUT" > "$LOG" 2>&1
  done
done

echo "[DONE] Fig.5 overall no FG-orig"
