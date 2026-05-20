#!/usr/bin/env bash
set -euo pipefail

REQS=(200 300 400 500 600 700 800 900 1000)
METHODS=(fg wo_frag)

ROOT=results/drtp/k8s_ca_figs/fig1_phase1
mkdir -p "$ROOT"

for REQ in "${REQS[@]}"
do
  CASE=cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_${REQ}.json

  if [ ! -f "$CASE" ]; then
    echo "[MISSING] $CASE"
    continue
  fi

  for M in "${METHODS[@]}"
  do
    OUT=$ROOT/${M}_cache1024_req${REQ}.json
    LOG=results/drtp/k8s_ca_figs/logs/fig1_${M}_${REQ}.log

    echo "[FIG1] method=$M req=$REQ"
    /usr/bin/time -v scripts/k8s_ca_figs/run_one_k8s_ca.sh "$M" "$CASE" "$OUT" \
      > "$LOG" 2>&1
  done
done

python3 scripts/k8s_ca_figs/summarize_results.py \
  --root "$ROOT" \
  --out results/drtp/k8s_ca_figs/summaries/fig1_phase1_summary.csv

python3 - <<'PY'
import json
import re
from pathlib import Path

ROOT = Path("results/drtp/k8s_ca_figs/fig1_phase1")
OUT = Path("results/drtp/k8s_ca_figs/summaries/fig1_phase1_potential_terms.csv")
OUT.parent.mkdir(parents=True, exist_ok=True)

rows = []
for p in sorted(ROOT.glob("*.json")):
    r = json.load(open(p, "r", encoding="utf-8"))
    s = r.get("summary", {})
    hist = r.get("phase1_history", [])
    if not hist:
        continue

    last = hist[-1]
    comps = last.get("node_components", {})

    delay_term = sum(v.get("cong_term", 0.0) for v in comps.values())
    frag_term = sum(v.get("frag_term", 0.0) for v in comps.values())
    aff_reward_term = sum(v.get("aff_term", 0.0) for v in comps.values())
    load_term = sum(v.get("task_load_term", 0.0) for v in comps.values())

    m = re.search(r"req(\d+)", p.name)
    req = int(m.group(1)) if m else ""

    rows.append({
        "file": p.name,
        "method": s.get("algo", p.stem),
        "requests": req,
        "phi_total": last.get("potential", 0.0),
        "delay_term": delay_term,
        "frag_term": frag_term,
        "aff_reward_term": aff_reward_term,
        "load_term": load_term,
        "CA": s.get("ca_triggered", s.get("num_failed", 0)),
        "objective_CA": s.get("objective_ca", s.get("objective", 0.0)),
    })

cols = ["file","method","requests","phi_total","delay_term","frag_term","aff_reward_term","load_term","CA","objective_CA"]
with open(OUT, "w", encoding="utf-8") as f:
    f.write(",".join(cols) + "\n")
    for r in rows:
        f.write(",".join(str(r[c]) for c in cols) + "\n")

print("written", OUT, "rows", len(rows))
PY
