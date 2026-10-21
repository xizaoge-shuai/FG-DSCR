#!/usr/bin/env bash

set -euo pipefail

cd "$(dirname "$0")/.."

export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1

ROOT="${REVIEW_ROOT:-results/reviewer_mechanism_$(date +%Y%m%d_%H%M%S)}"

mkdir -p "$ROOT"

echo "$ROOT" > results/LATEST_REVIEWER_MECHANISM

FORMAL=$(cat results/LATEST_FORMAL_DENSE_CORRECTED)

CASE="cases/cider_scale_dense_arrival_corrected/cider_nodes40_domains8_req800_weak.json"

NAME="cider_nodes40_domains8_req800_weak"

STATE="$FORMAL/scale/baselines/states/${NAME}__seed1.json"

test -f "$CASE" || {
    echo "ERROR: CASE NOT FOUND: $CASE"
    exit 2
}

# 若正式实验目录结构不同，则搜索真实 state
if [[ ! -f "$STATE" ]]; then

    STATE=$(
        find "$FORMAL" \
          -type f \
          -name "${NAME}__seed1.json" \
          -print -quit
    )

fi

test -f "$STATE" || {
    echo "ERROR: STATE NOT FOUND"
    exit 2
}

STATE_ROOT=$(dirname "$(dirname "$STATE")")

# 保存实验版本与输入校验信息
{
    echo "DATE=$(date -Is)"
    echo "GIT=$(git rev-parse HEAD 2>/dev/null || echo unknown)"
    echo "FORMAL=$FORMAL"
    echo "STATE=$STATE"
    echo "STATE_ROOT=$STATE_ROOT"

    sha256sum \
      "$STATE" \
      "$CASE" \
      secon_exp/cider/scout.py \
      secon_exp/cider_simulator.py

} > "$ROOT/manifest.txt"

# 固定 placement 与 workload 记录
python - "$STATE" "$ROOT" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

state = json.loads(
    Path(sys.argv[1]).read_text(encoding="utf-8")
)

def digest(x):
    return hashlib.sha256(
        json.dumps(
            x,
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()

result = {
    "placement_hash": digest(state["placement"]),
    "target_case_hash": digest(state["target_case"]),
    "warmup_requests": state.get("warmup_requests"),
    "target_requests": len(
        state["target_case"]["containers"]
    ),
}

(
    Path(sys.argv[2]) / "placement_audit.json"
).write_text(
    json.dumps(result, indent=2),
    encoding="utf-8",
)

print("STATE AUDIT:", result, flush=True)
PY


# ============================================================
# 全部实验固定配置
# ============================================================

COMMON=(

  --state-root "$STATE_ROOT"
  --cases "$CASE"
  --seed 1

  --wan 250

  --core-capacity 1000
  --domain-capacity 300
  --access-capacity 200
  --peer-upload 12.5

  --registry-latency-ms 40
  --inter-domain-latency-ms 10
  --intra-domain-latency-ms 2

  --pulse-budget-mb 2048
  --pulse-max-transfers 0
  --pulse-refill-ratio 0.5

  --scout-alpha 1
  --scout-beta 4
  --scout-gamma 0.5
  --scout-source-concurrency 0

  --keep-v 3e6
  --keep-upper-budget-mb-s 150

)


run_case() {

    local label="$1"
    local mode="$2"
    local abl="$3"
    local cache="$4"

    local out="$ROOT/$label"

    mkdir -p "$out"

    echo "[$(date -Is)] START $label" \
      | tee -a "$ROOT/progress.txt"

    if ! python -u -m scripts.review_mechanisms \
       --scout-mode "$mode" \
       --cache-policy "$cache" \
       --batch-passes 3 \
       --diagnostics "$out/review_diag.json" \
       -- \
       "${COMMON[@]}" \
       --ablation "$abl" \
       --out "$out" \
       > "$out/run.log" 2>&1
    then

        echo "FAILED $label" \
          | tee -a "$ROOT/progress.txt"

        tail -60 "$out/run.log"

        exit 1

    fi

    test -s "$out/raw.csv"

    echo "[$(date -Is)] DONE $label" \
      | tee -a "$ROOT/progress.txt"

}


# ============================================================
# 实验00：验证旧 SCOUT 与独立选源的等价程度
# ============================================================

run_case \
  00_audit_original \
  audit \
  full \
  keep


# 第一组必须复现旧正式结果，否则终止
python - "$ROOT/00_audit_original/raw.csv" <<'PY'
import csv
import sys

r = next(
    csv.DictReader(
        open(sys.argv[1], encoding="utf-8")
    )
)

mean = float(r["mean_ready_s"])
p95 = float(r["p95_ready_s"])

upper = (
    float(r["registry_mb"])
    + float(r["cross_domain_peer_mb"])
)

print(
    f"FORMAL REPRODUCTION: "
    f"mean={mean:.6f} "
    f"p95={p95:.6f} "
    f"upper={upper:.2f}",
    flush=True,
)

if (
    abs(mean - 10.880) > 0.05
    or abs(p95 - 52.682) > 0.05
    or abs(upper - 29541) > 5
):
    raise SystemExit(
        "STOP: baseline mismatches formal experiment"
    )
PY


# ============================================================
# 实验01：独立选源 + KEEP
# ============================================================

run_case \
  01_independent_keep \
  independent \
  full \
  keep


# ============================================================
# 实验02：Batch-SCOUT + KEEP
# ============================================================

run_case \
  02_batch_keep \
  batch \
  full \
  keep


# ============================================================
# 实验03：独立选源 + LRU
# ============================================================

run_case \
  03_independent_lru \
  independent \
  no_keep \
  keep


# ============================================================
# 实验04：Batch-SCOUT + LRU
# ============================================================

run_case \
  04_batch_lru \
  batch \
  no_keep \
  keep


# ============================================================
# 实验05：Batch-SCOUT + 预热频次缓存
# ============================================================

run_case \
  05_batch_warmup_lfu \
  batch \
  full \
  warmup_lfu


# ============================================================
# 汇总结果
# ============================================================

python - "$ROOT" <<'PY'
import csv
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])

rows = []

for directory in sorted(
    root.glob("[0-9][0-9]_*")
):

    data = next(
        csv.DictReader(
            (directory / "raw.csv").open(
                encoding="utf-8"
            )
        )
    )

    diag = json.loads(
        (
            directory / "review_diag.json"
        ).read_text(encoding="utf-8")
    )

    timing = diag["timings_ms"]["scout"]

    rows.append({
        "method": directory.name,
        "mean_s": data["mean_ready_s"],
        "p95_s": data["p95_ready_s"],

        "upper_mb": (
            float(data["registry_mb"])
            + float(data["cross_domain_peer_mb"])
        ),

        "peak_link_util": data["peak_link_utilization"],
        "p95_link_util": data["p95_link_utilization"],

        "scout_median_ms": timing["median_ms"],
        "scout_p95_ms": timing["p95_ms"],
        "scout_max_ms": timing["max_ms"],

        "batch_moves": diag["batch_moves"],
        "audit_differences": diag[
            "audit_differing_demands"
        ],
    })


with (
    root / "summary.csv"
).open(
    "w",
    newline="",
    encoding="utf-8",
) as f:

    writer = csv.DictWriter(
        f,
        fieldnames=list(rows[0]),
    )

    writer.writeheader()
    writer.writerows(rows)


print("\n========== SUMMARY ==========")

for r in rows:

    print(
        f"{r['method']:<28}"
        f"Mean={float(r['mean_s']):8.3f} "
        f"P95={float(r['p95_s']):8.3f} "
        f"Upper={float(r['upper_mb']):10.1f} "
        f"SCOUTmed={float(r['scout_median_ms']):8.3f}ms"
    )

print("SAVED:", root / "summary.csv")
PY

date -Is > "$ROOT/FINISHED.txt"

echo "ALL FINISHED: $ROOT"
