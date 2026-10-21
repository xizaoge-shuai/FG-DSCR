import json
import subprocess
import os
from pathlib import Path


CASES = [
    "cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_200.json",
    "cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_400.json",
    "cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_600.json",
    "cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_800.json",
    "cases/drtp_cache_only_sweep_88/drtp_img88_cacheonly_1024mb_1000.json",
]


ROUNDS=[1,3,5,10,20]


OUT=Path(
    "results/comnet_revision/convergence"
)

OUT.mkdir(parents=True,exist_ok=True)


for case in CASES:

    for r in ROUNDS:

        name=f"{Path(case).stem}_round{r}"

        cmd=[
            "python3",
            "scripts/fg_dscr.py",
            "--case",
            case,
            "--max-best-response-rounds",
            str(r),
            "--output",
            str(OUT/name+".json")
        ]


        print(
            "RUN:",
            " ".join(cmd)
        )

        subprocess.run(
            cmd,
            check=True
        )

print("DONE")
