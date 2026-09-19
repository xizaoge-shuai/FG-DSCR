#!/usr/bin/env bash
set -euo pipefail

cd ~/FG-DSCR

REQS=(300 600 1000)

# 围绕 coarse top: frag≈8/10, aff≈0.10/0.15
FRAGS=(6 8 10 12 14 16 20)
AFFS=(0 0.05 0.08 0.10 0.12 0.15 0.18 0.20)

# glf 先固定一个，避免重复跑
GLF=0.65

ROOT=results/drtp/k8s_same_scale/k8s_allrequest_sweep_fg/refine_effective
LOG_ROOT=results/drtp/k8s_same_scale/logs/k8s_allrequest_sweep_fg/refine_effective

mkdir -p "$ROOT" "$LOG_ROOT"

for FRAG in "${FRAGS[@]}"
do
  for AFF in "${AFFS[@]}"
  do
    TAG=frag${FRAG}_aff${AFF}_glf${GLF}
    OUTDIR=$ROOT/$TAG
    mkdir -p "$OUTDIR"

    for REQ in "${REQS[@]}"
    do
      CASE=cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_${REQ}.json
      OUT=$OUTDIR/fg_${TAG}_req${REQ}.json
      LOG=$LOG_ROOT/${TAG}_req${REQ}.log

      if [ ! -f "$CASE" ]; then
        echo "[MISSING] $CASE"
        continue
      fi

      if [ -f "$OUT" ]; then
        echo "[SKIP] $OUT"
        continue
      fi

      echo "[FG-REFINE-EFFECTIVE] tag=$TAG req=$REQ"

      FG_LAMBDA_FRAG="$FRAG" \
      LAMBDA_FRAG="$FRAG" \
      FG_LAMBDA_AFF="$AFF" \
      LAMBDA_AFF="$AFF" \
      FG_GREEDY_LOAD_FACTOR="$GLF" \
      GREEDY_LOAD_FACTOR="$GLF" \
      bash scripts/k8s_same_scale/run_one.sh fg "$CASE" "$OUT" > "$LOG" 2>&1
    done
  done
done

echo "[DONE] refine effective edge-like CA FG sweep"
