#!/usr/bin/env bash
set -Eeuo pipefail

cd ~/FG-DSCR

export PYTHONHASHSEED=0
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"

PY="$HOME/miniconda3/envs/fg/bin/python3.10"

CASE_ROOT=$(cat cases/LATEST_CIDER_RQ_SCALE_CASES)
RUN_ROOT=$(cat results/LATEST_CIDER_RQ3_NETWORK)

BASELINES=(registry dragonfly_style peersync_style metapipe_reactive_style ilrsa_style)

PULSE=(--pulse-budget-mb 2048 --pulse-max-transfers 0 --pulse-refill-ratio 0.50)

SCOUT=(--scout-alpha 1 --scout-beta 4 --scout-gamma 0.5 --scout-source-concurrency 0)

KEEP=(--keep-v 3e6 --keep-upper-budget-mb-s 150)

run_point() {

  local TAG="$1"
  local WAN="$2"
  local PEER="$3"

  for SEED in 1 2 3; do

    NAME="cider_nodes400_domains80_req8000_weak"

    CASE="$CASE_ROOT/seed${SEED}/weak_scale_arrival/${NAME}.json"

    OUT="$RUN_ROOT/$TAG/seed${SEED}"

    BOUT="$OUT/baselines"
    COUT="$OUT/cider"

    mkdir -p "$OUT"

    NET=(
      --wan "$WAN"
      --core-capacity 1000
      --domain-capacity 300
      --access-capacity 200
      --peer-upload "$PEER"
      --registry-latency-ms 40
      --inter-domain-latency-ms 10
      --intra-domain-latency-ms 2
    )

    echo "RQ3 $TAG SEED=$SEED"

    "$PY" -u \
      -m secon_exp.experiments.run_scale_baselines_v2 \
      --cases "$CASE" \
      --seeds "$SEED" \
      --warmup-fraction 0.5 \
      "${NET[@]}" \
      --baselines "${BASELINES[@]}" \
      --resume \
      --out "$BOUT" \
      > "$OUT/baselines.log" 2>&1

    test -s "$BOUT/raw.csv"

    "$PY" -u \
      -m secon_exp.experiments.run_cider_mainline \
      --state-root "$BOUT" \
      --cases "$CASE" \
      --seed "$SEED" \
      "${NET[@]}" \
      "${PULSE[@]}" \
      "${SCOUT[@]}" \
      "${KEEP[@]}" \
      --ablation full \
      --out "$COUT" \
      > "$OUT/cider.log" 2>&1

    test -s "$COUT/raw.csv"

  done
}

# WAN sweep: peer fixed at 12.5 MB/s
for WAN in 125 187.5 250 312.5 375 437.5 500; do

  TAG="wan_${WAN//./p}"

  run_point "$TAG" "$WAN" 12.5

done

# Peer sweep: WAN fixed at 250 MB/s
for PEER in 12.5 25 37.5 50; do

  TAG="peer_${PEER//./p}"

  run_point "$TAG" 250 "$PEER"

done

date -Is > "$RUN_ROOT/FINISHED.txt"

echo "RQ3 COMPLETED"
