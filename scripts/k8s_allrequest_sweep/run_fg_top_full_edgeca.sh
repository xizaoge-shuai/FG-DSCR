#!/usr/bin/env bash
set -euo pipefail

cd ~/FG-DSCR

SRC_RANK=${1:-results/drtp/k8s_same_scale/k8s_allrequest_sweep_fg/refine/top_tags.txt}
TOPN=${2:-10}

REQS=(200 300 400 500 600 700 800 900 1000)

ROOT=results/drtp/k8s_same_scale/k8s_allrequest_sweep_fg/top_full
LOG_ROOT=results/drtp/k8s_same_scale/logs/k8s_allrequest_sweep_fg/top_full

mkdir -p "$ROOT" "$LOG_ROOT"

head -n "$TOPN" "$SRC_RANK" | while read -r TAG
do
  [ -z "$TAG" ] && continue

  INIT=$(echo "$TAG" | sed -E 's/^init(.+)_frag[^_]+_aff[^_]+_glf[^_]+$/\1/')
  FRAG=$(echo "$TAG" | sed -E 's/.*_frag([^_]+)_aff.*/\1/')
  AFF=$(echo "$TAG" | sed -E 's/.*_aff([^_]+)_glf.*/\1/')
  GLF=$(echo "$TAG" | sed -E 's/.*_glf([^_]+)$/\1/')

  OUTDIR=$ROOT/$TAG
  mkdir -p "$OUTDIR"

  echo "[TOP-FULL] TAG=$TAG INIT=$INIT FRAG=$FRAG AFF=$AFF GLF=$GLF"

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

echo "[DONE] top-full edge-like CA FG sweep"
