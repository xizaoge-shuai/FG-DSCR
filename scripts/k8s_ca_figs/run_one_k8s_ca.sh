#!/usr/bin/env bash
set -euo pipefail

METHOD="$1"
CASE="$2"
OUT="$3"

mkdir -p "$(dirname "$OUT")"

LAMBDA_CA="${LAMBDA_CA:-1000}"

case "$METHOD" in
  ilrsa)
    python3 scripts/ilrsa_reference_impl_k8s_ca.py \
      --case "$CASE" \
      --out "$OUT" \
      --exact-threshold 12 \
      --knapsack exact \
      --lambda-ca "$LAMBDA_CA"
    ;;

  lrs)
    python3 scripts/run_lrscheduler_source_baseline_k8s_ca.py \
      --case "$CASE" \
      --out "$OUT" \
      --lambda-ca "$LAMBDA_CA"
    ;;

  gahrl)
    python3 scripts/run_gahrl_objective_greedy_k8s_ca.py \
      --case "$CASE" \
      --out "$OUT" \
      --order cache \
      --lambda-ca "$LAMBDA_CA"
    ;;

  orr)
    python3 scripts/run_recent_layer_baselines_k8s_ca.py \
      --case "$CASE" \
      --out "$OUT" \
      --algo orr \
      --order cache \
      --lambda-ca "$LAMBDA_CA"
    ;;

  lasa)
    python3 scripts/run_lasa_reimpl_k8s_ca.py \
      --case "$CASE" \
      --out "$OUT" \
      --enforce-resource-capacity \
      --lambda-ca "$LAMBDA_CA" \
      --algo-name LASA-reimpl-K8sCA
    ;;

  fg)
    python3 scripts/fg_dscr.py \
      --case "$CASE" \
      --out "$OUT" \
      --beam 1 \
      --phase1-neighbor-mode move \
      --max-best-response-rounds 5 \
      --move-topk-per-node 6 \
      --hard-resource-filter \
      --lambda-fail "$LAMBDA_CA" \
      --lambda-cong 1.0 \
      --lambda-frag 10 \
      --lambda-aff 0 \
      --k-pin 6 \
      --cache-policy pgdsf \
      --order-policy dynamic_state \
      --greedy-load-factor 1.0 \
      --init-order arrival \
      --algo-name FG-DSCR-GC-K8sCA-tuned
    ;;

  wo_frag)
    python3 scripts/fg_dscr.py \
      --case "$CASE" \
      --out "$OUT" \
      --beam 1 \
      --phase1-neighbor-mode move \
      --max-best-response-rounds 5 \
      --move-topk-per-node 6 \
      --hard-resource-filter \
      --lambda-fail "$LAMBDA_CA" \
      --lambda-cong 1.0 \
      --lambda-frag 0 \
      --lambda-aff 0 \
      --k-pin 6 \
      --cache-policy pgdsf \
      --order-policy dynamic_state \
      --greedy-load-factor 1.0 \
      --init-order arrival \
      --algo-name FG-w-o-frag-K8sCA
    ;;

  *)
    echo "[ERROR] unknown METHOD=$METHOD" >&2
    exit 1
    ;;
esac
