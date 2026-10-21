import json
import runpy
import sys
from collections import Counter
from pathlib import Path

from secon_exp.cider.keep import KeepOptimizer
from scripts.review_keep_v2 import make_v2


out = Path(sys.argv[1]).resolve()
out.mkdir(parents=True, exist_ok=True)

base = Path(
    Path("results/LATEST_KEEP_CAPACITY_CORRECTED")
    .read_text().strip()
).resolve()

name = "cider_nodes40_domains8_req800_weak__cap_1p0x"
case = base / "cases" / f"{name}.json"

full_choice = make_v2("v2_full")
no_comm_choice = make_v2("v2_no_comm")

stats = Counter()
examples = []


def frequency_choice(self, kw):
    capacity = float(kw["capacity_mb"])
    sizes = kw["sizes"]

    candidates = sorted(
        set(kw["candidate_layers"]),
        key=lambda x: (
            -float(self.p_hat.get(x, 0.0)),
            str(x),
        ),
    )

    selected = set()
    used = 0.0

    for layer in candidates:
        size = float(sizes[layer])

        if used + size <= capacity + 1e-9:
            selected.add(layer)
            used += size

    return selected


def audited_choice(self, *args, **kw):
    if args:
        raise RuntimeError(
            "Expected keyword arguments in KEEP interface"
        )

    stats["calls"] += 1

    candidates = set(kw["candidate_layers"])
    capacity = float(kw["capacity_mb"])
    sizes = kw["sizes"]

    total_size = sum(
        float(sizes[x])
        for x in candidates
    )

    if total_size > capacity + 1e-9:
        stats["pressure_calls"] += 1
    else:
        stats["no_pressure_calls"] += 1

    full = full_choice(self, **kw)
    no_comm = no_comm_choice(self, **kw)
    frequency = frequency_choice(self, kw)

    if full != no_comm:
        stats["full_vs_no_comm_different"] += 1
        stats["full_vs_no_comm_changed_layers"] += len(
            full.symmetric_difference(no_comm)
        )

        if len(examples) < 5:
            examples.append({
                "node": kw["node"],
                "candidate_count": len(candidates),
                "capacity_mb": capacity,
                "full_only": sorted(full - no_comm),
                "no_comm_only": sorted(no_comm - full),
            })

    if full != frequency:
        stats["full_vs_frequency_different"] += 1

    if no_comm != frequency:
        stats["no_comm_vs_frequency_different"] += 1

    return full


KeepOptimizer.choose_retention = audited_choice

sys.argv = [
    "review_mechanisms.py",
    "--scout-mode", "independent",
    "--cache-policy", "keep",
    "--diagnostics", str(out / "review_diag.json"),
    "--",

    "--state-root", str(base / "states"),
    "--cases", str(case),
    "--seed", "1",

    "--wan", "250",
    "--core-capacity", "1000",
    "--domain-capacity", "300",
    "--access-capacity", "200",
    "--peer-upload", "12.5",

    "--registry-latency-ms", "40",
    "--inter-domain-latency-ms", "10",
    "--intra-domain-latency-ms", "2",

    "--pulse-budget-mb", "2048",
    "--pulse-max-transfers", "0",
    "--pulse-refill-ratio", "0.5",

    "--scout-alpha", "1",
    "--scout-beta", "4",
    "--scout-gamma", "0.5",
    "--scout-source-concurrency", "0",

    "--keep-v", "3e6",
    "--keep-upper-budget-mb-s", "150",

    "--ablation", "full",
    "--out", str(out),
]

runpy.run_module(
    "scripts.review_mechanisms",
    run_name="__main__",
)

result = {
    "stats": dict(stats),
    "examples": examples,
}

(out / "decision_audit.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

print("\n========== KEEP-v2 DECISION AUDIT ==========")

for key, value in stats.items():
    print(f"{key:<36} {value}")

print("Saved:", out / "decision_audit.json")
