#!/usr/bin/env bash

set -euo pipefail

cd "$HOME/FG-DSCR"

ROOT=$(cat results/LATEST_FORMAL_SCOUT_BETA_DENSE)

FORMAL=$(cat results/LATEST_FORMAL_DENSE_CORRECTED)

CASE="cases/cider_scale_dense_arrival_corrected/cider_nodes40_domains8_req800_weak.json"

STATE_ROOT="$FORMAL/scale/baselines"

test -f "$CASE"

test -f \
  "$STATE_ROOT/states/cider_nodes40_domains8_req800_weak__seed1.json"


for BETA in \
    1 \
    2 \
    3 \
    4 \
    5 \
    6 \
    7 \
    8
do
    echo
    echo "############################################################"
    echo "SCOUT BETA=$BETA"
    echo "############################################################"

    OUT="$ROOT/beta_${BETA}/cider"

    rm -rf "$OUT"

    python -u \
      -m secon_exp.experiments.run_cider_mainline \
      --state-root "$STATE_ROOT" \
      --cases "$CASE" \
      --seed 1 \
      --wan 250 \
      --core-capacity 1000 \
      --domain-capacity 300 \
      --access-capacity 200 \
      --peer-upload 12.5 \
      --registry-latency-ms 40 \
      --inter-domain-latency-ms 10 \
      --intra-domain-latency-ms 2 \
      --pulse-budget-mb 2048 \
      --pulse-max-transfers 0 \
      --pulse-refill-ratio 0.50 \
      --scout-alpha 1 \
      --scout-beta "$BETA" \
      --scout-gamma 0.5 \
      --scout-source-concurrency 0 \
      --keep-v 3e6 \
      --keep-upper-budget-mb-s 150 \
      --ablation full \
      --out "$OUT"
done

date > "$ROOT/FINISHED.txt"

echo
echo "############################################################"
echo "BETA DENSE FINISHED"
echo "############################################################"
