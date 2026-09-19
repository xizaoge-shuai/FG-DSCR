#!/usr/bin/env bash
set -u

cd ~/FG-DSCR
source ~/miniconda3/etc/profile.d/conda.sh
conda activate fg

CASE_LIST=results/drtp/case_lists/normal88_cache1024_req200_1000_cases.txt
LOG=results/drtp/final_exp_logs/k8s_ca_baselines_old9.log

mkdir -p results/drtp/final_exp_logs
: > "$LOG"

if [ ! -f "$CASE_LIST" ]; then
  mkdir -p results/drtp/case_lists
  rm -f "$CASE_LIST"
  for REQ in 200 300 400 500 600 700 800 900 1000
  do
    echo "cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_${REQ}.json" >> "$CASE_LIST"
  done
fi

ALGOS="ilrsa lrscheduler gahrl orr lasa"

echo "[START] $(date)" | tee -a "$LOG"
echo "[CASE_LIST] $CASE_LIST cases=$(wc -l < "$CASE_LIST")" | tee -a "$LOG"

for ALG in $ALGOS
do
  OUT_DIR="results/drtp/final_exp/overall_normal88_k8s_ca_${ALG}"
  mkdir -p "$OUT_DIR"

  echo "" | tee -a "$LOG"
  echo "############################################################" | tee -a "$LOG"
  echo "[ALGO] $ALG" | tee -a "$LOG"
  echo "############################################################" | tee -a "$LOG"

  while read CASE
  do
    [ -n "$CASE" ] || continue
    BASE=$(basename "$CASE")
    OUT="$OUT_DIR/$BASE"

    echo "============================================================" | tee -a "$LOG"
    echo "[RUN][$ALG] $BASE" | tee -a "$LOG"
    echo "OUT=$OUT" | tee -a "$LOG"

    python3 -u tools/run_k8s_ca_baseline.py \
      --case "$CASE" \
      --out "$OUT" \
      --algo "$ALG" \
      --ca-penalty 1000 \
      --order-policy cache_greedy \
      2>&1 | tee -a "$LOG"

    RET=${PIPESTATUS[0]}
    if [ "$RET" -ne 0 ]; then
      echo "[FAILED][$ALG] $CASE return_code=$RET" | tee -a "$LOG"
      echo "$ALG $CASE" >> results/drtp/final_exp_logs/k8s_ca_baselines_old9_failed.txt
    fi
  done < "$CASE_LIST"
done

echo "[DONE] $(date)" | tee -a "$LOG"
