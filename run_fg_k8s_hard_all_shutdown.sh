#!/usr/bin/env bash

cd ~/FG-DSCR
source ~/miniconda3/etc/profile.d/conda.sh
conda activate fg

mkdir -p results/drtp/case_lists
mkdir -p results/drtp/final_exp_logs
mkdir -p results/drtp/final_exp/overall_normal88_k8s_hard_fg
mkdir -p results/drtp/final_exp/cacheonly_fg_k8s_hard_full81

LOG=results/drtp/final_exp_logs/fg_k8s_hard_all_shutdown.log
: > "$LOG"

echo "[START] $(date)" | tee -a "$LOG"
echo "[INFO] baseline will NOT be rerun." | tee -a "$LOG"
echo "[INFO] only run FG-DSCR-GC-K8sHard." | tee -a "$LOG"

# ============================================================
# 0. 确认 K8s-hard 脚本存在
# ============================================================
if [ ! -f scripts/fg_dscr_k8s_hard.py ]; then
  echo "[ERROR] scripts/fg_dscr_k8s_hard.py not found." | tee -a "$LOG"
  echo "[ERROR] Please create/patched it first." | tee -a "$LOG"
  exit 1
fi

python3 -m py_compile scripts/fg_dscr_k8s_hard.py 2>&1 | tee -a "$LOG"
if [ "${PIPESTATUS[0]}" -ne 0 ]; then
  echo "[ERROR] py_compile failed." | tee -a "$LOG"
  exit 1
fi

# ============================================================
# 1. old overall normal88: cache=1024, req=200~1000
# ============================================================
OLD_CASE_LIST=results/drtp/case_lists/normal88_cache1024_req200_1000_cases.txt
rm -f "$OLD_CASE_LIST"

for REQ in 200 300 400 500 600 700 800 900 1000
do
  CASE="cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_${REQ}.json"
  if [ -f "$CASE" ]; then
    echo "$CASE" >> "$OLD_CASE_LIST"
  else
    echo "[MISSING OLD CASE] $CASE" | tee -a "$LOG"
  fi
done

echo "[INFO] old overall cases = $(wc -l < "$OLD_CASE_LIST")" | tee -a "$LOG"

# ============================================================
# 2. full81 case list
# ============================================================
FULL81_CASE_LIST=results/drtp/case_lists/cacheonly_full81_cases.txt

if [ ! -f "$FULL81_CASE_LIST" ]; then
  echo "[WARN] $FULL81_CASE_LIST not found, regenerate it from cases/drtp_cache_only_sweep_88" | tee -a "$LOG"

  find cases/drtp_cache_only_sweep_88 -type f -name "drtp_img88_cacheonly_*mb_*.json" \
    | grep -E "drtp_img88_cacheonly_(0|128|256|384|512|640|768|896|1024)mb_(200|300|400|500|600|700|800|900|1000)\.json$" \
    | sort > "$FULL81_CASE_LIST"
fi

echo "[INFO] full81 cases = $(wc -l < "$FULL81_CASE_LIST")" | tee -a "$LOG"

# ============================================================
# 3. 运行函数：失败不中断，记录失败 case
# ============================================================
run_one_case () {
  local CASE="$1"
  local OUT_DIR="$2"
  local TAG="$3"

  local BASE
  BASE=$(basename "$CASE")
  local OUT="$OUT_DIR/$BASE"

  echo "" | tee -a "$LOG"
  echo "================================================================================" | tee -a "$LOG"
  echo "[RUN][$TAG] $BASE" | tee -a "$LOG"
  echo "CASE=$CASE" | tee -a "$LOG"
  echo "OUT=$OUT" | tee -a "$LOG"
  echo "================================================================================" | tee -a "$LOG"

  python3 -u scripts/fg_dscr_k8s_hard.py \
    --case "$CASE" \
    --out "$OUT" \
    --beam 4 \
    --lambda-cong 1.0 \
    --lambda-frag 0.1 \
    --lambda-aff 0.2 \
    --lambda-task-load 0.03 \
    --cache-policy pgdsf \
    --order-policy dynamic_state \
    --algo-name FG-DSCR-GC-K8sHard \
    2>&1 | tee -a "$LOG"

  RET=${PIPESTATUS[0]}

  if [ "$RET" -ne 0 ]; then
    echo "[FAILED][$TAG] $BASE return_code=$RET" | tee -a "$LOG"
    echo "$CASE" >> "results/drtp/final_exp_logs/fg_k8s_hard_failed_cases.txt"
  else
    echo "[OK][$TAG] $BASE" | tee -a "$LOG"
  fi
}

# ============================================================
# 4. 跑 old overall 9 cases
# ============================================================
echo "" | tee -a "$LOG"
echo "##############################" | tee -a "$LOG"
echo "[PHASE] Run old overall 9 cases" | tee -a "$LOG"
echo "##############################" | tee -a "$LOG"

while read CASE
do
  [ -n "$CASE" ] || continue
  run_one_case "$CASE" "results/drtp/final_exp/overall_normal88_k8s_hard_fg" "old9"
done < "$OLD_CASE_LIST"

# ============================================================
# 5. 跑 full81 cases
# ============================================================
echo "" | tee -a "$LOG"
echo "##############################" | tee -a "$LOG"
echo "[PHASE] Run full81 cases" | tee -a "$LOG"
echo "##############################" | tee -a "$LOG"

while read CASE
do
  [ -n "$CASE" ] || continue
  run_one_case "$CASE" "results/drtp/final_exp/cacheonly_fg_k8s_hard_full81" "full81"
done < "$FULL81_CASE_LIST"

# ============================================================
# 6. 汇总结果
# ============================================================
echo "" | tee -a "$LOG"
echo "##############################" | tee -a "$LOG"
echo "[SUMMARY] finished files" | tee -a "$LOG"
echo "##############################" | tee -a "$LOG"

echo "old9 completed = $(find results/drtp/final_exp/overall_normal88_k8s_hard_fg -maxdepth 1 -name 'drtp_img88_cacheonly_*.json' 2>/dev/null | wc -l)" | tee -a "$LOG"
echo "full81 completed = $(find results/drtp/final_exp/cacheonly_fg_k8s_hard_full81 -maxdepth 1 -name 'drtp_img88_cacheonly_*.json' 2>/dev/null | wc -l)" | tee -a "$LOG"

echo "" | tee -a "$LOG"
echo "[FAILED CASES]" | tee -a "$LOG"
if [ -f results/drtp/final_exp_logs/fg_k8s_hard_failed_cases.txt ]; then
  sort -u results/drtp/final_exp_logs/fg_k8s_hard_failed_cases.txt | tee -a "$LOG"
else
  echo "none" | tee -a "$LOG"
fi

echo "" | tee -a "$LOG"
echo "[DONE] $(date)" | tee -a "$LOG"

# ============================================================
# 7. 自动关机
# ============================================================
echo "[SHUTDOWN] machine will power off in 60 seconds." | tee -a "$LOG"
sudo shutdown -h +1
