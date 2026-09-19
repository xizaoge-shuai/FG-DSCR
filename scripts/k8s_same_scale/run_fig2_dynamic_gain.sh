#!/usr/bin/env bash
set -euo pipefail

cd ~/FG-DSCR

# 如果 best_fg_params.env 存在，就加载 FG-selected 参数
if [ -f results/drtp/k8s_same_scale/sweep/best_fg_params.env ]; then
  source results/drtp/k8s_same_scale/sweep/best_fg_params.env
fi

CACHES=(128 256 384 512 640 768 896 1024)
REQS=(200 300 400 500 600 700 800 900 1000)
METHODS=(ilrsa lrs gahrl orr lasa fg_orig fg)

ROOT=results/drtp/k8s_same_scale/fig2_dynamic_gain
CASE_ROOT=results/drtp/k8s_same_scale/cases/fig2_dynamic_gain
LOG_ROOT=results/drtp/k8s_same_scale/logs/fig2_dynamic_gain

mkdir -p "$ROOT" "$CASE_ROOT" "$LOG_ROOT"

for CACHE in "${CACHES[@]}"
do
  for REQ in "${REQS[@]}"
  do
    BASE=cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_${REQ}.json
    CASE=$CASE_ROOT/case_cache${CACHE}_req${REQ}.json

    if [ ! -f "$BASE" ]; then
      echo "[MISSING BASE] $BASE"
      continue
    fi

    if [ ! -f "$CASE" ]; then
      python3 scripts/k8s_same_scale/make_fig2_dynamic_case.py \
        --base "$BASE" \
        --out "$CASE" \
        --cache-mb "$CACHE"
    fi

    for M in "${METHODS[@]}"
    do
      OUT=$ROOT/${M}_cache${CACHE}_req${REQ}.json
      LOG=$LOG_ROOT/${M}_cache${CACHE}_req${REQ}.log

      if [ -f "$OUT" ]; then
        echo "[SKIP] $OUT"
        continue
      fi

      echo "[FIG2-DYNAMIC] method=$M cache=$CACHE req=$REQ"
      bash scripts/k8s_same_scale/run_one.sh "$M" "$CASE" "$OUT" > "$LOG" 2>&1
    done
  done
done

echo "[DONE] Fig.2 dynamic gain runs"
