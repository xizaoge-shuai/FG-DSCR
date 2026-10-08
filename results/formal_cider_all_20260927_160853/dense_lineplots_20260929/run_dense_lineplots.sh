#!/usr/bin/env bash
set -euo pipefail

cd "$HOME/FG-DSCR"

ROOT=$(cat results/LATEST_FORMAL_CIDER_ALL)
DENSE="$ROOT/dense_lineplots_20260929"

mkdir -p "$DENSE"

export PYTHONHASHSEED=0
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"

# ============================================================
# 固定正式参数
# ============================================================

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

CASE40="cases/cider_weak_scale_arrival/cider_nodes40_domains8_req800_weak.json"

# 使用原正式 N=40 状态，保证 workload / placement 完全一致
BASE40="$ROOT/fig12_scale/seed1/baselines"
BASE_STATE="$BASE40/states/cider_nodes40_domains8_req800_weak__seed1.json"

if [ ! -f "$CASE40" ]; then
    echo "ERROR: CASE40 not found:"
    echo "$CASE40"
    exit 1
fi

if [ ! -f "$BASE_STATE" ]; then
    echo "ERROR: base state not found:"
    echo "$BASE_STATE"
    exit 1
fi


# ============================================================
# Baselines
# ============================================================

run_baselines()
{
    OUT="$1"
    CASE="$2"
    WAN="$3"
    PEER="$4"
    RLAT="$5"
    ILAT="$6"
    INLAT="$7"

    mkdir -p "$OUT"

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
      --resume \
      --out "$OUT"
}


# ============================================================
# CIDER
# ============================================================

run_cider()
{
    STATE="$1"
    CASE="$2"
    WAN="$3"
    PEER="$4"
    RLAT="$5"
    ILAT="$6"
    INLAT="$7"
    MODE="$8"
    V="$9"
    OUT="${10}"

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
      --ablation "$MODE" \
      --out "$OUT"
}


# ============================================================
# 每一个敏感性点都从完全相同的正式状态开始
# ============================================================

prepare_state()
{
    BOUT="$1"

    rm -rf "$BOUT"
    mkdir -p "$BOUT/states"

    cp "$BASE_STATE" "$BOUT/states/"
}


# ============================================================
# Figure 3A
# WAN sensitivity
#
# 125 -> 500，步长 62.5
# 7 个均匀数据点
# ============================================================

echo
echo "============================================================"
echo "[1/4] Dense WAN sensitivity"
echo "============================================================"

for WAN in 125 187.5 250 312.5 375 437.5 500
do
    TAG=$(echo "$WAN" | sed 's/\./p/g')

    SDIR="$DENSE/wan/wan${TAG}/seed1"
    BOUT="$SDIR/baselines"

    echo
    echo "===== WAN = $WAN MB/s ====="

    prepare_state "$BOUT"

    run_baselines \
      "$BOUT" \
      "$CASE40" \
      "$WAN" \
      "$PEER_DEFAULT" \
      "$REG_LAT" \
      "$INTER_LAT" \
      "$INTRA_LAT"

    run_cider \
      "$BOUT" \
      "$CASE40" \
      "$WAN" \
      "$PEER_DEFAULT" \
      "$REG_LAT" \
      "$INTER_LAT" \
      "$INTRA_LAT" \
      full \
      "$KEEP_V" \
      "$SDIR/cider"
done


# ============================================================
# Figure 3B / Figure 4A
# Peer upload sensitivity
#
# 12.5 -> 50，步长 6.25
# 7 个均匀数据点
# ============================================================

echo
echo "============================================================"
echo "[2/4] Dense peer-upload sensitivity"
echo "============================================================"

for PEER in 12.5 18.75 25 31.25 37.5 43.75 50
do
    TAG=$(echo "$PEER" | sed 's/\./p/g')

    SDIR="$DENSE/peer/peer${TAG}/seed1"
    BOUT="$SDIR/baselines"

    echo
    echo "===== Peer Upload = $PEER MB/s ====="

    prepare_state "$BOUT"

    run_baselines \
      "$BOUT" \
      "$CASE40" \
      "$WAN_DEFAULT" \
      "$PEER" \
      "$REG_LAT" \
      "$INTER_LAT" \
      "$INTRA_LAT"

    run_cider \
      "$BOUT" \
      "$CASE40" \
      "$WAN_DEFAULT" \
      "$PEER" \
      "$REG_LAT" \
      "$INTER_LAT" \
      "$INTRA_LAT" \
      full \
      "$KEEP_V" \
      "$SDIR/cider"
done


# ============================================================
# Figure 3C / Figure 4B
# RTT sensitivity
#
# multiplier:
# 0.50 0.75 1.00 1.25 1.50 1.75 2.00
#
# 所有传播时延同比例变化
# ============================================================

echo
echo "============================================================"
echo "[3/4] Dense RTT sensitivity"
echo "============================================================"

for SPEC in \
  "0p5:20:5:1" \
  "0p75:30:7.5:1.5" \
  "1:40:10:2" \
  "1p25:50:12.5:2.5" \
  "1p5:60:15:3" \
  "1p75:70:17.5:3.5" \
  "2:80:20:4"
do
    IFS=: read -r TAG RLAT ILAT INLAT <<< "$SPEC"

    SDIR="$DENSE/rtt/rtt${TAG}/seed1"
    BOUT="$SDIR/baselines"

    echo
    echo "===== RTT multiplier = $TAG ====="
    echo "registry=$RLAT ms, inter=$ILAT ms, intra=$INLAT ms"

    prepare_state "$BOUT"

    run_baselines \
      "$BOUT" \
      "$CASE40" \
      "$WAN_DEFAULT" \
      "$PEER_DEFAULT" \
      "$RLAT" \
      "$ILAT" \
      "$INLAT"

    run_cider \
      "$BOUT" \
      "$CASE40" \
      "$WAN_DEFAULT" \
      "$PEER_DEFAULT" \
      "$RLAT" \
      "$ILAT" \
      "$INLAT" \
      full \
      "$KEEP_V" \
      "$SDIR/cider"
done


# ============================================================
# Figure KEEP-V
#
# V 跨数量级，不建议使用线性等差；
# 补到 8 个点，画图时用 log-x 或等距 category label。
# ============================================================

echo
echo "============================================================"
echo "[4/4] Dense KEEP-V sensitivity"
echo "============================================================"

for V in 3e6 5e6 1e7 2e7 3e7 5e7 7e7 1e8
do
    echo
    echo "===== KEEP V = $V ====="

    run_cider \
      "$BASE40" \
      "$CASE40" \
      "$WAN_DEFAULT" \
      "$PEER_DEFAULT" \
      "$REG_LAT" \
      "$INTER_LAT" \
      "$INTRA_LAT" \
      full \
      "$V" \
      "$DENSE/keep/V_${V}/seed1/cider"
done


echo
echo "============================================================"
echo "ALL DENSE EXPERIMENTS FINISHED"
echo "Output: $DENSE"
echo "============================================================"

touch "$DENSE/FINISHED"
