#!/usr/bin/env bash
set -u

cd ~/FG-DSCR
source ~/miniconda3/etc/profile.d/conda.sh
conda activate fg

CASE_LIST=results/drtp/case_lists/k8sca_stress_1024_cases.txt
LOG=results/drtp/final_exp_logs/online_k8sca_fgpack_sweep.log

mkdir -p results/drtp/final_exp_logs
: > "$LOG"

cat > /tmp/fgpack_configs.txt <<'EOF'
fg_cur|--lambda-cong 1.0 --lambda-frag 0.1 --lambda-aff 0.2 --lambda-task-load 0.03 --theta-cong-count 0.0 --greedy-load-factor 0.0
fg_frag3|--lambda-cong 1.0 --lambda-frag 3.0 --lambda-aff 0.2 --lambda-task-load 0.03 --theta-cong-count 0.0 --greedy-load-factor 0.0
fg_frag10|--lambda-cong 1.0 --lambda-frag 10.0 --lambda-aff 0.2 --lambda-task-load 0.03 --theta-cong-count 0.0 --greedy-load-factor 0.0
fg_pack_bal|--lambda-cong 1.0 --lambda-frag 5.0 --lambda-aff 0.2 --lambda-task-load 0.5 --theta-cong-count 1.0 --greedy-load-factor 1.0
fg_pack_heavy|--lambda-cong 0.5 --lambda-frag 10.0 --lambda-aff 0.1 --lambda-task-load 1.0 --theta-cong-count 1.0 --greedy-load-factor 2.0
fg_reuse_pack|--lambda-cong 1.0 --lambda-frag 5.0 --lambda-aff 1.0 --lambda-task-load 0.5 --theta-cong-count 1.0 --greedy-load-factor 1.0
fg_lowcong_pack|--lambda-cong 0.1 --lambda-frag 10.0 --lambda-aff 0.2 --lambda-task-load 1.0 --theta-cong-count 1.0 --greedy-load-factor 2.0
EOF

echo "[START] $(date)" | tee -a "$LOG"

while IFS='|' read -r NAME EXTRA_ARGS
do
  [ -n "$NAME" ] || continue

  OUT_DIR="results/drtp/final_exp/online_k8sca_stress1024_${NAME}"
  mkdir -p "$OUT_DIR"

  echo "############################################################" | tee -a "$LOG"
  echo "[CONFIG] $NAME" | tee -a "$LOG"
  echo "ARGS=$EXTRA_ARGS" | tee -a "$LOG"
  echo "############################################################" | tee -a "$LOG"

  while read CASE
  do
    [ -n "$CASE" ] || continue
    BASE=$(basename "$CASE")

    echo "[RUN][$NAME] $BASE" | tee -a "$LOG"

    python3 -u tools/run_online_k8sca.py \
      --case "$CASE" \
      --out "$OUT_DIR/$BASE" \
      --algo fg \
      --ca-penalty 1000 \
      --ams-mode avg \
      --queue-order cache \
      $EXTRA_ARGS \
      2>&1 | tee -a "$LOG"

    RET=${PIPESTATUS[0]}
    if [ "$RET" -ne 0 ]; then
      echo "[FAILED][$NAME] $CASE ret=$RET" | tee -a "$LOG"
      echo "$NAME $CASE" >> results/drtp/final_exp_logs/online_k8sca_fgpack_sweep_failed.txt
    fi
  done < "$CASE_LIST"

done < /tmp/fgpack_configs.txt

echo "[DONE] $(date)" | tee -a "$LOG"
