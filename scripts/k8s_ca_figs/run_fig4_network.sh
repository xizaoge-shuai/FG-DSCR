#!/usr/bin/env bash
set -euo pipefail

ENVS=(homo_good homo_bad hetero_good hetero_bad)
CACHES=(1024 1152 1280 1408 1536 1664 1792 2048)
REQS=(200 300 400 500 600 700 800 900 1000)

ROOT=results/drtp/k8s_ca_figs/fig4_network
VARROOT=results/drtp/k8s_ca_figs/cases/fig4_network
mkdir -p "$ROOT" "$VARROOT"

for ENV in "${ENVS[@]}"
do
  for CACHE in "${CACHES[@]}"
  do
    for REQ in "${REQS[@]}"
    do
      BASE=cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_${REQ}.json

      if [ ! -f "$BASE" ]; then
        echo "[MISSING] $BASE"
        continue
      fi

      CASE=$VARROOT/case_${ENV}_cache${CACHE}_req${REQ}.json

      python3 scripts/k8s_ca_figs/make_case_variant.py \
        --base "$BASE" \
        --out "$CASE" \
        --hetero-cache-mean "$CACHE" \
        --bandwidth-env "$ENV" >/dev/null

      OUT=$ROOT/fg_${ENV}_cache${CACHE}_req${REQ}.json
      LOG=results/drtp/k8s_ca_figs/logs/fig4_${ENV}_cache${CACHE}_req${REQ}.log

      echo "[FIG4] env=$ENV cache=$CACHE req=$REQ"
      /usr/bin/time -v scripts/k8s_ca_figs/run_one_k8s_ca.sh fg "$CASE" "$OUT" \
        > "$LOG" 2>&1
    done
  done
done

python3 scripts/k8s_ca_figs/summarize_results.py \
  --root "$ROOT" \
  --out results/drtp/k8s_ca_figs/summaries/fig4_network.csv
