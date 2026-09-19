#!/usr/bin/env bash
set -euo pipefail

cd ~/FG-DSCR

if [ -f results/drtp/k8s_same_scale/sweep/best_fg_edgeca_params.env ]; then
  source results/drtp/k8s_same_scale/sweep/best_fg_edgeca_params.env
elif [ -f results/drtp/k8s_same_scale/sweep/best_fg_params.env ]; then
  source results/drtp/k8s_same_scale/sweep/best_fg_params.env
fi

CATS=(16 50 68 88)
EDGES=(4 6 8 10 12 14 16 18)
REQS=(200 400 600 800 1000 1200 1500 2000)

CASE_DIR=cases/drtp_scale_prefix
OUT_DIR=results/drtp/k8s_same_scale/fig3_scale_prefix
LOG_DIR=results/drtp/k8s_same_scale/logs/fig3_scale_prefix

mkdir -p "$OUT_DIR" "$LOG_DIR"

for CAT in "${CATS[@]}"
do
  for EDGE in "${EDGES[@]}"
  do
    for REQ in "${REQS[@]}"
    do
      CASE=$CASE_DIR/drtp_img${CAT}_nodes${EDGE}_cache1024mb_${REQ}.json
      OUT=$OUT_DIR/fg_cat${CAT}_edge${EDGE}_req${REQ}.json
      LOG=$LOG_DIR/fg_cat${CAT}_edge${EDGE}_req${REQ}.log

      if [ ! -f "$CASE" ]; then
        echo "[MISSING CASE] $CASE"
        continue
      fi

      if [ -f "$OUT" ]; then
        echo "[SKIP] $OUT"
        continue
      fi

      echo "[FIG3-PREFIX] cat=$CAT edge=$EDGE req=$REQ"
      bash scripts/k8s_same_scale/run_one.sh fg "$CASE" "$OUT" > "$LOG" 2>&1
    done
  done
done

echo "[DONE] Fig.3 scale prefix"
