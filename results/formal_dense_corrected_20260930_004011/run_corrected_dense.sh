#!/usr/bin/env bash

set -euo pipefail

cd "$HOME/FG-DSCR"

ROOT=$(cat results/LATEST_FORMAL_DENSE_CORRECTED)

export PYTHONHASHSEED=0
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"

SEED=1

WAN_DEFAULT=250
PEER_DEFAULT=12.5

CORE=1000
DOMAIN=300
ACCESS=200

REG_LAT=40
INTER_LAT=10
INTRA_LAT=2

PULSE_BUDGET=2048
PULSE_REFILL=0.50

SCOUT_ALPHA=1
SCOUT_BETA=4
SCOUT_GAMMA=0.5

KEEP_V=3e6
KEEP_BUDGET=150


run_baselines()
{
    OUT="$1"
    CASE="$2"
    WAN="$3"
    PEER="$4"
    RLAT="$5"
    ILAT="$6"
    INLAT="$7"
    RESUME="${8:-0}"

    mkdir -p "$OUT"

    EXTRA=()

    if [ "$RESUME" = "1" ]
    then
        EXTRA+=(--resume)
    fi

    python -u \
      -m secon_exp.experiments.run_scale_baselines_v2 \
      --cases "$CASE" \
      --seeds "$SEED" \
      --wan "$WAN" \
      --warmup-fraction 0.5 \
      --core-capacity "$CORE" \
      --domain-capacity "$DOMAIN" \
      --access-capacity "$ACCESS" \
      --peer-upload "$PEER" \
      --registry-latency-ms "$RLAT" \
      --inter-domain-latency-ms "$ILAT" \
      --intra-domain-latency-ms "$INLAT" \
      --baselines \
        registry \
        dragonfly_style \
        peersync_style \
        metapipe_reactive_style \
        ilrsa_style \
      "${EXTRA[@]}" \
      --out "$OUT"
}


run_cider()
{
    STATE="$1"
    CASE="$2"
    WAN="$3"
    PEER="$4"
    RLAT="$5"
    ILAT="$6"
    INLAT="$7"
    V="$8"
    OUT="$9"

    rm -rf "$OUT"

    python -u \
      -m secon_exp.experiments.run_cider_mainline \
      --state-root "$STATE" \
      --cases "$CASE" \
      --seed "$SEED" \
      --wan "$WAN" \
      --core-capacity "$CORE" \
      --domain-capacity "$DOMAIN" \
      --access-capacity "$ACCESS" \
      --peer-upload "$PEER" \
      --registry-latency-ms "$RLAT" \
      --inter-domain-latency-ms "$ILAT" \
      --intra-domain-latency-ms "$INLAT" \
      --pulse-budget-mb "$PULSE_BUDGET" \
      --pulse-max-transfers 0 \
      --pulse-refill-ratio "$PULSE_REFILL" \
      --scout-alpha "$SCOUT_ALPHA" \
      --scout-beta "$SCOUT_BETA" \
      --scout-gamma "$SCOUT_GAMMA" \
      --scout-source-concurrency 0 \
      --keep-v "$V" \
      --keep-upper-budget-mb-s "$KEEP_BUDGET" \
      --ablation full \
      --out "$OUT"
}


############################################################
# 1. SCALE
############################################################

echo
echo "############################################################"
echo "# CORRECTED SCALE"
echo "############################################################"

SCALE_CASES=()

for N in 20 25 30 35 40 45 50
do
    D=$((N / 5))
    R=$((N * 20))

    CASE="cases/cider_scale_dense_arrival_corrected/cider_nodes${N}_domains${D}_req${R}_weak.json"

    test -f "$CASE"

    SCALE_CASES+=("$CASE")
done

SCALE_BASE="$ROOT/scale/baselines"

python -u \
  -m secon_exp.experiments.run_scale_baselines_v2 \
  --cases "${SCALE_CASES[@]}" \
  --seeds "$SEED" \
  --wan "$WAN_DEFAULT" \
  --warmup-fraction 0.5 \
  --core-capacity "$CORE" \
  --domain-capacity "$DOMAIN" \
  --access-capacity "$ACCESS" \
  --peer-upload "$PEER_DEFAULT" \
  --registry-latency-ms "$REG_LAT" \
  --inter-domain-latency-ms "$INTER_LAT" \
  --intra-domain-latency-ms "$INTRA_LAT" \
  --baselines \
    registry \
    dragonfly_style \
    peersync_style \
    metapipe_reactive_style \
    ilrsa_style \
  --out "$SCALE_BASE"

for CASE in "${SCALE_CASES[@]}"
do
    NAME=$(basename "$CASE" .json)

    echo
    echo "===== CIDER SCALE $NAME ====="

    run_cider \
      "$SCALE_BASE" \
      "$CASE" \
      "$WAN_DEFAULT" \
      "$PEER_DEFAULT" \
      "$REG_LAT" \
      "$INTER_LAT" \
      "$INTRA_LAT" \
      "$KEEP_V" \
      "$ROOT/scale/cider_${NAME}"
done


############################################################
# Fixed correct N40 state
############################################################

CASE40="cases/cider_scale_dense_arrival_corrected/cider_nodes40_domains8_req800_weak.json"

STATE40="$SCALE_BASE/states/cider_nodes40_domains8_req800_weak__seed1.json"

test -f "$STATE40"


############################################################
# 2. WAN
############################################################

for WAN in \
    125 \
    187.5 \
    250 \
    312.5 \
    375 \
    437.5 \
    500
do
    TAG=${WAN//./p}

    SDIR="$ROOT/wan/$TAG"
    BOUT="$SDIR/baselines"

    mkdir -p "$BOUT/states"

    cp "$STATE40" "$BOUT/states/"

    echo
    echo "===== WAN=$WAN ====="

    run_baselines \
      "$BOUT" "$CASE40" \
      "$WAN" "$PEER_DEFAULT" \
      "$REG_LAT" "$INTER_LAT" "$INTRA_LAT" \
      1

    run_cider \
      "$BOUT" "$CASE40" \
      "$WAN" "$PEER_DEFAULT" \
      "$REG_LAT" "$INTER_LAT" "$INTRA_LAT" \
      "$KEEP_V" \
      "$SDIR/cider"
done


############################################################
# 3. PEER
############################################################

for PEER in \
    12.5 \
    18.75 \
    25 \
    31.25 \
    37.5 \
    43.75 \
    50
do
    TAG=${PEER//./p}

    SDIR="$ROOT/peer/$TAG"
    BOUT="$SDIR/baselines"

    mkdir -p "$BOUT/states"

    cp "$STATE40" "$BOUT/states/"

    echo
    echo "===== PEER=$PEER ====="

    run_baselines \
      "$BOUT" "$CASE40" \
      "$WAN_DEFAULT" "$PEER" \
      "$REG_LAT" "$INTER_LAT" "$INTRA_LAT" \
      1

    run_cider \
      "$BOUT" "$CASE40" \
      "$WAN_DEFAULT" "$PEER" \
      "$REG_LAT" "$INTER_LAT" "$INTRA_LAT" \
      "$KEEP_V" \
      "$SDIR/cider"
done


############################################################
# 4. RTT
############################################################

for SPEC in \
    "0p50 20 5 1" \
    "0p75 30 7.5 1.5" \
    "1p00 40 10 2" \
    "1p25 50 12.5 2.5" \
    "1p50 60 15 3" \
    "1p75 70 17.5 3.5" \
    "2p00 80 20 4"
do
    set -- $SPEC

    TAG="$1"
    RLAT="$2"
    ILAT="$3"
    INLAT="$4"

    SDIR="$ROOT/rtt/$TAG"
    BOUT="$SDIR/baselines"

    mkdir -p "$BOUT/states"

    cp "$STATE40" "$BOUT/states/"

    echo
    echo "===== RTT=$TAG ====="

    run_baselines \
      "$BOUT" "$CASE40" \
      "$WAN_DEFAULT" "$PEER_DEFAULT" \
      "$RLAT" "$ILAT" "$INLAT" \
      1

    run_cider \
      "$BOUT" "$CASE40" \
      "$WAN_DEFAULT" "$PEER_DEFAULT" \
      "$RLAT" "$ILAT" "$INLAT" \
      "$KEEP_V" \
      "$SDIR/cider"
done


############################################################
# 5. LOAD
############################################################

for TAG in \
    0p5 \
    1p0 \
    1p5 \
    2p0 \
    2p5 \
    3p0 \
    3p5 \
    4p0
do
    CASE="cases/cider_load_dense_corrected_${TAG}x/cider_nodes40_domains8_req800_weak.json"

    test -f "$CASE"

    SDIR="$ROOT/load/$TAG"
    BOUT="$SDIR/baselines"

    echo
    echo "===== LOAD=$TAG ====="

    run_baselines \
      "$BOUT" "$CASE" \
      "$WAN_DEFAULT" "$PEER_DEFAULT" \
      "$REG_LAT" "$INTER_LAT" "$INTRA_LAT" \
      0

    run_cider \
      "$BOUT" "$CASE" \
      "$WAN_DEFAULT" "$PEER_DEFAULT" \
      "$REG_LAT" "$INTER_LAT" "$INTRA_LAT" \
      "$KEEP_V" \
      "$SDIR/cider"
done


############################################################
# 6. KEEP V
############################################################

for V in \
    3e6 \
    5e6 \
    1e7 \
    2e7 \
    3e7 \
    5e7 \
    7e7 \
    1e8
do
    echo
    echo "===== KEEP V=$V ====="

    run_cider \
      "$SCALE_BASE" \
      "$CASE40" \
      "$WAN_DEFAULT" \
      "$PEER_DEFAULT" \
      "$REG_LAT" \
      "$INTER_LAT" \
      "$INTRA_LAT" \
      "$V" \
      "$ROOT/keep_v/V_${V}/cider"
done


date > "$ROOT/FINISHED.txt"

echo
echo "############################################################"
echo "# CORRECTED DENSE EXPERIMENTS FINISHED"
echo "############################################################"
echo "ROOT=$ROOT"
