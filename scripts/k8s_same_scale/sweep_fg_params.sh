#!/usr/bin/env bash
set -euo pipefail

ROOT=results/drtp/k8s_same_scale/sweep/fg_params
mkdir -p "$ROOT" results/drtp/k8s_same_scale/logs

REQS=(200 500 1000)

FRAGS=(0 0.5 1 2 5 10)
AFFS=(0 0.05 0.1 0.2 0.4)
GLFS=(0.8 0.9 1.0)
INITS=(image_resource arrival resource_asc)

for INIT in "${INITS[@]}"
do
  for FRAG in "${FRAGS[@]}"
  do
    for AFF in "${AFFS[@]}"
    do
      for GLF in "${GLFS[@]}"
      do
        TAG=init${INIT}_frag${FRAG}_aff${AFF}_glf${GLF}

        for REQ in "${REQS[@]}"
        do
          CASE=cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_${REQ}.json
          OUT=$ROOT/${TAG}_req${REQ}.json
          LOG=results/drtp/k8s_same_scale/logs/sweep_${TAG}_req${REQ}.log

          if [ ! -f "$CASE" ]; then
            echo "[MISSING] $CASE"
            continue
          fi

          echo "[SWEEP] $TAG req=$REQ"

          FG_LAMBDA_FRAG="$FRAG" \
          FG_LAMBDA_AFF="$AFF" \
          FG_GREEDY_LOAD_FACTOR="$GLF" \
          FG_INIT_ORDER="$INIT" \
          /usr/bin/time -v scripts/k8s_same_scale/run_one.sh fg "$CASE" "$OUT" \
            > "$LOG" 2>&1
        done
      done
    done
  done
done

python3 - <<'PY'
import json
import re
from pathlib import Path
from collections import defaultdict

ROOT = Path("results/drtp/k8s_same_scale/sweep/fg_params")
acc = defaultdict(lambda: {
    "n": 0,
    "obj_k8s": 0.0,
    "obj_base": 0.0,
    "ca": 0.0,
    "ca_rate": 0.0,
    "downloaded": 0.0,
    "reuse": 0.0,
})

for p in ROOT.glob("*.json"):
    r = json.load(open(p, "r", encoding="utf-8"))
    s = r["summary"]

    name = p.stem
    tag = re.sub(r"_req\d+$", "", name)

    obj_k8s = float(s.get("objective_ca", s.get("objective", 0.0)))
    obj_base = float(s.get("objective_without_ca_penalty", s.get("objective_base", obj_k8s)))
    ca = float(s.get("ca_triggered", s.get("num_failed", 0)))
    ca_rate = float(s.get("ca_rate", s.get("fail_rate", 0.0)))

    acc[tag]["n"] += 1
    acc[tag]["obj_k8s"] += obj_k8s
    acc[tag]["obj_base"] += obj_base
    acc[tag]["ca"] += ca
    acc[tag]["ca_rate"] += ca_rate
    acc[tag]["downloaded"] += float(s.get("downloaded_mb", 0.0))
    acc[tag]["reuse"] += float(s.get("reuse_rate", 0.0))

rows = []
for tag, v in acc.items():
    if v["n"] == 0:
        continue
    n = v["n"]
    rows.append([
        tag,
        v["obj_k8s"] / n,
        v["obj_base"] / n,
        v["ca"] / n,
        v["ca_rate"] / n,
        v["downloaded"] / n,
        v["reuse"] / n,
        n,
    ])

rows.sort(key=lambda x: (x[1], x[3]))

out_md = Path("results/drtp/k8s_same_scale/sweep/fg_param_sweep_summary.md")
out_env = Path("results/drtp/k8s_same_scale/sweep/best_fg_params.env")

with open(out_md, "w", encoding="utf-8") as f:
    f.write("| rank | tag | avg_Obj_K8s | avg_Obj_base | avg_CA | avg_CA_rate | avg_downloaded | avg_reuse | n |\n")
    f.write("|---:|---|---:|---:|---:|---:|---:|---:|---:|\n")
    for i, x in enumerate(rows[:30], 1):
        f.write(
            f"| {i} | {x[0]} | {x[1]:.3f} | {x[2]:.3f} | {x[3]:.3f} | {x[4]:.4f} | {x[5]:.1f} | {x[6]:.6f} | {x[7]} |\n"
        )

best = rows[0][0]
m = re.match(r"init(.+)_frag(.+)_aff(.+)_glf(.+)", best)
if not m:
    raise RuntimeError(best)

init, frag, aff, glf = m.groups()

with open(out_env, "w", encoding="utf-8") as f:
    f.write(f"export FG_INIT_ORDER={init}\n")
    f.write(f"export FG_LAMBDA_FRAG={frag}\n")
    f.write(f"export FG_LAMBDA_AFF={aff}\n")
    f.write(f"export FG_GREEDY_LOAD_FACTOR={glf}\n")

print("written", out_md)
print("written", out_env)
print("best =", best)
print(open(out_env, encoding="utf-8").read())
PY
