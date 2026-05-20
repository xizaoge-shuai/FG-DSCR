#!/usr/bin/env bash
set -euo pipefail

cd ~/FG-DSCR
source results/drtp/k8s_same_scale/sweep/best_fg_params.env

CATS=(16 50 68 88)
EDGES=(4 6 8 10 12 14 16 18)
REQS=(200 400 600 800 1000 1200 1500 2000)

ROOT=results/drtp/k8s_same_scale/fig3_scale
mkdir -p "$ROOT" results/drtp/k8s_same_scale/logs

for CAT in "${CATS[@]}"
do
  for EDGE in "${EDGES[@]}"
  do
    for REQ in "${REQS[@]}"
    do
      CASE=cases/drtp_scale_nodes/drtp_img${CAT}_nodes${EDGE}_cache1024mb_${REQ}.json

      if [ ! -f "$CASE" ]; then
        echo "[MISSING] $CASE"
        continue
      fi

      OUT=$ROOT/fg_cat${CAT}_edge${EDGE}_req${REQ}.json
      echo "[FIG3] cat=$CAT edge=$EDGE req=$REQ"
      bash scripts/k8s_same_scale/run_one.sh fg "$CASE" "$OUT"
    done
  done
done
