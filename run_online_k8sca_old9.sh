#!/usr/bin/env bash
set -u

cd ~/FG-DSCR
source ~/miniconda3/etc/profile.d/conda.sh
conda activate fg

mkdir -p results/drtp/case_lists
mkdir -p results/drtp/final_exp_logs

LOG=results/drtp/final_exp_logs/online_k8sca_old9.log
: > "$LOG"

echo "[START] $(date)" | tee -a "$LOG"
echo "[PWD] $(pwd)" | tee -a "$LOG"
echo "[PYTHON] $(which python3)" | tee -a "$LOG"

CASE_LIST=results/drtp/case_lists/normal88_cache1024_req200_1000_cases.txt

rm -f "$CASE_LIST"
for REQ in 200 300 400 500 600 700 800 900 1000
do
  CASE="cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_${REQ}.json"
  if [ -f "$CASE" ]; then
    echo "$CASE" >> "$CASE_LIST"
  else
    echo "[MISSING CASE] $CASE" | tee -a "$LOG"
  fi
done

echo "[CASE_LIST] $CASE_LIST cases=$(wc -l < "$CASE_LIST")" | tee -a "$LOG"

for ALG in fg ilrsa lrscheduler gahrl orr lasa
do
  mkdir -p "results/drtp/final_exp/online_k8sca_old9_${ALG}"
done

while read CASE
do
  [ -n "$CASE" ] || continue
  BASE=$(basename "$CASE")

  for ALG in fg ilrsa lrscheduler gahrl orr lasa
  do
    OUT="results/drtp/final_exp/online_k8sca_old9_${ALG}/$BASE"

    echo "============================================================" | tee -a "$LOG"
    echo "[RUN][$ALG] $BASE" | tee -a "$LOG"
    echo "OUT=$OUT" | tee -a "$LOG"

    python3 -u tools/run_online_k8sca.py \
      --case "$CASE" \
      --out "$OUT" \
      --algo "$ALG" \
      --ca-penalty 1000 \
      --ams-mode avg \
      --queue-order cache \
      2>&1 | tee -a "$LOG"

    RET=${PIPESTATUS[0]}
    if [ "$RET" -ne 0 ]; then
      echo "[FAILED][$ALG] $CASE ret=$RET" | tee -a "$LOG"
      echo "$ALG $CASE" >> results/drtp/final_exp_logs/online_k8sca_old9_failed.txt
    else
      echo "[OK][$ALG] $BASE" | tee -a "$LOG"
    fi
  done

done < "$CASE_LIST"

echo "[DONE] $(date)" | tee -a "$LOG"
