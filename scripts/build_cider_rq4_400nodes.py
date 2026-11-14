import copy
import json
from pathlib import Path
from collections import Counter

from secon_exp.network.expand_case import expand_case_nodes

NAME = "cider_nodes40_domains8_req800_weak.json"

IBM_ROOT = Path(
    "cases/cider_review77_20261109/test"
)

ALIBABA_ROOT = Path(
    "cases/cider_alibaba_robust_20261111_134205"
)

OUT_ROOT = Path(
    "cases/cider_rq4_400nodes_20261113"
)

SEEDS = [1, 2, 3]

# --------------------------------------------------
# 1. Discover source cases
# --------------------------------------------------

ibm_sources = sorted(
    IBM_ROOT.glob(
        f"day_*/arrival/{NAME}"
    )
)

alibaba_sources = sorted(
    ALIBABA_ROOT.glob(
        f"day_*/{NAME}"
    )
)

print("=" * 65)
print("RQ4 INPUT PREFLIGHT")
print("=" * 65)

print("IBM cases:", len(ibm_sources))
print("Alibaba cases:", len(alibaba_sources))

print("IBM root:", IBM_ROOT)
print("Alibaba root:", ALIBABA_ROOT)

if len(ibm_sources) != 15:
    raise RuntimeError(
        f"Expected 15 IBM cases, got {len(ibm_sources)}"
    )

if len(alibaba_sources) != 8:
    raise RuntimeError(
        f"Expected 8 Alibaba cases, got {len(alibaba_sources)}"
    )

# --------------------------------------------------
# 2. Validate and expand
# --------------------------------------------------

sources = (
    [("IBM", p) for p in ibm_sources]
    +
    [("Alibaba", p) for p in alibaba_sources]
)

success = 0
errors = []

for group, src in sources:

    if group == "IBM":
        day = src.parent.parent.name
    else:
        day = src.parent.name

    for seed in SEEDS:

        try:

            case = json.loads(
                src.read_text(
                    encoding="utf-8"
                )
            )

            assert len(case["nodes"]) == 40
            assert len(case["containers"]) == 800

            expanded = expand_case_nodes(
                case,
                num_nodes=400,
                num_domains=80,
                seed=seed,
            )

            counts = Counter(
                n["domain"]
                for n in expanded["nodes"]
            )

            assert len(expanded["nodes"]) == 400
            assert len(expanded["containers"]) == 800
            assert len(counts) == 80
            assert set(counts.values()) == {5}

            expanded.setdefault(
                "metadata", {}
            )["rq4_expansion"] = {
                "group": group,
                "day": day,
                "nodes": 400,
                "domains": 80,
                "requests": 800,
                "seed": seed,
                "source": str(src),
            }

            dst = (
                OUT_ROOT
                / f"seed{seed}"
                / group
                / day
                / "cider_nodes400_domains80_req800_weak.json"
            )

            dst.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            dst.write_text(
                json.dumps(
                    expanded,
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            success += 1

            print(
                f"[PASS] {group} {day} "
                f"seed={seed} -> {dst}",
                flush=True,
            )

        except Exception as e:

            errors.append(
                (group, day, seed, repr(e))
            )

            print(
                f"[ERROR] {group} {day} "
                f"seed={seed}: {e}",
                flush=True,
            )

# --------------------------------------------------
# 3. Final audit
# --------------------------------------------------

print()
print("=" * 65)
print("RQ4 CASE GENERATION SUMMARY")
print("=" * 65)

print("Expected:", 23 * 3)
print("Generated:", success)
print("Errors:", len(errors))

if errors:
    for item in errors:
        print("ERROR DETAIL:", item)

else:
    print("ALL CASES GENERATED SUCCESSFULLY")
