#!/usr/bin/env bash
set -euo pipefail

CATS=(16 50 68 88)
EDGES=(4 6 8 10 12 14 16 18)
REQS=(200 400 600 800 1000 1200 1500 2000)

ROOT=results/drtp/k8s_ca_figs/fig3_scale
VARROOT=results/drtp/k8s_ca_figs/cases/fig3_scale
mkdir -p "$ROOT" "$VARROOT"

for CAT in "${CATS[@]}"
do
  for EDGE in "${EDGES[@]}"
  do
    for REQ in "${REQS[@]}"
    do
      BASE=cases/drtp_cache_only_sweep_${CAT}/drtp_img${CAT}_cacheonly_1024mb_${REQ}.json

      if [ ! -f "$BASE" ]; then
        echo "[MISSING] $BASE"
        continue
      fi

      CASE=$VARROOT/case_cat${CAT}_edge${EDGE}_req${REQ}.json

      python3 scripts/k8s_ca_figs/make_case_variant.py \
        --base "$BASE" \
        --out "$CASE" \
        --edge-nodes "$EDGE" \
        --cache 1024 \
        --bandwidth-env default >/dev/null

      OUT=$ROOT/fg_cat${CAT}_edge${EDGE}_req${REQ}.json
      LOG=results/drtp/k8s_ca_figs/logs/fig3_cat${CAT}_edge${EDGE}_req${REQ}.log

      echo "[FIG3] cat=$CAT edge=$EDGE req=$REQ"
      /usr/bin/time -v scripts/k8s_ca_figs/run_one_k8s_ca.sh fg "$CASE" "$OUT" \
        > "$LOG" 2>&1
    done
  done
done

python3 scripts/k8s_ca_figs/summarize_results.py \
  --root "$ROOT" \
  --out results/drtp/k8s_ca_figs/summaries/fig3_scale.csv
