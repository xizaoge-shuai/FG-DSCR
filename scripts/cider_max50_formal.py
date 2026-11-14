#!/usr/bin/env python3
"""CIDER max-50 two-stage formal experiment suite."""
import argparse
import csv
import json
import shutil
import subprocess
from pathlib import Path

import scripts.cider_max200_formal as m

m.SCALES = [20, 30, 40, 50]
m.SHORT_BASELINES = list(m.BASELINES)

OLD_RQ1 = m.RQ1_ROOT
OLD_ROOT = m.ROOT

ap = argparse.ArgumentParser()
ap.add_argument("phase", choices=["A", "B"])
ap.add_argument(
    "task",
    choices=["prepare", "rq1", "rq2", "rq3", "rq4", "rq5", "all"],
)
args = ap.parse_args()

ROOT_A = m.PROJ / "results/cider_max50_stageA_20261114"
ROOT_B = m.PROJ / "results/cider_max50_stageB_20261114"

m.ROOT = ROOT_A if args.phase == "A" else ROOT_B
m.RQ1_ROOT = m.ROOT / "rq1"

m.ROOT.mkdir(parents=True, exist_ok=True)

pointer = m.PROJ / f"results/LATEST_CIDER_MAX50_{args.phase}"
pointer.write_text(str(m.ROOT.relative_to(m.PROJ)) + "\n")


def stamp():
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=m.PROJ,
        text=True,
    ).strip()

    p = m.ROOT / "CODE_SHA"

    if p.exists() and p.read_text().strip() != sha:
        raise RuntimeError("Git commit changed during experiment")

    p.write_text(sha + "\n")

    if args.phase == "B":
        old = ROOT_A / "CODE_SHA"

        if not old.exists() or old.read_text().strip() == sha:
            raise RuntimeError(
                "Stage B requires a different committed method version"
            )

        for i in (1, 2, 3, 4, 5):
            if not (ROOT_A / f"RQ{i}_DONE").exists():
                raise RuntimeError(f"Stage A RQ{i} is incomplete")

        keep = (m.PROJ / "secon_exp/cider/keep.py").read_text()

        if "CIDER_KEEP_FILL_SLACK_20261114" not in keep:
            raise RuntimeError("Stage B KEEP correction is missing")


def good_state(path, case, seed):
    if not path.exists():
        return False

    try:
        state = json.loads(path.read_text())
        return (
            int(state.get("seed", -1)) == seed
            and Path(state.get("case", "")).resolve() == case.resolve()
        )
    except Exception:
        return False


def clone_previous(case, seed, srcbase, dstbase, methods):
    src = m.state_file(case, seed, srcbase)
    dst = m.state_file(case, seed, dstbase)

    if not good_state(src, case, seed):
        return False

    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    raw = srcbase / "raw.csv"
    new_raw = dstbase / "raw.csv"

    if raw.exists() and not new_raw.exists():
        rows = [
            r for r in m.readcsv(raw)
            if r.get("baseline") in methods
            and r.get("case") == case.stem
            and int(r.get("seed", -1)) == seed
            and float(r.get("wan_mb_s", -1)) == 250.0
        ]

        if rows:
            new_raw.parent.mkdir(parents=True, exist_ok=True)

            with new_raw.open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)

    return True


def copy_old_cider(case, seed, src, dst):
    if not m.complete(src / "raw.csv"):
        return

    if m.complete(dst / "raw.csv"):
        return

    row = m.readcsv(src / "raw.csv")[0]

    if (
        row.get("case") == case.stem
        and int(row.get("seed", -1)) == seed
    ):
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / "raw.csv", dst / "raw.csv")


def prepare():
    if args.phase == "B":
        return

    from secon_exp.network.expand_case import expand_case_nodes

    for seed in m.SEEDS:
        source = (
            m.CASE_ROOT
            / f"base/seed{seed}/cider_base_40000.json"
        )

        if not source.exists():
            source = m.case_path(50, seed)

        base = json.loads(source.read_text())

        stage = (
            m.CASE_ROOT
            / f"seed{seed}/weak_scale_nodes_max50"
        )
        stage.mkdir(parents=True, exist_ok=True)

        generated = False

        for n in m.SCALES:
            case = m.case_path(n, seed)

            if case.exists():
                obj = json.loads(case.read_text())

                if (
                    len(obj["nodes"]) != n
                    or len(obj["containers"]) != n * 20
                ):
                    raise RuntimeError(f"Invalid case: {case}")

                continue

            obj = json.loads(json.dumps(base))
            obj["containers"] = obj["containers"][:n * 20]

            obj = expand_case_nodes(
                obj,
                num_nodes=n,
                num_domains=n // 5,
                seed=seed,
            )

            obj.setdefault("metadata", {})["num_domains"] = n // 5
            obj["metadata"]["max50_scale"] = {
                "nodes": n,
                "seed": seed,
            }

            (stage / case.name).write_text(
                json.dumps(obj, ensure_ascii=False)
            )

            generated = True

        if generated:
            m.run(
                [
                    m.PYTHON,
                    "-u",
                    "scripts/add_drtp_arrivals.py",
                    "--input-dir", stage,
                    "--output-dir",
                    m.CASE_ROOT / f"seed{seed}/weak_scale_arrival",
                    "--arrival-csv",
                    "data/stats/drtp_stats/minute_arrivals.csv",
                    "--window-minutes", 1440,
                    "--horizon-s", 240,
                    "--seed", seed,
                ],
                m.ROOT / f"logs/case_seed{seed}.log",
            )

        for n in m.SCALES:
            if not m.case_path(n, seed).exists():
                raise RuntimeError(
                    f"Missing N={n}, seed={seed}"
                )

    (m.ROOT / "CASES_READY").touch()


def run_one(
    case, seed, bout, cout, network,
    origbase=None, origcider=None,
    ablation="full", warmup_lfu=False,
):
    if args.phase == "A":
        if origbase:
            clone_previous(
                case, seed, origbase, bout,
                [
                    x for x in m.BASELINES
                    if x != "dragonfly_style"
                ],
            )

        m.baseline(
            case, seed, bout, m.BASELINES, network
        )

        if origcider:
            copy_old_cider(
                case, seed, origcider, cout
            )

        m.cider(
            case, seed, bout, cout, network,
            ablation=ablation,
            warmup_lfu=warmup_lfu,
        )

    else:
        srcbase = Path(
            str(bout).replace(
                str(ROOT_B), str(ROOT_A), 1
            )
        )

        srcstate = m.state_file(case, seed, srcbase)
        dststate = m.state_file(case, seed, bout)

        if not good_state(srcstate, case, seed):
            raise RuntimeError(
                f"Missing matching Stage A state: {srcstate}"
            )

        if not dststate.exists():
            dststate.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(srcstate, dststate)

        if not (bout / "raw.csv").exists():
            shutil.copy2(
                srcbase / "raw.csv",
                bout / "raw.csv",
            )

        if m.complete(cout / "raw.csv"):
            return

        cmd = m.cider_args(
            case, seed, bout, cout,
            network, ablation,
        )

        # Stage B: existing Batch-SCOUT pilot and
        # corrected KEEP implementation.
        cmd = [
            m.PYTHON, "-u", "-m",
            "scripts.review_mechanisms",
            "--scout-mode", "batch",
            "--batch-passes", 3,
            "--cache-policy",
            "warmup_lfu" if warmup_lfu else "keep",
            "--diagnostics",
            cout / "review_diag.json",
            "--",
            *cmd[4:],
        ]

        m.run(cmd, cout / "run.log")

        if not m.complete(cout / "raw.csv"):
            raise RuntimeError(
                f"Incomplete CIDER: {cout}"
            )


def rq1():
    for n in m.SCALES:
        for seed in m.SEEDS:
            case = m.case_path(n, seed)
            bout = m.RQ1_ROOT / f"seed{seed}/N{n}/baselines"
            cout = m.RQ1_ROOT / f"seed{seed}/N{n}/cider"

            oldb = (
                OLD_RQ1 / f"seed{seed}/N50/baselines"
                if n == 50 else None
            )
            oldc = (
                OLD_RQ1 / f"seed{seed}/N50/cider"
                if n == 50 else None
            )

            m.log("[RQ1]", n, seed)

            run_one(
                case, seed, bout, cout, m.net(),
                origbase=oldb,
                origcider=oldc,
            )

    (m.ROOT / "RQ1_DONE").touch()


def rq2():
    n = 50

    for seed in m.SEEDS:
        case = m.case_path(n, seed)
        bout = m.RQ1_ROOT / f"seed{seed}/N50/baselines"

        if not m.state_file(case, seed, bout).exists():
            raise RuntimeError("Finish RQ1 first")

        for method in (
            "full",
            "no_pulse",
            "no_scout",
            "no_keep",
            "warmup_frequency",
        ):
            cout = m.ROOT / f"rq2/seed{seed}/{method}"

            if method == "full" and args.phase == "A":
                copy_old_cider(
                    case, seed,
                    m.RQ1_ROOT / f"seed{seed}/N50/cider",
                    cout,
                )

            if m.complete(cout / "raw.csv"):
                continue

            actual_ablation = (
                "full" if method == "warmup_frequency"
                else method
            )

            m.log("[RQ2]", method, seed)

            if args.phase == "A":
                m.cider(
                    case, seed, bout, cout, m.net(),
                    ablation=actual_ablation,
                    warmup_lfu=(method == "warmup_frequency"),
                )
            else:
                run_one(
                    case, seed, bout, cout, m.net(),
                    ablation=actual_ablation,
                    warmup_lfu=(method == "warmup_frequency"),
                )

    (m.ROOT / "RQ2_DONE").touch()


def rq3():
    sweeps = (
        [
            ("wan_" + str(v).replace(".", "p"), m.net(wan=v))
            for v in (125, 187.5, 250, 312.5, 375)
        ]
        + [
            ("peer_" + str(v).replace(".", "p"), m.net(peer=v))
            for v in (12.5, 18.75, 25, 31.25, 37.5)
        ]
        + [
            ("rtt_" + str(v).replace(".", "p"), m.net(factor=v))
            for v in (0.5, 0.75, 1, 1.25, 1.5)
        ]
    )

    for tag, network in sweeps:
        for seed in m.SEEDS:
            case = m.case_path(50, seed)

            bout = (
                m.ROOT
                / f"rq3/{tag}/seed{seed}/baselines"
            )
            cout = (
                m.ROOT
                / f"rq3/{tag}/seed{seed}/cider"
            )

            shared = (
                m.RQ1_ROOT
                / f"seed{seed}/N50/baselines"
            )

            same_network = network == m.net()

            if args.phase == "A" and not same_network:
                src = m.state_file(case, seed, shared)
                dst = m.state_file(case, seed, bout)

                if good_state(src, case, seed) and not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)

            m.log("[RQ3]", tag, seed)

            run_one(
                case, seed, bout, cout, network,
                origbase=shared if same_network else None,
                origcider=(
                    m.RQ1_ROOT / f"seed{seed}/N50/cider"
                    if same_network else None
                ),
            )

    (m.ROOT / "RQ3_DONE").touch()


def rq4():
    for group, day, case in m.production_cases():
        for seed in m.SEEDS:
            bout = (
                m.ROOT
                / f"rq4/{group}/{day}/seed{seed}/baselines"
            )
            cout = (
                m.ROOT
                / f"rq4/{group}/{day}/seed{seed}/cider"
            )

            oldb = (
                OLD_ROOT
                / f"rq4/{group}/{day}/seed{seed}/baselines"
            )
            oldc = (
                OLD_ROOT
                / f"rq4/{group}/{day}/seed{seed}/cider"
            )

            m.log("[RQ4]", group, day, seed)

            run_one(
                case, seed, bout, cout, m.net(),
                origbase=oldb,
                origcider=oldc,
            )

    (m.ROOT / "RQ4_DONE").touch()


stamp()

if args.task in ("prepare", "all", "rq1"):
    prepare()

for task in ("rq1", "rq2", "rq5", "rq3", "rq4"):
    if args.task in (task, "all"):
        runner = globals().get(
            task,
            m.rq5 if task == "rq5" else None,
        )
        runner()

m.log("[DONE]", args.phase, args.task, m.ROOT)
