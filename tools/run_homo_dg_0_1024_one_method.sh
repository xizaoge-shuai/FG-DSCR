#!/usr/bin/env bash
set -u

METHOD="$1"
CASE_LIST="${2:-/tmp/homo_dg_0_1024_cases.txt}"

cd ~/FG-DSCR

OUTROOT="results/drtp/final_exp/homo_dg_0_1024_${METHOD}"
LOGROOT="results/drtp/final_exp_logs/homo_dg_0_1024_${METHOD}"
TIMEROOT="results/drtp/final_exp_time/homo_dg_0_1024_${METHOD}"

mkdir -p "$OUTROOT" "$LOGROOT" "$TIMEROOT"

while read CASE; do
  [ -z "$CASE" ] && continue

  BASE=$(basename "$CASE" .json)
  OUT="$OUTROOT/${BASE}.json"
  LOG="$LOGROOT/log_${BASE}.txt"
  TIMEOUT="$TIMEROOT/time_${BASE}.txt"

  if [ -f "$OUT" ]; then
    echo "[SKIP existing] $OUT"
    continue
  fi

  echo "===================================================================================================="
  echo "[RUN ${METHOD} homo 0-1024] $CASE"
  echo "OUT=$OUT"
  echo "===================================================================================================="

  if [ "$METHOD" = "lrscheduler" ]; then
    /usr/bin/time -v -o "$TIMEOUT" \
      nice -n 10 ionice -c 2 -n 7 \
      python3 -u scripts/run_recent_layer_baselines.py \
        --case "$CASE" \
        --out "$OUT" \
        --algo lrscheduler \
        --order cache \
      2>&1 | tee "$LOG"

  elif [ "$METHOD" = "orr" ]; then
    /usr/bin/time -v -o "$TIMEOUT" \
      nice -n 10 ionice -c 2 -n 7 \
      python3 -u scripts/run_recent_layer_baselines.py \
        --case "$CASE" \
        --out "$OUT" \
        --algo orr \
        --order cache \
      2>&1 | tee "$LOG"

  elif [ "$METHOD" = "gahrl" ]; then
    /usr/bin/time -v -o "$TIMEOUT" \
      nice -n 10 ionice -c 2 -n 7 \
      python3 -u scripts/run_gahrl_objective_greedy.py \
        --case "$CASE" \
        --out "$OUT" \
        --order cache \
      2>&1 | tee "$LOG"

  elif [ "$METHOD" = "lasa" ]; then
    /usr/bin/time -v -o "$TIMEOUT" \
      nice -n 10 ionice -c 2 -n 7 \
      python3 -u scripts/run_lasa_reimpl.py \
        --case "$CASE" \
        --out "$OUT" \
        --alpha 0.5 \
        --cache-policy lru \
        --algo-name LASA-paper-reimpl \
        --enforce-resource-capacity \
      2>&1 | tee "$LOG"

  elif [ "$METHOD" = "ilrsa" ]; then
    /usr/bin/time -v -o "$TIMEOUT" \
      nice -n 10 ionice -c 2 -n 7 \
      python3 -u scripts/ilrsa_reference_impl.py \
        --case "$CASE" \
        --out "$OUT" \
        --alpha 0.5 \
        --knapsack greedy \
        --seed 42 \
      2>&1 | tee "$LOG"

  elif [ "$METHOD" = "fg" ]; then
    EXTRA_ARGS=""

    python3 scripts/fg_dscr.py --help | grep -q -- "--lambda-cache-core" && \
      EXTRA_ARGS="$EXTRA_ARGS --lambda-cache-core 0.0"

    python3 scripts/fg_dscr.py --help | grep -q -- "--cache-core-ratio" && \
      EXTRA_ARGS="$EXTRA_ARGS --cache-core-ratio 0.90"

    python3 scripts/fg_dscr.py --help | grep -q -- "--bw-gamma" && \
      EXTRA_ARGS="$EXTRA_ARGS --bw-gamma 1.0"

    python3 scripts/fg_dscr.py --help | grep -q -- "--cache-bw-eta" && \
      EXTRA_ARGS="$EXTRA_ARGS --cache-bw-eta 0.0 --cache-bw-ref 100"

    /usr/bin/time -v -o "$TIMEOUT" \
      nice -n 10 ionice -c 2 -n 7 \
      python3 -u scripts/fg_dscr.py \
        --case "$CASE" \
        --out "$OUT" \
        --beam 1 \
        --lambda-cong 1.0 \
        --lambda-frag 0.1 \
        --lambda-aff 0.6 \
        --lambda-task-load 0.10 \
        --cache-policy pgdsf \
        --order-policy dynamic_state \
        --greedy-load-factor 0.9 \
        --algo-name FG-DSCR-GC \
        $EXTRA_ARGS \
      2>&1 | tee "$LOG"

  else
    echo "[ERROR] unknown METHOD=$METHOD"
    exit 1
  fi

done < "$CASE_LIST"
