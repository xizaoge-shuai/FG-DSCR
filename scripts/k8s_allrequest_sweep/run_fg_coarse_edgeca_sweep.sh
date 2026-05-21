#!/usr/bin/env bash
set -euo pipefail

cd ~/FG-DSCR

REQS=(300 600 1000)

INITS=(resource_asc arrival)
FRAGS=(0 2 5 8 10 12 15 20 30)
AFFS=(0 0.02 0.05 0.10 0.15)
GLFS=(0.65 0.75 0.85 0.95)

ROOT=results/drtp/k8s_same_scale/k8s_allrequest_sweep_fg/coarse
LOG_ROOT=results/drtp/k8s_same_scale/logs/k8s_allrequest_sweep_fg/coarse

mkdir -p "$ROOT" "$LOG_ROOT"

for INIT in "${INITS[@]}"
do
  for FRAG in "${FRAGS[@]}"
  do
    for AFF in "${AFFS[@]}"
    do
      for GLF in "${GLFS[@]}"
      do
        TAG=init${INIT}_frag${FRAG}_aff${AFF}_glf${GLF}
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

          echo "[FG-COARSE] tag=$TAG req=$REQ"

          FG_INIT_POLICY="$INIT" \
          INIT_POLICY="$INIT" \
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
  done
done

echo "[DONE] coarse edge-like CA FG sweep"
