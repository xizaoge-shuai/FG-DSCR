#!/usr/bin/env bash
set -euo pipefail

CACHES=(1024 1152 1280 1408 1536 1664 1792 2048)
REQS=(200 300 400 500 600 700 800 900 1000)
METHODS=(ilrsa lrs gahrl orr lasa fg)

ROOT=results/drtp/k8s_ca_figs/fig2_cache
VARROOT=results/drtp/k8s_ca_figs/cases/fig2_cache
mkdir -p "$ROOT" "$VARROOT"

for CACHE in "${CACHES[@]}"
do
  for REQ in "${REQS[@]}"
  do
    BASE=cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_${REQ}.json
    CASE=$VARROOT/case_cache${CACHE}_req${REQ}.json

    if [ ! -f "$BASE" ]; then
      echo "[MISSING] $BASE"
      continue
    fi

    python3 scripts/k8s_ca_figs/make_case_variant.py \
      --base "$BASE" \
      --out "$CASE" \
      --hetero-cache-mean "$CACHE" \
      --bandwidth-env default >/dev/null

    for M in "${METHODS[@]}"
    do
      OUT=$ROOT/${M}_cache${CACHE}_req${REQ}.json
      LOG=results/drtp/k8s_ca_figs/logs/fig2_${M}_cache${CACHE}_req${REQ}.log

      echo "[FIG2] method=$M cache=$CACHE req=$REQ"
      /usr/bin/time -v scripts/k8s_ca_figs/run_one_k8s_ca.sh "$M" "$CASE" "$OUT" \
        > "$LOG" 2>&1
    done
  done
done

python3 scripts/k8s_ca_figs/summarize_results.py \
  --root "$ROOT" \
  --out results/drtp/k8s_ca_figs/summaries/fig2_cache.csv
