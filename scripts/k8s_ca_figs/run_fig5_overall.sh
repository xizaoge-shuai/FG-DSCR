#!/usr/bin/env bash
set -euo pipefail

REQS=(200 300 400 500 600 700 800 900 1000)
METHODS=(ilrsa lrs gahrl orr lasa fg)

ROOT=results/drtp/k8s_ca_figs/fig5_overall
mkdir -p "$ROOT"

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
    LOG=results/drtp/k8s_ca_figs/logs/fig5_${M}_${REQ}.log

    echo "[FIG5] method=$M req=$REQ"
    /usr/bin/time -v scripts/k8s_ca_figs/run_one_k8s_ca.sh "$M" "$CASE" "$OUT" \
      > "$LOG" 2>&1
  done
done

python3 scripts/k8s_ca_figs/summarize_results.py \
  --root "$ROOT" \
  --out results/drtp/k8s_ca_figs/summaries/fig5_overall.csv
