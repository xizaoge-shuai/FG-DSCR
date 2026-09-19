#!/usr/bin/env bash
set -u

cd ~/FG-DSCR
source ~/miniconda3/etc/profile.d/conda.sh
conda activate fg

CASE_LIST=results/drtp/case_lists/normal88_cache1024_req200_1000_cases.txt
mkdir -p results/drtp/final_exp_logs

for MODE in max_serial avg_serial max_delay300
do
  LOG="results/drtp/final_exp_logs/online_k8sca_old9_${MODE}.log"
  : > "$LOG"

  for ALG in fg ilrsa lrscheduler gahrl orr lasa
  do
    mkdir -p "results/drtp/final_exp/online_k8sca_old9_${MODE}_${ALG}"
  done

  while read CASE
  do
    [ -n "$CASE" ] || continue
    BASE=$(basename "$CASE")

    for ALG in fg ilrsa lrscheduler gahrl orr lasa
    do
      EXTRA_ARGS=""

      if [ "$MODE" = "max_serial" ]; then
        EXTRA_ARGS="--ams-mode max --ca-serial"
      elif [ "$MODE" = "avg_serial" ]; then
        EXTRA_ARGS="--ams-mode avg --ca-serial"
      elif [ "$MODE" = "max_delay300" ]; then
        EXTRA_ARGS="--ams-mode max --ca-extra-delay 300"
      fi

      echo "[RUN][$MODE][$ALG] $BASE" | tee -a "$LOG"

      python3 -u tools/run_online_k8sca.py \
        --case "$CASE" \
        --out "results/drtp/final_exp/online_k8sca_old9_${MODE}_${ALG}/$BASE" \
        --algo "$ALG" \
        --ca-penalty 1000 \
        --queue-order cache \
        $EXTRA_ARGS \
        2>&1 | tee -a "$LOG"
    done
  done < "$CASE_LIST"
done
