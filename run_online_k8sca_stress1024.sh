#!/usr/bin/env bash
set -u

cd ~/FG-DSCR
source ~/miniconda3/etc/profile.d/conda.sh
conda activate fg

CASE_LIST=results/drtp/case_lists/k8sca_stress_1024_cases.txt
LOG=results/drtp/final_exp_logs/online_k8sca_stress1024.log

mkdir -p results/drtp/final_exp_logs
: > "$LOG"

for ALG in fg ilrsa lrscheduler gahrl orr lasa
do
  mkdir -p "results/drtp/final_exp/online_k8sca_stress1024_${ALG}"
done

echo "[START] $(date)" | tee -a "$LOG"

while read CASE
do
  [ -n "$CASE" ] || continue
  BASE=$(basename "$CASE")

  for ALG in fg ilrsa lrscheduler gahrl orr lasa
  do
    echo "============================================================" | tee -a "$LOG"
    echo "[RUN][$ALG] $BASE" | tee -a "$LOG"

    python3 -u tools/run_online_k8sca.py \
      --case "$CASE" \
      --out "results/drtp/final_exp/online_k8sca_stress1024_${ALG}/$BASE" \
      --algo "$ALG" \
      --ca-penalty 1000 \
      --ams-mode avg \
      --queue-order cache \
      2>&1 | tee -a "$LOG"

    RET=${PIPESTATUS[0]}
    if [ "$RET" -ne 0 ]; then
      echo "[FAILED][$ALG] $CASE ret=$RET" | tee -a "$LOG"
      echo "$ALG $CASE" >> results/drtp/final_exp_logs/online_k8sca_stress1024_failed.txt
    fi
  done
done < "$CASE_LIST"

echo "[DONE] $(date)" | tee -a "$LOG"
