#!/usr/bin/env bash
set -euo pipefail

cd "$HOME/FG-DSCR"

ROOT=$(cat results/LATEST_FORMAL_CIDER_ALL)
EXTRA="$ROOT/dense_scale_load_20260929"

mkdir -p "$EXTRA"

export PYTHONHASHSEED=0
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"

SEED=1

WAN=250
PEER=12.5

CORE=1000
DOMAIN_CAP=300
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


# ============================================================
# 1. 重新生成 7 个严格 5 nodes/domain 的 weak-scale cases
#
# N = 20 25 30 35 40 45 50
# D =  4  5  6  7  8  9 10
# R = 400 500 600 700 800 900 1000
# ============================================================

SCALE_NODE_DIR="cases/cider_dense_scale_nodes"
SCALE_ARRIVAL_DIR="cases/cider_dense_scale_arrival"
TMP="$EXTRA/tmp_case_build"

rm -rf "$SCALE_NODE_DIR"
rm -rf "$SCALE_ARRIVAL_DIR"
rm -rf "$TMP"

mkdir -p "$SCALE_NODE_DIR"
mkdir -p "$TMP"

echo
echo "============================================================"
echo "BUILD DENSE SCALE NODE CASES"
echo "============================================================"

for SPEC in \
    "20 4 400" \
    "25 5 500" \
    "30 6 600" \
    "35 7 700" \
    "40 8 800" \
    "45 9 900" \
    "50 10 1000"
do
    set -- $SPEC

    N="$1"
    D="$2"
    R="$3"

    PREFIX="$TMP/base_n${N}"

    echo
    echo "===== BUILD N=$N domains=$D requests=$R ====="

    python scripts/build_fg_case_from_catalog_and_trace.py \
      --sizes "$R" \
      --num-nodes "$N" \
      --seed 1 \
      --out-prefix "$PREFIX"

    SRC="${PREFIX}_${R}.json"
    DST="$SCALE_NODE_DIR/cider_nodes${N}_domains${D}_req${R}_weak.json"

    if [ ! -f "$SRC" ]; then
        echo "ERROR: generated source case missing:"
        echo "$SRC"
        exit 1
    fi

    python - "$SRC" "$DST" "$N" "$D" "$R" <<'PY'
import copy
import json
import sys
from pathlib import Path

from secon_exp.network.expand_case import expand_case_nodes

src = Path(sys.argv[1])
dst = Path(sys.argv[2])

N = int(sys.argv[3])
D = int(sys.argv[4])
R = int(sys.argv[5])

case = json.loads(src.read_text(encoding="utf-8"))

case = expand_case_nodes(
    case,
    num_nodes=N,
    num_domains=D,
    seed=1,
)

case.setdefault("meta", {})
case["meta"]["num_nodes"] = N
case["meta"]["num_domains"] = D
case["meta"]["num_containers"] = R

assert len(case["containers"]) == R, (
    len(case["containers"]), R
)

assert len(case["nodes"]) == N, (
    len(case["nodes"]), N
)

dst.parent.mkdir(
    parents=True,
    exist_ok=True,
)

dst.write_text(
    json.dumps(
        case,
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)

print(
    f"SAVED: {dst} "
    f"N={N} domains={D} containers={R}"
)
PY

done


# ============================================================
# 2. 验证新 N=40 workload 与原正式 weak-scale workload 一致
#    只比较 layer + containers，不比较 meta 路径信息
# ============================================================

echo
echo "============================================================"
echo "VALIDATE N=40 CASE"
echo "============================================================"

python - <<'PY'
import json
from pathlib import Path

old = Path(
    "cases/cider_weak_scale_nodes/"
    "cider_nodes40_domains8_req800_weak.json"
)

new = Path(
    "cases/cider_dense_scale_nodes/"
    "cider_nodes40_domains8_req800_weak.json"
)

if not old.exists():
    raise SystemExit(
        f"ERROR: original case missing: {old}"
    )

a = json.loads(
    old.read_text(encoding="utf-8")
)

b = json.loads(
    new.read_text(encoding="utf-8")
)

print("old containers =", len(a["containers"]))
print("new containers =", len(b["containers"]))
print("old nodes      =", len(a["nodes"]))
print("new nodes      =", len(b["nodes"]))

same_layers = (
    a["layer_sizes_mb"]
    == b["layer_sizes_mb"]
)

same_containers = (
    a["containers"]
    == b["containers"]
)

print("same layer catalog =", same_layers)
print("same containers    =", same_containers)

if not same_layers or not same_containers:
    raise SystemExit(
        "ERROR: regenerated N40 workload differs "
        "from the original formal workload. "
        "STOP before experiments."
    )

print("PASS: N40 workload reproduction validated.")
PY


# ============================================================
# 3. 给 Scale cases 加 1x DRTP arrival
#    1x = horizon 240 s
# ============================================================

echo
echo "============================================================"
echo "ADD 1x ARRIVALS FOR SCALE"
echo "============================================================"

python scripts/add_drtp_arrivals.py \
  --input-dir "$SCALE_NODE_DIR" \
  --output-dir "$SCALE_ARRIVAL_DIR" \
  --arrival-csv data/stats/drtp_stats/minute_arrivals.csv \
  --window-minutes 1440 \
  --horizon-s 240 \
  --seed 1


# ============================================================
# 4. 生成 8 个均匀 Load cases
#
# load      horizon
# 0.5x       480
# 1.0x       240
# 1.5x       160
# 2.0x       120
# 2.5x        96
# 3.0x        80
# 3.5x        68.57142857
# 4.0x        60
# ============================================================

echo
echo "============================================================"
echo "BUILD DENSE LOAD CASES"
echo "============================================================"

for SPEC in \
    "0p5 480" \
    "1 240" \
    "1p5 160" \
    "2 120" \
    "2p5 96" \
    "3 80" \
    "3p5 68.57142857142857" \
    "4 60"
do
    set -- $SPEC

    TAG="$1"
    H="$2"

    OUT="cases/cider_dense_arrival_load_${TAG}x"

    rm -rf "$OUT"

    echo
    echo "===== LOAD=${TAG}x horizon=$H s ====="

    python scripts/add_drtp_arrivals.py \
      --input-dir "$SCALE_NODE_DIR" \
      --output-dir "$OUT" \
      --arrival-csv data/stats/drtp_stats/minute_arrivals.csv \
      --window-minutes 1440 \
      --horizon-s "$H" \
      --seed 1
done


# ============================================================
# 公共运行函数
# ============================================================

run_baselines()
{
    OUT="$1"
    CASE="$2"

    rm -rf "$OUT"
    mkdir -p "$OUT"

    python -u \
      -m secon_exp.experiments.run_scale_baselines_v2 \
      --cases "$CASE" \
      --seeds "$SEED" \
      --wan "$WAN" \
      --warmup-fraction 0.5 \
      --core-capacity "$CORE" \
      --domain-capacity "$DOMAIN_CAP" \
      --access-capacity "$ACCESS" \
      --peer-upload "$PEER" \
      --registry-latency-ms "$REG_LAT" \
      --inter-domain-latency-ms "$INTER_LAT" \
      --intra-domain-latency-ms "$INTRA_LAT" \
      --baselines \
        registry \
        dragonfly_style \
        peersync_style \
        metapipe_reactive_style \
        ilrsa_style \
      --out "$OUT"
}


run_cider()
{
    STATE="$1"
    CASE="$2"
    OUT="$3"

    rm -rf "$OUT"

    python -u \
      -m secon_exp.experiments.run_cider_mainline \
      --state-root "$STATE" \
      --cases "$CASE" \
      --seed "$SEED" \
      --wan "$WAN" \
      --core-capacity "$CORE" \
      --domain-capacity "$DOMAIN_CAP" \
      --access-capacity "$ACCESS" \
      --peer-upload "$PEER" \
      --registry-latency-ms "$REG_LAT" \
      --inter-domain-latency-ms "$INTER_LAT" \
      --intra-domain-latency-ms "$INTRA_LAT" \
      --pulse-budget-mb "$PULSE_BUDGET" \
      --pulse-max-transfers 0 \
      --pulse-refill-ratio "$PULSE_REFILL" \
      --scout-alpha "$SCOUT_ALPHA" \
      --scout-beta "$SCOUT_BETA" \
      --scout-gamma "$SCOUT_GAMMA" \
      --scout-source-concurrency 0 \
      --keep-v "$KEEP_V" \
      --keep-upper-budget-mb-s "$KEEP_BUDGET" \
      --ablation full \
      --out "$OUT"
}


# ============================================================
# 5. 跑 Scale
# ============================================================

echo
echo "============================================================"
echo "RUN DENSE SCALE"
echo "============================================================"

for SPEC in \
    "20 4 400" \
    "25 5 500" \
    "30 6 600" \
    "35 7 700" \
    "40 8 800" \
    "45 9 900" \
    "50 10 1000"
do
    set -- $SPEC

    N="$1"
    D="$2"
    R="$3"

    CASE="$SCALE_ARRIVAL_DIR/cider_nodes${N}_domains${D}_req${R}_weak.json"

    SDIR="$EXTRA/scale/N${N}/seed1"
    BOUT="$SDIR/baselines"

    echo
    echo "############################################################"
    echo "SCALE N=$N domains=$D requests=$R"
    echo "############################################################"

    if [ ! -f "$CASE" ]; then
        echo "ERROR missing case:"
        echo "$CASE"
        exit 1
    fi

    run_baselines \
      "$BOUT" \
      "$CASE"

    run_cider \
      "$BOUT" \
      "$CASE" \
      "$SDIR/cider"
done


# ============================================================
# 6. 跑 Load
#    只取 N=40 / 8 domains / 800 requests
# ============================================================

echo
echo "============================================================"
echo "RUN DENSE ARRIVAL LOAD"
echo "============================================================"

for TAG in \
    0p5 \
    1 \
    1p5 \
    2 \
    2p5 \
    3 \
    3p5 \
    4
do
    CASE="cases/cider_dense_arrival_load_${TAG}x/cider_nodes40_domains8_req800_weak.json"

    SDIR="$EXTRA/load/${TAG}x/seed1"
    BOUT="$SDIR/baselines"

    echo
    echo "############################################################"
    echo "LOAD=${TAG}x"
    echo "############################################################"

    if [ ! -f "$CASE" ]; then
        echo "ERROR missing case:"
        echo "$CASE"
        exit 1
    fi

    run_baselines \
      "$BOUT" \
      "$CASE"

    run_cider \
      "$BOUT" \
      "$CASE" \
      "$SDIR/cider"
done


touch "$EXTRA/FINISHED"

echo
echo "============================================================"
echo "ALL DENSE SCALE + LOAD EXPERIMENTS FINISHED"
echo "============================================================"
echo "RESULT = $EXTRA"
